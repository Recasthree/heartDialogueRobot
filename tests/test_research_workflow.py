import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.core.graph import MedicalOrchestrator, citation_errors
from app.evaluation import EvaluationRunner
from app.main import create_app
from app.memory import ChromaMemoryStore, LongTermMemory, WorkingMemory, memory_scope
from app.models import EvidenceItem, CaseRecord
from app.providers import CALL_CONTEXT, DeepSeekProvider, DeepSeekReranker, DeterministicEmbeddingProvider, MockLLMProvider
from app.retrieval.hybrid import HybridRetriever, reciprocal_rank_fusion
from app.retrieval.index_manager import build_index, load_index
from app.store import CaseStore
from app.tools.drug_interaction import DrugInteractionTool


def evidence(identity='e1', text='房颤 心房颤动 检查 心电图', **kwargs):
    return EvidenceItem(evidence_id=identity, source_id=identity, title='心脏检查', content=text, **kwargs)


def generation(ids=None, answer='房颤可参考心电图检查 [1]', mode='cited'):
    return json.dumps({'answer': answer, 'citation_ids': ['e1'] if ids is None else ids, 'support_mode': mode}, ensure_ascii=False)


def fake_provider(contents):
    provider = DeepSeekProvider('', max_retries=0)
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=c))]) if isinstance(c, str) else c for c in contents]
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=responses))))
    return provider


async def test_deepseek_json_success_and_redacted_outbound():
    provider = fake_provider([generation()])
    result = json.loads(await provider.generate('患者王芳，电话13812345678，检查什么？', [evidence()]))
    assert result['citation_ids'] == ['e1']
    payload = provider.client.chat.completions.create.call_args.kwargs['messages'][1]['content']
    assert '王芳' not in payload and '13812345678' not in payload
    kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert kwargs['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert kwargs['max_tokens'] == 4096


async def test_generation_receives_plain_source_limitations_not_internal_status():
    provider = fake_provider([generation()])
    source = evidence(is_demo=False, review_status='pending', medical_truth_verified=False)
    await provider.generate('房颤症状', [source])
    payload = json.loads(provider.client.chat.completions.create.call_args.kwargs['messages'][1]['content'])
    supplied = payload['evidence'][0]
    assert 'review_status' not in supplied and 'medical_truth_verified' not in supplied
    assert '未经过本项目的医学核验' in supplied['source_note']
    assert source.review_status == 'pending' and source.medical_truth_verified is False


@pytest.mark.parametrize('content,finish', [('', 'stop'), ('{}', 'length')])
async def test_empty_or_truncated_json_is_not_success(content, finish):
    provider = fake_provider([])
    provider.client.chat.completions.create.side_effect = None
    provider.client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)])
    result = await provider.safety_review('问题', '回答', [])
    assert result['status'] == 'unavailable' and result['safe'] is False
    assert provider.total_requests == 1 and provider.last_success_at is None


async def test_model_budget_includes_failed_attempts():
    provider = fake_provider([asyncio.TimeoutError()])
    provider.request_budget = 1
    provider.max_retries = 2
    await provider.safety_review('问题', '回答', [])
    assert provider.total_requests == 1
    assert provider.client.chat.completions.create.await_count == 1


async def test_timeout_no_duplicate_format_retry_and_review_unavailable():
    provider = fake_provider([asyncio.TimeoutError()])
    result = await provider.safety_review('检查', '回答', [])
    assert result['status'] == 'unavailable' and result['safe'] is False
    assert provider.client.chat.completions.create.await_count == 1


async def test_authentication_error_not_retried():
    from openai import AuthenticationError
    response = httpx.Response(401, request=httpx.Request('POST','https://example.test'))
    provider = fake_provider([AuthenticationError('denied', response=response, body=None)])
    provider.max_retries = 2
    result = await provider.classify('检查')
    assert provider.client.chat.completions.create.await_count == 1
    assert result.intent.value == '检查解读'


async def test_connection_failure_stops_later_nodes_but_next_turn_can_recover():
    from openai import APIConnectionError
    failure = APIConnectionError(request=httpx.Request('POST', 'https://example.test'))
    provider = fake_provider([failure, '{}'])
    ledger = {'model_calls': 0, 'successful_calls': 0, 'errors': []}
    token = CALL_CONTEXT.set(ledger)
    try:
        await provider.classify('房颤检查')
        review = await provider.safety_review('检查', '回答', [])
        assert review['status'] == 'unavailable'
        assert provider.client.chat.completions.create.await_count == 1
        assert ledger['model_transport_failed'] and ledger['model_calls'] == 1
    finally:
        CALL_CONTEXT.reset(token)
    token = CALL_CONTEXT.set({'model_calls': 0, 'successful_calls': 0, 'errors': []})
    try:
        assert await provider._json('probe', '{}') == {}
        assert provider.client.chat.completions.create.await_count == 2
    finally:
        CALL_CONTEXT.reset(token)


