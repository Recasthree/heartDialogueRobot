import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from format_medical_dialogue import (  # noqa: E402
    flatten_sft,
    format_file,
    parse_record,
)


NORMAL_RECORD = """id=1
year=2015
source=Medical-Dialogue-Dataset-Chinese
https://example.com/1

Doctor faculty
某医院 心血管内科

Description
疾病：房颤，冠心病
患病多久：大于半年
病情描述：反复心悸，已经做过心电图
希望获得的帮助：请医生解释检查结果
未知字段：保留这个字段

Dialogue
医生：
建议完善检查。
病人：
谢谢医生。

Diagnosis and suggestions
临床诊断：原文保留
"""


def test_parse_normal_record_and_alias_fields():
    record = parse_record(NORMAL_RECORD)
    assert record["record_id"] == "mddc-000001"
    assert record["source"]["doctor_faculty"] == "某医院 心血管内科"
    segment = record["segments"][0]
    assert segment["description"]["diagnoses"] == ["房颤", "冠心病"]
    assert segment["description"]["duration"] == "大于半年"
    assert segment["description"]["help_request"] == "请医生解释检查结果"
    assert segment["description"]["extra_fields"]["未知字段"] == "保留这个字段"
    assert segment["dialogue_blocks"][0]["turns"] == [
        {"turn_id": 1, "role": "assistant", "raw_role": "医生", "content": "建议完善检查。"},
        {"turn_id": 2, "role": "user", "raw_role": "病人", "content": "谢谢医生。"},
    ]
    assert record["segments"][0]["extra_sections"][0]["title"] == "Diagnosis and suggestions"


def test_multiple_descriptions_and_dialogues_are_preserved_as_segments():
    raw = """id=2
year=2018
source=test
https://example.com/2

Doctor faculty
医院 心内科

Description
疾病：房颤
病情描述：第一段

Dialogue
医生：
第一段回答

Description
疾病：心衰
病情描述：第二段

Dialogue
病人：
第二段问题
"""
    record = parse_record(raw)
    assert len(record["segments"]) == 2
    assert record["segments"][0]["dialogue_blocks"][0]["turns"][0]["content"] == "第一段回答"
    assert record["segments"][1]["dialogue_blocks"][0]["turns"][0]["role"] == "user"
    assert "multiple_description_blocks" in record["quality_flags"]
    assert "possible_concatenated_cases" in record["quality_flags"]


def test_no_dialogue_is_retained_but_not_exported_to_sft():
    raw = """id=3
year=2019
source=test
https://example.com/3

Doctor faculty
医院 心内科

Description
疾病：高血压
病情描述：只有病例描述，没有医生回复。
"""
    record = parse_record(raw)
    samples, reviews = flatten_sft(record)
    assert samples == []
    assert reviews == []
    assert "no_dialogue" in record["quality_flags"]


def test_unknown_section_and_role_issue_are_reviewable():
    raw = """id=4
year=2020
source=test
https://example.com/4

Doctor faculty
医院 心内科

Description
疾病：心衰
病情描述：需要复核

Other section
这不是标准区块

Dialogue
家属：
无法识别的说话人
"""
    record = parse_record(raw)
    assert "unknown_section" in record["quality_flags"]
    assert "unknown_speaker" in record["segments"][0]["issue_flags"]


def test_rule_format_outputs_and_hashes_input(tmp_path):
    input_path = tmp_path / "source.txt"
    output_dir = tmp_path / "out"
    input_path.write_text(NORMAL_RECORD, encoding="utf-8")
    report = format_file(input_path, output_dir, mode="rule")
    canonical = [
        json.loads(line)
        for line in (output_dir / "canonical_records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    sft = [
        json.loads(line)
        for line in (output_dir / "sft_messages.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    loaded_report = json.loads((output_dir / "format_report.json").read_text(encoding="utf-8"))
    assert report == loaded_report
    assert len(canonical) == 1
    assert len(sft) == 1
    assert all(message["role"] in {"user", "assistant"} for message in sft[0]["messages"])
    assert report["input_sha256"] == hashlib.sha256(NORMAL_RECORD.encode("utf-8")).hexdigest()
    assert report["outputs"]["canonical_records_jsonl"] == 1
    assert report["outputs"]["sft_messages_jsonl"] == 1


def test_hybrid_mock_does_not_invent_normalized_facts(tmp_path):
    input_path = tmp_path / "source.txt"
    output_dir = tmp_path / "out"
    input_path.write_text(NORMAL_RECORD, encoding="utf-8")
    format_file(input_path, output_dir, mode="hybrid", llm_provider="mock")
    record = json.loads(
        (output_dir / "canonical_records.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    normalized = record["segments"][0]["description"]["normalized_fields"]
    assert record["llm_normalization_status"] == "mock"
    assert normalized["normalized_diagnoses"] == []
    assert normalized["needs_review"] is True
