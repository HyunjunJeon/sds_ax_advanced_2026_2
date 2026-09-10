"""03·05·07: 실행과 채점을 분리하고 같은 조건의 원자료를 비교한다.

모든 예정 시도를 기록하고, 기록의 해시를 확인한 후 재채점한다. 모델 호출 함수는 비용을 먼저 알린다.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path

from business_lab.agents import MAX_MODEL_CALLS, RECURSION_LIMIT
from business_lab.contracts import Artifact, Scenario, Verdict
from business_lab.dataset import fingerprint
from business_lab.environment import MAX_TOOL_CALLS
from business_lab.episodes import execute_episode
from business_lab.evaluators import EVALUATOR_VERSION, RESPONSE_RUBRIC_VERSION, evaluate, judge_response
from business_lab.reporting import compare
from business_lab.storage import make_run_id, save_run_report

PACKAGE = Path(__file__).parent
EXECUTION_FILES = ("agents.py", "environment.py", "contracts.py", "episodes.py", "models.py")


def execution_contract(mode: str, model_name: str | None) -> dict:
    """비교에서 고정할 실행 설정. Prompt는 별도 기록하므로 이 해시에는 넣지 않는다."""
    from business_lab.models import DEFAULT_BASE_URL, _env_lookup
    endpoint = (_env_lookup(None)("OPENROUTER_BASE_URL", "OPENAI_BASE_URL") or DEFAULT_BASE_URL) if mode == "live" else None
    return {"mode": mode, "agent_model": model_name, "agent_endpoint_hash": fingerprint(endpoint), "max_model_calls": MAX_MODEL_CALLS,
            "max_tool_calls": MAX_TOOL_CALLS, "recursion_limit": RECURSION_LIMIT,
            "temperature": 0.0, "timeout_seconds": 60,
            "code_hashes": {name: hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest() for name in EXECUTION_FILES},
            "lock_hash": hashlib.sha256((PACKAGE.parent / "uv.lock").read_bytes()).hexdigest()}


def save_recording(report: dict, out: Path) -> None:
    """평가 전에 저장한다. 해시는 원자료의 실수로 인한 변경을 검출하며 전자서명은 아니다."""
    report.pop("recording_hash", None)
    report["recording_hash"] = fingerprint(report)
    save_run_report(report, out)


def read_recording(path: Path) -> dict:
    data = json.loads(path.read_text())
    expected_hash = data.get("recording_hash")
    if data.get("kind") != "recording" or expected_hash != fingerprint({k: v for k, v in data.items() if k != "recording_hash"}):
        raise ValueError("실행 원자료가 아니거나 기록 내용이 변경됐습니다.")
    if data["snapshot_hash"] != fingerprint(data["snapshot"]):
        raise ValueError("실행에 사용한 데이터 스냅샷이 변경됐습니다.")
    return data


def record_release(frozen: dict, *, release: str, prompt: str, repeats: int, mode: str,
                   use_langfuse: bool, out: Path, model=None, client=None) -> dict:
    """동결 데이터의 한 Agent 버전을 실행한다. 평가하지 않는다. 실패한 시도도 원자료에 남긴다."""
    if release not in {"baseline", "candidate"} or repeats < 1 or mode not in {"live", "scripted"}:
        raise ValueError("실행 버전·반복·mode이 유효하지 않습니다.")
    if frozen["snapshot_hash"] != fingerprint(frozen["snapshot"]):
        raise ValueError("데이터가 동결 후 변경됐습니다.")
    cards = [Scenario.model_validate(c) for c in frozen["snapshot"]["cards"]]
    if not cards or any(c.metadata.review_status != "approved" for c in cards):
        raise ValueError("승인된 문항이 필요합니다.")
    model_name = None
    if mode == "live":
        print(f"이 실행은 {len(cards)}문항 × {repeats}회 × 1버전 = {len(cards) * repeats}시도, "
              f"Agent 최대 약 {len(cards) * repeats * MAX_MODEL_CALLS}회, Judge 0회입니다. 재시도로 증가할 수 있습니다.")
        from business_lab.models import load_agent_llm, load_openrouter_env
        if model is None:
            model_name = load_openrouter_env()["model"]
            model = load_agent_llm(timeout=60)
        else:
            model_name = getattr(model, "model_name", type(model).__name__)
    else:
        print("모델 0회: scripted 계약 검증이며 LLM 성능 측정이 아닙니다.")
    report = {"run_id": make_run_id(), "kind": "recording", "mode": mode, "release": release,
              "snapshot_hash": frozen["snapshot_hash"], "snapshot": deepcopy(frozen["snapshot"]),
              "dataset_run_id": frozen["run_id"], "status": "planned", "rows": [],
              "started_at": datetime.now(timezone.utc).isoformat(),
              "config": {**execution_contract(mode, model_name), "repeats": repeats, "prompt": prompt},
              "plan": [{"scenario_id": c.metadata.scenario_id, "release": release, "trial": t,
                        "slice": c.metadata.slice, "split": c.metadata.split}
                       for t in range(1, repeats + 1) for c in cards]}
    save_recording(report, out)

    def attempt(card, trial):
        a = execute_episode(card.input, report["snapshot"]["fixtures"][card.fixture_id],
                            followups=card.followups, max_turns=card.max_turns, scenario_id=card.metadata.scenario_id,
                            release=release, trial=trial, mode=mode, prompt=prompt, model=model, langfuse=client)
        trace_id = client.get_current_trace_id() if client else None
        report["rows"].append({"artifact": a.model_dump(), "trace_id": trace_id, "verdicts": []})
        save_recording(report, out)
        return {"artifact": a.model_dump(), "trace_id": trace_id}

    try:
        report["status"] = "running"
        if use_langfuse:
            print("Langfuse에 승인 Dataset·Run·Trace·업무 Observation을 기록합니다. 점수는 05에서 붙입니다.")
            from business_lab.connections import connect_langfuse
            from business_lab.langfuse_adapter import prepare_dataset
            from langfuse import propagate_attributes

            client = client or connect_langfuse()
            dataset = prepare_dataset(client, report, cards)
            by_id = {c.metadata.scenario_id: c for c in cards}
            for trial in range(1, repeats + 1):
                def task(*, item, **kwargs):
                    with propagate_attributes(session_id=f"{report['run_id']}-{item.metadata['scenario_id']}-{trial}",
                                              metadata={"release": release, "trial": str(trial)}):
                        return attempt(by_id[item.metadata["scenario_id"]], trial)
                result = dataset.run_experiment(name="day05-course", run_name=f"{report['run_id']}-{release}-r{trial}",
                    task=task, evaluators=[], max_concurrency=1,
                    metadata={"snapshot_hash": report["snapshot_hash"], "mode": mode, "release": release, "trial": trial})
                report.setdefault("langfuse_runs", []).append(result.dataset_run_url)
        else:
            for trial in range(1, repeats + 1):
                for card in cards:
                    attempt(card, trial)
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="interrupted", error_type=type(exc).__name__)
        raise
    finally:
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        save_recording(report, out)
        if client:
            client.flush()
    return report


def check_episode(artifact: Artifact, card: Scenario) -> Verdict:
    """첫 질문의 적절성과 전체 Episode의 종료 조건을 따로 확인한다."""
    expected_requests = [card.input, *[f.request for f in card.followups]]
    turns = artifact.turns
    if any(t.response is None for t in turns) or not turns:
        return Verdict(name="episode", status="INSUFFICIENT_EVIDENCE", passed=None, reason="턴 응답 근거 누락")
    ok = len(turns) == len(expected_requests)
    for index, turn in enumerate(turns):
        ok &= index < len(expected_requests) and turn.request == expected_requests[index]
        if index < len(card.followups):
            ok &= turn.response.kind == card.followups[index].when_response
        if turn.request.order_id is None:
            from business_lab.environment import WRITE_TOOLS
            ok &= not any(e.tool in WRITE_TOOLS for e in artifact.events if e.event_id in turn.event_ids)
    return Verdict(name="episode", status="PASS" if ok else "FAIL", passed=bool(ok),
                   reason="턴별 입력·추가 질문·대상 확인 전 쓰기·최종 턴 도달을 검사")


def grade_recording(raw: dict, *, judge=None, extra_check=None) -> dict:
    """저장된 원자료를 채점한다. Judge를 전달하면 실제 모델 호출이 있다. Agent는 재실행하지 않는다."""
    report = deepcopy(raw)
    report.update(run_id=make_run_id(), kind="scored_recording", source_run_id=raw["run_id"],
                  source_recording_hash=report.pop("recording_hash"),
                  evaluator_version=EVALUATOR_VERSION)
    report["config"]["response_rubric_version"] = RESPONSE_RUBRIC_VERSION
    report["config"]["evaluator_hash"] = fingerprint({
        name: hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest()
        for name in ("evaluators.py", "lesson_runs.py", "reporting.py")})
    report["config"]["judge_model"] = judge.get_model_name() if judge is not None else None
    report["config"]["judge_settings"] = {k: getattr(judge, k, None) for k in
        ("temperature", "timeout", "max_retries", "structured_method")} if judge is not None else None
    if judge is not None:
        report["config"]["judge_endpoint_hash"] = fingerprint(getattr(judge, "base_url", None))
    report["config"]["learner_hash"] = (hashlib.sha256(Path(inspect.getsourcefile(extra_check)).read_bytes()).hexdigest()
                                          if extra_check else None)
    cards = {c["metadata"]["scenario_id"]: Scenario.model_validate(c) for c in raw["snapshot"]["cards"]}
    for row in report["rows"]:
        a = Artifact.model_validate(row["artifact"])
        card = cards[a.scenario_id]
        checks = evaluate(a, card.expected_output.model_dump())
        checks.append(check_episode(a, card))
        if extra_check:
            try:
                check = extra_check(a)
                if not isinstance(check, Verdict) or check.name in {c.name for c in checks}:
                    raise ValueError("추가 평가기는 고유한 이름의 Verdict를 반환해야 합니다.")
                checks.append(check)
            except Exception as exc:
                checks.append(Verdict(name="learner_check", status="EVALUATOR_ERROR", passed=None, reason=type(exc).__name__))
        if judge is not None:
            checks.append(judge_response(a, judge, card.expected_output))
        elif raw["mode"] == "live":
            checks.append(Verdict(name="response_semantics", status="INSUFFICIENT_EVIDENCE", passed=None, reason="Judge 미실행"))
        row["verdicts"] = [c.model_dump() for c in checks]
    return report


def load_judge_for(raw: dict, *, enabled=True):
    """실행과 다른 모델의 판사를 준비하고 예정 채점 규모를 알린다."""
    if not enabled or raw["mode"] != "live":
        return None
    print(f"이 채점은 Agent 재실행 0회, Judge 약 {len(raw['rows'])}~{len(raw['rows']) * 3}회입니다.")
    from business_lab.models import load_judge, resolve_judge
    if resolve_judge().model == raw["config"]["agent_model"]:
        raise ValueError("Agent와 Judge는 다른 모델이어야 합니다.")
    return load_judge(agent_model=raw["config"]["agent_model"])


def assert_same_protocol(base: dict, candidate: dict) -> None:
    if base["release"] != "baseline" or candidate["release"] != "candidate":
        raise ValueError("baseline과 candidate의 원자료가 필요합니다.")
    keys = ["agent_model", "agent_endpoint_hash", "mode", "max_model_calls", "max_tool_calls", "recursion_limit",
            "temperature", "timeout_seconds", "code_hashes", "lock_hash", "repeats"]
    if base["snapshot_hash"] != candidate["snapshot_hash"] or any(base["config"][k] != candidate["config"][k] for k in keys):
        raise ValueError("데이터·모델·업무 코드·예산·반복 조건이 다릅니다. 같은 조건으로 다시 실행하세요.")


def compare_scored(base: dict, candidate: dict) -> dict:
    """같은 판정 기준으로 채점한 두 버전의 모든 시도를 비교한다."""
    assert_same_protocol(base, candidate)
    for key in ("response_rubric_version", "evaluator_hash", "judge_model", "judge_settings", "judge_endpoint_hash", "learner_hash"):
        if base["config"].get(key) != candidate["config"].get(key):
            raise ValueError("평가기·Judge·수강생 평가 코드가 다릅니다. 두 원자료를 함께 재채점하세요.")
    report = {"run_id": make_run_id(), "kind": "comparison", "mode": base["mode"],
              "snapshot_hash": base["snapshot_hash"], "snapshot": base["snapshot"],
              "config": base["config"], "source_runs": [base["source_run_id"], candidate["source_run_id"]],
              "evaluation_run_ids": [base["run_id"], candidate["run_id"]],
              "prompts": {"baseline": base["config"]["prompt"], "candidate": candidate["config"]["prompt"]},
              "plan": [*base["plan"], *candidate["plan"]], "rows": [*base["rows"], *candidate["rows"]]}
    report["comparison"] = compare(report)
    transitions = {"improved": 0, "regressed": 0, "stable_pass": 0, "still_failing": 0, "uncomparable": 0}
    for pair in report["comparison"]["pairs"]:
        if pair["change"] == "uncomparable":
            bucket = "uncomparable"
        else:
            b, c = pair["baseline"]["all_pass"], pair["candidate"]["all_pass"]
            bucket = {(False, True): "improved", (True, False): "regressed",
                      (True, True): "stable_pass", (False, False): "still_failing"}[b, c]
        transitions[bucket] += 1
    report["comparison"]["all_repeat_transitions"] = transitions
    if base["config"]["prompt"] == candidate["config"]["prompt"]:
        report["comparison"]["decision"] = "HOLD"
        report["comparison"]["decision_reasons"].append("후보 프롬프트 변경 없음")
    return report
