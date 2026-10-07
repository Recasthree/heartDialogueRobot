"""Evaluate every RAGCare context without waiting for the larger clinical corpus build.

Uses the same adapter, encoder, BM25, Chroma, parent IDs and runner as production.
Only benchmark contexts are candidates; gold questions/answers are never evidence.
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.core.graph import MedicalOrchestrator
from app.evaluation import EvaluationRunner
from app.memory import WorkingMemory
from app.providers import MockLLMProvider
from app.retrieval.corpus import chunk_text, item_from_row, load_jsonl
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.index_manager import make_embedding, chroma


async def run():
    cfg = Settings.from_env()
    folder=Path(cfg.data_dir)/'verification'; folder.mkdir(parents=True,exist_ok=True)
    embedding=make_embedding(cfg)
    await asyncio.to_thread(embedding._load)
    source=Path(cfg.knowledge_root)/'evaluation/ragcare_cardiology_golden.jsonl'
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    version='ragcare-'+uuid4().hex[:12]
    vector=chroma(cfg,'cardio_'+version.replace('-','_'))
    retriever=HybridRetriever(embedding,vector_store=vector,index_path=folder/(version+'.sqlite3'))
    docs=[]
    started=time.perf_counter()
    for index,row in enumerate(load_jsonl(source)):
        context='\n'.join(str(row[k]) for k in ['source_text_zh','source_text_original'] if row.get(k))
        for parent in row.get('relevant_evidence_ids',[]):
            for n,chunk in enumerate(chunk_text(context,embedding)):
                clean={k:row[k] for k in ['review_status','medical_truth_verified','translation_status'] if k in row}
                clean['title']='RAGCare context '+parent
                docs.append(item_from_row(clean,'evaluation/'+source.name,'benchmark_context',sha,index,
                                          f'context:{n}',chunk,'benchmark',parent))
    try:
        stats=await retriever.add(docs)
        if not stats['vector_persisted']:
            raise RuntimeError('benchmark_vector_persistence_failed')
        retriever.set_version(version)
        graph=MedicalOrchestrator(MockLLMProvider(),retriever,WorkingMemory())
        runner=EvaluationRunner(graph,retriever,cfg.evaluation_dir,cfg.knowledge_root)
        report=await runner.run('ragcare','retrieval_only',32,[],False)
        result={'status':'completed','scope':'all 32 RAGCare benchmark contexts; independent of full clinical corpus activation',
                'run_id':report['run_id'],'collection':vector.collection_name,'database':str(retriever.index_path),
                'documents':len(retriever.documents),'model_calls':report['model_calls'],
                'embedding':embedding.model_name,'revision':embedding.resolved_revision,
                'duration_seconds':round(time.perf_counter()-started,3),
                'metrics':{k:v for k,v in report['suites']['ragcare'].items() if k!='results'}}
        (folder/'ragcare.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        await vector.close()


if __name__=='__main__':
    asyncio.run(run())
