"""Bounded graph nodes. Tool proposals are data, not executable code."""
import asyncio
import json
import re
from uuid import uuid4

from ..attachments.store import scope_key
from ..models import EvidenceItem, MedicalIntent
from ..privacy import redact_payload, redact_text
from ..providers import CALL_CONTEXT, EMERGENCY_RESPONSE, detect_emergency, detect_unsafe_medication_request
from ..retrieval.hybrid import _terms, reciprocal_rank_fusion, stable_evidence_id
from ..tools.contracts import DESCRIPTIONS, NETWORK_TOOLS, ToolAction, ToolArgs, ToolContext, ToolPlan
from ..tools.network import network_query
from ..tools.translation import glossary_plan
from ..tools.patient_sources import education_links
from ..tools.network import on_domain


class AgenticFlow:
    def __init__(self, owner):
        self.owner = owner

    async def attachments(self, state):
        aids = state.get("attachment_ids", [])
        scope = scope_key(state["session_id"], state.get("case_id"), state.get("user_id"))
        state["tool_context"] = ToolContext(scope=scope,
            network=state.get("network_mode") == "auto", attachment_ids=aids,
            max_calls=self.owner.gateway.settings.tool_max_calls if self.owner.gateway else 8,
            partitions=state.get("partitions", ["evidence"]))
        state["tool_context"].user_urls.update(re.findall(r"https?://[^\s<>\"，。]+", state["query"]))
        state["tool_context"].allowed_urls.update(state["tool_context"].user_urls)
        state['tool_context'].ledger = CALL_CONTEXT.get()
        if hasattr(self.owner.retriever, 'capture'):
            state['tool_context'].index_handle = self.owner.retriever.capture()
        state["attachment_context"], state["attachment_refs"], state["attachment_evidence"] = [], [], []
        if not aids:
            return state
        if not self.owner.attachments:
            from ..attachments.store import AttachmentError
            raise AttachmentError("attachment_service_unavailable", 503)
        items = self.owner.attachments.context(aids, scope)
        for item in items:
            for page in item["pages"]:
                text = page["text"]
                if item["purpose"] == "patient_context":
                    # Only a section explicitly describing current symptoms can trigger this gate.
                    lines = [s for s in text.splitlines() if re.search(r"主诉|现病史|目前|现在|正在|今日", s)]
                    hits = detect_emergency("\n".join(lines))
                    if hits:
                        state["result"] = self.owner._fixed_result(state, EMERGENCY_RESPONSE, MedicalIntent.EMERGENCY, True, ["emergency", "attachment_symptoms"] + hits)
                        return state
                    state["attachment_context"].append({"attachment_id": item["id"], "page": page["page"], "text": redact_text(text)[0]})
                    state["attachment_refs"].append({"attachment_id": item["id"], "page": page["page"], "version": item["version"], "purpose": "patient_context"})
                else:
                    for start in range(0, len(text), 1800):
                        chunk = redact_text(text[start:start + 2000])[0]
                        state["attachment_evidence"].append(EvidenceItem(
                            evidence_id=stable_evidence_id(item["id"], item["version"], str(page["page"]) + ":" + str(start), chunk),
                            source_id=item["id"], source_type="session_attachment_reference", title="用户提供的参考资料",
                            content=chunk, page=page["page"], source_sha256=item["sha256"], is_demo=False,
                            metadata={"partition": "session_evidence", "untrusted_input": True, "attachment_version": item["version"]}))
        return state

    async def retrieve(self, state):
        ctx = state["tool_context"]
        ctx.protected_terms = list(state['intent'].entities.drug)
        types = set(self.owner.retriever.source_types())
        route = state["route"]
        jobs = []
        targets=glossary_plan(state['rewritten_query'])
        queries=targets['local_queries'] if len(targets.get('goals', []))>1 and targets.get('local_queries') else state['subqueries']
        for q in queries[:2]:
            if route in {"medication_review", "combined_review"}:
                jobs.append(("lookup_drug", {"query": q}))
            if route in {"guideline_review", "combined_review"}:
                sources = [t for t in types if any(s in t for s in ("guideline", "clinical", "指南"))] or ["clinical_guideline"]
                jobs.append(("search_local", {"query": q, "source_types": sources}))
            if not jobs or route == "general_retrieval":
                jobs.append(("search_local", {"query": q}))
        if state.get("partitions") == ["benchmark"]:
            jobs = [("search_local", {"query": state["rewritten_query"]})]
        results = await asyncio.gather(*(self.owner.gateway.call(name, args, ctx, "按意图检索本地资料") for name, args in jobs[:4]))
        candidates = [[EvidenceItem.model_validate(row) for row in r.get("evidence", [])] for r in results]
        merged = reciprocal_rank_fusion(candidates)[:20]
        if not merged and route != "general_retrieval":
            result = await self.owner.gateway.call("search_local", {"query": state["rewritten_query"]}, ctx, "专科分区未找到，回退通用分区")
            merged = [EvidenceItem.model_validate(r) for r in result.get("evidence", [])]
        if state["intent"].intent == MedicalIntent.DRUG_INTERACTION:
            drugs = state["intent"].entities.drug
            ddi = await self.owner.gateway.call("lookup_ddi", {"drug_a": drugs[0] if drugs else "", "drug_b": drugs[1] if len(drugs) > 1 else ""}, ctx, "核对本地相互作用记录")
            state["ddi_result"] = ddi
        state["evidence"] = merged + state.get("attachment_evidence", [])
        state["tools"] = ctx.trace
        state["retrieval_status"] = "available" if any(r.get("chroma") == "available" for r in results) else "degraded" if merged else "unavailable"
        state["plan_rounds"], state["pending_actions"], state["evidence_gaps"] = 0, [], []
        return state

    async def plan(self, state):
        ctx = state["tool_context"]
        state["pending_actions"] = []
        if not self.owner.gateway or not ctx.network or state.get("plan_rounds", 0) >= 2 or ctx.calls >= ctx.max_calls:
            return state
        if detect_unsafe_medication_request(state.get('answer_query', state['query'])):
            return state
        if re.search(r"(?:只|仅|必须).{0,8}(?:本地|离线)|local (?:evidence|sources) only", state["query"], re.I):
            return state
        candidates = state.get("evidence", [])
        terms_for_gate = set(_terms(state["rewritten_query"]))
        relevant = [e for e in candidates if
                    len(terms_for_gate & set(_terms(e.content + " " + e.title))) / max(1, len(terms_for_gate)) >= .25
                    or e.metadata.get("semantic_score", 0) >= self.owner.retriever.semantic_min_score
                    or e.metadata.get("external")]
        explicit_network = bool(re.search(r"最新|联网|网上|查网|来源|文献|指南|https?://", state["query"]))
        # Dialogue matches are leads, not automatically sufficient clinical
        # evidence. In auto mode weak local material must trigger bounded public
        # evidence collection, even if the planner says `ready`.
        authoritative_local = any(
            e.metadata.get("external") or e.medical_truth_verified and (
                "guideline" in (e.source_type or "").lower()
                or "clinical" in (e.source_type or "").lower()
                or "指南" in (e.source_type or "")
            )
            for e in relevant
        )
        network_required = bool(ctx.network and (explicit_network or not relevant or not authoritative_local))
        topics = state["intent"].entities.model_dump()
        # Public search queries are composed from medical concepts, not patient paragraphs.
        concepts = [x for group in topics.values() if isinstance(group, list) for x in group if isinstance(x, str)]
        active_query = state.get('answer_query', state['query'])
        bilingual = state.get('query_variants') or glossary_plan(active_query)
        if 'query_variants' not in state:
            keywords = bilingual.get('local_query','') if bilingual.get('variants') else ''
            if not keywords:
                keywords = ' '.join(concepts)+' '+' '.join(bilingual.get('goals', []))
            translated = await self.owner.gateway.call('translate_query', {'query': network_query(keywords)}, ctx, '保留疾病与问题目标，构造中英文查询')
            if translated.get('variants'): bilingual = translated
            state['query_variants'] = bilingual
        variants = bilingual.get('variants', [])
        ctx.approved_queries.update(network_query(v['query']) for v in variants)
        public_query = next((v['query'] for v in variants if v['language']=='en'), '')
        if not public_query:
            public_query = network_query(" ".join(concepts))
        if not public_query:
            terms = [t for t in _terms(state["query"]) if len(t) >= 2 and not re.search(r"\d", t)]
            public_query = network_query(" ".join(terms[:10]))
        ctx.approved_queries.add(public_query)
        # Basic patient questions need explanatory source text, not only the
        # newest highly specialized papers. These host-owned URLs still pass
        # the gateway allowlist, SSRF checks and real MCP fetch.
        links = education_links(active_query) if self.owner.gateway.settings.patient_education_enabled else []
        links = [link for link in links if on_domain(link['url'], self.owner.gateway.backend.network.domains)]
        if links and network_required and state.get('plan_rounds', 0) == 0:
            ctx.allowed_urls.update(link['url'] for link in links)
            state['plan_rounds'] = 1
            state['pending_actions'] = [ToolAction(tool='fetch_evidence', arguments=ToolArgs(url=link['url']),
                reason='基础症状/检查问题：优先获取 NIH/NHLBI 患者教育正文') for link in links[:2]]
            state['force_refusal'] = False
            state['evidence_gaps'] = ['本地资料不足，正在尝试网络补证']
            return state
        fetched_goals = {e.metadata.get('source_goal') for e in candidates
                         if e.metadata.get('source_category') == 'patient_education'
                         and e.url in {link['url'] for link in links} and len(e.content.strip()) >= 80}
        goals = set(bilingual.get('goals', []))
        if goals and goals <= fetched_goals and not re.search(r'最新|指南|文献|论文|latest|guidelines?|papers?', state['query'], re.I):
            return state  # Final relevance + generation + safety gates still apply.
        payload = {"question": active_query, "public_query": public_query, "intent": state["intent"].intent.value,
                   'approved_queries': variants, 'uncovered_goals': state.get('evidence_gaps', []),
                   "drugs": state["intent"].entities.drug, "round": state.get("plan_rounds", 0),
                   "explicit_network": explicit_network,
                   "evidence": [{"evidence_id": e.evidence_id, "content": e.content[:1000], "title": e.title,
                                 "external": e.metadata.get("external", False)} for e in relevant[:10]],
                   "leads": state.get("web_leads", [])[:3], "allowed_urls": sorted(ctx.allowed_urls)[:5],
                   "tools": {k: v for k, v in DESCRIPTIONS.items() if k in NETWORK_TOOLS
                             and (k != "search_web" or self.owner.gateway.settings.tavily_api_key or k in self.owner.gateway.external)
                             and (k != "search_cpubmed" or self.owner.gateway.backend.cpubmed or k in self.owner.gateway.external)},
                   "previous_calls": [{"tool": t["tool"], "status": t["status"], 'query':t.get('query')} for t in ctx.trace],
                   "schema": ToolPlan.model_json_schema()}
        state["plan_rounds"] = state.get("plan_rounds", 0) + 1
        try:
            raw = await self.owner.llm.plan_tools(redact_payload(payload))
            decision = ToolPlan.model_validate(raw)
            state["evidence_gaps"] = decision.missing
            if decision.decision == "tools":
                for action in decision.actions:
                    if action.tool not in payload["tools"]:
                        continue
                    # Model cannot smuggle a patient paragraph into a public search query.
                    if action.tool in {"search_pubmed", "search_web", "search_cpubmed"}:
                        used = {t.get('query') for t in ctx.trace if t.get('tool')==action.tool}
                        proposed = network_query(action.arguments.query)
                        # Keep an approved goal-specific rewrite; never overwrite every round with the disease alone.
                        if proposed not in ctx.approved_queries or proposed in used:
                            proposed = next((v['query'] for v in variants if v['language']=='en' and v['query'] not in used), '')
                        if not proposed: continue
                        action.arguments.query = proposed
                    if action.tool == "search_drug_labels":
                        drugs = state["intent"].entities.drug
                        if action.arguments.query not in drugs:
                            if not drugs:
                                continue
                            action.arguments.query = drugs[0]
                    state["pending_actions"].append(action)
            elif decision.decision in {"clarify", "refuse"}:
                state["evidence_gaps"] = decision.missing or ["需要补充信息或可靠证据"]
                state["force_refusal"] = not bool(relevant)
        except Exception as exc:
            state["errors"].append("tool_plan_unavailable:" + type(exc).__name__)
            state["evidence_gaps"] = ["工具规划不可用，未完成补证"]
        # Do not let a model `ready` decision suppress the server-side coverage
        # rule. It remains bounded by the existing tool budget and two rounds.
        if network_required and not any(
            t.get("tool") in {"search_pubmed", "search_cpubmed", "search_web"}
            and t.get("status") == "available" for t in ctx.trace
        ):
            used = {t.get("query") for t in ctx.trace if t.get("tool") in {"search_pubmed", "search_cpubmed", "search_web"}}
            fallback_query = next((v["query"] for v in variants if v.get("language") == "en" and v["query"] not in used), public_query)
            if "search_pubmed" in payload["tools"] and fallback_query and fallback_query not in used:
                state["pending_actions"] = [a for a in state.get("pending_actions", []) if a.tool != "search_pubmed"]
                state["pending_actions"].insert(0, ToolAction(
                    tool="search_pubmed", arguments=ToolArgs(query=fallback_query),
                    reason="本地候选不足以覆盖问题，自动补充 PubMed 公开证据"))
                state["force_refusal"] = False
                state["evidence_gaps"] = list(dict.fromkeys(["本地资料不足，正在尝试网络补证"] + state.get("evidence_gaps", [])))
            elif not any(t.get("tool") == "search_pubmed" for t in ctx.trace):
                state["evidence_gaps"] = list(dict.fromkeys(["本地资料不足，PubMed 工具未配置或不可用"] + state.get("evidence_gaps", [])))
        return state

    async def execute(self, state):
        ctx = state["tool_context"]
        results = await asyncio.gather(*(self.owner.gateway.call(a.tool, a.arguments, ctx, a.reason)
                                       for a in state.get("pending_actions", [])[:2]))
        merged = {e.evidence_id: e for e in state.get("evidence", [])}
        for result in results:
            for row in result.get("evidence", []):
                e = EvidenceItem.model_validate(row)
                merged[e.evidence_id] = e
            state.setdefault("web_leads", []).extend(result.get("leads", []))
            if result.get("status") in {"unavailable", "not_configured", "blocked", "needs_confirmation"}:
                state["errors"].append("tool_" + result["status"] + ":" + str(result.get("error", "")))
        state["evidence"] = list(merged.values())
        state["tools"] = ctx.trace
        if any(e.metadata.get("external") for e in state["evidence"]):
            state["retrieval_status"] = "local_and_network" if self.owner.retriever.documents else "network_only"
            state['evidence_gaps'] = [gap for gap in state.get('evidence_gaps', [])
                                      if gap != '本地资料不足，正在尝试网络补证']
        return state

    async def rerank(self, state):
        if not self.owner.gateway:
            return state  # Legacy branch already reranked once.
        query = state["rewritten_query"]
        terms = set(_terms(query))
        # Chinese overlap cannot eliminate every English network result before
        # the bilingual reranker sees it. Reserve candidates for each origin.
        from ..tools.translation import local_variants
        goal_queries=glossary_plan(query).get('local_queries') or [query]
        term_sets=[set(_terms(v)) for q in goal_queries for v in local_variants(q)]
        def relevance(e):
            content=set(_terms(e.content+' '+e.title))
            return max((len(t & content)/max(1,len(t)) for t in term_sets),default=0)
        rows=state.get('evidence', [])
        groups=[sorted([e for e in rows if bool(e.metadata.get('external'))==external],key=relevance,reverse=True) for external in (False,True)]
        candidates=[]; seen=set()
        for rank in range(20):
            for group in groups:
                if rank<len(group) and group[rank].evidence_id not in seen:
                    candidates.append(group[rank]); seen.add(group[rank].evidence_id)
                    if len(candidates)==20: break
            if len(candidates)==20: break
        ranked = await self.owner.retriever.rerank(
            query, candidates, 5, query_variants=state.get('query_variants'))
        # The retriever reserves network slots only among relevant results.
        # Never reinsert a raw candidate that the reranker explicitly rejected.
        state["evidence"] = ranked
        goals=glossary_plan(query).get('goals',[])
        if 'preparation' in goals and not any(re.search(r'空腹|禁食|fasting|fasted|nil.by.mouth',e.content,re.I) for e in state['evidence']):
            state.setdefault('evidence_gaps',[]).append('检查准备/空腹：最终命中片段未覆盖，不能据此给出个人准备要求')
        if not state["evidence"]:
            state["evidence_gaps"] = state.get("evidence_gaps") or ["没有足够相关的医学证据"]
        ctx = state["tool_context"]
        if ctx.network and state['evidence'] and ctx.calls < ctx.max_calls:
            ctx.evidence = {e.evidence_id:e for e in state['evidence']}
            target = 'zh' if re.search(r'[\u4e00-\u9fff]', state['query']) else 'en'
            translated = await self.owner.gateway.call('translate_evidence',
                {'evidence_ids':list(ctx.evidence), 'target_language':target}, ctx, '仅翻译最终命中片段供辅助阅读，不改变医学可信度')
            for e in state['evidence']:
                e.metadata['display_translation'] = translated.get('translations', {}).get(e.evidence_id, {})
        state["errors"].extend(t["degradation"] for t in ctx.trace if t.get("degradation"))
        if state.get("ddi_result") is not None:
            state["tools"] = list(ctx.trace) + [{"tool": "ddi", **{k: v for k, v in state["ddi_result"].items() if k != "record"}}]
        context = CALL_CONTEXT.get()
        if context is not None:
            context['instructions'] += '\n复合问题分项回答：仅解释有引用支持的部分；未覆盖部分明确列为证据缺口，不猜测空腹要求或药物建议。'
            context.setdefault('clinical_context', {})['evidence_gaps'] = state.get('evidence_gaps', [])
            context.setdefault("clinical_context", {})["tool_status"] = [{"tool": t["tool"], "status": t.get("status")} for t in state.get("tools", [])]
        return state
