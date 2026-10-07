import pytest

from app.memory import WorkingMemory
from app.models import EvidenceItem, MedicalIntent
from app.providers import MockLLMProvider, detect_emergency
from app.retrieval.hybrid import HybridRetriever, dedup_by_hash, reciprocal_rank_fusion


def item(content, name):
    return EvidenceItem(evidence_id=name, content=content, source_id=name, title=name)


def test_emergency_rules_are_deterministic():
    assert detect_emergency("突然胸痛并且大汗")
    assert detect_emergency("呼吸困难，不能平卧")
    assert not detect_emergency("我想了解胸痛的定义")


@pytest.mark.asyncio
async def test_mock_classifier_covers_test_interpretation():
    result = await MockLLMProvider().classify("肌钙蛋白检查报告怎么看")
    assert result.intent == MedicalIntent.TEST_INTERPRETATION


def test_dedup_and_rrf_have_stable_shape():
    first = item("相同内容", "a")
    second = item("相同内容", "b")
    fused = reciprocal_rank_fusion([[first], [second]])
    assert len(dedup_by_hash([first, second])) == 1
    # Identical text with different provenance must keep both identities.
    assert len(fused) == 2
    assert fused[0].retrieval_score > 0


@pytest.mark.asyncio
async def test_hybrid_retrieval_and_demo_flag():
    retriever = HybridRetriever()
    await retriever.add([item("演示占位证据 心血管", "a"), item("完全不同的文本", "b")])
    result = await retriever.search("心血管证据")
    assert result
    assert result[0].is_demo is True


@pytest.mark.asyncio
async def test_memory_is_bounded():
    memory = WorkingMemory(redis_url=None, ttl=3600, max_messages=2)
    await memory.append("s", "user", "1")
    await memory.append("s", "assistant", "2")
    await memory.append("s", "user", "3")
    context = await memory.get_context("s")
    assert [message["content"] for message in context] == ["2", "3"]

