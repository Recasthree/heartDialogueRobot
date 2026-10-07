from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    redis_url: str = ""
    chroma_host: str = "127.0.0.1"
    chroma_port: int = 8001
    llm_backend: str = "mock"
    aihubmix_base_url: str = "https://aihubmix.com/v1"
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_model: str = "deepseek-v4.1-flash"
    deepseek_timeout_seconds: float = 60.0
    deepseek_max_retries: int = 2
    deepseek_thinking_mode: str = "disabled"
    deepseek_max_tokens: int = 4096
    model_concurrency: int = 2
    request_timeout_seconds: float = 180.0
    chroma_mode: str = "http"
    chroma_path: str = str(ROOT / "data/runtime/chroma")
    chroma_collection: str = "cardio_evidence"
    memory_collection: str = "cardio_memory_v2"
    knowledge_root: str = str(ROOT / "data/verified")
    evaluation_dir: str = str(ROOT / "data/runtime/evaluations")
    session_ttl_seconds: int = 3600
    max_context_messages: int = 20
    data_dir: str = str(ROOT / "data/runtime")
    embedding_backend: str = "e5"
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_revision: str = "main"
    model_cache_dir: str = str(ROOT / "data/runtime/models")
    embedding_batch_size: int = 8
    vector_write_batch_size: int = 128
    build_min_free_memory_gib: float = 2.0
    build_max_rss_gib: float = 3.0
    build_min_free_disk_gib: float = 5.0
    build_start_free_disk_gib: float = 10.0
    translation_ttl_seconds: int = 86400
    embedding_local_only: bool = True
    semantic_min_score: float = 0.78
    pubmed_enabled: bool = False
    cpubmed_url: str = ""
    agentic_enabled: bool = True
    network_enabled: bool = True
    patient_education_enabled: bool = True
    network_dns_mode: str = "public_doh"
    mcp_enabled: bool = True
    mcp_base_url: str = "http://127.0.0.1:8000/mcp/"
    mcp_servers_file: str = str(ROOT / "config/mcp_servers.local.json")
    tavily_api_key: str = field(default="", repr=False)
    tool_max_calls: int = 8
    tool_concurrency: int = 2
    tool_timeout_seconds: float = 25.0
    attachment_ttl_seconds: int = 86400
    attachment_timeout_seconds: float = 120.0
    ocr_model_dir: str = str(ROOT / "data/runtime/ocr-models")
    web_allowed_domains: str = "nhlbi.nih.gov,ncbi.nlm.nih.gov,nlm.nih.gov,fda.gov,who.int,escardio.org,heart.org,acc.org,nice.org.uk,nmpa.gov.cn,cma.org.cn"

    @classmethod
    def from_env(cls, env_file=None):
        from dotenv import dotenv_values
        local = dotenv_values(env_file or ROOT / ".env.local")
        values = {**local, **os.environ}
        args = {}
        for item in fields(cls):
            raw = values.get(item.name.upper())
            if raw is None:
                continue
            if item.type is bool:
                args[item.name] = str(raw).lower() in {"1", "true", "yes"}
            elif item.type in (int, float):
                args[item.name] = item.type(raw)
            else:
                args[item.name] = str(raw)
        return cls(**args)


@lru_cache(maxsize=1)
def get_settings():
    return Settings.from_env()

