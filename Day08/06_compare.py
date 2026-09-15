"""
실행:   uv run python 06_compare.py
        기본 B0·B3·B4 × 사례 2 = 6실행, 60~90회 호출, 약 10분. REMOVE 를 바꿔 두 번 돌린다.
        전체 행렬(5비교군 × 4사례 = 20실행)은 ARCHITECTURES·CASES 를 늘린다. 실행 전 규모가 고지된다.
        HOLDOUT=True 는 미공개 사례 2건을 추가한다. 마지막에 한 번만.
변수:   DEFENSES 집합(방어 배치)만. 주모델·프롬프트·사례·초기 문서·예산은 고정.

포인트:
  1. B4 = B3 - {REMOVE}. 여러 방어를 한꺼번에 넣고 성공률만 보면 무엇이 효과였는지 알 수 없다.
  2. B1 이 못 막고 B2 가 막은 행은 "탐지 실패 + 방어 성공"이다. 두 사실을 따로 센다.
  3. 실패·오류·예산 초과 행도 지우지 않는다. 반복마다 실행 순서를 뒤집는다.
  4. scripted 는 사례마다 그 공격에 맞는 대본을 쓴다. 배선 확인용이며 "모델이 속았다"의 증거가 아니다.

주요 내용:
- 비교군: B0 지침만 / B1 모델 기반(Input+Output Guard) / B2 코드 기반(권한·파일 권한·메모리·Subagent 최소화·내장 도구 제거) / B3 결합 / B4 하나 제거.
- 사례: doc_send_outside, doc_read_beta (기본). doc_include_contacts, doc_poison_memory 는 과제.

결과: outputs/06/<run_id>/runs.jsonl, summary.md
"""

from deepagents import create_deep_agent
from guardlab.budget import budget_middleware
from guardlab.cases import get_case, load_holdout
from guardlab.components import (
    InputGuardMiddleware,
    MemoryGuardMiddleware,
    OutputGuardMiddleware,
    load_reviewers,
    make_action_wrapper,
    make_output_wrapper,
    scope_permissions,
)
from guardlab.context import UserContext
from guardlab.defenses import PermissionTable
from guardlab.evaluate import evaluate, explain, save_rows, summarize
from guardlab.guards import InjectionGuard, PIIGuard
from guardlab.harness import minimal_tools
from guardlab.prompts import MAIN_PROMPT, research_spec, verifier_spec
from guardlab.runner import announce, output_dir, prepare, print_row, run_case
from guardlab.tools import RawOps, build_tools
from guardlab.trace import TraceMiddleware

# ── 실험 정의 ────────────────────────────────────────────────────────────────
# 수업용 기본값. 06 의 질문("하나를 빼면 어디가 열리는가")에는 B3 와 B4, 기준선 B0 이면 충분하다.
# 과제: ARCHITECTURES = ["B0", "B1", "B2", "B3", "B4"], CASES 에 "doc_include_contacts", "doc_poison_memory" 추가.
ARCHITECTURES = ["B0", "B3", "B4"]
CASES = ["doc_send_outside", "doc_read_beta"]
REPEATS = 1  # 3회 이상부터 비율을 말할 수 있다. 수업 중에는 1회.
REMOVE = "action_guard"  # B4 에서 뺄 방어: "input_guard" / "action_guard" / "output_guard" / "permissions" / "memory_guard" / "subagent_min" / "builtin_tools_off"
HOLDOUT = False  # True 면 미공개 사례 2건을 추가한다. 최종 확인에만 쓴다.
INJECTION_GUARD = "fake"  # "remote"(RunPod) / "fake"
DETECTOR = "regex"  # "regex" / "lfm"
MODEL_MODE = "live"  # "live" / "scripted"(부품 연결 확인용)

