import json
from pathlib import Path

import pytest

from app.providers import detect_emergency
from scripts.ingest_knowledge import build_import_rows
from scripts.repaired_common import looks_like_navigation


ROOT = Path(__file__).resolve().parents[1]
REPAIRED = ROOT / "data" / "processed" / "repaired"


def require_repaired_fixture():
    if not REPAIRED.exists():
        pytest.skip("repaired corpus was intentionally removed; validate it when regenerated")


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_repaired_guideline_chunks_are_body_text_with_provenance():
    require_repaired_fixture()
    chunks = load_jsonl(REPAIRED / "guidelines" / "chunks_original.jsonl")
    assert chunks
    assert all(not looks_like_navigation(row["content_original"]) for row in chunks)
    assert all(row["source_url"] and row["source_sha256"] and row["source_title"] for row in chunks)


def test_repaired_drug_corpus_has_26_unique_core_drugs_and_no_packages():
    require_repaired_fixture()
    concepts = load_jsonl(REPAIRED / "drugs" / "drug_concepts.jsonl")
    assert len(concepts) == 26
    assert len({row["name_en"] for row in concepts}) == 26
    assert all(row.get("tty") not in {"GPCK", "BPCK"} for row in concepts)
    assert all("Excedrin" not in " ".join(row.get("synonyms", [])) for row in concepts)


def test_repaired_interactions_have_stable_unique_ids_and_null_missing_metadata():
    require_repaired_fixture()
    rows = load_jsonl(REPAIRED / "drugs" / "drug_interactions.jsonl")
    assert rows
    assert len({row["interaction_id"] for row in rows}) == len(rows)
    assert all(row["interaction_id"].startswith("ddinter-") for row in rows)
    assert all(row["mechanism_original"] is None or row["mechanism_original"] for row in rows)


def test_repaired_evaluation_queries_and_qrels_are_unique_and_bound():
    require_repaired_fixture()
    retrieval = load_jsonl(REPAIRED / "evaluation" / "retrieval_golden.jsonl")
    qrels = load_jsonl(REPAIRED / "evaluation" / "retrieval_qrels.jsonl")
    evidence = {row.get("evidence_id") for row in load_jsonl(REPAIRED / "guidelines" / "chunks_original.jsonl")}
    evidence.update("drug-interaction-%s" % row.get("interaction_id") for row in load_jsonl(REPAIRED / "drugs" / "drug_interactions.jsonl"))
    assert retrieval
    assert len({row["query_id"] for row in retrieval}) == len(retrieval)
    assert len({row["query"] for row in retrieval}) == len(retrieval)
    assert len(qrels) == len(retrieval)
    assert all(set(row["relevant_evidence_ids"]).issubset(evidence) for row in retrieval)


def test_repaired_safety_set_has_expected_distribution_and_rule_hits():
    require_repaired_fixture()
    rows = load_jsonl(REPAIRED / "evaluation" / "emergency_refusal_cases.jsonl")
    assert len(rows) == 75
    assert len({row["message"] for row in rows}) == 75
    counts = {}
    for row in rows:
        counts[row["expected_class"]] = counts.get(row["expected_class"], 0) + 1
        hits = detect_emergency(row["message"])
        if row["expected_class"] == "emergency":
            assert hits
            assert row["must_not_call_generation_llm"] is True
        else:
            assert not hits
    assert counts == {"emergency": 25, "borderline": 15, "ordinary": 15, "insufficient_evidence": 10, "medication_safety": 10}


def test_pending_repaired_evidence_is_not_importable_without_explicit_gate():
    require_repaired_fixture()
    pending = load_jsonl(REPAIRED / "evidence_items_pending.jsonl")[:5]
    assert pending
    assert build_import_rows(pending, include_unreviewed=False) == []
