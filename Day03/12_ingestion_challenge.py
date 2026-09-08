"""12. 고급 과제 C — 만료된 Worker를 배제하는 영속 적재와 버전 일관성.

가르치는 것:
- 동시 갱신의 일관성: lease fencing(epoch)으로 만료된 Worker의 늦은 완료가 최신
  ready head를 덮지 못하게 한다. content hash 중복 검사만으로는 막을 수 없는 경합이다.
- 잠금의 범위 설계: 예약은 짧은 DB 트랜잭션에서, 외부 업로드·색인 대기는 그 밖에서
  실행한다. 긴 구간에 DB 잠금을 잡으면 다른 Worker까지 멈춘다.
- 안전한 전환: 새 버전이 ready되기 전까지 이전 ready head를 유지해 서비스 공백을
  만들지 않고, 프로세스 재시작 후에도 계약이 살아있음을 실제 SQLite로 증명한다.

출발점: 08, common/ingestion.py, common/source_registry.py.
실패 상황: Worker A의 lease가 끝나 B가 새 문서를 게시한 뒤 A의 늦은 완료가 도착한다.
단순한 content hash 중복 검사나 Python lock만으로는 이 경합을 해결할 수 없다.

구현 순서:
1. student_tasks.FencedRegistry에 URL별 단조 증가 epoch와 ready head를 저장한다.
2. 예약은 짧은 DB 트랜잭션에서, 외부 업로드/색인은 그 밖에서 실행한다.
3. commit 시 현재 lease와 소유 epoch를 비교하고 오래된 완료를 거절한다.
4. 실제 Ingestion.store에 연결해 업로드 응답 유실·재시작·늦은 완료를 주입한다.
5. 한 응답의 검색 snapshot을 고정하고 도중의 문서 갱신이 답변 근거를 섞지 않게 한다.

제약: 기존 live-v2가 아닌 임시 workspace를 사용한다. ready는 원문 저장과 색인 검색이
모두 확인된 상태다. 새 버전이 준비되지 않았는데 이전 ready를 지우면 서비스 공백이 생긴다.
"""

from common.challenges import run_challenge

if __name__ == "__main__":
    run_challenge(
        "과제 C: lease fencing과 원자적인 ready head 전환",
        __doc__,
        "test_ingestion_challenge.py",
    )
