"""Real E5 + Chroma smoke, with cross-process reopening and isolated collections."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from app.config import Settings
from app.memory import ChromaMemoryStore, LongTermMemory
from app.models import EvidenceItem
from app.retrieval.corpus import load_jsonl
from app.retrieval.index_manager import chroma, make_embedding
from app.retrieval.hybrid import HybridRetriever


async def reopen(path):
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    cfg = Settings.from_env()
    store = chroma(cfg,payload['collection'])
    memory_store = chroma(cfg,payload['memory_collection'])
    try:
        results = await store.query(payload['query_vector'],3,where={'partition':'evidence'})
        assert results[0].evidence_id == 'semantic-context'
        profile = await LongTermMemory(ChromaMemoryStore(memory_store)).recall_profile('semantic-scope')
        assert profile['user_statements'] == ['研究记忆样例']
        assert not await LongTermMemory(ChromaMemoryStore(memory_store)).recall_profile('another-scope')
        print(json.dumps({'reopened':True,'top_id':results[0].evidence_id,'memory_isolated':True}))
    finally:
        await store.close()
        await memory_store.close()


async def verify():
    cfg = Settings.from_env()
    output = Path(cfg.data_dir)/'verification'; output.mkdir(parents=True,exist_ok=True)
    embedding = make_embedding(cfg)
    row = next(load_jsonl(Path(cfg.knowledge_root)/'evaluation/ragcare_cardiology_golden.jsonl'))
    content = row.get('source_text_original') or row.get('source_text_zh')
    chunks = await asyncio.to_thread(embedding.tokenize_chunks,content)
    collection = 'cardio_semantic_smoke_' + uuid4().hex[:12]
    store = chroma(cfg,collection)
    memory_store = chroma(cfg,collection+'_memory')
    docs = [EvidenceItem(evidence_id='semantic-context',source_id=row['query_id'],title='RAGCare context',content=chunks[0],is_demo=False,
                         metadata={'partition':'evidence','retrieval_allowed':True}),
            EvidenceItem(evidence_id='control-space',source_id='smoke',title='Control',content='Astronomy studies stars, galaxies, and planetary orbits.',metadata={'partition':'evidence'}),
            EvidenceItem(evidence_id='excluded-answer',source_id='smoke',title='Fixture',content=row['query_zh'],metadata={'partition':'test','retrieval_allowed':False})]
    started = time.perf_counter()
    try:
        vectors = await embedding.embed_documents([d.content for d in docs])
        assert len(vectors[0]) == 384
        assert abs(sum(v*v for v in vectors[0])-1)<.001
        assert await store.upsert(docs,vectors)
        checks = []
        retriever = HybridRetriever(embedding, vector_store=store, semantic_min_score=cfg.semantic_min_score)
        retriever.by_id = {d.evidence_id:d for d in docs}
        retriever._rebuild()
        query_vector = None
        for query in [row['query_zh'],'What is the primary pathophysiologic defect in restrictive cardiomyopathy?']:
            query_vector = await embedding.embed_query(query)
            result = await store.query(query_vector,3,where={'partition':'evidence'})
            assert result and result[0].evidence_id == 'semantic-context'
            assert all(r.evidence_id != 'excluded-answer' for r in result)
            retrieved = await retriever.search(query)
            assert retrieved and retrieved[0].evidence_id == 'semantic-context'
            checks.append({'query':query,'top_id':result[0].evidence_id,'similarity':result[0].retrieval_score})
        unrelated = await retriever.search('How do I repair a bicycle puncture?')
        assert not unrelated, 'irrelevant question passed evidence sufficiency gate'
        memory = LongTermMemory(ChromaMemoryStore(memory_store))
        assert await memory.save_profile('semantic-scope',{'user_statements':['研究记忆样例']})
        state = output/'semantic-reopen.json'
        state.write_text(json.dumps({'collection':collection,'memory_collection':collection+'_memory','query_vector':query_vector}),encoding='utf-8')
        await store.close(); await memory_store.close()
        process = await asyncio.to_thread(subprocess.run,[sys.executable,'-m','scripts.verify_semantic','--reopen',str(state)],
            cwd=Path(__file__).resolve().parents[1],env=os.environ.copy(),capture_output=True,text=True,encoding='utf-8',
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=90)
        if process.returncode:
            raise RuntimeError('cross_process_reopen_failed:' + process.stderr[-1500:])
        report = {'status':'passed','dimension':384,'model':embedding.model_name,
                  'resolved_revision':getattr(embedding,'resolved_revision','unknown'),'checks':checks,
                  'cross_process':json.loads(process.stdout),'duration_seconds':round(time.perf_counter()-started,3)}
        (output/'semantic.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        await store.close(); await memory_store.close()


if __name__ == '__main__':
    asyncio.run(reopen(sys.argv[2]) if len(sys.argv)>1 and sys.argv[1]=='--reopen' else verify())
