"""OpenRouter 연동 패키지를 init_chat_model로 불러온다."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel


def load_env() -> None:
    """Day04의 공통 .env를 읽는다. 셸 환경변수가 우선이다."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def chat_model(
    model: str | None = None,
    temperature: float = 0,
    *,
    timeout: float | None = None,
) -> BaseChatModel:
    """OpenRouter 모델을 만든다. 이 함수의 timeout 단위는 초다."""
    load_env()
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("Day04/.env에 OPENROUTER_API_KEY를 직접 설정하세요.")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout은 양수여야 합니다.")
    # langchain-openrouter의 timeout은 밀리초다. 실행기의 초 단위를 변환한다.
    timeout_ms = None if timeout is None else max(1, round(timeout * 1000))
    return init_chat_model(
        model=model or os.getenv("OPENROUTER_MODEL", "openai/gpt-5.6-luna"),
        model_provider="openrouter",
        temperature=temperature,
        timeout=timeout_ms,
    )
