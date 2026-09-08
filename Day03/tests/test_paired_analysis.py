"""분석기가 성능을 부풀리는 입력을 거절하는지 검증한다. 모델 호출 없음."""

from copy import deepcopy

import pytest

from common.paired_analysis import analyze_pairs


def runs(architecture="baseline"):
    return [
        {
            "case_id": "simple",
            "repeat": repeat,
            "turn": 0,
            "architecture": architecture,
            "request": {"question": "가용률?", "entity": "알파", "as_of": "2026-09-08"},
            "initial_corpus": "same",
            "model": "same-model",
            "backend": "local",
            "mode": "live",
            "exa_mode": "live",
            "web_mode": "off",
            "settings": {"context_bytes": 1000, "serial_workers": False},
            "result": {"status": "complete"},
            "trace": [],
            "metrics": {"elapsed_s": 2, "calls": {"model": 2}, "input_tokens": 100},
            "evaluation": {"citation_ids_valid": True, "semantic_quality": {"correct": True}},
        }
        for repeat in range(3)
    ]


def compare(left, right, **kw):
    return analyze_pairs(
        left, right, baseline_architecture="baseline", candidate_architecture="supervisor", **kw
    )


def test_failed_and_unjudged_turns_remain_in_denominator():
    left, right = runs(), runs("supervisor")
    right[0]["result"]["status"] = "failed"
    right[0].pop("evaluation")
    right[0]["metrics"]["input_tokens"] = None
    result = compare(left, right)
    assert result["pairs"] == 3
    assert result["candidate"]["failure_fraction_all_turns"] == 1 / 3
    assert result["paired_quality"]["coverage"] == 2 / 3
    assert result["paired_delta_candidate_minus_baseline"]["input_tokens"]["observed_pairs"] == 2


def test_missing_or_duplicate_run_does_not_disappear_into_an_intersection():
    with pytest.raises(ValueError, match="사례·반복·턴"):
        compare(runs(), runs("supervisor")[:-1])
    with pytest.raises(ValueError, match="중복"):
        compare(runs(), runs("supervisor") + runs("supervisor")[:1])


@pytest.mark.parametrize("change", ["initial_corpus", "model", "backend"])
def test_changed_control_condition_is_rejected(change):
    candidate = runs("supervisor")
    candidate[0][change] = "different"
    with pytest.raises(ValueError, match="통제 조건"):
        compare(runs(), candidate)


def test_undeclared_second_variable_is_rejected():
    candidate = runs("supervisor")
    candidate[0]["settings"]["context_bytes"] = 50
    with pytest.raises(ValueError, match="선언하지 않은"):
        compare(runs(), candidate)


def test_single_repeat_requires_explicit_smoke_override():
    with pytest.raises(ValueError, match="최소 3회"):
        compare(runs()[:1], runs("supervisor")[:1])
    assert compare(runs()[:1], runs("supervisor")[:1], min_repeats=1)["pairs"] == 1


def test_same_plan_control_detects_changed_decomposition():
    candidate = runs("supervisor")
    candidate[0]["trace"] = [
        {"event": "plan", "plan": {"tasks": [{"id": "a", "objective": "other"}]}}
    ]
    with pytest.raises(ValueError, match="분해 계획"):
        compare(runs(), candidate, require_same_plan=True)


def test_followup_first_turn_is_not_marked_as_a_missing_judge():
    left, right = runs(), runs("supervisor")
    for rows in [left, right]:
        for row in list(rows):
            first = deepcopy(row)
            row["turn"] = 1
            first.pop("evaluation")
            rows.append(first)
    result = compare(left, right)
    assert result["pairs"] == 6
    assert result["paired_quality"]["eligible_final_turns"] == 3
    assert result["paired_quality"]["coverage"] == 1


def test_code_change_must_be_declared_and_gold_must_stay_fixed():
    left, right = runs(), runs("supervisor")
    for row in left:
        row["provenance"] = {
            "source_sha256": "before",
            "assets": {"gold": "same"},
            "python": "3.13",
        }
    for row in right:
        row["provenance"] = {"source_sha256": "after", "assets": {"gold": "same"}, "python": "3.13"}
    with pytest.raises(ValueError, match="implementation"):
        compare(left, right)
    result = compare(left, right, changed=frozenset({"architecture", "implementation"}))
    assert result["actual_changes"] == ["architecture", "implementation"]
    right[0]["provenance"]["assets"]["gold"] = "altered"
    with pytest.raises(ValueError, match="資料|자료"):
        compare(left, right, changed=frozenset({"architecture", "implementation"}))