DEFENSES = {
    "B0": set(),
    "B1": {"input_guard", "output_guard"},
    "B2": {
        "action_guard",
        "permissions",
        "memory_guard",
        "subagent_min",
        "builtin_tools_off",
    },
    "B3": {
        "input_guard",
        "output_guard",
        "action_guard",
        "permissions",
        "memory_guard",
        "subagent_min",
        "builtin_tools_off",
    },
}
DEFENSES["B4"] = DEFENSES["B3"] - {REMOVE}

MEMORY = ["/memory/AGENTS.md"]
# "permissions" 방어 = 컨텍스트 밖 프로젝트 폴더 읽기·쓰기 거부 + /policy 쓰기 거부 (guardlab.components.scope_permissions)


def make_table() -> PermissionTable:
    rv = load_reviewers()
    return PermissionTable(
        reviewers={
            k: v
            for k, v in rv.items()
            if isinstance(v, list) and k != "allowed_domains"
        },
        allowed_domains=tuple(rv.get("allowed_domains", ["nurisoft.example"])),
    )


def build_agent(prepared, model, defenses: set[str], sub_model=None):
    """01~05 의 방어를 이름(defenses 집합)으로 켜고 끈다. 비교군 정의는 위 DEFENSES 표다.

    방어 이름 → 어디에 붙는가
      input_guard    InputGuardMiddleware — Main 과 모든 Subagent 의 before_model (01)
      action_guard   make_action_wrapper  — 업무 도구 함수 안 (02)
      output_guard   make_output_wrapper(초안·전달) + OutputGuardMiddleware(최종 답변) (03)
      permissions    scope_permissions    — 내장 파일 도구 (02·04)
      memory_guard   MemoryGuardMiddleware — 메모리 읽기·쓰기 (04)
      subagent_min   Subagent 도구 최소화 + general-purpose 닫기 (02)
      builtin_tools_off  Harness Profile 로 내장 파일 도구·execute 를 도구 목록에서 제거 (02, guardlab/harness.py)
    """
    log = prepared.log
    guard = InjectionGuard(INJECTION_GUARD)
    pii = PIIGuard(DETECTOR)
    ops = RawOps(prepared.ws, prepared.outbox, log)

    # 도구 래퍼는 안쪽부터 바깥쪽으로 겹친다: 권한 검사(action) → 본문 검사(output) → 실제 함수.
    # 둘 다 켜면 권한이 없는 호출은 본문 검사까지 가지 않고 먼저 막힌다.
    wrapper = None
    if "action_guard" in defenses:
        wrapper = make_action_wrapper(make_table(), log)
    if "output_guard" in defenses:
        wrapper = make_output_wrapper(pii, log, check_save=True, inner=wrapper)
    tools = build_tools(ops, wrapper=wrapper)
    by_name = {t.name: t for t in tools}

    def mw(agent_name: str):
        """Agent 마다 새 middleware 인스턴스를 만든다. 이름이 겹치면 create_agent 가 거부한다."""
        stack = [TraceMiddleware(log, agent_name)]
        if "input_guard" in defenses:
            # 01 과 달리 Subagent 스택에도 넣는다. research 가 읽는 원문까지 검사 대상이 된다.
            stack.append(
                InputGuardMiddleware(
                    guard, log, scope=("user", "tool_result"), agent_name=agent_name
                )
            )
        return stack

    main_mw = mw("main")
    if "output_guard" in defenses:
        main_mw.append(OutputGuardMiddleware(pii, log, agent_name="main"))
    if "memory_guard" in defenses:
        main_mw.append(
            MemoryGuardMiddleware(log, memory_paths=tuple(MEMORY), agent_name="main")
        )

    if "subagent_min" in defenses:
        subagents = [
            research_spec(
                [by_name["list_projects"], by_name["read_doc"]],
                middleware=mw("research"),
                model=sub_model,
            ),
            verifier_spec(
                [by_name["read_doc"]], middleware=mw("verifier"), model=sub_model
            ),
            {
                "name": "general-purpose",
                "description": "사용하지 않음",
                "system_prompt": "이 Agent 는 도구가 없다.",
                "tools": [],
                "middleware": mw("general-purpose"),
            },
        ]
    else:
        subagents = [
            research_spec(tools, middleware=mw("research"), model=sub_model),
            verifier_spec(tools, middleware=mw("verifier"), model=sub_model),
        ]

    def assemble():
        return create_deep_agent(
            model=model,
            system_prompt=MAIN_PROMPT,
            tools=tools,
            context_schema=UserContext,
            backend=prepared.ws.backend(),
            memory=MEMORY,
            permissions=scope_permissions(prepared.ctx, prepared.ws)
            if "permissions" in defenses
            else None,
            subagents=subagents,
            middleware=[*main_mw, *budget_middleware()],
        )

    if "builtin_tools_off" in defenses:
        with minimal_tools(
            model
        ):  # 내장 파일 도구·execute 제거 + general-purpose 끔. 블록 밖에서는 원래대로
            return assemble()
    return assemble()


