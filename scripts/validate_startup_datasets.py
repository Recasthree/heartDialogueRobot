"""Validate the local synthetic startup corpus and its safety gates."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from app.models import CaseRecord
from app.privacy import redact_text
from app.providers import detect_emergency


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "data/synthetic"


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def validate() -> Dict[str, Any]:
    errors: List[str] = []
    case_rows = read_jsonl(SYNTHETIC / "cases/case_records.jsonl")
    if len(case_rows) != 50:
        errors.append(f"case_count:{len(case_rows)}")
    for row in case_rows:
        try:
            CaseRecord.model_validate(row)
        except Exception as exc:
            errors.append(f"invalid_case:{row.get('case_id')}:{exc}")
        if row.get("medical_truth_verified") is not False or row.get("is_demo") is not True:
            errors.append(f"case_gate:{row.get('case_id')}")
    case_ids = [row.get("case_id") for row in case_rows]
    if len(set(case_ids)) != len(case_ids):
        errors.append("duplicate_case_ids")

    emergency = read_jsonl(SYNTHETIC / "safety/emergency_cases.jsonl")
    fallback = read_jsonl(SYNTHETIC / "safety/emergency_fallback_cases.jsonl")
    if len(emergency) != 45 or len(fallback) != 5:
        errors.append(f"safety_count:{len(emergency)}+{len(fallback)}")
    for row in emergency:
        hits = detect_emergency(row["message"])
        if row["expected_class"] == "emergency" and not hits:
            errors.append(f"emergency_rule_miss:{row['case_id']}")
        if row["expected_class"] != "emergency" and hits:
            errors.append(f"unexpected_emergency_hit:{row['case_id']}")
        if row["expected_class"] == "emergency" and not row["must_not_call_generation_llm"]:
            errors.append(f"emergency_allows_llm:{row['case_id']}")
    for row in fallback:
        if row.get("failure_mode") is None:
            errors.append(f"fallback_missing_failure_mode:{row['case_id']}")

    ragcare = read_jsonl(SYNTHETIC / "evaluation/ragcare_cardiology_golden.jsonl")
    qrels = read_jsonl(SYNTHETIC / "evaluation/ragcare_cardiology_qrels.jsonl")
    evidence_ids = {ref for row in ragcare for ref in row.get("relevant_evidence_ids", [])}
    if len(ragcare) != 32:
        errors.append(f"ragcare_cardiology_count:{len(ragcare)}")
    if len({row.get("query") for row in ragcare}) != len(ragcare):
        errors.append("duplicate_ragcare_queries")
    if len(qrels) != len(ragcare) or any(row.get("evidence_id") not in evidence_ids for row in qrels):
        errors.append("ragcare_qrels_mismatch")

    demo_evidence = read_jsonl(SYNTHETIC / "evidence/demo_evidence.jsonl")
    demo_queries = read_jsonl(SYNTHETIC / "evidence/demo_retrieval_golden.jsonl")
    demo_ids = {row.get("evidence_id") for row in demo_evidence}
    if len(demo_evidence) != 30 or len(demo_queries) != 40:
        errors.append(f"demo_retrieval_counts:{len(demo_evidence)}/{len(demo_queries)}")
    if len({row.get("query") for row in demo_queries}) != len(demo_queries):
        errors.append("duplicate_demo_queries")
    if any(ref not in demo_ids for row in demo_queries for ref in row.get("relevant_evidence_ids", [])):
        errors.append("demo_qrels_missing_evidence")

    judges = read_jsonl(SYNTHETIC / "evaluation/judge_cases.jsonl")
    dimensions = {"relevance", "accuracy", "completeness", "helpfulness", "citation_validity"}
    if len(judges) != 40:
        errors.append(f"judge_count:{len(judges)}")
    for row in judges:
        if set(row.get("expected_scores", {})) != dimensions:
            errors.append(f"judge_dimensions:{row.get('judge_id')}")

    safety_refusal = read_jsonl(SYNTHETIC / "evaluation/safety_refusal_cases.jsonl")
    if len(safety_refusal) != 60 or len({row.get("case_id") for row in safety_refusal}) != 60:
        errors.append("safety_refusal_ids_or_count")
    for row in safety_refusal:
        validation = row.get("citation_validation", {})
        if validation.get("expected_valid") is False and row.get("expected_action") not in {"reject_invalid_citation", "refuse_insufficient_evidence", "refuse_specific_dosing"}:
            errors.append(f"safety_action:{row.get('case_id')}")

    pii = read_jsonl(SYNTHETIC / "privacy/pii_cases.jsonl")
    pii_errors = 0
    for row in pii:
        redacted, entities = redact_text(row["input_text"])
        if redacted != row["expected_redacted_text"] or not entities:
            pii_errors += 1
        for entity in row["expected_entities"]:
            if entity["value"] in redacted:
                pii_errors += 1
    if len(pii) != 40 or pii_errors:
        errors.append(f"pii_validation:{len(pii)}/{pii_errors}")

    report = {
        "valid": not errors,
        "errors": errors,
        "counts": {
            "cases": len(case_rows),
            "emergency": len(emergency),
            "fallback": len(fallback),
            "ragcare": len(ragcare),
            "demo_evidence": len(demo_evidence),
            "demo_queries": len(demo_queries),
            "judge": len(judges),
            "safety_refusal": len(safety_refusal),
            "privacy": len(pii),
        },
        "medical_truth_verified": False,
    }
    (SYNTHETIC / "validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="校验快速启动合成数据")
    parser.parse_args()
    report = validate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
