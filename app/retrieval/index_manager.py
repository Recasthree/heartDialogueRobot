"""Versioned index build/load. No source corpus is changed by this module."""
import json
from pathlib import Path

from ..providers import DeterministicEmbeddingProvider, LocalSemanticEmbedding
from .chroma_store import ChromaStore


def make_embedding(settings, download=False):
    if settings.embedding_backend == 'deterministic':
        return DeterministicEmbeddingProvider()
    return LocalSemanticEmbedding(settings.embedding_model, settings.model_cache_dir,
        settings.embedding_revision, local_only=not download, batch_size=settings.embedding_batch_size)


def chroma(settings, collection):
    return ChromaStore(settings.chroma_host, settings.chroma_port, collection,
                       mode=settings.chroma_mode, path=settings.chroma_path)


def read_active(settings):
    path = Path(settings.data_dir) / 'indexes/active.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def load_index(settings, reranker=None):
    manifest = read_active(settings)
    embedding = make_embedding(settings)
    if not manifest:
        from .disk import DiskRetriever
        return DiskRetriever(embedding, reranker, index_path=Path(settings.data_dir) / 'indexes/runtime-v2.sqlite3',
                               semantic_min_score=settings.semantic_min_score)
    if manifest.get('schema_version') != 2:
        raise RuntimeError('legacy_index_requires_disk_migration: run staged build with --recover-from')
    if manifest['embedding']['model'] != embedding.model_name or manifest['embedding']['revision'] != embedding.revision:
        raise RuntimeError('index_embedding_mismatch: rebuild index or restore configured model')
    # Query with the exact snapshot used at build time, not a subsequently moved 'main'.
    if embedding.semantic:
        embedding.revision = manifest['embedding'].get('resolved_revision', embedding.revision)
    store = chroma(settings, manifest['collection']) if manifest.get('dense_ready',True) and manifest.get('collection') else None
    from .disk import DiskRetriever
    result = DiskRetriever(embedding, reranker, store, Path(settings.data_dir) / 'indexes' / manifest['database'], settings.semantic_min_score)
    result.index_version = result.index_version or manifest['index_version']
    return result


# Public construction is disk-backed and resumable.
from .build_job import build_index
