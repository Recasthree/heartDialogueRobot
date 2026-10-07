import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.retrieval.build_job import build_index
from app.retrieval.disk import DiskRepository
from tests.test_disk_bilingual import settings, corpus, doc


async def test_incremental_only_adapts_changed_source_preserves_cache_and_base(tmp_path, monkeypatch):
    corpus(tmp_path, '房颤检查包括心电图')
    drug = tmp_path/'verified/drugs/cardiovascular_drugs_enriched_zh.jsonl'
    drug.parent.mkdir(parents=True)
    row = {'set_id':'aspirin-one','generic_name':'ASPIRIN','openfda_indications_and_usage':'aspirin usage',
           'openfda_indications_and_usage_zh':'阿司匹林的用途', 'translation_status':'translated_unverified'}
    drug.write_text(json.dumps(row,ensure_ascii=False)+'\n',encoding='utf-8')
    cfg = settings(tmp_path)
    first = await build_index(cfg,progress=lambda _:None)
    base = DiskRepository(Path(cfg.data_dir)/'indexes'/first['database'])
    original = [d.model_dump() for d in base.iter_docs()]
    row['openfda_indications_and_usage_zh'] = '阿司匹林的用途及注意事项'
    drug.write_text(json.dumps(row,ensure_ascii=False)+'\n',encoding='utf-8')
    from app.retrieval import build_job
    adapter = build_job.snapshot_items
    adapted = []
    def track(entry,*args):
        adapted.append(entry['file'])
        yield from adapter(entry,*args)
    monkeypatch.setattr(build_job,'snapshot_items',track)
    second = await build_index(cfg,stage='lexical',incremental=True,progress=lambda _:None)
    assert adapted == ['drugs/cardiovascular_drugs_enriched_zh.jsonl']
    fresh = DiskRepository(Path(cfg.data_dir)/'indexes'/second['database'])
    assert fresh.count() == base.count()
    assert all('阿司匹林的用途' not in d.content or '注意事项' in d.content for d in fresh.iter_docs())
    assert [d.model_dump() for d in base.iter_docs()] == original
    with fresh.connect() as db:
        assert db.execute('SELECT sum(vector_done) FROM docs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM vectors').fetchone()[0] == first['build']['encoded']
    done = await build_index(cfg,stage='vectors',resume=second['index_version'],progress=lambda _:None)
    assert done['build']['encoded'] == 1
    assert done['build']['reused_vectors'] == 2
    assert {d.translation_status for d in fresh.iter_docs("source_type='drug_label_machine_translation'")} == {'translated_unverified'}


async def test_incremental_prunes_removed_source_but_keeps_runtime_ingest(tmp_path):
    corpus(tmp_path)
    removed = tmp_path/'verified/knowledge/drug_labels.jsonl'
    removed.write_text(json.dumps({'id':'old','generic_name':'药名','openfda_warnings_and_precautions':'旧资料'})+'\n',encoding='utf-8')
    cfg = settings(tmp_path)
    first = await build_index(cfg,stage='lexical',progress=lambda _:None)
    repo = DiskRepository(Path(cfg.data_dir)/'indexes'/first['database'])
    runtime = doc('runtime'); runtime.metadata['runtime_ingest'] = True
    repo.upsert([runtime])
    removed.unlink()  # Only a test fixture in pytest's temporary directory.
    second = await build_index(cfg,stage='lexical',incremental=True,progress=lambda _:None)
    fresh = DiskRepository(Path(cfg.data_dir)/'indexes'/second['database'])
    assert fresh.count() == 2
    assert fresh.get('runtime') is not None
    assert not any(d.source_file == 'knowledge/drug_labels.jsonl' for d in fresh.iter_docs())
    assert repo.count() == 3


