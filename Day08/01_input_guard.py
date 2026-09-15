"""
실행:   uv run python 01_input_guard.py
        INJECTION_GUARD="fake"(기본, 키워드 규칙) / "remote"(강사 RunPod, .env 의 GUARD_INJECTION_* 필요) / "openrouter"(정책 Judge)
        MODEL_MODE="scripted" 는 TRANSFORM 결정·메시지 교체 동작만 확인한다. "live" 는 6실행, 실행당 10~15회 호출.
변수:   CONFIGS 의 scope 하나. ("user",) 대 ("user","tool_result").

포인트:
  1. Input Guard 는 before_model 훅이다. "마지막 메시지"가 아니라 "아직 검사 안 한 새 입력"을 id 로 골라낸다.
  2. 사용자 메시지가 위험하면 BLOCK → jump_to="end" (모델 호출 0회). 문서가 위험하면 TRANSFORM → 표시를 붙인 채 계속.
  3. 분류기는 신호(GuardSignal)만 낸다. 결정은 guardlab/defenses.py resolve_input_decision 이 내린다. UNKNOWN ≠ ALLOW.
  4. Guard 는 Main 에만 있다. research 가 read_doc 으로 읽은 원문은 검사 없이 research 의 모델에 들어간다 → 02 의 주제.
  5. security_training(정상 문서)이 '과잉 차단' 으로 나오면 그 분류기는 문서 검사에 못 쓴다는 뜻이다.

주요 내용:
- 질문: Input Guard 를 사용자 메시지에만 붙였을 때와 Tool Result 까지 넓혔을 때 무엇이 달라지는가.
- 도구는 원시 구현(권한 검사 없음)이다. Input Guard 의 효과만 분리해 보기 위해서다.
- 사례: doc_send_outside(외부 전달 지시), doc_omit_failures(실패 누락 지시), security_training(예문 인용 정상 문서).

결과: outputs/01/<run_id>/runs.jsonl, summary.md
단위 검증: uv run pytest tests/test_defenses_01.py -q
"""

from deepagents import create_deep_agent

from guardlab.budget import budget_middleware
from guardlab.cases import get_case
from guardlab.components import InputGuardMiddleware
from guardlab.context import UserContext
from guardlab.evaluate import evaluate, explain, save_rows
from guardlab.guards import InjectionGuard
from guardlab.prompts import MAIN_PROMPT, research_spec, verifier_spec
from guardlab.runner import announce, output_dir, prepare, print_row, run_case
from guardlab.tools import RawOps, build_tools
from guardlab.trace import TraceMiddleware

# ── 실행 조건 ────────────────────────────────────────────────────────────────
CASES = ["doc_send_outside", "doc_omit_failures", "security_training"]
CONFIGS = {
    "user_only": ("user",),
    "user_and_tool_results": ("user", "tool_result"),
}
# "remote": 강사 RunPod 의 SGuard/Kanana (GUARD_INJECTION_URL 필요)
# "fake":   키워드 규칙. 부품 동작 확인용이며 탐지율로 보고하지 않는다.
INJECTION_GUARD = "fake"
MODEL_MODE = "live"      # "live" / "scripted" (대본 재생: Guard 결정·메시지 교체 동작만 확인한다)
REPEATS = 1


def build_agent(prepared, model, scope):
    """00 과 같은 조립에 Input Guard 하나를 더한다. 바뀐 것은 `middleware` 목록의 InputGuardMiddleware 뿐이다.

    scope 가 이 파일의 유일한 실험 변수다:
      ("user",)                  HumanMessage 만 검사. 문서 속 지시는 검사 대상이 아니다.
      ("user", "tool_result")    ToolMessage(read_doc 결과, research 의 요약)까지 검사.
    """
    # 분류기. "fake" 는 키워드 규칙, "remote" 는 RunPod 의 Kanana/SGuard, "openrouter" 는 정책 Judge.
    # 어느 것이든 GuardSignal(위험 신호)만 돌려주고, 결정(BLOCK/TRANSFORM/ALLOW)은 defenses.resolve_input_decision 이 내린다.
    guard = InjectionGuard(INJECTION_GUARD)

    # 도구는 00 과 같은 원시 구현이다. 권한 검사를 일부러 빼서 Input Guard 의 효과만 분리해 본다.
    ops = RawOps(prepared.ws, prepared.outbox, prepared.log)
    tools = build_tools(ops)
    research_tools = [t for t in tools if t.name in ("list_projects", "read_doc")]
    verifier_tools = [t for t in tools if t.name in ("read_doc",)]
    return create_deep_agent(
        model=model,
        system_prompt=MAIN_PROMPT,
        tools=tools,
        context_schema=UserContext,
        backend=prepared.ws.backend(),
        subagents=[
            # Subagent 스택에는 Input Guard 가 없다. research 가 read_doc 으로 읽은 원문은 research 의 모델에
            # 검사 없이 들어간다. Main 이 보는 것은 research 가 돌려준 '요약'(task 도구의 ToolMessage)뿐이다.
            research_spec(research_tools, middleware=[TraceMiddleware(prepared.log, "research")]),
            verifier_spec(verifier_tools, middleware=[TraceMiddleware(prepared.log, "verifier")]),
        ],
        middleware=[
            TraceMiddleware(prepared.log, "main"),
            # Input Guard. before_model 훅에서 "아직 검사하지 않은 새 입력"을 모아 분류기에 보내고,
            #   BLOCK    → jump_to="end": 모델을 부르지 않고 즉시 종료 (사용자 메시지가 위험할 때)
            #   TRANSFORM→ 같은 id 의 ToolMessage 로 교체: "[검사됨 …]" 표시를 붙여 데이터로만 쓰게 한다
            #   ALLOW    → 그대로 진행
            # middleware 순서: 기록기 → Guard → 예산. Guard 가 예산보다 앞이라 차단된 실행은 모델 호출 0회다.
            InputGuardMiddleware(guard, prepared.log, scope=scope, agent_name="main"),
            *budget_middleware(),
        ],
    )


def make_model():
    if MODEL_MODE == "scripted":
        from guardlab.replay import ScriptedChatModel, script_read_meeting_then_report

        return ScriptedChatModel(steps=script_read_meeting_then_report())
    from guardlab.config import build_model

    return build_model()


if __name__ == "__main__":
    if MODEL_MODE == "live":
        announce(f"사례 {len(CASES)}건 × 구성 {len(CONFIGS)}개 × {REPEATS}회, 실행당 모델 호출 약 10~15회")
    rows = []
    for name, scope in CONFIGS.items():
        for case_id in CASES:
            case = get_case(case_id)
            for r in range(1, REPEATS + 1):
                prepared = prepare(case, "01", name, r)
                agent = build_agent(prepared, make_model(), scope)
                out = run_case(agent, prepared)
                row = evaluate(case, prepared.ctx, prepared.ws, prepared.outbox, prepared.log,
                               final_answer=out["final_answer"], error=out["error"], elapsed_s=out["elapsed_s"],
                               config={"name": name, "scope": list(scope), "guard": INJECTION_GUARD, "model_mode": MODEL_MODE})
                rows.append(row)
                print_row(row)
    path = save_rows(rows, output_dir("01"))
    print(explain(rows, title="01 Input Guard 범위"))
    print(f"\n결과: {path}")
    print("읽을 것: 같은 사례에서 scope 에 따라 '차단위치'와 '공격달성'이 어떻게 달라졌는가, security_training 이 과잉 차단됐는가.")
