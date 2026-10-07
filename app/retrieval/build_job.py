"""Recoverable two-stage index construction with bounded memory and immutable sources."""
from array import array
import asyncio
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from uuid import uuid4

from .corpus import CHUNK_VERSION, snapshot_inventory, snapshot_items
from .disk import DiskRepository
from .hybrid import content_hash


class BuildPaused(RuntimeError): pass


async def persist_batch(store, docs, vectors, job, guard):
    """Bounded retry of an idempotent upsert; never mark an uncertain write done."""
    if not store:
        return
    for attempt in range(3):
        guard()
        if await store.upsert(docs,vectors):
            job['last_vector_write_error']=None
            return
        reason=getattr(store,'last_error',None) or 'unknown'
        status=getattr(store,'last_status_code',None)
        job['last_vector_write_error']=reason
        job['last_vector_write_status_code']=status
        retryable=reason in {'ReadTimeout','WriteTimeout','ConnectTimeout','ConnectError','PoolTimeout','RemoteProtocolError'} or status in {408,429,500,502,503,504}
        if not retryable or attempt==2:
            raise RuntimeError('vector_persistence_failed:'+reason)
        job['write_retries']=job.get('write_retries',0)+1
        await asyncio.sleep(attempt+1)


def atomic_json(path,value):
    path=Path(path); tmp=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for attempt in range(10):
            try:
                os.replace(tmp,path)
                break
            except PermissionError:
                # Windows readers/AV can briefly hold a destination without
                # delete-sharing. Keep the old complete file and retry replacement.
                if attempt==9: raise
                time.sleep(min(.02*2**attempt,.25))
    finally:
        tmp.unlink(missing_ok=True)


def resource_check(cfg, job, starting=False):
    import psutil
    proc=psutil.Process()
    rss=proc.memory_info().rss
    for child in proc.children(recursive=True):
        try: rss+=child.memory_info().rss
        except psutil.Error: pass
    free=psutil.virtual_memory().available
    disk=shutil.disk_usage(cfg.data_dir).free
    job['peak_rss_bytes']=max(job.get('peak_rss_bytes',0),rss)
    job['available_memory_bytes']=free
    job['free_disk_bytes']=disk
    if disk < (cfg.build_start_free_disk_gib if starting else cfg.build_min_free_disk_gib)*1024**3:
        raise BuildPaused('paused_disk')
    if free < cfg.build_min_free_memory_gib*1024**3 or rss>cfg.build_max_rss_gib*1024**3:
        raise BuildPaused('paused_memory')


class EncoderProcess:
    def __init__(self,cfg,identity): self.cfg,self.identity,self.process=cfg,identity,None
    async def encode(self,texts):
        if self.cfg.embedding_backend=='deterministic':
            from .index_manager import make_embedding
            return await make_embedding(self.cfg).embed_documents(texts)
        if self.process is None:
            config={'model_name':self.identity['model'],'cache_dir':self.cfg.model_cache_dir,
                    'revision':self.identity['resolved_revision'],'local_only':True,'batch_size':self.cfg.embedding_batch_size}
            env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','PYTHONPATH'}}
            env.update(PYTHONIOENCODING='utf-8',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='2')
            self.process=await asyncio.create_subprocess_exec(sys.executable,'-m','app.retrieval.encoder_worker',json.dumps(config),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env,limit=4194304,
                **({'creationflags':0x08000000} if os.name=='nt' else {}))
        self.process.stdin.write((json.dumps({'texts':texts},ensure_ascii=True)+'\n').encode())
        await self.process.stdin.drain()
        line=await asyncio.wait_for(self.process.stdout.readline(),180)
        if not line: raise RuntimeError('encoder_worker_exited')
        result=json.loads(line)
        if 'error' in result: raise RuntimeError('encoder_'+result['error'])
        vectors=result.get('vectors')
        if not isinstance(vectors,list) or len(vectors)!=len(texts): raise RuntimeError('encoder_invalid_batch')
        for vector in vectors:
            if len(vector)!=self.identity['dimension'] or any(not math.isfinite(v) for v in vector) or abs(sum(v*v for v in vector)-1)>.02:
                raise RuntimeError('encoder_invalid_vector')
        return vectors
    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try: await asyncio.wait_for(self.process.wait(),3)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait()


