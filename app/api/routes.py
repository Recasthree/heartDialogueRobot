import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..models import CasePatch, CaseRecord, ChatRequest, ChatResponse, EvaluationRequest, IngestRequest, EvidenceItem
from ..privacy import redact_payload
from ..retrieval.hybrid import stable_evidence_id
from ..retrieval.corpus import chunk_text
from ..attachments.store import AttachmentError, AttachmentPatch, SaveAttachmentFacts, scope_key
from ..privacy import redact_text
from ..browser import browser_user


logger = logging.getLogger(__name__)


def build_router():
    router = APIRouter(prefix="/api/v1")

    def services(request):
        svc = request.app.state.services
        if svc is None:
            raise HTTPException(503, "application_lifespan_not_started")
        return svc

    async def status(s, probe=False):
        if hasattr(s.retriever, 'refresh'):
            await s.retriever.refresh()
        vector = s.retriever.vector_store
        available = bool(vector and vector.available)
        if probe and vector:
            try:
                available = await asyncio.wait_for(vector.ping(), 1.5)
            except asyncio.TimeoutError:
                available = False
        embedding_ready = not getattr(s.retriever.embedding, 'semantic', False) or s.retriever.embedding._model is not None
        success = getattr(s.llm, 'last_success_at', None)
        model_error = getattr(s.llm, 'last_error', None)
        model_health = 'mock' if s.settings.llm_backend == 'mock' else ('failed' if model_error else 'last_request_succeeded' if success else 'configured_not_probed')
        lexical = getattr(s.retriever, 'lexical_ready', bool(s.retriever.documents))
        dense = getattr(s.retriever, 'dense_ready', available)
        service_available = available
        if s.settings.chroma_mode == 'http':
            import httpx
            try:
                async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                    reply = await client.get(f'http://{s.settings.chroma_host}:{s.settings.chroma_port}/api/v2/heartbeat')
                    service_available = reply.status_code == 200
            except httpx.HTTPError:
                service_available = False
        build_path = Path(s.settings.data_dir)/'indexes/build-status.json'
        try:
            build = json.loads(build_path.read_text(encoding='utf-8')) if build_path.exists() else {}
        except (OSError,ValueError):
            build = {'status':'status_temporarily_unavailable'}
        return {"status": "ok", "environment": s.settings.app_env, "mode": s.settings.llm_backend,
                'lexical_ready': lexical, 'dense_ready': dense, 'chroma_service_available':service_available,
                'build_stage':build.get('stage'), 'build_status':build.get('status'),
                'build_progress':{k:build.get(k) for k in ['job_id','documents','upserted','encoded','reused_vectors','peak_rss_bytes','updated_at']},
                'pause_reason':build.get('status') if build.get('status','').startswith('paused_') else None,
                'last_build_error':build.get('error'),
                "llm_configured": s.settings.llm_backend == "mock" or bool(s.settings.deepseek_api_key),
                "llm_available": True if model_health in {'mock', 'last_request_succeeded'} else False if model_error else None, "llm_model": s.llm.model,
                "llm_last_error": getattr(s.llm, "last_error", None),
                "model_health": model_health, "llm_last_success_at": success,
                "redis": bool(s.memory._redis), "chroma": available,
                "dependency_probe": 'live' if probe else 'cached',
                "retrieval": "chroma+bm25" if available and dense else 'disk_bm25' if lexical else "unavailable",
                "knowledge_documents": len(s.retriever.documents),
                'partition_counts':{scope:s.retriever.repo.count(scope) for scope in ['evidence','benchmark','test']} if hasattr(s.retriever,'repo') else {},
                "index_version": s.retriever.index_version,
                "index_error": getattr(s.retriever, 'index_error', None),
                "embedding_model": s.retriever.embedding.model_name,
                "embedding_ready": embedding_ready,
                "langgraph": True, "index_ready": lexical,
                "degraded": not dense or not available or bool(getattr(s.llm, "last_error", None)),
                "agentic": s.gateway.health() if s.gateway else {"mcp": "disabled", "network_enabled": False}}

    def attachment_scope(request, session_id, case_id=None, user_id=None):
        owner = browser_user(request)
        if not owner and (str(session_id).startswith('web-') or str(user_id).startswith('browser-')):
            raise HTTPException(401, 'browser_cookie_required')
        if owner:
            session = services(request).browser.session(owner, session_id)
            if (session['case_id'] or '') != (case_id or ''):
                raise HTTPException(403, 'browser_case_scope_mismatch')
            user_id = owner
        try:
            return scope_key(session_id, case_id, user_id)
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)

    def queue_parse(s, aid, scope):
        if not s.gateway:
            raise HTTPException(503, "agentic_tools_disabled")
        task = asyncio.create_task(s.gateway.parse_upload(aid, scope))
        s.attachments.jobs[aid] = task
        def done(completed):
            if not completed.cancelled():
                completed.exception()
            s.attachments.jobs.pop(aid, None)
        task.add_done_callback(done)

    @router.post("/attachments", status_code=202)
    async def upload_attachment(request: Request, file: UploadFile = File(...), session_id: str = Form(...),
                                case_id: str = Form(""), user_id: str = Form(""), purpose: str = Form("patient_context"),
                                selected_pages: str = Form("[]")):
        s = services(request)
        if not s.gateway:
            raise HTTPException(503, "agentic_tools_disabled")
        if case_id and not s.cases.get(case_id):
            raise HTTPException(404, "case_not_found")
        scope = attachment_scope(request, session_id, case_id, user_id)
        try:
            pages = json.loads(selected_pages)
            if not isinstance(pages, list) or len(pages) > 20 or any(type(n) is not int or n < 1 for n in pages):
                raise ValueError()
            data = await file.read(20 * 1024 * 1024 + 1)
            item = s.attachments.create(data, file.filename, scope, purpose, pages or None)
            queue_parse(s, item["id"], scope)
            return item
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_selected_pages")
        finally:
            await file.close()

    @router.get("/attachments/{aid}")
    async def get_attachment(aid: str, request: Request, session_id: str, case_id: str = "", user_id: str = ""):
        try:
            return services(request).attachments.get(aid, attachment_scope(request, session_id, case_id, user_id))
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)

    @router.get("/attachments/{aid}/preview")
    async def preview_attachment(aid: str, request: Request, session_id: str, case_id: str = "", user_id: str = ""):
        from fastapi.responses import FileResponse
        s = services(request)
        try:
            item = s.attachments.get(aid, attachment_scope(request, session_id, case_id, user_id))
            mime = {"png": "image/png", "jpeg": "image/jpeg", "pdf": "application/pdf", "txt": "text/plain"}[item["kind"]]
            return FileResponse(s.attachments._file(aid), media_type=mime,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)

    @router.patch("/attachments/{aid}")
    async def patch_attachment(aid: str, body: AttachmentPatch, request: Request, session_id: str, case_id: str = "", user_id: str = ""):
        s = services(request)
        scope = attachment_scope(request, session_id, case_id, user_id)
        try:
            item = s.attachments.patch(aid, scope, body)
            if body.retry:
                queue_parse(s, aid, scope)
            return item
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)

    @router.delete("/attachments/{aid}")
    async def delete_attachment(aid: str, request: Request, session_id: str, case_id: str = "", user_id: str = ""):
        try:
            await services(request).attachments.delete(aid, attachment_scope(request, session_id, case_id, user_id))
            return {"deleted": True, "attachment_id": aid}
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)

    @router.post("/attachments/{aid}/save-facts")
    async def save_attachment_facts(aid: str, body: SaveAttachmentFacts, request: Request,
                                    session_id: str, case_id: str = "", user_id: str = ""):
        s = services(request)
        try:
            item = s.attachments.context([aid], attachment_scope(request, session_id, case_id, user_id))[0]
            if item["version"] != body.version:
                raise AttachmentError("attachment_version_conflict", 409)
            if item["purpose"] != "patient_context" or case_id and case_id != body.target_case_id:
                raise AttachmentError("attachment_case_scope_mismatch", 403)
            case = s.cases.get(body.target_case_id)
            if not case:
                raise HTTPException(404, "case_not_found")
            original = "\n".join(p["text"] for p in item["pages"])
            if any(not fact.strip() or len(fact) > 1000 or fact not in original for fact in body.facts):
                raise AttachmentError("select_exact_confirmed_facts")
            facts = [redact_text(fact)[0] for fact in body.facts]
            notes = (case.notes or "") + "\n用户明确保存的附件转录事实（非医学审核）：\n" + "\n".join(facts)
            s.cases.update(case.case_id, CasePatch(notes=notes))
            return {"saved": True, "case_id": case.case_id, "facts_count": len(facts)}
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)

    @router.get("/health")
    async def health(request: Request):
        return await status(services(request))

    @router.get("/ready")
    async def ready(request: Request):
        data = await status(services(request), probe=True)
        data["status"] = "ready" if data["index_ready"] else "not_ready"
        return JSONResponse(data, status_code=200 if data["index_ready"] else 503)

    @router.get('/knowledge/index/status')
    async def index_status(request: Request):
        data = await status(services(request))
        return {k:v for k,v in data.items() if k.startswith('build_') or k in {'lexical_ready','dense_ready','chroma_service_available','pause_reason','last_build_error','index_version','knowledge_documents'}}

    @router.get("/cases", response_model=list[CaseRecord])
    async def list_cases(request: Request):
        return services(request).cases.list()

    @router.post("/cases", response_model=CaseRecord)
    async def create_case(case: CaseRecord, request: Request):
        try:
            return services(request).cases.create(case)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "case_id_already_exists")

    @router.get("/cases/{case_id}", response_model=CaseRecord)
    async def get_case(case_id: str, request: Request):
        case = services(request).cases.get(case_id)
        if not case:
            raise HTTPException(404, "case_not_found")
        return case

    @router.patch("/cases/{case_id}", response_model=CaseRecord)
    async def update_case(case_id: str, patch: CasePatch, request: Request):
        try:
            case = services(request).cases.update(case_id, patch)
        except ValidationError as exc:
            raise HTTPException(422, detail=json.loads(exc.json(include_input=False)))
        if not case:
            raise HTTPException(404, "case_not_found")
        return case

    @router.delete("/cases/{case_id}")
    async def delete_case(case_id: str, request: Request):
        if not services(request).cases.delete(case_id):
            raise HTTPException(404, "case_not_found")
        return {"deleted": True, "case_id": case_id}

    @router.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request):
        s = services(request)
        owner = browser_user(request)
        if not owner and (str(body.session_id).startswith('web-') or str(body.user_id).startswith('browser-')):
            raise HTTPException(401, 'browser_cookie_required')
        session = body.session_id or "local-" + uuid4().hex[:16]
        if owner:
            current = s.browser.session(owner, body.session_id) if body.session_id else s.browser.create_session(owner, body.case_id)
            session = current['id']
            if current['case_id'] != body.case_id:
                raise HTTPException(403, 'browser_case_scope_mismatch')
        try:
            answer = await s.orchestrator.run(body.message, session, body.case_id, owner or body.user_id, body.allow_model_supplement,
                attachment_ids=body.attachment_ids, network_mode=body.network_mode if s.settings.network_enabled else "off")
        except AttachmentError as exc:
            raise HTTPException(exc.status, exc.code)
        except LookupError:
            raise HTTPException(404, "case_not_found")
        except Exception as exc:
            # Keep an unexpected provider/tool failure inside the JSON API
            # contract. The browser must never receive plain-text ASGI 500
            # output and then fail while parsing it as JSON.
            trace_id = uuid4().hex
            logger.exception("chat_request_failed trace_id=%s error=%s", trace_id, type(exc).__name__)
            return JSONResponse({
                "error": "chat_failed",
                "detail": "本轮回答生成失败，服务已进入降级保护。请稍后重试。",
                "trace_id": trace_id,
                "degradation_reasons": ["chat_internal_error:" + type(exc).__name__],
            }, status_code=503)
        result = ChatResponse(session_id=session, **answer.model_dump())
        if owner:
            s.browser.append(owner, session, body.message, result.model_dump(), bool(body.attachment_ids))
        return result

    @router.post("/knowledge/ingest")
    async def ingest(body: IngestRequest, request: Request):
        s = services(request)
        source = body.source_id or "runtime-" + uuid4().hex[:12]
        try:
            parts = await asyncio.to_thread(chunk_text, body.content, s.retriever.embedding)
            docs = [EvidenceItem(evidence_id=stable_evidence_id("runtime", source, str(i), content),
                source_id=source, source_type=body.source_type, title=body.title, content=content, page=body.page,
                is_demo=body.source_type == "演示占位", source_file="runtime",
                metadata={"runtime_ingest": True, "partition": "evidence", "retrieval_allowed": True,
                          "parent_evidence_id": source}) for i, content in enumerate(parts)]
            result = await s.retriever.add(docs)
            if not result["persisted"]:
                raise RuntimeError("local_persistence_failed")
            # Same source_id means an intentional update of this runtime source only.
            obsolete = [eid for eid in s.retriever.runtime_ids(source) if eid not in {r.evidence_id for r in docs}]
            if result['vector_persisted'] or s.settings.chroma_mode == 'disabled':
                await s.retriever.remove(obsolete)
            elif obsolete:
                result['errors'].append('previous_source_retained_until_vector_write_succeeds')
            version = (s.retriever.index_version or "runtime") + "-" + uuid4().hex[:6]
            s.retriever.set_version(version)
            data = {**result, "source_id": source, "evidence_ids": [d.evidence_id for d in docs],
                    "index_version": version, "is_demo": docs[0].is_demo}
            return JSONResponse(data, status_code=200 if result["vector_persisted"] else 207)
        except Exception as exc:
            return JSONResponse({"ingested": False, "persisted": False, "error": type(exc).__name__}, status_code=503)

    @router.post("/evaluations/run")
    async def evaluations(body: EvaluationRequest, request: Request):
        runner = services(request).runner
        kwargs = body.model_dump(exclude={"background"})
        if not body.background:
            return await runner.run(**kwargs)
        if any(not job.done() for job in runner.jobs.values()):
            raise HTTPException(409, 'background_evaluation_already_running')
        run_id = "eval-" + uuid4().hex
        runner.progress[run_id] = {"run_id": run_id, "status": "queued", "completed_cases": 0}
        task = asyncio.create_task(runner.run(**kwargs, run_id=run_id))
        runner.jobs[run_id] = task
        def done(completed):
            if not completed.cancelled():
                completed.exception()  # runner persisted the redacted failure status
            runner.jobs.pop(run_id, None)
        task.add_done_callback(done)
        return JSONResponse(runner.progress[run_id], status_code=202)

    @router.get("/evaluations/{run_id}")
    async def evaluation_status(run_id: str, request: Request):
        runner = services(request).runner
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", run_id):
            raise HTTPException(400, "invalid_run_id")
        if run_id in runner.progress:
            return runner.progress[run_id]
        path = runner.output_dir / (run_id + ".json")
        if not path.exists():
            raise HTTPException(404, "evaluation_not_found")
        return json.loads(path.read_text(encoding="utf-8"))

    @router.get("/traces/{trace_id}")
    async def trace(trace_id: str, request: Request):
        owner = browser_user(request)
        if not owner and services(request).browser.is_browser_trace(trace_id):
            raise HTTPException(404, 'trace_not_found')
        if owner and not services(request).browser.owns_trace(owner, trace_id):
            raise HTTPException(404, 'trace_not_found')
        graph = services(request).orchestrator
        if not re.fullmatch(r"[a-f0-9-]{36}", trace_id):
            raise HTTPException(400, "invalid_trace_id")
        if trace_id in graph.traces:
            return graph.traces[trace_id]
        path = graph.trace_dir / (trace_id + ".json")
        if not path.exists():
            raise HTTPException(404, "trace_not_found")
        return json.loads(path.read_text(encoding="utf-8"))

    return router

