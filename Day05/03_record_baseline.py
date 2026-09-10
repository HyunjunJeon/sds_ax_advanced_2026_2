"""03. 동결한 데이터로 baseline을 실행한다. 실제 상태·응답·오류를 기록하고 채점은 05에서 한다."""

from business_lab.agents import COMMON_PROMPT
from business_lab.authoring import load_frozen
from business_lab.lesson_runs import record_release
from course_config import BASELINE, DATASET, MODE, REPEATS, USE_LANGFUSE


def main():
    data = load_frozen(DATASET)
    report = record_release(data, release="baseline", prompt=COMMON_PROMPT, repeats=REPEATS,
                            mode=MODE, use_langfuse=USE_LANGFUSE, out=BASELINE)
    print(f"원자료: {BASELINE}\n실행 ID: {report['run_id']}")
    print(f"예정 {len(report['plan'])}건 / 기록 {len(report['rows'])}건. 오류도 그대로 보존합니다.")
    print("04에서 요청 → 호출 인자·결과 → 실제 상태 순으로 읽으세요.")


if __name__ == "__main__":
    main()
