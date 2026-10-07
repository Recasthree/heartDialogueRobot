"""Public tool contracts; scope and budgets are supplied by the host, never the LLM."""
from dataclasses import dataclass, field
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(default="", max_length=1000)
    url: str = Field(default="", max_length=2000)
    drug_a: str = Field(default="", max_length=100)
    drug_b: str = Field(default="", max_length=100)
    attachment_id: str = Field(default="", max_length=50)
    pages: list[int] = Field(default_factory=list, max_length=20)
    source_types: list[str] = Field(default_factory=list, max_length=20)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)
    target_language: Literal['zh', 'en'] = 'zh'


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: str = Field(max_length=100)
    arguments: ToolArgs = Field(default_factory=ToolArgs)
    reason: str = Field(max_length=200)


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["ready", "tools", "clarify", "refuse"]
    missing: list[str] = Field(default_factory=list, max_length=4)
    actions: list[ToolAction] = Field(default_factory=list, max_length=2)


@dataclass
class ToolContext:
    scope: str
    node: str = "evidence"
    network: bool = False
    attachment_ids: list[str] = field(default_factory=list)
    allowed_urls: set[str] = field(default_factory=set)
    user_urls: set[str] = field(default_factory=set)
    partitions: list[str] = field(default_factory=lambda: ["evidence"])
    calls: int = 0
    max_calls: int = 8
    trace: list[dict[str, Any]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    evidence: dict = field(default_factory=dict)
    index_handle: Any = None
    ledger: Any = None
    approved_queries: set[str] = field(default_factory=set)
    protected_terms: list[str] = field(default_factory=list)


NETWORK_TOOLS = {"search_pubmed", "search_drug_labels", "search_web", "fetch_evidence", "search_cpubmed"}
TRANSLATION_TOOLS = {'translate_query', 'translate_evidence'}
NODE_TOOLS = {"attachment": {"parse_attachment", "ocr_pages"},
              "evidence": {"search_local", "lookup_drug", "lookup_ddi"} | NETWORK_TOOLS | TRANSLATION_TOOLS}
DESCRIPTIONS = {
    'translate_query': '将脱敏医学概念与检索目标转换为批准的中英文查询；术语表优先',
    'translate_evidence': '仅翻译本轮最终命中证据 ID 的展示片段，保留原文与来源',
    "parse_attachment": "解析当前会话的已上传附件，不接受文件路径",
    "ocr_pages": "对附件指定页执行本地 OCR",
    "search_local": "查询现有本地 BM25/Chroma 医学资料",
    "lookup_drug": "查询本地药品资料；参数 query",
    "lookup_ddi": "查询本地相互作用；参数 drug_a 和 drug_b；未查到不表示安全",
    "search_pubmed": "查询 PubMed 文献摘要，不自动视为指南；参数 query",
    "search_drug_labels": "查询美国公开药品说明书；参数 query 必须是明确的药名",
    "search_web": "在限定医学来源中搜索线索，线索尚不可引用；参数 query",
    "fetch_evidence": "获取已发现或用户指定 URL 的正文；参数 url",
    "search_cpubmed": "可选 CPubMed 知识图谱查询",
}