def identity_for(cfg,embedding):
    resolved=cfg.embedding_revision
    if embedding.semantic:
        from huggingface_hub import try_to_load_from_cache
        cached=try_to_load_from_cache(cfg.embedding_model,'config.json',cache_dir=cfg.model_cache_dir,revision=cfg.embedding_revision)
        if isinstance(cached,str): resolved=Path(cached).parent.name
    return {'model':embedding.model_name,'revision':embedding.revision,'resolved_revision':resolved,
            'dimension':embedding.dimension,'chunk_version':CHUNK_VERSION,'encoding':'passage-normalized-f32-v1' if embedding.semantic else 'deterministic-v1'}


async def import_legacy(path,repo,model,encoder,identity,job,save,guard):
    old=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        tables={r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'vectors' in tables:  # Already fingerprinted v2 cache.
            cursor=old.execute('SELECT hash,value FROM vectors WHERE model=? ORDER BY hash',(model,))
            while rows:=cursor.fetchmany(256):
                guard()
                valid=[]
                for digest,blob in rows:
                    vals=array('f'); vals.frombytes(blob)
                    if len(vals)!=identity['dimension'] or any(not math.isfinite(v) for v in vals) or abs(sum(v*v for v in vals)-1)>.02:
                        job['invalid_cache_rows']=job.get('invalid_cache_rows',0)+1; continue
                    valid.append((model,digest,blob))
                with repo.connect() as db: db.executemany('INSERT OR IGNORE INTO vectors VALUES(?,?,?)',valid)
            job['recovery_validated']=True; save(); return
        if not {'documents','vector_cache'}<=tables:
            raise ValueError('unsupported_recovery_database')
        if not job.get('recovery_validated'):
            if old.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('recovery_database_corrupt')
            sample=old.execute('SELECT payload,vector FROM documents WHERE length(vector)>10 ORDER BY id LIMIT 32').fetchall()
            if len(sample)<32 and identity['dimension']==384: raise ValueError('insufficient_legacy_verification_samples')
            for offset in range(0,len(sample),8):
                guard(); batch=sample[offset:offset+8]
                texts=[json.loads(row[0])['content'] for row in batch]
                actual=await encoder.encode(texts)
                job['recovery_validation_encoded']=job.get('recovery_validation_encoded',0)+len(actual)
                for (_,raw),vector in zip(batch,actual):
                    expected=json.loads(raw)
                    if len(expected)!=identity['dimension'] or any(not math.isfinite(v) for v in expected): raise ValueError('legacy_vector_incompatible')
                    if max(abs(a-b) for a,b in zip(vector,expected))>1e-4: raise ValueError('legacy_model_fingerprint_mismatch')
            job['recovery_validated']=True; save()
        last=repo.meta('legacy_cache_cursor','')
        while True:
            guard()
            rows=old.execute('SELECT hash,vector FROM vector_cache WHERE hash>? ORDER BY hash LIMIT 256',(last,)).fetchall()
            if not rows: break
            valid=[]
            for digest,raw in rows:
                vals=json.loads(raw)
                if len(vals)!=identity['dimension'] or any(not math.isfinite(v) for v in vals) or abs(sum(v*v for v in vals)-1)>.02:
                    job['invalid_cache_rows']=job.get('invalid_cache_rows',0)+1; continue
                valid.append((model,digest,array('f',vals).tobytes()))
            last=rows[-1][0]
            with repo.connect() as db:
                db.executemany('INSERT OR IGNORE INTO vectors VALUES(?,?,?)',valid)
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('legacy_cache_cursor',json.dumps(last)))
            job['imported_cache_rows']=job.get('imported_cache_rows',0)+len(valid); save()
    finally: old.close()


