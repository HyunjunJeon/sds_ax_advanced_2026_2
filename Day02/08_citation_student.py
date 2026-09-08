"""08. [학생 구현] 근거 카드 검증 — 현재 턴 인용·해시·범위·수치 주장 판정.

04_rag_with_skills.py의 검증 단계를 관찰했다면, 이 파일에서 그 인용 검증기를
student_tasks.validate_claims로 직접 구현한다. 구현 전 NotImplementedError가 나는
것이 정상이고, challenge_tests/test_citation_student.py가 전부 통과하면 완성이다.

만들어야 할 구조(순수 함수, 모델 호출 없음):
    claims(주장 + 인용 ID) + turn(이번 턴 근거 카드·검색 여부·범위)
      → 검색 여부 → 빈 인용 → ID 존재 → 해시 일치 → 적용 범위
      → 수치 주장의 근거 종류 → 오류 목록(중복 제거, 검사 순서)

실패 상황(이 검증이 지켜야 할 것):
- 이전 턴에서 받은 인용 ID를 이번 턴 답변에 붙이면 거절된다(세션은 대화만 공유).
- 원문이 바뀐 뒤 온 인용문은 해시 불일치로 걸린다.
- 시각 모델 관찰(visual_observation)만으로 "최대 20%" 같은 수치를 단정하지 못한다.

구현 완료 후의 비교 실험(실제 검증 연결):
1) 04_rag_with_skills.py로 답변 하나의 실행 기록(outputs/runs/<id>.json)을 확보한다.
2) student_tasks.validate_claims를 AnswerPayload+RetrievalContext에 맞게 변환해
   src/day02/agents/rag.py의 extra_claim_validation으로 연결한다.
3) 검증 통과 답변과 validation_retry가 발생한 답변의 trace에서 이 검증기가 잡은
   오류와 프로덕션 검증이 잡은 오류를 분리해 설명한다.
"""

from challenges import run_challenge

if __name__ == "__main__":
    run_challenge(
        "08 [학생 구현] 근거 카드 인용 검증",
        "답변 주장의 인용 ID·해시·적용 범위·수치 주장 근거 종류를 검증한다. 이전 턴 "
        "인용 재사용과 변조 인용이 대표 반례다.",
        "test_citation_student.py",
    )
