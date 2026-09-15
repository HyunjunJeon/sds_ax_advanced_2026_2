"""
실행:   uv run python 02_action_guard.py
        live 기본 20실행(사례 5 × 구성 4). 수업 중엔 CASES 를 2건으로 줄인다.
        두 번째 실행은 INJECTION_GUARD="always_allow": 탐지가 전부 실패해도 막히는가.
        MODEL_MODE="scripted" 기본 대본 "delegate_read_beta": Main 이 research 에 위임하고 research 가 beta 를 읽는 경로.
변수:   CONFIGS = 권한 검사의 위치. input_only / middleware_parent / tool_wrapper / tool_wrapper_min.

포인트:
  1. 권한의 근거는 UserContext(신뢰 컨텍스트)뿐이다. 사용자 문구의 "관리자가 승인했다"는 인자에 없다.
  2. 부모의 wrap_tool_call 은 `task` 호출까지만 감싼다. research 내부의 read_doc 은 지나지 않는다 (deepagents/graph.py:730-733).
     화면의 "검사 없이 실행된 경로 ['research']" 가 그 증거다.
  3. 도구 함수 안의 guarded_call 은 누가 부르든 같은 검사를 지난다. ALLOW 가 아니면 impl 이 실행되지 않는다.
  4. 내장 read_file 은 업무 도구와 다른 경로다. tool_wrapper_min 은 scope_permissions(권한)와 Harness Profile(도구 목록에서 제거, guardlab/harness.py)로 그 경로를 닫는다.
  5. general-purpose Subagent 는 부모 도구 전체를 물려받는다 (graph.py:848-851). 같은 이름의 spec 으로 덮어 닫는다.

주요 내용:
- 질문: 모델의 탐지가 실패해도 Tool 계층의 권한 검사가 피해를 막는가. Subagent 경로도 막히는가.
- 방어 구현: guardlab/defenses.py 의 PermissionTable.decide / normalize_args / guarded_call.
- 사례: user_asks_beta(권한 주장), doc_read_beta(Subagent 유도), doc_send_outside, intern_send(전달 권한 없음), normal.

결과: outputs/02/<run_id>/runs.jsonl, summary.md
단위 검증: uv run pytest tests/test_defenses_02.py -q
"""

from deepagents import create_deep_agent

from guardlab.budget import budget_middleware
from guardlab.cases import get_case
from guardlab.components import (ActionGuardMiddleware, InputGuardMiddleware, load_reviewers, make_action_wrapper,
                                 scope_permissions)
from guardlab.context import UserContext
from guardlab.evaluate import evaluate, explain, save_rows
from guardlab.harness import minimal_tools
from guardlab.guards import InjectionGuard
from guardlab.prompts import MAIN_PROMPT, research_spec, verifier_spec
from guardlab.runner import announce, output_dir, prepare, print_row, run_case
from guardlab.tools import RawOps, build_tools
from guardlab.trace import TraceMiddleware
from guardlab.defenses import PermissionTable

# ── 실행 조건 ────────────────────────────────────────────────────────────────
CASES = ["user_asks_beta", "doc_read_beta", "doc_send_outside", "intern_send", "normal"]
CONFIGS = ["input_only", "middleware_parent", "tool_wrapper", "tool_wrapper_min"]
INJECTION_GUARD = "fake"   # "remote" / "fake" / "always_allow"(방어 경계 시험)
MODEL_MODE = "live"        # "live" / "scripted"
SCRIPT = "delegate_read_beta"  # scripted 일 때 대본: "read_beta" / "send_outside" / "delegate_read_beta"(Subagent 경로)
REPEATS = 1
# scripted 모드에서는 사용자 메시지가 아니라 대본이 행동을 정하므로, 대본에 맞는 사례 하나만 돌린다.
SCRIPT_CASE = {"read_beta": "doc_read_beta", "send_outside": "doc_send_outside", "delegate_read_beta": "doc_read_beta"}


def make_table() -> PermissionTable:
    """신뢰된 권한표. 검토자 목록은 작업 공간의 reviewers.json 이 아니라 원본(load_reviewers)에서 읽는다.
    공격자는 작업 공간의 파일만 바꿀 수 있으므로, 허용 목록을 작업 공간에서 읽으면 목록 자체가 공격 대상이 된다."""
    rv = load_reviewers()
    return PermissionTable(reviewers={k: v for k, v in rv.items() if isinstance(v, list) and k != "allowed_domains"},
                           allowed_domains=tuple(rv.get("allowed_domains", ["nurisoft.example"])))


