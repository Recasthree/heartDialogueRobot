import asyncio
import hashlib
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from .privacy import redact_payload, redact_text


def memory_scope(user_id=None, patient_id=None, session_id=None):
    # User is part of the scope even when a case is shared.
    raw = f"user:{user_id or 'local'}|patient:{patient_id}" if patient_id else (
        f"user:{user_id}" if user_id else f"session:{session_id}")
    return hashlib.sha256(raw.encode()).hexdigest()


class WorkingMemory:
    def __init__(self, redis_url=None, ttl=3600, max_messages=20, long_term=None):
        self.redis_url, self.ttl, self.max_messages = redis_url, ttl, max_messages
        self._local, self._expires = defaultdict(list), {}
        self._redis = None
        self.long_term = long_term
        self._locks = {}
        self.last_error = None
        self.turns = defaultdict(int)

    @asynccontextmanager
    async def session_lock(self, key):
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(key, None)

    async def connect(self):
        if self.redis_url:
            try:
                from redis.asyncio import from_url
                self._redis = from_url(self.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
                await self._redis.ping()
            except Exception:
                await self._disconnect("redis_connect_failed")

    async def _disconnect(self, reason):
        old, self._redis = self._redis, None
        self.last_error = reason
        if old:
            try:
                await old.aclose()
            except Exception:
                pass

    async def get_context(self, session_id):
        if self._redis:
            try:
                raw = await self._redis.lrange("session:" + session_id, -self.max_messages, -1)
                result = redact_payload([json.loads(r) for r in raw])
                self._local[session_id] = result
                self._expires[session_id] = time.time() + self.ttl
                return result
            except Exception:
                await self._disconnect("redis_read_failed")
        if self._expires.get(session_id, 0) < time.time():
            self._local.pop(session_id, None)
        return list(self._local.get(session_id, []))[-self.max_messages:]

    async def append(self, session_id, role, content, evidence_ids=None):
        message = {"role": role, "content": redact_text(content[:4000])[0], "evidence_ids": evidence_ids or []}
        self._local[session_id].append(message)
        self._local[session_id] = self._local[session_id][-self.max_messages:]
        self._expires[session_id] = time.time() + self.ttl
        if role == "user":
            self.turns[session_id] += 1
        if self._redis:
            try:
                pipe = self._redis.pipeline()
                pipe.rpush("session:" + session_id, json.dumps(message, ensure_ascii=False))
                pipe.ltrim("session:" + session_id, -self.max_messages, -1)
                pipe.expire("session:" + session_id, self.ttl)
                await pipe.execute()
            except Exception:
                await self._disconnect("redis_write_failed")

    def redact_for_log(self, value):
        return redact_payload(value)

    async def close(self):
        await self._disconnect(self.last_error)


class ChromaMemoryStore:
    """Metadata-scoped persistence, never part of the medical evidence collection."""
    def __init__(self, chroma_store):
        self.store = chroma_store
        self.last_error = None

    async def save(self, identity, content, metadata):
        if not self.store or not self.store.collection:
            self.last_error = "chroma_unavailable"
            return False
        try:
            await asyncio.to_thread(self.store.collection.upsert, ids=[identity],
                documents=[redact_text(content)[0]], embeddings=[[1.0]],
                metadatas=[{**metadata, "updated_at": time.time()}])
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = type(exc).__name__
            return False

    async def search(self, query="", where=None, top_k=3):
        if not self.store or not self.store.collection:
            self.last_error = "chroma_unavailable"
            return []
        try:
            rows = await self.store.get(where=where, include=["documents", "metadatas"])
            results = [{"content": d, **(m or {})} for d, m in zip(rows.get("documents", []), rows.get("metadatas", []))]
            self.last_error = None
            return sorted(results, key=lambda x: x.get("updated_at", 0), reverse=True)[:top_k]
        except Exception as exc:
            self.last_error = type(exc).__name__
            return []


class LongTermMemory:
    def __init__(self, chroma_store=None, summary_store=None):
        self.profiles, self.summaries = {}, {}
        self.chroma_store = chroma_store
        self.summary_store = summary_store or chroma_store
        self._profile_locks = defaultdict(asyncio.Lock)

    async def merge_profile(self, scope, case, statement):
        async with self._profile_locks[scope]:
            current = dict(await self.recall_profile(scope))
            if case:
                current['case'] = case
            current['user_statements'] = (current.get('user_statements', []) + [statement])[-12:]
            return await self.save_profile(scope, current)

    async def save_profile(self, user_id, profile):
        profile = redact_payload(profile)
        self.profiles[user_id] = profile
        return bool(self.chroma_store and await self.chroma_store.save(
            "profile:" + user_id, json.dumps(profile, ensure_ascii=False), {"scope": user_id, "kind": "profile"}))

    async def recall_profile(self, user_id, query=""):
        if self.chroma_store:
            rows = await self.chroma_store.search(query, where={"$and": [{"scope": user_id}, {"kind": "profile"}]}, top_k=1)
            if rows:
                return redact_payload(json.loads(rows[0]["content"]))
        return self.profiles.get(user_id, {})

    async def save_summary(self, session_id, summary, scope=None):
        scope = scope or session_id
        self.summaries[(scope, session_id)] = redact_text(summary[:4000])[0]
        return bool(self.summary_store and await self.summary_store.save(
            "summary:" + scope + ":" + session_id, summary[:4000], {"scope": scope, "session": session_id, "kind": "summary"}))

    async def recall_summaries(self, scope):
        if self.summary_store:
            rows = await self.summary_store.search(where={"$and": [{"scope": scope}, {"kind": "summary"}]}, top_k=3)
            if rows:
                return [redact_text(row["content"])[0] for row in rows]
        return [text for (owner, _), text in self.summaries.items() if owner == scope][-3:]
