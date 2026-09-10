"""05. 평가기 반례를 검증하고 저장된 baseline을 채점한다. Agent 재실행 없음."""

import learner_evaluator
from business_lab.counterexamples import verify_learner_check
from business_lab.evidence import push_scores
from business_lab.lesson_runs import grade_recording, load_judge_for, read_recording
from business_lab.storage import save_run_report
from course_config import BASELINE, SCORED_BASELINE, USE_LANGFUSE

USE_JUDGE = True  # live 기록에서만 호출. False이면 의미 평가 누락으로 남는다.


def main():
    verify_learner_check(learner_evaluator.check)
    print("평가기 정상·차단된 쓰기·근거 누락 반례 통과")
    raw = read_recording(BASELINE)
    judge = load_judge_for(raw, enabled=USE_JUDGE)
    scored = grade_recording(raw, judge=judge, extra_check=learner_evaluator.check)
    save_run_report(scored, SCORED_BASELINE)
    if USE_LANGFUSE and any(r["trace_id"] for r in scored["rows"]):
        from business_lab.connections import connect_langfuse
        print("Langfuse의 기존 Trace·위반 Observation에 Code/Judge 점수를 기록합니다.")
        push_scores(scored, connect_langfuse())
    print(f"판정 저장: {SCORED_BASELINE}")
    print("06의 사람 검수에서는 Judge 점수를 먼저 보지 말고 요청·실제 상태·응답을 대조하세요.")


if __name__ == "__main__":
    main()
