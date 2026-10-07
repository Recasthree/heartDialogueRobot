from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class MedicalIntent(str, Enum):
    SYMPTOM_CONSULT = "症状咨询"
    MEDICATION_QUERY = "用药询问"
    DRUG_INTERACTION = "药物相互作用"
    GUIDELINE_QUERY = "指南解读"
    TEST_INTERPRETATION = "检查解读"
    EMERGENCY = "危急症状"
    GENERAL = "通用兜底"


class IntentEntities(BaseModel):
    model_config = ConfigDict(extra="allow")
    drug: List[str] = Field(default_factory=list)
    disease: List[str] = Field(default_factory=list)
    symptom: List[str] = Field(default_factory=list)


class IntentResult(BaseModel):
    intent: MedicalIntent
    confidence: float = Field(ge=0.0, le=1.0)
    entities: IntentEntities = Field(default_factory=IntentEntities)
    is_emergency: bool = False
    rule_hits: List[str] = Field(default_factory=list)


class Medication(BaseModel):
    name: str
    dose: Optional[str] = None
    frequency: Optional[str] = None
    route: Optional[str] = None
    started_at: Optional[str] = None


class LabResult(BaseModel):
    name: str
    value: Optional[str] = None
    unit: Optional[str] = None
    reference_range: Optional[str] = None
    collected_at: Optional[str] = None


class CaseRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: str = "0.1"
    case_id: str = Field(default_factory=lambda: str(uuid4()))
    patient_id: str = Field(default_factory=lambda: "local-" + uuid4().hex[:12])
    age: Optional[int] = Field(default=None, ge=0, le=130)
    sex: Optional[str] = None
    chief_complaint: Optional[str] = None
    present_illness: Optional[str] = None
    past_medical_history: List[str] = Field(default_factory=list)
    diagnoses: List[str] = Field(default_factory=list)
    medications: List[Medication] = Field(default_factory=list)
    allergies: List[str] = Field(default_factory=list)
    vital_signs: Dict[str, Any] = Field(default_factory=dict)
    lab_results: List[LabResult] = Field(default_factory=list)
    imaging_results: List[str] = Field(default_factory=list)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    age: Optional[int] = Field(default=None, ge=0, le=130)
    sex: Optional[str] = None
    chief_complaint: Optional[str] = None
    present_illness: Optional[str] = None
    past_medical_history: Optional[List[str]] = None
    diagnoses: Optional[List[str]] = None
    medications: Optional[List[Medication]] = None
    allergies: Optional[List[str]] = None
    vital_signs: Optional[Dict[str, Any]] = None
    lab_results: Optional[List[LabResult]] = None
    imaging_results: Optional[List[str]] = None
    notes: Optional[str] = None


class EvidenceItem(BaseModel):
    evidence_id: str
    content: str
    source_type: str = "演示占位"
    source_id: str
    title: str
    url: Optional[str] = None
    page: Optional[int] = None
    evidence_level: Optional[str] = None
    is_demo: bool = True
    review_status: str = "pending"
    medical_truth_verified: bool = False
    is_synthetic: bool = False
    translation_status: Optional[str] = None
    source_file: Optional[str] = None
    source_sha256: Optional[str] = None
    retrieval_score: float = 0.0
    rerank_score: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    citation_id: int
    evidence_id: str
    source_type: str
    title: str
    snippet: str = Field(max_length=200)
    url: Optional[str] = None
    page: Optional[int] = None
    evidence_level: Optional[str] = None
    is_demo: bool = True
    review_status: str = "pending"
    medical_truth_verified: bool = False
    is_synthetic: bool = False
    translation_status: Optional[str] = None
    source_file: Optional[str] = None
    source_sha256: Optional[str] = None
    retrieved_at: Optional[str] = None
    document_date: Optional[str] = None
    jurisdiction: Optional[str] = None
    original_text: Optional[str] = None
    content_sha256: Optional[str] = None
    original_language: Optional[str] = None
    translated_text: Optional[str] = None
    translation_language: Optional[str] = None
    display_translation_status: Optional[str] = None
    is_network: bool = False


class GenerationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(min_length=1)
    citation_ids: List[str] = Field(default_factory=list)
    support_mode: Literal["cited", "mixed", "model_supplement", "refusal"] = Field(
        description="cited: medical claims supported by supplied evidence, including answers with a medication boundary; "
                    "mixed: evidence plus explicitly permitted unsupported medical supplementation, NOT local plus network sources; "
                    "model_supplement: no evidence, supplementation explicitly permitted; refusal: cannot answer, no citations")
    uncertainty: str = ""
    unsupported_claims: List[str] = Field(default_factory=list)
    safety_flags: List[str] = Field(default_factory=list)


class GeneratedAnswer(BaseModel):
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    confidence: Literal["高", "中", "低"] = "低"
    disclaimer: str = "本回答仅供参考，不能替代专业医生面诊。"
    intent: MedicalIntent = MedicalIntent.GENERAL
    is_emergency: bool = False
    evidence_status: Literal["demo", "verified", "unverified", "mixed", "none", "unavailable"] = "none"
    support_mode: Literal["cited", "mixed", "model_supplement", "refusal"] = "refusal"
    unsupported_claims: List[str] = Field(default_factory=list)
    safety_flags: List[str] = Field(default_factory=list)
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    environment: str = "local"
    uncertainty: str = ""
    actual_model: str = "mock"
    degradation_reasons: List[str] = Field(default_factory=list)
    retrieval_status: str = "not_run"
    index_version: Optional[str] = None
    model_calls: int = 0
    memory_status: str = "not_run"
    attachment_context_refs: List[Dict[str, Any]] = Field(default_factory=list)
    tool_summary: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_gaps: List[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    case_id: Optional[str] = None
    user_id: Optional[str] = Field(default=None, max_length=200)
    allow_model_supplement: bool = False
    message: str = Field(min_length=1, max_length=4000)
    attachment_ids: List[str] = Field(default_factory=list, max_length=3)
    network_mode: Literal["auto", "off"] = "auto"


class ChatResponse(GeneratedAnswer):
    session_id: str


class IngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=50000)
    source_type: str = "演示占位"
    source_id: Optional[str] = None
    page: Optional[int] = None


class EvaluationRequest(BaseModel):
    queries: List[str] = Field(default_factory=lambda: ["这个问题是否有相关证据？"])
    suite: Literal["custom", "ragcare", "safety", "judge", "all"] = "custom"
    mode: Literal["retrieval_only", "full"] = "full"
    max_cases: int = Field(default=32, ge=1, le=500)
    use_llm_judge: bool = True
    background: bool = False


class EvaluationReport(BaseModel):
    total: int
    average_score: Optional[float] = None
    status: str = "completed"
    results: List[Dict[str, Any]] = Field(default_factory=list)
    run_id: Optional[str] = None
    suites: Dict[str, Any] = Field(default_factory=dict)


class MedicalState(BaseModel):
    query: str
    session_id: str
    case_id: Optional[str] = None
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    intent: Optional[IntentResult] = None
    retrieved_evidence: List[EvidenceItem] = Field(default_factory=list)
    agent_outputs: Dict[str, Any] = Field(default_factory=dict)
    tool_errors: List[str] = Field(default_factory=list)
    evidence_status: Literal["demo", "verified", "none", "unavailable"] = "none"
    safety_check: Dict[str, Any] = Field(default_factory=dict)
    final_response: Optional[GeneratedAnswer] = None
    synthetic: bool = False
    source_status: str = "unknown"
    review_status: str = "pending"
