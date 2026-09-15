"""
실행:   uv run python 03_output_guard.py
        DETECTOR="regex"(기본) / "lfm"(LiquidAI PII-Detector, 첫 실행에 1.3GB 다운로드, 로컬 CPU)
        MODEL_MODE="scripted" 대본: 개인정보가 든 본문을 승인 검토자에게 저장·전달 제안. "live" 는 12실행.
변수:   CONFIGS = 어느 공개 경로를 검사하는가. builtin_regex_middleware / final_only / before_send / all_boundaries.

포인트:
  1. 공개 경로는 셋이다: 최종 답변, send_report 의 body, save_report 의 content. 답변만 검사하면 나머지 둘로 샌다.
  2. 수신자가 승인된 검토자여도 내용은 따로 본다. 02 의 수신자 허용 목록으로는 이 사례가 막히지 않는다.
  3. after_agent(최종 답변 검사)는 이미 실행된 전달을 되돌리지 못한다. 실행 직전 검사(도구 래퍼)가 필요하다.
  4. 탐지기는 구간(Span)만 준다. 병합·마스킹·공개 대상별 허용은 guardlab/defenses.py 의 코드가 한다.
  5. check_before_send 는 몰래 고쳐 보내지 않고 BLOCK 으로 돌려보낸다. 모델이 다시 쓰고 재검증한다.

주요 내용:
- 질문: PII 탐지기가 개인정보를 찾아냈다는 사실과 실제 공개를 막았다는 사실 사이에 어떤 코드가 필요한가.
- 비스트리밍으로 고정한다. 스트리밍 중 이미 공개된 내용을 나중에 막는 문제는 심화 대상이다.
- 사례: doc_include_contacts(문서 지시), user_asks_contacts(사용자 요청), normal(과잉 삭제 확인).

결과: outputs/03/<run_id>/runs.jsonl, summary.md
단위 검증: uv run pytest tests/test_defenses_03.py -q
"""

from deepagents import create_deep_agent
from guardlab.budget import budget_middleware
from guardlab.cases import get_case
from guardlab.components import OutputGuardMiddleware, make_output_wrapper
from guardlab.context import UserContext
from guardlab.evaluate import evaluate, explain, save_rows
from guardlab.guards import PIIGuard
from guardlab.prompts import MAIN_PROMPT, research_spec, verifier_spec
from guardlab.runner import announce, output_dir, prepare, print_row, run_case
from guardlab.tools import RawOps, build_tools
from guardlab.trace import TraceMiddleware
from langchain.agents.middleware import PIIMiddleware

# ── 실행 조건 ────────────────────────────────────────────────────────────────
CASES = ["doc_include_contacts", "user_asks_contacts", "normal"]
CONFIGS = ["builtin_regex_middleware", "final_only", "before_send", "all_boundaries"]
DETECTOR = "regex"  # "regex" / "lfm"
MODEL_MODE = (
    "live"  # "live" / "scripted" (대본: 개인정보가 든 본문을 승인 검토자에게 전달 제안)
)
REPEATS = 1
SCRIPT_CASE = "doc_include_contacts"  # scripted 모드에서는 대본이 행동을 정하므로 이 사례만 돌린다


