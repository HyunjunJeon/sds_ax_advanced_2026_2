"""07. [학생 구현] 하이브리드 검색 합류 — 범위·버전·다양성을 지키는 최종 순위.

03_search.py는 dense/sparse/RRF 비교를 보여주기만 한다. 이 파일에서는 그 합류
정책 자체를 student_tasks.fuse_and_rank로 구현한다. 구현 전 NotImplementedError가
나는 것이 정상이고, challenge_tests/test_fusion_student.py가 전부 통과하면 완성이다.

만들어야 할 구조(순수 함수, I/O 없음):
    rankings(검색기별 URI 순위) + facts(관리 메타데이터)
      → 입력 검증 → 범위 필터(approved·고객·기준일)
      → 버전 대체(같은 family+title은 최신 version만)
      → RRF 점수 → 다양성 배치(family 대표 우선, per_family 한도) → limit

실패 상황(이 정책이 지켜야 할 것):
- 만료된 v1이 키워드 유사성으로 1위에 올라와도 as_of 기준 유효한 v2로 대체된다.
- LLM reranker가 입력에 없는 URI를 반환하면 ValueError로 거절한다(환각 인용 차단).
- 한 family의 유사 문서 5개가 상위를 독점해도 다른 family의 유일한 근거가
  limit 안에 들어온다.

구현 완료 후의 비교 실험(실제 검색 연결):
1) 02_ingest.py로 적재한 뒤 03_search.py를 실행해 dense/sparse 순위를 확보한다.
2) student_tasks.fuse_and_rank를 src/day02/tools/search.py의 candidate_policy로
   연결해 RetrievalContext.search와 day02 ask를 다시 실행한다.
3) 기본 정책과 학생 정책의 최종 문서 순서·답변 인용이 어떻게 달라지는지, 그 이유가
   범위·버전·다양성 규칙 중 무엇 때문인지 설명한다.
"""

from challenges import run_challenge

if __name__ == "__main__":
    run_challenge(
        "07 [학생 구현] 하이브리드 검색 합류",
        "여러 검색기의 순위를 범위 필터 아래에서 합류한다. 만료 버전 대체, 미등록 ID "
        "거절, family 다양성이 계약이다.",
        "test_fusion_student.py",
    )
