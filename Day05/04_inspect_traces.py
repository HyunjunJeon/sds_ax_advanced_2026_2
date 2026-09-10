"""04. 원자료에서 사실·가설·대안 가설을 분리해 기록한다. 모델 호출 없음."""

from business_lab.evidence import fetch_observations, inspect_recording
from business_lab.lesson_runs import read_recording
from business_lab.storage import make_run_id, save_run_report
from course_config import BASELINE, CHANGE_PLAN, WORK

FETCH_REMOTE_OBSERVATIONS = True  # Trace ID가 있는 경우 Langfuse 전체 페이지를 읽는다. 쓰기는 없다.


def main():
    raw = read_recording(BASELINE)
    path = inspect_recording(raw, WORK, CHANGE_PLAN)
    print(f"실행 근거: {path}\n직접 작성할 가설: {CHANGE_PLAN}")
    if FETCH_REMOTE_OBSERVATIONS and any(r["trace_id"] for r in raw["rows"]):
        from business_lab.connections import connect_langfuse
        remote = fetch_observations(connect_langfuse(), raw)
        save_run_report({"run_id": make_run_id(), "source_run_id": raw["run_id"], "traces": remote}, WORK / "04_observations.json")
        missing = sum(len(t["missing_service_observation_ids"]) for t in remote.values())
        print(f"원격 Observation 조회 완료. 누락된 서비스 근거 {missing}건.")
    print("Trace의 모든 스팬 수와 실제 서비스 호출 수는 다릅니다. call_id와 부모 Observation을 연결해 읽으세요.")


if __name__ == "__main__":
    main()
