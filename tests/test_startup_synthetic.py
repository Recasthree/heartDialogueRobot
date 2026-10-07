import json
from pathlib import Path

import pytest

from app.memory import WorkingMemory
from app.models import GeneratedAnswer
from app.privacy import redact_text
from app.providers import MockLLMProvider


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "data" / "verified"


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def test_startup_fixture_counts_and_case_metadata():
    cases = load_jsonl(SYNTHETIC / "cases" / "case_records.jsonl")
    assert len(cases) == 50
    assert len({row["case_id"] for row in cases}) == 50
    assert all(row.get("medical_truth_verified") is True for row in cases)


def test_startup_ragcare_uses_raw_source_count_and_valid_qrels():
    rows = load_jsonl(SYNTHETIC / "evaluation" / "ragcare_cardiology_golden.jsonl")
    qrels = load_jsonl(SYNTHETIC / "evaluation" / "ragcare_cardiology_qrels.jsonl")
    assert len(rows) == 32
    assert len(qrels) == 96
    evidence = {item for row in rows for item in row["relevant_evidence_ids"]}
    assert all(item["evidence_id"] in evidence for item in qrels)


def test_privacy_fixture_redacts_identifiers():
    rows = load_jsonl(SYNTHETIC / "privacy" / "pii_cases.jsonl")
    assert len(rows) == 76
    for row in rows:
        redacted, entities = redact_text(row["input_text"])
        assert redacted == row["expected_redacted_text"]
        assert entities or redacted == row["input_text"]
        assert all(entity["value"] not in redacted for entity in entities)


def test_memory_ttl_and_context_limit():
    import asyncio

    async def run():
        memory = WorkingMemory(redis_url=None, ttl=1, max_messages=2)
        await memory.append("synthetic", "user", "one")
        await memory.append("synthetic", "user", "two")
        await memory.append("synthetic", "user", "three")
        assert [item["content"] for item in await memory.get_context("synthetic")] == ["two", "three"]

    asyncio.run(run())


@pytest.mark.asyncio
async def test_emergency_path_does_not_call_generation_provider():
    from app.core.graph import MedicalOrchestrator
    from app.memory import WorkingMemory
    from app.retrieval.hybrid import HybridRetriever

    class SpyProvider(MockLLMProvider):
        def __init__(self):
            self.calls = {"classify": 0, "rewrite": 0, "generate": 0, "safety_review": 0}

        async def classify(self, query):
            self.calls["classify"] += 1
            return await super().classify(query)

        async def rewrite(self, query, history):
            self.calls["rewrite"] += 1
            return await super().rewrite(query, history)

        async def generate(self, query, evidence):
            self.calls["generate"] += 1
            return await super().generate(query, evidence)

        async def safety_review(self, query, answer):
            self.calls["safety_review"] += 1
            return await super().safety_review(query, answer)

    provider = SpyProvider()
    answer = await MedicalOrchestrator(provider, HybridRetriever(), WorkingMemory(redis_url=None)).run("突然胸痛并且大汗", "synthetic-emergency")
    assert answer.is_emergency is True
    assert "120" in answer.answer
    assert provider.calls == {"classify": 0, "rewrite": 0, "generate": 0, "safety_review": 0}
