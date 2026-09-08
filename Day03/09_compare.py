"""09. 동일 사례를 여러 구조에서 실행하고 실패까지 결과에 남긴다.

가르치는 것:
- 통제된 비교 실험의 설계: 같은 사례·초기 코퍼스·예산에서 구조만 바꿔야 구조의
  효과를 분리할 수 있다. workspace 격리, seed snapshot 복사, 반복마다 실행 순서
  뒤집기는 모두 이 통제를 위한 장치다.
- 정직한 집계: 실패·보류 실행도 분모에서 빼지 않고 즉시 기록한다. 성공한 실행만
  남기면 평균이 거짓으로 좋아진다(생존자 편향).
- 실행과 평가의 분리: 정답표(gold)는 judge 호출에만 제공되며 Agent 입력으로 가지
  않는다. 인용 검증·자체 검토(complete)·의미 평가(judge)가 서로 다른 검사임을 안다.

아래 __main__의 리스트를 편집해 실험을 정의한다. 실행 입력(data/cases.jsonl)과
사후 기대 사실(data/gold.jsonl)은 분리되어 있고, 정답표는 judge 호출에만 제공된다.
judge는 별도 모델 평가이고 그 호출 비용도 별도 기록된다. 사람의 원문 대조를
대체하지 않는다.

처음에는 단순 질문 하나로 구조의 추가 호출 비용을 확인한다:
    cases=["simple"], repeats=1, judge=True
그다음 대표 6문제를 동일한 초기 코퍼스에서 비교한다. 기본 반복은 3회이며 실행
순서를 번갈아 바꾼다(반복마다 architectures 순서를 뒤집는다).

구조별 workspace는 자동으로 분리된다. seed_workspace를 지정하면 준비된 코퍼스
snapshot을 각 workspace에 복사하는데, 문서 상태만 복사하고 대화 상태는 복사하지
않는다. 서버에서 이미 임베딩된 문서를 구조마다 다시 임베딩하지 않는 용도다.

고급 실험에서는 13_mini_pjt.py로 반복/사례별 짝을 맞추고 실패율과 평가 누락
커버리지도 함께 확인한다. complete는 자체 검토 상태이지 정답률이 아니다.
"""


from common.lab import LabConfig, compare

# ── 실험 정의 ────────────────────────────────────────────────────────────────
# 비교의 출발점은 모든 구조가 같아야 한다. model·backend·예산·context_bytes를
# 바꾸면 구조 효과와 조건 효과가 섞이므로 바꾼 이유를 주석으로 남긴다.
실험 = LabConfig(
    backend="local",
    workspace="work/rag/compare",
    web="off",
)

# 비교할 구조. 05/07은 학생 구현 파일이므로 구현 전에는 failed 행으로 기록된다.
ARCHITECTURES = [
    "baseline",
    "adaptive",
    "sequential",
    "router",
    "parallel",
    "supervisor",
    "handoff",
]

# 비교할 사례(data/cases.jsonl의 id). 전체 18개 중 대표 6유형:
#   simple(단순 조회) / amendment(추가 약정) / parallel(독립 병렬) /
#   sequential(순차 의존) / followup(후속 대화) / unknown(답 불가·보류 판단)
CASES = ["simple", "amendment", "parallel", "sequential", "followup", "unknown"]

# 반복 횟수(1~5). 3회 이상부터 13_mini_pjt의 짝 비교 요건을 채운다.
REPEATS = 3

# 별도 모델로 기대 사실 대조 평가를 실행한다. 비용은 evaluation_metrics에 따로 기록된다.
JUDGE = True

# 준비된 코퍼스 snapshot 경로. 사용하지 않을 때는 빈 문자열로 둔다.
# local backend의 문서는 자동 준비되므로 비워 두어도 된다.
SEED_WORKSPACE = ""


if __name__ == "__main__":
    compare(
        실험,
        architectures=ARCHITECTURES,
        cases=CASES,
        repeats=REPEATS,
        judge=JUDGE,
        seed_workspace=SEED_WORKSPACE,
    )
    # 결과: outputs/rag-comparison/<run_id>/ 의 runs.jsonl, summary.csv, REPORT.md
