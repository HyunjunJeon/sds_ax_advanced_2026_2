"""요청·후속 입력·업무 상태의 수명을 관리한다. 이 모듈은 평가 정답을 받지 않는다."""

from contextlib import nullcontext
from time import perf_counter
from uuid import uuid4

from business_lab.agents import make_live_session, run_scripted
from business_lab.contracts import Artifact, Followup, Request, TurnRecord
from business_lab.environment import OrderEnvironment


def execute_episode(request: Request, fixture: dict, *, followups: list[Followup], max_turns: int,
                    scenario_id: str, release: str, trial: int, mode: str, prompt: str,
                    model=None, langfuse=None) -> Artifact:
    """같은 Episode에서 환경·대화·예산을 유지한다. 오류가 나도 부분 상태·각 턴을 보존한다.

    live는 실제 모델 호출이다. scripted는 정답을 읽지 않는 서비스 계약 검증이다.
    후속 입력은 직전 응답이 사전 조건에 맞을 때만 공급한다. 무조건 다음 정답 입력을 주지 않는다.
    """
    started = perf_counter()
    env, initial, final, response = None, {}, None, None
    current, turns = request, []
    status, error = "completed", None
    usage = {"model_calls": 0, "input_tokens": None, "output_tokens": None, "cost_usd": None}
    episode_id = uuid4().hex
    try:
        if max_turns < 1 or len(followups) + 1 > max_turns:
            raise ValueError("InvalidEpisodeBudget")
        env = OrderEnvironment(fixture, langfuse=langfuse)
        initial = env.snapshot()
        if mode == "live":
            invoke = make_live_session(env, release, model, usage=usage, prompt=prompt)
        elif mode == "scripted":
            usage.update(input_tokens=0, output_tokens=0, cost_usd=0.0)

            def invoke(req):
                return run_scripted(req, env, release)[0]
        else:
            raise ValueError("UnknownMode")
        for index in range(max_turns):
            if index:
                if index > len(followups) or response is None:
                    break
                next_input = followups[index - 1]
                if response.kind != next_input.when_response:
                    break
                current = next_input.request
            env.turn_index = index + 1
            before = env.snapshot()
            event_offset = len(env.events)
            response = None
            scope = (langfuse.start_as_current_observation(name=f"turn-{index + 1}", as_type="agent",
                       input=current.model_dump(), metadata={"episode_id": episode_id, "turn_index": index + 1})
                     if langfuse else nullcontext())
            with scope as span:
                try:
                    response = invoke(current)
                finally:
                    turns.append(TurnRecord(turn_index=index + 1, request=current, response=response,
                        event_ids=[e.event_id for e in env.events[event_offset:]], initial_state=before,
                        final_state=env.snapshot(), trace_id=langfuse.get_current_trace_id() if langfuse else None))
                    if span:
                        span.update(output=response.model_dump() if response else {"response_missing": True})
    except Exception as exc:
        status = "execution_error" if env else "infra_error"
        error = type(exc).__name__
    finally:
        if env:
            final = env.snapshot()
            env.close()
    return Artifact(artifact_id=uuid4().hex, scenario_id=scenario_id, release=release, trial=trial,
                    request=current, initial_request=request, episode_id=episode_id, turns=turns,
                    execution_status=status, initial_state=initial, final_state=final,
                    events=env.events if env else [], response=response, error_type=error,
                    elapsed_seconds=perf_counter() - started, **usage)
