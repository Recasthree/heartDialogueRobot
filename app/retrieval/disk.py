"""Bounded SQLite/FTS5 retrieval. No corpus-sized Python collection is loaded."""
import asyncio
from array import array
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3

from ..models import EvidenceItem
from .hybrid import HybridRetriever, _terms, content_hash, partition, reciprocal_rank_fusion, SEARCH_STATUS


class DiskRepository:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS docs(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
              payload TEXT NOT NULL, tokens TEXT NOT NULL, scope TEXT NOT NULL, source_type TEXT,
              source_id TEXT, parent_id TEXT, verified INTEGER, runtime INTEGER, hash TEXT,
              vector_done INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS docs_scope ON docs(scope,source_type,verified);
            CREATE INDEX IF NOT EXISTS docs_parent ON docs(parent_id);
            CREATE INDEX IF NOT EXISTS docs_pending ON docs(vector_done,scope,rowid);
            CREATE INDEX IF NOT EXISTS docs_vector_pending_order ON docs(rowid) WHERE vector_done=0 AND scope!='test';
            CREATE TABLE IF NOT EXISTS vectors(model TEXT, hash TEXT, value BLOB, PRIMARY KEY(model,hash));
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS lexical_cache(key TEXT PRIMARY KEY,tokens TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(tokens);
            CREATE VIRTUAL TABLE IF NOT EXISTS benchmark_fts USING fts5(tokens);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA cache_size=-16384')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA mmap_size=0')
            db.execute('PRAGMA busy_timeout=30000')
            with db:
                yield db
        finally:
            db.close()

    def meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key,json.dumps(value)))

    def count(self, scope=None):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM docs' + (' WHERE scope=?' if scope else ''), (scope,) if scope else ()).fetchone()[0]

    def source_types(self):
        with self.connect() as db:
            return {r[0] for r in db.execute('SELECT DISTINCT source_type FROM docs')}

    def get(self, identity):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM docs WHERE id=?', (identity,)).fetchone()
        return EvidenceItem.model_validate_json(row[0]) if row else None

    def iter_docs(self, where='1', params=(), batch=128):
        last = 0
        while True:
            with self.connect() as db:
                rows = db.execute('SELECT rowid,payload FROM docs WHERE rowid>? AND ('+where+') ORDER BY rowid LIMIT ?', (last,*params,batch)).fetchall()
            if not rows:
                return
            for last, raw in rows:
                yield EvidenceItem.model_validate_json(raw)

    def upsert(self, docs, checkpoint=None, track_source=None):
        changed = 0
        with self.connect() as db:
            for d in docs:
                payload = d.model_dump_json()
                if track_source is not None:
                    db.execute('INSERT OR IGNORE INTO build_source_seen VALUES(?,?)',(track_source,d.evidence_id))
                old = db.execute('SELECT rowid,scope,payload,hash,tokens FROM docs WHERE id=?',(d.evidence_id,)).fetchone()
                if old and old[2] == payload:
                    continue
                scope, digest = partition(d), content_hash(d.content)
                tokens = ''
                if scope in {'evidence','benchmark'}:
                    digest_key = content_hash(d.title+'\0'+d.content)
                    cached_tokens = db.execute('SELECT tokens FROM lexical_cache WHERE key=?',(digest_key,)).fetchone()
                    tokens = cached_tokens[0] if cached_tokens else ' '.join(_terms(d.title+' '+d.content))
                    if not cached_tokens:
                        db.execute('INSERT INTO lexical_cache VALUES(?,?)',(digest_key,tokens))
                # Updating provenance alone must not rebuild the postings for
                # identical text. Filters still read the freshly updated docs row.
                lexical_changed = not old or old[1] != scope or old[4] != tokens
                if lexical_changed and old and old[1] in {'evidence','benchmark'}:
                    db.execute('DELETE FROM '+old[1]+'_fts WHERE rowid=?',(old[0],))
                db.execute('''INSERT INTO docs(id,payload,tokens,scope,source_type,source_id,parent_id,verified,runtime,hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,tokens=excluded.tokens,
                    scope=excluded.scope,source_type=excluded.source_type,source_id=excluded.source_id,parent_id=excluded.parent_id,
                    verified=excluded.verified,runtime=excluded.runtime,hash=excluded.hash,
                    vector_done=CASE WHEN docs.hash=excluded.hash AND docs.payload=excluded.payload THEN docs.vector_done ELSE 0 END''',
                    (d.evidence_id,payload,tokens,scope,d.source_type,d.source_id,d.metadata.get('parent_evidence_id',d.evidence_id),int(d.medical_truth_verified),int(bool(d.metadata.get('runtime_ingest'))),digest))
                rowid = old[0] if old else db.execute('SELECT rowid FROM docs WHERE id=?',(d.evidence_id,)).fetchone()[0]
                if lexical_changed and scope in {'evidence','benchmark'}:
                    db.execute('INSERT INTO '+scope+'_fts(rowid,tokens) VALUES(?,?)',(rowid,tokens))
                changed += 1
            if checkpoint is not None:
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('lexical_checkpoint',json.dumps(checkpoint)))
        return changed

    def prepare_source_tracking(self):
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS build_source_seen(file TEXT,id TEXT,PRIMARY KEY(file,id))')
            db.execute("CREATE INDEX IF NOT EXISTS docs_source_file ON docs(json_extract(payload,'$.source_file'))")

    def prune_source(self, source_file, guard=lambda: None):
        """Remove obsolete IDs only from this candidate index, in bounded batches."""
        removed = 0
        while True:
            guard()
            with self.connect() as db:
                ids = [r[0] for r in db.execute("""SELECT id FROM docs
                    WHERE json_extract(payload,'$.source_file')=? AND runtime=0
                    AND NOT EXISTS (SELECT 1 FROM build_source_seen s WHERE s.file=? AND s.id=docs.id)
                    LIMIT 256""",(source_file,source_file))]
            if not ids:
                return removed
            removed += self.delete(ids)

    def delete(self, ids):
        count = 0
        with self.connect() as db:
            for identity in ids:
                old = db.execute('SELECT rowid,scope FROM docs WHERE id=?',(identity,)).fetchone()
                if old:
                    if old[1] in {'evidence','benchmark'}:
                        db.execute('DELETE FROM '+old[1]+'_fts WHERE rowid=?',(old[0],))
                    db.execute('DELETE FROM docs WHERE rowid=?',(old[0],))
                    count += 1
        return count

    def lexical(self, query, partitions, source_types=None, include_unverified=True, top_k=20):
        terms = list(dict.fromkeys(_terms(query)))[:32]
        if not terms:
            return []
        match = ' OR '.join('"'+t.replace('"','""')+'"' for t in terms)
        results = []
        with self.connect() as db:
            for scope in partitions:
                if scope not in {'evidence','benchmark'}:
                    continue
                table = scope+'_fts'
                # Force ranked FTS iteration before primary-key metadata lookup. Letting
                # SQLite start at the scope index repeats MATCH for every corpus row.
                sql = 'SELECT d.payload,'+table+'.rank FROM '+table+' CROSS JOIN docs d ON d.rowid='+table+'.rowid WHERE '+table+' MATCH ? AND d.scope=?'
                params = [match,scope]
                if source_types:
                    sql += ' AND d.source_type IN ('+','.join('?' for _ in source_types)+')'
                    params.extend(source_types)
                if not include_unverified:
                    sql += ' AND d.verified=1'
                sql += ' ORDER BY '+table+'.rank LIMIT ?'
                for raw, score in db.execute(sql,(*params,top_k)):
                    d = EvidenceItem.model_validate_json(raw)
                    d.retrieval_score = -score
                    d.metadata['lexical_match'] = True
                    results.append(d)
        return sorted(results,key=lambda d:-d.retrieval_score)[:top_k]

    def cached(self, model, digest, dimension=None):
        return self.cached_many(model,[digest],dimension).get(digest)

    def cached_many(self,model,digests,dimension=None):
        digests=list(dict.fromkeys(digests)); result={d:None for d in digests}
        if not digests: return result
        with self.connect() as db:
            rows=db.execute('SELECT hash,value FROM vectors WHERE model=? AND hash IN ('+','.join('?' for _ in digests)+')',(model,*digests)).fetchall()
        for digest,blob in rows:
            values=array('f')
            try: values.frombytes(blob)
            except ValueError: continue
            if (dimension is not None and len(values)!=dimension) or any(not math.isfinite(v) for v in values) or abs(sum(v*v for v in values)-1)>.02:
                continue
            result[digest]=list(values)
        return result

    def cache(self, model, digest, values, dimension):
        if len(values) != dimension or any(not math.isfinite(v) for v in values) or abs(sum(v*v for v in values)-1) > .02:
            raise ValueError('invalid_cached_vector')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)',(model,digest,array('f',values).tobytes()))

    def mark_vectors(self, ids):
        with self.connect() as db:
            db.executemany('UPDATE docs SET vector_done=1 WHERE id=?',[(i,) for i in ids])


