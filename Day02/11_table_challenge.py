"""11. [챌린지 B] 표 근거 보존 — 예산 안에서 단위·조건까지 함께 읽는다.

작은 근거 예산에서 관련도 순서로만 표 조각을 고르면 단위 행과 각주가 잘리고,
조건 없는 수치("20%")가 근거로 남는다. student_tasks.select_table_evidence는
커버 최대 → 렌더 바이트 최소 → ID 사전순의 우선순위로, requires 폐쇄를 지키는
최적 집합을 반환한다. 구현 전 NotImplementedError가 나는 것이 정상이고,
challenge_tests/test_table_challenge.py가 전부 통과하면 완성이다.

만들어야 할 구조(순수 함수, I/O 없음):
    blocks(표 조각 + covers 조건 태그 + requires 의존) + required + max_bytes
      → 입력 검증(중복/미지정/순환/음수) → 의존성 폐쇄
      → 커버 최대 → 바이트 최소 → ID 사전순 탐색 → 정렬된 ID tuple

실패 상황(이 선택이 지켜야 할 것):
- 데이터 행만 선택하고 단위·각주 블록을 빼면, 남은 수치는 적용 조건을 잃는다.
  requires 폐쇄가 이를 막는다.
- 비슷한 표 조각 18개가 관련도 때문에 예산을 독점해도, 필수 묶음(본표+각주)이
  먼저 들어온다.
- 예산이 묶음보다 1바이트 작으면 묶음 전체가 배제되고 부분해가 남는다.

구현 완료 후의 비교 실험(실제 표 연결):
1) sla-a-v2.md를 tools/tables.py의 markdown_tables로 파싱해 헤더·데이터·각주
   블록과 covers/requires 관계를 구성한다.
2) 검색으로 받은 근거 카드 바이트 예산(기본 24,000)의 1/10 수준으로 max_bytes를
   잡고 관련도 상위 순서와 선택 결과를 비교한다.
3) 같은 질문의 table-analysis 답변에서 인용이 단위와 함께 남았는지 대조한다.
"""

from challenges import run_challenge

if __name__ == "__main__":
    run_challenge(
        "11 [챌린지 B] 표 근거 보존 선택 — 25점",
        "예산 안에서 required 커버를 최대화하며 requires 의존성을 지킨다. 단위 행이 "
        "잘린 조각이 근거로 남지 않는 것이 계약이다.",
        "test_table_challenge.py",
    )
