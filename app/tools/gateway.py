"""One policy gate for native/local and MCP tool execution."""
import asyncio
from contextlib import asynccontextmanager, nullcontext
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import time

from ..models import EvidenceItem
from ..privacy import redact_payload
from .contracts import DESCRIPTIONS, NETWORK_TOOLS, TRANSLATION_TOOLS, NODE_TOOLS, ToolArgs, ToolContext
from ..providers import CALL_CONTEXT
from .translation import TranslationTools
from .network import NetworkEvidence, network_query, valid_url, on_domain


class ToolBackend:
    def __init__(self, settings, retriever, attachments, ddi, cpubmed=None, network=None, llm=None):
        self.settings, self.retriever, self.attachments, self.ddi = settings, retriever, attachments, ddi
        self.cpubmed = cpubmed
        self.network = network or NetworkEvidence(settings, attachments)
        self.translation = TranslationTools(settings, llm)

    async def execute(self, name, args, context):
        if name == 'translate_query':
            return await self.translation.query(args.query)
        if name == 'translate_evidence':
            return await self.translation.evidence(args.evidence_ids, args.target_language, context)
        if name in {"parse_attachment", "ocr_pages"}:
            if args.attachment_id not in context.attachment_ids:
                raise PermissionError("attachment_scope_denied")
            return await self.attachments.parse(args.attachment_id, context.scope, args.pages or None, name == "ocr_pages")
        if name in {"search_local", "lookup_drug"}:
            types = args.source_types or None
            if name == "lookup_drug" and not types:
                types = [t for t in self.retriever.source_types() if 'drug' in t or '药' in t] or ["drug_label"]
            rows, status = await self.retriever.candidates(args.query, partitions=context.partitions, source_types=types)
            return {**status, "status": status.get("status", "no_record" if self.retriever.documents else "unavailable"),
                    "evidence": [r.model_dump() for r in rows]}
        if name == "lookup_ddi":
            return await self.ddi.query(args.drug_a, args.drug_b)
        if name == "search_pubmed":
            return await self.network.pubmed(args.query)
        if name == "search_drug_labels":
            return await self.network.labels(args.query)
        if name == "search_web":
            return await self.network.search_web(args.query)
        if name == "fetch_evidence":
            return await self.network.fetch(args.url, context.scope, user_provided=args.url in context.user_urls)
        if name == "search_cpubmed":
            if not self.cpubmed:
                return {"status": "not_configured", "evidence": []}
            rows = await self.cpubmed.search(args.query)
            return {"status": "available" if rows else "no_record", "evidence": [r.model_dump() for r in rows]}
        raise PermissionError("unknown_tool")


class LocalMCPAuth:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            auth = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
            try:
                loopback = ipaddress.ip_address(scope.get("client", ("", 0))[0]).is_loopback
            except ValueError:
                loopback = False
            # Browser never receives the MCP credential. SDK transport also checks Host.
            if not loopback or origin or not secrets.compare_digest(auth, "Bearer " + self.token):
                from starlette.responses import JSONResponse
                return await JSONResponse({"error": "mcp_auth_required"}, status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)


