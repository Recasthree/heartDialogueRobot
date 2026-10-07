"""Persisted BM25 inverted index plus filtered Chroma retrieval, fused once."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from ..models import EvidenceItem
from ..providers import DeterministicEmbeddingProvider, MockReranker, record_error

SEARCH_STATUS = ContextVar("search_status", default=None)
STOP = set("的 了 和 与 或 是 在 有 我 你 他 它 这 那 吗 呢 啊 请 问 什么 如何 怎么 可以 是否 应该 哪些 需要 进行 了解 问题".split())


def content_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]


def stable_evidence_id(source_file, source_id, section, content):
    return "ev-" + content_hash("\0".join([str(source_file), str(source_id), str(section), content]))


def dedup_by_hash(chunks):
    # Legacy helper only; production fusion deduplicates identity, not provenance.
    seen, result = set(), []
    for item in chunks:
        key = content_hash(item.content)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def reciprocal_rank_fusion(ranked_lists, k=60):
    scores, documents = {}, {}
    for ranked in ranked_lists:
        seen = set()
        for rank, document in enumerate(ranked, 1):
            key = document.evidence_id
            if key in seen:
                continue
            seen.add(key)
            if key in documents:
                documents[key] = documents[key].model_copy(update={'metadata': {**documents[key].metadata, **document.metadata}})
            else:
                documents[key] = document
            scores[key] = scores.get(key, 0.) + 1. / (k + rank)
    return [documents[key].model_copy(update={"retrieval_score": scores[key]})
            for key in sorted(scores, key=lambda key: (-scores[key], key))]


def _terms(text):
    text = (text or "").lower()
    try:
        import jieba
        jieba.setLogLevel(40)
        tokens = jieba.lcut(text)
    except (ImportError, AttributeError):
        tokens = re.findall(r"[\u4e00-\u9fff]{2}|[A-Za-z0-9_]+", text)
    return tuple(t for t in tokens if t not in STOP and re.search(r"[\w\u4e00-\u9fff]", t))


def select_supported(items, top_k):
    """Reserve a source slot only AFTER the relevance gate, never before it."""
    selected = items[:top_k]
    if selected and not any(e.metadata.get('external') for e in selected):
        external = next((e for e in items[top_k:] if e.metadata.get('external')), None)
        if external is not None:
            selected = selected[:-1] + [external]
    return selected


def partition(item):
    return item.metadata.get("partition", "evidence" if item.metadata.get("retrieval_allowed", True) else "test")


class BM25Index:
    def __init__(self):
        self.documents = []
        self.term_frequencies = []
        self.postings = defaultdict(dict)
        self.lengths = []
        self.average_length = 0.
        self.rebuild_count = 0
        self.scope_stats = {}

    def rebuild(self, documents, frequencies=None):
        self.rebuild_count += 1
        self.scope_stats = {}
        self.documents = list(documents)
        self.term_frequencies = frequencies if frequencies is not None else [
            Counter(_terms(d.content + " " + d.title)) for d in self.documents]
        self.postings = defaultdict(dict)
        self.lengths = [sum(counts.values()) for counts in self.term_frequencies]
        for i, counts in enumerate(self.term_frequencies):
            for term, count in counts.items():
                self.postings[term][i] = count
        self.average_length = sum(self.lengths) / max(1, len(documents))
        self.index_by_id = {d.evidence_id: i for i,d in enumerate(self.documents)}

    def search(self, query, top_k=20, allowed=None):
        scores = defaultdict(float)
        if allowed is not None:
            scope_key = frozenset(allowed)
            if scope_key not in self.scope_stats:
                members = {self.index_by_id[key] for key in allowed}
                self.scope_stats[scope_key] = (members, sum(self.lengths[i] for i in members)/max(1,len(members)))
            members, average_length = self.scope_stats[scope_key]
        else:
            members, average_length = None, self.average_length
        n = len(allowed) if allowed is not None else len(self.documents)
        for term in set(_terms(query)):
            posting = self.postings.get(term, {})
            df = len(posting.keys() & members) if members is not None else len(posting)
            idf = math.log(1 + (n - df + .5) / (df + .5))
            for i, tf in posting.items():
                doc = self.documents[i]
                if allowed is not None and doc.evidence_id not in allowed:
                    continue
                scores[i] += idf * tf * 2.5 / (tf + 1.5 * (.25 + .75 * self.lengths[i] / max(1, average_length)))
        return [self.documents[i].model_copy(update={"retrieval_score": scores[i]})
                for i in sorted(scores, key=lambda i: (-scores[i], self.documents[i].evidence_id))[:top_k]]


class HybridRetriever:
    def source_types(self):
        return {d.source_type for d in self.documents}

    def runtime_ids(self, source):
        return [d.evidence_id for d in self.documents if d.metadata.get('runtime_ingest') and d.source_id==source]

    def __init__(self, embedding=None, reranker=None, vector_store=None, index_path=None, semantic_min_score=.78):
        self.embedding = embedding or DeterministicEmbeddingProvider()
        self.reranker = reranker or MockReranker()
        self.vector_store = vector_store
        self.documents = []
        self.by_id = {}
        self.bm25 = BM25Index()
        self.index_path = Path(index_path) if index_path else None
        self.index_version = None
        self.semantic_min_score = semantic_min_score
        self.write_lock = asyncio.Lock()
        self.last_write = {}
        if self.index_path and self.index_path.exists():
            self.load()

    @contextmanager
    def _db(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.index_path)
        db.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, payload TEXT, tokens TEXT, vector TEXT, vector_persisted INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS vector_cache (hash TEXT PRIMARY KEY, vector TEXT)")
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self):
        with self._db() as db:
            rows = db.execute("SELECT payload, tokens FROM documents ORDER BY id").fetchall()
            version = db.execute("SELECT value FROM metadata WHERE key='index_version'").fetchone()
        self.documents = [EvidenceItem.model_validate_json(r[0]) for r in rows]
        self.by_id = {d.evidence_id: d for d in self.documents}
        self.bm25.rebuild(self.documents, [json.loads(r[1]) for r in rows])
        self.index_version = version[0] if version else None

    def _rebuild(self):
        self.documents = list(self.by_id.values())
        self.bm25.rebuild(self.documents)

    async def add(self, documents, progress=None):
        docs = list(documents)
        async with self.write_lock:
            pending = [d for d in docs if d.evidence_id not in self.by_id or
                       d.model_dump() != self.by_id[d.evidence_id].model_dump()]
            persisted, encoded, failed = 0, 0, []
            # Retry unpersisted rows on subsequent builds.
            if self.index_path:
                with self._db() as db:
                    dirty = {r[0] for r in db.execute("SELECT id FROM documents WHERE vector_persisted=0")}
                known = {d.evidence_id for d in pending}
                pending.extend(d for d in docs if d.evidence_id in dirty and d.evidence_id not in known)
            for offset in range(0, len(pending), 64):
                batch = pending[offset:offset+64]
                vectors = []
                missing, positions = [], []
                cache = {}
                if self.index_path:
                    with self._db() as db:
                        for d in batch:
                            row = db.execute("SELECT vector FROM vector_cache WHERE hash=?", (content_hash(d.content),)).fetchone()
                            if row:
                                cache[content_hash(d.content)] = json.loads(row[0])
                unique_missing = {}
                for i, d in enumerate(batch):
                    if partition(d) == 'test':
                        vectors.append([])
                        continue
                    key = content_hash(d.content)
                    vectors.append(cache.get(key))
                    if vectors[-1] is None:
                        unique_missing.setdefault(key, d.content)
                        positions.append((i, key))
                missing = list(unique_missing.values())
                if missing:
                    new = await self.embedding.embed_documents(missing)
                    encoded += len(new)
                    cache.update(dict(zip(unique_missing, new)))
                    for i, key in positions:
                        vectors[i] = cache[key]
                vector_rows = [(d,v) for d,v in zip(batch,vectors) if v]
                ok = not vector_rows or bool(self.vector_store and await self.vector_store.upsert([d for d,v in vector_rows], [v for d,v in vector_rows]))
                persisted += len(vector_rows) if ok else 0
                if not ok and self.vector_store:
                    failed.append("chroma_upsert_failed")
                if self.index_path:
                    with self._db() as db:
                        db.executemany("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)", [
                            (d.evidence_id, d.model_dump_json(), json.dumps(Counter(_terms(d.content + " " + d.title)), ensure_ascii=False),
                             json.dumps(v), int(ok or not v)) for d, v in zip(batch, vectors)])
                        db.executemany('INSERT OR IGNORE INTO vector_cache VALUES (?,?)',
                            [(content_hash(d.content), json.dumps(v)) for d,v in vector_rows])
                for d in batch:
                    self.by_id[d.evidence_id] = d
                if progress:
                    progress({"processed": offset+len(batch), "total": len(pending), "encoded": encoded})
            if pending:
                if self.index_path:
                    self.load()
                else:
                    self._rebuild()
            self.last_write = {"ingested": True, "changed": len(pending), "encoded": encoded,
                               "status": "partial" if failed else "completed",
                               "persisted": bool(self.index_path),
                               "vector_persisted": not failed and bool(self.vector_store),
                               "upserted": persisted, "errors": sorted(set(failed))}
            return self.last_write

    async def remove(self, ids):
        ids = set(ids) & set(self.by_id)
        if self.vector_store and getattr(self.vector_store, 'mode', '') != 'disabled' and not await self.vector_store.delete(ids):
            raise RuntimeError("chroma_delete_failed")
        if self.index_path:
            with self._db() as db:
                db.executemany("DELETE FROM documents WHERE id=?", [(i,) for i in ids])
        for i in ids:
            self.by_id.pop(i, None)
        if ids:
            self._rebuild()
        return len(ids)

    def set_version(self, version):
        self.index_version = version
        if self.index_path:
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('index_version', ?)", (version,))

    def get_by_parent_ids(self, ids):
        ids = set(ids)
        return [d for d in self.documents if d.evidence_id in ids or d.metadata.get("parent_evidence_id") in ids]

    async def candidates(self, query, *, partitions=None, source_types=None, include_unverified=True):
        partitions = partitions or ["evidence"]
        allowed = {d.evidence_id for d in self.documents if partition(d) in partitions
                   and (not source_types or d.source_type in source_types)
                   and (include_unverified or d.medical_truth_verified)}
        status = {"bm25": "available" if self.documents else "unavailable", "chroma": "unavailable",
                  "partitions": partitions, "candidate_ids": [], "errors": []}
        if not allowed:
            SEARCH_STATUS.set(status)
            return [], status
        lexical = self.bm25.search(query, 20, allowed=allowed)
        lists = [lexical]
        for d in lexical:
            d.metadata = {**d.metadata, "lexical_match": True}
        if self.vector_store and self.vector_store.collection and getattr(self.embedding, "semantic", False):
            filters = [{"partition": {"$in": partitions}}]
            if source_types:
                filters.append({"source_type": {"$in": list(source_types)}})
            if not include_unverified:
                filters.append({"medical_truth_verified": True})
            where = filters[0] if len(filters) == 1 else {"$and": filters}
            try:
                vector = await asyncio.wait_for(self.embedding.embed_query(query), 35)
                dense = await self.vector_store.query(vector, top_k=20, where=where)
                # The additional identity guard excludes stale rows without changing filter scope.
                dense = [d for d in dense if d.evidence_id in allowed and d.retrieval_score >= self.semantic_min_score]
                for d in dense:
                    d.metadata = {**d.metadata, "semantic_score": d.retrieval_score}
                lists.append(dense)
                status["chroma"] = "available"
            except Exception as exc:
                status["errors"].append("vector_unavailable:" + type(exc).__name__)
        fused = reciprocal_rank_fusion(lists)[:20]
        status["candidate_ids"] = [d.evidence_id for d in fused]
        status["status"] = "available" if status["chroma"] == "available" else "degraded"
        SEARCH_STATUS.set(status)
        return fused, status

    async def rerank(self, query, candidates, top_k=5, enabled=True, query_variants=None):
        ranked = list(candidates)[:20]
        if enabled:
            try:
                ranked = await self.reranker.rank(query, ranked)
            except Exception as exc:
                record_error("rerank_fallback:" + type(exc).__name__)
        # Use both the user's language and approved bilingual variants. A
        # Chinese-only gate incorrectly removes English PubMed abstracts.
        variants = [query]
        if isinstance(query_variants, dict):
            variants.extend(v.get('query', '') for v in query_variants.get('variants', []) if isinstance(v, dict))
        elif isinstance(query_variants, (list, tuple)):
            variants.extend(str(v) for v in query_variants)
        from ..tools.translation import glossary_plan
        variants.extend(v['query'] for v in glossary_plan(query).get('variants', []))
        term_sets = [set(_terms(v)) for v in variants if v]
        selected = []
        for d in ranked:
            content_terms = set(_terms(d.content + " " + d.title))
            overlap = max((len(terms & content_terms) / max(1, len(terms)) for terms in term_sets), default=0)
            supported = (d.rerank_score >= .35 if d.rerank_score is not None else
                         overlap >= .25 or d.metadata.get("semantic_score", 0) >= self.semantic_min_score)
            if supported:
                selected.append(d)
        return select_supported(selected, top_k)

    async def search(self, query, top_k=5, include_unverified=True, include_test_data=False,
                     partitions=None, source_types=None, rerank=True):
        scope = partitions or (["evidence", "benchmark"] if include_test_data else ["evidence"])
        candidates, _ = await self.candidates(query, partitions=scope, source_types=source_types, include_unverified=include_unverified)
        return await self.rerank(query, candidates, top_k, enabled=rerank)
