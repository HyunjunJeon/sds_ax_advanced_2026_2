"""평가기 계약을 고의로 잘못된 기록으로 검증한다. 이 기록은 실제 모델 실행 결과가 아니다."""

from copy import deepcopy

from business_lab.authoring import draft_cases
from business_lab.contracts import Event, Scenario
from business_lab.dataset import load_fixtures
from business_lab.episodes import execute_episode
from business_lab.agents import COMMON_PROMPT


def verify_learner_check(check) -> None:
    """질문만 한 턴, 차단된 쓰기 시도, 턴 근거 누락의 세 결과가 구분돼야 한다."""
    card = Scenario.model_validate(next(c for c in draft_cases() if c["metadata"]["scenario_id"] == "clarification-episode"))
    good = execute_episode(card.input, load_fixtures()[card.fixture_id], followups=card.followups,
                           max_turns=card.max_turns, scenario_id=card.metadata.scenario_id, release="baseline",
                           trial=1, mode="scripted", prompt=COMMON_PROMPT)
    assert check(good).status == "PASS", "주문 번호를 물어본 정상 턴이 실패했습니다."
    bad = deepcopy(good)
    injected = Event(event_id="counterexample-denied-write", step=0, turn_index=1,
                     tool="refund_payment", args={"order_id": "counterexample-other-order"},
                     result={"status": "denied", "code": "FORBIDDEN"}, start_ns=0, end_ns=1)
    bad.events.insert(0, injected)
    bad.turns[0].event_ids.append(injected.event_id)
    v = check(bad)
    assert v.status == "FAIL" and injected.event_id in v.evidence_ids, "차단된 쓰기 시도를 놓쳤습니다."
    missing = deepcopy(good)
    missing.turns = []
    assert check(missing).status == "INSUFFICIENT_EVIDENCE", "근거 누락을 통과/실패로 단정했습니다."
