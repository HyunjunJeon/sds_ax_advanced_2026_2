"""DeepEval 용 OpenRouter 모델 어댑터.

deepeval 기본 ``OpenAIModel`` 은 내장 모델 테이블(46개 OpenAI 모델)에 없는 이름을
받으면 단가 조회 단계에서 깨진다. 이 프로젝트는 ``langchain-openrouter`` 경유라
모델명이 ``openai/gpt-5.6-luna`` 형태이므로 커스텀 LLM 으로 붙인다.

deepeval 은 커스텀 모델(``is_native_model()`` 이 False)에 대해
``model.generate(prompt, schema=...)`` 를 호출하고 **스키마 인스턴스를 그대로**
돌려받는다 (native 모델처럼 ``(res, cost)`` 튜플이 아니다).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from deepeval.models.base_model import DeepEvalBaseLLM

__all__ = [
    "OpenRouterModel",
    "load_openrouter_env",
    "JudgeConfig",
    "resolve_judge",
    "load_judge",
    "load_agent_llm",
    "DEFAULT_JUDGE_MODEL",
    "timeout_ms",
]

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def timeout_ms(timeout_s: float | None) -> int | None:
    """초 단위 timeout 을 ``langchain-openrouter`` 가 받는 밀리초로 바꾼다.

    ``ChatOpenRouter`` 의 timeout 단위는 밀리초다
    (``Day04/build_mcp/llm.py:30``, ``Day04/README.md:60``).
    ``ChatOpenAI(timeout=180)`` 값을 그대로 넘기면 180ms 로 끊긴다.
    """
    if timeout_s is None:
        return None
    if timeout_s <= 0:
        raise ValueError("timeout은 양수여야 합니다.")
    return max(1, round(timeout_s * 1000))


def _chat_openrouter(
    *,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float = 0.0,
    max_retries: int = 3,
    timeout: float | None = None,
):
    """Day04 와 같은 ``langchain-openrouter`` 클라이언트를 만든다.

    ``init_chat_model(..., model_provider="openrouter")`` 와 동일 패키지다.
    판사만 다른 키/엔드포인트를 쓸 수 있게 값을 명시적으로 넘긴다.
    """
    from langchain_openrouter import ChatOpenRouter

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "max_retries": max_retries,
    }
    if base_url:
        kwargs["base_url"] = base_url
    converted = timeout_ms(timeout)
    if converted is not None:
        kwargs["timeout"] = converted
    return ChatOpenRouter(**kwargs)

# 판사 기본값. 에이전트 모델(OPENROUTER_MODEL)과 일부러 다른 계열로 둔다.
# 실측으로 확인한 이유 두 가지:
#   · google/gemini-3.8-flash 를 판사로 쓰면 StepEfficiency 등 중첩 스키마에서
#     유효하지 않은 JSON 을 뱉어 평가가 통째로 중단된다.
#   · 같은 모델이 자기 출력을 채점하면 자기 선호 편향이 생긴다.
DEFAULT_JUDGE_MODEL = "anthropic/claude-haiku-4.5"


DAY05_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = DAY05_DIR / ".env"


def _env_lookup(env_path: str | Path | None):
    """셸 환경변수 → ``.env`` 파일 순으로 찾는 조회 함수를 돌려준다.

    이 저장소 관례(Day03 ``.env.example``)대로 **셸 환경변수가 .env 보다 우선**한다.
    ``env_path`` 를 주지 않으면 ``Day05/.env`` 를 읽는다.
    """
    from dotenv import dotenv_values

    path = Path(env_path) if env_path else DEFAULT_ENV_PATH
    file_values = (
        {k: v for k, v in dotenv_values(path).items() if v} if path.exists() else {}
    )

    def lookup(*keys: str) -> Optional[str]:
        for key in keys:
            value = os.environ.get(key) or file_values.get(key)
            if value:
                return value
        return None

    return lookup


def load_openrouter_env(env_path: str | Path | None = None) -> dict[str, str]:
    """에이전트용 OpenRouter 설정을 읽는다.

    ``OPENROUTER_MODEL`` / ``OPENROUTER_BASE_URL`` 이 있으면 각각
    ``OPENAI_MODEL`` / ``OPENAI_BASE_URL`` 보다 우선한다 (Day03 과 같은 규칙).

    Returns:
        ``api_key`` / ``base_url`` / ``model`` 키를 가진 dict.
    """
    lookup = _env_lookup(env_path)
    api_key = lookup("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY 가 설정되지 않았습니다. Day05/.env.example 을 "
            "Day05/.env 로 복사한 뒤 직접 채워 주세요."
        )
    return {
        "api_key": api_key,
        "base_url": lookup("OPENROUTER_BASE_URL", "OPENAI_BASE_URL") or DEFAULT_BASE_URL,
        "model": lookup("OPENROUTER_MODEL", "OPENAI_MODEL") or "google/gemini-3.8-flash",
    }


@dataclass(frozen=True)
class JudgeConfig:
    """판사 모델 설정. 에이전트 모델과 독립적으로 해석된다."""

    model: str
    api_key: str
    base_url: str
    source: str  # 어디서 정해졌는지 (코드 설정 / 환경변수 / 기본값) — 로그·하이퍼파라미터용

    def as_hyperparameters(self) -> dict[str, str]:
        """평가 실행에 남길 값. 판사가 바뀌면 점수 비교가 무의미해지므로 반드시 기록한다."""
        return {"judge_model": self.model, "judge_base_url": self.base_url}


def resolve_judge(
    env_path: str | Path | None = None,
    *,
    model: Optional[str] = None,
) -> JudgeConfig:
    """판사 설정을 정한다. 우선순위는 **스크립트 상단 설정 > 환경변수 > 기본값**.

    환경변수 (셸 → ``Day05/.env`` 순으로 찾는다)
        DEEPEVAL_JUDGE_MODEL     판사 모델명. 없으면 :data:`DEFAULT_JUDGE_MODEL`
        DEEPEVAL_JUDGE_BASE_URL  판사만 다른 엔드포인트로 보낼 때
        DEEPEVAL_JUDGE_API_KEY   판사만 다른 키를 쓸 때

    판사 전용 키/엔드포인트가 없으면 에이전트와 같은 OpenRouter 설정을 쓴다.
    """
    lookup = _env_lookup(env_path)

    if model:
        chosen, source = model, "코드 설정"
    elif lookup("DEEPEVAL_JUDGE_MODEL"):
        chosen, source = lookup("DEEPEVAL_JUDGE_MODEL"), "환경변수"
    else:
        chosen, source = DEFAULT_JUDGE_MODEL, "기본값"

    base = load_openrouter_env(env_path)
    return JudgeConfig(
        model=chosen,
        api_key=lookup("DEEPEVAL_JUDGE_API_KEY") or base["api_key"],
        base_url=lookup("DEEPEVAL_JUDGE_BASE_URL") or base["base_url"],
        source=source,
    )


def load_judge(
    env_path: str | Path | None = None,
    *,
    model: Optional[str] = None,
    agent_model: Optional[str] = None,
    verbose: bool = True,
) -> "OpenRouterModel":
    """판사 모델 인스턴스를 만든다.

    Args:
        agent_model: 넘기면 판사와 같은 모델인지 검사해 경고한다.
    """
    cfg = resolve_judge(env_path, model=model)
    if verbose:
        print(f"judge = {cfg.model}  (정한 곳: {cfg.source})")
    if agent_model and agent_model == cfg.model:
        print(
            f"  ⚠ 판사와 에이전트가 같은 모델({cfg.model})입니다. 자기 출력을 자기가 "
            "채점하면 점수가 후해집니다. 스크립트의 JUDGE_MODEL 또는 DEEPEVAL_JUDGE_MODEL 로 "
            "분리하세요."
        )
    judge = OpenRouterModel(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)
    judge.config = cfg  # 호출자가 하이퍼파라미터로 남길 수 있게 붙여 둔다
    return judge


def load_agent_llm(
    env_path: str | Path | None = None,
    *,
    model: Optional[str] = None,
    temperature: float = 0.0,
    timeout: float | None = None,
):
    """에이전트 본체가 쓰는 LangChain 채팅 모델.

    모델명은 ``OPENROUTER_MODEL`` → ``OPENAI_MODEL`` 순으로 읽는다
    (``load_openrouter_env``). timeout 인자는 **초**다.
    """
    cfg = load_openrouter_env(env_path)
    return _chat_openrouter(
        model=model or cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=temperature,
        timeout=timeout,
    )


class OpenRouterModel(DeepEvalBaseLLM):
    """OpenRouter(또는 모든 OpenAI 호환 엔드포인트)를 DeepEval 에 연결한다."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.0,
        max_retries: int = 3,
        timeout: float = 180.0,
        structured_method: str = "function_calling",
    ):
        # load_model() 이 base __init__ 안에서 호출되므로 그 전에 세팅해야 한다.
        self.model_name = model
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        # OpenRouter 를 거치면 provider 마다 구조화 출력 지원이 제각각이다.
        # json_schema 방식은 중첩 스키마에서 finish_reason=error 로 조용히 깨지는
        # 경우가 있어 function_calling 을 기본값으로 둔다.
        self.structured_method = structured_method
        super().__init__(model)

    def load_model(self):
        return _chat_openrouter(
            model=self.model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_retries=self.max_retries,
            timeout=self.timeout,
        )

    def _structured(self, schema: Any):
        """구조화 출력 러너블. ``parallel_tool_calls=False`` 를 반드시 명시한다.

        ``ChatOpenAI`` 는 function_calling 구조화 출력에 이 값을 자동으로 넣지만
        ``ChatOpenRouter`` 는 넣지 않는다. 빠지면 같은 모델·같은 프롬프트인데도 판정이
        달라진다 (실측: 같은 트리의 StepEfficiency 가 0.75 → 0.50). 판사 점수가
        클라이언트 설정만으로 바뀌면 이전 기록과 비교할 수 없게 된다.
        """
        kwargs: dict[str, Any] = {"method": self.structured_method}
        if self.structured_method == "function_calling":
            kwargs["parallel_tool_calls"] = False
        return self.model.with_structured_output(schema, **kwargs)

    def generate(self, prompt: str, schema: Optional[Any] = None, **kwargs) -> Any:
        if schema is None:
            return self.model.invoke(prompt).content
        # TypeError 를 절대 밖으로 내보내면 안 된다. DeepEval 의
        # ``a_generate_with_schema`` 는 TypeError 를 "이 모델은 schema 를 못 받는다"
        # 로 해석해 스키마 없이 재호출하고, 그 결과 원시 문자열이 돌아가
        # "Evaluation LLM outputted an invalid JSON" 으로 깨진다.
        try:
            return self._structured(schema).invoke(prompt)
        except Exception:
            pass
        try:
            raw = self.model.invoke(_json_prompt(prompt, schema)).content
            return _parse_json_into(raw, schema)
        except TypeError as exc:
            raise ValueError(f"구조화 출력 폴백 실패: {exc}") from exc

    async def a_generate(
        self, prompt: str, schema: Optional[Any] = None, **kwargs
    ) -> Any:
        if schema is None:
            result = await self.model.ainvoke(prompt)
            return result.content
        try:
            return await self._structured(schema).ainvoke(prompt)
        except Exception:
            pass
        try:
            result = await self.model.ainvoke(_json_prompt(prompt, schema))
            return _parse_json_into(result.content, schema)
        except TypeError as exc:
            raise ValueError(f"구조화 출력 폴백 실패: {exc}") from exc

    def get_model_name(self) -> str:
        return self.model_name

    def supports_structured_outputs(self) -> bool:
        return True


def _json_prompt(prompt: str, schema: Any) -> str:
    """구조화 출력이 실패했을 때 쓰는 폴백 — JSON 스키마를 프롬프트로 직접 지시한다."""
    import json

    return (
        f"{prompt}\n\n"
        "위 요청의 결과를 아래 JSON 스키마에 정확히 맞는 JSON 하나로만 출력하라.\n"
        "코드펜스, 설명, 앞뒤 문장 없이 JSON 본문만 출력한다.\n\n"
        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )


def _parse_json_into(raw: Any, schema: Any):
    """모델이 뱉은 텍스트에서 JSON 본문만 잘라내 스키마로 검증한다."""
    import json
    import re

    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return schema.model_validate_json(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"JSON 을 찾지 못했습니다: {text[:200]}")
    return schema.model_validate(json.loads(match.group(0)))