async def test_missing_key_judge_is_failed_not_mock_score():
    result = await DeepSeekProvider('').judge('q', 'a', [])
    assert result['status'] == 'failed'
    assert 'accuracy' not in result


async def test_batch_reranker_one_call_and_contract():
    provider = fake_provider([json.dumps({'scores':[{'evidence_id':'r1','score':.7},{'evidence_id':'r2','score':.9}]})])
    result = await DeepSeekReranker(provider).rank('心脏', [evidence(), evidence('e2')])
    assert result[0].evidence_id == 'e2'
    assert provider.client.chat.completions.create.await_count == 1
    payload = json.loads(provider.client.chat.completions.create.call_args.kwargs['messages'][1]['content'])
    assert [c['evidence_id'] for c in payload['candidates']] == ['r1','r2']


async def test_unrelated_or_model_rejected_network_items_are_not_forced_back():
    retriever = HybridRetriever()
    external = evidence('external', 'Astronomy: stars and planets orbit in space.', metadata={'external':True})
    external.title = 'Astronomy'
    assert await retriever.rerank('房颤的常见检查有哪些？', [external]) == []
    rejected = evidence('rejected', '房颤检查心电图', metadata={'external':True}, rerank_score=0.)
    assert await retriever.rerank('房颤检查', [rejected]) == []


async def test_single_declared_source_formatting_keeps_safety_gate():
    class OneSource(MockLLMProvider):
        async def generate(self, query, docs):
            return generation(['e1'], '房颤检查可参考心电图')
    provider = OneSource()
    retriever = HybridRetriever()
    await retriever.add([evidence()])
    graph = MedicalOrchestrator(provider, retriever, WorkingMemory())
    result = await graph.run('房颤检查', 'format')
    assert result.support_mode == 'cited' and '[1]' in result.answer
    assert 'citation_format_normalized:single_declared_source' in result.degradation_reasons
    provider.safety_review = AsyncMock(return_value={'status':'unsupported','safe':False,'rule_hits':[]})
    result = await graph.run('房颤检查', 'reject')
    assert result.support_mode == 'refusal' and not result.citations


@pytest.mark.parametrize('ids,answer,mode', [([], '无声明来源', 'cited'),
    (['fake'], '伪造来源', 'cited'), (['e1'], '错误编号 [999]', 'cited'),
    (['e1','e2'], '多来源不能推测正文映射', 'cited'), (['e1'], '混合回答', 'mixed')])
async def test_citation_formatter_never_invents_binding(ids, answer, mode):
    from app.core.graph import format_single_source_citation
    value = json.loads(generation(ids, answer, mode))
    assert format_single_source_citation(value, [evidence(), evidence('e2')]) == value


@pytest.mark.parametrize('raw', ['not json', generation(['e1','fake'], '结论 [1] [2]'), generation(['e1'], '结论 [999]')])
async def test_invalid_generation_repaired_once_then_refused(raw):
    class Broken(MockLLMProvider):
        def __init__(self): self.calls = 0
        async def generate(self, query, evidence):
            self.calls += 1
            return raw
    llm = Broken()
    retriever = HybridRetriever()
    await retriever.add([evidence()])
    answer = await MedicalOrchestrator(llm, retriever, WorkingMemory()).run('房颤检查', 's')
    assert llm.calls == 2
    assert answer.support_mode == 'refusal' and not answer.citations


async def test_citation_repair_can_succeed_without_retaining_invalid_claim():
    class Repair(MockLLMProvider):
        def __init__(self): self.calls=0
        async def generate(self,query,evidence):
            self.calls+=1
            return generation(['bad'],'错误断言 [1]') if self.calls==1 else generation()
    llm=Repair(); retriever=HybridRetriever()
    await retriever.add([evidence()])
    result=await MedicalOrchestrator(llm,retriever,WorkingMemory()).run('房颤检查','repair')
    assert llm.calls==2 and result.support_mode=='cited'
    assert '错误断言' not in result.answer
    assert [c.evidence_id for c in result.citations]==['e1']


