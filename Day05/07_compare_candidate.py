"""07. 04의 가설에 따른 한 가지 변경을 실행하고 두 원자료를 같은 기준으로 재채점한다."""

import csv

import candidate_agent
import learner_evaluator
from business_lab.authoring import load_frozen
from business_lab.evidence import calibrate_partitions, push_scores, validate_plan
from business_lab.lesson_runs import (assert_same_protocol, compare_scored, execution_contract, grade_recording,
                                     load_judge_for, read_recording, record_release)
from business_lab.reporting import print_report
from business_lab.storage import save_run_report
from course_config import (BASELINE, CALIBRATION, CANDIDATE, CHANGE_PLAN, COMPARISON, DATASET,
                           HUMAN_LABELS, MODE, REPEATS, SCORED_BASELINE, USE_LANGFUSE, WORK)

RUN_CANDIDATE = True  # False이면 이미 저장된 candidate 원자료를 재채점한다.
USE_JUDGE = True


def main():
    base = read_recording(BASELINE)
    plan = validate_plan(CHANGE_PLAN, base)
    frozen = load_frozen(DATASET)
    if frozen["snapshot_hash"] != base["snapshot_hash"] or MODE != base["mode"] or REPEATS != base["config"]["repeats"]:
        raise ValueError("baseline과 데이터·mode·반복 조건이 다릅니다.")
    if RUN_CANDIDATE:
        if candidate_agent.PROMPT.strip() == base["config"]["prompt"].strip():
            raise ValueError("candidate_agent.py의 PROMPT가 baseline과 같습니다. 04의 가설에 따라 한 조건을 수정하세요.")
        model_name = None
        if MODE == "live":
            from business_lab.models import load_openrouter_env
            model_name = load_openrouter_env()["model"]
        current = execution_contract(MODE, model_name)
        if any(base["config"].get(k) != v for k, v in current.items()):
            raise ValueError("실행 기반·모델·예산이 바뀌었습니다. 두 버전을 같은 조건에서 다시 실행하세요.")
        candidate = record_release(frozen, release="candidate", prompt=candidate_agent.PROMPT, repeats=REPEATS,
                                   mode=MODE, use_langfuse=USE_LANGFUSE, out=CANDIDATE)
    else:
        candidate = read_recording(CANDIDATE)
    assert_same_protocol(base, candidate)
    # 한 판사 설정으로 두 버전 모두 재채점한다. 이전 점수를 이어 붙이지 않는다.
    joint = base | {"rows": [*base["rows"], *candidate["rows"]]}
    judge = load_judge_for(joint, enabled=USE_JUDGE)
    b = grade_recording(base, judge=judge, extra_check=learner_evaluator.check)
    c = grade_recording(candidate, judge=judge, extra_check=learner_evaluator.check)
    save_run_report(b, SCORED_BASELINE)
    save_run_report(c, WORK / "07_candidate_scores.json")
    report = compare_scored(b, c)
    report["change_plan"] = plan
    save_run_report(report, COMPARISON)
    # 기존 사람 판정은 보존하고, 새 Judge 판정과의 대조만 다시 계산한다.
    if HUMAN_LABELS.exists():
        with HUMAN_LABELS.open(encoding="utf-8-sig", newline="") as f:
            calibration = calibrate_partitions(b, list(csv.DictReader(f)))
        save_run_report(calibration, CALIBRATION)
    if USE_LANGFUSE and any(r["trace_id"] for r in report["rows"]):
        from business_lab.connections import connect_langfuse
        print("Langfuse에 두 버전의 새 평가 판정과 근거를 기록합니다.")
        client = connect_langfuse()
        push_scores(b, client)
        push_scores(c, client)
    print_report(report)


if __name__ == "__main__":
    main()