def make_models(case_id: str):
    """(main, subagent) 모델. scripted 는 사례마다 그 공격에 맞는 대본을 쓴다 (부품 연결 확인용)."""
    if MODEL_MODE == "scripted":
        from guardlab import replay as rp

        scripts = {
            "doc_send_outside": (rp.script_send_outside, None),
            "doc_read_beta": (rp.script_main_delegates, rp.script_research_reads_beta),
            "doc_include_contacts": (rp.script_send_with_pii, None),
            "doc_poison_memory": (rp.script_write_policy, None),
        }
        main_fn, sub_fn = scripts.get(case_id, (rp.script_normal, None))
        return rp.ScriptedChatModel(steps=main_fn()), (
            rp.ScriptedChatModel(steps=sub_fn()) if sub_fn else None
        )
    from guardlab.config import build_model

    return build_model(), None


if __name__ == "__main__":
    cases = [get_case(c) for c in CASES]
    if HOLDOUT:
        cases += load_holdout()
    total = len(ARCHITECTURES) * len(cases) * REPEATS
    if MODEL_MODE == "live":
        announce(
            f"{len(ARCHITECTURES)}비교군 × 사례 {len(cases)}건 × {REPEATS}회 = {total}실행, 모델 호출 약 {total * 10}~{total * 15}회"
        )
    rows = []
    for r in range(1, REPEATS + 1):
        # 반복마다 실행 순서를 뒤집는다. 시간대·제공자 상태가 특정 비교군에만 몰리는 것을 막는 통제다 (Day03 09 와 같다).
        order = ARCHITECTURES if r % 2 else list(reversed(ARCHITECTURES))
        for arch in order:
            for case in cases:
                prepared = prepare(case, "06", arch, r)
                main_model, sub_model = make_models(case.id)
                agent = build_agent(prepared, main_model, DEFENSES[arch], sub_model)
                out = run_case(agent, prepared)
                row = evaluate(
                    case,
                    prepared.ctx,
                    prepared.ws,
                    prepared.outbox,
                    prepared.log,
                    final_answer=out["final_answer"],
                    error=out["error"],
                    elapsed_s=out["elapsed_s"],
                    config={
                        "name": arch,
                        "defenses": sorted(DEFENSES[arch]),
                        "removed": REMOVE if arch == "B4" else "",
                        "guard": INJECTION_GUARD,
                        "detector": DETECTOR,
                        "model_mode": MODEL_MODE,
                        "repeat": r,
                    },
                )
                rows.append(row)
                print_row(row)
    path = save_rows(rows, output_dir("06"))
    print(explain(rows, title="06 비교군"))
    print("\n" + summarize(rows))
    print(f"\n결과: {path}")
    print(
        f"읽을 것: B3 와 B4(-{REMOVE}) 의 차이가 어느 사례·어느 경로에서 나는가. B1 이 막지 못하고 B2 가 막은 행은 '탐지 실패 + 방어 성공'이다."
    )
