import json
from dataclasses import replace
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import EvidenceItem
from app.providers import DeterministicEmbeddingProvider, MockReranker
from app.retrieval.disk import DiskRepository, DiskRetriever, DocumentView
from app.retrieval.build_job import build_index, resource_check, BuildPaused, atomic_json
from app.retrieval.index_manager import load_index
from app.retrieval.live import LiveRetriever
from app.tools.contracts import ToolContext
from app.tools.translation import TranslationTools, glossary_plan, local_variants, protected, restore_translation


def doc(eid, text='房颤检查包括心电图。', scope='evidence', source='clinical', **kw):
    return EvidenceItem(evidence_id=eid,source_id=eid,source_type=source,title=eid,content=text,
        is_demo=False,metadata={'partition':scope},**kw)


def settings(tmp_path):
    return Settings(data_dir=str(tmp_path/'runtime'),knowledge_root=str(tmp_path/'verified'),embedding_backend='deterministic',
        chroma_mode='disabled',build_min_free_memory_gib=0,build_start_free_disk_gib=0,build_min_free_disk_gib=0,build_max_rss_gib=100)


def corpus(tmp_path, text='房颤检查包括心电图。'):
    path=tmp_path/'verified/knowledge/evidence_items.jsonl'; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'evidence_id':'source','content':text},ensure_ascii=False)+'\n',encoding='utf-8')
    return path


def test_sql_filters_before_limit_and_identity(tmp_path):
    repo=DiskRepository(tmp_path/'index.db')
    repo.upsert([doc(str(i),source='exclude') for i in range(30)]+[doc('a',source='keep'),doc('b',source='keep'),doc('bench',scope='benchmark'),doc('test',scope='test')])
    assert {d.evidence_id for d in repo.lexical('房颤',['evidence'],['keep'])}=={'a','b'}
    assert [d.evidence_id for d in repo.lexical('房颤',['benchmark'])]==['bench']
    assert repo.lexical('房颤',['test'])==[]
    assert repo.lexical('房颤',['evidence'],include_unverified=False)==[]
    assert repo.count()==34
    repo.upsert([doc('a',text='冠心病治疗',source='keep')])
    assert [d.evidence_id for d in repo.lexical('房颤',['evidence'],['keep'])]==['b']
    with repo.connect() as db:
        plan=db.execute('EXPLAIN QUERY PLAN SELECT d.payload,evidence_fts.rank FROM evidence_fts CROSS JOIN docs d ON d.rowid=evidence_fts.rowid WHERE evidence_fts MATCH ? AND d.scope=? ORDER BY evidence_fts.rank LIMIT 20',('房颤','evidence')).fetchall()
    assert 'VIRTUAL TABLE' in plan[0][-1] and not any('TEMP B-TREE' in p[-1] for p in plan)


@pytest.mark.asyncio
async def test_disk_restart_no_encoding_no_bm25_rebuild(tmp_path):
    e=DeterministicEmbeddingProvider(); e.embed_documents=AsyncMock(side_effect=AssertionError('no encoding'))
    r=DiskRetriever(e,MockReranker(),index_path=tmp_path/'db')
    r.bm25.rebuild=lambda *a:pytest.fail('in memory rebuild')
    await r.add([doc('en','Atrial fibrillation diagnostic tests include electrocardiogram and Holter monitoring.')])
    r=DiskRetriever(e,MockReranker(),index_path=tmp_path/'db')
    rows,status=await r.candidates('房颤的常见检查有哪些？')
    assert isinstance(r.documents,DocumentView) and rows[0].evidence_id=='en'
    assert status['bm25']=='available' and status['chroma']=='unavailable'
    assert r.embedding.embed_documents.await_count==0
    assert (await r.search('房颤的常见检查有哪些？',rerank=False))[0].evidence_id=='en'
    assert await r.search('火星上的足球队队长是谁？',rerank=False)==[]