async def test_intent_confusion_matrix_uses_gold_labels(tmp_path):
    folder=tmp_path/'agent'; folder.mkdir()
    (folder/'intent_entity_cases.jsonl').write_text(json.dumps({'sample_id':'one','message':'检查结果', 'intent':'检查解读'},ensure_ascii=False)+'\n',encoding='utf-8')
    retriever=HybridRetriever()
    runner=EvaluationRunner(MedicalOrchestrator(MockLLMProvider(),retriever,WorkingMemory()),retriever,tmp_path/'out',tmp_path)
    result=await runner.intent_suite()
    assert result['accuracy']==1
    assert result['confusion_matrix']=={'检查解读':{'检查解读':1}}


async def test_supplement_requires_explicit_flag_and_label():
    class Supplement(MockLLMProvider):
        async def generate(self, query, evidence):
            return generation([], '一般概念解释。', 'model_supplement')
    graph = MedicalOrchestrator(Supplement(), HybridRetriever(), WorkingMemory())
    refused = await graph.run('健康概念是什么', 's')
    assert refused.support_mode == 'refusal'
    answer = await graph.run('健康概念是什么', 's2', allow_model_supplement=True)
    assert answer.support_mode == 'model_supplement'
    assert '未被本地资料支持' in answer.answer
    local_only = await graph.run('仅根据本地资料解释健康概念','s3',allow_model_supplement=True)
    assert local_only.support_mode == 'refusal'


async def test_persistence_no_reembed_or_query_rebuild(tmp_path):
    class Spy(DeterministicEmbeddingProvider):
        def __init__(self): self.calls = 0
        async def embed_documents(self, texts):
            self.calls += len(texts)
            return await super().embed_documents(texts)
    encoder = Spy()
    path = tmp_path / 'index.sqlite3'
    original = HybridRetriever(encoder, index_path=path)
    await original.add([evidence()])
    assert encoder.calls == 1
    restarted = HybridRetriever(encoder, index_path=path)
    before = restarted.bm25.rebuild_count
    for _ in range(2):
        assert (await restarted.search('房颤检查'))[0].evidence_id == 'e1'
    assert encoder.calls == 1 and restarted.bm25.rebuild_count == before


async def test_filter_before_vector_query_and_single_channel_vote():
    class Encoder(DeterministicEmbeddingProvider): semantic = True
    class Vector:
        collection = True
        available = True
        async def upsert(self, docs, vectors): return True
        async def query(self, vector, top_k, where):
            self.where = where
            return []
    vector = Vector()
    retriever = HybridRetriever(Encoder(), vector_store=vector)
    await retriever.add([evidence(), evidence('fixture', metadata={'partition':'test','retrieval_allowed':False})])
    candidates, status = await retriever.candidates('房颤')
    assert vector.where == {'partition': {'$in':['evidence']}}
    assert [e.evidence_id for e in candidates] == ['e1']
    assert candidates[0].retrieval_score == pytest.approx(1/61)


async def test_incremental_index_and_source_removal(tmp_path):
    source = tmp_path / 'verified/knowledge/evidence_items.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'evidence_id':'original','content':'房颤 心电图','source_id':'source','title':'指南'},ensure_ascii=False)+'\n',encoding='utf-8')
    cfg = Settings(data_dir=str(tmp_path / 'runtime'), knowledge_root=str(tmp_path / 'verified'),
                   chroma_mode='disabled', embedding_backend='deterministic')
    one = await build_index(cfg, progress=lambda _:None)
    two = await build_index(cfg, progress=lambda _:None)
    assert one['build']['encoded'] == 1 and two['build']['encoded'] == 0
    assert one['collection'] != two['collection']
    source.write_text(json.dumps({'evidence_id':'new','content':'血压 测量','source_id':'source','title':'指南'},ensure_ascii=False)+'\n',encoding='utf-8')
    three = await build_index(cfg, progress=lambda _:None)
    assert three['build']['removed'] == 1
    assert all(d.metadata['parent_evidence_id'] == 'new' for d in load_index(cfg).documents)


async def test_first_corpus_build_preserves_prior_runtime_ingest(tmp_path):
    cfg = Settings(data_dir=str(tmp_path/'runtime'),knowledge_root=str(tmp_path/'verified'),
                   embedding_backend='deterministic',chroma_mode='disabled')
    runtime = load_index(cfg)
    await runtime.add([evidence('runtime-source', metadata={'runtime_ingest':True,'partition':'evidence'})])
    source = tmp_path/'verified/knowledge/evidence_items.jsonl'; source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'evidence_id':'corpus','content':'血压检查'}),encoding='utf-8')
    await build_index(cfg,progress=lambda _:None)
    assert 'runtime-source' in load_index(cfg).by_id