def build_agent(prepared, model, config: str):
    """네 구성은 '어느 공개 경로를 검사하는가'만 다르다. 공개 경로는 세 개다:
    최종 답변(사용자에게), 전달 본문(send_report 의 body), 초안 파일(save_report 의 content).

    config:
      builtin_regex_middleware  LangChain 내장 PIIMiddleware. 최종 답변만 가린다.
      final_only                OutputGuardMiddleware(after_agent). 최종 답변만 검사.
      before_send               send_report 직전에 body 검사. 초안은 검사하지 않는다.
      all_boundaries            초안·전달·최종 답변 모두 검사.
    """
    log = prepared.log
    pii = PIIGuard(
        DETECTOR
    )  # 구간(Span)만 준다. 마스킹·허용 판단은 defenses 의 코드가 한다
    ops = RawOps(prepared.ws, prepared.outbox, log)

    # 도구 래퍼: send_report(그리고 check_save=True 면 save_report)의 인자를 실행 직전에 검사한다.
    # 공개 불가 라벨이 있으면 BLOCK 문자열을 돌려주고 실제 함수는 실행되지 않는다. 몰래 고쳐 보내지 않는다.
    wrapper = None
    if config == "before_send":
        wrapper = make_output_wrapper(pii, log, check_save=False)
    elif config == "all_boundaries":
        wrapper = make_output_wrapper(pii, log, check_save=True)
    tools = build_tools(ops, wrapper=wrapper)
    by_name = {t.name: t for t in tools}

    main_mw = [TraceMiddleware(log, "main")]
    if config == "builtin_regex_middleware":
        # 내장 부품. apply_to_output=True 는 최종 AIMessage 본문만 가린다. send_report 의 body 인자는 검사하지 않는다.
        main_mw += [
            PIIMiddleware(
                "email", strategy="redact", apply_to_input=False, apply_to_output=True
            ),
            PIIMiddleware(
                "kr_phone",
                detector=r"01[016789]-?\d{3,4}-?\d{4}",
                strategy="redact",
                apply_to_input=False,
                apply_to_output=True,
            ),
        ]
    elif config in ("final_only", "all_boundaries"):
        # after_agent 훅: 실행이 끝난 뒤 마지막 AIMessage 를 검사해 마스킹한다.
        # 이 시점에는 send_report 가 이미 실행된 뒤라, 전송함에 나간 본문은 되돌릴 수 없다 (슬라이드 18·20).
        main_mw.append(OutputGuardMiddleware(pii, log, agent_name="main"))

    return create_deep_agent(
        model=model,
        system_prompt=MAIN_PROMPT,
        tools=tools,
        context_schema=UserContext,
        backend=prepared.ws.backend(),
        subagents=[
            research_spec(
                [by_name["list_projects"], by_name["read_doc"]],
                middleware=[TraceMiddleware(log, "research")],
            ),
            verifier_spec(
                [by_name["read_doc"]], middleware=[TraceMiddleware(log, "verifier")]
            ),
        ],
        middleware=[*main_mw, *budget_middleware()],
    )


def make_model():
    if MODEL_MODE == "scripted":
        from guardlab.replay import ScriptedChatModel, script_send_with_pii

        return ScriptedChatModel(steps=script_send_with_pii())
    from guardlab.config import build_model

    return build_model()


if __name__ == "__main__":
    if MODEL_MODE == "live":
        announce(
            f"사례 {len(CASES)}건 × 구성 {len(CONFIGS)}개 × {REPEATS}회, 실행당 모델 호출 약 10~15회",
            "DETECTOR='lfm' 이면 첫 실행에 모델 다운로드(약 1.4GB)가 있다.",
        )
    rows = []
    case_ids = CASES if MODEL_MODE == "live" else [SCRIPT_CASE]
    for config in CONFIGS:
        for case_id in case_ids:
            case = get_case(case_id)
            for r in range(1, REPEATS + 1):
                prepared = prepare(case, "03", config, r)
                agent = build_agent(prepared, make_model(), config)
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
                        "name": config,
                        "detector": DETECTOR,
                        "model_mode": MODEL_MODE,
                    },
                )
                rows.append(row)
                print_row(row)
    path = save_rows(rows, output_dir("03"))
    print(explain(rows, title="03 공개 경계"))
    print(f"\n결과: {path}")
    print(
        "읽을 것: '유출' 열이 어느 구성에서 비는가. final_only 에서 답변은 깨끗한데 전송함에 개인정보가 남는 행을 찾아라."
    )
    print("        normal 사례에서 필요한 이름까지 지워지지 않았는가(과잉 삭제).")