def seed_incremental(previous, database, identity, guard):
    """Consistent disk backup; never mutate the active/legacy database or collection."""
    if not previous or previous.get('schema_version') != 2 or previous.get('embedding') != identity:
        raise ValueError('incremental_requires_compatible_active_index')
    source = database.parent / previous['database']
    if source.resolve() == database.resolve() or not source.exists():
        raise ValueError('invalid_incremental_source')
    fd, temporary = tempfile.mkstemp(prefix=database.stem+'-seed-',suffix='.sqlite3',dir=database.parent)
    os.close(fd)
    try:
        old = sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
        candidate = sqlite3.connect(temporary)
        try:
            candidate.execute('PRAGMA cache_size=-16384')
            old.backup(candidate,pages=256,progress=lambda *_:guard())
            if candidate.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('incremental_source_corrupt')
            # A fresh Chroma collection has no remotely verified rows yet.
            candidate.execute('UPDATE docs SET vector_done=0 WHERE vector_done!=0')
            candidate.execute("DELETE FROM metadata WHERE key IN ('lexical_checkpoint','legacy_cache_cursor','index_version','inherited_cache_rows')")
            candidate.execute('DROP TABLE IF EXISTS build_source_seen')
            candidate.commit()
            candidate.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            old.close(); candidate.close()
        os.replace(temporary,database)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def build_index(cfg,download=False,limit=None,progress=print,*,stage='all',resume=None,recover_from=None,activate=True,incremental=False):
    from .index_manager import make_embedding,chroma,read_active
    if stage not in {'all','lexical','vectors'}: raise ValueError('invalid_build_stage')
    if incremental and limit: raise ValueError('incremental_limit_not_supported')
    root=Path(cfg.data_dir)/'indexes'; root.mkdir(parents=True,exist_ok=True)
    jobs=root/'jobs'; jobs.mkdir(exist_ok=True)
    if resume and not re.fullmatch(r'[A-Za-z0-9-]{1,100}',resume): raise ValueError('invalid_job_id')
    jobfile=jobs/(resume+'.json') if resume else jobs/(time.strftime('%Y%m%d-%H%M%S-')+uuid4().hex[:8]+'.json')
    previous=read_active(cfg)
    job=json.loads(jobfile.read_text(encoding='utf-8')) if resume else {
        'job_id':jobfile.stem,'stage':'snapshot','status':'running','started_at':time.time(),
        'encoded':0,'reused_vectors':0,'upserted':0,'limited_build':limit is not None,
        'incremental':incremental,
        'recover_from':str(Path(recover_from).resolve()) if recover_from else None,
        'previous_database':previous['database'] if previous else 'runtime-v2.sqlite3'}
    database=root/('disk-'+job['job_id']+'.sqlite3')
    job['database']=database.name
    job['pid']=os.getpid()
    job['run_started_at']=time.time()
    def save():
        job['updated_at']=time.time(); atomic_json(jobfile,job); atomic_json(root/'build-status.json',job)
        progress(json.dumps({k:v for k,v in job.items() if k not in {'sources','embedding'}},ensure_ascii=False))
    def guard(): resource_check(cfg,job)
    encoder=None; store=None
    # Advisory single-writer ownership is scoped to this job and released by process exit.
    from filelock import FileLock
    with FileLock(str(jobfile.with_suffix('.lock')),timeout=0):
        try:
            resource_check(cfg,job,starting=not resume)
            embedding=make_embedding(cfg,download)
            identity=identity_for(cfg,embedding)
            if job.get('embedding') and job['embedding']!=identity: raise ValueError('resume_embedding_mismatch')
            job['embedding']=identity
            job.pop('error',None)
            model=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            if not job.get('sources'):
                job['sources']=await asyncio.to_thread(snapshot_inventory,cfg.knowledge_root,Path(cfg.data_dir)/'source_snapshots'); save()
            # Resume is pinned to immutable snapshots, never to moving translation output.
            for entry in job['sources']:
                if entry.get('indexed'):
                    p=Path(entry['snapshot_file'])
                    if not p.exists() or p.stat().st_size!=entry['bytes']: raise ValueError('snapshot_missing_or_changed')
                    digest=hashlib.sha256()
                    with p.open('rb') as stream:
                        while block:=stream.read(1024*1024): digest.update(block)
                    if digest.hexdigest()!=entry['sha256']: raise ValueError('snapshot_hash_changed')
            if job.get('incremental') and not job.get('lexical_seeded'):
                job.update(stage='lexical_seed',status='running'); save()
                await asyncio.to_thread(seed_incremental,previous,database,identity,guard)
                old_sources={e['file']:e for e in previous['sources'] if e.get('indexed')}
                new_sources={e['file']:e for e in job['sources'] if e.get('indexed')}
                job['unchanged_sources']=[name for name,entry in new_sources.items() if name in old_sources
                    and all(old_sources[name].get(k)==entry.get(k) for k in ['sha256','partition'])]
                job['removed_sources']=sorted(set(old_sources)-set(new_sources))
                job['incremental_base']=previous['index_version']
                job['lexical_seeded']=True
                # The backup contains fingerprinted BLOBs; cached_many validates
                # every reused vector. No reimport/reencoding of the legacy cache.
                job['recovery_complete']=True
                save()
            repo=DiskRepository(database)
            if job.get('incremental'):
                repo.prepare_source_tracking()
                job['inherited_cache_rows']=repo.meta('inherited_cache_rows')
                if job['inherited_cache_rows'] is None:
                    with repo.connect() as db:
                        job['inherited_cache_rows']=db.execute('SELECT count(*) FROM vectors WHERE model=?',(model,)).fetchone()[0]
                    repo.set_meta('inherited_cache_rows',job['inherited_cache_rows'])
            if not job.get('lexical_ready'):
                if stage=='vectors': raise ValueError('lexical_stage_required')
                job.update(stage='lexical',status='running'); save()
                checkpoint=repo.meta('lexical_checkpoint',{'file':0,'offset':0,'chunk':0})
                def lexical():
                    stopped=False
                    for index,entry in enumerate(job['sources']):
                        if index<checkpoint['file'] or not entry.get('indexed') or entry['file'] in job.get('unchanged_sources',[]): continue
                        offset=checkpoint['offset'] if index==checkpoint['file'] else 0
                        chunk=checkpoint['chunk'] if index==checkpoint['file'] else 0
                        batch=[]; position=None
                        for item,(offset,chunk) in snapshot_items(entry,embedding,offset,chunk):
                            batch.append(item); position={'file':index,'offset':offset,'chunk':chunk}
                            if len(batch)>=128:
                                guard(); repo.upsert(batch,position,entry['file'] if job.get('incremental') else None); batch=[]
                                job['documents']=repo.count(); job['checkpoint']=position; save()
                            if limit and repo.count()+len(batch)>=limit: stopped=True; break
                        if batch: repo.upsert(batch,position,entry['file'] if job.get('incremental') else None)
                        if job.get('incremental'):
                            job['pruned']=job.get('pruned',0)+repo.prune_source(entry['file'],guard)
                        repo.set_meta('lexical_checkpoint',{'file':index+1,'offset':0,'chunk':0})
                        if stopped: break
                    for source in job.get('removed_sources',[]):
                        job['pruned']=job.get('pruned',0)+repo.prune_source(source,guard)
                    oldpath=root/job['previous_database']
                    if oldpath.exists() and oldpath!=database:
                        old=sqlite3.connect(oldpath.resolve().as_uri()+'?mode=ro',uri=True)
                        try:
                            table='docs' if old.execute("SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone() else 'documents'
                            cursor=old.execute('SELECT payload FROM '+table+" WHERE json_extract(payload,'$.metadata.runtime_ingest')=1")
                            from ..models import EvidenceItem
                            while rows:=cursor.fetchmany(128): repo.upsert([EvidenceItem.model_validate_json(r[0]) for r in rows])
                            if table=='docs':
                                with repo.connect() as db:
                                    db.execute('ATTACH DATABASE ? AS old',(str(oldpath),))
                                    job['removed']=db.execute('SELECT count(*) FROM old.docs WHERE id NOT IN (SELECT id FROM main.docs)').fetchone()[0]
                        finally: old.close()
                await asyncio.to_thread(lexical)
                if not repo.count(): raise RuntimeError('empty_corpus: no index activated')
                repo.set_meta('index_version',job['job_id'])
                with repo.connect() as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('lexical_integrity_failed')
                    for scope in ('evidence','benchmark'):
                        if db.execute('SELECT count(*) FROM '+scope+'_fts').fetchone()[0]!=repo.count(scope): raise RuntimeError('fts_count_mismatch')
                job.update(lexical_ready=True,stage='lexical_ready',documents=repo.count()); save()
            def publish(dense):
                manifest={'schema_version':2,'index_version':job['job_id'],'generation':job['job_id']+('-dense' if dense else '-lexical'),
                    'database':database.name,'collection':job.get('collection'),'embedding':identity,'sources':job['sources'],
                    'documents':repo.count(),'lexical_ready':True,'dense_ready':dense,'limited_build':job['limited_build'],
                    'partition_counts':{s:repo.count(s) for s in ['evidence','benchmark','test']},
                    'build':{k:job.get(k,0) for k in ['encoded','reused_vectors','upserted','removed','peak_rss_bytes']}}
                atomic_json(root/('manifest-'+job['job_id']+'.json'),manifest)
                if activate and not job['limited_build']: atomic_json(root/'active.json',manifest)
                return manifest
            manifest=publish(job.get('dense_ready',False) if stage=='lexical' else False)
            if stage=='lexical': job.update(status='lexical_ready'); save(); return manifest
            encoder=EncoderProcess(cfg,identity)
            recovery=job.get('recover_from')
            if not recovery and (root/job['previous_database']).exists(): recovery=str(root/job['previous_database'])
            if recovery and not job.get('recovery_complete'):
                job.update(stage='recovery',status='running'); save()
                try:
                    await import_legacy(recovery,repo,model,encoder,identity,job,save,guard)
                    job['recovery_complete']=True
                except ValueError as exc:
                    job['recovery_error']=str(exc); job['recovery_complete']=True
                save()
                await encoder.close()
                encoder=EncoderProcess(cfg,identity)
            job['collection']=job.get('collection') or 'cardio_disk_'+job['job_id'].replace('-','_')
            if cfg.chroma_mode!='disabled': store=await asyncio.to_thread(chroma,cfg,job['collection'])
            if store and not store.collection: raise RuntimeError('chroma_service_unavailable')
            if store and resume:
                # Do not trust even our own persisted flags after a remote restart/deletion.
                with repo.connect() as db: maximum=db.execute('SELECT max(rowid) FROM docs').fetchone()[0] or 0
                for offset in range(0,maximum,256):
                    guard()
                    with repo.connect() as db:
                        ids=[r[0] for r in db.execute('SELECT id FROM docs WHERE rowid>? AND rowid<=? AND vector_done=1',(offset,offset+256))]
                    if ids:
                        found=set((await store.get(ids=ids,include=[])).get('ids',[]))
                        with repo.connect() as db: db.executemany('UPDATE docs SET vector_done=0 WHERE id=?',[(i,) for i in ids if i not in found])
            with repo.connect() as db: job['upserted']=db.execute('SELECT count(*) FROM docs WHERE vector_done=1').fetchone()[0]
            job.update(stage='vectors',status='running'); save()
            while True:
                guard()
                with repo.connect() as db:
                    rows=db.execute("SELECT id,payload,hash FROM docs INDEXED BY docs_vector_pending_order WHERE vector_done=0 AND scope!='test' ORDER BY rowid LIMIT ?",(cfg.vector_write_batch_size,)).fetchall()
                if not rows: break
                from ..models import EvidenceItem
                docs=[EvidenceItem.model_validate_json(r[1]) for r in rows]
                cache=repo.cached_many(model,[r[2] for r in rows],identity['dimension']); missing={}
                for d,(_,_,digest) in zip(docs,rows):
                    if cache[digest] is None: missing[digest]=d.content
                    else: job['reused_vectors']+=1
                if missing:
                    pending=list(missing.items())
                    for offset in range(0,len(pending),cfg.embedding_batch_size):
                        guard(); batch=pending[offset:offset+cfg.embedding_batch_size]
                        start=time.perf_counter()
                        vectors=await encoder.encode([text for _,text in batch])
                        job['encoding_seconds']=job.get('encoding_seconds',0)+time.perf_counter()-start
                        for (digest,_),value in zip(batch,vectors): repo.cache(model,digest,value,identity['dimension']); cache[digest]=value
                        job['encoded']+=len(vectors)
                guard()
                await persist_batch(store,docs,[cache[r[2]] for r in rows],job,guard)
                repo.mark_vectors([d.evidence_id for d in docs]); job['upserted']+=len(docs)
                if time.time()-job.get('updated_at',0)>2: save()
            job.update(stage='verify'); save()
            expected=repo.count('evidence')+repo.count('benchmark')
            if store:
                if await asyncio.to_thread(store.collection.count)!=expected: raise RuntimeError('chroma_count_mismatch')
                # Verify every identity in bounded pages; no complete ID set in RAM.
                with repo.connect() as db: maximum=db.execute('SELECT max(rowid) FROM docs').fetchone()[0] or 0
                for offset in range(0,maximum,256):
                    guard()
                    with repo.connect() as db: ids=[r[0] for r in db.execute("SELECT id FROM docs WHERE rowid>? AND rowid<=? AND scope!='test'",(offset,offset+256))]
                    if ids and set((await store.get(ids=ids,include=[])).get('ids',[]))!=set(ids): raise RuntimeError('chroma_identity_mismatch')
            job.update(status='ready',stage='ready',dense_ready=bool(store),finished_at=time.time()); save()
            job['elapsed_seconds']=round(job['finished_at']-job['started_at'],3)
            job['encoding_throughput_docs_per_second']=round(job['encoded']/max(1,job.get('encoding_seconds',0)),3)
            job['chroma_verified_count']=expected if store else None
            save()
            return publish(bool(store))
        except BuildPaused as exc:
            job.update(status=str(exc),error=str(exc)); save(); return job
        except BaseException as exc:
            job.update(status='interrupted' if isinstance(exc,(KeyboardInterrupt,asyncio.CancelledError)) else 'failed',error=type(exc).__name__+':'+str(exc)[:250]); save(); raise
        finally:
            if encoder: await encoder.close()
            if store: await store.close()
