"""
사용: middleware=[..., *budget_middleware()]
포인트: 모델 호출 24회·도구 호출 40회 상한을 모든 비교군에 같게 건다. exit_behavior="end" 라 초과 시 오류 대신 종료한다.
        상한을 올려 실패를 감추지 않는다 (루트 AGENTS.md 5장).

주요 내용:
모든 비교군에 같은 값으로 거는 실행 예산. 실패를 감추려고 올리지 않는다.
"""


from __future__ import annotations

from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

MODEL_CALLS = 24  # Agent 하나의 한 실행(run)에서 허용하는 모델 호출 수
TOOL_CALLS = 40   # Agent 하나의 한 실행에서 허용하는 도구 호출 수


def budget_middleware(model_calls: int = MODEL_CALLS, tool_calls: int = TOOL_CALLS) -> list:
    return [
        ModelCallLimitMiddleware(run_limit=model_calls, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=tool_calls, exit_behavior="end"),
    ]
