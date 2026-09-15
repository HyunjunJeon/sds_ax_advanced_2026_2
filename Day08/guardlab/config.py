"""
사용: from guardlab.config import build_model, model_id, guard_status
포인트:
  1. 주모델은 OPENROUTER_MODEL 하나. 모든 비교군이 같은 값을 쓴다. 기본은 openai/gpt-5.6-luna (00 실측 76초/실행).
  2. gemini-3.8-flash 는 도구 루프에서 "Corrupted thought signature" 400 이 간헐적으로 났고, z-ai/glm-5.3-flash 는 안정적이지만
     실행당 5~8분(호출당 약 24초)으로 느려 기본에서 뺐다 (2026-09-15 실측).
  3. ChatOpenRouter 의 timeout 단위는 밀리초다 (Day05 와 같은 함정).
  4. guard_status() 는 값이 아니라 설정됨/미설정만 알려준다. 키 값은 어디에도 출력하지 않는다.

주요 내용:
환경·경로·주모델. 항상 Day08 안에서 실행한다.
"""


from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

DAY08 = Path(__file__).resolve().parents[1]
DATA = DAY08 / "guardlab" / "data"
OUTPUTS = DAY08 / "outputs"
WORK = DAY08 / "work"

DEFAULT_MODEL = "openai/gpt-5.6-luna"
POLICY_VERSION = "rules-v3"


def load_env() -> None:
    """Day08/.env 를 읽는다. 셸 환경변수가 우선한다. 값은 절대 출력하지 않는다."""
    load_dotenv(DAY08 / ".env", override=False)


def env(name: str, default: str = "") -> str:
    load_env()
    return os.environ.get(name, default) or default


def model_id() -> str:
    return env("OPENROUTER_MODEL", DEFAULT_MODEL)


def build_model(*, temperature: float = 0.0, timeout_s: float = 120.0):
    """Day05 와 같은 langchain-openrouter 클라이언트. timeout 단위는 밀리초다."""
    from langchain_openrouter import ChatOpenRouter

    key = env("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY 가 없습니다. .env.example 을 .env 로 복사해 직접 채우세요.")
    kwargs = {
        "model": model_id(),
        "api_key": key,
        "temperature": temperature,
        "max_retries": 2,
        "timeout": max(1, round(timeout_s * 1000)),
    }
    base_url = env("OPENROUTER_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenRouter(**kwargs)


def guard_status() -> dict[str, str]:
    """설정됨/미설정만 알려준다. 값은 노출하지 않는다."""
    return {
        "OPENROUTER_API_KEY": "설정됨" if env("OPENROUTER_API_KEY") else "미설정",
        "OPENROUTER_MODEL": model_id(),
        "GUARD_INJECTION_URL": "설정됨" if env("GUARD_INJECTION_URL") else "미설정",
        "GUARD_INJECTION_KIND": env("GUARD_INJECTION_KIND", "sguard"),
        "GUARD_PII_MODEL": env("GUARD_PII_MODEL", "LiquidAI/LFM2.5-Encoder-350M-PII-Detector"),
        "GUARD_PII_REVISION": "고정됨" if env("GUARD_PII_REVISION") else "미고정(main)",
    }