@pytest.mark.asyncio
async def test_stages_resume_cache_and_snapshot_integrity(tmp_path):
    path=corpus(tmp_path); cfg=settings(tmp_path)
    first=await build_index(cfg,stage='lexical',progress=lambda x:None)
    assert first['lexical_ready'] and not first['dense_ready'] and first['build']['encoded']==0
    path.write_text('source changed but frozen snapshot must remain usable',encoding='utf-8')
    second=await build_index(cfg,stage='vectors',resume=first['index_version'],progress=lambda x:None)
    assert second['build']['encoded']==1
    again=await build_index(cfg,stage='vectors',resume=first['index_version'],progress=lambda x:None)
    assert again['build']['encoded']==1
    from pathlib import Path
    snap=Path(next(s for s in first['sources'] if s.get('indexed'))['snapshot_file'])
    data=snap.read_bytes(); snap.write_bytes(b'x'+data[1:])
    with pytest.raises(ValueError,match='snapshot_hash'):
        await build_index(cfg,resume=first['index_version'],progress=lambda x:None)


@pytest.mark.asyncio
async def test_pause_never_publishes_empty_index(tmp_path):
    corpus(tmp_path); cfg=replace(settings(tmp_path),build_min_free_memory_gib=1e6)
    result=await build_index(cfg,progress=lambda x:None)
    assert result['status']=='paused_memory'
    assert not (tmp_path/'runtime/indexes/active.json').exists()


@pytest.mark.asyncio
async def test_generation_is_pinned_during_atomic_switch(tmp_path):
    path=corpus(tmp_path); cfg=settings(tmp_path)
    a=await build_index(cfg,stage='lexical',progress=lambda x:None)
    live=LiveRetriever(cfg,load_index(cfg))
    async with live.lease():
        path.write_text(json.dumps({'evidence_id':'new','content':'高血压指南'})+'\n',encoding='utf-8')
        b=await build_index(cfg,stage='lexical',progress=lambda x:None)
        await live.refresh()
        assert live.index_version==a['index_version']
    async with live.lease(): assert live.index_version==b['index_version']
    await live.close()


@pytest.mark.parametrize('query',['房颤的常见检查有哪些？','What diagnostic tests check atrial fibrillation?','房颤 diagnostic tests'])
def test_bilingual_disease_and_goal(query):
    plan=glossary_plan(query)
    assert any('atrial fibrillation' in v['query'] and 'diagnostic' in v['query'] for v in plan['variants'])


def test_composite_goals_and_alternatives():
    plan=glossary_plan('房颤的检查方法有哪些，要不要早上空腹？')
    assert set(plan['goals'])=={'diagnosis','preparation'}
    english=[v['query'] for v in plan['variants'] if v['language']=='en']
    assert len(set(english))==4 and any('fasting' in q for q in english)
    assert '心房颤动' in ' '.join(local_variants('房颤检查'))
    assert all(sum(v['goal']==goal for v in plan['variants'])<=2 for goal in plan['goals'])


def test_translation_protects_numbers_drugs_negation():
    original='Do not take aspirin 5 mg with warfarin.'
    masked,mapping=protected(original)
    result=restore_translation(original,'不要将 __P0__ __P1__ 与 __P2__ 合用。',mapping)
    assert '5 mg' in result and 'aspirin' in result
    for invalid in ['可以服用 __P0__ __P1__ 与 __P2__。','不要服用 __P0__。','不要服用 __P0__ __P1__ __P2__，每日 8 次。']:
        with pytest.raises(ValueError): restore_translation(original,invalid,mapping)


def test_corrupt_blob_is_not_reused(tmp_path):
    repo=DiskRepository(tmp_path/'db')
    with repo.connect() as db:
        db.execute('INSERT INTO vectors VALUES(?,?,?)',('m','bad',b'broken'))
    assert repo.cached('m','bad',384) is None


def test_atomic_manifest_retries_windows_reader_lock(tmp_path,monkeypatch):
    import os
    original=os.replace; attempts=[]
    def locked(src,dst):
        attempts.append(1)
        if len(attempts)<3: raise PermissionError('sharing violation')
        return original(src,dst)
    monkeypatch.setattr('app.retrieval.build_job.os.replace',locked)
    atomic_json(tmp_path/'status.json',{'complete':True})
    assert len(attempts)==3 and json.loads((tmp_path/'status.json').read_text())['complete']