class ToolGateway:
    def __init__(self, settings):
        self.settings = settings
        self.backend = None
        self.capabilities = {}
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.asgi_app = None
        self.last_status = "disabled"
        self.semaphore = asyncio.Semaphore(settings.tool_concurrency)
        self.external = {}
        self._load_external()
        if settings.mcp_enabled:
            try:
                from mcp.server import MCPServer
                self.server = MCPServer("HeartDialogue evidence tools", log_level="WARNING")
                for name, description in DESCRIPTIONS.items():
                    self._register(name, description)
                self.asgi_app = LocalMCPAuth(self.server.streamable_http_app(streamable_http_path="/"), self.token)
                self.last_status = "configured_not_probed"
            except ImportError:
                self.last_status = "sdk_unavailable"

    def _register(self, name, description):
        async def handler(arguments: ToolArgs, capability: str) -> dict:
            context = self.capabilities.get(capability)
            if context is None or name not in NODE_TOOLS.get(context.node, set()):
                raise PermissionError("host_capability_required")
            if name in NETWORK_TOOLS | TRANSLATION_TOOLS and not context.network:
                raise PermissionError("network_disabled")
            if self.backend is None:
                raise RuntimeError("tool_backend_not_ready")
            try:
                token = CALL_CONTEXT.set(context.ledger)
                try:
                    with self.backend.retriever.use(context.index_handle) if context.index_handle is not None else nullcontext():
                        return await self.backend.execute(name, arguments, context)
                finally:
                    CALL_CONTEXT.reset(token)
            except Exception as exc:
                # Never leak source text, HTTP URLs with tokens, or full tracebacks through MCP.
                return {"status": "unavailable", "error": type(exc).__name__,
                        "http_status": getattr(getattr(exc, "response", None), "status_code", None), "evidence": []}
        self.server.tool(name=name, description=description)(handler)

    def _load_external(self):
        path = Path(self.settings.mcp_servers_file)
        if not path.exists():
            return
        config = json.loads(path.read_text(encoding="utf-8"))
        for server in config.get("servers", []):
            if not server.get("enabled") or server.get("read_only") is not True:
                continue
            if server.get("transport") not in {"stdio", "streamable-http"}:
                raise ValueError("unsupported_mcp_transport")
            for alias, remote_name in server.get("tools", {}).items():
                # Private attachments and local scopes are never handed to external servers.
                if alias not in NETWORK_TOOLS:
                    raise ValueError("external_tool_must_be_network_read_only")
                self.external[alias] = {**server, "remote_name": remote_name}

    @asynccontextmanager
    async def lifespan(self):
        if self.server:
            async with self.server.session_manager.run():
                yield
        else:
            yield

    async def _remote(self, name, args, context):
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        import httpx2
        import os
        server = self.external.get(name)
        if server and server["transport"] == "stdio":
            from mcp import StdioServerParameters
            env = {key: os.environ[key] for key in server.get("env_allowlist", []) if key in os.environ}
            spec = StdioServerParameters(command=server["command"], args=server.get("args", []), env=env)
            async with Client(spec) as client:
                result = await client.call_tool(server["remote_name"], args.model_dump(exclude_defaults=True))
        else:
            url = server["url"] if server else self.settings.mcp_base_url
            auth = os.environ.get(server.get("token_env", ""), "") if server else self.token
            headers = {"Authorization": "Bearer " + auth} if auth else {}
            capability = secrets.token_urlsafe(24)
            self.capabilities[capability] = context
            try:
                timeout = self.settings.attachment_timeout_seconds + 10 if context.node == "attachment" else self.settings.tool_timeout_seconds
                async with httpx2.AsyncClient(headers=headers, timeout=timeout, trust_env=False) as http:
                    transport = streamable_http_client(url, http_client=http)
                    async with Client(transport) as client:
                        result = await client.call_tool(server["remote_name"] if server else name,
                            args.model_dump(exclude_defaults=True) if server else {"arguments": args.model_dump(), "capability": capability})
            finally:
                self.capabilities.pop(capability, None)
        if result.is_error:
            raise RuntimeError("mcp_tool_error")
        value = result.structured_content
        if value is None:
            texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
            value = json.loads("".join(texts))
        if not isinstance(value, dict) or "status" not in value:
            raise ValueError("invalid_tool_output")
        return value

    async def call(self, name, arguments, context, reason=""):
        started = time.perf_counter()
        calls_before = (context.ledger or {}).get('model_calls', 0)
        args = arguments if isinstance(arguments, ToolArgs) else ToolArgs.model_validate(arguments)
        if name in NETWORK_TOOLS | TRANSLATION_TOOLS and name != 'fetch_evidence':
            args = args.model_copy(update={'query': network_query(args.query)})
        transport = "mcp_external" if name in self.external else "mcp_http"
        signature_args = args.model_copy(update={'query':args.query.casefold()}) if name in NETWORK_TOOLS | TRANSLATION_TOOLS else args
        signature = name + signature_args.model_dump_json()
        error = None
        if name not in NODE_TOOLS.get(context.node, set()):
            error = "tool_not_allowed_at_node"
        elif name in NETWORK_TOOLS | TRANSLATION_TOOLS and (not context.network or not self.settings.network_enabled):
            error = "network_disabled"
        elif name == 'translate_evidence' and not set(args.evidence_ids) <= set(context.evidence):
            error = 'evidence_translation_scope_denied'
        elif name in {'search_pubmed','search_web','search_cpubmed'} and context.approved_queries and args.query not in context.approved_queries:
            error = 'query_not_approved'
        elif name in {"parse_attachment", "ocr_pages"} and args.attachment_id not in context.attachment_ids:
            error = "attachment_scope_denied"
        elif context.calls >= context.max_calls:
            error = "tool_budget_exhausted"
        elif signature in context.seen:
            error = "duplicate_tool_call"
        if error:
            value = {"status": "blocked", "error": error, "evidence": []}
        else:
            if name == "fetch_evidence":
                try:
                    valid_url(args.url)
                    if args.url not in context.allowed_urls:
                        raise ValueError("url_not_discovered_or_user_provided")
                except ValueError:
                    value = {"status": "blocked", "error": "url_not_allowed", "evidence": []}
                    context.trace.append({"tool": name, "status": "blocked", "error": "url_not_allowed", "transport": "not_called"})
                    return value
            context.calls += 1
            context.seen.add(signature)
            if name in NETWORK_TOOLS and name != "fetch_evidence":
                args = args.model_copy(update={"query": network_query(args.query)})
            async with self.semaphore:
                try:
                    timeout = self.settings.attachment_timeout_seconds + 10 if context.node == "attachment" else self.settings.tool_timeout_seconds
                    value = await asyncio.wait_for(self._remote(name, args, context), timeout)
                    self.last_status = "available"
                except Exception as exc:
                    self.last_status = "unavailable"
                    if name in {"search_local", "lookup_drug", "lookup_ddi"} and self.backend:
                        transport = "native_fallback"
                        try:
                            value = await asyncio.wait_for(self.backend.execute(name, args, context), self.settings.tool_timeout_seconds)
                            value["degradation"] = "mcp_unavailable:" + type(exc).__name__
                        except Exception as fallback_error:
                            value = {"status": "unavailable", "error": type(fallback_error).__name__, "evidence": []}
                    else:
                        value = {"status": "unavailable", "error": type(exc).__name__, "evidence": []}
        # Validate returned documents. Remote data can never grant itself trust.
        clean = []
        for row in value.get("evidence", [])[:40]:
            try:
                item = EvidenceItem.model_validate(row)
                if len(item.content) > 16000:
                    continue
                if name in NETWORK_TOOLS:
                    if not item.url:
                        continue
                    valid_url(item.url)
                    from ..retrieval.hybrid import stable_evidence_id
                    item = item.model_copy(update={"evidence_id": stable_evidence_id(name, item.url, str(item.page), item.content),
                        "medical_truth_verified": False, "review_status": "pending", "is_demo": False,
                        "metadata": {**item.metadata, "external": True, "untrusted_input": True}})
                clean.append(item.model_dump())
            except (ValueError, TypeError):
                continue
        value["evidence"] = clean
        for lead in value.get("leads", [])[:3]:
            try:
                valid_url(lead["url"])
                domains = self.backend.network.domains if self.backend else tuple(self.settings.web_allowed_domains.split(','))
                if on_domain(lead["url"], domains):
                    context.allowed_urls.add(lead["url"])
            except (ValueError, KeyError):
                continue
        # Avoid source text / raw uploaded names in durable traces.
        trace = {"tool": name, "status": value.get("status", "unavailable"), "transport": transport,
                 'model_calls':(context.ledger or {}).get('model_calls', 0)-calls_before,
                 "reason": redact_payload(reason), "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                 "candidate_ids": [e["evidence_id"] for e in clean], "error": value.get("error"),
                 "degradation": value.get("degradation")}
        if name in NETWORK_TOOLS | TRANSLATION_TOOLS:
            trace["query"] = args.query
            trace['query_language'] = 'zh' if any('\u4e00' <= c <= '\u9fff' for c in args.query) else 'en'
            trace['cache_hit'] = value.get('cache_hit', False)
            trace['method'] = value.get('method')
            if args.url:
                parsed = valid_url(args.url)
                trace["source_host"] = parsed.hostname
        context.trace.append(trace)
        return value

    async def parse_upload(self, aid, scope):
        ctx = ToolContext(scope=scope, node="attachment", attachment_ids=[aid], max_calls=1)
        result = await self.call("parse_attachment", {"attachment_id": aid}, ctx, "用户上传附件")
        if result.get("status") not in {"ready", "failed"}:
            item = self.backend.attachments.get(aid, scope)
            item.update(status="failed", error="mcp_parse_unavailable")
            self.backend.attachments._save(item)
        return result

    def health(self):
        return {"mcp": self.last_status, "ocr_models": (Path(self.settings.ocr_model_dir) / "manifest.json").exists(),
                "network_enabled": self.settings.network_enabled,
                "network_dns_mode": self.settings.network_dns_mode,
                "tools": {"pubmed": "configured", "drug_labels": "configured",
                          'translation':'glossary+model' if self.backend and self.backend.translation.llm and hasattr(self.backend.translation.llm,'_json') else 'glossary_only',
                          "web_search": "configured" if self.settings.tavily_api_key else "not_configured",
                          "external_mcp": sorted(self.external)}}
