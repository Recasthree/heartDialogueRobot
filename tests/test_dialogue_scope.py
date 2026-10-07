import json
from unittest.mock import AsyncMock

import pytest

from app.dialogue_policy import request_scope, unsafe_answer, patient_wording, MEDICATION_BOUNDARY
from app.core.graph import MedicalOrchestrator
from app.memory import WorkingMemory
from app.models import EvidenceItem
from app.providers import MockLLMProvider, CALL_CONTEXT
from app.retrieval.hybrid import HybridRetriever
from app.tools.patient_sources import education_links
from app.tools.translation import glossary_plan


@pytest.mark.parametrize('question', [
    '心率不齐的症状是什么样子的，要用什么药缓解？',
    '心律不齐有什么症状，华法林吃多少毫克？',
    '要用什么药缓解？心率不齐的症状是什么样子的？',
    '心律不齐有什么症状要用什么药缓解',
])
def test_mixed_question_keeps_independent_symptoms(question):
    scope = request_scope(question)
    assert scope['medication_boundary']
    assert '症状' in scope['answer_query'] and '药' not in scope['answer_query']
    assert '心律不齐' in scope['answer_query']


def test_alias_keeps_symptoms_goal_and_official_source():
    assert education_links('心率不齐有什么症状') == education_links('心律不齐有什么症状')
    plan = glossary_plan('心率不齐的症状是什么样子的')
    assert 'arrhythmia' in plan['topics'] and plan['goals'] == ['symptoms']
    assert all('symptoms' in v['query'] for v in plan['variants'])
    assert not request_scope('要用什么药缓解')['medication_boundary']
    assert not request_scope('我吃多少华法林')['medication_boundary']


@pytest.mark.parametrize('answer', [
    '请不要自行停药。', '我不能替你决定停药。', '不建议你自行加量。',
    '这些信息不能说明你确诊为某种疾病。', MEDICATION_BOUNDARY,
])
def test_negated_medication_advice_is_not_an_instruction(answer):
    assert not unsafe_answer(answer)


@pytest.mark.parametrize('answer', [
    '建议你停药。', '你应该加量。', '建议你不要自行停药，但你今晚停药。',
    '不要自行停药，建议你加倍。', '你可以服用5mg药物。', '你不是不能停药。',
])
def test_real_dangerous_advice_is_still_blocked(answer):
    assert unsafe_answer(answer)


async def test_mixed_then_standalone_question_does_not_repeat_old_medication_task():
    seen = []
    class Scoped(MockLLMProvider):
        async def generate(self, query, evidence):
            seen.append({'query':query, 'context':dict(CALL_CONTEXT.get()['clinical_context'])})
            return json.dumps({'answer':'心律不齐可能表现为心悸 [1]。',
                'citation_ids':['s1'], 'support_mode':'cited'}, ensure_ascii=False)
    llm = Scoped()
    llm.rewrite = AsyncMock(side_effect=AssertionError('standalone query should not inherit prior task'))
    retriever = HybridRetriever()
    await retriever.add([EvidenceItem(evidence_id='s1',source_id='s1',title='心律不齐症状',
        content='心律不齐症状包括心悸，需结合检查了解情况。',source_type='clinical_guideline',is_demo=False)])
    graph = MedicalOrchestrator(llm, retriever, WorkingMemory())
    first = await graph.run('心率不齐的症状是什么样子的，要用什么药缓解？','scope-session')
    assert first.support_mode == 'cited' and '心悸' in first.answer
    assert MEDICATION_BOUNDARY in first.answer and len(first.citations) == 1
    assert 'medication_part_declined' in first.safety_flags
    second = await graph.run('心率不齐的症状是什么样子的','scope-session')
    assert second.support_mode == 'cited' and '心悸' in second.answer
    assert '用药' not in second.answer and 'medication_part_declined' not in second.safety_flags
    assert all('药' not in row['query'] and 'history' not in row['context'] and 'summaries' not in row['context'] for row in seen)
    llm.rewrite.assert_not_called()


async def test_followup_with_pronoun_still_resolves_history():
    llm = MockLLMProvider()
    llm.rewrite = AsyncMock(return_value='心律不齐的检查有哪些？')
    graph = MedicalOrchestrator(llm, HybridRetriever(), WorkingMemory())
    await graph.run('它要做哪些检查？', 'follow-up')
    llm.rewrite.assert_awaited_once()


async def test_negated_output_still_gets_model_safety_review():
    llm = MockLLMProvider()
    llm.safety_review = AsyncMock(return_value={'status':'pass','safe':True,'rule_hits':[]})
    graph = MedicalOrchestrator(llm, HybridRetriever(), WorkingMemory())
    state = {'query':'一般问题','generated':{'answer':'请不要自行停药。','support_mode':'cited'},'evidence':[]}
    result = await graph._safety_review(state)
    assert result['safety']['safe']
    llm.safety_review.assert_awaited_once()


def test_internal_status_translated_without_removing_uncertainty():
    text = patient_wording('部分来源 review_status 为 `pending`，medical_truth_verified=false，不能用于个人诊断。')
    assert not any(word in text for word in ['pending','review_status','medical_truth_verified'])
    assert '尚未完成专业审核' in text and '尚未经过医学核验' in text and '不能用于个人诊断' in text


async def test_citation_repair_records_reason_and_does_not_claim_no_sources():
    llm = MockLLMProvider()
    # A fabricated ID must stay invalid, even for the answerable half of a
    # mixed question. Formatting failures must not be presented as no data.
    llm.generate = AsyncMock(return_value=json.dumps({'answer':'心律不齐资料 [1]。',
        'citation_ids':['invented'], 'support_mode':'cited'}))
    retriever = HybridRetriever()
    await retriever.add([EvidenceItem(evidence_id='real',source_id='real',title='心律不齐症状',
        content='心律不齐症状的说明。',source_type='clinical_guideline',is_demo=False)])
    graph = MedicalOrchestrator(llm,retriever,WorkingMemory())
    answer = await graph.run('心率不齐的症状是什么样子的，要用什么药缓解？','bad-citation')
    assert answer.support_mode == 'refusal' and not answer.citations
    assert '引用格式校验未通过' in answer.answer and '已找到相关资料' in answer.answer
    assert MEDICATION_BOUNDARY in answer.answer
    assert 'generation_contract_invalid:invalid_citation_id' in answer.degradation_reasons
    assert llm.generate.await_count == 2
    assert 'invalid_citation_id' in llm.generate.await_args.args[0]


async def test_partial_answer_still_rejects_unsupported_supplementation():
    llm = MockLLMProvider()
    llm.generate = AsyncMock(return_value=json.dumps({'answer':'心律不齐资料 [1]。另外，无来源断言。',
        'citation_ids':['real'],'support_mode':'mixed','unsupported_claims':['无来源断言']}))
    graph = MedicalOrchestrator(llm,HybridRetriever(),WorkingMemory())
    state = {'query':'心律不齐症状，要用什么药', 'answer_query':'心律不齐症状',
             'medication_boundary':True, 'intent':await llm.classify('心律不齐症状'),
             'evidence':[EvidenceItem(evidence_id='real',source_id='real',title='症状',content='资料',is_demo=False)],'errors':[]}
    await graph._generate(state)
    assert state['generated']['support_mode'] == 'refusal'
    assert 'generation_contract_invalid:supplement_not_allowed' in state['errors']
    assert llm.generate.await_count == 2