@pytest.mark.asyncio
async def test_failed_remote_write_resumes_cached_encoding(tmp_path,monkeypatch):
    corpus(tmp_path); cfg=replace(settings(tmp_path),chroma_mode='http')
    class Store:
        def __init__(self): self.collection=self; self.ok=False; self.ids=set()
        async def upsert(self,docs,vectors):
            if not self.ok: return False
            self.ids.update(d.evidence_id for d in docs); return True
        async def get(self,ids,include): return {'ids':[i for i in ids if i in self.ids]}
        def count(self): return len(self.ids)
        async def close(self): pass
    store=Store()
    monkeypatch.setattr('app.retrieval.index_manager.chroma',lambda *args:store)
    with pytest.raises(RuntimeError,match='vector_persistence_failed'):
        await build_index(cfg,progress=lambda x:None)
    from pathlib import Path
    job=json.loads((Path(cfg.data_dir)/'indexes/build-status.json').read_text())
    assert job['encoded']==1 and job['upserted']==0
    assert not json.loads((Path(cfg.data_dir)/'indexes/active.json').read_text())['dense_ready']
    store.ok=True
    result=await build_index(cfg,resume=job['job_id'],stage='vectors',progress=lambda x:None)
    assert result['dense_ready'] and result['build']['encoded']==1
    # Losing a remote row must trigger idempotent re-upsert, not false readiness.
    store.ids.clear()
    repaired=await build_index(cfg,resume=job['job_id'],stage='vectors',progress=lambda x:None)
    assert len(store.ids)==1 and repaired['build']['encoded']==1


@pytest.mark.asyncio
async def test_english_network_candidate_survives_chinese_shortlist():
    from app.core.agentic import AgenticFlow
    local=[doc(str(i),'房颤检查方法有哪些 要不要空腹') for i in range(25)]
    external=doc('network','Electrocardiography and Holter monitoring are used to evaluate atrial fibrillation.')
    external.metadata['external']=True
    retriever=SimpleNamespace(rerank=AsyncMock(return_value=[external]))
    flow=AgenticFlow(SimpleNamespace(retriever=retriever,gateway=object()))
    state={'query':'房颤检查','rewritten_query':'房颤检查','evidence':local+[external],'tool_context':ToolContext('s'),'errors':[],'tools':[]}
    await flow.rerank(state)
    shortlisted=retriever.rerank.call_args.args[1]
    assert len(shortlisted)==20 and any(e.evidence_id=='network' for e in shortlisted)
    assert retriever.rerank.await_count==1


@pytest.mark.asyncio
async def test_translation_scoped_cached_and_trust_unchanged(tmp_path):
    llm=SimpleNamespace(model='mock',_json=AsyncMock(return_value={'translations':[{'evidence_id':'en','text':'房颤可使用心电图检查。'}]}))
    tool=TranslationTools(settings(tmp_path),llm)
    evidence=doc('en','Atrial fibrillation can be evaluated using an electrocardiogram.')
    ctx=ToolContext('s',network=True,evidence={'en':evidence})
    result=await tool.evidence(['en'],'zh',ctx)
    assert result['translations']['en']['display_translation_status']=='machine_unreviewed'
    assert (await tool.evidence(['en'],'zh',ctx))['cache_hit']
    assert llm._json.await_count==1 and not evidence.medical_truth_verified and evidence.review_status=='pending'
    with pytest.raises(PermissionError): await tool.evidence(['other'],'zh',ctx)
    query=await tool.query('房颤检查')
    assert query['method']=='glossary' and llm._json.await_count==1


def test_glossary_preserves_arrhythmia_symptom_goal():
    plan = glossary_plan('心律不齐的症状是什么样的')
    assert 'symptoms' in plan['goals']
    assert any('arrhythmia' in item['query'] and 'symptoms' in item['query']
               for item in plan['variants'] if item['language'] == 'en')
