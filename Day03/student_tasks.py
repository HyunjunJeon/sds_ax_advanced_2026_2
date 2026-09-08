"""고급 구현 과제의 공개 API — 10/11/12번 파일과 WORKSHEET.md를 함께 읽는다.

가르치는 것:
- 각 과제는 실무의 한 실패 상황을 압축한 알고리즘 계약이다. A는 예산·의존성을 동시에
  만족하는 Context 선택(조합 최적화), B는 도착 순서와 무관한 재시도 상태 합류와 예약
  기반 배정(분산 조정), C는 lease fencing과 원자적 ready head 전환(동시 갱신 일관성).
- 공개 테스트는 계약의 최소 예시일 뿐이다. 통과했다고 끝이 아니라 실제 common/
  구현에 연결해 실패 반례를 만들고 효과를 측정하는 것이 과제의 본체다.

아래 NotImplementedError는 수강생이 구현할 지점이다. 기본 00~09 서비스에서는 이
모듈을 import하지 않으므로 예제 실행에는 영향을 주지 않는다. 과제별 공개 테스트는
challenge_tests/에 있고, 기본 회귀검사 tests/와 분리되어 있다.

알고리즘을 여기서 검증한 뒤 실제 common/ 구현에 연결하는 것까지가 과제다.
고객/시점 필터, 인용 검증, 전체 요청 예산을 우회해 점수를 올리는 변경은 허용하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ContextItem:
    """선택 가능한 원문 단위. facets는 검색 계획의 항목이며 사후 정답표가 아니다.

    requires는 추가 약정→기본 조항처럼 함께 읽어야 하는 단위 ID다. 원문에 없는 관계를
    코드가 자동으로 보증하지 않는다. 실제 연결에서는 이 관계의 추출·검증도 설명해야 한다.
    """

    id: str
    text: str
    facets: frozenset[str]
    requires: frozenset[str] = frozenset()


def render_selection(items: list[ContextItem], selected: tuple[str, ...]) -> str:
    """공개된 비용 함수. UTF-8 본문뿐 아니라 ID/개행도 Context 예산에 포함한다.

    모든 선택 알고리즘이 같은 렌더러를 써야 바이트 절감을 비교할 수 있다.
    원문을 중간에서 자르거나 ID를 없애 비용 함수를 피하는 것은 정답이 아니다.
    """
    by_id = {item.id: item for item in items}
    return "".join(f"[{key}]\n{by_id[key].text}\n" for key in sorted(selected))


def select_context(
    items: list[ContextItem], required: frozenset[str], max_bytes: int
) -> tuple[str, ...]:
    """과제 A: 의존성을 만족하는 예산 내 최적 근거 집합의 정렬된 ID tuple을 반환한다.

    목적함수 우선순위: (1) required 중 덮은 항목 수 최대화, (2) 렌더링 바이트 최소화,
    (3) ID tuple의 사전순 최소화. 완전 커버가 불가능해도 부분해를 반환해야 한다.
    모든 선택 항목의 requires가 함께 선택되어야 하며 최대 24개 후보를 다룬다.
    중복 ID/알 수 없는 의존성/순환/음수 예산은 선택 전 ValueError로 거절한다.

    TODO A1: 의존성 폐쇄와 입력 검증. 선택되지 않을 후보의 잘못된 의존성도 거절한다.
    TODO A2: 중복 커버 때문에 단순 점수/길이 정렬이 실패하는 반례를 고려해 탐색한다.
    TODO A3: common.service.Service.context에 연결하고 원문 provenance를 그대로 보존한다.
    """
    raise NotImplementedError("과제 A: select_context를 구현하세요. 10_context_challenge.py 참고")


@dataclass(frozen=True)
class Attempt:
    """한 논리 작업의 한 시도 관측. number는 그 작업에서 단조 증가해야 한다.

    running에서 한 terminal 상태로 진행할 수 있다. complete의 output_hash는 검증된
    구조화 결과의 식별자다. 같은 시도에서 다른 완료 내용을 조용히 덮어쓰면 안 된다.
    """

    task_id: str
    number: int
    status: Literal["running", "complete", "retryable", "terminal"]
    output_hash: str = ""


@dataclass(frozen=True)
class WorkItem:
    id: str
    depends_on: tuple[str, ...] = ()
    call_cost: int = 1
    priority: int = 0


@dataclass(frozen=True)
class Dispatch:
    task_id: str
    attempt: int
    reserved_calls: int


def merge_attempts(current: dict[str, Attempt], incoming: list[Attempt]) -> dict[str, Attempt]:
    """과제 B1: 입력을 변경하지 않는 latest-attempt reducer.

    작업별 가장 높은 number만 유효하다. 낮은 시도의 성공이 늦게 와도 최신 실패/진행
    상태를 바꾸지 않는다. 같은 number의 running보다 종료 관측을 우선하고, 서로 다른
    종료 상태나 서로 다른 완료 hash는 ValueError다. 동일 관측의 재전달은 멱등이다.
    number<1, 빈 task_id, 잘못된 status, complete인데 빈 hash도 ValueError다.
    current의 키와 Attempt.task_id가 다르면 거절한다.

    TODO B1: 도착 시각이나 목록 순서 대신 작업 ID·시도 번호를 권위로 삼는다.
    """
    raise NotImplementedError("과제 B1: merge_attempts를 구현하세요")


def schedule_ready(
    tasks: list[WorkItem],
    attempts: dict[str, Attempt],
    *,
    max_inflight: int,
    calls_left: int,
    reserve_calls: int = 2,
) -> tuple[Dispatch, ...]:
    """과제 B2: 현재 ready인 작업만 결정적으로 배정한다. I/O는 하지 않는다.

    running 개수만큼 동시성 슬롯을 차감한다. complete/terminal/running 작업은 재배정하지
    않고 미실행 또는 retryable만 후보가 된다. 모든 선행 작업이 최신 complete여야 한다.
    priority 내림차순, ID 오름차순으로 검사한다. calls_left-reserve_calls 안에 비용이
    들어가는 작업만 배정하며 비싼 작업을 건너뛴 뒤 더 싼 작업은 배정할 수 있다.
    새 작업의 attempt는 1, 재시도는 최신 number+1이다. 이미 running인 작업의 비용은
    calls_left에서 차감된 것으로 본다. reserve_calls는 최종 합류/검토용 호출 여유다.
    잘못된 DAG/중복 ID/비양수 call_cost/알 수 없는 attempts/음수 예산은 ValueError다.

    TODO B2: 한 번의 판단에서 예산과 슬롯을 함께 예약한다.
    TODO B3: 실제 Supervisor에 연결할 때 배정→running 기록을 원자적으로 묶는다.
    이 순수 함수만으로 여러 프로세스의 중복 배정을 방지했다고 주장하면 안 된다.
    """
    raise NotImplementedError("과제 B2: schedule_ready를 구현하세요")


@dataclass(frozen=True)
class Lease:
    url: str
    content_hash: str
    epoch: int
    expires_at: float


@dataclass(frozen=True)
class Published:
    url: str
    content_hash: str
    uri: str
    epoch: int


class FencedRegistry:
    """과제 C: SQLite에 지속되는 URL별 lease와 ready head.

    epoch는 URL별 예약 순서다. 높은 epoch는 더 최근에 예약했다는 뜻이며 문서의
    실제 발행일/의미상 최신 버전을 보증하지 않는다. 그 판단은 수집 정책의 역할이다.
    각 메서드는 별도 프로세스에서 호출되어도 계약이 유지되어야 한다.

    TODO C1: schema와 짧은 트랜잭션을 설계한다. 외부 색인 대기 동안 DB lock을 잡지 않는다.
    TODO C2: commit에서 현재 lease의 epoch/hash/만료/URI를 검증한다.
    TODO C3: common.ingestion에 연결하고 publish 응답 유실·늦은 완료를 주입해 검증한다.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # TODO: 연결별 timeout/트랜잭션 정책과 CREATE TABLE IF NOT EXISTS를 구현한다.
        # 테스트는 임시 DB를 사용한다. 검증된 work/rag/live-v2를 과제 실험으로 변경하지 않는다.

    def claim(self, url: str, content_hash: str, *, now: float, ttl: float) -> Lease | None:
        """새 시도권 또는 None(동일 내용 ready/동일 내용 lease 활성)을 반환한다.

        URL은 common.source_registry.canonical_url로 정규화한다. 다른 내용은 기존 lease가
        활성이어도 새 epoch를 발급해 이전 시도를 무효화한다. 동일 내용도 lease가 만료되면
        epoch를 올려 재발급한다. 이미 ready인 내용으로 되돌리는 claim 역시 다른 내용의
        활성 시도가 있으면 새 epoch로 예약해야 한다. 기존 head는 commit까지 보존한다.
        ttl<=0, 비어 있는 content_hash, 유효하지 않은 URL은 ValueError다.
        """
        raise NotImplementedError("과제 C1: claim을 구현하세요")

    def commit(self, lease: Lease, uri: str, *, now: float) -> bool:
        """현재 epoch/hash/만료 시각이 일치하고 now<expires_at이면 ready head를 갱신한다.

        최신 시도의 동일 URI 완료 재전달은 만료 후에도 True다. 다른 URI 재전달, 이전
        epoch, 만료된 미완료 시도는 False이며 head를 바꾸지 않는다. 빈 URI는 ValueError다.
        입력 Lease 값만 믿지 말고 DB의 권위 있는 예약과 모든 필드를 대조한다.
        """
        raise NotImplementedError("과제 C2: commit을 구현하세요")

    def head(self, url: str) -> Published | None:
        """현재 ready head를 반환한다. 새 버전이 준비 중이면 이전 ready head를 유지한다."""
        raise NotImplementedError("과제 C3: head를 구현하세요")

    def snapshot(self) -> dict[str, Published]:
        """한 읽기 트랜잭션에서 ready heads만 반환한다. lease 중인 버전은 포함하지 않는다."""
        raise NotImplementedError("과제 C4: snapshot을 구현하세요")
