"""실행 계획을 먼저 저장하고, 모든 예정 시도와 결과를 대조한다.

scripted는 파이프라인 검증, live는 실제 LLM 실험이다. 서로의 점수를 섞어 비교하지 않는다.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from business_lab.agents import MAX_MODEL_CALLS, PROMPTS, RECURSION_LIMIT, run_live, run_scripted
from business_lab.contracts import Artifact, Request, Verdict
from business_lab.dataset import DATA, POLICY_VERSION, fingerprint, load_dataset, load_fixtures
from business_lab.environment import MAX_TOOL_CALLS, OrderEnvironment
from business_lab.evaluators import EVALUATOR_VERSION, RESPONSE_RUBRIC_VERSION, evaluate, judge_response
from business_lab.storage import make_run_id, save_run_report


def execute(request: Request, fixture: dict, *, scenario_id: str, release: str, trial: int,
            mode: str, model=None, langfuse=None) -> Artifact:
    """초기 상태를 복원해 실행한다. 실패해도 부분 완료 상태와 호출 기록을 반환한다."""
    started = perf_counter()
    env, initial, final, response = None, {}, None, None
    status, error = "completed", None
    usage = {"model_calls": 0, "input_tokens": None, "output_tokens": None, "cost_usd": None}
    try:
        env = OrderEnvironment(fixture, langfuse=langfuse)
        initial = env.snapshot()
        if mode == "scripted":
            response, usage = run_scripted(request, env, release)
        elif mode == "live":
            response, usage = run_live(request, env, release, model, usage=usage)
        else:
            raise ValueError("mode은 scripted 또는 live입니다.")
    except Exception as exc:
        status = "execution_error" if env else "infra_error"
        error = type(exc).__name__
    finally:
        if env:
            final = env.snapshot()
            env.close()
    return Artifact(artifact_id=uuid4().hex, scenario_id=scenario_id, release=release, trial=trial,
                    request=request, execution_status=status, initial_state=initial, final_state=final,
                    events=env.events if env else [], response=response, error_type=error,
                    elapsed_seconds=perf_counter() - started, **usage)


def make_manifest(cards, fixtures, *, repeats: int, mode: str, model_name: str | None, judge_name: str | None) -> dict:
    """동결한 데이터·fixture·정책·프롬프트·코드·예산과 예정 실행을 모델 호출 전에 기록한다."""
    if repeats < 1 or mode not in {"scripted", "live"}:
        raise ValueError("repeats는 양수, mode은 scripted/live여야 합니다.")
    records = [card.model_dump() for card in cards]
    selected_fixtures = {card.fixture_id: fixtures[card.fixture_id] for card in cards}
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob("*.py"))}
    snapshot = {"cards": records, "fixtures": selected_fixtures,
                "policy": (DATA / "order_policy.md").read_text()}
    return {"run_id": make_run_id(), "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode, "snapshot_hash": fingerprint(snapshot), "snapshot": snapshot,
            "config": {"repeats": repeats, "agent_model": model_name, "judge_model": judge_name,
                       "policy_version": POLICY_VERSION, "evaluator_version": EVALUATOR_VERSION,
                       "response_rubric_version": RESPONSE_RUBRIC_VERSION,
                       "code_hashes": sources, "prompts": PROMPTS, "max_tool_calls": MAX_TOOL_CALLS,
                       "max_model_calls": MAX_MODEL_CALLS, "recursion_limit": RECURSION_LIMIT, "judge_threshold": 0.8},
            "plan": [{"scenario_id": c.metadata.scenario_id, "release": r, "trial": t,
                      "slice": c.metadata.slice, "split": c.metadata.split}
                     for r in PROMPTS for t in range(1, repeats + 1) for c in cards],
            "rows": [], "status": "planned"}


def assess(artifact: Artifact, expected: dict, *, judge=None, require_judge=False) -> list[Verdict]:
    checks = evaluate(artifact, expected)
    if judge is not None:
        checks.append(judge_response(artifact, judge))
    elif require_judge:
        checks.append(Verdict(name="response_semantics", status="INSUFFICIENT_EVIDENCE", passed=None,
                              reason="실제 모델 응답의 의미 평가가 수행되지 않음"))
    return checks


def run(*, mode="scripted", repeats=2, split="dev", out="outputs/business_eval.json",
        use_langfuse=False, use_judge=True, scenario_ids: list[str] | None = None) -> dict:
    """골든 검수 확인 → 계획 저장 → 격리 실행 → 평가 → 짝비교. 외부 호출 여부를 먼저 알린다."""
    from business_lab.reporting import compare, print_report

    cards, fixtures = load_dataset(split=split), load_fixtures()
    if scenario_ids is not None:
        known = {c.metadata.scenario_id for c in cards}
        if not scenario_ids or len(set(scenario_ids)) != len(scenario_ids) or set(scenario_ids) - known:
            raise ValueError("SCENARIO_IDS에 현재 분할의 유효한 문항 ID를 중복 없이 지정하세요.")
        cards = [c for c in cards if c.metadata.scenario_id in scenario_ids]
    model, judge, model_name, judge_name = None, None, None, None
    if mode == "live":
        print(f"이 실행은 {len(cards) * repeats * 2}시도, 에이전트 최대 약 {len(cards) * repeats * 2 * MAX_MODEL_CALLS}회"
              f" + 판사 {'시도당 약 1~3회' if use_judge else '0회'}입니다. 공급자 재시도로 증가할 수 있습니다.")
        from business_lab.models import load_agent_llm, load_judge, load_openrouter_env, resolve_judge

        cfg = load_openrouter_env()
        model_name = cfg["model"]
        if use_judge:
            judge_name = resolve_judge().model
            if judge_name == model_name:
                raise ValueError("에이전트와 판사는 다른 모델을 지정하세요.")
            judge = load_judge(agent_model=model_name)
        model = load_agent_llm(timeout=60)
    elif mode == "scripted":
        print("오프라인 계약 검증: 모델 호출 0회. 이 결과는 LLM 에이전트의 성능 측정이 아닙니다.")
    else:
        raise ValueError("MODE는 scripted 또는 live입니다.")
    report = make_manifest(cards, fixtures, repeats=repeats, mode=mode, model_name=model_name, judge_name=judge_name)
    save_run_report(report, out)

    def persist(artifact, trace_id=None):
        row = {"artifact": artifact.model_dump(), "trace_id": trace_id, "verdicts": []}
        report["rows"].append(row)
        save_run_report(report, out)  # 평가 중 중단돼도 실행 근거가 남는다
        return row

    def record(artifact, expected, trace_id=None):
        row = next((r for r in report["rows"] if r["artifact"]["artifact_id"] == artifact.artifact_id), None)
        if row is None:
            row = persist(artifact, trace_id)
        checks = assess(artifact, expected, judge=judge, require_judge=mode == "live")
        row["verdicts"] = [check.model_dump() for check in checks]
        save_run_report(report, out)
        return checks

    report["status"] = "running"
    try:
        if use_langfuse:
            print("Langfuse에 교육용 데이터셋·실험 실행·업무 스팬·점수를 기록합니다.")
            from business_lab.langfuse_adapter import run_hosted
            run_hosted(report, cards, fixtures, model=model, record=record, persist=persist)
        else:
            by_id = {c.metadata.scenario_id: c for c in cards}
            for planned in report["plan"]:
                card = by_id[planned["scenario_id"]]
                artifact = execute(card.input, fixtures[card.fixture_id], scenario_id=card.metadata.scenario_id,
                                   release=planned["release"], trial=planned["trial"], mode=mode, model=model)
                record(artifact, card.expected_output.model_dump())
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="interrupted", error_type=type(exc).__name__)
        raise
    finally:
        report["comparison"] = compare(report)
        save_run_report(report, out)
    print_report(report)
    return report
