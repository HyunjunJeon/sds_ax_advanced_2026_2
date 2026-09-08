"""10. [챌린지 A] 시점·버전 선택 — as_of가 바꾸는 문서 세계를 결정한다.

검색 품질 이전에 "그 날짜에 존재하는 문서가 무엇인가"가 답변을 결정한다.
student_tasks.effective_documents는 catalog 전체에서 as_of 기준 유효 문서와
제외 이유(other_entity/status/not_yet/expired/superseded)를 결정적으로 반환한다.
구현 전 NotImplementedError가 나는 것이 정상이고,
challenge_tests/test_version_challenge.py가 전부 통과하면 완성이다.

만들어야 할 구조(순수 함수, I/O 없음):
    catalog(DocumentFacts) + entity + as_of
      → 제외 이유 판정(고객 → 상태 → 미래 → 만료 순서)
      → (family, title) 겹침 해소(valid_from이 늦은 쪽) → 정렬된 EffectiveSet

실패 상황(이 선택이 지켜야 할 것):
- 2026-06-30과 2026-07-01의 유효 SLA가 v1/v2로 정확히 갈린다(반개구간 경계).
- draft 개정안(유효일 2026-08-01, status draft)은 9월에도 답변 근거가 되지 않는다.
- 유효 기간이 겹치는 catalog 오류에서 두 버전이 함께 남지 않는다.

구현 완료 후의 비교 실험(실제 질의 연결):
1) data/business/catalog.json을 DocumentFacts로 읽는다.
2) as_of를 6/30, 7/1, 7/15, 9/8로 바꿔가며 집합 변화를 기록한다.
3) day02 ask "알파 SLA 신청 기한" --as-of 2026-07-15는 15일, --as-of 2026-09-08은
   20일(추가 약정)로 답하는지 대조한다. 집합과 답변이 함께 바뀌어야 한다.
"""

from challenges import run_challenge

if __name__ == "__main__":
    run_challenge(
        "10 [챌린지 A] 시점·버전 선택 — 25점",
        "as_of 기준 유효 문서 집합과 제외 이유를 결정한다. 반개구간 경계, draft 배제, "
        "겹침 해소가 계약이다.",
        "test_version_challenge.py",
    )