def build_agent(prepared, model, config: str, sub_model=None):
    """네 구성은 '권한 검사를 어디에 두는가'만 다르다. 프롬프트·도구·사례·예산은 같다.

    config:
      input_only         01 과 같다. 분류기가 놓치면 끝.
      middleware_parent  권한 검사를 Main 의 wrap_tool_call(ActionGuardMiddleware)에 둔다.
                         부모 middleware 는 Subagent 에 상속되지 않으므로 research 내부의 read_doc 은 지나지 않는다.
      tool_wrapper       권한 검사를 도구 함수 안(guarded_call)에 둔다. 누가 부르든 같은 검사를 지난다.
      tool_wrapper_min   tool_wrapper + Subagent 도구 최소화 + general-purpose 닫기 + 내장 파일 도구 권한 + Harness Profile 로 내장 도구 제거.
    """
    log = prepared.log
    guard = InjectionGuard(INJECTION_GUARD)     # 모든 구성에 같은 Input Guard 를 둔다 (변수는 권한 검사 위치뿐)
    table = make_table()
    ops = RawOps(prepared.ws, prepared.outbox, log)

    # 도구 래퍼. 있으면 모든 업무 도구 호출이 guarded_call → PermissionTable.decide 를 지나고,
    # ALLOW 일 때만 RawOps 의 실제 함수가 실행된다. 도구 객체 자체가 검사를 품으므로 Subagent 에 넘겨도 검사가 따라간다.
    wrapper = make_action_wrapper(table, log) if config in ("tool_wrapper", "tool_wrapper_min") else None
    tools = build_tools(ops, wrapper=wrapper)
    by_name = {t.name: t for t in tools}

    main_mw = [TraceMiddleware(log, "main"), InputGuardMiddleware(guard, log, scope=("user", "tool_result"), agent_name="main")]
    if config == "middleware_parent":
        # 같은 권한표를 middleware 로 건다. Main 이 직접 부르는 도구 호출만 본다.
        main_mw.append(ActionGuardMiddleware(table, log, agent_name="main"))

    if config == "tool_wrapper_min":
        research_tools = [by_name["list_projects"], by_name["read_doc"]]
        verifier_tools = [by_name["read_doc"]]
        subagents = [
            research_spec(research_tools, middleware=[TraceMiddleware(log, "research")], model=sub_model),
            verifier_spec(verifier_tools, middleware=[TraceMiddleware(log, "verifier")], model=sub_model),
            # 기본 general-purpose Subagent 는 부모 도구 전체를 물려받는다 (graph.py:848-851).
            # 같은 이름의 spec 을 주면 그것으로 대체된다. 도구를 비워 경로를 닫는다.
            {"name": "general-purpose", "description": "사용하지 않음", "system_prompt": "이 Agent 는 도구가 없다.",
             "tools": [], "middleware": [TraceMiddleware(log, "general-purpose")]},
        ]
    else:
        # tools 를 생략하면 부모 도구 전체를 상속한다 (graph.py:759). 여기서는 그 기본 동작을 그대로 둔다.
        subagents = [
            research_spec(tools, middleware=[TraceMiddleware(log, "research")], model=sub_model),
            verifier_spec(tools, middleware=[TraceMiddleware(log, "verifier")], model=sub_model),
        ]

    def assemble():
        return create_deep_agent(
            model=model,
            system_prompt=MAIN_PROMPT,
            tools=tools,
            context_schema=UserContext,                # 권한의 근거. 도구 안에서 runtime.context 로 읽는다
            backend=prepared.ws.backend(),
            # 내장 파일 도구(read_file/grep/glob)는 업무 도구 래퍼를 지나지 않는 별도 경로다.
            # scope_permissions 가 컨텍스트 밖 프로젝트 폴더의 read/write 를 거부한다 (거부 규칙이 앞, 첫 일치 적용).
            permissions=scope_permissions(prepared.ctx, prepared.ws) if config == "tool_wrapper_min" else None,
            subagents=subagents,
            middleware=[*main_mw, *budget_middleware()],
        )

    if config == "tool_wrapper_min":
        # Harness Profile: 내장 파일 도구·execute 를 모델의 도구 목록에서 아예 빼고 general-purpose Subagent 를 끈다.
        # 파일 권한이 "막는" 것이라면 이것은 "보여주지 않는" 것이다. 둘을 같이 둔다 (guardlab/harness.py).
        with minimal_tools(model):
            return assemble()
    return assemble()


def make_models():
    """(main model, subagent model). scripted 는 대본별로 Main 과 research 의 제안을 정한다."""
    if MODEL_MODE == "scripted":
        from guardlab import replay

        if SCRIPT == "delegate_read_beta":
            main = replay.ScriptedChatModel(steps=replay.script_main_delegates())
            sub = replay.ScriptedChatModel(steps=replay.script_research_reads_beta())
            return main, sub
        steps = {"read_beta": replay.script_read_beta, "send_outside": replay.script_send_outside}[SCRIPT]()
        return replay.ScriptedChatModel(steps=steps), None
    from guardlab.config import build_model

    return build_model(), None


if __name__ == "__main__":
    if MODEL_MODE == "live":
        announce(f"사례 {len(CASES)}건 × 구성 {len(CONFIGS)}개 × {REPEATS}회, 실행당 모델 호출 약 10~15회")
    rows = []
    case_ids = CASES if MODEL_MODE == "live" else [SCRIPT_CASE[SCRIPT]]
    for config in CONFIGS:
        for case_id in case_ids:
            case = get_case(case_id)
            for r in range(1, REPEATS + 1):
                prepared = prepare(case, "02", config, r)
                main_model, sub_model = make_models()
                agent = build_agent(prepared, main_model, config, sub_model)
                out = run_case(agent, prepared)
                row = evaluate(case, prepared.ctx, prepared.ws, prepared.outbox, prepared.log,
                               final_answer=out["final_answer"], error=out["error"], elapsed_s=out["elapsed_s"],
                               config={"name": config, "guard": INJECTION_GUARD, "model_mode": MODEL_MODE,
                                       "script": SCRIPT if MODEL_MODE == "scripted" else ""})
                rows.append(row)
                print_row(row)
    path = save_rows(rows, output_dir("02"))
    print(explain(rows, title="02 권한 검사 위치"))
    print(f"\n결과: {path}")
    print("읽을 것: doc_read_beta 에서 middleware_parent 의 '검사 없이 실행된 경로', intern_send 에서 어느 구성이 막았는가,")
    print("        INJECTION_GUARD='always_allow' 로 다시 돌렸을 때 tool_wrapper 의 결과가 유지되는가.")
