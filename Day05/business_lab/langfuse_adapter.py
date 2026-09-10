"""동결한 Scenario를 Langfuse Dataset → Run → Trace → Score로 연결한다.

업로드와 실행은 명시적으로 USE_LANGFUSE=True일 때만 호출된다. 외부 시스템 쓰기가 있다.
계약은 Day05에 고정된 langfuse 4.15.1의 DatasetClient.run_experiment를 따른다.
"""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from business_lab.contracts import Artifact, Request
from business_lab.dataset import fingerprint
from business_lab.experiment import execute


def scenario_input(card):
    """Agent 요청과 실행 하네스의 후속 입력·초기 조건을 구분해 Dataset에 보관한다."""
    return {"request": card.input.model_dump(), "fixture_id": card.fixture_id,
            "followups": [f.model_dump() for f in card.followups], "max_turns": card.max_turns}


def prepare_dataset(client, report, cards):
    """내용 해시별 Dataset을 만들고, 회수한 스냅샷이 로컬 승인 자료와 같은지 검사한다."""
    from langfuse.api.commons.errors.not_found_error import NotFoundError

    name = f"day05/order-cancellation/{report['snapshot_hash'][:20]}"
    try:
        dataset = client.get_dataset(name, version=datetime.now(timezone.utc))
    except NotFoundError:
        client.create_dataset(name=name, description="교육용 주문 취소 시나리오. 실제 고객 데이터 없음",
                              metadata={"snapshot_hash": report["snapshot_hash"]})
        for card in cards:
            client.create_dataset_item(dataset_name=name,
                id=str(uuid5(NAMESPACE_URL, f"{name}/{card.metadata.scenario_id}")),
                input=scenario_input(card),
                expected_output=card.expected_output.model_dump(), metadata=card.metadata.model_dump())
        dataset = client.get_dataset(name, version=datetime.now(timezone.utc))
    actual = {item.metadata["scenario_id"]: {"input": item.input, "expected": item.expected_output, "metadata": item.metadata}
              for item in dataset.items}
    expected = {card.metadata.scenario_id: {"input": scenario_input(card),
                                           "expected": card.expected_output.model_dump(), "metadata": card.metadata.model_dump()} for card in cards}
    if fingerprint(actual) != fingerprint(expected) or len(dataset.items) != len(cards):
        raise ValueError("Langfuse Dataset과 로컬 동결 자료가 다릅니다. 실행하지 않습니다.")
    report["langfuse_dataset"] = {"name": name, "version": str(dataset.version)}
    return dataset


def run_hosted(report, cards, fixtures, *, model, record, persist, client=None):
    """동일 Dataset 객체로 버전·반복별 Run을 실행한다. Agent에는 input.request만 보낸다."""
    from langfuse import Evaluation, propagate_attributes
    from business_lab.connections import connect_langfuse

    client = client or connect_langfuse()
    dataset = prepare_dataset(client, report, cards)
    for release in ("baseline", "candidate"):
        for trial in range(1, report["config"]["repeats"] + 1):
            def task(*, item, **kwargs):
                # item.expected_output을 Agent 경로에서 사용하지 않는다.
                with propagate_attributes(metadata={"release": release, "trial": str(trial)}):
                    artifact = execute(Request.model_validate(item.input["request"]), fixtures[item.input["fixture_id"]],
                                       scenario_id=item.metadata["scenario_id"], release=release, trial=trial,
                                       mode=report["mode"], model=model, langfuse=client)
                trace_id = client.get_current_trace_id()
                persist(artifact, trace_id)
                return {"artifact": artifact.model_dump(), "trace_id": trace_id}

            def evaluator(*, input, output, expected_output, **kwargs):
                artifact = Artifact.model_validate(output["artifact"])
                checks = record(artifact, expected_output, output["trace_id"])
                # 최초 위반이 일어난 서비스 Observation에도 근거를 붙인다.
                by_id = {event.event_id: event for event in artifact.events}
                for check in checks:
                    for eid in check.evidence_ids:
                        event = by_id[eid]
                        if event.observation_id:
                            client.create_score(trace_id=output["trace_id"], observation_id=event.observation_id,
                                                name=f"order.{check.name}", value=check.status,
                                                data_type="CATEGORICAL", comment=check.reason)
                return [Evaluation(name=f"order.{v.name}", value=v.status, data_type="CATEGORICAL",
                                   comment=v.reason, metadata={"evidence_ids": v.evidence_ids}) for v in checks]

            result = dataset.run_experiment(name="day05-order-eval",
                run_name=f"{report['run_id']}-{release}-r{trial}", task=task, evaluators=[evaluator],
                max_concurrency=1, metadata={"snapshot_hash": report["snapshot_hash"], "mode": report["mode"],
                                             "release": release, "trial": trial, **report["config"]})
            report.setdefault("langfuse_runs", []).append({"release": release, "trial": trial,
                                                          "url": result.dataset_run_url})
    client.flush()
