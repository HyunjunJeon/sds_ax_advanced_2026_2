"""13. 종합 과제 — 통제된 실험 결과를 사례·반복·턴별로 짝지어 분석한다.

가르치는 것:
- 측정을 해석하는 방법론: 짝 비교(같은 사례·반복·턴끼리)만이 조건 차이를 분리하며,
  빠진 짝을 교집합으로 덮거나 선언하지 않은 변수를 묵인하면 분석기가 거절한다.
- 성능 개선의 올바른 읽기: 실패가 빨리 끝나 지연이 낮아진 것은 개선이 아니고,
  토큰이 줄어도 필수 조항이 누락됐다면 손실이다. 승자 선언은 사람이 한다.
- 코드 변경도 실험 변수다: 실행 행의 provenance(코드·자료 hash)로 "설정이 같고
  코드만 바뀐 전후"를 CHANGED의 implementation으로 선언·검증한다.

구현 과제 A/B/C 중 최소 두 개를 실제 RAG에 연결하고 09_compare.py로 변경 전후의
실행 파일을 남긴다. 이 파일은 그 JSONL을 읽을 뿐이며 모델이나 EXA를 호출하지 않는다.

기본 규칙(common/paired_analysis.py가 강제한다):
- 같은 질문/고객/시점/모델/검색 backend/초기 코퍼스. 사례별 최소 3회 반복.
- 바꾼 변수는 아래 CHANGED로 미리 선언한다. 선언하지 않은 조건 차이가 있으면 거절한다.
- 대응 행이 빠지면 교집합으로 덮지 않고 거절한다(생존자 편향 방지).
- REQUIRE_SAME_PLAN=True는 순차/병렬처럼 분해가 같아야 하는 실험에 사용한다.
- usage와 judge 결과의 누락은 별도 커버리지로 남긴다. 자동으로 승자를 선언하지 않는다.

병렬 vs 순차 짝 비교 예(05번 실험과 이어진다):
- BASELINE: 05를 serial_workers=False로 반복 실행해 얻은 runs.jsonl
- CANDIDATE: 같은 plan_file으로 serial_workers=True(label="parallel-serial")로
  반복 실행해 얻은 runs.jsonl
- CHANGED={"architecture", "serial_workers"}, REQUIRE_SAME_PLAN=True
두 실행의 trace에 동일한 plan 이벤트가 남아야 "같은 분해"였다는 증거가 된다.

제출에는 실패 반례, 구현 diff, 학생이 추가한 테스트, 전체 실행 기록과 사람의 원문
판정이 필요하다. 체크리스트와 점수 배점은 WORKSHEET.md의 종합 과제를 따른다.
"""

import json
from pathlib import Path

from common.paired_analysis import analyze_pairs, read_runs

# ── 분석 정의 ────────────────────────────────────────────────────────────────
# 09_compare.py 또는 05번 실습이 남긴 실행 파일 경로로 바꾼다.
BASELINE = Path("outputs/rag-comparison/여기에-변경전-run-id/runs.jsonl")
CANDIDATE = Path("outputs/rag-comparison/여기에-변경후-run-id/runs.jsonl")

BASELINE_ARCHITECTURE = "parallel"  # 변경 전 행의 architecture 값
CANDIDATE_ARCHITECTURE = "parallel-serial"  # 변경 후 행의 architecture 값(label)

# 이 실험에서 바꾼 변수만 나열한다. 허용 목록(sorted(CHANGEABLE), 14개):
# architecture, context_bytes, context_mode, implementation, max_calls, max_tools,
# max_web_documents, no_owner_memory, no_replan, seconds, serial_workers, skill,
# web_max_age_hours, web_mode
CHANGED = frozenset({"architecture", "serial_workers"})

MIN_REPEATS = 3  # 종합 과제 제출 요건은 사례별 3회 이상
REQUIRE_SAME_PLAN = True  # 순차/병렬 비교처럼 동일 분해가 필요한 실험에서 True
OUT = Path("outputs/mini-pjt-analysis.json")  # 분석 결과 저장 경로


if __name__ == "__main__":
    # 기본값은 자리표시자다. 바꾸지 않고 실행하면 stack trace 대신 할 일을 알려준다.
    for name, path in (("BASELINE", BASELINE), ("CANDIDATE", CANDIDATE)):
        if "여기에" in str(path):
            raise SystemExit(f"{name}에 09_compare.py가 남긴 runs.jsonl 경로를 지정하세요.")
        if not path.exists():
            raise SystemExit(f"{name} 경로가 없습니다: {path}")
    try:
        result = analyze_pairs(
            read_runs(BASELINE),
            read_runs(CANDIDATE),
            baseline_architecture=BASELINE_ARCHITECTURE,
            candidate_architecture=CANDIDATE_ARCHITECTURE,
            changed=CHANGED,
            min_repeats=MIN_REPEATS,
            require_same_plan=REQUIRE_SAME_PLAN,
        )
    except ValueError as exc:
        # 계약 위반(짝 누락·선언 안 한 변경·반복 부족)은 분석이 아니라 재실행 대상이다.
        raise SystemExit("분석 거부: " + str(exc)) from exc
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text + "\n")
