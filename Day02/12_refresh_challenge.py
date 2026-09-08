"""12. [챌린지 C] 문서 갱신과 세션 일관성 — 낡은 인용을 잡는 턴 장부.

세션의 후속 질문은 대화를 참조하지만 근거는 매 턴 다시 확보한다(05_multiturn).
그렇다면 턴 사이에 문서가 재적재되면 어떻게 알아챌까. student_tasks.SessionLedger는
SQLite에 턴별 (evidence_id, uri, content_hash)을 남기고, 현재 원문 해시와 대조해
낡은 인용을 stale로 보고한다. 구현 전 NotImplementedError가 나는 것이 정상이고,
challenge_tests/test_refresh_challenge.py가 전부 통과하면 완성이다.

만들어야 할 구조(SQLite 지속, 모델 호출 없음):
    record_turn(session, scope, question, evidence)  → 턴 번호(스코프 안 단조 증가)
    stale_evidence(session, scope, current_hashes)   → (evidence_id, turn) 목록
    history(session, scope, limit=4)                 → 최근 턴의 질문·인용 ID

실패 상황(이 장부가 지켜야 할 것):
- 턴 1이 v2 원문 해시로 인용했는데 문서가 재적재되면, 턴 2 이후 그 인용은
  stale로 보고된다. 원문이 사라져 해시를 못 확인해도 보수적으로 stale다.
- 같은 session 이름도 스코프(고객·기준일·namespace)가 다르면 다른 대화다.
- 프로세스를 다시 떠도 기록과 턴 번호가 유지된다. memory dict로는 안 된다.

구현 완료 후의 비교 실험(실제 세션 연결):
1) 05_multiturn.py를 실행하며 각 턴의 근거를 record_turn으로 기록한다.
2) sla-a-v2.md 문장 하나를 수정해 02_ingest.py로 재적재한다(fingerprint가 바뀌어
   새 target_uri로 적재된다).
3) stale_evidence에 현재 manifest의 remote_hash를 넣어 이전 턴 인용이 잡히는지,
   새로 검색한 근거의 evidence_id가 잡히지 않는지 확인한다. 문서는 원복한다.
"""

from challenges import run_challenge

if __name__ == "__main__":
    run_challenge(
        "12 [챌린지 C] 문서 갱신·세션 일관성 — 30점",
        "SQLite 턴 장부로 문서 갱신을 감지하고 낡은 인용을 stale 보고한다. 스코프 "
        "격리와 프로세스 재시작 후 유지가 계약이다.",
        "test_refresh_challenge.py",
    )
