"""
실행:   cd Day08 && uv run python 00_baseline_agent.py
        상수 MODEL_MODE="scripted" 로 바꾸면 모델 없이 대본이 돈다 (약 1초). "live" 는 OpenRouter 주모델(10~15회 호출).
읽는 곳: build_agent() 의 create_deep_agent(...) 인자 하나하나. 이후 파일들은 이 조립에서 한 줄씩만 바꾼다.

포인트:
  1. B0 의 방어는 MAIN_PROMPT 의 "하지 마라" 문장뿐이다. 도구는 RawOps 원시 구현이라 무엇이든 실행된다.
  2. 평가는 Agent 의 답변이 아니라 상태(전송함 outbox.jsonl, /drafts, 실행 기록)로 한다 → guardlab/evaluate.py
  3. TraceMiddleware 를 Main·research·verifier 에 각각 넣는 이유: Subagent 는 부모 middleware 를 상속하지 않는다.
  4. 결과 위치: outputs/00/<run_id>/runs.jsonl·summary.md, 작업 공간: work/00/<case>__<config>__r1/


주요 내용:
- 이 실습의 공통 시스템: Main Agent + research/verifier Subagent + 업무 도구 4개 + 로컬 전송함.
- 평가는 Agent 의 "전달했습니다"가 아니라 outbox.jsonl·/drafts·실행 기록으로 한다.
- B0 의 방어는 시스템 프롬프트의 안전 지침뿐이다 (guardlab/prompts.py). 이후 파일은 여기에 방어를 하나씩 붙인다.

실행 조건은 아래 상수다. CLI 플래그는 없다.
    uv run python 00_baseline_agent.py
결과: outputs/00/<run_id>/runs.jsonl, summary.md. 
작업 공간: work/00/<case>__<config>__r1/
"""

from deepagents import create_deep_agent

from guardlab.budget import budget_middleware
from guardlab.cases import get_case
from guardlab.config import guard_status
from guardlab.context import UserContext
from guardlab.evaluate import evaluate, explain, save_rows
from guardlab.prompts import MAIN_PROMPT, research_spec, verifier_spec
from guardlab.runner import announce, output_dir, prepare, print_row, run_case
from guardlab.tools import RawOps, build_tools
from guardlab.trace import TraceMiddleware

# ── 실행 조건 ────────────────────────────────────────────────────────────────
CASE = "normal"          # cases.jsonl 의 id. 00 은 정상 사례만 돌린다.
MODEL_MODE = "live"      # "live": OpenRouter 주모델 / "scripted": 모델 없이 대본 재생 (guardlab/replay.py)
REPEATS = 1


def build_agent(prepared, model):
    """B0. 방어는 프롬프트뿐이다. 도구는 원시 구현을 그대로 노출한다.

    `prepared` 는 runner.prepare() 가 만든 묶음이다:
      prepared.ws      이 실행만의 작업 공간 (data/base 복사본 + 사례별 공격 문서)
      prepared.outbox  로컬 전송함 (send_report 가 여기에 기록한다)
      prepared.log     실행 기록 (모델 호출 수, 도구 호출, 정책 결정). 부모·Subagent 가 같은 객체를 쓴다
      prepared.ctx     신뢰된 사용자 컨텍스트 (누가, 어느 프로젝트에, 전달 권한이 있는가)
    """
    # 1) 업무 도구. RawOps 는 실제 상태를 바꾸는 함수들이고 권한 검사가 없다.
    #    build_tools(ops) 에 wrapper 를 주지 않았으므로, 모델이 제안한 호출은 그대로 실행된다. 이것이 B0 의 정의다.
    ops = RawOps(prepared.ws, prepared.outbox, prepared.log)
    tools = build_tools(ops)                       # wrapper 없음 = 권한 검사 없음

    # 2) Subagent 별 도구 목록. 조사(research)는 읽기만, 검증(verifier)은 read_doc 만 준다.
    #    "역할을 나눴다"는 것과 "권한을 나눴다"는 것은 다르다. 여기서는 도구 목록만 다르고 검사는 없다.
    research_tools = [t for t in tools if t.name in ("list_projects", "read_doc")]
    verifier_tools = [t for t in tools if t.name in ("read_doc",)]

    return create_deep_agent(
        model=model,                               # 주모델. 모든 비교군에서 같은 값 (OPENROUTER_MODEL)
        system_prompt=MAIN_PROMPT,                 # 안전 지침이 들어 있는 프롬프트. B0 의 유일한 방어
        tools=tools,                               # Main Agent 가 직접 부를 수 있는 업무 도구 4개
        context_schema=UserContext,                # invoke(..., context=ctx) 로 넣는 신뢰 컨텍스트의 타입
        backend=prepared.ws.backend(),             # 내장 파일 도구(read_file 등)가 보는 루트 = 작업 공간
        subagents=[
            # 각 Subagent 는 별도의 create_agent 그래프다. 부모의 middleware 를 물려받지 않으므로
            # 기록기(TraceMiddleware)도 Subagent 마다 따로 넣어야 그 안의 도구 호출이 보인다.
            research_spec(research_tools, middleware=[TraceMiddleware(prepared.log, "research")]),
            verifier_spec(verifier_tools, middleware=[TraceMiddleware(prepared.log, "verifier")]),
        ],
        # Main 의 middleware: 기록기 + 실행 예산(모델 호출·도구 호출 상한). 예산은 모든 파일에서 같다.
        middleware=[TraceMiddleware(prepared.log, "main"), *budget_middleware()],
    )


def make_model():
    """MODEL_MODE 에 따라 실제 모델 또는 대본 모델. 대본 모델은 정해진 순서로 도구 호출을 '제안'만 한다."""
    if MODEL_MODE == "scripted":
        from guardlab.replay import ScriptedChatModel, script_normal

        return ScriptedChatModel(steps=script_normal())
    from guardlab.config import build_model

    return build_model()


if __name__ == "__main__":
    print("환경:", guard_status())
    case = get_case(CASE)
    rows = []
    if MODEL_MODE == "live":
        announce(f"사례 {REPEATS}건, 실행당 모델 호출 약 10~15회 (Subagent 포함)")
    for r in range(1, REPEATS + 1):
        # 실행 절차는 모든 번호 파일이 같다: 준비 → 조립 → 실행 → 평가 → 기록
        prepared = prepare(case, "00", f"B0-{MODEL_MODE}", r)   # work/00/<case>__<config>__r<n>/ 에 작업 공간 생성
        agent = build_agent(prepared, make_model())
        out = run_case(agent, prepared)                          # 예외도 out["error"] 로 받는다. 실패 행도 남긴다
        row = evaluate(case, prepared.ctx, prepared.ws, prepared.outbox, prepared.log,   # 답변이 아니라 상태를 읽는다
                       final_answer=out["final_answer"], error=out["error"], elapsed_s=out["elapsed_s"],
                       config={"name": f"B0-{MODEL_MODE}", "model_mode": MODEL_MODE})
        rows.append(row)
        print_row(row)
        print("  전송함:", row["outbox"], "| 초안:", row["drafts"])
    path = save_rows(rows, output_dir("00"))
    print(explain(rows, title="00 기준선"))
    print(f"\n결과: {path}\n평가기가 읽은 것: outbox.jsonl, /drafts, 실행 기록(log). Agent 의 답변은 참고일 뿐이다.")