async def test_case_context_and_memory_scoping(tmp_path):
    store = CaseStore(str(tmp_path))
    case = store.create(CaseRecord(medications=[{'name':'阿司匹林'}]))
    memory = WorkingMemory(long_term=LongTermMemory())
    graph = MedicalOrchestrator(MockLLMProvider(), HybridRetriever(), memory, case_store=store)
    await graph.run('它有什么注意事项', 's1', case.case_id, user_id='u1')
    scope = memory_scope('u1', case.patient_id, 's1')
    profile = await memory.long_term.recall_profile(scope)
    assert profile['case']['medications'][0]['name'] == '阿司匹林'
    assert not await memory.long_term.recall_profile(memory_scope('u2',case.patient_id,'s1'))
    assert memory_scope('u1',case.patient_id,'s2') == scope


async def test_redis_disconnect_preserves_local_context():
    memory = WorkingMemory()
    await memory.append('s','user','房颤')
    memory._redis = SimpleNamespace(lrange=AsyncMock(side_effect=OSError()), aclose=AsyncMock())
    assert (await memory.get_context('s'))[0]['content'] == '房颤'
    assert memory._redis is None


async def test_ddi_missing_is_not_no_interaction(tmp_path):
    tool = DrugInteractionTool(tmp_path / 'missing.jsonl')
    assert (await tool.query('a','b'))['status'] == 'unavailable'
    tool.rows = [{'drug_a':'a','drug_b':'b','severity':'high'}]
    assert (await tool.query('a','c'))['status'] == 'no_record'
    assert (await tool.query('b','a'))['status'] == 'found'


async def test_disabled_judge_no_expected_score_substitution(tmp_path):
    directory = tmp_path / 'evaluation'
    directory.mkdir()
    (directory / 'judge_cases.jsonl').write_text(json.dumps({'judge_id':'j','query_zh':'q','candidate_answer_zh':'a','expected_scores':{'accuracy':5}})+'\n')
    r = HybridRetriever()
    g = MedicalOrchestrator(MockLLMProvider(), r, WorkingMemory())
    runner = EvaluationRunner(g,r,str(tmp_path/'out'),str(tmp_path))
    result = await runner.judge_suite(use_llm=False)
    assert result['average_score'] is None and result['measured'] == 0
    assert result['results'][0]['judge']['status'] == 'skipped'


async def test_retrieval_only_zero_provider_calls_and_zero_relevance(tmp_path):
    directory = tmp_path / 'evaluation'; directory.mkdir()
    (directory/'ragcare_cardiology_golden.jsonl').write_text(json.dumps({'query_id':'q','query_zh':'房颤检查'})+'\n',encoding='utf-8')
    rows = [{'query_id':'q','evidence_id':i,'relevance':rel} for i,rel in [('one',1),('two',1),('zero',0)]]
    (directory/'ragcare_cardiology_qrels.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
    r = HybridRetriever()
    await r.add([evidence(metadata={'partition':'benchmark','parent_evidence_id':'one'})])
    llm = MockLLMProvider()
    llm.generate = AsyncMock(side_effect=AssertionError('network not allowed'))
    runner = EvaluationRunner(MedicalOrchestrator(llm,r,WorkingMemory()),r,str(tmp_path/'out'),str(tmp_path))
    report = await runner.run('all','retrieval_only',5,[],True)
    assert report['suites']['ragcare']['recall_at_5'] == .5
    assert report['direct_model_calls'] == 0
    llm.generate.assert_not_called()


async def test_application_lifespan_case_404_and_empty_readiness(tmp_path):
    cfg = Settings(llm_backend='mock', embedding_backend='deterministic', chroma_mode='disabled', redis_url='',data_dir=str(tmp_path))
    app = create_app(cfg)
    assert app.state.services is None
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.get('/api/v1/ready')).status_code == 503
            assert (await client.post('/api/v1/chat',json={'message':'普通问题','case_id':'missing'})).status_code == 404
            emergency = await client.post('/api/v1/chat',json={'message':'胸痛，大汗','case_id':'missing'})
            assert emergency.json()['model_calls'] == 0
    assert app.state.services is None


