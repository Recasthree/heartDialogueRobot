import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from enrich_medical_dialogue import (  # noqa: E402
    build_payload,
    enrich_file,
    validate_payload,
)


def sample_segment():
    return {
        "segment_id": "mddc-000001-s001",
        "description": {
            "raw_text": "疾病：房颤，冠心病。病情描述：反复心悸、胸闷。"
        },
        "dialogue_blocks": [
            {
                "block_id": "mddc-000001-s001-d001",
                "turns": [
                    {
                        "turn_id": 1,
                        "role": "assistant",
                        "content": "建议做心电图和心脏超声，必要时考虑射频消融。",
                    },
                    {
                        "turn_id": 2,
                        "role": "user",
                        "content": "目前口服阿司匹林肠溶片，每日1片，否认高血压。",
                    },
                ],
            }
        ],
        "extra_sections": [],
        "issue_flags": [],
    }


def test_build_payload_extracts_source_grounded_entities():
    payload = build_payload(sample_segment())
    fields = payload["normalized_fields"]
    entities = payload["normalized_entities"]

    assert "房颤" in fields["diagnoses"]
    assert "心悸" in fields["symptoms"]
    assert "心电图" in fields["examinations"]
    assert "阿司匹林肠溶片" in fields["medications"]
    assert "射频消融" in fields["treatments"]
    assert any(item["source_role"] == "doctor" for item in entities["examinations"])
    aspirin = next(item for item in entities["medications"] if item["name"] == "阿司匹林肠溶片")
    assert aspirin["dose"] == "1片"
    assert aspirin["frequency"] == "每日1片"
    assert aspirin["route"] == "口服"
    assert any(item["mention_type"] == "negated" for item in entities["diagnoses"])
    for category, values in entities.items():
        assert fields[category] == list(dict.fromkeys(item["name"] for item in values))
        for item in values:
            assert item["evidence"] in "\n".join(
                [sample_segment()["description"]["raw_text"]]
                + [turn["content"] for block in sample_segment()["dialogue_blocks"] for turn in block["turns"]]
            )


def test_invalid_evidence_is_rejected():
    payload = build_payload(sample_segment())
    broken = copy.deepcopy(payload)
    broken["normalized_entities"]["diagnoses"][0]["evidence"] = "这段话不在原文"
    with pytest.raises(ValueError, match="evidence not found"):
        validate_payload(broken, "\n".join([sample_segment()["description"]["raw_text"], "反复心悸。"]))


def test_enrich_file_preserves_input_and_writes_report(tmp_path):
    record = {
        "schema_version": "medical-dialogue-v1",
        "record_id": "mddc-000001",
        "source": {"original_id": 1},
        "segments": [sample_segment()],
        "raw_text": "原始病例文本",
        "parse_status": "parsed",
        "quality_flags": [],
    }
    input_path = tmp_path / "canonical_records.jsonl"
    input_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    before = input_path.read_bytes()

    report = enrich_file(input_path, tmp_path / "out")

    assert input_path.read_bytes() == before
    enriched = json.loads((tmp_path / "out" / "canonical_records_enriched.jsonl").read_text(encoding="utf-8"))
    assert enriched["normalization_status"] == "llm_enriched_unverified"
    assert enriched["segments"][0]["description"]["normalized_fields"]["diagnoses"] == ["冠心病", "高血压", "房颤"]
    assert report["record_count"] == 1
    assert report["segment_count"] == 1
    assert report["external_api_called"] is False
    assert report["outputs"]["canonical_records_enriched"] == 1
