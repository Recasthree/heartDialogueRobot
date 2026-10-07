import json

import pytest
from pydantic import ValidationError

from app.core.graph import MedicalOrchestrator, _parse_generation
from app.evaluation import retrieval_metrics
from app.memory import WorkingMemory
from app.models import CasePatch, CaseRecord, EvidenceItem, IntentResult, MedicalIntent
from app.privacy import redact_text
from app.providers import MockLLMProvider, CALL_CONTEXT
from app.retrieval.hybrid import HybridRetriever
from app.store import CaseStore


@pytest.mark.parametrize('text', ['胸痛，大汗', '患者胸痛伴大汗', '房颤，应该做哪些检查', '我是高血压患者'])
def test_redaction_preserves_medical_terms(text):
    assert redact_text(text)[0] == text


async def test_emergency_precedes_memory():
    class BrokenMemory(WorkingMemory):
        async def get_context(self, *args):
            raise RuntimeError('offline')
        async def append(self, *args):
            raise RuntimeError('offline')
    result = await MedicalOrchestrator(MockLLMProvider(), HybridRetriever(), BrokenMemory()).run('胸痛，大汗', 'emergency')
    assert result.is_emergency and '120' in result.answer


def test_plain_text_must_not_acquire_citations():
    with pytest.raises((ValueError, ValidationError)):
        _parse_generation('not JSON', [])


@pytest.mark.parametrize('safety', [{'status': 'unsupported', 'safe': False}, {'status': 'pass', 'safe': False}, {'status': 'unavailable'}])
async def test_failed_review_refuses(safety):
    graph = MedicalOrchestrator(MockLLMProvider(), HybridRetriever(), WorkingMemory())
    item = EvidenceItem(evidence_id='e1', source_id='s1', title='t', content='证据')
    state = await graph._finalize({'query': '问题', 'trace_id': 'test', 'intent': IntentResult(intent=MedicalIntent.GENERAL, confidence=1), 'evidence': [item], 'generated': {'answer': '结论 [1]', 'citation_ids': ['e1'], 'support_mode': 'cited'}, 'safety': safety})
    assert state['result'].support_mode == 'refusal'
    assert state['result'].citations == []


def test_recall_is_not_hit():
    result = retrieval_metrics(['e1', 'e1'], ['e1', 'e2'])
    assert result == {'recall_at_5': .5, 'hit_at_5': 1., 'mrr_at_5': 1.}


def test_case_patch_forbids_identity():
    with pytest.raises(ValidationError):
        CasePatch.model_validate({'case_id': 'changed'})


def test_case_patch_validates_nested_and_null(tmp_path):
    store = CaseStore(str(tmp_path))
    original = store.create(CaseRecord())
    updated = store.update(original.case_id, CasePatch(medications=[{'name': 'test'}]))
    assert updated.medications[0].name == 'test'
    with pytest.raises(ValidationError):
        store.update(original.case_id, CasePatch(medications=None))
    assert CaseStore(str(tmp_path)).get(original.case_id).medications[0].name == 'test'


async def test_unrelated_hash_neighbor_is_not_evidence():
    retriever = HybridRetriever()
    await retriever.add([EvidenceItem(evidence_id='e1', source_id='s', title='fruit', content='apple tree fruit')])
    assert await retriever.search('quantum mechanics') == []


@pytest.mark.parametrize('safety_flag', ['generation_unavailable', 'unsafe_medication_request'])
async def test_network_failure_does_not_hide_model_failure_or_medication_refusal(safety_flag):
    graph = MedicalOrchestrator(MockLLMProvider(), HybridRetriever(), WorkingMemory())
    token = CALL_CONTEXT.set({'model_transport_failed': True})
    try:
        state = await graph._finalize({'query': '问题', 'trace_id': 'connection-test',
            'network_mode': 'auto', 'evidence': [],
            'tools': [{'tool': 'search_pubmed', 'status': 'unavailable'}],
            'generated': {'answer': '不能自行调整药物', 'citation_ids': [],
                          'support_mode': 'refusal', 'safety_flags': [safety_flag]},
            'safety': {'status': 'pass', 'safe': True}})
        if safety_flag == 'generation_unavailable':
            assert '模型服务连接失败' in state['result'].answer
            assert state['result'].uncertainty == 'model_connection_unavailable'
        else:
            assert state['result'].answer == '不能自行调整药物'
    finally:
        CALL_CONTEXT.reset(token)
