"""Explicit, small paid-model probe. Credentials are read locally, never serialized."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import sys

from app.config import Settings
from app.core.graph import MedicalOrchestrator
from app.memory import ChromaMemoryStore, LongTermMemory, WorkingMemory
from app.models import CaseRecord
from app.providers import CALL_CONTEXT, DeepSeekProvider, DeepSeekReranker
from app.retrieval.corpus import load_jsonl
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.index_manager import chroma, make_embedding
from app.store import CaseStore


async def run():
    cfg = Settings.from_env()
    if cfg.llm_backend != 'deepseek' or not cfg.deepseek_api_key:
        raise RuntimeError('Configure LLM_BACKEND=deepseek and a local key before this explicit probe')
    output = Path(cfg.data_dir)/'verification'; output.mkdir(parents=True,exist_ok=True)
    fixture = json.loads((output/'semantic-reopen.json').read_text(encoding='utf-8'))
    vector = chroma(cfg,fixture['collection'])
    memory_store = chroma(cfg,fixture['memory_collection'])
    llm = DeepSeekProvider(cfg.deepseek_api_key,cfg.aihubmix_base_url,cfg.deepseek_model,timeout=30,max_retries=0)
    started = time.perf_counter()
    try:
        raw = await vector.get(include=['documents','metadatas'])
        docs = [vector._item(i,d,m) for i,d,m in zip(raw['ids'],raw['documents'],raw['metadatas'])]
        retriever = HybridRetriever(make_embedding(cfg),DeepSeekReranker(llm),vector,semantic_min_score=cfg.semantic_min_score)
        retriever.by_id = {d.evidence_id:d for d in docs}; retriever._rebuild()
        await asyncio.to_thread(retriever.embedding._load)
        with tempfile.TemporaryDirectory(prefix='cardio-live-') as directory:
            cases = CaseStore(directory)
            case = cases.create(CaseRecord(age=58,chief_complaint='合成研究病例',medications=[{'name':'阿司匹林'}]))
            memory = WorkingMemory(long_term=LongTermMemory(ChromaMemoryStore(memory_store)))
            graph = MedicalOrchestrator(llm,retriever,memory,case_store=cases,trace_dir=output/'live-traces',timeout=90)
            row = next(load_jsonl(Path(cfg.knowledge_root)/'evaluation/ragcare_cardiology_golden.jsonl'))
            scenarios = [('emergency','胸痛，大汗',None),('source_grounded',row['query_zh'],None),
                         ('case_context','我正在服用的药有什么一般注意事项？',case.case_id),
                         ('drug_interaction','它和华法林一起吃有什么相互作用？',case.case_id)]
            if '--grounded-only' in sys.argv:
                scenarios = [scenarios[1]]
            details = []
            for name,query,cid in scenarios:
                answer = await graph.run(query,'live-case' if cid else 'live-'+name,cid,user_id='research-smoke')
                trace = graph.traces[answer.trace_id]
                details.append({'scenario':name,'response':answer.model_dump(mode='json'),'route':trace['route'],'duration_ms':trace['duration_ms']})
                print(json.dumps({'scenario':name,'model_calls':answer.model_calls,'support_mode':answer.support_mode,'degradation':answer.degradation_reasons},ensure_ascii=False),flush=True)
            grounded = next(d['response'] for d in details if d['scenario']=='source_grounded')
            cited = [d for d in docs if d.evidence_id in {c['evidence_id'] for c in grounded['citations']}]
            ledger = {'model_calls':0,'successful_calls':0,'errors':[]}
            token = CALL_CONTEXT.set(ledger)
            try:
                judge = await llm.judge(row['query_zh'],grounded['answer'],cited,reference=row.get('reference_answer_zh',''),citations=grounded['citations'])
            finally:
                CALL_CONTEXT.reset(token)
            report = {'status':'completed','scope':'isolated real-corpus semantic smoke collection, not full-corpus evaluation',
                      'model':cfg.deepseek_model,'model_calls':sum(d['response']['model_calls'] for d in details)+ledger['model_calls'],
                      'duration_seconds':round(time.perf_counter()-started,3),'scenarios':details,'judge':judge}
            report['grounded_answer_passed'] = grounded['support_mode']=='cited' and bool(grounded['citations'])
            report['status'] = 'passed' if report['grounded_answer_passed'] and judge.get('status')=='completed' else 'partial'
            filename = 'live-grounded.json' if '--grounded-only' in sys.argv else 'live-model.json'
            (output/filename).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'model_calls':report['model_calls'],'judge_status':judge.get('status'),'duration_seconds':report['duration_seconds']},ensure_ascii=False))
    finally:
        await llm.close(); await vector.close(); await memory_store.close()


if __name__ == '__main__':
    asyncio.run(run())