async def test_incremental_requires_compatible_model_and_never_replaces_active_on_failure(tmp_path):
    corpus(tmp_path); cfg = settings(tmp_path)
    first = await build_index(cfg,stage='lexical',progress=lambda _:None)
    active = Path(cfg.data_dir)/'indexes/active.json'
    before = active.read_bytes()
    # Model identity is part of the manifest; a different identity cannot reuse
    # old chunk/posting state even when vector dimensions happen to match.
    manifest = json.loads(before); manifest['embedding']['chunk_version'] = 'old-chunks'
    active.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='compatible_active_index'):
        await build_index(cfg,incremental=True,progress=lambda _:None)
    assert json.loads(active.read_text()) == manifest
    assert (Path(cfg.data_dir)/'indexes'/first['database']).exists()


def test_provenance_update_keeps_postings_and_tracks_seen_ids(tmp_path):
    repo = DiskRepository(tmp_path/'repo.db')
    item = doc('one'); item.source_file = 'drugs/a.jsonl'
    repo.upsert([item]); repo.prepare_source_tracking()
    with repo.connect() as db:
        db.execute("CREATE TRIGGER reject_fts_delete BEFORE DELETE ON evidence_fts_content BEGIN SELECT RAISE(ABORT,'postings rebuilt'); END")
    item.source_sha256 = 'updated-file-hash'
    repo.upsert([item],track_source='drugs/a.jsonl')
    assert repo.prune_source('drugs/a.jsonl') == 0
    assert repo.lexical('房颤',['evidence'])[0].source_sha256 == 'updated-file-hash'


async def test_incremental_checkpoint_survives_pause_before_pruning(tmp_path,monkeypatch):
    path=corpus(tmp_path)
    path.write_text(''.join(json.dumps({'id':str(n),'content':'旧的房颤资料 '+str(n)})+'\n' for n in range(140)),encoding='utf-8')
    cfg=settings(tmp_path)
    original=await build_index(cfg,stage='lexical',progress=lambda _:None)
    path.write_text(''.join(json.dumps({'id':str(n),'content':'更新的房颤资料 '+str(n)})+'\n' for n in range(140)),encoding='utf-8')
    from app.retrieval import build_job
    guard=build_job.resource_check
    def pause_after_batch(cfg,job,starting=False):
        if job.get('stage')=='lexical' and job.get('checkpoint',{}).get('offset',0)>0:
            raise build_job.BuildPaused('paused_memory')
        return guard(cfg,job,starting)
    monkeypatch.setattr(build_job,'resource_check',pause_after_batch)
    paused=await build_index(cfg,stage='lexical',incremental=True,progress=lambda _:None)
    assert paused['status']=='paused_memory'
    active=json.loads((Path(cfg.data_dir)/'indexes/active.json').read_text())
    assert active['index_version']==original['index_version']
    monkeypatch.setattr(build_job,'resource_check',guard)
    result=await build_index(cfg,stage='lexical',resume=paused['job_id'],progress=lambda _:None)
    repo=DiskRepository(Path(cfg.data_dir)/'indexes'/result['database'])
    assert repo.count()==140 and all(d.content.startswith('更新的') for d in repo.iter_docs())


async def test_vector_persistence_retries_only_transient_errors(monkeypatch):
    from app.retrieval.build_job import persist_batch
    monkeypatch.setattr('app.retrieval.build_job.asyncio.sleep',AsyncMock())
    store=SimpleNamespace(upsert=AsyncMock(side_effect=[False,True]),last_error='ReadTimeout',last_status_code=None)
    job={}
    await persist_batch(store,[],[],job,lambda:None)
    assert store.upsert.await_count==2 and job['write_retries']==1
    store.upsert=AsyncMock(return_value=False)
    store.last_error='HTTPStatusError'; store.last_status_code=400
    with pytest.raises(RuntimeError,match='vector_persistence_failed:HTTPStatusError'):
        await persist_batch(store,[],[],{},lambda:None)
    assert store.upsert.await_count==1
    store.upsert=AsyncMock(return_value=False)
    store.last_error='ReadTimeout'; store.last_status_code=None
    with pytest.raises(RuntimeError,match='ReadTimeout'):
        await persist_batch(store,[],[],{},lambda:None)
    assert store.upsert.await_count==3