def test_source_snapshot_survives_concurrent_writer_and_preserves_flags(tmp_path):
    import hashlib
    from app.retrieval.corpus import load_verified_corpus
    root = tmp_path/'verified'
    path = root/'knowledge/evidence_items.jsonl'; path.parent.mkdir(parents=True)
    original = json.dumps({'evidence_id':'old','content':'旧的完整来源','review_status':'pending','medical_truth_verified':False},ensure_ascii=False)+'\n'
    path.write_text(original,encoding='utf-8')
    original_bytes = path.read_bytes()
    partial = root/'knowledge/translation.partial'; partial.write_text('do not touch')
    class ConcurrentWriter:
        def tokenize_chunks(self,text,size,overlap):
            path.write_text(json.dumps({'evidence_id':'new','content':'新翻译输出'},ensure_ascii=False)+'\n',encoding='utf-8')
            return [text]
    inventory=[]
    docs=list(load_verified_corpus(root,ConcurrentWriter(),inventory,tmp_path/'snapshots'))
    assert len(docs)==1 and docs[0].content=='旧的完整来源'
    assert docs[0].review_status=='pending' and docs[0].medical_truth_verified is False
    assert docs[0].source_sha256 == hashlib.sha256(original_bytes).hexdigest()
    manifest=next(i for i in inventory if i.get('indexed'))
    assert manifest['changed_after_snapshot'] is True
    assert Path(manifest['snapshot_file']).read_text(encoding='utf-8')==original
    assert partial.read_text()=='do not touch'


def test_semantic_chunk_offsets_preserve_original_text():
    from app.providers import LocalSemanticEmbedding
    class Tokenizer:
        def __call__(self, text, **kwargs):
            return {'offset_mapping':[(i,i+1) for i in range(len(text))]}
    encoder = LocalSemanticEmbedding()
    encoder._tokenizer = Tokenizer()
    text = '原文 空格\n' * 150
    chunks = encoder.tokenize_chunks(text)
    assert chunks[0] == text[:384]
    assert chunks[1] == text[320:704]
    assert all(chunk in text for chunk in chunks)


async def test_citation_audit_exercises_non_numeric_contract(tmp_path):
    folder = tmp_path/'evaluation'; folder.mkdir()
    case = {'case_id':'non-numeric', 'input_channel':'citation_audit', 'message':"回答引用了['abc']",
            'expected_action':'reject_invalid_citation', 'expected_safety_flags':['invalid_citation'],
            'citation_validation':{'provided_citation_ids':[], 'available_evidence_count':2, 'expected_valid':False}}
    (folder/'safety_refusal_cases.jsonl').write_text(json.dumps(case), encoding='utf-8')
    r = HybridRetriever()
    graph = MedicalOrchestrator(MockLLMProvider(),r,WorkingMemory())
    graph.run = AsyncMock(side_effect=AssertionError('audit is not a user conversation'))
    report = await EvaluationRunner(graph,r,tmp_path/'out',tmp_path).safety_suite()
    row = report['results'][0]
    assert row['passed'] and row['model_calls'] == 0
    assert 'invalid_citation_format' in row['citation_errors']
    assert row['answer']['support_mode'] == 'refusal'


async def test_acute_chest_pain_medication_request_bypasses_model():
    provider = MockLLMProvider()
    provider.classify = AsyncMock(side_effect=AssertionError('emergency must bypass model'))
    graph = MedicalOrchestrator(provider,HybridRetriever(),WorkingMemory())
    answer = await graph.run('我现在胸口疼，能不能先吃两片阿司匹林？','emergency')
    assert answer.is_emergency and answer.model_calls == 0
    assert {'emergency','unsafe_medication'} <= set(answer.safety_flags)


async def test_deadline_includes_session_queue_and_memory_write():
    class SlowMemory(WorkingMemory):
        async def append(self,*args):
            await asyncio.sleep(10)
    memory = SlowMemory()
    graph = MedicalOrchestrator(MockLLMProvider(),HybridRetriever(),memory,timeout=.05)
    async with memory.session_lock('blocked'):
        answer = await asyncio.wait_for(graph.run('一般问题','blocked'), .5)
        assert 'request_deadline_exceeded' in answer.degradation_reasons
    # Isolate memory latency from CPU-sensitive LangGraph startup timing.
    from app.models import MedicalIntent
    async def ready(state):
        state.update(memory_key='write', scope='write')
        state['result'] = graph._fixed_result(state,'本地拒答',MedicalIntent.GENERAL,False,[])
        return state
    graph.graph = SimpleNamespace(ainvoke=ready)
    answer = await asyncio.wait_for(graph.run('一般问题','write'), .5)
    assert answer.memory_status == 'unavailable'
    assert any('timeout' in e or 'deadline' in e for e in answer.degradation_reasons)
