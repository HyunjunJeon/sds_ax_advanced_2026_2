"""08. 실패를 검수할 새 사례로 승격하거나, 수정에 쓰지 않은 holdout으로 최종 확인한다."""

import json

import learner_evaluator
from business_lab.authoring import freeze
from business_lab.dataset import load_dataset, load_fixtures
from business_lab.evidence import promote_failure, validate_plan
from business_lab.lesson_runs import compare_scored, execution_contract, grade_recording, load_judge_for, read_recording, record_release
from business_lab.storage import save_run_report
from course_config import BASELINE, CHANGE_PLAN, COMPARISON, HOLDOUT, MODE, REGRESSION_DRAFT, REPEATS, USE_LANGFUSE, WORK

ACTION = "promote"  # promote / holdout
SANITIZATION_RECORD = ""  # 원자료의 보관 가능 여부를 직접 확인한 뒤 기록한다.


def main():
    if ACTION == "promote":
        raw = read_recording(BASELINE)
        plan = validate_plan(CHANGE_PLAN, raw)
        card = promote_failure(raw, plan, REGRESSION_DRAFT, sanitization_record=SANITIZATION_RECORD)
        print(f"미검수 회귀 초안: {REGRESSION_DRAFT}\n출처 Artifact: {card['metadata']['source_artifact_id']}")
        print("실제 결과를 정답으로 복사하지 않았습니다. 기대 상태와 재현 조건을 검수해 새 데이터 버전으로 사용하세요.")
        return
    if ACTION != "holdout":
        raise ValueError("ACTION은 promote 또는 holdout입니다.")
    previous = json.loads(COMPARISON.read_text())
    model_name = None
    if MODE == "live":
        from business_lab.models import load_openrouter_env
        model_name = load_openrouter_env()["model"]
    current = execution_contract(MODE, model_name)
    if REPEATS != previous["config"]["repeats"] or any(previous["config"].get(k) != v for k, v in current.items()):
        raise ValueError("개발 실험과 모델·실행 코드·예산이 다릅니다. 같은 후보를 최종 확인해야 합니다.")
    cards = [c.model_dump() for c in load_dataset(split="holdout")]
    frozen = freeze(cards, load_fixtures(), WORK / "08_holdout_dataset.json", split="holdout")
    records = [record_release(frozen, release=release, prompt=previous["prompts"][release], repeats=REPEATS,
                mode=MODE, use_langfuse=USE_LANGFUSE, out=WORK / f"08_{release}_holdout.json")
               for release in ("baseline", "candidate")]
    judge = load_judge_for(records[0] | {"rows": [r for record in records for r in record["rows"]]})
    scores = [grade_recording(r, judge=judge, extra_check=learner_evaluator.check) for r in records]
    for score in scores:
        save_run_report(score, WORK / f"08_{score['release']}_holdout_scores.json")
    report = compare_scored(*scores)
    report["development_comparison_id"] = previous["run_id"]
    save_run_report(report, HOLDOUT)
    if USE_LANGFUSE and any(r["trace_id"] for r in report["rows"]):
        from business_lab.connections import connect_langfuse
        from business_lab.evidence import push_scores
        print("Langfuse의 holdout 실행에 평가 판정을 기록합니다.")
        client = connect_langfuse()
        for score in scores:
            push_scores(score, client)
    print(f"최종 확인: {HOLDOUT}\n{report['comparison']['decision_reasons']}")


if __name__ == "__main__":
    main()
