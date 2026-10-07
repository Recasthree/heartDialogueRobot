"""Application factory. Importing this module never opens databases or model clients."""
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.routes import build_router
from .config import Settings, ROOT
from .core.graph import MedicalOrchestrator
from .evaluation import EvaluationRunner
from .memory import ChromaMemoryStore, LongTermMemory, WorkingMemory
from .providers import DeepSeekProvider, DeepSeekReranker, MockLLMProvider, MockReranker
from .retrieval.index_manager import load_index, chroma, make_embedding
from .retrieval.live import LiveRetriever
from .retrieval.hybrid import HybridRetriever
from .retrieval.corpus import load_verified_corpus
from .store import CaseStore
from .tools.pubmed import PubMedTool
from .tools.cpubmed_kg import CPubMedKGTool
from .tools.gateway import ToolGateway, ToolBackend
from .attachments.store import AttachmentStore
from .browser import BrowserStore, browser_router


def create_app(settings=None):
    cfg = settings or Settings.from_env()
    gateway = ToolGateway(cfg) if cfg.agentic_enabled else None
    @asynccontextmanager
    async def lifespan(app):
        llm = DeepSeekProvider(cfg.deepseek_api_key, cfg.aihubmix_base_url, cfg.deepseek_model,
            cfg.deepseek_timeout_seconds, cfg.deepseek_max_retries, concurrency=cfg.model_concurrency,
            thinking_mode=cfg.deepseek_thinking_mode, max_tokens=cfg.deepseek_max_tokens) if cfg.llm_backend == "deepseek" else MockLLMProvider()
        reranker = DeepSeekReranker(llm) if cfg.llm_backend == "deepseek" else MockReranker()
        try:
            retriever = await asyncio.to_thread(load_index, cfg, reranker)
        except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
            retriever = HybridRetriever(make_embedding(cfg), reranker)
            retriever.index_error = 'index_load_failed:' + type(exc).__name__
        retriever = LiveRetriever(cfg, retriever)
        profile_store = await asyncio.to_thread(chroma, cfg, cfg.memory_collection + "_profiles")
        summary_store = await asyncio.to_thread(chroma, cfg, cfg.memory_collection + "_summaries")
        long_term = LongTermMemory(ChromaMemoryStore(profile_store), ChromaMemoryStore(summary_store))
        memory = WorkingMemory(cfg.redis_url, cfg.session_ttl_seconds, cfg.max_context_messages, long_term)
        await memory.connect()
        cases = CaseStore(cfg.data_dir)
        pubmed = PubMedTool(enabled=cfg.pubmed_enabled) if cfg.pubmed_enabled else None
        cpubmed = CPubMedKGTool(cfg.cpubmed_url) if cfg.cpubmed_url else None
        orchestrator = MedicalOrchestrator(llm, retriever, memory, cfg.app_env, cases,
            Path(cfg.data_dir) / "traces", cfg.knowledge_root, cfg.request_timeout_seconds, pubmed, cpubmed)
        attachments = AttachmentStore(Path(cfg.data_dir) / "attachments", cfg.ocr_model_dir,
                                      cfg.attachment_ttl_seconds, cfg.attachment_timeout_seconds)
        orchestrator.gateway, orchestrator.attachments = gateway, attachments
        if gateway:
            gateway.backend = ToolBackend(cfg, retriever, attachments, orchestrator.ddi, cpubmed, llm=llm)
        runner = EvaluationRunner(orchestrator, retriever, cfg.evaluation_dir, cfg.knowledge_root)
        warm_task = None
        # Encoder stays lazy, especially while only the lexical index is active.
        app.state.services = SimpleNamespace(settings=cfg, llm=llm, retriever=retriever,
            memory=memory, cases=cases, orchestrator=orchestrator, runner=runner, warm_task=warm_task,
            attachments=attachments, gateway=gateway, browser=BrowserStore(cfg.data_dir))
        async def cleanup():
            while True:
                try:
                    await attachments.cleanup()
                    if gateway:
                        gateway.backend.network.cleanup()
                except OSError:
                    pass
                await asyncio.sleep(60)
        cleanup_task = asyncio.create_task(cleanup())
        try:
            async with AsyncExitStack() as stack:
                if gateway:
                    await stack.enter_async_context(gateway.lifespan())
                yield
        finally:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
            await attachments.close()
            if gateway:
                await gateway.backend.network.close()
            tasks = list(runner.jobs.values())
            if warm_task:
                tasks.append(warm_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await memory.close()
            await llm.close()
            await retriever.close()
            for store in [profile_store, summary_store]:
                if store:
                    await store.close()
            for tool in [pubmed, cpubmed]:
                if tool:
                    await tool.close()
            app.state.services = None

    app = FastAPI(title="心血管对话研究台", version="0.4.0",
                  description="本地研究 toy，不提供生产医疗服务。", lifespan=lifespan)
    app.state.services = None
    app.include_router(build_router())
    app.include_router(browser_router())
    if gateway and gateway.asgi_app:
        app.mount("/mcp", gateway.asgi_app, name="mcp")
    app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")
    return app


app = create_app()
