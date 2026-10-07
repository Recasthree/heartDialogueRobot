import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from filter_huatuo_cardio import (  # noqa: E402
    deduplicate_records,
    filter_records,
    match_concepts,
    apply_review,
    write_outputs,
)


def row(identifier, label="内科", related="冠心病", question="问题", answer="答案", score=5):
    return {
        "id": identifier,
        "label": label,
        "related_diseases": related,
        "question": question,
        "answer": answer,
        "score": score,
    }


def test_empty_related_and_non_inner_department_are_excluded():
    result = filter_records(
        [
            row(1, related="", question="心肌梗死和房颤", answer="需要评估"),
            row(2, label="皮肤性病科", question="心肌梗死和房颤", answer="需要评估"),
        ]
    )
    assert result["high_quality"] == []
    assert result["review"] == []
    assert result["report"]["excluded_empty_related"] == 1
    assert result["report"]["excluded_department"] == 1


def test_related_disease_anchors_cardiovascular_match():
    result = filter_records(
        [row(1, related="冠心病", question="同时有房颤怎么办", answer="需要专业评估")]
    )
    assert len(result["high_quality"]) == 1
    assert set(result["high_quality"][0]["matched_groups"]) == {
        "arrhythmia",
        "coronary_ischemia_infarction",
    }


def test_synonyms_in_one_group_count_once():
    match = match_concepts("心肌梗死", "心梗和急性心肌梗死", "冠心病")
    assert "coronary_ischemia_infarction" in match["matched_groups"]
    assert len(match["matched_groups"]) == 1


def test_two_different_groups_are_required():
    result = filter_records([row(1, related="冠心病", question="心肌梗死怎么处理")])
    assert result["high_quality"] == []
    assert result["review"][0]["auto_bucket"] == "score5_below_original_threshold"


def test_score_five_final_and_score_four_non_hypertension_are_high_quality():
    records = [
        row(1, related="冠心病", question="同时有房颤怎么办", answer="需要评估", score=5),
        row(2, related="冠心病", question="房颤合并冠心病怎么办", answer="需要评估", score=4),
    ]
    result = filter_records(records)
    assert [item["id"] for item in result["high_quality"]] == [1, 2]
    assert [item["id"] for item in result["high_quality"] if item["score"] == 4] == [2]
    assert result["high_quality"][1]["auto_bucket"] == "score4_non_hypertension"


def test_exact_hypertension_labels_are_excluded_for_all_scores():
    result = filter_records(
        [
            row(1, related="高血压", question="房颤和冠心病并存", score=4),
            row(2, related="高血压病", question="房颤和冠心病并存", score=5),
        ]
    )
    assert result["high_quality"] == []
    assert result["review"] == []
    assert result["report"]["excluded_exact_hypertension"] == 2


def test_hypertension_score_four_requires_three_groups():
    result = filter_records(
        [
            row(1, related="原发性高血压", question="同时有房颤", score=4),
            row(2, related="继发性高血压", question="同时有房颤和冠心病并有心衰", score=4),
        ]
    )
    assert [item["id"] for item in result["high_quality"]] == [2]
    assert result["high_quality"][0]["auto_bucket"] == "score4_hypertension_3plus"
    assert result["review"][0]["auto_bucket"] == "hypertension_insufficient_groups"


def test_non_hypertension_score_four_with_one_group_is_promoted():
    result = filter_records([row(1, related="冠心病", question="冠心病平时需要注意什么", score=4)])
    assert [item["id"] for item in result["high_quality"]] == [1]
    assert result["high_quality"][0]["auto_bucket"] == "score4_non_hypertension"


def test_negated_anchor_is_not_a_valid_match():
    match = match_concepts("否认心肌梗死", "没有房颤", "排除冠心病")
    assert match["anchor_hits"] == []
    assert match["anchor_group_count"] == 0
    assert match["negated_hits"]


def test_risk_flags_capture_broad_terms_and_off_topic_answers():
    result = filter_records(
        [
            row(
                1,
                related="心绞痛",
                question="我最近总是胃痛怎么办",
                answer="可能是胃肠消化问题，建议先观察",
                score=4,
            )
        ]
    )
    flags = result["high_quality"][0]["risk_flags"]
    assert "broad_cardio_term" in flags
    assert "possible_off_topic_answer" in flags
    assert "label_content_mismatch" in flags
    assert result["report"]["risk_flag_counts"]["broad_cardio_term"] == 1


def test_duplicate_keeps_highest_score():
    records = [row(2, score=4), row(1, score=5)]
    unique, duplicates = deduplicate_records(records)
    assert len(unique) == 1
    assert duplicates == 1
    assert unique[0]["id"] == 1


def test_outputs_are_reloadable(tmp_path):
    result = filter_records(
        [row(1, related="冠心病", question="同时有房颤怎么办", answer="需要评估")]
    )
    write_outputs(result, tmp_path, Path("data/raw/huatuo_lite"))
    with (tmp_path / "cardio_high_quality.jsonl").open(encoding="utf-8") as handle:
        output = [json.loads(line) for line in handle if line.strip()]
    review = json.loads((tmp_path / "cardio_review.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "filter_report.json").read_text(encoding="utf-8"))
    assert len(output) == 1
    assert review == []
    assert report["outputs"]["high_quality_jsonl"] == 1
    assert report["outputs"]["review_json"] == len(review)
    assert report["high_quality_count"] == len(output)
    assert report["review_count"] == len(review)


def test_apply_review_only_exports_approved_records_with_notes(tmp_path):
    review_path = tmp_path / "cardio_review.json"
    review_path.write_text(
        json.dumps(
            [
                {
                    **row(1, related="冠心病", question="同时有房颤怎么办"),
                    "review_status": "approved",
                    "keep": True,
                    "review_notes": "问题和答案均与心血管主题相关",
                },
                {
                    **row(2, related="冠心病", question="另一个问题"),
                    "review_status": "pending",
                    "keep": None,
                    "review_notes": "",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stats = apply_review(review_path, tmp_path / "approved")
    approved = json.loads((tmp_path / "approved" / "approved.json").read_text(encoding="utf-8"))
    assert stats == {"approved": 1, "rejected": 1, "invalid": 0}
    assert [item["id"] for item in approved] == [1]
    assert approved[0]["knowledge_status"] == "human_reviewed"