class DocumentView(Sequence):
    def __init__(self, repo): self.repo=repo
    def __len__(self): return self.repo.count()
    def __iter__(self): return self.repo.iter_docs()
    def __getitem__(self, i):
        if not isinstance(i,int): raise TypeError('Use paginated document iteration')
        with self.repo.connect() as db:
            row=db.execute('SELECT payload FROM docs ORDER BY rowid LIMIT 1 OFFSET ?',(i,)).fetchone()
        if not row: raise IndexError(i)
        return EvidenceItem.model_validate_json(row[0])


class IdentityView(Mapping):
    def __init__(self,repo): self.repo=repo
    def __len__(self): return self.repo.count()
    def __iter__(self): return (d.evidence_id for d in self.repo.iter_docs())
    def __getitem__(self,key):
        value=self.repo.get(key)
        if value is None: raise KeyError(key)
        return value


class DiskRetriever(HybridRetriever):
    def __init__(self, embedding, reranker=None, vector_store=None, index_path=None, semantic_min_score=.78):
        super().__init__(embedding,reranker,vector_store,None,semantic_min_score)
        self.index_path=Path(index_path)
        self.repo=DiskRepository(index_path)
        self.documents=DocumentView(self.repo)
        self.by_id=IdentityView(self.repo)
        self.index_version=self.repo.meta('index_version')
        self.lexical_ready=bool(self.repo.count('evidence')+self.repo.count('benchmark'))
        self.dense_ready=bool(vector_store)

    def source_types(self): return self.repo.source_types()

    def get_by_parent_ids(self, ids):
        ids=list(ids)
        if not ids: return []
        marks=','.join('?' for _ in ids)
        return list(self.repo.iter_docs('id IN ('+marks+') OR parent_id IN ('+marks+')',(*ids,*ids)))

    def runtime_ids(self, source):
        return [d.evidence_id for d in self.repo.iter_docs('runtime=1 AND source_id=?',(source,))]

    def set_version(self, version):
        self.repo.set_meta('index_version',version); self.index_version=version

    async def add(self, documents, progress=None):
        changed=encoded=0; errors=[]
        docs=list(documents)  # API import is bounded by its 50k input; corpus builder never calls this.
        async with self.write_lock:
            changed=await asyncio.to_thread(self.repo.upsert,docs)
            if self.vector_store:
                for offset in range(0,len(docs),8):
                    batch=docs[offset:offset+8]
                    vectors=await self.embedding.embed_documents([d.content for d in batch]); encoded+=len(batch)
                    if await self.vector_store.upsert(batch,vectors): self.repo.mark_vectors([d.evidence_id for d in batch])
                    else: errors.append('chroma_upsert_failed')
        self.lexical_ready=bool(self.repo.count())
        return {'ingested':True,'changed':changed,'encoded':encoded,'persisted':True,
                'vector_persisted':bool(self.vector_store) and not errors,'upserted':encoded if not errors else 0,
                'status':'partial' if errors else 'completed','errors':errors}

    async def remove(self,ids):
        ids=list(ids)
        if self.vector_store and not await self.vector_store.delete(ids): raise RuntimeError('chroma_delete_failed')
        return self.repo.delete(ids)

    async def rerank(self,query,candidates,top_k=5,enabled=True,query_variants=None):
        # Offline evaluation must also retain a genuine bilingual lexical match.
        from ..providers import MockReranker
        # DeepSeekReranker intentionally derives from MockReranker for the
        # deterministic interface, so isinstance() would incorrectly bypass
        # the provider exception boundary below.
        if enabled and type(self.reranker) is not MockReranker:
            # HybridRetriever owns the provider exception boundary. Invalid
            # DeepSeek JSON therefore falls back to the fused order instead
            # of escaping through /chat as an ASGI 500.
            return await super().rerank(query,candidates,top_k,enabled=True,query_variants=query_variants)
        if enabled:
            candidates = await self.reranker.rank(query,list(candidates)[:20])
        from ..tools.translation import local_variants
        variants=[query]
        if isinstance(query_variants, dict):
            variants.extend(v.get('query','') for v in query_variants.get('variants', []) if isinstance(v,dict))
        term_sets=[set(_terms(q)) for v in variants if v for q in local_variants(v)]
        scored=[]
        for d in candidates:
            text=d.content+' '+d.title; terms=set(_terms(text))
            score=max((len(ts & terms)/max(1,len(ts)) for ts in term_sets),default=0)
            if score>=.25 or d.metadata.get('semantic_score',0)>=self.semantic_min_score:
                scored.append((score,d))
        from .hybrid import select_supported
        return select_supported([d for _,d in sorted(scored,key=lambda p:-p[0])],top_k)

    async def candidates(self, query, *, partitions=None, source_types=None, include_unverified=True):
        from ..tools.translation import local_variants
        scopes=partitions or ['evidence']
        lists=[]
        for variant in local_variants(query):
            lists.append(await asyncio.to_thread(self.repo.lexical,variant,scopes,source_types,include_unverified))
        # Best language rank per identity: one lexical vote, not a vote per language.
        best={}
        for rows in lists:
            for rank,d in enumerate(rows):
                if d.evidence_id not in best or rank<best[d.evidence_id][0]: best[d.evidence_id]=(rank,d)
        lexical=[pair[1] for pair in sorted(best.values(),key=lambda p:(p[0],p[1].evidence_id))][:20]
        channels=[lexical]
        status={'bm25':'available' if self.repo.count() else 'unavailable','chroma':'unavailable','partitions':scopes,'errors':[]}
        if self.dense_ready and self.vector_store and self.vector_store.collection and self.embedding.semantic:
            filters=[{'partition':{'$in':scopes}}]
            if source_types: filters.append({'source_type':{'$in':list(source_types)}})
            if not include_unverified: filters.append({'medical_truth_verified':True})
            try:
                vector=await asyncio.wait_for(self.embedding.embed_query(query),35)
                rows=await self.vector_store.query(vector,top_k=20,where=filters[0] if len(filters)==1 else {'$and':filters})
                dense=[]
                for d in rows:
                    if d.retrieval_score>=self.semantic_min_score and self.repo.get(d.evidence_id):
                        d.metadata['semantic_score']=d.retrieval_score; dense.append(d)
                channels.append(dense); status['chroma']='available'
            except Exception as exc: status['errors'].append('vector_unavailable:'+type(exc).__name__)
        fused=reciprocal_rank_fusion(channels)[:20]
        status.update(status='available' if status['chroma']=='available' else 'degraded' if self.repo.count() else 'unavailable',candidate_ids=[d.evidence_id for d in fused])
        SEARCH_STATUS.set(status)
        return fused,status
