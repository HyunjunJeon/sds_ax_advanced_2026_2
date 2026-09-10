"""전문가 답변의 반영 경로를 검증한다. 검수 기록은 모두 테스트용이며 API 호출은 없다."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from business_lab.agents import COMMON_PROMPT
from business_lab.authoring import draft_cases, freeze, read_workbook, write_workbook
from business_lab.dataset import load_fixtures
from business_lab.evidence import export_human_review
from business_lab.lesson_runs import grade_recording, record_release


def load_lesson(name):
    spec = importlib.util.spec_from_file_location("refinement_test_" + name, Path(__file__).parents[1] / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def revision():
    return {"scenario_id": "normal", "feedback_id": "test-Q-001", "source": "가상 검수 파일#Q-001",
            "feedback": "자동 테스트용 의견: 원장 기록과 정산 완료를 구분한다.",
            "reference_answer": "원장에 환불이 기록됐습니다. 카드사 정산 완료를 뜻하지는 않습니다.",
            "required_facts": ["환불 원장 기록과 카드사 정산을 구분한다."]}


def test_revision_preserves_original_cases_and_requires_new_review(revision, tmp_path):
    cards = draft_cases()
    cards[0]["metadata"].update(review_status="approved", reviewer="test-reviewer", review_reason="이전 테스트 검수")
    original = deepcopy(cards)
    updated, changes = load_lesson("01b_refine_goldens.py").refine_goldens(cards, [revision])
    assert cards == original
    assert updated[1:] == original[1:]
    assert updated[0]["input"] == original[0]["input"]
    assert updated[0]["expected_output"]["final_state"] == original[0]["expected_output"]["final_state"]
    assert updated[0]["metadata"]["review_status"] == "pending"
    assert updated[0]["metadata"]["reviewer"] == ""
    assert changes[0]["before"] == original[0]
    assert changes[0]["revision"] == revision
    with pytest.raises(ValueError, match="검수"):
        freeze(updated, load_fixtures(), tmp_path / "blocked.json", scenario_ids=["normal"])


@pytest.mark.parametrize("problem", ["unknown", "duplicate", "empty_answer", "empty_feedback", "bad_facts", "holdout", "unknown_field"])
def test_invalid_revisions_do_not_change_the_source(revision, problem):
    cards = draft_cases()
    revisions = [deepcopy(revision)]
    if problem == "unknown":
        revisions[0]["scenario_id"] = "absent"
    elif problem == "duplicate":
        revisions.append(deepcopy(revision))
    elif problem == "empty_answer":
        revisions[0]["reference_answer"] = " "
    elif problem == "empty_feedback":
        revisions[0]["feedback"] = " "
    elif problem == "bad_facts":
        revisions[0]["required_facts"] = [""]
    elif problem == "holdout":
        cards[0]["metadata"]["split"] = "holdout"
    else:
        revisions[0]["review_status"] = "approved"
    original = deepcopy(cards)
    with pytest.raises(ValueError):
        load_lesson("01b_refine_goldens.py").refine_goldens(cards, revisions)
    assert cards == original


def test_refined_answer_reaches_frozen_dataset_judge_and_human_review(revision, tmp_path, monkeypatch):
    source, output = tmp_path / "original.xlsx", tmp_path / "refined.xlsx"
    write_workbook(draft_cases(), source)
    source_bytes = source.read_bytes()
    lesson = load_lesson("01b_refine_goldens.py")
    lesson.SOURCE_XLSX, lesson.OUTPUT_XLSX, lesson.REVISIONS = source, output, [revision]
    lesson.main()
    assert source.read_bytes() == source_bytes
    history = json.loads(output.with_suffix(".changes.json").read_text())
    assert history["changes"][0]["revision"]["feedback"] == revision["feedback"]
    output_bytes = output.read_bytes()
    with pytest.raises(FileExistsError):
        lesson.main()
    assert output.read_bytes() == output_bytes

    reviewed = read_workbook(output)
    reviewed[0]["metadata"].update(review_status="approved", reviewer="test-reviewer", review_reason="테스트용 재검수")
    reviewed_path = tmp_path / "reviewed.xlsx"
    write_workbook(reviewed, reviewed_path)
    second = load_lesson("02_freeze_dataset.py")
    second.DRAFT_XLSX, second.REFINED_XLSX = source, reviewed_path
    second.USE_REFINED, second.SCENARIO_IDS, second.DATASET = True, ["normal"], tmp_path / "dataset.json"
    second.main()
    frozen = json.loads(second.DATASET.read_text())
    assert frozen["snapshot"]["cards"][0]["expected_output"]["reference_answer"] == revision["reference_answer"]

    # 실제 업무 환경은 scripted로 실행하고 Judge의 입구만 가로채 전달된 LLMTestCase를 검증한다.
    raw = record_release(frozen, release="baseline", prompt=COMMON_PROMPT, repeats=1, mode="scripted",
                         use_langfuse=False, out=tmp_path / "baseline.json")
    seen = []

    class CaptureMetric:
        score, reason = 1.0, "테스트용 판정"

        def __init__(self, **kwargs):
            self.params = kwargs["evaluation_params"]

        def measure(self, case, **kwargs):
            seen.append((case, self.params))

        def is_successful(self):
            return True

    class FakeJudge:
        def get_model_name(self):
            return "test-judge"

    import deepeval.metrics
    from deepeval.test_case import SingleTurnParams
    monkeypatch.setattr(deepeval.metrics, "GEval", CaptureMetric)
    scored = grade_recording(raw, judge=FakeJudge())
    case, params = seen[0]
    assert SingleTurnParams.EXPECTED_OUTPUT in params
    assert json.loads(case.expected_output) == {"reference_answer": revision["reference_answer"],
                                               "required_facts": revision["required_facts"]}
    assert revision["feedback"] not in case.input  # 자유 의견은 채점 지시로 넣지 않는다.
    assert revision["reference_answer"] not in json.dumps(raw["rows"][0]["artifact"], ensure_ascii=False)
    context = json.loads(export_human_review(scored, tmp_path / "human.csv").read_text())
    assert context["response_references"]["normal"] == json.loads(case.expected_output)
