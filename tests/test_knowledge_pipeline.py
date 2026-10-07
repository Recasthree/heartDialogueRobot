import json
from pathlib import Path

from scripts.build_evaluation_sets import make_retrieval_set, make_safety_cases
from scripts.ingest_knowledge import build_import_rows
from scripts.download_cardiology_guidelines import GUIDELINE_TARGETS


ROOT = Path(__file__).resolve().parents[1]


def test_guideline_catalog_has_reproducible_target_count():
    assert len(GUIDELINE_TARGETS) == 40
    legacy_report = ROOT / "data/quarantine/unusable_legacy/processed_legacy_knowledge/knowledge/guideline_report.json"
    if legacy_report.exists():
        report = json.loads(legacy_report.read_text(encoding="utf-8"))
        assert report["downloaded_count"] >= 30
        assert report["chunk_count"] >= report["downloaded_count"]
        assert report["error_count"] == len(report["errors"])


def test_evaluation_set_has_required_counts_and_valid_refs():
    chunks = [
        {
            "evidence_id": "test-guideline-1",
            "content_original": "Atrial fibrillation guideline evaluation evidence.",
            "source_title": "Test guideline",
            "source_url": "https://example.test/guideline",
            "source_type": "clinical_guideline",
            "document_id": "test-guideline",
            "page": 1,
        },
        {
            "evidence_id": "test-guideline-2",
            "content_original": "Heart failure and blood pressure evaluation evidence.",
            "source_title": "Test heart guideline",
            "source_url": "https://example.test/heart",
            "source_type": "clinical_guideline",
            "document_id": "test-heart",
            "page": 2,
        },
    ]
    retrieval, qrels, errors = make_retrieval_set(chunks)
    safety = make_safety_cases()
    assert len(retrieval) == 150
    assert len(qrels) == 150
    assert not errors
    assert len(safety) == 75
    assert {case["expected_class"] for case in safety} == {
        "emergency", "borderline", "ordinary", "insufficient_evidence", "medication_safety"
    }
    evidence_ids = {chunk["evidence_id"] for chunk in chunks}
    assert all(row["relevant_evidence_ids"][0] in evidence_ids for row in retrieval)


def test_chroma_import_preserves_distinct_sources_with_identical_content():
    rows = build_import_rows([
        {
            "evidence_id": "source-1",
            "content": "same source-grounded evidence",
            "source_type": "clinical_guideline",
            "source_id": "doc-1",
            "title": "Guideline",
            "source_url": "https://example.test/a",
        },
        {
            "evidence_id": "source-2",
            "content": "same source-grounded evidence",
            "source_type": "clinical_guideline",
            "source_id": "doc-2",
            "title": "Duplicate",
            "source_url": "https://example.test/b",
        },
    ])
    assert len(rows) == 2
    assert rows[0]['chroma_id'] != rows[1]['chroma_id']
    assert rows[0]["chroma_id"]
    assert rows[0]["is_demo"] is False
