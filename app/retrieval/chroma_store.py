import asyncio
import json
from pathlib import Path
from typing import Iterable, List, Optional

import httpx

from ..models import EvidenceItem


class _HttpCollection:
    """Small HTTP collection adapter used when the app lacks chromadb locally."""

    def __init__(self, client: httpx.Client, base_url: str, collection_id: str):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.collection_id = collection_id

    def upsert(self, **payload):
        response = self.client.post(
            f"{self.base_url}/collections/{self.collection_id}/upsert",
            json=payload,
        )
        response.raise_for_status()
        return response.json() if response.content else None

    def query(self, **payload):
        response = self.client.post(
            f"{self.base_url}/collections/{self.collection_id}/query",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def get(self, **payload):
        response = self.client.post(f"{self.base_url}/collections/{self.collection_id}/get", json=payload)
        response.raise_for_status()
        return response.json()

    def count(self):
        response = self.client.get(f"{self.base_url}/collections/{self.collection_id}/count")
        response.raise_for_status()
        return response.json()

    def delete(self, **payload):
        response = self.client.post(f"{self.base_url}/collections/{self.collection_id}/delete", json=payload)
        response.raise_for_status()


class ChromaStore:
    """Chroma-backed evidence store with HTTP and local persistent modes."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8001,
        collection_name: str = "cardio_evidence",
        mode: str = "auto",
        path: str = "data/runtime/chroma",
    ):
        self.host = host
        self.port = port
        self.collection_name = collection_name
        self.mode = mode
        self.path = path
        self.client = None
        self.collection = None
        self.last_error = None
        self.last_status_code = None
        if mode in {"http", "auto"}:
            self._connect_http(host, port, collection_name)
        if self.collection is None and mode in {"local", "auto"}:
            self._connect_local(collection_name, path)

    def _connect_http(self, host: str, port: int, collection_name: str):
        client = httpx.Client(timeout=20.0)
        base_url = f"http://{host}:{port}/api/v2/tenants/default_tenant/databases/default_database"
        try:
            heartbeat = client.get(f"http://{host}:{port}/api/v1/heartbeat")
            heartbeat.raise_for_status()
            response = client.post(
                f"{base_url}/collections",
                json={"name": collection_name, "metadata": {"hnsw:space": "cosine"}, "configuration": None, "get_or_create": True},
            )
            response.raise_for_status()
            self.client = client
            self.collection = _HttpCollection(client, base_url, response.json()["id"])
        except Exception as exc:
            self.last_error = type(exc).__name__
            client.close()
            self.client = None
            self.collection = None

    def _connect_local(self, collection_name: str, path: str):
        try:
            import chromadb
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=path)
            self.collection = self.client.get_or_create_collection(collection_name, metadata={"hnsw:space": "cosine"})
        except Exception:
            self.client = None
            self.collection = None

    @property
    def available(self) -> bool:
        return self.collection is not None and self.last_error is None

    async def ping(self):
        if not self.collection:
            return False
        try:
            await asyncio.to_thread(self.collection.count)
            self.last_error = None
            self.last_status_code = None
            return True
        except Exception as exc:
            self.last_error = type(exc).__name__
            return False

    async def close(self):
        if isinstance(self.client, httpx.Client):
            await asyncio.to_thread(self.client.close)

    async def delete(self, ids):
        if not ids:
            return True
        if not self.collection:
            return False
        try:
            identities = list(ids)
            for start in range(0, len(identities), 1000):
                await asyncio.to_thread(self.collection.delete, ids=identities[start:start+1000])
            return True
        except Exception as exc:
            self.last_error = type(exc).__name__
            return False

    async def get(self, **kwargs):
        if not self.collection:
            raise RuntimeError('chroma_unavailable')
        return await asyncio.to_thread(self.collection.get, **kwargs)

    @staticmethod
    def _metadata(item: EvidenceItem):
        metadata = dict(item.metadata or {})
        return {
            "source_type": item.source_type,
            "source_id": item.source_id,
            "title": item.title,
            "url": item.url or "",
            "page": item.page if item.page is not None else -1,
            "evidence_level": item.evidence_level or "",
            "is_demo": item.is_demo,
            "review_status": item.review_status,
            "medical_truth_verified": item.medical_truth_verified,
            "is_synthetic": item.is_synthetic,
            "translation_status": item.translation_status or "",
            "source_file": item.source_file or "",
            "source_sha256": item.source_sha256 or "",
            "partition": metadata.get("partition", "evidence" if metadata.get("retrieval_allowed", True) else "test"),
            "retrieval_allowed": bool(metadata.get("retrieval_allowed", True)),
            "parent_evidence_id": metadata.get("parent_evidence_id", item.evidence_id),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        }

    @staticmethod
    def _item(evidence_id: str, content: str, metadata: dict) -> EvidenceItem:
        try:
            extra = json.loads(metadata.get("metadata_json", "{}"))
        except Exception:
            extra = {}
        page = metadata.get("page")
        return EvidenceItem(
            evidence_id=evidence_id,
            content=content,
            source_type=metadata.get("source_type", "unknown"),
            source_id=metadata.get("source_id", evidence_id),
            title=metadata.get("title", evidence_id),
            url=metadata.get("url") or None,
            page=None if page in (None, -1) else int(page),
            evidence_level=metadata.get("evidence_level") or None,
            is_demo=bool(metadata.get("is_demo", False)),
            review_status=metadata.get("review_status", "pending"),
            medical_truth_verified=bool(metadata.get("medical_truth_verified", False)),
            is_synthetic=bool(metadata.get("is_synthetic", False)),
            translation_status=metadata.get("translation_status") or None,
            source_file=metadata.get("source_file") or None,
            source_sha256=metadata.get("source_sha256") or None,
            metadata=extra,
        )

    async def upsert(self, documents: Iterable[EvidenceItem], vectors: List[List[float]]):
        if not self.collection:
            return False
        docs = list(documents)
        try:
            await asyncio.to_thread(
                self.collection.upsert,
                ids=[item.evidence_id for item in docs],
                documents=[item.content for item in docs],
                embeddings=vectors,
                metadatas=[self._metadata(item) for item in docs],
            )
            self.last_error = None
            self.last_status_code = None
            return True
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.last_status_code = getattr(getattr(exc,'response',None),'status_code',None)
            return False

    async def query(self, vector: List[float], top_k: int = 20, where: Optional[dict] = None) -> List[EvidenceItem]:
        if not self.collection:
            return []
        try:
            result = await asyncio.to_thread(
                self.collection.query,
                query_embeddings=[vector],
                n_results=top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
            ids = (result.get("ids") or [[]])[0]
            documents = (result.get("documents") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            items = []
            for index, evidence_id in enumerate(ids):
                item = self._item(evidence_id, documents[index], metadatas[index] or {})
                distance = distances[index] if index < len(distances) else 0.0
                items.append(item.model_copy(update={"retrieval_score": 1.0 - float(distance)}))
            self.last_error = None
            return items
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise RuntimeError('chroma_query_failed:' + type(exc).__name__) from exc
