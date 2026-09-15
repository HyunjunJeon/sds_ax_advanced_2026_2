"""Model configuration management.

Handles loading and saving model configuration from TOML files, providing a
structured way to define available models and providers.
"""
# [해설] ── 모듈 개요 ──────────────────────────────────────────────
# [해설] 역할: dcode의 "모델·프로바이더·자격증명" 설정의 중심 모듈(약 6.7천 줄). 크게 다섯 덩어리로 나뉜다.
# [해설]   (1) env 해석: `resolve_env_var`(`DEEPAGENTS_CODE_` 접두 우선), 프로바이더↔env var 매핑 테이블.
# [해설]   (2) 모델 카탈로그: 설치된 LangChain 패키지·config.toml·Ollama에서 모델 목록/프로필 수집
# [해설]       (`get_available_models`, `get_model_profiles`).
# [해설]   (3) 자격증명/인증 상태: `resolve_provider_credential`, `get_provider_auth_status`,
# [해설]       `apply_stored_credentials`(`/auth` 저장 키를 env로 복사).
# [해설]   (4) `ModelConfig`: config.toml(+managed 정책) 로드, `models.allowed` 정책, provider별 kwargs/base_url.
# [해설]   (5) config.toml 쓰기 헬퍼: default/recent 모델, effort, 경고 억제, MCP 신뢰 목록, thread/startup/agent 설정.
# [해설] 실행 프로세스: 클라이언트와 서버 양쪽. 클라이언트는 `/model`·`/auth` UI·시작 시 모델 결정에,
# [해설]   서버는 `config.create_model`(모델 생성)·`configurable_model.py`(런타임 교체)에서 사용한다.
# [해설] 주요 호출자: `config.py`(`create_model`, `_get_default_model_spec`), `configurable_model.py`,
# [해설]   `tui/`의 모델 선택기·auth 매니저, `app.py`, `mcp_*` 신뢰 처리.
# [해설] 관련 분석: `analysis/03-config-models-credentials.md`(모델·자격증명), `analysis/07-*`(MCP 신뢰 목록).
# [해설] 공식 문서: `docs_official/code/providers.md`, `config-file.md`, `credentials.md`, `configuration.md`.
# [해설][문서 불일치] 키 해석 순서: 문서(`credentials.md` "Key resolution order")는 접두 env > `/auth` 저장 > 표준 env
# [해설]   이지만, `resolve_provider_credential`은 저장 키를 먼저 반환한다(해당 함수 주석 참고).

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, TypedDict, cast
from urllib.parse import urlparse

import tomli_w

from deepagents_code import _env_vars, auth_store
from deepagents_code._constants import (
    LANGSMITH_API_KEY_ENV,
    LANGSMITH_API_KEY_FALLBACK_ENV_VARS,
)
from deepagents_code._git import find_git_common_dir
from deepagents_code._paths import PATHS
from deepagents_code.configuration.writer import USER_CONFIG_WRITE_LOCK

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from deepagents_code.config_manifest import ConfigOption
    from deepagents_code.configuration.resolver import RankedProviderValue
    from deepagents_code.configuration.service import ConfigSources
    from deepagents_code.configuration.types import ProviderStatus
    from deepagents_code.json_types import JsonValue

logger = logging.getLogger(__name__)

# [해설] 모든 env var의 "dcode 전용 오버라이드" 접두. `DEEPAGENTS_CODE_OPENAI_API_KEY`처럼 붙여 쓴다.
_ENV_PREFIX = "DEEPAGENTS_CODE_"
# [해설] 접두 env로 해석 성공한 이름을 한 번만 DEBUG 로그로 남기기 위한 집합과 그 락(스레드 안전).
_resolved_env_var_log_lock = threading.Lock()
_resolved_env_var_log_names: set[str] = set()


# [해설] 위 로그 중복 억제 집합을 비운다. 설정 generation이 바뀔 때(`/reload` 등) 다시 로그되게 하려는 용도(추정).
def reset_env_resolution_log() -> None:
    """Allow successful prefixed environment resolutions to be logged again."""
    with _resolved_env_var_log_lock:
        _resolved_env_var_log_names.clear()


# [해설] `/auth` 저장 키를 표준 env var(`env_var`)에 복사했을 때 실제 런타임이 그 값을 읽는지 판정.
# [해설] 접두 env가 "존재"(빈 문자열 포함)하면 `resolve_env_var`가 표준 이름을 무시하므로 False.
# [해설] 호출자: `apply_stored_service_credentials`, `get_service_auth_status`(Tavily/LangSmith 등 서비스 전용).
# [해설][주의] 모델 프로바이더 키 경로(`resolve_provider_credential`)에는 이 가드가 쓰이지 않는다.
# [해설][주의] 여기서는 `active_environment()`가 아니라 `os.environ`을 직접 본다(워크스페이스 환경 스냅샷 미반영).
def stored_key_reaches_runtime(env_var: str) -> bool:
    """Whether a stored credential copied onto `env_var` would be read.

    A present `DEEPAGENTS_CODE_` override outranks the store: the apply pass
    skips the copy outright, and the canonical name it would have written is
    then ignored by `resolve_env_var`. That holds even when the override is
    empty, which suppresses the canonical name entirely.

    Args:
        env_var: Canonical env var name the store would be copied onto.

    Returns:
        `True` when no prefixed override stands in the way.
    """
    if env_var.startswith(_ENV_PREFIX):
        return True
    return f"{_ENV_PREFIX}{env_var}" not in os.environ


# [해설] UI 라벨용: 실제 값을 공급하는 env var 이름(접두/표준 중 무엇인지)을 돌려준다. 우선순위 규칙은 `resolve_env_var`와 동일.
def resolved_env_var_name(canonical: str) -> str:
    """Return whichever env var name actually carries the resolved value.

    Mirrors `resolve_env_var`'s precedence: when the prefixed variant is
    present in `os.environ` (even empty), it wins; otherwise the canonical
    name is returned. Useful for UI labels that need to reflect what the
    app is actually reading rather than the canonical name.

    Args:
        canonical: The canonical environment variable name.

    Returns:
        The resolving env var name (prefixed or canonical).
    """
    from deepagents_code.config import active_environment

    environ = active_environment()
    if not canonical.startswith(_ENV_PREFIX):
        prefixed = f"{_ENV_PREFIX}{canonical}"
        if prefixed in environ:
            return prefixed
    return canonical


# [해설] dcode 전역의 env 조회 기본 함수. 접두 env(`DEEPAGENTS_CODE_X`)가 존재하면 무조건 그것, 아니면 `X`.
# [해설] 빈 문자열은 None으로 정규화 → `DEEPAGENTS_CODE_X=""`로 표준 값을 "가리는" 용도가 가능.
# [해설] 환경 소스는 `config.active_environment()`(워크스페이스별 `use_environment` 바인딩, 없으면 `os.environ`).
# [해설] 호출자: 이 모듈의 자격증명/엔드포인트 해석 전반, `config.py` 설정 해석 등.
def resolve_env_var(name: str) -> str | None:
    """Look up an env var with `DEEPAGENTS_CODE_` prefix override.

    Checks `DEEPAGENTS_CODE_{name}` first, then falls back to `{name}`.

    If the prefixed variable is *present* in the environment (even as an empty
    string), the canonical variable is never consulted. This lets users
    set `DEEPAGENTS_CODE_X=""` to shadow a canonically-set key -- the function
    will return `None` (since empty strings are normalized to `None`),
    effectively suppressing the canonical value.

    If `name` already carries the prefix, the double-prefixed lookup is skipped
    to avoid nonsensical `DEEPAGENTS_CODE_DEEPAGENTS_CODE_*` reads
    (e.g., when the name comes from a user's `config.toml`).

    Args:
        name: The canonical environment variable name (e.g.
            `ANTHROPIC_API_KEY`).

    Returns:
        The resolved value, or `None` when absent or empty.
    """
    from deepagents_code.config import active_environment

    environ = active_environment()
    # [해설][흐름] 1) 이미 접두가 붙은 이름(config.toml의 `api_key_env` 등)은 이중 접두 조회를 건너뛴다.
    if not name.startswith(_ENV_PREFIX):
        prefixed = f"{_ENV_PREFIX}{name}"
        if prefixed in environ:
            val = environ[prefixed]
            # [해설][주의] 접두 env가 빈 값인데 표준 env에 값이 있으면 사용자 실수일 수 있어 DEBUG로만 알린다.
            if not val and environ.get(name):
                logger.debug(
                    "%s is set but empty, blocking non-empty %s. "
                    "Unset %s to use the canonical variable.",
                    prefixed,
                    name,
                    prefixed,
                )
            if val and logger.isEnabledFor(logging.DEBUG):
                # `resolve_env_var` is called frequently; log each successful
                # prefixed resolution only once per generation to avoid spam.
                with _resolved_env_var_log_lock:
                    should_log = name not in _resolved_env_var_log_names
                    _resolved_env_var_log_names.add(name)
                if should_log:
                    logger.debug("Resolved %s from %s", name, prefixed)
            return val or None
    # [해설][흐름] 2) 접두 env가 없을 때만 표준 이름 조회.
    return environ.get(name) or None


# [해설] 프로바이더 설정 문서 URL. `UnknownProviderError`와 `/auth` 매니저가 공통으로 링크한다.
PROVIDERS_DOCS_URL = (
    "https://docs.langchain.com/oss/python/deepagents/code/providers#provider-reference"
)
"""Public docs page for configuring model providers.

Referenced by `UnknownProviderError` and the `/auth` manager so the same
URL is used everywhere a user is sent to read about provider setup.
"""


# [해설] managed 정책(관리자 배포 설정)이 값을 결정했음을 뜻하는 출처 라벨. `ModelNotAllowedError` 문구 분기에 쓰인다.
MANAGED_CONFIG_SOURCE = "managed config"
"""Resolver provenance label for a value managed policy decided.

Mirrors `configuration.service.MANAGED_SOURCE`, which this module cannot
import at module scope without pulling the configuration service onto the
import path of every `model_config` consumer. `test_model_config` asserts the
two stay equal, so a rename on either side fails loudly instead of silently
degrading `ModelNotAllowedError` to generic wording.
"""


# [해설] 이 모듈·`config.create_model` 계열 오류의 공통 기반. 하위 클래스들이 복구 힌트용 속성을 가진다.
# [해설] `configurable_model._apply_overrides`는 이 타입을 잡아 "현재 모델로 계속" 폴백한다(strict 아닐 때).
class ModelConfigError(Exception):
    """Raised when model configuration or creation fails."""


# [해설] `models.allowed` allowlist 정책 위반. `ModelConfig.policy_error`/`require_model_allowed`가 생성한다.
# [해설][주의] `ModelConfigError` 하위지만 런타임 교체 경로에서는 반드시 전파되어야 한다(`configurable_model._apply_overrides`).
class ModelNotAllowedError(ModelConfigError):
    """Raised when a model is outside the effective `models.allowed` policy."""

    def __init__(
        self,
        *,
        model_spec: str | None,
        source: str | None,
        allowed_models: tuple[str, ...],
        context: str | None = None,
    ) -> None:
        """Initialize an actionable policy error.

        Args:
            context: Where the offending spec was declared (e.g. a subagent name
                and file path), prefixed to the message. Without it a rejection
                inside a loop over many declaration files names only the model,
                leaving the user to bisect by hand.
            model_spec: The spec that was rejected, as the user supplied it (so
                a bare model name is echoed back unqualified). Pass `None` when
                no specific model was requested -- an empty allowlist blocking
                default resolution -- so the message does not invent a spec the
                user never typed.
            source: Human-readable label for the configuration layer that
                supplied the policy, as produced by the manifest resolver (e.g.
                `'config.toml'`). `MANAGED_CONFIG_SOURCE` is compared literally
                to select administrator wording; `None` yields generic wording.
            allowed_models: The specs the policy permits. An empty tuple is a
                deny-all policy and selects a distinct message.
        """
        # [해설][흐름] 정책 출처(managed/파일/없음)에 따라 문구를 고르고, spec 없음·deny-all·비정규 spec·일반 거부 4가지 메시지로 분기.
        if source == MANAGED_CONFIG_SOURCE:
            policy = "the administrator-managed models.allowed policy"
        elif source:
            policy = f"models.allowed from {source}"
        else:
            policy = "the active models.allowed policy"
        if model_spec is None:
            message = f"No model can be used because {policy} allows no models."
        elif not allowed_models:
            message = (
                f"Model {model_spec!r} is blocked because {policy} allows no models."
            )
        elif ModelSpec.try_parse(model_spec.strip()) is None:
            message = (
                f"Model {model_spec!r} cannot be matched against {policy}; "
                "use a fully qualified provider:model spec."
            )
        else:
            allowed = ", ".join(allowed_models)
            message = (
                f"Model {model_spec!r} is not included in {policy}. "
                f"Allowed models: {allowed}."
            )
        if context:
            message = f"{context}: {message}"
        super().__init__(message)
        self.model_spec = model_spec
        self.source = source
        self.allowed_models = allowed_models
        self.context = context


# [해설] 기본 모델 자동 감지(`config._get_default_model_spec`) 시 쓸 수 있는 자격증명이 전혀 없을 때.
# [해설] TUI/CLI는 이 타입을 보고 "모델 생성 지연 상태로 TUI 시작" 복구를 한다.
class NoCredentialsConfiguredError(ModelConfigError):
    """Raised when no credentials are configured for any default-resolvable provider.

    Distinct from `MissingCredentialsError` (which targets a specific provider
    the user has selected): this fires from `_get_default_model_spec()` when
    auto-detection finds no usable credentials at all. Callers (the deferred-
    start path in the TUI and CLI) `isinstance`-check this type to recover by
    launching the TUI with model creation deferred, rather than string-matching
    the formatted message.
    """


# [해설] allowlist가 켜져 있는데 허용 모델 중 인증 가능한 것이 없을 때. 위 오류의 하위 타입이라 기존 복구 경로를 재사용.
class NoAllowedModelCredentialsError(NoCredentialsConfiguredError):
    """Raised when `models.allowed` is active but none of its models can auth.

    A `NoCredentialsConfiguredError` so existing deferred-start recovery keeps
    working, but distinguishable because the recovery differs: adding *any*
    credential fixes the base case, while here only a credential for a provider
    named in the allowlist helps. Handlers that would otherwise silently retry
    surface this message instead, so `/auth` never accepts a key and then
    appears to do nothing.
    """


# [해설] bare 모델 이름에서 프로바이더를 추론하지 못했을 때. `docs_url`을 속성으로 노출해 UI가 링크를 렌더링한다.
class UnknownProviderError(ModelConfigError):
    """Raised when neither the app nor `init_chat_model` can infer a provider.

    Carries the offending model spec as an attribute and exposes
    `PROVIDERS_DOCS_URL` as a class-level constant so callers can render
    a clickable link without string-scanning the formatted message. This
    mirrors how `MissingCredentialsError` exposes `provider` / `env_var`
    for targeted recovery hints.
    """

    docs_url: ClassVar[str] = PROVIDERS_DOCS_URL
    """Provider-reference docs URL. Class-level so callers don't pass it."""

    def __init__(self, *, model_spec: str) -> None:
        """Initialize the error.

        Args:
            model_spec: The bare model name the user supplied (e.g.
                `'mystery-model'`). When the input had a `provider:model`
                form, parsing succeeds and this exception does not fire.

        Raises:
            ValueError: If `model_spec` is empty.
        """
        if not model_spec:
            msg = "model_spec must be non-empty"
            raise ValueError(msg)
        message = (
            f"Unable to infer a model provider for {model_spec!r}. "
            f"Specify one explicitly (e.g. 'anthropic:{model_spec}') "
            f"or see the provider reference at {self.docs_url}."
        )
        super().__init__(message)
        self.model_spec = model_spec


# [해설] 사용자가 고른 특정 프로바이더의 키가 없을 때. `provider`/`env_var` 속성으로 UI가 "X를 설정하라" 힌트를 만든다.
class MissingCredentialsError(ModelConfigError):
    """Raised when a provider is selected but its API key env var is unset.

    Subclasses `ModelConfigError` so existing `except ModelConfigError` blocks
    keep working. Carries the `provider` name and the canonical `env_var` so
    callers can render targeted recovery hints (e.g., "set OPENAI_API_KEY" or
    "run `/model <other_provider>:<model>`") without string-matching on the
    formatted exception message and without re-deriving the env-var name.
    """

    def __init__(
        self, message: str, *, provider: str, env_var: str | None = None
    ) -> None:
        """Initialize the error.

        Args:
            message: Human-readable message describing the missing credential.
            provider: The provider whose credentials are missing
                (e.g., `'openai'`).
            env_var: The canonical env var name expected to hold the
                credential (e.g., `'OPENAI_API_KEY'`). `None` when the
                provider has no registered env-var mapping.
        """
        super().__init__(message)
        self.provider = provider
        self.env_var = env_var


# [해설] 프로바이더의 LangChain 패키지(예: `langchain-fireworks`) 미설치. `/install` 힌트용 `package` 속성 보유.
class MissingProviderPackageError(ModelConfigError):
    """Raised when a provider is selected but its LangChain package is not installed.

    Subclasses `ModelConfigError` so existing `except ModelConfigError` blocks
    keep working. Carries the `provider` name and the `package` to install so
    callers can render targeted recovery hints (e.g., suggest `/install fireworks`
    or the `/model` slash command) without string-matching on the formatted
    exception message.
    """

    def __init__(self, message: str, *, provider: str, package: str) -> None:
        """Initialize the error.

        Args:
            message: Human-readable message describing the missing package.
            provider: The provider whose package is missing (e.g., `'fireworks'`).
            package: The pip-installable package name (e.g.,
                `'langchain-fireworks'`).
        """
        super().__init__(message)
        self.provider = provider
        self.package = package


# [해설] 프로바이더 인증 준비 상태 6종. `get_provider_auth_status`가 계산하고 `/model`·`/auth` UI와 시작 검사가 소비.
# [해설] MISSING만 시작/전환을 막는다(`ProviderAuthStatus.blocks_start`).
class ProviderAuthState(StrEnum):
    """Credential readiness state for a model provider."""

    CONFIGURED = "configured"
    """An explicit credential source is configured and non-empty."""

    MISSING = "missing"
    """An explicit credential source is required but missing."""

    NOT_REQUIRED = "not_required"
    """This provider configuration does not require API-key credentials."""

    IMPLICIT = "implicit"
    """The provider supports ambient auth outside CLI env-var checks."""

    MANAGED = "managed"
    """A custom provider class is expected to manage auth itself."""

    UNKNOWN = "unknown"
    """The app cannot determine whether provider auth is ready."""


# [해설] CONFIGURED 상태일 때 키 출처(저장소 vs env). UI 표시 구분용.
class ProviderAuthSource(StrEnum):
    """Origin of a `CONFIGURED` credential, used to discriminate display."""

    STORED = "stored"
    """Persisted in a local credential store under `~/.deepagents/.state`.

    Usually the `/auth` API-key map (`auth.json`), but also covers the
    file-backed ChatGPT OAuth token used by the codex provider
    (`chatgpt-auth.json`).
    """

    ENV = "env"
    """Resolved from an environment variable."""


# [해설] 인증 상태 값 객체(불변). 상태·출처·관련 env var·사용자 표시 문구를 담는다.
# [해설] 불변식: CONFIGURED ⇔ source가 있음(`__post_init__`에서 강제).
@dataclass(frozen=True)
class ProviderAuthStatus:
    """Credential readiness information for a provider.

    Args:
        state: Provider auth state.
        provider: Provider name.
        env_var: Env var name associated with the state, when applicable.
        source: For `CONFIGURED` states, where the credential value came
            from. `None` for non-configured states or when the source is
            not meaningful (e.g., implicit/managed auth).
        detail: Short user-facing context for selectors and logs.
    """

    state: ProviderAuthState
    provider: str
    env_var: str | None = None
    source: ProviderAuthSource | None = None
    detail: str | None = None

    # [해설] source/state 불변식 위반 시 즉시 ValueError — 잘못된 상태 조합이 UI까지 흘러가지 않게 한다.
    def __post_init__(self) -> None:
        """Enforce the source-vs-state invariant.

        Raises:
            ValueError: If `source` is set but `state` is not `CONFIGURED`,
                or if `state` is `CONFIGURED` but no `source` is recorded.
        """
        is_configured = self.state is ProviderAuthState.CONFIGURED
        has_source = self.source is not None
        if is_configured != has_source:
            msg = (
                f"ProviderAuthStatus invariant violated: "
                f"state={self.state!r} requires "
                f"{'a source' if is_configured else 'source=None'}, "
                f"got source={self.source!r}"
            )
            raise ValueError(msg)

    # [해설] MISSING일 때만 모델 생성/전환 차단.
    @property
    def blocks_start(self) -> bool:
        """Whether this status should block model creation or switching."""
        return self.state is ProviderAuthState.MISSING

    # [해설] 과거 API `has_provider_credentials`의 tri-state(True/False/None) 호환 변환.
    def as_legacy_bool(self) -> bool | None:
        """Return the historic `has_provider_credentials` tri-state value."""
        if self.state is ProviderAuthState.MISSING:
            return False
        if self.state is ProviderAuthState.UNKNOWN:
            return None
        return True

    # [해설] 누락 사유 문구. detail > env_var 템플릿 > "미등록 프로바이더" 순.
    def missing_detail(self) -> str:
        """Return a user-facing reason for a missing-credential status.

        `detail` wins over the `env_var` template because a status that names
        several accepted variables (a service with fallbacks) has already
        spelled the fuller sentence there.
        """
        if self.detail:
            return self.detail
        if self.env_var:
            return f"{self.env_var} is not set or is empty"
        return (
            f"provider '{self.provider}' is not recognized. "
            f"Add it to {PATHS.display(PATHS.profile.config_file)} with an "
            "api_key_env field"
        )


# [해설] `provider:model` 문자열의 파싱 결과 값 객체. 첫 콜론에서만 분리하므로 모델명에 콜론이 있어도 된다(예: `ollama:qwen3:4b`).
@dataclass(frozen=True)
class ModelSpec:
    """A model specification in `provider:model` format.

    Examples:
        >>> spec = ModelSpec.parse("anthropic:claude-sonnet-4-5")
        >>> spec.provider
        'anthropic'
        >>> spec.model
        'claude-sonnet-4-5'
        >>> str(spec)
        'anthropic:claude-sonnet-4-5'
    """

    provider: str
    """The provider name (e.g., `'anthropic'`, `'openai'`)."""

    model: str
    """The model identifier (e.g., `'claude-sonnet-4-5'`, `'gpt-5.5'`)."""

    def __post_init__(self) -> None:
        """Validate the model spec after initialization.

        Raises:
            ValueError: If provider or model is empty.
        """
        if not self.provider:
            msg = "Provider cannot be empty"
            raise ValueError(msg)
        if not self.model:
            msg = "Model cannot be empty"
            raise ValueError(msg)

    # [해설] 엄격 파싱: 콜론이 없으면 ValueError. 빈 provider/model은 `__post_init__`에서 거부.
    @classmethod
    def parse(cls, spec: str) -> ModelSpec:
        """Parse a model specification string.

        Args:
            spec: Model specification in `'provider:model'` format.

        Returns:
            Parsed ModelSpec instance.

        Raises:
            ValueError: If the spec is not in valid `'provider:model'` format.
        """
        if ":" not in spec:
            msg = (
                f"Invalid model spec '{spec}': must be in provider:model format "
                "(e.g., 'anthropic:claude-sonnet-4-5')"
            )
            raise ValueError(msg)
        provider, model = spec.split(":", 1)
        return cls(provider=provider, model=model)

    # [해설] 예외 대신 None을 돌려주는 파싱. 입력 검증·정책 비교에서 널리 쓰인다.
    @classmethod
    def try_parse(cls, spec: str) -> ModelSpec | None:
        """Non-raising variant of `parse`.

        Args:
            spec: Model specification in `provider:model` format.

        Returns:
            Parsed `ModelSpec`, or `None` when *spec* is not valid.
        """
        try:
            return cls.parse(spec)
        except ValueError:
            return None

    def __str__(self) -> str:
        """Return the model spec as a string in `provider:model` format."""
        return f"{self.provider}:{self.model}"


# [해설] `[models].allowed` 값 검증·정규화. 정확한 `provider:model` 또는 `provider:*` 와일드카드만 허용.
# [해설] 호출자: `configuration/providers.py`(매니페스트 옵션 `models.allowed` 값 강제변환) → `ModelConfig.load`가 `_resolve_model_file_option`으로 받아 사용.
# [해설] 결과 튜플은 `ModelConfig.allowed_models`로 들어가 `is_model_allowed`에서 사용된다.
def parse_model_allowlist(value: object) -> tuple[str, ...]:
    """Parse an ordered model allowlist of exact specs and provider wildcards.

    Args:
        value: Raw TOML value to validate.

    Returns:
        Canonical entries in declaration order with duplicates removed. Each is
        either an exact `provider:model` spec or a `provider:*` wildcard
        permitting every model from that provider.

    Raises:
        TypeError: If the value is not a list.
        ValueError: If an entry is not an exact `provider:model` string or
            `provider:*` wildcard, or is a bare Bedrock model ID (see below).
    """
    # [해설][흐름] 1) 타입 검사: 리스트가 아니면 TypeError.
    from deepagents_code.config import _is_bedrock_model_id

    if not isinstance(value, list):
        msg = "expected a list of provider:model strings"
        raise TypeError(msg)

    allowed: list[str] = []
    seen: set[str] = set()
    # [해설][흐름] 2) 항목별 검증: 빈 문자열/비문자열 거부.
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            msg = "every entry must be a non-empty provider:model string"
            raise ValueError(msg)
        normalized = entry.strip()
        if _is_bedrock_model_id(normalized.lower()):
            # A bare Bedrock ID such as `anthropic.claude-3-5-sonnet-v2:0`
            # splits at its *version* colon, yielding a nonsense provider that
            # no spec can ever match -- `create_model` normalizes the same
            # input to `bedrock:<id>`. Left accepted, the allowlist would
            # silently become deny-all, so demand the explicit prefix.
            msg = (
                f"invalid model spec {entry!r}; bare Bedrock model IDs must be "
                f"written as 'bedrock:{normalized}'"
            )
            raise ValueError(msg)
        # [해설][흐름] 3) `provider:*` 와일드카드 분기 — provider 부분을 가짜 spec(`provider:_`)으로 검증해 공백·빈 값 거부.
        if normalized.endswith(":*"):
            provider = normalized[:-2].strip()
            # Validate the prefix as a one-character model spec rather than
            # accepting any non-empty string, so `o penai:*` and `:*` reject
            # here instead of matching nothing (or everything) later. The
            # `strip()` round-trip mirrors the exact-spec check below, which
            # treats internal whitespace as noncanonical.
            if (
                not provider
                or provider != provider.strip()
                or any(ch.isspace() for ch in provider)
                or ModelSpec.try_parse(f"{provider}:_") is None
            ):
                msg = (
                    f"invalid model spec {entry!r}; a wildcard must name a "
                    f"provider as 'provider:*'"
                )
                raise ValueError(msg)
            canonical = f"{provider}:*"
            if canonical not in seen:
                seen.add(canonical)
                allowed.append(canonical)
            continue
        # [해설][흐름] 4) 와일드카드 외 위치의 `*`는 금지.
        if "*" in normalized:
            msg = (
                f"invalid model spec {entry!r}; '*' is only supported as a "
                f"whole-provider wildcard ('provider:*')"
            )
            raise ValueError(msg)
        # [해설][흐름] 5) 정확한 spec: 파싱 + 앞뒤 공백이 섞인 비정규형 거부, 중복 제거하며 선언 순서 유지.
        parsed = ModelSpec.try_parse(normalized)
        if (
            parsed is None
            or parsed.provider != parsed.provider.strip()
            or parsed.model != parsed.model.strip()
        ):
            msg = f"invalid model spec {entry!r}; expected provider:model"
            raise ValueError(msg)
        canonical = str(parsed)
        if canonical not in seen:
            seen.add(canonical)
            allowed.append(canonical)
    return tuple(allowed)


# [해설] allowlist가 "선언은 됐지만 파싱 실패"한 경우 그 출처 라벨을 만든다.
# [해설][설계][주의] 잘못된 allowlist를 "무제한"으로 취급하지 않고 deny-all(fail-closed)로 바꾸기 위한 헬퍼.
# [해설] managed 계층을 사용자 계층보다 먼저 검사한다.
def _malformed_allowlist_source(
    sources: object,
    user_data: Mapping[str, Any],
    config_path: Path,
) -> str | None:
    """Describe where a declared-but-unusable `models.allowed` came from.

    Called only when the manifest resolver produced no tuple. A declaration
    that survives to here failed `parse_model_allowlist`, so the caller turns
    it into a deny-all policy instead of letting it vanish into "unrestricted".

    Args:
        sources: The loaded configuration sources (duck-typed to avoid a
            circular import of the configuration service).
        user_data: The user layer's TOML table, already emptied when the file
            is unreadable.
        config_path: Path the user layer was read from, for the label.

    Returns:
        A provenance label naming the layer and the defect, or `None` when
            `models.allowed` was never declared and the policy is genuinely
            absent.
    """
    managed_data = getattr(getattr(sources, "managed", None), "data", None)
    for data, label in (
        (managed_data, MANAGED_CONFIG_SOURCE),
        (user_data, str(config_path)),
    ):
        if not isinstance(data, dict):
            continue
        models = data.get("models")
        if isinstance(models, dict) and models.get("allowed") is not None:
            return f"{label} ([models].allowed is malformed)"
    return None


# [해설] 모델 프로필(컨텍스트 한도·툴콜 지원 등) + config.toml로 덮어쓴 키 추적. `get_model_profiles`의 반환 원소.
class ModelProfileEntry(TypedDict):
    """Profile data for a model with override tracking."""

    profile: dict[str, Any]
    """Merged profile dict (upstream defaults + config.toml overrides).

    Keys vary by provider (e.g., `max_input_tokens`, `tool_calling`).
    """

    overridden_keys: frozenset[str]
    """Keys in `profile` whose values came from config.toml rather than the
    upstream provider package."""


# [해설] `[models.providers.<name>]` 테이블의 스키마(TypedDict). 공식 문서 `config-file.md` "Provider configuration"과 대응.
# [해설][주의] `class_path`는 사용자 config에서 임의 파이썬 클래스를 import·실행한다(보안 신뢰 경계 = 사용자 자신).
class ProviderConfig(TypedDict, total=False):
    """Configuration for a model provider.

    The optional `class_path` field allows bypassing `init_chat_model` entirely
    and instantiating an arbitrary `BaseChatModel` subclass via importlib.

    !!! warning

        Setting `class_path` executes arbitrary Python code from the user's
        config file. This has the same trust model as `pyproject.toml` build
        scripts — the user controls their own machine.
    """

    enabled: bool
    """Whether this provider appears in the model switcher.

    Defaults to `True`. Set to `False` to hide a package-discovered provider
    and all its models from the `/model` selector. Useful when a LangChain
    provider package is installed as a transitive dependency but should not
    be user-visible.
    """

    models: list[str]
    """List of model identifiers available from this provider."""

    api_key_env: str
    """Name of the environment variable that holds the API key.

    This is the env var *name* (e.g., `"OPENAI_API_KEY"`), not the secret
    itself. The app resolves it at startup to verify credentials before model
    creation.
    """

    display_name: str
    """Human-readable provider name shown in auth UI.

    Useful for arbitrary providers whose config key is optimized for machine use
    (e.g., `my_gateway`) but whose UI label should include spaces or brand
    capitalization.
    """

    short_name: str
    """Compact brand label for space-constrained UI (e.g. the `/model` Recent
    tag), where the full `display_name` — which may carry a parenthetical
    qualifier like `"OpenAI (Subscription login)"` — is too long. Optional;
    when unset, callers fall back to `display_name`.
    """

    api_key_url: str
    """Provider page where users can create or manage API keys.

    Used by `/auth` as an acquisition link before the API-key input. The value is
    a URL, not a credential. Must use an `http` or `https` scheme to render as a
    clickable link; values with other schemes are ignored with a warning.
    """

    base_url: str
    """Custom base URL."""

    base_url_env: str
    """Name of the environment variable that holds this provider's base URL.

    Parallel to `api_key_env`: lets a provider that is not one of the built-in
    `PROVIDER_BASE_URL_ENV` entries participate in endpoint resolution and in
    the key/endpoint pairing applied by `apply_stored_credentials` (so a stored
    `/auth` override clears an inherited gateway URL). The static `base_url`
    field still wins over this when both are set.
    """

    # [해설] 아래 `class_path`/`params`/`profile`은 init_chat_model을 우회하거나 생성자 kwargs·프로필을 덮어쓰는 고급 설정.
    # Level 2: arbitrary BaseChatModel classes

    class_path: str
    """Fully-qualified Python class in `module.path:ClassName` format.

    When set, `create_model` imports this class and instantiates it directly
    instead of calling `init_chat_model`.
    """

    params: dict[str, Any]
    """Extra keyword arguments forwarded to the model constructor.

    Flat keys (e.g., `temperature = 0`) are provider-wide defaults applied to
    every model from this provider. Model-keyed sub-tables (e.g.,
    `[params."qwen3:4b"]`) override individual values for that model only;
    the merge is shallow (model wins on conflict).

    Do not set `api_key` here — the early credential check runs before
    `params` are read, so the app will reject the model before it sees the key.
    Use `api_key_env` to point at an environment variable instead.
    """

    profile: dict[str, Any]
    """Overrides merged into the model's runtime profile dict.

    Flat keys (e.g., `max_input_tokens = 4096`) are provider-wide defaults.
    Model-keyed sub-tables (e.g., `[profile."claude-sonnet-4-5"]`) override
    individual values for that model only; the merge is shallow.
    """


# [해설] 프로필 루트/설정 파일/상태 디렉터리 경로. 실제 경로 계산은 `_paths.PATHS`(`DEEPAGENTS_HOME` 반영).
# [해설] 기본값: `~/.deepagents`, `~/.deepagents/config.toml`, `~/.deepagents/.state`(추정: PATHS 기본 규칙).
DEFAULT_CONFIG_DIR = PATHS.profile.root
"""User-level Deep Agents directory, optionally set by `DEEPAGENTS_HOME`."""

DEFAULT_CONFIG_PATH = PATHS.profile.config_file
"""Path to the selected profile's model configuration file."""

DEFAULT_STATE_DIR = PATHS.profile.state_dir
"""Directory for app-managed internal state in the selected profile.

Holds files the app writes for its own bookkeeping — OAuth tokens, the
sessions database, version-check caches, input history. Kept separate from
top-level user-facing config and agent directories so listing the profile root
doesn't conflate state with agents.
"""


# [해설] OS별 캐시 루트(macOS `~/Library/Caches`, Windows `LOCALAPPDATA`, 그 외 XDG)를 계산한다.
# [해설] 홈 디렉터리를 절대경로로 못 구하면 프로필 `.state/cache`로 폴백. 업데이트 로그 등 캐시성 파일 위치 결정에 쓰인다(추정).
# [해설][주의] 설치 스크립트는 일부러 XDG 규칙을 따르므로 macOS에서 두 로그 위치가 다르다(docstring 참고).
def default_cache_dir() -> Path:
    """Return the OS-appropriate cache directory for Deep Agents Code.

    Uses `~/Library/Caches` on macOS, `LOCALAPPDATA` on Windows (falling back
    to `~/AppData/Local`), and `XDG_CACHE_HOME` elsewhere when it is an
    absolute path (falling back to `~/.cache`). The XDG spec treats relative
    `XDG_CACHE_HOME` values as invalid, so they are ignored rather than
    resolved against the launch directory. If the OS home directory cannot be
    resolved to an absolute path, caches fall back to the selected profile's
    `.state/cache` directory so an absolute `DEEPAGENTS_HOME` remains usable.

    Platform-native locations are the convention for a long-lived app (this is
    what `platformdirs` codifies and what `uv` itself does — its own cache is
    `~/Library/Caches/uv` on macOS). The install script deliberately does not
    follow this: as a portable one-shot POSIX bootstrap it uses XDG-style
    `${XDG_CACHE_HOME:-~/.cache}` on every platform (like the rustup and uv
    installers), so on macOS its `<cache>/deepagents-code/install.log` lands
    under a different root than the update logs. That divergence is
    intentional; do not "fix" one side to match the other without a concrete
    need (e.g., a diagnostic command that collects both logs).

    Returns:
        Base cache directory, before the `deepagents-code` subdirectory.
    """
    # [해설][흐름] 1) 플랫폼 env 우선(Windows LOCALAPPDATA / Linux 등 절대경로 XDG_CACHE_HOME만).
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data)
    elif sys.platform != "darwin":
        xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache_home and Path(xdg_cache_home).is_absolute():
            return Path(xdg_cache_home)
    # [해설][흐름] 2) 홈 디렉터리 기반 기본 경로, 실패 시 프로필 상태 디렉터리로 폴백.
    try:
        home = Path.home()
    except RuntimeError:
        return DEFAULT_STATE_DIR / "cache"
    if not home.is_absolute():
        return DEFAULT_STATE_DIR / "cache"
    if sys.platform == "win32":
        return home / "AppData" / "Local"
    if sys.platform == "darwin":
        return home / "Library" / "Caches"
    return home / ".cache"


# [해설] `/model` 선택기 상단의 "최근 사용 모델(MRU)" 목록 파일명과 최대 개수. `load_recent_models`/`touch_recent_model`이 사용.
RECENT_MODELS_FILENAME = "recent_models.json"
"""Filename under `DEFAULT_STATE_DIR` for the MRU list shown in `/model`."""

RECENT_MODELS_LIMIT = 5
"""Maximum number of `provider:model` specs retained in the recent list.

Sized to fit comfortably above the provider-grouped list in `/model` without
pushing the rest of the catalog off-screen on a typical terminal.
"""

# [해설] LangSmith LLM Gateway 연동을 지원하는 프로바이더 집합. `_resolve_gateway_configured`에서 게이트웨이 인증 판정에 쓰인다.
LANGSMITH_GATEWAY_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "baseten", "fireworks", "google_genai", "openai"}
)
"""Providers whose LangChain integrations support LangSmith LLM Gateway env vars."""

# [해설] LangSmith SaaS 게이트웨이 호스트. 반드시 `is_langsmith_gateway_host`로 비교(부분 문자열 비교는 피싱 호스트 허용 위험).
LANGSMITH_GATEWAY_HOST = "smith.langchain.com"
"""Host identifying LangSmith's managed (SaaS) gateway.

The single definition: compare against it through `is_langsmith_gateway_host`
rather than testing it as a raw-URL substring, which also accepts lookalikes
such as `smith.langchain.com.evil.example`.
"""


# [해설] 호스트가 LangSmith 게이트웨이(또는 그 서브도메인)인지 판정. 점 경계 suffix 매칭으로 유사 도메인 공격을 막는다.
# [해설][주의] 보안 검사 성격 — `smith.langchain.com.evil.example` 같은 호스트를 거부해야 키가 새지 않는다.
def is_langsmith_gateway_host(host: str | None) -> bool:
    """Return whether a parsed hostname is the LangSmith managed gateway.

    Matches the host exactly or as a subdomain (org-scoped gateway URLs). The
    suffix match requires a dot boundary, so `notsmith.langchain.com` does not
    qualify; a host that merely *contains* the gateway name earlier in the
    string (`smith.langchain.com.evil.example`) is not a suffix and is
    likewise rejected.

    Case and a trailing root dot are normalized here rather than left to the
    caller. Both callers reach this from a different parser, and a precondition
    enforced only by a comment in each caller is the way the two drift apart.

    Args:
        host: A hostname, or `None` when the URL could not be parsed.

    Returns:
        `True` when the host is the gateway or a subdomain of it.
    """
    if host is None:
        return False
    normalized = host.strip().lower().removesuffix(".")
    return normalized == LANGSMITH_GATEWAY_HOST or normalized.endswith(
        f".{LANGSMITH_GATEWAY_HOST}"
    )


# [해설] 게이트웨이 사용 토글 env(`LANGSMITH_GATEWAY`), 게이트웨이 전용 키 env, 그리고 토글을 끄는 값 집합("false/0/no").
LANGSMITH_GATEWAY_ENV = "LANGSMITH_GATEWAY"
LANGSMITH_GATEWAY_API_KEY_ENV = "LANGSMITH_GATEWAY_API_KEY"
_LANGSMITH_GATEWAY_FALSE_VALUES = frozenset({"false", "0", "no"})


# [해설] 잘 알려진 프로바이더 → API 키 env var 매핑. 자격증명 사전 검사(`get_provider_auth_status`),
# [해설] `/auth` 저장 키 복사 대상(`apply_stored_credentials`), 에러 힌트(`MissingCredentialsError.env_var`)의 기준 테이블.
# [해설] Vertex 계열은 "키"가 아니라 `GOOGLE_CLOUD_PROJECT`로 준비 여부를 본다(ambient ADC 인증, `IMPLICIT_AUTH_PROVIDERS` 참고).
PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "baseten": "BASETEN_API_KEY",
    "cohere": "COHERE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google_anthropic_vertex": "GOOGLE_CLOUD_PROJECT",
    "google_genai": "GOOGLE_API_KEY",
    "google_vertexai": "GOOGLE_CLOUD_PROJECT",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "ibm": "WATSONX_APIKEY",
    "litellm": "LITELLM_API_KEY",
    "meta": "MODEL_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PPLX_API_KEY",
    "together": "TOGETHER_API_KEY",
    "xai": "XAI_API_KEY",
}
"""Well-known providers mapped to the env var that holds their API key.

Used by `has_provider_credentials` to verify credentials *before* model
creation, so the UI can show a warning icon and a specific error message
(e.g., "ANTHROPIC_API_KEY not set") instead of letting the provider fail at call
time.

Providers not listed here fall through to the config-file check or the langchain
registry fallback.
"""

# [해설] 프로바이더 SDK 자체 재시도를 끄기 위한 생성자 kwarg 이름. dcode는 모델 노드 미들웨어가 재시도 예산을 소유하므로
# [해설] SDK 재시도와 곱해지지 않도록 생성 시 0(또는 아래 특수값)을 넣는다. 사용처: `config.py`의 재시도 설정 해석(추정: `_get_provider_kwargs` 부근).
# [해설] 공식 문서 `config-file.md` "Retries"(`--max-retries` > `[retries.<provider>]` > `[retries]` > 5).
RETRY_PARAM_BY_PROVIDER: dict[str, str | None] = {
    "anthropic": "max_retries",
    "azure_openai": "max_retries",
    "baseten": "max_retries",
    "bedrock": "max_retries",
    "deepseek": "max_retries",
    "fireworks": "max_retries",
    "google_anthropic_vertex": "max_retries",
    "google_genai": "max_retries",
    "google_vertexai": "max_retries",
    "groq": "max_retries",
    "litellm": "max_retries",
    "meta": "max_retries",
    "mistralai": "max_retries",
    "openai": "max_retries",
    "openai_codex": "max_retries",
    "openrouter": "max_retries",
    "perplexity": "max_retries",
    "together": "max_retries",
    "xai": "max_retries",
    # `None` means "checked, and this integration has no retry-count kwarg" --
    # distinct from a provider absent from the table, which means dcode does
    # not know. Only the absent case warrants the "SDK retries stay active"
    # warning: a `None` provider has no SDK retry loop to multiply, so warning
    # about it told the user to set `[retries.<provider>].param` to a kwarg
    # their integration would drop.
    #
    # `cohere` is deliberately NOT listed here: `langchain_cohere`'s `BaseCohere`
    # appears to expose `max_retries`, so it likely belongs above with a kwarg
    # name rather than here. Left absent until someone can check it against an
    # installed `langchain_cohere` -- absent warns, which is noisy but honest,
    # whereas a wrong `None` would silently leave its SDK retries running.
    "huggingface": None,
    "ibm": None,
    "nvidia": None,
    "ollama": None,
}
"""Constructor kwargs used to disable provider-owned retry loops.

dcode's model-node middleware owns the retry budget, so integrations with a
known retry-count parameter receive their provider-specific disable value at
construction time. A `None` value records an integration reported to
have no retry-count parameter. Providers absent from this mapping are unknown
to dcode and must declare one with `[retries.<provider>].param` in
`config.toml`.

A kwarg name is verified against that integration's chat model constructor,
never inferred from the provider name. The `None` entries are the weaker claim:
none of those packages is installed in this repo, so they rest on the
integrations' own documentation. Re-check one before relying on it.
"""

# [해설] google-genai는 "총 시도 횟수"를 세므로 0이 아니라 1이 재시도 끄기 값이다.
RETRY_DISABLE_VALUE_BY_PROVIDER: dict[str, int] = {"google_genai": 1}
"""Non-zero provider-specific values that disable SDK retries.

`google-genai` counts *total attempts*, not retries. Before 1.68.0 it read zero
as unset and restored its own five-attempt default; from 1.68.0 on it coerces
zero to one (`_api_client._retry_args`), so zero and one now behave alike. One is
sent because it disables retries on both, and it is the only value that means
"the initial request only" on every version.

Other registered providers count retries, so zero disables them.
"""

# [해설] `/auth`에서 관리하는 비모델 서비스 이름들. LangSmith는 키 저장 시 트레이싱 활성화까지 연결되고,
# [해설] Tavily는 서버 spawn 시 `web_search` 도구 생성 여부를 좌우한다(서버 재시작 필요).
LANGSMITH_SERVICE = "langsmith"
"""Service name for LangSmith tracing in `SERVICE_API_KEY_ENV`.

Storing a key for this service via `/auth` also enables tracing at startup
(see `config._apply_stored_langsmith_tracing`) and can carry a custom project
name, so it gets special handling beyond a plain key copy.
"""

TAVILY_SERVICE = "tavily"
"""Service name for Tavily web search in `SERVICE_API_KEY_ENV`.

Storing a key for this service via `/auth` gates the spawn-time `web_search`
tool (see `server_graph._build_tools`), so a key added to a running server
takes effect only after a respawn — the app offers that restart, and this
constant is the single name its `/auth` handling compares against.
"""

# [해설] 서비스 → 키 env var 매핑과 폴백 env 목록. `get_service_auth_status`/`apply_stored_service_credentials`에서 사용.
SERVICE_API_KEY_ENV: dict[str, str] = {
    LANGSMITH_SERVICE: LANGSMITH_API_KEY_ENV,
    TAVILY_SERVICE: "TAVILY_API_KEY",
}
"""Non-model services configurable via `/auth`, mapped to their API-key env var.

These are not LLM providers — they back features such as web search (Tavily) or
agent tracing (LangSmith) — but their credentials follow the same store-on-disk
model as model providers, so they appear in the `/auth` manager and can be
entered directly in the TUI instead of being exported as environment variables
before launch.
"""

SERVICE_API_KEY_FALLBACK_ENV_VARS: dict[str, tuple[str, ...]] = {
    LANGSMITH_SERVICE: LANGSMITH_API_KEY_FALLBACK_ENV_VARS,
}
"""Fallback env vars per non-model service, tried after its primary env var.

A service absent from this map has no fallbacks.
"""

# [해설] ChatGPT 구독(OAuth) 로그인 기반 Codex 프로바이더. `openai`와 인증·모델 클래스·엔드포인트가 모두 다르다.
# [해설] 인증 상태는 `_get_codex_auth_status`, OAuth 흐름은 `integrations/openai_codex`.
CODEX_PROVIDER = "openai_codex"
"""Provider name for `_ChatOpenAICodex` models authenticated via ChatGPT OAuth.

Distinct from `"openai"` (which uses an `OPENAI_API_KEY`) because the auth
source, model class, and request endpoint all differ. See
`deepagents_code.integrations.openai_codex` for the OAuth flow.
"""

# [해설] Codex 백엔드가 실제로 서비스하는 모델만 노출하기 위한 큐레이션 목록(openai 프로필을 미러링하되 이 집합으로 필터).
CODEX_MODELS: frozenset[str] = frozenset(
    {
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.2",
    }
)
"""Curated allowlist of models the Codex (ChatGPT OAuth) backend serves.

The provider mirrors `openai` profiles, but only models in this set are
exposed under `openai_codex`. The Codex backend serves a narrower lineup than
the full `openai` API, so mirroring every openai model would surface specs the
backend rejects at call time.
"""


# [해설] 프로바이더별 base URL env var 전체 목록(첫 번째가 canonical = 저장 엔드포인트를 쓰는 이름).
# [해설] 읽기: `ModelConfig.get_base_url`이 순서대로 `resolve_env_var`로 조회(접두 env 우선 규칙 동일).
# [해설] 쓰기/정리: `apply_stored_credentials` → `_apply_stored_base_url`이 나머지 별칭까지 지워 오래된 게이트웨이 URL 누수를 막는다.
# [해설][설계] 키와 엔드포인트는 같은 출처에서 한 쌍으로 해석되어야 한다(게이트웨이 키는 게이트웨이 URL에서만 유효).
PROVIDER_BASE_URL_ENV: dict[str, tuple[str, ...]] = {
    # Each tuple lists every base-URL env var the provider's LangChain
    # integration and underlying SDK may read, canonical name first. Names were
    # verified against the integration and SDK source, not inferred:
    #   anthropic     langchain_anthropic reads ANTHROPIC_API_URL; the anthropic
    #                 SDK reads ANTHROPIC_BASE_URL.
    #   azure_openai  AzureChatOpenAI and the openai SDK both read
    #                 AZURE_OPENAI_ENDPOINT.
    #   baseten       ChatBaseten reads BASETEN_BASE_URL, then falls back to
    #                 BASETEN_API_BASE.
    #   cohere        langchain_cohere passes base_url=None, so the cohere SDK's
    #                 CO_API_URL is what takes effect.
    #   deepseek      ChatDeepSeek reads DEEPSEEK_API_BASE (alias base_url).
    #   fireworks     ChatFireworks reads FIREWORKS_API_BASE; when unset the
    #                 fireworks SDK reads FIREWORKS_BASE_URL.
    #   google_genai  the google-genai SDK reads GOOGLE_GEMINI_BASE_URL (the lone
    #                 name langchain_google_genai threads through HttpOptions).
    #   groq          ChatGroq reads GROQ_API_BASE; when unset the groq SDK reads
    #                 GROQ_BASE_URL.
    #   huggingface   the integration and huggingface_hub both read
    #                 HF_INFERENCE_ENDPOINT.
    #   ibm           ChatWatsonx reads WATSONX_URL.
    #   meta          ChatMetaModel reads MODEL_API_BASE.
    #   mistralai     ChatMistralAI reads MISTRAL_BASE_URL.
    #   nvidia        ChatNVIDIA reads NVIDIA_BASE_URL.
    #   openai        langchain_openai reads OPENAI_API_BASE; the openai SDK
    #                 reads OPENAI_BASE_URL.
    #   openrouter    ChatOpenRouter reads OPENROUTER_API_BASE (alias base_url).
    #   perplexity    the integration passes no base_url, so the perplexity SDK's
    #                 PERPLEXITY_BASE_URL is what takes effect.
    #   together      ChatTogether reads TOGETHER_API_BASE (alias base_url).
    #   xai           ChatXAI reads XAI_API_BASE (alias base_url).
    #
    # OpenAI-compatible providers (deepseek, openrouter, together, xai, baseten)
    # sit on the openai SDK, whose only base-URL env var is the shared
    # OPENAI_BASE_URL. That name is intentionally NOT listed under those
    # providers: writing or clearing it under another provider's name would
    # clobber the user's real OpenAI endpoint. Each is listed above under its own
    # dedicated name(s) instead. In practice the integration always passes
    # base_url explicitly, so the shared fallback never fires.
    #
    # Omitted (no dedicated, provider-specific endpoint env var): litellm
    # (api_base arg, per-provider env), google_vertexai (endpoint derived from the
    # region). A `/auth` endpoint for these still resolves through the
    # stored-credential step of `get_base_url` and reaches the model as the
    # `base_url` kwarg.
    "anthropic": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"),
    "azure_openai": ("AZURE_OPENAI_ENDPOINT",),
    "baseten": ("BASETEN_BASE_URL", "BASETEN_API_BASE"),
    "cohere": ("CO_API_URL",),
    "deepseek": ("DEEPSEEK_API_BASE",),
    "fireworks": ("FIREWORKS_BASE_URL", "FIREWORKS_API_BASE"),
    "google_genai": ("GOOGLE_GEMINI_BASE_URL",),
    "groq": ("GROQ_BASE_URL", "GROQ_API_BASE"),
    "huggingface": ("HF_INFERENCE_ENDPOINT",),
    "ibm": ("WATSONX_URL",),
    "meta": ("MODEL_API_BASE",),
    "mistralai": ("MISTRAL_BASE_URL",),
    "nvidia": ("NVIDIA_BASE_URL",),
    "openai": ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
    "openrouter": ("OPENROUTER_API_BASE",),
    "perplexity": ("PERPLEXITY_BASE_URL",),
    "together": ("TOGETHER_API_BASE",),
    "xai": ("XAI_API_BASE",),
}
"""Every base-URL env var a provider's SDK may read.

Element `[0]` is the *canonical* name — the one we write a stored endpoint to.
`get_base_url` reads each name in tuple order through `resolve_env_var`, so every
base URL gets the same `DEEPAGENTS_CODE_*` > plain-var precedence as API keys.
The remaining names are alternates the SDK might also honor;
`apply_stored_credentials` clears them when applying or resetting an endpoint, so
a stale value (e.g. an inherited gateway URL) can't leak through. Clearing every
name is what lets the write path treat the canonical as authoritative regardless
of which name the SDK prefers.

The key and its endpoint are a coherent pair: a gateway key only works against
the gateway URL, a provider-native key only against the provider's own endpoint,
so both must resolve from the same source.
"""


# [해설] canonical base URL env 이름(튜플 첫 원소). 내장 목록 밖 프로바이더는 None → config.toml `base_url_env`로 보완(`get_default_base_url_env`).
def _canonical_base_url_env(provider: str) -> str | None:
    """Return the canonical (written) base-URL env var name for a provider.

    The canonical name is element `[0]` of the provider's `PROVIDER_BASE_URL_ENV`
    tuple. Returns `None` for providers outside the built-in set.

    Args:
        provider: Provider name.

    Returns:
        Canonical env var name, or `None` if the provider has no built-in entry.
    """
    names = PROVIDER_BASE_URL_ENV.get(provider)
    return names[0] if names else None


# [해설] env 키 없이도 ambient 인증(GCP ADC 등)이 가능한 프로바이더. `create_model`의 조기 키 검사를 건너뛰고 상태는 IMPLICIT로 표시.
IMPLICIT_AUTH_PROVIDERS: frozenset[str] = frozenset(
    {"google_anthropic_vertex", "google_vertexai"}
)
"""Providers that support ambient auth outside app env-var checks.

These providers can authenticate without the env var listed in
`PROVIDER_API_KEY_ENV`, so a missing env var should not be treated as a hard
credential failure. Used by `create_model` to skip the early credential check
and by `get_provider_auth_status` for user-facing auth labels.
"""

# [해설] 로컬 기본 구성에서 키가 필요 없는 프로바이더(Ollama).
NO_AUTH_REQUIRED_PROVIDERS: frozenset[str] = frozenset({"ollama"})
"""Providers whose default local configuration does not require API keys."""

# [해설] 있으면 인증 모드를 켜는 선택적 키 env(Ollama 원격/클라우드용).
OPTIONAL_AUTH_ENV: dict[str, str] = {"ollama": "OLLAMA_API_KEY"}
"""Optional env vars that enable authenticated provider modes when present."""

# [해설] 로컬 프로바이더를 원격 호스트로 가리키는 env(`OLLAMA_HOST`). `_get_provider_endpoint` 등 엔드포인트 결정에 사용(추정).
PROVIDER_HOST_ENV: dict[str, str] = {"ollama": "OLLAMA_HOST"}
"""Provider-specific env vars that can point a local provider at a remote host."""

# [해설] 커스텀 헤더 env — 게이트웨이 인증을 헤더로 주입하는 경우 인증 상태 판정에 참고된다(추정: `_resolve_gateway_configured`).
PROVIDER_CUSTOM_HEADERS_ENV: dict[str, str] = {"anthropic": "ANTHROPIC_CUSTOM_HEADERS"}
"""Provider SDK env vars that inject custom request headers (e.g. gateway auth)."""

# [해설] Ollama 기본 엔드포인트와 탐색(discovery) 소켓 타임아웃(1초 — 죽은 데몬 때문에 `/model` 목록 로딩이 멈추지 않게).
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
"""Default endpoint assumed when no `base_url` or `OLLAMA_HOST` is configured."""

OLLAMA_DISCOVERY_TIMEOUT_SECONDS = 1.0
"""Socket timeout for Ollama discovery probes.

Kept short so a dead daemon does not stall switcher loading. Discovery runs
off the UI loop in a worker thread and may call `/api/tags` and `/api/show`,
so this caps the worst-case wait visible to the user.
"""


# [해설] 모듈 전역 캐시들. `/reload`(→ `clear_caches`)에서 초기화된다.
# [해설] `_available_models_cache`: 모델 목록, `_builtin_providers_cache`: langchain 레지스트리, `_default_config_cache`: `ModelConfig.load()` 결과,
# [해설] `_provider_profiles_cache`: 패키지별 `_PROFILES` dict(락으로 보호).
# Module-level caches — cleared by `clear_caches()`.
_available_models_cache: dict[str, list[str]] | None = None
_builtin_providers_cache: dict[str, Any] | None = None
_default_config_cache: ModelConfig | None = None
_provider_profiles_cache: dict[str, dict[str, Any]] = {}
_provider_profiles_lock = threading.Lock()
# [해설][주의] config.toml read-modify-write 트랜잭션 전체를 감싸는 프로세스 전역 재진입 락. 모든 writer가 같은 락을 공유해야 한다.
# [해설] `configuration.writer.USER_CONFIG_WRITE_LOCK`과 동일 객체. 프로세스 간 경쟁은 범위 밖.
_config_write_lock = USER_CONFIG_WRITE_LOCK
"""Process-wide lock serializing read-modify-write transactions on `config.toml`.

Any helper that reads the file, mutates a section, and atomically replaces it
must hold this lock for the whole transaction. The atomic rename alone only
prevents torn writes; without a lock covering read-through-replace, two
overlapping writers (e.g. concurrent effort-selection workers) can each read the
same snapshot and the last `replace()` silently drops the other's change.

Because the hazard is on the whole-file replace (not per-section), *every* writer
of `config.toml` must share this one lock — a second lock guarding the same file
would not mutually exclude, so a `[effort]` write could still clobber a `[ui]`
write. `configuration.writer.update_user_config` is the preferred wrapper and
holds this same lock object; the helpers in this module still take it directly
around their own read-modify-write.

It is reentrant so a caller can hold it across several of these helpers without
self-deadlock. Cross-process races are out of scope (mirrors the existing
helpers)."""


# [해설] 로그 문구용 출처 라벨: managed 정책이 존재하면 "사용자 파일 또는 managed"로 표기해 엉뚱한 파일을 고치게 하지 않는다.
def _effective_source_label(config_path: Path) -> str:
    """Return log context for a value that may have come from either layer.

    `_load_effective_config_data` returns the *user* path alongside *merged*
    data, so "Ignoring X in ~/.deepagents/config.toml" can name a file that does
    not contain the offending value and send the reader to edit the wrong one.

    Returns:
        The user path, noting managed policy when it declares anything.
    """
    from deepagents_code.configuration.service import get_managed_snapshot

    if get_managed_snapshot().data:
        return f"{config_path} or managed config"
    return str(config_path)


# [해설] 사용자 config.toml + managed 정책을 병합한 "유효 설정" dict를 로드한다. 이 모듈 대부분의 load_* 함수가 사용.
# [해설] 호출: `configuration.service.get_config_sources`. config_path를 명시하면 managed가 빠지므로 운영 코드는 None을 넘겨야 한다.
# [해설][설계][주의] 기본 경로에서 사용자 파일이 깨져도 예외 대신 managed만 적용 — 사용자가 파일을 망가뜨려 관리자 정책을 우회하지 못하게.
def _load_effective_config_data(
    config_path: Path | None,
) -> tuple[dict[str, Any], Path]:
    """Load user TOML plus managed policy for default-path reads.

    Passing a non-`None` `config_path` excludes managed policy, so production
    callers must pass `None`. The returned path is the user file while the data
    is merged, so a caller logging about one value wants
    `_effective_source_label` rather than the bare path.

    Returns:
        Effective data and resolved user path.

    Raises:
        OSError: If an explicitly requested user TOML is present but unusable.
            A default-path read never raises for a bad user file, because
            administrator policy must still apply.
    """
    # [해설][흐름] 1) 설정 소스 로드(기본 경로면 managed 포함).
    is_default = config_path is None
    resolved_path = DEFAULT_CONFIG_PATH if config_path is None else config_path
    from deepagents_code.configuration.service import get_config_sources

    # `None` on the default path: that is what includes managed policy.
    sources = get_config_sources(user_path=None if is_default else resolved_path)
    # [해설][흐름] 2) 사용자 계층 사용 불가: 명시 경로면 OSError, 기본 경로면 경고 후 managed 데이터만 반환.
    if not sources.user.status.usable:
        detail = sources.user.status.detail or sources.user.status.health.value
        if not is_default:
            raise OSError(detail)
        # The user owns `config.toml`, so raising here would let anyone drop
        # administrator policy by writing one invalid byte into their own
        # file. Keep the managed layer, which parsed cleanly, and report the
        # user-side problem instead of failing the whole read.
        logger.warning(
            "Ignoring unusable config file %s (%s); managed policy still applies",
            resolved_path,
            detail,
        )
        return dict(sources.managed.data), resolved_path
    # [해설][흐름] 3) managed 정책이 적용되지 못한 경우(파싱 실패 등) 에러 로그로 드러낸다.
    dropped = sources.dropped_managed_detail()
    if dropped is not None:
        logger.error(
            "Managed policy from %s is not being applied: %s",
            sources.managed.status.path,
            dropped,
        )
    # [해설][흐름] 4) 기본 경로면 managed+user 병합본, 명시 경로면 user 데이터만.
    data = sources.merged()[0] if is_default else sources.user.data
    return dict(data), resolved_path


# [해설] 사용자 TOML 계층이 정상 파싱됐는지만 확인. 프로세스 수명 캐시를 가진 호출자가 "열화된 읽기"를 캐시하지 않도록 쓰인다.
def _user_config_layer_usable(config_path: Path | None = None) -> bool:
    """Return whether the user TOML layer parsed cleanly.

    `_load_effective_config_data` degrades a default-path read to managed-only
    data instead of raising, so a caller that caches its result cannot tell a
    complete read from a degraded one. Callers that cache for the process
    lifetime have to ask.

    Returns:
        Whether the user layer is usable.
    """
    from deepagents_code.configuration.service import get_config_sources

    is_default = config_path is None
    resolved_path = DEFAULT_CONFIG_PATH if is_default else config_path
    sources = get_config_sources(user_path=None if is_default else resolved_path)
    return sources.user.status.usable


# [해설] Ollama 탐색 캐시: 엔드포인트별 설치 모델 목록, 연결 거부된 엔드포인트(음성 캐시), (엔드포인트, 모델)별 프로필.
_ollama_installed_models_cache: dict[str, list[str]] = {}
_ollama_unreachable_endpoints: set[str] = set()
"""Local endpoints (trailing slash stripped) whose daemon refused the TCP
presence preflight.

Lets `_get_ollama_installed_models` negatively-cache the empty result for a
daemon that is definitively absent (connection refused) so it probes and logs
"not detected" once per reload. A *reachable* daemon that merely has no models
pulled yet -- and a daemon whose preflight is only ambiguous (a connect
timeout, which defers to the HTTP probe) -- is still re-probed (its empty
result is not cached), so a later `ollama pull` is discovered without
`/reload`. Cleared by `clear_caches()`."""
_ollama_model_profiles_cache: dict[tuple[str, str], dict[str, Any]] = {}
# [해설] `get_model_profiles` 결과 캐시(오버라이드 없는 경우 / CLI 프로필 오버라이드 해시별).
_profiles_cache: Mapping[str, ModelProfileEntry] | None = None
_profiles_override_cache: tuple[int, Mapping[str, ModelProfileEntry]] | None = None


# [해설] 모든 모듈 캐시 초기화. 호출자: `/reload` 명령과 테스트. thread 설정 캐시도 함께 무효화한다.
def clear_caches() -> None:
    """Reset module-level caches so the next call recomputes from scratch.

    Intended for tests and for the `/reload` command.
    """
    global _available_models_cache, _builtin_providers_cache, _default_config_cache, _profiles_cache, _profiles_override_cache  # noqa: PLW0603, E501  # Module-level caches require global statement
    _available_models_cache = None
    _builtin_providers_cache = None
    _default_config_cache = None
    _provider_profiles_cache.clear()
    _ollama_installed_models_cache.clear()
    _ollama_unreachable_endpoints.clear()
    _ollama_model_profiles_cache.clear()
    _profiles_cache = None
    _profiles_override_cache = None
    # The thread config cache holds `[threads]` from the same file. Its read
    # path deliberately has no invalidator (see `load_thread_config`), so
    # `/reload` is the only thing that picks up a hand edit -- dropping this
    # call left one cache serving the pre-reload file for the process lifetime.
    invalidate_thread_config_cache()


# [해설] config.toml을 쓴 직후 호출: 이 모듈의 `ModelConfig` 캐시 + 공유 resolver 캐시 + thread 캐시를 함께 비운다.
# [해설][주의] resolver를 갱신하지 않으면 저장한 설정이 프로세스 수명 동안 반영되지 않는다.
def _invalidate_config_caches(config_path: Path) -> None:
    """Drop cached views of `config.toml` after a committed write.

    Two caches hold the file: this module's `[models]` snapshot, and the shared
    process resolver every manifest reader resolves against. A writer that
    clears only the first leaves the resolver serving the pre-write generation
    for the life of the process, so the saved preference never takes effect.

    Args:
        config_path: Path the caller just wrote.
    """
    global _default_config_cache  # noqa: PLW0603  # Module-level cache requires global statement
    _default_config_cache = None
    from deepagents_code.configuration.writer import refresh_shared_resolver

    refresh_shared_resolver(config_path)
    invalidate_thread_config_cache()


# [해설] langchain `init_chat_model`이 아는 내장 프로바이더 레지스트리를 가져온다(신/구 속성명 모두 지원).
# [해설][SDK] `langchain.chat_models.base._BUILTIN_PROVIDERS`(구 `_SUPPORTED_PROVIDERS`) — 비공개 API 의존.
def _get_builtin_providers() -> dict[str, Any]:
    """Return langchain's built-in provider registry.

    Tries the newer `_BUILTIN_PROVIDERS` name first, then falls back to
    the legacy `_SUPPORTED_PROVIDERS` for older langchain versions.

    Results are cached after the first call; use `clear_caches()` to reset.

    Returns:
        The provider registry dict from `langchain.chat_models.base`.
    """
    global _builtin_providers_cache  # noqa: PLW0603  # Module-level cache requires global statement
    if _builtin_providers_cache is not None:
        return _builtin_providers_cache

    # Deferred: langchain.chat_models pulls in heavy provider registry,
    # only needed when resolving provider names for model config.
    from langchain.chat_models import base

    registry: dict[str, Any] | None = getattr(base, "_BUILTIN_PROVIDERS", None)
    if registry is None:
        registry = getattr(base, "_SUPPORTED_PROVIDERS", None)
    _builtin_providers_cache = registry if registry is not None else {}
    return _builtin_providers_cache


# [해설] 레지스트리의 각 프로바이더 패키지에서 `<package>.data._profiles` 모듈 경로를 유도한다.
# [해설] 모델 목록·프로필(컨텍스트 한도 등)을 패키지에 동봉된 데이터에서 읽기 위한 준비 단계.
def _get_provider_profile_modules() -> list[tuple[str, str]]:
    """Build a `(provider, profile_module)` list from langchain's provider registry.

    Reads the built-in provider registry from `langchain.chat_models.base`
    to discover every provider that `init_chat_model` knows about, then derives
    the `<package>.data._profiles` module path for each.

    Returns:
        List of `(provider_name, profile_module_path)` tuples.
    """
    providers = _get_builtin_providers()

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for provider_name, (module_path, *_rest) in providers.items():
        package_root = module_path.split(".", maxsplit=1)[0]
        profile_module = f"{package_root}.data._profiles"
        key = (provider_name, profile_module)
        if key not in seen:
            seen.add(key)
            result.append((provider_name, profile_module))

    return result


# [해설] 프로바이더 패키지의 `data/_profiles.py` 파일만 직접 로드해 `_PROFILES`를 얻는다(패키지 전체 import 회피로 시작 속도 확보).
# [해설] 호출자: `_discover_available_models`, `get_model_profiles`. 결과는 모듈 경로별 캐시.
def _load_provider_profiles(module_path: str) -> dict[str, Any]:
    """Load `_PROFILES` from a provider's data module.

    Results are cached by `module_path` so repeated calls (e.g., from both
    `get_available_models` and `get_model_profiles`) reuse the same dict.
    Use `clear_caches()` to reset.

    Locates the package on disk with `importlib.util.find_spec` and loads *only*
    the `_profiles.py` file via `spec_from_file_location`.

    Args:
        module_path: Dotted module path (e.g., `"langchain_openai.data._profiles"`).

    Returns:
        The `_PROFILES` dictionary from the module, or an empty dict if
            the module has no such attribute.

    Raises:
        ImportError: If the package is not installed or the profile module
            cannot be found on disk.
    """
    with _provider_profiles_lock:
        cached = _provider_profiles_cache.get(module_path)
        if cached is not None:  # `is not None` so empty profile dicts are cached
            return cached

        # [해설][흐름] 1) `find_spec`으로 패키지 설치 위치만 찾는다(import하지 않음).
        parts = module_path.split(".")
        package_root = parts[0]

        spec = importlib.util.find_spec(package_root)
        if spec is None:
            msg = f"Package {package_root} is not installed"
            raise ImportError(msg)

        # Determine the package directory from the spec.
        if spec.origin:
            package_dir = Path(spec.origin).parent
        elif spec.submodule_search_locations:
            package_dir = Path(next(iter(spec.submodule_search_locations)))
        else:
            msg = f"Cannot determine location for {package_root}"
            raise ImportError(msg)

        # [해설][흐름] 2) `data/_profiles.py` 파일 경로 조립·존재 확인.
        # Build the path to the target file (e.g., data/_profiles.py).
        relative_parts = parts[1:]  # ["data", "_profiles"]
        profiles_path = package_dir.joinpath(
            *relative_parts[:-1], f"{relative_parts[-1]}.py"
        )

        if not profiles_path.exists():
            msg = f"Profile module not found: {profiles_path}"
            raise ImportError(msg)

        # [해설][흐름] 3) 해당 파일만 모듈로 실행해 `_PROFILES`를 읽고 캐시.
        file_spec = importlib.util.spec_from_file_location(module_path, profiles_path)
        if file_spec is None or file_spec.loader is None:
            msg = f"Could not create module spec for {profiles_path}"
            raise ImportError(msg)

        module = importlib.util.module_from_spec(file_spec)
        file_spec.loader.exec_module(module)
        profiles = getattr(module, "_PROFILES", {})
        _provider_profiles_cache[module_path] = profiles
        return profiles


# [해설] `class_path`(`pkg.mod:Class`)에서 같은 패키지의 `_profiles` 모듈 경로를 추정한다(커스텀 클래스 프로바이더의 모델 목록용).
def _profile_module_from_class_path(class_path: str) -> str | None:
    """Derive the profile module path from a `class_path` config value.

    Args:
        class_path: Fully-qualified class in `module.path:ClassName` format.

    Returns:
        Dotted module path like `langchain_baseten.data._profiles`, or None
            if `class_path` is malformed.
    """
    if ":" not in class_path:
        return None
    module_part, _ = class_path.split(":", 1)
    package_root = module_part.split(".", maxsplit=1)[0]
    if not package_root:
        return None
    return f"{package_root}.data._profiles"


# [해설] `/model` 선택기 등에 쓰는 "사용 가능한 모델 목록"(allowlist 적용, 캐시). 실제 수집은 `_discover_available_models`.
def get_available_models() -> dict[str, list[str]]:
    """Get available models dynamically from installed LangChain provider packages.

    Imports model profiles from each provider package and extracts model names.

    Results are cached after the first call; use `clear_caches()` to reset.

    Returns:
        Dictionary mapping provider names to lists of model identifiers.
            Includes providers from the langchain registry, config-file
            providers with explicit model lists, and `class_path` providers
            whose packages expose a `_profiles` module.
    """
    global _available_models_cache  # noqa: PLW0603  # Module-level cache requires global statement
    if _available_models_cache is not None:
        return _available_models_cache

    available = _discover_available_models(apply_allowlist=True)
    _available_models_cache = available
    return available


# [해설] 모델 목록 수집 본체. 출처 4단계를 합친다: langchain 레지스트리 패키지 프로필 → config.toml 프로바이더
# [해설] (→ class_path 패키지 프로필) → Ollama 데몬 탐색 → openai 모델 중 Codex 미러링. 마지막에 allowlist 필터(옵션).
# [해설] 호출자: `get_available_models`(필터 on), `get_discovered_models`(필터 off — `provider:*` 와일드카드 확장용).
def _discover_available_models(*, apply_allowlist: bool) -> dict[str, list[str]]:
    """Discover the model lineup before any allowlist filtering.

    This is the `get_available_models` body with the policy filter factored
    out so allowlist machinery can expand `provider:*` wildcards against the
    discovered lineup without recursing back into its own filter.

    Args:
        apply_allowlist: Filter each provider's models through the active
            `models.allowed` policy. Only allowlist internals pass `False`.

    Returns:
        Dictionary mapping provider names to lists of model identifiers.
    """
    # [해설][흐름] 1) 레지스트리 프로바이더: 비활성화(`enabled=false`) 건너뛰고, 설치 안 된 패키지는 조용히 skip.
    available: dict[str, list[str]] = {}
    config = ModelConfig.load()

    # Try to load from langchain provider profile data.
    # Build the list dynamically from langchain's supported-provider registry
    # so new providers are picked up automatically when langchain adds them.
    provider_modules = _get_provider_profile_modules()
    registry_providers: set[str] = set()

    for provider, module_path in provider_modules:
        registry_providers.add(provider)
        # Skip providers explicitly disabled in config.
        if not config.is_provider_enabled(provider):
            logger.debug(
                "Provider '%s' is disabled in config; skipping registry discovery",
                provider,
            )
            continue
        try:
            profiles = _load_provider_profiles(module_path)
        except ImportError:
            logger.debug(
                "Could not import profiles from %s (package may not be installed)",
                module_path,
            )
            continue
        except Exception:
            logger.warning(
                "Failed to load profiles from %s, skipping provider '%s'",
                module_path,
                provider,
                exc_info=True,
            )
            continue

        # [해설][설계] 코딩 에이전트는 툴 호출이 필수이므로 tool_calling 지원 + 텍스트 입출력 모델만 선택기에 노출한다.
        # Filter to models that support tool calling and text I/O.
        models = [
            name
            for name, profile in profiles.items()
            if profile.get("tool_calling", False)
            and profile.get("text_inputs", True) is not False
            and profile.get("text_outputs", True) is not False
        ]

        models.sort()
        if models:
            available[provider] = models

    # [해설][흐름] 2) config.toml `[models.providers.*]`의 `models` 목록 병합(없으면 class_path 패키지 프로필에서 자동 발견).
    # Merge in models from config file (custom providers like ollama, fireworks)
    for provider_name, provider_config in config.providers.items():
        # Respect enabled = false (hide provider entirely).
        if not config.is_provider_enabled(provider_name):
            logger.debug(
                "Provider '%s' is disabled in config; skipping",
                provider_name,
            )
            continue

        config_models = list(provider_config.get("models", []))

        # For class_path providers not in the built-in registry, auto-discover
        # models from the package's _profiles.py when no explicit models list.
        if (
            not config_models
            and provider_name not in registry_providers
            and provider_name not in available
        ):
            class_path = provider_config.get("class_path", "")
            profile_module = _profile_module_from_class_path(class_path)
            if profile_module:
                try:
                    profiles = _load_provider_profiles(profile_module)
                except ImportError:
                    logger.debug(
                        "Could not import profiles from %s for class_path "
                        "provider '%s' (package may not be installed)",
                        profile_module,
                        provider_name,
                    )
                except Exception:
                    logger.warning(
                        "Failed to load profiles from %s for class_path provider '%s'",
                        profile_module,
                        provider_name,
                        exc_info=True,
                    )
                else:
                    config_models = sorted(
                        name
                        for name, profile in profiles.items()
                        if profile.get("tool_calling", False)
                        and profile.get("text_inputs", True) is not False
                        and profile.get("text_outputs", True) is not False
                    )

        if provider_name not in available:
            if config_models:
                available[provider_name] = config_models
        else:
            # Append any config models not already discovered
            existing = set(available[provider_name])
            for model in config_models:
                if model not in existing:
                    available[provider_name].append(model)

    # [해설][흐름] 3) Ollama: 프로필 데이터가 없으므로 데몬에 설치 모델을 물어 병합(config 순서 유지, 발견분 뒤에 추가).
    # [해설]    조건: `OLLAMA_DISCOVERY` 미비활성 + 레지스트리에 ollama + 활성화 + `langchain_ollama` 설치.
    # `langchain-ollama` ships no profile data, so the steps above leave the
    # switcher empty unless the user hand-curates `models = [...]` in config.
    # Probe the daemon for installed models and merge them in,
    # preserving explicit config order (config wins) with discoveries appended.
    # Cached alongside the rest of `available`; refresh by
    # calling `clear_caches()` (e.g. via the `/reload` slash command).
    if (
        _ollama_discovery_enabled()
        and "ollama" in registry_providers
        and config.is_provider_enabled("ollama")
        and importlib.util.find_spec("langchain_ollama") is not None
    ):
        endpoint = _get_provider_endpoint("ollama", config)
        discovered = _get_ollama_installed_models(endpoint)
        if discovered:
            available["ollama"] = list(
                dict.fromkeys([*available.get("ollama", []), *discovered])
            )
        else:
            logger.debug(
                "Ollama discovery returned no models for %s; "
                "daemon may be down or have no pulls",
                endpoint or OLLAMA_DEFAULT_BASE_URL,
            )

    # [해설][흐름] 4) Codex 미러링: openai 목록 ∩ `CODEX_MODELS`를 `openai_codex`로 복제하고 dict 순서를 openai 바로 뒤로 재배치.
    # Mirror the curated `CODEX_MODELS` subset of `openai` models under a
    # dedicated `openai_codex` provider entry so the switcher offers them under
    # their own ChatGPT-OAuth auth context. Eligibility is filtered by the
    # allowlist because the Codex backend serves a narrower lineup than the
    # full `openai` API and rejects unsupported models at call time.
    if config.is_provider_enabled(CODEX_PROVIDER):
        openai_models = available.get("openai")
        if openai_models:
            mirrored = [name for name in openai_models if name in CODEX_MODELS]
            codex_models = list(
                dict.fromkeys([*available.get(CODEX_PROVIDER, []), *mirrored])
            )
            # Place `openai_codex` directly after `openai` so the switcher
            # keeps the two OpenAI-backed providers adjacent (codex before
            # azure_openai etc.) instead of trailing it at the end of the
            # dict. dict insertion order is the switcher's display order, so
            # rebuild the dict, dropping any prior codex entry and re-inserting
            # it right after `openai`.
            reordered: dict[str, list[str]] = {}
            for name, models in available.items():
                if name == CODEX_PROVIDER:
                    continue
                reordered[name] = models
                if name == "openai":
                    reordered[CODEX_PROVIDER] = codex_models
            available = reordered

    # [해설][흐름] 5) allowlist 적용: 허용되지 않은 모델 제거, 비게 된 프로바이더 제거.
    if apply_allowlist and config.allowed_models is not None:
        available = {
            provider: [
                model
                for model in models
                if config.is_model_allowed(f"{provider}:{model}")
            ]
            for provider, models in available.items()
        }
        available = {
            provider: models for provider, models in available.items() if models
        }

    return available


# [해설] 정책 필터 없이 한 프로바이더의 발견 모델 목록. allowlist의 `provider:*` 확장이 재귀 없이 쓰기 위한 진입점.
# [해설][주의] 캐시되지 않으므로 호출할 때마다 프로필 로드·Ollama 탐색이 다시 일어날 수 있다(모듈 캐시가 일부 흡수).
def get_discovered_models(provider_name: str) -> list[str]:
    """Get the discovered lineup for one provider, unfiltered by policy.

    `get_available_models()` applies `models.allowed` itself, so allowlist
    machinery expanding a `provider:*` wildcard cannot call it without
    recursing. This reads the same discovery result with the filter off.

    Args:
        provider_name: The provider whose models to list.

    Returns:
        Model identifiers discovery knows about, empty when the provider
            declares none and none were discovered.
    """
    return _discover_available_models(apply_allowlist=False).get(provider_name, [])


# [해설] 프로필 병합 규칙: 업스트림 < config.toml `profile` < `--profile-override`. 덮어쓴 키 집합도 기록(UI 표시용).
def _build_entry(
    base: dict[str, Any],
    overrides: dict[str, Any],
    cli_override: dict[str, Any] | None,
) -> ModelProfileEntry:
    """Build a profile entry by merging base, overrides, and app override.

    Args:
        base: Upstream profile dict (empty for config-only models).
        overrides: `config.toml` profile overrides.
        cli_override: Extra fields from `--profile-override`.

    Returns:
        Profile entry with merged data and override tracking.
    """
    merged = {**base, **overrides}
    overridden_keys = set(overrides)
    if cli_override:
        merged = {**merged, **cli_override}
        overridden_keys |= set(cli_override)
    return ModelProfileEntry(
        profile=merged,
        overridden_keys=frozenset(overridden_keys),
    )


# [해설] `provider:model` → 병합 프로필(컨텍스트 한도 등). 모델 선택기 표시와 `config.create_model`의 프로필 적용에 쓰인다(추정).
# [해설] `get_available_models`와 달리 capability 필터가 없다. 캐시는 오버라이드 없는 것 1개 + `id(cli_override)` 단일 슬롯.
# [해설][주의] `id()` 키 캐시라 호출자가 같은 dict 객체를 재사용해야 적중한다.
def get_model_profiles(
    *,
    cli_override: dict[str, Any] | None = None,
) -> Mapping[str, ModelProfileEntry]:
    """Load upstream profiles merged with config.toml overrides.

    Keyed by `provider:model` spec string. Each entry contains the
    merged profile dict and the set of keys overridden by config.toml.

    Unlike `get_available_models()`, this includes all models from upstream
    profiles regardless of capability filters (tool calling, text I/O).

    Results are cached; use `clear_caches()` to reset. When `cli_override` is
    provided the result is stored in a single-slot cache keyed by
    `id(cli_override)`. This relies on the caller retaining the same dict
    object for the session (the app stores it once on the app instance);
    passing a different dict with the same contents will bypass the cache
    and overwrite the previous entry.

    Args:
        cli_override: Extra profile fields from `--profile-override`.

            When provided, these are merged on top of every profile entry
            (after upstream + config.toml) and their keys are added to
            `overridden_keys`.

    Returns:
        Read-only mapping of spec strings to profile entries.
    """
    # [해설][흐름] 1) 캐시 적중 확인.
    global _profiles_cache, _profiles_override_cache  # noqa: PLW0603  # Module-level caches require global statement
    if cli_override is None and _profiles_cache is not None:
        return _profiles_cache
    if cli_override is not None and _profiles_override_cache is not None:
        cached_id, cached_result = _profiles_override_cache
        if cached_id == id(cli_override):
            return cached_result

    # [해설][흐름] 2) 레지스트리 패키지 프로필 수집 + openai→openai_codex 미러링.
    result: dict[str, ModelProfileEntry] = {}
    config = ModelConfig.load()

    # Collect upstream profiles from provider packages.
    seen_specs: set[str] = set()
    provider_modules = _get_provider_profile_modules()
    registry_providers: set[str] = set()
    for provider, module_path in provider_modules:
        registry_providers.add(provider)
        # Skip providers explicitly disabled in config.
        if not config.is_provider_enabled(provider):
            logger.debug(
                "Provider '%s' is disabled in config; skipping profiles",
                provider,
            )
            continue
        try:
            profiles = _load_provider_profiles(module_path)
        except ImportError:
            logger.debug(
                "Could not import profiles from %s for provider '%s'",
                module_path,
                provider,
            )
            continue
        except Exception:
            logger.warning(
                "Failed to load profiles from %s for provider '%s'",
                module_path,
                provider,
                exc_info=True,
            )
            continue

        for model_name, upstream_profile in profiles.items():
            spec = f"{provider}:{model_name}"
            seen_specs.add(spec)
            overrides = config.get_profile_overrides(provider, model_name=model_name)
            result[spec] = _build_entry(upstream_profile, overrides, cli_override)
            # Mirror the curated `CODEX_MODELS` subset of openai profiles under
            # the `openai_codex` provider so `/model openai_codex:<model>`
            # resolves to the same upstream profile without duplicating data.
            # Filtered by the allowlist — see the note in `get_available_models`.
            if (
                provider == "openai"
                and model_name in CODEX_MODELS
                and config.is_provider_enabled(CODEX_PROVIDER)
            ):
                codex_spec = f"{CODEX_PROVIDER}:{model_name}"
                seen_specs.add(codex_spec)
                codex_overrides = config.get_profile_overrides(
                    CODEX_PROVIDER, model_name=model_name
                )
                result[codex_spec] = _build_entry(
                    upstream_profile, codex_overrides, cli_override
                )

    # [해설][흐름] 3) config 전용 모델·class_path 패키지 프로필 추가(업스트림 없는 모델은 빈 base에 overrides만).
    # Add config-only models and class_path provider profiles.
    for provider_name, provider_config in config.providers.items():
        if not config.is_provider_enabled(provider_name):
            logger.debug(
                "Provider '%s' is disabled in config; skipping profiles",
                provider_name,
            )
            continue
        # For class_path providers not in the built-in registry, load
        # upstream profiles from the package's _profiles.py.
        if provider_name not in registry_providers:
            class_path = provider_config.get("class_path", "")
            profile_module = _profile_module_from_class_path(class_path)
            if profile_module:
                try:
                    pkg_profiles = _load_provider_profiles(profile_module)
                except ImportError:
                    logger.debug(
                        "Could not import profiles from %s for class_path "
                        "provider '%s' (package may not be installed)",
                        profile_module,
                        provider_name,
                    )
                except Exception:
                    logger.warning(
                        "Failed to load profiles from %s for class_path provider '%s'",
                        profile_module,
                        provider_name,
                        exc_info=True,
                    )
                else:
                    for model_name, upstream_profile in pkg_profiles.items():
                        spec = f"{provider_name}:{model_name}"
                        seen_specs.add(spec)
                        overrides = config.get_profile_overrides(
                            provider_name, model_name=model_name
                        )
                        result[spec] = _build_entry(
                            upstream_profile, overrides, cli_override
                        )

        config_models = provider_config.get("models", [])
        for model_name in config_models:
            spec = f"{provider_name}:{model_name}"
            if spec not in seen_specs:
                overrides = config.get_profile_overrides(
                    provider_name, model_name=model_name
                )
                result[spec] = _build_entry({}, overrides, cli_override)

    # [해설][흐름] 4) Ollama 데몬 `/api/show`로 컨텍스트 길이·capability를 채운다.
    # `langchain-ollama` does not ship static profile data. When discovery is
    # enabled, ask the daemon for model metadata so the selector can show
    # context length and capabilities for locally pulled models.
    if (
        _ollama_discovery_enabled()
        and "ollama" in registry_providers
        and config.is_provider_enabled("ollama")
        and importlib.util.find_spec("langchain_ollama") is not None
    ):
        endpoint = _get_provider_endpoint("ollama", config)
        discovered_model_names = _get_ollama_installed_models(endpoint)
        configured_model_names = [
            spec.removeprefix("ollama:")
            for spec in result
            if spec.startswith("ollama:")
        ]
        model_names = list(
            dict.fromkeys([*configured_model_names, *discovered_model_names])
        )
        if model_names:
            discovered_profiles = _fetch_ollama_installed_model_profiles(
                endpoint,
                model_names,
            )
            for model_name in model_names:
                profile = discovered_profiles.get(model_name, {})
                spec = f"ollama:{model_name}"
                existing = result.get(spec)
                base = dict(existing["profile"]) if existing is not None else {}
                base.update(profile)
                overrides = config.get_profile_overrides(
                    "ollama", model_name=model_name
                )
                result[spec] = _build_entry(base, overrides, cli_override)
                seen_specs.add(spec)

    # [해설][흐름] 5) 읽기 전용 `MappingProxyType`으로 동결 후 캐시에 저장.
    frozen = MappingProxyType(result)
    if cli_override is None:
        _profiles_cache = frozen
    else:
        _profiles_override_cache = (id(cli_override), frozen)
    return frozen


# [해설] "로컬 머신"으로 간주하는 호스트명. Ollama 키 전달 허용 여부와 TCP 사전 점검 대상 판정에 사용.
_LOCAL_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",  # noqa: S104  # hostname comparison, not socket binding
    }
)


# [해설] 엔드포인트가 로컬인지 판정. URL이 비어 있으면 기본값(localhost)을 쓸 것이므로 True.
# [해설][주의] 보안 경계: 이 값이 True일 때만 `OLLAMA_API_KEY`가 탐색 요청 헤더로 전송된다(`_ollama_discovery_headers`).
def _is_local_endpoint(url: object) -> bool:
    """Return whether a provider endpoint points at the local machine.

    Accepts `object` rather than `str | None` because the endpoint originates
    from untyped TOML; the `isinstance` guard below defends against drift.
    """
    if not url:
        return True
    if not isinstance(url, str):
        return False

    # Bare hostname literal (no scheme, no port) — short-circuit so IPv6
    # forms like `::1` don't get misparsed by urlparse.
    if url in _LOCAL_HOSTNAMES:
        return True

    candidate = url if "://" in url else f"http://{url}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    return parsed.hostname in _LOCAL_HOSTNAMES


# [해설] 엔드포인트 결정: `ModelConfig.get_base_url`(config/env/저장) → 프로바이더 host env(`OLLAMA_HOST`) 순.
def _get_provider_endpoint(provider: str, config: ModelConfig) -> str | None:
    """Return a provider endpoint from config or provider-specific env vars."""
    base_url = config.get_base_url(provider)
    if base_url:
        return base_url

    host_env = PROVIDER_HOST_ENV.get(provider)
    if not host_env:
        return None
    return resolve_env_var(host_env)


# [해설] `OLLAMA_DISCOVERY` env 값 해석용 참/거짓 문자열 집합.
_OLLAMA_DISCOVERY_FALSY: frozenset[str] = frozenset({"0", "false", "no", "off"})
"""Normalized values that disable Ollama discovery when set in `OLLAMA_DISCOVERY`."""

_OLLAMA_DISCOVERY_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})
"""Normalized values that enable Ollama discovery when set in `OLLAMA_DISCOVERY`."""


# [해설] Ollama 탐색 on/off(기본 on). env 이름 자체가 `DEEPAGENTS_CODE_OLLAMA_DISCOVERY`(`_env_vars.OLLAMA_DISCOVERY`)라 이중 접두 조회는 없다.
# [해설][주의] `config_manifest.py`에 `models.ollama_discovery` TOML 키가 같은 env로 선언돼 있지만, 이 함수는 env만 읽는다(TOML 값 반영 여부는 이 함수 밖에서 결정).
def _ollama_discovery_enabled() -> bool:
    """Return whether Ollama model/profile discovery may run.

    Defaults to enabled. Opt out via `_env_vars.OLLAMA_DISCOVERY` set to a
    falsy value (`0`, `false`, `no`, `off`); truthy values (`1`, `true`,
    `yes`, `on`) explicitly enable. Unrecognized values warn and fall through
    to the default because the user clearly tried to configure something.
    """
    raw = resolve_env_var(_env_vars.OLLAMA_DISCOVERY)
    if raw is None:
        return True
    normalized = raw.strip().lower()
    if normalized in _OLLAMA_DISCOVERY_FALSY:
        return False
    if normalized in _OLLAMA_DISCOVERY_TRUTHY:
        return True
    logger.warning(
        "Unrecognized value for %s: %r; expected one of %s. Defaulting to enabled.",
        _env_vars.OLLAMA_DISCOVERY,
        raw,
        sorted(_OLLAMA_DISCOVERY_FALSY | _OLLAMA_DISCOVERY_TRUTHY),
    )
    return True


# [해설] 엔드포인트별 설치 모델 캐시 래퍼. 모델이 있거나 "연결 거부"로 확정된 경우만 캐시해서
# [해설] 이후 `ollama pull`한 모델은 `/reload` 없이도 발견되게 한다.
def _get_ollama_installed_models(endpoint: str | None) -> list[str]:
    """Return cached Ollama model names for `endpoint`.

    The result is cached when the daemon returns models, and also when a local
    daemon definitively refuses the TCP presence preflight, so the two startup
    callers (`get_available_models` and `get_model_profiles`) share a single
    probe and a single "not detected" log line per reload. A reachable daemon
    that reports no models -- and one whose preflight is merely ambiguous (a
    connect timeout) -- is left uncached so a later pull can still be discovered
    without `/reload`.

    Args:
        endpoint: Base URL of the Ollama daemon. When `None`, defaults to
            `OLLAMA_DEFAULT_BASE_URL`.

    Returns:
        Sorted list of model names reported by `/api/tags`.
    """
    key = (endpoint or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
    cached = _ollama_installed_models_cache.get(key)
    if cached is not None:
        return list(cached)
    models = _fetch_ollama_installed_models(endpoint)
    if models or key in _ollama_unreachable_endpoints:
        _ollama_installed_models_cache[key] = models
    return list(models)


# [해설] Ollama 데몬 존재 여부를 TCP 연결로 빠르게 확인하는 사전 점검. 거부=False(음성 캐시 가능), 타임아웃/판단불가=True(HTTP 프로브에 위임).
def _ollama_host_reachable(
    base: str, *, timeout: float = OLLAMA_DISCOVERY_TIMEOUT_SECONDS
) -> bool:
    """Return whether a TCP listener appears to accept connections at `base`.

    A lightweight presence preflight so Ollama discovery can skip the HTTP
    probe entirely when no daemon is running (e.g. Ollama is not installed).
    The check opens and immediately closes a TCP connection to the endpoint's
    host and port. A *definitive* failure -- connection refused, DNS error, or
    sockets blocked under `pytest-socket` -- reports "not reachable" so
    discovery falls back gracefully (and the caller may negatively cache it). A
    *connect timeout* is ambiguous -- a present-but-slow or still-booting daemon
    times out just like an absent one -- so it defers to the HTTP probe
    (reports "reachable") rather than being cached as absent. An unexpected
    (non-`OSError`) failure is additionally logged at warning so a real bug
    isn't misreported as absence.

    Args:
        base: Base URL of the Ollama daemon, e.g. `http://localhost:11434`.
        timeout: Socket connection timeout in seconds.

    Returns:
        `True` when a connection is established (a daemon appears present) or
            when presence cannot be determined -- unparseable target or a
            connect timeout -- so the caller defers to the HTTP probe; `False`
            when the connection is definitively refused.
    """
    # [해설][흐름] 1) URL에서 host/port 추출. 파싱 불가하면 판단을 HTTP 프로브에 넘긴다.
    import socket

    parsed = urlparse(base)
    host = parsed.hostname
    if not host:
        # Can't determine a target host; let the HTTP probe make the decision.
        return True
    try:
        port = parsed.port
    except ValueError:
        # Malformed port (out of range / non-numeric); defer to the HTTP probe.
        return True
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    # Expected transport failures split by how definitive they are. A refusal
    # (`ECONNREFUSED` and friends -- an `OSError`) is a fast, certain "nothing
    # is listening", so it reports absent and lets the caller negatively cache
    # it. A connect *timeout* is ambiguous (present-but-slow vs. absent-and-
    # firewalled), so it defers to the HTTP probe rather than being cached as
    # absent and stuck until the next reload. `TimeoutError` is an `OSError`
    # subclass, so its branch must precede the broad `OSError` one. Anything
    # non-`OSError` is surfaced at warning so a real bug isn't misreported as
    # "not detected"; `pytest-socket`'s `SocketBlockedError` inherits from
    # `Exception` (not `OSError`), so the broad branch catches it. The socket
    # is its own context manager, so `with` closes the probe connection.
    # [해설][흐름] 2) 연결 시도. 예외 분기 순서(TimeoutError가 OSError 하위이므로 먼저)가 의미를 가진다.
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except TimeoutError:
        return True
    except OSError:
        return False
    except Exception as exc:  # noqa: BLE001  # see comment above
        logger.warning(
            "Ollama presence preflight raised unexpected %s for %s: %s",
            type(exc).__name__,
            base,
            exc,
        )
        return False


# [해설] `GET /api/tags`로 설치 모델 이름 목록 조회(best-effort, 실패 시 빈 리스트).
# [해설] 호출자: `_get_ollama_installed_models`. 네트워크 I/O가 있으므로 UI 루프 밖 워커 스레드에서 도는 것이 전제.
def _fetch_ollama_installed_models(
    endpoint: str | None,
    *,
    timeout: float = OLLAMA_DISCOVERY_TIMEOUT_SECONDS,
) -> list[str]:
    """Discover models installed in a local or hosted Ollama daemon.

    Issues a `GET {endpoint}/api/tags` and returns the sorted list of model
    names reported by the daemon. The probe is best-effort: any error
    (timeout, connection refused, malformed JSON) yields an empty list and is
    logged at debug level so the model switcher can fall back gracefully.

    When probing a local endpoint and `OLLAMA_API_KEY` (or the
    `DEEPAGENTS_CODE_`-prefixed variant) is set, its value is forwarded as a
    `Bearer` token. Discovery never forwards credentials to non-local endpoints.

    Args:
        endpoint: Base URL of the Ollama daemon. When `None`, defaults to
            `OLLAMA_DEFAULT_BASE_URL`. A trailing `/` is tolerated.
        timeout: Socket timeout in seconds.

    Returns:
        Sorted list of model names; empty when the daemon is unreachable or
            returns no models.
    """
    import json
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    # [해설][흐름] 1) 스킴 검증(http/https만) — `urlopen`에 file:// 등이 들어가는 것을 막는 보안 가드이기도 하다.
    base = (endpoint or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
    if not base.startswith(("http://", "https://")):
        logger.warning(
            "Skipping Ollama discovery: %r has no http:// or https:// scheme. "
            "Set base_url or OLLAMA_HOST to e.g. http://localhost:11434.",
            base,
        )
        return []

    # [해설][흐름] 2) 로컬 엔드포인트만 TCP 사전 점검. 거부되면 unreachable 집합에 넣고 종료.
    # Presence preflight (local endpoints only -- remote hosts may be reachable
    # only through a proxy that the HTTP probe honors but a raw socket does
    # not). A dead/absent daemon (the common case when Ollama is not installed)
    # refuses the connection; detecting that here lets us skip the HTTP probe
    # and log a quiet "not detected" line instead of a misleading
    # "discovery failed ... Connection refused" debug line.
    if _is_local_endpoint(base) and not _ollama_host_reachable(base, timeout=timeout):
        logger.debug("Ollama daemon not detected at %s; skipping discovery", base)
        _ollama_unreachable_endpoints.add(base)
        return []

    # [해설][흐름] 3) HTTP 요청(로컬이면 선택적 Bearer 키 포함).
    url = f"{base}/api/tags"

    headers = _ollama_discovery_headers(base, content_type=False)
    request = Request(url, headers=headers)  # noqa: S310  # scheme guarded above
    # Catch-all is intentional: discovery is best-effort and must never break
    # the model selector. The narrow tuple is fully subsumed by `Exception`
    # below; we keep it only to log expected transport failures at debug while
    # surfacing unexpected ones at warning so a real bug doesn't disappear.
    # Notably catches `pytest-socket`'s `SocketBlockedError`, which inherits
    # from `Exception` (not `OSError`) and would otherwise propagate during
    # unit tests run with `--disable-socket`. `KeyboardInterrupt` and
    # `SystemExit` derive from `BaseException` and bypass both branches.
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310  # scheme guarded above
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        logger.debug("Ollama model discovery failed for %s: %s", url, exc)
        return []
    except Exception as exc:  # noqa: BLE001  # see comment above
        logger.warning(
            "Ollama model discovery raised unexpected %s for %s: %s",
            type(exc).__name__,
            url,
            exc,
        )
        return []

    # [해설][흐름] 4) 응답 형태 검증 후 이름만 추출·정렬.
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        logger.debug(
            "Ollama discovery: %s returned unexpected payload shape (%s); "
            "endpoint may not be an Ollama daemon",
            url,
            type(payload).__name__,
        )
        return []

    names: list[str] = []
    for entry in payload["models"]:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    names.sort()
    return names


# [해설] Ollama 탐색 요청 헤더. 로컬 엔드포인트일 때만 `OLLAMA_API_KEY`를 Bearer로 붙여 원격으로 키가 새지 않게 한다.
def _ollama_discovery_headers(endpoint: str, *, content_type: bool) -> dict[str, str]:
    """Build headers for Ollama discovery requests.

    Args:
        endpoint: Base URL for the discovery request.
        content_type: Whether to include a JSON `Content-Type` header.

    Returns:
        HTTP headers including optional bearer auth for local endpoints.
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = "application/json"
    optional_env = OPTIONAL_AUTH_ENV.get("ollama")
    if optional_env and _is_local_endpoint(endpoint):
        api_key = resolve_env_var(optional_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    return headers


# [해설] TOML/JSON 값에서 양의 정수만 안전하게 추출(bool은 int 하위라 명시적으로 제외).
def _coerce_positive_int(value: object) -> int | None:
    """Return `value` as a positive integer, or `None` when unavailable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0 and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        if parsed > 0:
            return parsed
    return None


# [해설] Ollama `/api/show` 응답을 LangChain 프로필 필드로 변환: `*.context_length` 최댓값 → `max_input_tokens`,
# [해설] capabilities(completion/tools/thinking) → `text_inputs`/`text_outputs`/`tool_calling`/`reasoning_output`.
def _profile_from_ollama_show_payload(payload: object) -> dict[str, Any]:
    """Extract LangChain-style profile fields from an Ollama `/api/show` payload.

    Args:
        payload: Decoded JSON response from `POST /api/show`.

    Returns:
        Profile fields understood by the model selector, such as
        `max_input_tokens` and `tool_calling`.
    """
    if not isinstance(payload, dict):
        return {}
    payload_dict = cast("dict[str, object]", payload)

    profile: dict[str, Any] = {}
    model_info = payload_dict.get("model_info")
    if isinstance(model_info, dict):
        context_lengths = [
            length
            for key, value in model_info.items()
            if isinstance(key, str)
            and (key == "context_length" or key.endswith(".context_length"))
            and (length := _coerce_positive_int(value)) is not None
        ]
        if context_lengths:
            profile["max_input_tokens"] = max(context_lengths)

    capabilities = payload_dict.get("capabilities")
    if isinstance(capabilities, list):
        capability_names = {item for item in capabilities if isinstance(item, str)}
        if "completion" in capability_names:
            profile["text_inputs"] = True
            profile["text_outputs"] = True
        if "tools" in capability_names:
            profile["tool_calling"] = True
        if "thinking" in capability_names:
            profile["reasoning_output"] = True

    if not profile and ("model_info" in payload_dict or "capabilities" in payload_dict):
        logger.debug(
            "Ollama profile discovery returned a payload with no recognized "
            "profile fields; top-level keys: %s",
            sorted(str(key) for key in payload_dict),
        )

    return profile


# [해설] 모델별 `POST /api/show`로 프로필 수집(모델 하나 실패해도 계속). 성공 결과는 (엔드포인트, 모델) 캐시.
# [해설] 호출자: `get_model_profiles`.
# [해설][주의] 이 경로는 TCP 사전 점검을 하지 않고 스킴만 검사한다(설치 목록 조회 쪽에서 이미 걸러졌다는 전제, 추정).
def _fetch_ollama_installed_model_profiles(
    endpoint: str | None,
    model_names: list[str],
    *,
    timeout: float = OLLAMA_DISCOVERY_TIMEOUT_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Discover profile metadata for installed Ollama models.

    Issues `POST {endpoint}/api/show` for each model. The probe is best-effort:
    failures for one model are logged and do not stop profile discovery for the
    remaining models.

    Args:
        endpoint: Base URL of the Ollama daemon. When `None`, defaults to
            `OLLAMA_DEFAULT_BASE_URL`. A trailing `/` is tolerated.
        model_names: Model names to inspect.
        timeout: Socket timeout in seconds.

    Returns:
        Mapping of model name to extracted profile fields.
    """
    import json
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    base = (endpoint or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
    if not base.startswith(("http://", "https://")):
        logger.warning(
            "Skipping Ollama profile discovery: %r has no http:// or https:// scheme. "
            "Set base_url or OLLAMA_HOST to e.g. http://localhost:11434.",
            base,
        )
        return {}

    url = f"{base}/api/show"
    profiles: dict[str, dict[str, Any]] = {}
    headers = _ollama_discovery_headers(base, content_type=True)

    for model_name in model_names:
        cache_key = (base, model_name)
        cached = _ollama_model_profiles_cache.get(cache_key)
        if cached is not None:
            profiles[model_name] = dict(cached)
            continue

        body = json.dumps({"model": model_name}).encode("utf-8")
        request = Request(  # noqa: S310  # scheme guarded above
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310  # scheme guarded above
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            logger.debug(
                "Ollama profile discovery failed for %s via %s: %s",
                model_name,
                url,
                exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001  # see _fetch_ollama_installed_models
            logger.warning(
                "Ollama profile discovery raised unexpected %s for %s via %s: %s",
                type(exc).__name__,
                model_name,
                url,
                exc,
            )
            continue

        profile = _profile_from_ollama_show_payload(payload)
        if profile:
            _ollama_model_profiles_cache[cache_key] = profile
            profiles[model_name] = profile

    return profiles


# [해설] `/auth`로 저장된 키 존재 여부만 확인(값은 반환하지 않음). `auth.json` 손상은 "없음"으로 흡수해 선택기가 계속 동작하게 한다.
# [해설] 호출자: `_resolve_configured`. 저장소 구현: `auth_store.get_stored_key`.
def _has_stored_credential(provider: str) -> bool:
    """Return whether `provider` has a credential persisted via `/auth`.

    A corrupt `auth.json` is swallowed (logged, treated as absent) so the
    model selector and other read-side callers can keep listing providers.
    The user-visible signal lives in `AuthManagerScreen` — opening `/auth`
    surfaces a corruption banner directly. Read-side resilience here means
    you can still pick a different provider while the file is broken.
    """
    try:
        return auth_store.get_stored_key(provider) is not None
    except RuntimeError:
        logger.warning(
            "Could not read stored credentials for provider %s; treating as absent",
            provider,
        )
        return False


# [해설] 프로바이더 키 "값"을 해석한다. 호출자: `config.create_model`(스코프 없는 환경에서 `apply_stored_credentials` 직후
# [해설] 호출해 결과를 생성자 kwargs `api_key`로 강제 주입), 기타 UI/검사 코드.
# [해설] 실제 순서: ① `auth_store` 저장 키 → ② `get_credential_env_var`로 얻은 이름을 `resolve_env_var`(접두 env > 표준 env)로 조회.
# [해설][문서 불일치] `docs_official/code/credentials.md` "Key resolution order"는 ① `DEEPAGENTS_CODE_` 접두 env ② `/auth` 저장 키
# [해설]   ③ 표준 env 순이라고 하며 "접두 env로 저장 키를 1회성 override할 수 있다"고 예시까지 든다. 그러나 이 함수는 저장 키가
# [해설]   있으면 접두 env를 보지도 않고 저장 키를 반환한다. `config.create_model`은 이 값을 `kwargs["api_key"]`로 덮어쓰므로
# [해설]   `_get_provider_kwargs`가 접두 env에서 넣은 키보다 저장 키가 이긴다(정적 분석, 미실행 — `analysis/03-*.md` 동일 결론).
# [해설][주의] 접두 env를 존중하는 가드 `stored_key_reaches_runtime`은 서비스(Tavily/LangSmith) 경로에만 적용된다.
def resolve_provider_credential(provider: str) -> str | None:
    """Resolve the credential value for `provider` from any configured source.

    Lookup order:

    1. Stored API key in `~/.deepagents/.state/auth.json` (added via `/auth`).
    2. Canonical env var via `resolve_env_var()` (which honors the
        `DEEPAGENTS_CODE_` prefix and dotenv files).

    A user who has *both* a stored key and an env var set gets the stored
    key — entering one in the TUI is the more deliberate, more recent
    action, so "I just typed this in" beats whatever the shell exported.

    Args:
        provider: Provider name (e.g., `"anthropic"`).

    Returns:
        The credential value, or `None` when no source has one or the
        provider has no env-var mapping at all.
    """
    # [해설][흐름] 1) 저장 키 우선. 저장소 파일이 깨졌으면(RuntimeError) 경고 후 env로 폴백.
    try:
        stored = auth_store.get_stored_key(provider)
    except RuntimeError:
        logger.warning(
            "Could not read stored credentials for provider %s; falling back to env",
            provider,
        )
        stored = None
    if stored:
        return stored
    # [해설][흐름] 2) 저장 키가 없을 때만 env var(config.toml `api_key_env` > 내장 매핑)를 접두 우선 규칙으로 조회.
    env_var = get_credential_env_var(provider)
    if env_var:
        return resolve_env_var(env_var)
    return None


# [해설] LangSmith LLM Gateway(`LANGSMITH_GATEWAY` + `LANGSMITH_GATEWAY_API_KEY`)만으로 인증 가능한지 판정.
# [해설] 호출자: `get_provider_auth_status`(네이티브 키가 없을 때의 폴백). class_path 프로바이더에는 적용하면 안 된다.
# [해설][주의] 여기서는 `resolve_env_var`가 아니라 활성 환경을 직접 조회하므로 게이트웨이 env에는 접두 오버라이드가 적용되지 않는다.
def _resolve_gateway_configured(provider: str) -> ProviderAuthStatus | None:
    """Return `CONFIGURED` when LangSmith Gateway can authenticate a provider.

    Credential preflight normally requires each provider's native API key
    (for example `OPENAI_API_KEY`). Users who route traffic through the
    LangSmith LLM Gateway often set only `LANGSMITH_GATEWAY` and
    `LANGSMITH_GATEWAY_API_KEY`. Without this fallback, model selection and
    startup treat those models as missing credentials even though the
    gateway-aware chat integration will authenticate the request.

    Example:
        A user enables the gateway with:

            LANGSMITH_GATEWAY=true
            LANGSMITH_GATEWAY_API_KEY=lsv2_...

        and has no `OPENAI_API_KEY`. Selecting `openai:gpt-5.5` should still
        pass preflight because OpenAI is a gateway-supported provider and
        both gateway env vars are present.

    The gateway counts only when all of the following hold:

    - `provider` is in `LANGSMITH_GATEWAY_PROVIDERS` (built-in chats that
      actually read the gateway env vars)
    - `LANGSMITH_GATEWAY` is set and is not a disable value
      (`false` / `0` / `no`)
    - `LANGSMITH_GATEWAY_API_KEY` is non-empty

    Callers must still skip this path for `class_path` provider overrides:
    those construct an arbitrary class that need not consume the gateway
    variables, so their own `api_key_env` preflight has to stand alone.

    Args:
        provider: Provider name (e.g., `"openai"`, `"anthropic"`).

    Returns:
        A `CONFIGURED` status pointing at `LANGSMITH_GATEWAY_API_KEY`, or
        `None` when the gateway cannot authenticate this provider.
    """
    from deepagents_code.config import active_environment

    environ = active_environment()
    gateway = environ.get(LANGSMITH_GATEWAY_ENV)
    gateway_key = environ.get(LANGSMITH_GATEWAY_API_KEY_ENV)
    if (
        provider not in LANGSMITH_GATEWAY_PROVIDERS
        or not gateway
        or gateway.lower() in _LANGSMITH_GATEWAY_FALSE_VALUES
        or not gateway_key
    ):
        return None
    return ProviderAuthStatus(
        state=ProviderAuthState.CONFIGURED,
        provider=provider,
        env_var=LANGSMITH_GATEWAY_API_KEY_ENV,
        source=ProviderAuthSource.ENV,
        detail="LangSmith Gateway credentials set",
    )


# [해설] 저장 키 또는 env(주 이름 → 폴백 이름들)에 값이 있으면 CONFIGURED 상태를 만든다. 없으면 None.
# [해설] 우선순위는 `resolve_provider_credential`과 같게 저장 키 먼저. 서비스 경로는 `allow_stored=False`로 저장 키를 무시할 수 있다.
def _resolve_configured(
    provider: str,
    env_var: str,
    fallback_env_vars: tuple[str, ...] = (),
    *,
    allow_stored: bool = True,
) -> ProviderAuthStatus | None:
    """Return a `CONFIGURED` status if a stored or env credential is set.

    Stored credentials beat env vars (matches `resolve_provider_credential`),
    unless the service caller disables stored credentials because a prefixed
    override prevents them from reaching the runtime.

    Args:
        provider: Provider name (e.g., `"anthropic"`).
        env_var: Canonical env var name to check when no stored credential
            exists. Recorded on the returned status either way.
        fallback_env_vars: Canonical env vars read, in order, when `env_var`
            is unset. The one that resolves is recorded on the status.
        allow_stored: Whether a stored credential can reach the runtime.

    Returns:
        A `CONFIGURED` status, or `None` when no source is set.
    """
    if allow_stored and _has_stored_credential(provider):
        return ProviderAuthStatus(
            state=ProviderAuthState.CONFIGURED,
            provider=provider,
            env_var=env_var,
            source=ProviderAuthSource.STORED,
            detail="stored credential",
        )
    for candidate in (env_var, *fallback_env_vars):
        if resolve_env_var(candidate):
            return ProviderAuthStatus(
                state=ProviderAuthState.CONFIGURED,
                provider=provider,
                env_var=candidate,
                source=ProviderAuthSource.ENV,
                detail="credentials set",
            )
    return None


# [해설] Codex(ChatGPT OAuth) 인증 상태. API 키 대신 `integrations.openai_codex.get_status()`의 토큰 파일 상태를 번역한다.
# [해설] 액세스 토큰이 만료돼도 refresh 토큰으로 갱신 가능하므로 CONFIGURED로 본다.
def _get_codex_auth_status() -> ProviderAuthStatus:
    """Translate the ChatGPT OAuth on-disk state into a `ProviderAuthStatus`.

    The codex provider uses a file-backed OAuth token store rather than
    `auth_store`'s API-key map, so it gets its own branch in
    `get_provider_auth_status`. The `STORED` source is reused only to satisfy
    the `ProviderAuthStatus` "CONFIGURED implies a source" invariant; it is
    cosmetic here, since `format_auth_badge` routes the codex provider to its
    own `[chatgpt]` / `[sign in to chatgpt]` badge before the source is ever
    consulted.

    Returns:
        `CONFIGURED` / `STORED` when a token bundle sits at the upstream
            default store path; `MISSING` otherwise. Expired access tokens
            are still reported as configured because the file-backed model
            provider can refresh them with the saved refresh token when the
            model is constructed.
    """
    from deepagents_code.integrations import openai_codex

    status = openai_codex.get_status()
    if status.unreadable_reason:
        return ProviderAuthStatus(
            state=ProviderAuthState.MISSING,
            provider=CODEX_PROVIDER,
            detail=f"token store unreadable: {status.unreadable_reason}",
        )
    if not status.logged_in:
        return ProviderAuthStatus(
            state=ProviderAuthState.MISSING,
            provider=CODEX_PROVIDER,
            detail="not signed in to ChatGPT",
        )
    detail = "signed in to ChatGPT"
    if status.plan_type:
        detail = f"signed in to ChatGPT ({status.plan_type})"
    if status.is_expired:
        detail = f"{detail}; access token will refresh on use"
    return ProviderAuthStatus(
        state=ProviderAuthState.CONFIGURED,
        provider=CODEX_PROVIDER,
        source=ProviderAuthSource.STORED,
        detail=detail,
    )


# [해설] 프로바이더 인증 준비 상태의 단일 판정 함수. 호출자: `/model` 선택기 배지, `/auth` 매니저, 시작 시 기본 모델 결정,
# [해설] `config.create_model`의 조기 키 검사(`has_provider_credentials`가 False면 `MissingCredentialsError`).
# [해설] 우선순위: Codex 전용 → config.toml `api_key_env` → class_path(MANAGED) → 내장 키 매핑 → 게이트웨이 → IMPLICIT
# [해설] → 선택적 키 → 로컬 무인증(NOT_REQUIRED)/원격(UNKNOWN) → 미지(UNKNOWN).
def get_provider_auth_status(provider: str) -> ProviderAuthStatus:
    """Return credential readiness details for a provider.

    Combines config, well-known provider metadata, optional provider auth,
    and implicit-auth provider metadata before attempting model creation:

    1. **Config-file providers** (`config.toml`
        `[models.providers.<name>]`):
        - If the section declares `api_key_env`, that env var is checked
            via `resolve_env_var()` (which honors `DEEPAGENTS_CODE_` prefixes).
        - If the section has `class_path` but no `api_key_env`, the provider is
            assumed to manage its own auth (e.g., custom headers, JWT, mTLS).
        - If neither `api_key_env` nor `class_path` is set, falls through
            to provider-specific defaults.
    2. **Hardcoded registry** (`PROVIDER_API_KEY_ENV`): a module-level dict
        mapping well-known provider names to their canonical env var
        (e.g., `"anthropic"` → `"ANTHROPIC_API_KEY"`). The env var is checked
        via `resolve_env_var()`.
    3. **Implicit auth providers** (e.g., Vertex AI ADC): a missing env var is
        not treated as missing credentials.
    4. **Optional auth env vars** (`OPTIONAL_AUTH_ENV`): when present, mark
        the provider as configured for hosted/cloud use.
    5. **No-auth-required providers** (`NO_AUTH_REQUIRED_PROVIDERS`): default
        local endpoints report `NOT_REQUIRED`; non-local endpoints fall back
        to `UNKNOWN` so the SDK can decide.
    6. **Unknown providers** not present in any source defer auth failures to
        the provider SDK.

    Use `has_provider_credentials()` when compatibility with the historic
    `True`/`False`/`None` contract is required.

    Args:
        provider: Provider name (e.g., `"anthropic"`, `"openai"`).

    Returns:
        Provider auth status for selectors, startup checks, and compatibility
            wrappers.
    """
    # ChatGPT-OAuth-backed codex provider has no env var and stores tokens
    # in its own on-disk JSON; route it through a dedicated helper before
    # the standard config / env-var lookup so callers get the codex-specific
    # `[chatgpt]` / `[sign in to chatgpt]` badge and a "signed in as <plan>"
    # detail.
    # [해설][흐름] 1) Codex는 토큰 파일 기반이라 전용 헬퍼로 먼저 분기.
    if provider == CODEX_PROVIDER:
        return _get_codex_auth_status()

    # [해설][흐름] 2) config.toml에 선언된 프로바이더: `api_key_env` 검사(→ 실패 시 게이트웨이, 단 class_path 아니면) 또는 class_path면 MANAGED.
    # Config-file providers take priority when api_key_env is specified.
    config = ModelConfig.load()
    provider_config = config.providers.get(provider)
    if provider_config:
        env_var = provider_config.get("api_key_env")
        if env_var:
            configured = _resolve_configured(provider, env_var)
            if configured:
                return configured
            # The gateway fallback is only valid when the built-in,
            # gateway-aware integration will actually be constructed. A
            # `class_path` override builds an arbitrary custom class via
            # `_create_model_from_class` that need not consume the gateway
            # variables, so its own `api_key_env` preflight must stand.
            if not provider_config.get("class_path"):
                gateway_configured = _resolve_gateway_configured(provider)
                if gateway_configured:
                    return gateway_configured
            return ProviderAuthStatus(
                state=ProviderAuthState.MISSING,
                provider=provider,
                env_var=env_var,
                detail=f"{env_var} is not set or is empty",
            )
        # class_path providers that omit api_key_env manage their own auth
        # (e.g., custom headers, JWT, mTLS).
        if provider_config.get("class_path"):
            return ProviderAuthStatus(
                state=ProviderAuthState.MANAGED,
                provider=provider,
                detail="custom auth",
            )
        # No api_key_env in config — fall through to provider-specific and
        # hardcoded maps.

    # [해설][흐름] 3) 내장 매핑(`PROVIDER_API_KEY_ENV`) 프로바이더: 키 → 게이트웨이 → implicit(ADC) → MISSING.
    # Fall back to hardcoded well-known providers.
    env_var = PROVIDER_API_KEY_ENV.get(provider)
    if env_var:
        configured = _resolve_configured(provider, env_var)
        if configured:
            return configured
        gateway_configured = _resolve_gateway_configured(provider)
        if gateway_configured:
            return gateway_configured
        if provider in IMPLICIT_AUTH_PROVIDERS:
            return ProviderAuthStatus(
                state=ProviderAuthState.IMPLICIT,
                provider=provider,
                env_var=env_var,
                detail="implicit auth",
            )
        return ProviderAuthStatus(
            state=ProviderAuthState.MISSING,
            provider=provider,
            env_var=env_var,
            detail=f"{env_var} is not set or is empty",
        )

    # [해설][흐름] 4) 매핑 없는 implicit 프로바이더.
    if provider in IMPLICIT_AUTH_PROVIDERS:
        return ProviderAuthStatus(
            state=ProviderAuthState.IMPLICIT,
            provider=provider,
            detail="implicit auth",
        )

    # [해설][흐름] 5) 선택적 키(Ollama `OLLAMA_API_KEY`)가 있으면 CONFIGURED.
    optional_env = OPTIONAL_AUTH_ENV.get(provider)
    if optional_env:
        configured = _resolve_configured(provider, optional_env)
        if configured:
            return configured

    # [해설][흐름] 6) 무인증 프로바이더: 로컬이면 NOT_REQUIRED, 원격이면 막지 않고 UNKNOWN + 힌트.
    if provider in NO_AUTH_REQUIRED_PROVIDERS:
        endpoint = _get_provider_endpoint(provider, config)
        if _is_local_endpoint(endpoint):
            return ProviderAuthStatus(
                state=ProviderAuthState.NOT_REQUIRED,
                provider=provider,
                detail="local provider",
            )
        # Remote endpoint may or may not require auth (private network vs.
        # hosted). Don't block; surface the optional env var as a hint.
        detail = (
            f"remote endpoint; set {optional_env} if auth is required"
            if optional_env
            else "remote endpoint"
        )
        return ProviderAuthStatus(
            state=ProviderAuthState.UNKNOWN,
            provider=provider,
            env_var=optional_env,
            detail=detail,
        )

    # [해설][흐름] 7) 어디에도 정보가 없으면 UNKNOWN — 실제 인증 실패는 SDK가 모델 생성/호출 시 보고한다.
    # Provider not found in config or hardcoded map — credential status is
    # unknown. The provider itself will report auth failures at
    # model-creation time.
    logger.debug(
        "No credential information for provider '%s'; deferring auth to provider",
        provider,
    )
    return ProviderAuthStatus(
        state=ProviderAuthState.UNKNOWN,
        provider=provider,
        detail="credentials unknown",
    )


# [해설] 과거 tri-state 계약(True/False/None) 호환 래퍼. 새 코드는 `get_provider_auth_status`를 쓰는 편이 정보가 많다.
def has_provider_credentials(provider: str) -> bool | None:
    """Check if credentials are available for a provider.

    This compatibility wrapper preserves the historic tri-state contract while
    `get_provider_auth_status()` carries the richer user-facing distinctions:
    configured credentials, missing credentials, no-auth local providers,
    implicit auth, custom provider-managed auth, and unknown providers.

    Args:
        provider: Provider name (e.g., `"anthropic"`, `"openai"`).

    Returns:
        `True` if auth is configured, implicit, provider-managed, or not
            required.
        `False` if a required env var is known but not set.
        `None` if credential status cannot be determined.
    """
    return get_provider_auth_status(provider).as_legacy_bool()


# [해설] 프로바이더 키 env var "이름" 결정: config.toml `api_key_env`(사용자 정의) > `PROVIDER_API_KEY_ENV`(내장).
# [해설] 호출자: `resolve_provider_credential`, `apply_stored_credentials`, `warn_on_split_credential_source` 등.
def get_credential_env_var(provider: str) -> str | None:
    """Return the env var name that holds credentials for a provider.

    Checks the config file first (user override), then falls back to the
    hardcoded `PROVIDER_API_KEY_ENV` map.

    Args:
        provider: Provider name.

    Returns:
        Environment variable name, or None if unknown.
    """
    config = ModelConfig.load()
    config_env = config.get_api_key_env(provider)
    if config_env:
        return config_env
    return PROVIDER_API_KEY_ENV.get(provider)


# [해설] base URL env var 이름 목록(해석 순서). config.toml `base_url_env`가 있으면 그것 하나만, 없으면 내장 튜플 전체.
def get_base_url_env_vars(provider: str) -> tuple[str, ...]:
    """Return base-URL env var names for a provider in resolution order.

    Checks the config file's `base_url_env` first (user override), then falls
    back to the hardcoded `PROVIDER_BASE_URL_ENV` map.

    Args:
        provider: Provider name.

    Returns:
        Environment variable names, or an empty tuple if the provider has no
        base-URL env var (config-declared or built-in).
    """
    config = ModelConfig.load()
    config_env = config.get_base_url_env(provider)
    if config_env:
        return (config_env,)
    return PROVIDER_BASE_URL_ENV.get(provider, ())


# [해설] canonical base URL env 이름(목록의 첫 번째). `get_credential_env_var`의 엔드포인트판.
def get_base_url_env_var(provider: str) -> str | None:
    """Return the canonical base-URL env var name for a provider.

    Checks the config file's `base_url_env` first (user override), then falls
    back to the canonical name in the hardcoded `PROVIDER_BASE_URL_ENV` map.
    Parallel to `get_credential_env_var`.

    Args:
        provider: Provider name.

    Returns:
        Environment variable name, or None if the provider has no base-URL env
        var (config-declared or built-in).
    """
    env_vars = get_base_url_env_vars(provider)
    return env_vars[0] if env_vars else None


# [해설] `/auth`의 base URL 입력칸을 비워 저장했을 때 여전히 값을 공급하는 접두 env 이름(UI 힌트용, 값은 노출하지 않음).
# [해설][주의] `active_environment()`가 아닌 `os.environ`을 직접 본다.
def get_default_base_url_env(provider: str) -> str | None:
    """Return the env var that supplies a provider's endpoint when none is stored.

    Answers "what does leaving the `/auth` base-URL field blank fall back to?"
    A blank save clears the *plain* endpoint env vars (so an inherited gateway
    URL can't leak through — see `apply_stored_credentials`), so the only env
    var that still supplies a value afterward is the `DEEPAGENTS_CODE_`-prefixed
    one. The name is returned (not its value) for display next to the field, so
    the user sees the knob rather than a long or sensitive URL.

    Returns `None` when that variable holds no value — the endpoint then comes
    from a `config.toml` literal or the provider SDK's own default, neither of
    which is a single env var to name here.

    Args:
        provider: Provider name.

    Returns:
        The `DEEPAGENTS_CODE_`-prefixed env var name still in effect after a
        blank save, or `None`.
    """
    for env_var in get_base_url_env_vars(provider):
        prefixed = f"{_ENV_PREFIX}{env_var}"
        if os.environ.get(prefixed):
            return prefixed
    return None


# [해설] `/auth`에서 다루는 비모델 서비스(Tavily/LangSmith) 판별 헬퍼들.
def is_service(name: str) -> bool:
    """Return whether `name` is a non-model service configurable via `/auth`."""
    return name in SERVICE_API_KEY_ENV


def is_langsmith(name: str) -> bool:
    """Return whether `name` is the LangSmith tracing service.

    Centralizes the identity check so the LangSmith-specific branches (project
    field instead of a base URL, tracing auto-enable) share one definition
    rather than scattering `== LANGSMITH_SERVICE` comparisons.
    """
    return name == LANGSMITH_SERVICE


# [해설] 서비스 키 준비 상태. 모델 프로바이더와 달리 접두 env가 존재하면 저장 키를 무시한다(`stored_key_reaches_runtime`).
# [해설] → 서비스는 문서(`credentials.md`)의 "접두 env > 저장 > 표준 env" 순서와 일치하고, 모델 프로바이더 경로만 다르다.
def get_service_auth_status(service: str) -> ProviderAuthStatus:
    """Return credential readiness for a non-model service (e.g. `"tavily"`).

    Checks a stored key, then `SERVICE_API_KEY_ENV[service]`, then each entry in
    `SERVICE_API_KEY_FALLBACK_ENV_VARS[service]` in order. Mirrors
    `get_provider_auth_status`, except a prefixed override suppresses the stored
    service key. Otherwise a stored key beats the env vars, and the
    `/auth` manager can render the same `[stored]` / `[env: ...]` / `[missing]`
    badges. Recorded env var names stay canonical; callers resolve the
    `DEEPAGENTS_CODE_` spelling at display time.

    Args:
        service: Service name (e.g. `"tavily"`).

    Returns:
        `CONFIGURED` when a stored, env, or fallback env credential is set,
            else `MISSING`.
    """
    env_var = SERVICE_API_KEY_ENV[service]
    fallbacks = SERVICE_API_KEY_FALLBACK_ENV_VARS.get(service, ())
    configured = _resolve_configured(
        service, env_var, fallbacks, allow_stored=stored_key_reaches_runtime(env_var)
    )
    if configured:
        return configured
    accepted = " or ".join((env_var, *fallbacks))
    return ProviderAuthStatus(
        state=ProviderAuthState.MISSING,
        provider=service,
        env_var=env_var,
        detail=f"{accepted} is not set or is empty",
    )


# [해설] 저장된 서비스 키(Tavily/LangSmith)를 표준 env var에 복사한다(프로세스 전역 `os.environ` 변경 — 부작용).
# [해설] 호출자: `config.py`(설정 부트스트랩)와 `app.py`(/auth 저장·재적용 시). 접두 env가 있으면 복사하지 않아 접두 env가 이긴다.
def apply_stored_service_credentials() -> None:
    """Export every stored service key into `os.environ`.

    Services (e.g. web search via Tavily) have no base URL to reconcile, so
    this is a plain key copy onto the canonical env var name the underlying
    SDK reads. A stored key takes precedence over an existing plain env var,
    matching `apply_stored_credentials`; a `DEEPAGENTS_CODE_`-prefixed override
    is left authoritative because the app already treats it as the top-priority
    per-session credential.
    """
    for service, env_var in SERVICE_API_KEY_ENV.items():
        try:
            stored = auth_store.get_stored_key(service)
        except RuntimeError:
            logger.warning(
                "Could not read stored credentials for service %s; the credential "
                "file may be corrupt. Re-add the key via /auth.",
                service,
            )
            continue
        if not stored:
            continue
        if not stored_key_reaches_runtime(env_var):
            continue
        if os.environ.get(env_var) != stored:
            os.environ[env_var] = stored


# [해설] 프로바이더의 저장 키 + 저장 엔드포인트를 한 쌍으로 `os.environ`에 반영한다. 호출자: `config.create_model`(스코프 없는 환경일 때).
# [해설] LangChain 채팅 모델 팩토리가 env에서 키를 읽기 때문에 필요한 브리지. 표준(비접두) 이름만 쓴다.
# [해설][주의] 프로세스 전역 env를 바꾸는 부작용 — 서버 프로세스에서 호출되면 이후 모든 모델 생성에 영향.
# [해설][설계] 엔드포인트를 먼저 정리해 "새 키 + 옛 게이트웨이 URL" 조합으로 개인 키가 게이트웨이로 전송되는 사고를 막는다.
def apply_stored_credentials(provider: str) -> bool:
    """Export this provider's stored key *and endpoint* into `os.environ`.

    LangChain's chat-model factories read credentials from process env vars,
    so a stored key only takes effect once it's copied onto the env var name
    registered for that provider. This is a no-op when the provider has no
    env-var mapping (custom auth) or no stored credential.

    The key env var is overwritten whether or not it was already set, matching
    the precedence rule documented on `resolve_provider_credential`: a
    credential the user typed in `/auth` is the most recent deliberate
    action and should take effect.

    Because a key and its endpoint are a coherent pair (a gateway key only
    works against the gateway URL; a provider-native key only against the
    provider's own endpoint), the base URL is applied atomically with the key:

    - A stored `base_url` is written to the provider's canonical base-URL env
        var, and every *other* base-URL name the SDK reads is cleared so an
        inherited gateway URL can't leak through an alternate variable.
    - No stored `base_url` (the user left the field blank) clears *all* of the
        provider's base-URL env vars, so the SDK falls back to the provider
        default rather than an inherited gateway URL. This is what prevents a
        personal key from being shipped to the gateway.

    Only the unprefixed canonical names are written, so an explicit
    `DEEPAGENTS_CODE_{VAR}` override still wins via `resolve_env_var`.

    Args:
        provider: Provider name.

    Returns:
        `True` if a stored key was applied, `False` otherwise.
    """
    # [해설][흐름] 1) 키 env 이름 결정(없으면 커스텀 인증 → no-op).
    env_var = get_credential_env_var(provider)
    if not env_var:
        return False
    # [해설][흐름] 2) 저장소에서 키·엔드포인트 읽기(손상 시 포기). 저장 키가 없으면 env를 전혀 건드리지 않는다.
    try:
        stored = auth_store.get_stored_key(provider)
        stored_base_url = auth_store.get_stored_base_url(provider)
    except RuntimeError:
        logger.warning("Could not read stored credentials for provider %s", provider)
        return False
    if not stored:
        return False
    # [해설][흐름] 3) 엔드포인트 env 정리/기록 → 4) 키 env 덮어쓰기(기존 표준 env 값보다 저장 키 우선).
    # Reconcile the endpoint first: it resolves env-var names (which can touch
    # the config) and so is the only step that might raise. Doing it before the
    # key write means the key is never left applied while an inherited gateway
    # URL stays uncleared — the key and endpoint move together.
    _apply_stored_base_url(provider, stored_base_url)
    if os.environ.get(env_var) != stored:
        os.environ[env_var] = stored
    return True


# [해설] base URL env들을 저장 엔드포인트와 맞춘다: 있으면 canonical에만 기록하고 별칭 삭제, 없으면 전부 삭제.
# [해설] 네이티브 키로 전환 시 게이트웨이용 커스텀 헤더 env(`ANTHROPIC_CUSTOM_HEADERS`)도 지운다(단, config/접두 env base URL이 남아 있으면 유지).
def _apply_stored_base_url(provider: str, base_url: str | None) -> None:
    """Reconcile a provider's base-URL env vars with a `/auth` credential.

    Writes `base_url` to the canonical name and clears the alternates, or
    clears every name when `base_url` is `None` (reset to the provider
    default). See `apply_stored_credentials` for the pairing rationale.

    When switching to a provider-native key (no `base_url`), also clears the
    provider's custom-headers env var (e.g. `ANTHROPIC_CUSTOM_HEADERS`) so a
    gateway-provisioned auth header isn't sent to the native endpoint.

    Args:
        provider: Provider name.
        base_url: The stored endpoint, or `None` to reset to the default.
    """
    # [해설][흐름] 1) 정리 대상 이름 집합(내장 별칭 + config 선언 `base_url_env`).
    canonical = get_base_url_env_var(provider)
    # Clear every name the SDK might read: the built-in alternates plus any
    # config-declared `base_url_env` (which extends pairing to providers
    # outside the hardcoded set).
    names = set(PROVIDER_BASE_URL_ENV.get(provider, ()))
    if canonical:
        names.add(canonical)
    if not names:
        return
    # [해설][흐름] 2) 헤더 유지 여부를 env 정리 "전에" 판정한 뒤, env 기록/삭제.
    configured_base_url_survives = _configured_base_url_survives_env_clear(provider)
    for name in names:
        if base_url and name == canonical:
            os.environ[name] = base_url
        else:
            os.environ.pop(name, None)

    # A provider SDK's custom-header env var (e.g. `ANTHROPIC_CUSTOM_HEADERS`)
    # injects headers into every request. A gateway-provisioned environment
    # often sets it to `X-Api-Key: <gateway-key>`, which overrides the SDK's
    # own `api_key`-derived header. When switching to a provider-native key
    # (no stored `base_url`), that header must also be cleared — otherwise the
    # gateway key is sent to the native endpoint and rejected.
    # [해설][흐름] 3) 커스텀 헤더 env 처리. 로그에는 이름만 남기고 값(인증 헤더)은 절대 남기지 않는다.
    custom_headers_env = PROVIDER_CUSTOM_HEADERS_ENV.get(provider)
    if custom_headers_env and not base_url:
        if not configured_base_url_survives:
            if os.environ.pop(custom_headers_env, None) is not None:
                # Log the env var name only — never its value, which carries
                # auth headers. Surfaces the removal for the user who set a
                # header deliberately for the native endpoint and later wonders
                # where it went.
                logger.info(
                    "Cleared %s while applying a provider-native %s key",
                    custom_headers_env,
                    provider,
                )
        elif os.environ.get(custom_headers_env) is not None:
            # A provider base URL still routes (config or a prefixed env var),
            # so the custom-header env is deliberately kept. Log the name only —
            # never the value — so the retention is observable when a user later
            # wonders why a gateway header is still in effect after applying a
            # native key.
            logger.debug(
                "Kept %s: a %s base URL is still configured",
                custom_headers_env,
                provider,
            )


# [해설] 표준 env를 지운 뒤에도 base URL이 살아남는지(config.toml `base_url` 또는 접두 env) 판정. `_apply_stored_base_url`의 헤더 유지 조건.
def _configured_base_url_survives_env_clear(provider: str) -> bool:
    """Return whether endpoint config still routes after plain env cleanup."""
    config = ModelConfig.load()
    provider_cfg = config.providers.get(provider)
    if provider_cfg and provider_cfg.get("base_url"):
        return True
    from deepagents_code.config import active_environment

    environment = active_environment()
    for env_var in get_base_url_env_vars(provider):
        if environment.get(f"{_ENV_PREFIX}{env_var}"):
            return True
    return False


# [해설] 키는 접두 env에서, 엔드포인트는 표준 env에서 오는 "출처 분리"를 DEBUG로 경고(진단 전용, 동작 변경 없음).
# [해설] 호출자: `config.create_model`. TUI 화면을 깨지 않도록 DEBUG 레벨(`DEEPAGENTS_CODE_DEBUG` 파일 로그)로만 남긴다.
def warn_on_split_credential_source(provider: str) -> None:
    """Log when a provider's key and endpoint resolve from different env tiers.

    The `DEEPAGENTS_CODE_` prefix is a *per-variable* override, not a credential
    bundle: setting `DEEPAGENTS_CODE_OPENAI_API_KEY` while leaving the endpoint to
    a plain `OPENAI_BASE_URL` makes the key resolve from the prefixed tier and the
    endpoint from the unprefixed one. A key and its endpoint are a coherent pair
    (see `PROVIDER_BASE_URL_ENV`), so a split source is a likely misconfiguration
    -- e.g. a provider-native key shipped to a gateway URL, or vice versa.

    This is purely diagnostic: it never mutates `os.environ` or changes
    resolution. Only the env var *names* are logged, never the secret value or
    the URL. It is emitted at DEBUG because the `deepagents_code` package logger
    only attaches a handler when `DEEPAGENTS_CODE_DEBUG` is set, and DEBUG stays
    below `logging.lastResort`'s WARNING stderr threshold so it cannot bleed onto
    stderr and corrupt the Textual TUI. The `DEEPAGENTS_CODE_DEBUG` file log is
    where someone chasing a wrong-endpoint bug will look.

    A `config.toml` `base_url` literal wins over env vars in `get_base_url`, so
    when one is set there is no env-tier split to flag and this returns early.

    Args:
        provider: Provider name (e.g. `"openai"`).
    """
    key_env = get_credential_env_var(provider)
    base_env = get_base_url_env_var(provider)
    if not key_env or not base_env:
        return
    config = ModelConfig.load()
    provider_cfg = config.providers.get(provider)
    if provider_cfg and provider_cfg.get("base_url"):
        return
    prefixed_key = f"{_ENV_PREFIX}{key_env}"
    prefixed_base = f"{_ENV_PREFIX}{base_env}"
    # Key must actually resolve from the prefixed tier (present and non-empty),
    # while the endpoint falls back to the plain tier: no prefixed override
    # present (an empty prefixed var would shadow the plain one in
    # `resolve_env_var`, so its mere presence means the endpoint is not "plain").
    key_from_prefixed = bool(os.environ.get(prefixed_key))
    base_from_plain = prefixed_base not in os.environ and bool(os.environ.get(base_env))
    if key_from_prefixed and base_from_plain:
        logger.debug(
            "Provider %s: API key resolved from %s but base URL resolved from "
            "the unprefixed %s. Key and endpoint came from different sources and "
            "may not be a matching pair. Set %s to pin the endpoint, or unset %s.",
            provider,
            prefixed_key,
            base_env,
            prefixed_base,
            base_env,
        )


# [해설] `[models]` 섹션 원본을 resolver용 순위값(`RankedProviderValue`)으로 감싼다. `_resolve_models_section`의 입력.
def _ranked_models_section(
    data: Mapping[str, Any],
    *,
    rank: int,
    status: ProviderStatus,
) -> RankedProviderValue[object]:
    """Read the raw `[models]` root for callsite structural validation.

    Returns:
        A durable ranked provider containing the raw section when declared.
    """
    from deepagents_code.configuration.resolver import RankedProviderValue
    from deepagents_code.configuration.types import Found, Unset

    result = Found(data["models"]) if status.usable and "models" in data else Unset()
    return RankedProviderValue(rank, True, status, result)


# [해설] managed 계층과 사용자 계층의 `[models]` 섹션을 순위(MANAGED_RANK > USER_RANK)로 deep-merge한다.
# [해설] 호출자: `ModelConfig.load`(구조 검사용). resolver 구현: `configuration/resolver.py`.
def _resolve_models_section(
    sources: ConfigSources,
    *,
    user_data: Mapping[str, Any],
) -> object:
    """Resolve the model namespace before model-specific validation.

    Returns:
        The deep-merged raw section, or an empty table when neither tier sets it.
    """
    from deepagents_code.configuration.resolver import (
        MANAGED_RANK,
        USER_RANK,
        resolve_ranked,
    )

    resolved = resolve_ranked(
        (
            _ranked_models_section(
                sources.managed.data,
                rank=MANAGED_RANK,
                status=sources.managed.status,
            ),
            _ranked_models_section(
                user_data,
                rank=USER_RANK,
                status=sources.user.status,
            ),
        ),
        strategy="deep_merge",
    )
    return {} if resolved is None else resolved.value


# [해설] 매니페스트 옵션 하나(`models.default` 등)를 "파일 계층(managed+user)만"으로 해석한다. env 계층은 의도적으로 제외.
# [해설] 반환: (값, 출처 라벨). 출처 라벨은 `ModelNotAllowedError` 문구 등에 쓰인다.
def _resolve_model_file_option(
    option: ConfigOption[object],
    sources: ConfigSources,
    *,
    user_data: Mapping[str, Any],
) -> tuple[object | None, str | None]:
    """Resolve one stored model option without admitting the env tier.

    `ModelConfig` describes persisted choices. In particular its
    `auto_classifier_model` field intentionally does not reflect the runtime
    environment override, so this reader constructs only the two file tiers.

    Returns:
        The ranked file value and its source, or `(None, None)` when both tiers
        abstain.
    """
    from deepagents_code.configuration.providers import ranked_toml_value
    from deepagents_code.configuration.resolver import (
        MANAGED_RANK,
        USER_RANK,
        resolve_ranked,
    )

    managed_section = sources.managed.data.get("models")
    managed_data = (
        {"models": managed_section} if isinstance(managed_section, dict) else {}
    )
    user_section = user_data.get("models")
    typed_user_data = {"models": user_section} if isinstance(user_section, dict) else {}
    resolved = resolve_ranked(
        (
            ranked_toml_value(
                option,
                managed_data,
                rank=MANAGED_RANK,
                durable=True,
                status=sources.managed.status,
            ),
            ranked_toml_value(
                option,
                typed_user_data,
                rank=USER_RANK,
                durable=True,
                status=sources.user.status,
            ),
        ),
        strategy=option.merge_strategy.value,
    )
    if resolved is None:
        return None, None
    source = " + ".join(resolved.provider_status[rank].name for rank in resolved.ranks)
    return resolved.value, source


# [해설] config.toml `[models]`(+managed 정책)의 파싱 결과 불변 객체. `ModelConfig.load()`가 프로세스 캐시 싱글턴을 돌려준다.
# [해설] 담는 것: default/recent/요약/Auto 분류기 모델, provider별 설정(`ProviderConfig`), allowlist 정책과 그 출처.
# [해설] 호출자: 거의 모든 모델 관련 코드(`config.create_model`, `configurable_model._cache_endpoint_identity`, 선택기 UI 등).
# [해설] 공식 문서: `config-file.md` "Default and recent model"(default가 recent보다 우선, `/model`은 recent에만 기록).
@dataclass(frozen=True)
class ModelConfig:
    """Parsed model configuration from `config.toml`.

    Instances are immutable once constructed. The `providers` mapping is
    wrapped in `MappingProxyType` to prevent accidental mutation of the
    globally cached singleton returned by `load()`.
    """

    default_model: str | None = None
    """The user's intentional default model (from config file `[models].default`)."""

    recent_model: str | None = None
    """The most recently switched-to model (from config file `[models].recent`)."""

    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    """Read-only mapping of provider names to their configurations."""

    auto_classifier_model: str | None = None
    """The stored Auto classifier model (from config file `[models].auto_classifier`).

    Carries the raw string with only a `str` type guard, so a blank or
    unbuildable spec reaches this field. Its only *value* consumer is the
    `/auto model` picker's `(default)` marker, which needs the stored text
    rather than a resolved model, so this field never rewrites or drops a value:
    `_validate` logs a warning when the text lacks a `provider:` prefix,
    `config.resolve_auto_classifier_model_with_problem` rejects a blank or
    non-string value at launch, and a spec that cannot be built fails closed at
    review time.

    Not the resolution path — a `DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL` export
    or `--auto-classifier-model` flag outranks this value at launch, so it may
    differ from the classifier Auto actually reviews with.
    """

    summarization_default_model: str | None = None
    """The default summary model from `[models].summarization_default`.

    Not the resolution path -- `--summarization-model` outranks this value at
    launch, so it may differ from the model summaries are actually generated
    with. Stored unvalidated: `_validate` only warns when the spec omits a
    `provider:` prefix, because `create_model`'s provider auto-detection makes
    a bare name legitimate.
    """

    # [해설][주의] allowlist 3상태: None=무제한, ()=전부 차단, 비어있지 않음=목록만 허용. 반드시 `is None`으로 검사할 것.
    allowed_models: tuple[str, ...] | None = None
    """Ordered model specs and provider wildcards the policy permits.

    Three states, and the difference between the last two matters:

    - `None` -- no policy is active; every model is allowed.
    - `()` -- a policy is active and permits **nothing**. Model construction
      and default resolution both fail closed.
    - non-empty -- only these entries are permitted, in preference order
      (`_get_default_model_spec` walks the tuple in declaration order). An
      entry is either an exact `provider:model` spec or a `provider:*`
      wildcard permitting every model from that provider; a wildcard is never
      selected as a default itself, but models it admits remain candidates.

    Test `is None`, never truthiness: `if not config.allowed_models` conflates
    "unrestricted" with "deny all" and inverts the policy.
    """

    allowed_models_source: str | None = None
    """Configuration layer that supplied `allowed_models`.

    A resolver provenance label such as `MANAGED_CONFIG_SOURCE` or
    `'config.toml'`. Compared literally against `MANAGED_CONFIG_SOURCE` to
    select administrator wording in errors and in the model selector, so it
    tracks that constant rather than being free-form. `None` exactly when
    `allowed_models` is `None`.
    """

    # [해설] providers를 읽기 전용 프록시로 동결하고, allowlist와 그 출처가 함께 설정/미설정인지 검증.
    def __post_init__(self) -> None:
        """Freeze the providers dict into a read-only proxy.

        Raises:
            ValueError: If `allowed_models` and `allowed_models_source` disagree
                about whether a policy is active.
        """
        if not isinstance(self.providers, MappingProxyType):
            object.__setattr__(self, "providers", MappingProxyType(self.providers))
        # The pair varies together: every consumer reads the source only to
        # describe an active policy. Guarding here mirrors
        # `ProviderAuthStatus.__post_init__` and makes an incoherent policy
        # unconstructible rather than silently mis-attributed.
        if (self.allowed_models is None) != (self.allowed_models_source is None):
            msg = (
                "allowed_models and allowed_models_source must both be set or "
                f"both be None (got {self.allowed_models!r} from "
                f"{self.allowed_models_source!r})"
            )
            raise ValueError(msg)

    # [해설] 설정 로드 진입점. 기본 경로면 managed+user 병합·캐시, 명시 경로면 해당 파일만(테스트용).
    # [해설] 절대 예외로 죽지 않도록(깨진 TOML·잘못된 구조는 경고 후 빈 설정) 설계 — `/auth` 모달 등이 total 함수로 가정.
    # [해설][설계] 사용자 파일 문제는 사용자 계층만 버리고 managed 정책은 계속 적용한다(보안 정책 우회 방지).
    @classmethod
    def load(cls, config_path: Path | None = None) -> ModelConfig:
        """Load config from file.

        When called with the default path, results are cached for the
        lifetime of the process. Use `clear_caches()` to reset.

        Args:
            config_path: Path to config file. Defaults to
                ~/.deepagents/config.toml. Passing a path also excludes managed
                policy from this read, so production callers must pass `None`.

        Returns:
            Parsed `ModelConfig` instance. A user file that is missing,
                unreadable, contains invalid TOML syntax, or is structurally
                invalid (valid TOML of the wrong shape, e.g. a scalar
                `[models]`) drops only the user layer: managed values still
                apply on the default path. The result is empty only when
                neither layer supplies values. An explicit `config_path` reads
                that file alone, with no managed layer.

        Raises:
            RuntimeError: If required model options are missing from the manifest.
        """
        # [해설][흐름] 1) 기본 경로 캐시 적중 시 즉시 반환.
        global _default_config_cache  # noqa: PLW0603  # Module-level cache requires global statement
        is_default = config_path is None
        if is_default and _default_config_cache is not None:
            return _default_config_cache

        if config_path is None:
            config_path = DEFAULT_CONFIG_PATH

        from deepagents_code.config_manifest import get_option
        from deepagents_code.configuration.service import get_config_sources
        from deepagents_code.configuration.types import ProviderHealth

        # [해설][흐름] 2) 소유자 읽기 권한(0o400) 제거 여부 확인 — 권한 있는 프로세스가 사용자가 막아둔 파일을 읽지 않게 한다.
        # `0o400`, not `0o444`: the question is whether the *owner* has made
        # the file unavailable. A file readable only by other users is not the
        # case this guard describes, and a privileged process could read it.
        stat_error: str | None = None
        try:
            user_mode = config_path.stat().st_mode if config_path.exists() else None
        except OSError as exc:
            # Not necessarily a permission problem, so report what happened
            # rather than asserting one cause.
            stat_error = f"{type(exc).__name__}: {exc}"
            user_mode = 0
        user_unreadable = user_mode is not None and user_mode & 0o400 == 0
        if user_unreadable:
            logger.warning(
                "Could not read config file %s: %s",
                config_path,
                stat_error or "owner has removed read permission",
            )

        # [해설][흐름] 3) 설정 소스 로드 + 손상/읽기 불가/managed 미적용 진단 로그.
        # `None` on the default path: that is what includes managed policy.
        sources = get_config_sources(user_path=None if is_default else config_path)
        if sources.user.status.health is ProviderHealth.CORRUPT:
            logger.warning(
                "Config file %s has invalid TOML syntax: %s. "
                "Ignoring config file. Fix the file or delete it to reset.",
                config_path,
                sources.user.status.detail or "unknown parse error",
            )
        elif sources.user.status.health is ProviderHealth.UNREADABLE:
            logger.warning(
                "Could not read config file %s: %s",
                config_path,
                sources.user.status.detail or "unknown read error",
            )
        dropped = sources.dropped_managed_detail()
        if dropped is not None:
            logger.error(
                "Managed policy from %s is not being applied: %s",
                sources.managed.status.path,
                dropped,
            )
        # Do not let a privileged process read a config the owning user has
        # made unavailable, but preserve administrator-managed policy.
        user_data = {} if user_unreadable else sources.user.data
        # A warning below describes the effective ranked value, so it must not
        # assert that the value sits in the user's file.
        source_label = (
            f"{config_path} or managed config"
            if sources.managed.data
            else str(config_path)
        )

        # [해설][흐름] 4) `models.allowed` 정책 해석. 파싱 실패한 선언은 deny-all `()`로 fail-closed.
        allowed_models: tuple[str, ...] | None = None
        allowed_models_source: str | None = None
        allowed_option = get_option("models.allowed")
        if allowed_option is None:
            msg = "models.allowed is missing from the config manifest"
            raise RuntimeError(msg)
        allowed_value, allowed_source = _resolve_model_file_option(
            allowed_option,
            sources,
            user_data=user_data,
        )
        if isinstance(allowed_value, tuple):
            allowed_models = cast("tuple[str, ...]", allowed_value)
            allowed_models_source = allowed_source
        elif (
            declared := _malformed_allowlist_source(sources, user_data, config_path)
        ) is not None:
            # `models.allowed` has no manifest default, so an unparseable list
            # resolves to `None` -- which means *unrestricted*. Failing open on
            # a security control because of a typo is the wrong default, so a
            # declaration that produced no usable value becomes deny-all. The
            # managed layer additionally refuses to start
            # (`ENFORCED_MANAGED_KEYS`); this covers the user layer, where the
            # only other signal is a log line nobody reads.
            logger.error(
                "Ignoring malformed [models].allowed from %s; blocking all "
                "models until it is fixed or removed",
                declared,
            )
            allowed_models = ()
            allowed_models_source = declared

        # [해설][흐름] 5) `[models]` 구조 검사 후 매니페스트 옵션(default/recent/summarization_default/auto_classifier/providers)을 파일 계층만으로 해석.
        try:
            models_section = cast(
                "Any", _resolve_models_section(sources, user_data=user_data)
            )
            # Preserve the structural diagnostic before resolving children.
            # Calling `.get` intentionally raises for an effective scalar root,
            # exactly as the tolerant legacy reader did.
            models_section.get("default")

            option_keys = (
                "models.default",
                "models.recent",
                "models.summarization_default",
                "models.auto_classifier",
                "models.providers",
            )
            options = {key: get_option(key) for key in option_keys}
            if any(option is None for option in options.values()):
                msg = "model options are missing from the config manifest"
                raise RuntimeError(msg)
            resolved = {
                key: _resolve_model_file_option(
                    cast("ConfigOption[object]", option),
                    sources,
                    user_data=user_data,
                )[0]
                for key, option in options.items()
            }

            # [해설][흐름] 6) 사용자 파일의 default/recent가 강제변환에서 탈락했으면 기존 경고 문구를 재생(값은 채택하지 않음).
            user_models = user_data.get("models")
            if isinstance(user_models, dict):
                for key in ("default", "recent"):
                    option_key = f"models.{key}"
                    if resolved[option_key] is None and key in user_models:
                        # Provider coercion already rejected this tier. Replay
                        # the existing callsite diagnostic without making the
                        # raw value authoritative again.
                        _toml_model_spec(
                            user_models[key],
                            key=key,
                            path=config_path,
                            source_label=source_label,
                        )

            # [해설][흐름] 7) 필드별 형태 정규화(`_toml_model_spec`, `_toml_providers_table`)로 인스턴스 생성.
            # Coerce each resolved field to the shape the readers below assume.
            # Callsite validation remains separate from provider coercion where
            # it needs model-specific context and warning text.
            config = cls(
                default_model=_toml_model_spec(
                    resolved["models.default"],
                    key="default",
                    path=config_path,
                    source_label=source_label,
                ),
                recent_model=_toml_model_spec(
                    resolved["models.recent"],
                    key="recent",
                    path=config_path,
                    source_label=source_label,
                ),
                summarization_default_model=_toml_model_spec(
                    resolved["models.summarization_default"],
                    key="summarization_default",
                    path=config_path,
                    source_label=source_label,
                ),
                auto_classifier_model=(
                    resolved["models.auto_classifier"]
                    if isinstance(resolved["models.auto_classifier"], str)
                    else None
                ),
                providers=_toml_providers_table(
                    resolved["models.providers"]
                    if resolved["models.providers"] is not None
                    else {},
                    path=config_path,
                    source_label=source_label,
                ),
                allowed_models=allowed_models,
                allowed_models_source=allowed_models_source,
            )
        # [해설][흐름] 구조 오류(스칼라 `[models]` 등)는 빈 설정으로 대체 — 호출자를 크래시시키지 않는다.
        except (AttributeError, TypeError) as e:
            # Syntactically valid TOML can still have the wrong shape (e.g. a
            # scalar `[models]`). Treat it like any other unreadable config
            # rather than letting it crash callers (e.g. the /auth modal on
            # Ctrl+R) that assume load() is total and never raises.
            logger.warning(
                "Config file %s is structurally invalid: %s. "
                "Ignoring config file. Fix the file or delete it to reset.",
                config_path,
                e,
            )
            config = cls()

        # [해설][흐름] 8) 일관성 경고(`_validate`) 후 기본 경로면 캐시에 저장.
        # Validate config consistency
        config._validate()

        if is_default:
            _default_config_cache = config

        return config

    # [해설] 경고만 내는 일관성 검사: 모델 필드의 `provider:` 접두 누락, provider 테이블의 타입 오류, 잘못된 class_path,
    # [해설] `models` 목록에 없는 모델용 per-model `params`. 예외는 던지지 않는다(열화 모드로 계속).
    def _validate(self) -> None:
        """Validate internal consistency of the config.

        Issues warnings for invalid configurations but does not raise exceptions,
        allowing the app to continue with potentially degraded functionality.
        """
        # Warn if a model field is set but doesn't use provider:model format
        model_fields = (
            ("default_model", self.default_model, "anthropic:claude-sonnet-4-5"),
            ("recent_model", self.recent_model, "anthropic:claude-sonnet-4-5"),
            (
                "summarization_default_model",
                self.summarization_default_model,
                "openai:gpt-5.4-mini",
            ),
            (
                "auto_classifier_model",
                self.auto_classifier_model,
                "anthropic:claude-sonnet-4-5",
            ),
        )
        for field_name, spec, example in model_fields:
            if spec and ":" not in spec:
                logger.warning(
                    "%s '%s' should use provider:model format (e.g., '%s')",
                    field_name,
                    spec,
                    example,
                )

        # Validate enabled field type and class_path format / params references
        for name, provider in self.providers.items():
            # `enabled` originates from untyped TOML; cast to `object` so the
            # runtime non-bool validation below stays reachable (the TypedDict
            # types it as `bool`, which would otherwise mark this branch dead).
            enabled = cast("object", provider.get("enabled"))
            if enabled is not None and not isinstance(enabled, bool):
                logger.warning(
                    "Provider '%s' has non-boolean 'enabled' value %r "
                    "(expected true/false). Provider will remain visible.",
                    name,
                    enabled,
                )

            # `display_name`/`api_key_url` also originate from untyped TOML; cast
            # to `object` so the runtime non-string checks stay reachable (the
            # TypedDict types them as `str`).
            display_name = cast("object", provider.get("display_name"))
            if display_name is not None and not isinstance(display_name, str):
                logger.warning(
                    "Provider '%s' has non-string 'display_name' value %r "
                    "(expected a string). Falling back to the default label.",
                    name,
                    display_name,
                )

            short_name = cast("object", provider.get("short_name"))
            if short_name is not None and not isinstance(short_name, str):
                logger.warning(
                    "Provider '%s' has non-string 'short_name' value %r "
                    "(expected a string). Falling back to the display name.",
                    name,
                    short_name,
                )

            api_key_url = cast("object", provider.get("api_key_url"))
            if api_key_url is not None and not isinstance(api_key_url, str):
                logger.warning(
                    "Provider '%s' has non-string 'api_key_url' value %r "
                    "(expected a string). Ignoring it.",
                    name,
                    api_key_url,
                )

            class_path = provider.get("class_path")
            if class_path and ":" not in class_path:
                logger.warning(
                    "Provider '%s' has invalid class_path '%s': "
                    "must be in module.path:ClassName format "
                    "(e.g., 'my_package.models:MyChatModel')",
                    name,
                    class_path,
                )

            models = set(provider.get("models", []))

            params = provider.get("params", {})
            for key, value in params.items():
                if isinstance(value, dict) and key not in models:
                    logger.warning(
                        "Provider '%s' has params for '%s' "
                        "which is not in its models list",
                        name,
                        key,
                    )

    # [해설] 정확한 spec이 allowlist를 통과하는지(정확 일치 또는 `provider:*`). 정책 없음이면 항상 True.
    # [해설] 호출자: `_discover_available_models`(목록 필터), `policy_error`.
    def is_model_allowed(self, model_spec: str) -> bool:
        """Return whether an exact model spec is allowed by active policy.

        A spec is permitted when it appears in `allowed_models` verbatim or a
        `provider:*` wildcard entry names its provider.
        """
        if self.allowed_models is None:
            return True
        parsed = ModelSpec.try_parse(model_spec.strip())
        return parsed is not None and (
            str(parsed) in self.allowed_models
            or f"{parsed.provider}:*" in self.allowed_models
        )

    # [해설] 사용자가 입력한 spec(bare 이름 포함)을 정책 비교용 `provider:model`로 정규화. `create_model`의 프로바이더 추론 규칙을 복제한다.
    # [해설] 분기: config 커스텀 프로바이더 → Bedrock ID → 일반 spec → 선행 콜론(`:model`) → bare 이름 추론(`config.detect_provider`).
    # [해설][주의] `create_model`의 추론 규칙이 바뀌면 여기와 어긋날 수 있다(사전 검사 vs 실제 게이트 불일치).
    def canonical_model_spec(self, model_spec: str) -> str | None:
        """Resolve a user-typed spec to the form the policy gate matches on.

        Mirrors `create_model`'s provider resolution, including the custom
        provider, Bedrock, and leading-colon branches, so a preflight check
        agrees with the authoritative gate instead of rejecting a bare name
        that `create_model` would infer a provider for and allow.

        Args:
            model_spec: A spec as the user typed it, bare name included.

        Returns:
            The canonical `provider:model` string, or `None` when no provider
                can be established -- which policy treats as unmatchable.
        """
        from deepagents_code.config import detect_provider

        normalized = model_spec.strip()
        if not normalized:
            return None
        inferred = detect_provider(normalized)
        parsed = ModelSpec.try_parse(normalized)
        if parsed and parsed.provider in self.providers:
            provider, model_name = parsed.provider, parsed.model
        elif inferred == "bedrock":
            provider, model_name = inferred, normalized
        elif parsed:
            provider, model_name = parsed.provider, parsed.model
        elif ":" in normalized:
            _, _, after = normalized.partition(":")
            if not after:
                return None
            model_name = after
            provider = detect_provider(model_name) or ""
        else:
            model_name = normalized
            provider = inferred or ""
        return f"{provider}:{model_name}" if provider else None

    # [해설] 정책 위반 오류 객체를 "만들기만" 한다(던지지 않음). 런치 안내·선택기 푸터처럼 메시지만 필요한 곳과 던지는 곳이 같은 문구를 쓰게 한다.
    def policy_error(
        self,
        model_spec: str | None,
        *,
        context: str | None = None,
        canonicalize: bool = False,
    ) -> ModelNotAllowedError | None:
        """Build the policy error blocking a spec, or `None` when it is allowed.

        The one place that turns this config's policy fields into an error, so
        callers that need the *message* without raising (a launch advisory, a
        selector footer) cannot drift from callers that raise.

        Args:
            model_spec: The spec to check, or `None` to ask for the error that
                describes a deny-all policy blocking default resolution.
            context: Where the spec was declared, prefixed to the message.
            canonicalize: Infer a provider for a bare name before matching, the
                way `create_model` does. Set this on preflight checks against
                text a user typed; leave it off where the caller already holds a
                canonical spec, so resolution stays off hot paths such as the
                recent-models cache.

        Returns:
            The error to raise or render, or `None` when no policy blocks this.
        """
        if self.allowed_models is None:
            return None
        if model_spec is not None:
            candidate = model_spec
            if canonicalize:
                # Fall back to the raw text when no provider can be inferred:
                # it stays unmatchable, and the message then advises a fully
                # qualified spec, which is the actionable advice.
                candidate = self.canonical_model_spec(model_spec) or model_spec
            if self.is_model_allowed(candidate):
                return None
        # The message quotes what the user supplied, not the canonical form, so
        # it echoes back the text they can see in front of them.
        return ModelNotAllowedError(
            model_spec=model_spec,
            source=self.allowed_models_source,
            allowed_models=self.allowed_models,
            context=context,
        )

    # [해설] 정책 위반 시 `ModelNotAllowedError`를 던진다. 호출자: `_save_model_field`, `config.create_model` 등 실제 게이트.
    def require_model_allowed(
        self, model_spec: str, *, context: str | None = None
    ) -> None:
        """Raise when an exact model spec is outside active policy.

        Args:
            model_spec: The spec to check.
            context: Where the spec was declared, prefixed to the message.

        Raises:
            ModelNotAllowedError: If `model_spec` is not in the active allowlist.
        """  # noqa: DOC502 - propagates from `policy_error`
        error = self.policy_error(model_spec, context=context)
        if error is not None:
            raise error

    # [해설] `enabled = false`로 명시한 경우만 비활성(설정에 없는 프로바이더는 활성). 모델 선택기 노출 여부 결정.
    def is_provider_enabled(self, provider_name: str) -> bool:
        """Check whether a provider should appear in the model switcher.

        A provider is disabled when its config explicitly sets
        `enabled = false`. Providers not present in the config file are
        always considered enabled.

        Args:
            provider_name: The provider to check.

        Returns:
            `False` if the provider is explicitly disabled, `True` otherwise.
        """
        provider = self.providers.get(provider_name)
        if not provider:
            return True
        return provider.get("enabled") is not False

    # [해설] config.toml에 선언된 모든 (모델, 프로바이더) 쌍 — enabled/allowlist 필터 없음.
    def get_all_models(self) -> list[tuple[str, str]]:
        """Get all models as `(model_name, provider_name)` tuples.

        Returns raw config data — does not filter by `is_provider_enabled`.
        For the filtered set shown in the model switcher, use
        `get_available_models()`.

        Returns:
            List of tuples containing `(model_name, provider_name)`.
        """
        return [
            (model, provider_name)
            for provider_name, provider_config in self.providers.items()
            for model in provider_config.get("models", [])
        ]

    # [해설] bare 모델 이름을 config.toml `models` 목록에서 찾아 프로바이더를 역추적(프로바이더 자동 추론 보조, 추정).
    def get_provider_for_model(self, model_name: str) -> str | None:
        """Find the provider that contains this model.

        Returns raw config data — does not filter by `is_provider_enabled`.

        Args:
            model_name: The model identifier to look up.

        Returns:
            Provider name if found, None otherwise.
        """
        for provider_name, provider_config in self.providers.items():
            if model_name in provider_config.get("models", []):
                return provider_name
        return None

    # [해설] config.toml `api_key_env` 기반 키 존재 검사(구 API). 저장 키는 보지 않는다 — 신규 경로는 `get_provider_auth_status`.
    def has_credentials(self, provider_name: str) -> bool | None:
        """Check if credentials are available for a provider.

        This is the config-file-driven credential check, supporting custom
        providers (e.g., local Ollama with no key required). For the hardcoded
        `PROVIDER_API_KEY_ENV`-based check used in the hot-swap path, see the
        module-level `has_provider_credentials()`.

        Args:
            provider_name: The provider to check.

        Returns:
            True if credentials are confirmed available, False if confirmed
                missing, or None if no `api_key_env` is configured and
                credential status cannot be determined.
        """
        provider = self.providers.get(provider_name)
        if not provider:
            return False
        env_var = provider.get("api_key_env")
        if not env_var:
            return None  # No key configured — can't verify
        return bool(resolve_env_var(env_var))

    # [해설] provider의 base URL 해석: ① config.toml `base_url` ② base URL env들(`resolve_env_var`, 접두 우선) ③ `/auth` 저장 엔드포인트.
    # [해설] 호출자: `get_effective_kwargs`, `_get_provider_endpoint`, `config._get_provider_kwargs`(추정), `configurable_model._cache_endpoint_identity`.
    # [해설] 공식 문서 `config-file.md` "Endpoints, keys, and gateways"의 순서와 일치(SDK 기본값은 None 반환 후 SDK가 결정).
    # [해설][주의] `auth_store` 파일을 매번 읽는 블로킹 I/O 경로 — 서버 이벤트 루프에서는 스레드 오프로드 필요.
    def get_base_url(self, provider_name: str) -> str | None:
        """Get the configured base URL for a provider.

        Resolution order (first match wins):

        1. `base_url` in the provider's `config.toml` section.
        2. The provider's base-URL env vars via `resolve_env_var`, in provider
            precedence order, so `DEEPAGENTS_CODE_{VAR}` beats the plain `{VAR}`
            for each name — mirroring how API keys resolve. This also surfaces
            the value `apply_stored_credentials` bridged in from a `/auth`
            credential, and the gateway-provisioned URL in the default
            (no-override) case.
        3. The endpoint stored with a `/auth` credential. This is the source
            for providers that have no base-URL env var (e.g. an OpenAI-
            compatible provider like Litellm): step 2 has no name to read, so
            the stored endpoint is taken directly. It then reaches the model as
            the `base_url` constructor kwarg via
            `_get_provider_kwargs`, the same path a `config.toml` literal uses.
            For providers that *do* have an env var, the stored endpoint already
            arrives via step 2 (it was bridged onto the env var), so this step
            is a redundant — and consistent — fallback.

        This function only *resolves* the endpoint; whether it takes effect is a
        separate contract owned by the provider's LangChain class. The value is
        delivered as the `base_url` kwarg (see `_get_provider_kwargs`), which the
        OpenAI/Anthropic-compatible classes accept via a Pydantic `base_url`
        alias. A class that names the field differently may silently
        ignore `base_url` — Pydantic models default to `extra="ignore"` — so for
        those the endpoint must be set via `params`.

        A corrupt credential store is treated as "no stored endpoint" rather than
        propagating, so endpoint resolution never newly raises.

        Args:
            provider_name: The provider to get base URL for.

        Returns:
            Base URL if configured, None otherwise.
        """
        # [해설][흐름] 1) config.toml 리터럴 → 2) env 이름 목록 순회 → 3) 저장소 엔드포인트(손상 시 None).
        provider = self.providers.get(provider_name)
        config_url = provider.get("base_url") if provider else None
        if config_url:
            return config_url
        config_env = provider.get("base_url_env") if provider else None
        env_vars = (
            (config_env,)
            if config_env
            else PROVIDER_BASE_URL_ENV.get(provider_name, ())
        )
        for env_var in env_vars:
            resolved = resolve_env_var(env_var)
            if resolved:
                return resolved
        try:
            return auth_store.get_stored_base_url(provider_name)
        except RuntimeError:
            return None

    # [해설] 아래는 `[models.providers.<name>]`의 단일 필드 접근자들(api_key_env, display_name, short_name, api_key_url, base_url_env, class_path).
    # [해설] UI용 문자열 필드는 TOML 타입 오류 대비로 `str`일 때만 반환한다.
    def get_api_key_env(self, provider_name: str) -> str | None:
        """Get the environment variable name for a provider's API key.

        Args:
            provider_name: The provider to get API key env var for.

        Returns:
            Environment variable name if configured, None otherwise.
        """
        provider = self.providers.get(provider_name)
        return provider.get("api_key_env") if provider else None

    def get_provider_display_name(self, provider_name: str) -> str | None:
        """Get the configured display name for a provider.

        Args:
            provider_name: The provider to look up.

        Returns:
            Human-readable display name if configured, None otherwise.
        """
        provider = self.providers.get(provider_name)
        name = provider.get("display_name") if provider else None
        return name if isinstance(name, str) else None

    def get_provider_short_name(self, provider_name: str) -> str | None:
        """Get the configured compact brand name for a provider.

        Args:
            provider_name: The provider to look up.

        Returns:
            Compact brand name if configured, None otherwise.
        """
        provider = self.providers.get(provider_name)
        name = provider.get("short_name") if provider else None
        return name if isinstance(name, str) else None

    def get_provider_api_key_url(self, provider_name: str) -> str | None:
        """Get the configured API-key management URL for a provider.

        Args:
            provider_name: The provider to look up.

        Returns:
            API-key management URL if configured, None otherwise.
        """
        provider = self.providers.get(provider_name)
        url = provider.get("api_key_url") if provider else None
        return url if isinstance(url, str) else None

    def get_base_url_env(self, provider_name: str) -> str | None:
        """Get the environment variable name for a provider's base URL.

        Args:
            provider_name: The provider to get the base-URL env var for.

        Returns:
            Environment variable name if configured, None otherwise.
        """
        provider = self.providers.get(provider_name)
        return provider.get("base_url_env") if provider else None

    def get_class_path(self, provider_name: str) -> str | None:
        """Get the custom class path for a provider.

        Args:
            provider_name: The provider to look up.

        Returns:
            Class path in `module.path:ClassName` format, or None.
        """
        provider = self.providers.get(provider_name)
        return provider.get("class_path") if provider else None

    # [해설] 생성자 추가 kwargs(`params`): 평면 키 = 프로바이더 공통, 모델명 서브테이블 = 해당 모델만(얕은 병합, 모델 우선).
    def get_kwargs(
        self, provider_name: str, *, model_name: str | None = None
    ) -> dict[str, Any]:
        """Get extra constructor kwargs for a provider.

        Reads the `params` table from the provider config. Flat keys are
        provider-wide defaults; model-keyed sub-tables are per-model
        overrides that shallow-merge on top (model wins on conflict).

        Args:
            provider_name: The provider to look up.
            model_name: Optional model name for per-model overrides.

        Returns:
            Dictionary of extra kwargs (empty if none configured).
        """
        provider = self.providers.get(provider_name)
        if not provider:
            return {}
        params = provider.get("params", {})
        result = {k: v for k, v in params.items() if not isinstance(v, dict)}
        if model_name:
            overrides = params.get(model_name)
            if isinstance(overrides, dict):
                result.update(overrides)
        return result

    # [해설] 실제 요청에 쓰일 유효 kwargs: `get_kwargs` → `base_url` → 런타임 오버라이드(최우선). 모델 생성 순서를 흉내 낸다.
    # [해설] 호출자: `configurable_model._cache_endpoint_identity`/`_effective_cache_params`(cold-cache 식별), 앱 쪽 캐시 비교.
    def get_effective_kwargs(
        self,
        provider_name: str,
        *,
        model_name: str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve the effective configured and runtime model kwargs.

        This mirrors the ordering used when constructing a model: provider and
        per-model `params`, then the resolved `base_url`, then runtime
        overrides. Consumers that need to reason about an actual request (such
        as cache policy checks) must use this rather than inspecting one source
        of settings in isolation.

        Args:
            provider_name: Provider whose configuration should be resolved.
            model_name: Optional model name for per-model params.
            overrides: Per-request params, which take highest precedence.

        Returns:
            Effective model kwargs.
        """
        result = self.get_kwargs(provider_name, model_name=model_name)
        base_url = self.get_base_url(provider_name)
        if base_url:
            result["base_url"] = base_url
        if overrides:
            result.update(overrides)
        return result

    # [해설] 프로필 오버라이드(`profile` 테이블): `get_kwargs`와 같은 평면/모델별 얕은 병합 규칙. `get_model_profiles`에서 사용.
    def get_profile_overrides(
        self, provider_name: str, *, model_name: str | None = None
    ) -> dict[str, Any]:
        """Get profile overrides for a provider.

        Reads the `profile` table from the provider config. Flat keys are
        provider-wide defaults; model-keyed sub-tables are per-model overrides
        that shallow-merge on top (model wins on conflict).

        Args:
            provider_name: The provider to look up.
            model_name: Optional model name for per-model overrides.

        Returns:
            Dictionary of profile overrides (empty if none configured).
        """
        provider = self.providers.get(provider_name)
        if not provider:
            return {}
        profile = provider.get("profile", {})
        result = {k: v for k, v in profile.items() if not isinstance(v, dict)}
        if model_name:
            overrides = profile.get(model_name)
            if isinstance(overrides, dict):
                result.update(overrides)
        return result


# [해설] config.toml `[section].field` 단일 키를 read-modify-write로 저장하는 공통 헬퍼. 값이 같으면 쓰지 않는다.
# [해설] 원자적 쓰기(임시 파일 → rename) + 전역 `_config_write_lock`, 성공 시 `_invalidate_config_caches`.
# [해설][주의] `tomli_w`로 다시 직렬화하므로 사용자 파일의 주석·서식이 사라진다.
def _save_toml_field(
    section: str,
    field: str,
    value: str | bool,
    config_path: Path | None = None,
) -> bool:
    """Read-modify-write a `[section].<field>` key in the config file.

    Args:
        section: TOML table name (e.g., `'models'`, `'agents'`).
        field: Key within the table (e.g., `'default'`, `'recent'`).
        value: String or boolean value to persist.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.
    """
    # [해설][흐름] 1) 락 획득 → 기존 파일 읽기(없으면 빈 dict).
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)

            # Read existing config or start fresh
            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            # [해설][흐름] 2) 값 비교(타입까지) 후 변경이 있을 때만 갱신·원자적 기록.
            existing_section = data.get(section)
            existing = (
                existing_section.get(field)
                if isinstance(existing_section, dict)
                else None
            )
            unchanged = type(existing) is type(value) and existing == value
            if not unchanged:
                if section not in data:
                    data[section] = {}
                data[section][field] = value

                # Write to temp file then rename so an interrupted write can't corrupt
                fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
                try:
                    with os.fdopen(fd, "wb") as f:
                        tomli_w.dump(data, f)
                    Path(tmp_path).replace(config_path)
                except BaseException:
                    # Clean up temp file on any failure
                    with contextlib.suppress(OSError):
                        Path(tmp_path).unlink()
                    raise
    # [해설][흐름] 3) 실패는 bool 계약(False)으로 흡수, 성공 시 캐시 무효화.
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        # `TypeError` covers `tomli_w.dump` rejecting a non-serializable
        # payload; `ValueError` covers things like `os.fdopen` on a
        # closed fd. Folding them in keeps the `bool` contract intact for
        # the UI branches that toggle on the return value.
        logger.exception("Could not save %s.%s preference", section, field)
        return False
    else:
        _invalidate_config_caches(config_path)
        return True


# [해설] `[goals].auto_accept_criteria` 저장 — Auto 모드에서 생성된 목표 기준을 검토 없이 적용할지(`analysis/05-*` 참고).
def save_goal_auto_accept_criteria(
    enabled: bool,
    config_path: Path | None = None,
) -> bool:
    """Persist whether Auto mode applies generated goal criteria without review.

    Args:
        enabled: Whether Auto should accept goal criteria automatically.
        config_path: Path to config file. Defaults to
            `~/.deepagents/config.toml`.

    Returns:
        `True` when the preference was saved, otherwise `False`.
    """
    return _save_toml_field(
        "goals",
        "auto_accept_criteria",
        enabled,
        config_path,
    )


# [해설] `[models].<field>` 저장 전 allowlist 정책 검사(위반 시 예외). default/auto_classifier 저장이 공유한다.
# [해설][주의] config_path를 명시하면 managed 정책이 빠진 채 검사된다 — 운영 코드는 None을 넘겨야 한다.
def _save_model_field(
    field: str, model_spec: str, config_path: Path | None = None
) -> bool:
    """Enforce `models.allowed`, then read-modify-write a `[models].<field>` key.

    Args:
        field: Key name under the `[models]` table (e.g., `'default'` or `'recent'`).
        model_spec: The model to save in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.

    Raises:
        ModelNotAllowedError: If `model_spec` is outside the effective
            `models.allowed` policy. Distinct from the `False` return so callers
            do not report a policy refusal as a filesystem problem; best-effort
            callers that genuinely cannot act on it suppress it explicitly.
    """  # noqa: DOC502 - propagates from `_save_model_field`
    # `load(config_path)` skips the managed layer, so an explicit path checks
    # only the user allowlist. Every production caller passes `None`; keep it
    # that way or this refusal stops enforcing administrator policy.
    ModelConfig.load(config_path).require_model_allowed(model_spec)
    return _save_toml_field("models", field, model_spec, config_path)


# [해설] `[models].default` 저장(`/model --default` 류 동작, 추정). 공식 문서상 default가 recent보다 우선.
def save_default_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Update the default model in config file.

    Reads existing config (if any), updates `[models].default`, and writes
    back using proper TOML serialization.

    Args:
        model_spec: The model to set as default in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.

    Raises:
        ModelNotAllowedError: If `model_spec` is outside the effective
            `models.allowed` policy.

    Note:
        This function does not preserve comments in the config file.
    """  # noqa: DOC502 - propagates from `_save_model_field`
    return _save_model_field("default", model_spec, config_path)


# [해설] `[models].auto_classifier` 저장 — Auto 승인 분류기 모델(`analysis/04-*`). 실행 시 CLI 플래그 > env > 이 값.
def save_auto_classifier_model(
    model_spec: str, config_path: Path | None = None
) -> bool:
    """Persist the model the Auto approval classifier reviews actions with.

    Writes `[models].auto_classifier`, the persistent counterpart of the
    session-only `/auto model` switch. Both `--auto-classifier-model` and a
    `DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL` export outrank the stored value at
    launch (flag > env > this key), so a successful write does not guarantee the
    next launch reviews with it.

    Args:
        model_spec: The classifier model in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.

    Raises:
        ModelNotAllowedError: If `model_spec` is outside the effective
            `models.allowed` policy.

    Note:
        This function does not preserve comments in the config file.
    """  # noqa: DOC502 - propagates from `_save_model_field`
    return _save_model_field("auto_classifier", model_spec, config_path)


# [해설] `[models].default` 삭제 → 다음 실행부터 recent 또는 env 자동 감지로 폴백.
def clear_default_model(config_path: Path | None = None) -> bool:
    """Remove the default model from the config file.

    Deletes the `[models].default` key so that future launches fall back to
    `[models].recent` or environment auto-detection.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if the key was removed or was already absent, False when the config
            file could not be read or written or its `[models]` section is not a
            table. See `_clear_model_field` for the full contract.
    """
    return _clear_model_field("default", config_path)


# [해설] `[models].auto_classifier` 삭제 → 메인 에이전트 모델로 분류(플래그/env가 없을 때).
def clear_auto_classifier_model(config_path: Path | None = None) -> bool:
    """Remove the stored Auto classifier model from the config file.

    Deletes the `[models].auto_classifier` key so future launches review gated
    actions with the main agent model, unless `--auto-classifier-model` or
    `DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL` supplies one — both outrank this key,
    so clearing it does not guarantee the main agent model is used.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if the key was removed or was already absent, False when the config
            file could not be read or written or its `[models]` section is not a
            table. See `_clear_model_field` for the full contract.

    Note:
        This function does not preserve comments in the config file.
    """
    return _clear_model_field("auto_classifier", config_path)


# [해설] `[models].<field>` 삭제 공통 헬퍼. 파일/섹션/키가 없으면 True(이미 깨끗함), `[models]`가 테이블이 아니면 False(수동 수리 필요).
def _clear_model_field(field: str, config_path: Path | None = None) -> bool:
    """Delete a `[models].<field>` key from the config file.

    Args:
        field: Key name under the `[models]` table (e.g., `'default'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if the key was removed, or was already absent because the file or
            its `[models]` table does not exist. False on I/O error, on
            unparseable TOML, and when `[models]` is present but is not a table
            — nothing can be deleted from those and the file needs hand repair,
            so callers must not report a clean clear.

    Note:
        This function does not preserve comments in the config file.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        with _config_write_lock:
            if not config_path.exists():
                return True  # Nothing to clear

            with config_path.open("rb") as f:
                data = tomllib.load(f)

            models_section = data.get("models")
            if models_section is None:
                # No `[models]` table at all — an ordinary config that simply
                # never stored a model. Nothing to clear and nothing to report.
                return True
            if not isinstance(models_section, dict):
                # Valid TOML of the wrong shape (e.g. a scalar `models = 1`).
                # There is no key to delete and the file needs hand repair, so
                # report failure: `True` is this contract's clean-clear signal,
                # and callers relay it to the user as "cleared".
                logger.warning(
                    "Config file %s has a non-table [models] section (%s); "
                    "cannot clear models.%s",
                    config_path,
                    type(models_section).__name__,
                    field,
                )
                return False
            if field not in models_section:
                return True  # Already absent

            del models_section[field]

            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        # See `_save_toml_field` for why `TypeError` / `ValueError` are
        # folded into the bool return contract.
        logger.exception("Could not clear models.%s preference", field)
        return False
    else:
        _invalidate_config_caches(config_path)
        return True


# [해설] 모델별 reasoning effort 선택을 `[effort.by_model]."provider:model"`에 저장. 사용처: `reasoning_effort.py`/effort 선택 UI(추정).
def save_effort_for_model(
    model_spec: str,
    effort: str,
    config_path: Path | None = None,
) -> bool:
    """Persist the selected reasoning effort for a model.

    Args:
        model_spec: Model in `provider:model` format.
        effort: Reasoning effort label selected by the user.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        `True` if save succeeded, `False` if it failed.
    """
    return _update_effort_for_model(model_spec, effort, config_path)


# [해설] 모델별 저장 effort 로드(managed 병합). 형태가 틀리면 경고 후 None — 저장 안 됨과 구분하지 않는다.
def load_effort_for_model(
    model_spec: str,
    config_path: Path | None = None,
) -> str | None:
    """Load the selected reasoning effort for a model.

    Reads managed config merged over `config.toml`, so a managed `[effort]`
    still applies when the user file is unusable.

    Args:
        model_spec: Model in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`. Passing a path also excludes
            managed policy from this read, so production callers must pass
            `None`.

    Returns:
        The persisted effort label, or `None`. `None` is returned both when no
        preference is stored and when neither layer can supply one (unreadable
        file, invalid TOML, or a malformed `[effort]` section); the two cases
        are not distinguished by the return value, but a read failure is always
        logged rather than swallowed silently.
    """
    # [해설][흐름] 테이블 형태를 단계별(`[effort]` → `by_model` → 값 문자열)로 검증하며 내려간다.
    try:
        data, config_path = _load_effective_config_data(config_path)
        effort_section = data.get("effort")
        if effort_section is None:
            return None  # No preference stored; not a failure.
        if not isinstance(effort_section, dict):
            logger.warning(
                "Ignoring malformed [effort] in %s: expected a table, got %s",
                _effective_source_label(config_path),
                type(effort_section).__name__,
            )
            return None
        by_model = effort_section.get("by_model")
        if by_model is None:
            return None
        if not isinstance(by_model, dict):
            logger.warning(
                "Ignoring malformed [effort.by_model] in %s: expected a table, got %s",
                _effective_source_label(config_path),
                type(by_model).__name__,
            )
            return None
        effort = by_model.get(model_spec)
        if effort is None:
            return None
        if not isinstance(effort, str):
            logger.warning(
                "Ignoring malformed reasoning effort for %s in %s: expected a "
                "string, got %s",
                model_spec,
                _effective_source_label(config_path),
                type(effort).__name__,
            )
            return None
        return effort.strip() or None
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception(
            "Could not load reasoning effort preference for %s", model_spec
        )
        return None


# [해설] 모델별 effort 삭제(= `_update_effort_for_model(..., None)`).
def clear_effort_for_model(
    model_spec: str,
    config_path: Path | None = None,
) -> bool:
    """Remove the selected reasoning effort for a model.

    Args:
        model_spec: Model in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        `True` if the entry was removed or absent, `False` if clearing failed.
    """
    return _update_effort_for_model(model_spec, None, config_path)


# [해설] `[effort.by_model]` 한 항목 저장/삭제 공통 로직. 삭제로 테이블이 비면 상위 테이블까지 정리한다.
def _update_effort_for_model(
    model_spec: str,
    effort: str | None,
    config_path: Path | None = None,
) -> bool:
    """Read-modify-write one entry in `[effort.by_model]`.

    Args:
        model_spec: Model in `provider:model` format.
        effort: Reasoning effort label to save, or `None` to clear it.
        config_path: Path to config file.

    Returns:
        `True` if the update succeeded, `False` if it failed.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    if effort is None and not config_path.exists():
        return True

    # [해설] 기존 값이 테이블이 아니면 TypeError로 중단(아래 except에서 False로 변환) — 손상된 파일을 덮어쓰지 않기 위함.
    def _require_table(value: object, name: str) -> dict:
        if not isinstance(value, dict):
            msg = f"{name} must be a table"
            raise TypeError(msg)
        return value

    # [해설][흐름] 1) 락 + 읽기 → 2) 테이블 보장 → 3) 삭제 또는 설정 → 4) 원자적 기록 → 5) 캐시 무효화.
    try:
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            effort_section = _require_table(data.setdefault("effort", {}), "[effort]")
            by_model = _require_table(
                effort_section.setdefault("by_model", {}), "[effort.by_model]"
            )

            if effort is None:
                if model_spec not in by_model:
                    return True
                del by_model[model_spec]
                if not by_model:
                    del effort_section["by_model"]
                if not effort_section:
                    del data["effort"]
            else:
                by_model[model_spec] = effort

            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception(
            "Could not update reasoning effort preference for %s", model_spec
        )
        return False
    else:
        _invalidate_config_caches(config_path)
        return True


# [해설] `[warnings].suppress` 목록에 키가 있는지(예: `ripgrep` 경고 숨김). managed 병합 읽기.
# [해설][설계] 형태 오류 시 False(억제하지 않음) — 오타 때문에 경고가 조용히 사라지지 않게 fail-open.
def is_warning_suppressed(key: str, config_path: Path | None = None) -> bool:
    """Check if a warning key is suppressed in the config file.

    Reads the `[warnings].suppress` list from managed config merged over
    `config.toml` and checks whether `key` is present. A managed suppression
    still applies when the user file is unusable.

    Args:
        key: Warning identifier to check (e.g., `'ripgrep'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`. Passing a path also
            excludes managed policy from this read, so production callers must
            pass `None`.

    Returns:
        `True` if the warning is suppressed, `False` otherwise (including
            when neither layer supplies the key, or the `[warnings]` section is
            missing or malformed).
    """
    try:
        data, config_path = _load_effective_config_data(config_path)
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug(
            "Could not read config file %s for warning suppression check",
            config_path,
            exc_info=True,
        )
        return False

    # A hand-edited `warnings = [...]` (or any non-table) would make the
    # chained `.get` below raise `AttributeError`; fail open instead so a
    # typo can never silently mute a warning.
    warnings_section = data.get("warnings", {})
    if not isinstance(warnings_section, dict):
        logger.debug(
            "[warnings] in %s should be a table, got %s",
            config_path,
            type(warnings_section).__name__,
        )
        return False

    suppress_list = warnings_section.get("suppress", [])
    if not isinstance(suppress_list, list):
        logger.debug(
            "[warnings].suppress in %s should be a list, got %s",
            config_path,
            type(suppress_list).__name__,
        )
        return False
    return key in suppress_list


# [해설] 경고 억제 키 추가(bool 결과). 실패 원인이 필요하면 `suppress_warning_reason` 사용.
def suppress_warning(key: str, config_path: Path | None = None) -> bool:
    """Add a warning key to the suppression list in the config file.

    Reads existing config (if any), adds `key` to `[warnings].suppress`,
    and writes back using atomic temp-file rename. Deduplicates entries.

    Args:
        key: Warning identifier to suppress (e.g., `'ripgrep'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        `True` if save succeeded, `False` if it failed (I/O error, unparseable
            file, or a malformed `[warnings]` section). Callers that surface
            the failure to the user should prefer `suppress_warning_reason`,
            which distinguishes those causes.
    """
    return suppress_warning_reason(key, config_path) is None


# [해설] 경고 억제 키 추가 + 실패 원인 문구 반환(None=성공). 원인에 따라 사용자에게 다른 해결책을 안내하기 위함.
def suppress_warning_reason(key: str, config_path: Path | None = None) -> str | None:
    """Suppress a warning, reporting *why* the save failed when it does.

    Same write as `suppress_warning`. The distinct causes matter because they
    have different fixes: a malformed `[warnings]` section is one line of TOML
    the user can correct, while telling them to check file permissions -- the
    only advice a bare `False` supports -- sends them to `chmod` a file that
    was never unwritable.

    Args:
        key: Warning identifier to suppress (e.g., `'ripgrep'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        `None` when the save succeeded, otherwise a short phrase naming the
            cause, suitable for interpolating into a user-facing message.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    # [해설][흐름] 1) 락 + 읽기 → 2) `[warnings]` 테이블 검사(아니면 원인 반환) → 3) 중복 없이 추가 → 4) 원자적 기록.
    try:
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)

            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            if "warnings" not in data:
                data["warnings"] = {}
            # A hand-edited `warnings = [...]` (or any non-table) would make
            # `.get("suppress")` below raise `AttributeError`, and the callers'
            # detached tasks cannot surface a raised exception — report the
            # failure so the caller can fall back to an in-app warning.
            if not isinstance(data["warnings"], dict):
                # Warning, not debug: this is a user-fixable config error whose
                # only other symptom is a preference that silently fails to
                # save.
                logger.warning(
                    "[warnings] in %s should be a table, got %s",
                    config_path,
                    type(data["warnings"]).__name__,
                )
                return f"[warnings] in {config_path} is not a table"
            suppress_list = data["warnings"].get("suppress", [])
            if not isinstance(suppress_list, list):
                logger.debug(
                    "[warnings].suppress in %s should be a list, got %s",
                    config_path,
                    type(suppress_list).__name__,
                )
                suppress_list = []
            if key not in suppress_list:
                suppress_list.append(key)
            data["warnings"]["suppress"] = suppress_list

            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    # [해설][흐름] 예외 종류별 원인 문구(잘못된 TOML / 쓰기 실패). 성공 시 캐시 무효화.
    except tomllib.TOMLDecodeError:
        logger.exception("Could not save warning suppression for '%s'", key)
        return f"{config_path} is not valid TOML"
    except OSError:
        logger.exception("Could not save warning suppression for '%s'", key)
        return f"{config_path} could not be written"
    _invalidate_config_caches(config_path)
    return None


# [해설] 경고 억제 키 제거. 파일·키가 없으면 no-op True.
def unsuppress_warning(key: str, config_path: Path | None = None) -> bool:
    """Remove a warning key from the suppression list in the config file.

    Reads existing config (if any), removes `key` from `[warnings].suppress`,
    and writes back using atomic temp-file rename. No-op if the key is not
    present or the file does not exist.

    Args:
        key: Warning identifier to unsuppress (e.g., `'ripgrep'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        `True` if save succeeded, `False` if it failed (I/O error, unparseable
            file, or a malformed `[warnings]` section).
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        with _config_write_lock:
            if not config_path.exists():
                return True  # nothing to remove

            with config_path.open("rb") as f:
                data = tomllib.load(f)

            warnings_section = data.get("warnings", {})
            if not isinstance(warnings_section, dict):
                logger.debug(
                    "[warnings] in %s should be a table, got %s",
                    config_path,
                    type(warnings_section).__name__,
                )
                return False
            suppress_list = warnings_section.get("suppress", [])
            if not isinstance(suppress_list, list):
                logger.debug(
                    "[warnings].suppress in %s should be a list, got %s",
                    config_path,
                    type(suppress_list).__name__,
                )
                return True  # treat as nothing to remove
            if key not in suppress_list:
                return True  # already unsuppressed

            suppress_list.remove(key)
            warnings_section["suppress"] = suppress_list

            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not remove warning suppression for '%s'", key)
        return False
    _invalidate_config_caches(config_path)
    return True


# [해설] ── 이하 MCP 프로젝트 서버 신뢰 목록(config.toml) 영역 — `analysis/07-mcp-hooks-extensions-plugins.md` 참고 ──
# [해설] MCP 신뢰 식별자: Git common dir(워크트리 공유) 또는 정확한 프로젝트 루트, 그리고 그 종류 플래그.
class _McpProjectScope(NamedTuple):
    """A resolved MCP trust identity and whether it is Git-common scoped.

    A `NamedTuple` (mirroring `_git.RepositoryMetadata`) so the boolean slot is
    self-documenting at every call site instead of a load-bearing positional.
    """

    identity: str
    """Normalized trust identity: a Git common directory or an exact root."""

    git_common_dir: bool
    """Whether `identity` is a validated Git common-directory path."""


# [해설] 프로젝트 `.mcp.json`의 서버 하나에 대한 사용자 승인 레코드(프로젝트 식별자 + 서버 이름 + 정의 지문).
# [해설] 승인 집합에 "값이 같은" 객체가 있는지로 신뢰를 판정하므로, 쓰기(`add_enabled_project_mcp_servers`)와 읽기(`is_enabled`)가
# [해설] 반드시 같은 팩토리(`create`/`from_toml`)로 정규화해야 한다. 정의가 바뀌면 지문이 달라져 재승인을 요구한다.
# [해설][설계] 잘못 만든 인스턴스는 무엇과도 같지 않으므로 fail-closed(아무것도 신뢰하지 않음).
@dataclass(frozen=True, order=True)
class McpProjectServerApproval:
    """A project-scoped, definition-bound MCP server approval.

    Membership in a `McpServerTrustLists.approvals` set *is* the trust decision
    (`is_enabled` reconstructs an approval and tests `approval in approvals`), so
    value equality must line up between the write side
    (`add_enabled_project_mcp_servers`) and the read side (`is_enabled`). Build new
    approvals through `create` and persisted ones through `from_toml`, never the raw
    constructor. Legacy unmarked entries intentionally retain their exact-worktree
    scope, while new entries reconstruct the same transport-aware scope on both
    sides. `order=True` exists only so `sorted()` yields deterministic TOML output.

    The raw constructor only enforces non-emptiness (`__post_init__`), not that
    `project_root` is normalized or that `fingerprint` is a real digest. A
    hand-built instance is therefore *safe but useless*: with a mismatched root
    or fingerprint it simply never equals a `create`/`from_toml` peer, so it
    fails closed (nothing is trusted) rather than granting stray access — but it
    also won't authorize anything. Always go through the factories.
    """

    project_root: str
    """Shared fixed-URL identity or exact worktree-scoped identity."""

    name: str
    """MCP server name within the project config."""

    fingerprint: str
    """Fingerprint of the approved MCP server definition."""

    git_common_dir: bool = field(default=False, kw_only=True)
    """Whether `project_root` is a persisted Git common-directory identity."""

    # [해설] 빈 필드 승인은 저장 자체를 금지(영원히 매치되지 않는 쓰레기 레코드 방지).
    def __post_init__(self) -> None:
        """Reject degenerate approvals so a bad one can't silently never match.

        An empty `project_root`, `name`, or `fingerprint` can only ever equal a
        malformed peer, so forbid the state entirely rather than let it persist.

        Raises:
            ValueError: If any field is empty or whitespace-only.
        """
        if not (
            self.project_root.strip() and self.name.strip() and self.fingerprint.strip()
        ):
            msg = (
                "McpProjectServerApproval requires non-empty project_root, name, "
                "and fingerprint"
            )
            raise ValueError(msg)

    # [해설] 이미 해석된 scope로 승인 객체 생성(이름 strip + 정의 SHA-256 지문). `create`와 `is_enabled`의 레거시 경로가 공유.
    @classmethod
    def _create_for_scope(
        cls,
        *,
        scope: _McpProjectScope,
        name: str,
        server: JsonValue,
    ) -> McpProjectServerApproval:
        """Build an approval from one already-resolved trust scope.

        Args:
            scope: Normalized identity and Git-common marker.
            name: MCP server name.
            server: Parsed MCP server definition to fingerprint.

        Returns:
            The normalized, definition-bound approval.
        """
        return cls(
            project_root=scope.identity,
            name=name.strip(),
            fingerprint=fingerprint_mcp_server_config(server),
            git_common_dir=scope.git_common_dir,
        )

    # [해설] 새 승인 생성 팩토리. 고정 URL 원격 서버는 Git common dir 기준(링크된 워크트리 간 공유),
    # [해설] 로컬 명령·보간 URL 서버는 정확한 워크트리 루트 기준(체크아웃마다 동작이 달라질 수 있어서).
    @classmethod
    def create(
        cls, *, project_root: str | Path | None, name: str, server: JsonValue
    ) -> McpProjectServerApproval | None:
        """Build an approval, normalizing the root and fingerprinting `server`.

        Remote servers with fixed URLs use the validated Git common directory so
        their approvals can be shared across linked worktrees. Local commands and
        remote definitions with interpolated URLs use the exact resolved worktree
        because their behavior can differ between checkouts.

        Args:
            project_root: Project root to normalize.
            name: MCP server name.
            server: Parsed MCP server definition to fingerprint.

        Returns:
            The approval, or `None` when `project_root` cannot be normalized.
        """
        scope = _normalize_mcp_project_scope(
            project_root,
            share_across_worktrees=_mcp_server_uses_remote_transport(server),
        )
        if scope is None:
            return None
        return cls._create_for_scope(scope=scope, name=name, server=server)

    # [해설] config.toml에 저장된 승인 테이블 역직렬화. 형식 오류면 None(허용 목록이므로 fail-closed).
    # [해설] `git_common_dir=true` 표식 항목은 저장된 경로를 그대로 정규화만 하고 재탐색하지 않는다(메타데이터 조작으로 상위 저장소로 확장 방지).
    @classmethod
    def from_toml(cls, item: Mapping[str, object]) -> McpProjectServerApproval | None:
        """Deserialize a persisted approval table, normalizing the root.

        Legacy entries without `git_common_dir` remain scoped to their exact
        stored worktree. Marked entries retain their exact Git identity, so stale
        metadata cannot redirect them to an enclosing repository.

        Args:
            item: A parsed TOML table with `project_root`, `name`, and
                `fingerprint` string fields plus an optional `git_common_dir`
                boolean.

        Returns:
            The approval, or `None` for a malformed table — fail-closed for an
            allowlist.
        """
        project_root = item.get("project_root")
        name = item.get("name")
        fingerprint = item.get("fingerprint")
        git_common_dir = item.get("git_common_dir", False)
        if not (
            isinstance(project_root, str)
            and project_root.strip()
            and isinstance(name, str)
            and name.strip()
            and isinstance(fingerprint, str)
            and fingerprint.strip()
            and isinstance(git_common_dir, bool)
        ):
            return None

        if git_common_dir:
            normalized_root = _normalize_persisted_git_common_dir(project_root)
            normalized_is_common = True
        else:
            scope = _normalize_mcp_project_scope(
                project_root, share_across_worktrees=False
            )
            if scope is None:
                return None
            normalized_root, normalized_is_common = scope.identity, scope.git_common_dir
        if normalized_root is None:
            return None
        return cls(
            project_root=normalized_root,
            name=name.strip(),
            fingerprint=fingerprint.strip(),
            git_common_dir=normalized_is_common,
        )

    # [해설] TOML 직렬화 형태. 레거시 호환을 위해 `git_common_dir`은 True일 때만 기록.
    def as_toml(self) -> dict[str, str | bool]:
        """Return a TOML-serializable representation."""
        item: dict[str, str | bool] = {
            "project_root": self.project_root,
            "name": self.name,
            "fingerprint": self.fingerprint,
        }
        if self.git_common_dir:
            item["git_common_dir"] = True
        return item


# [해설] MCP 신뢰 식별자 해석: 경로 확장·resolve 후, 공유 허용이면 `_git.find_git_common_dir`로 common dir 사용.
# [해설][설계] resolve 실패(OSError)는 미해석 경로로 폴백 — 양쪽 문자열이 달라져 재질문(안전한 방향)만 생긴다.
def _normalize_mcp_project_scope(
    project_root: str | Path | None,
    *,
    share_across_worktrees: bool,
) -> _McpProjectScope | None:
    """Resolve an MCP trust identity and whether it is Git-common scoped.

    Args:
        project_root: Project root path to normalize.
        share_across_worktrees: Whether a validated Git common directory may be
            used instead of the exact worktree root.

    Returns:
        One of three outcomes:

        - `(<git-common-dir>, True)` when `share_across_worktrees` is set and the
          resolved root validates as a Git worktree.
        - `(<resolved-root>, False)` otherwise.
        - `(<unresolved-expanded-root>, False)` when `resolve()` raises `OSError`;
          the returned string is the expanded-but-unresolved path. A transient
          resolve failure on only one of the write/read sides then yields
          different identity strings and a spurious re-prompt (fail-closed),
          never a false match.

        Returns `None` only when `project_root` is `None`, cannot be expanded, or
        resolution detects a path loop (`RuntimeError`).
    """
    if project_root is None:
        return None
    try:
        expanded_root = Path(project_root).expanduser()
    except (OSError, RuntimeError):
        logger.warning(
            "Could not expand MCP project root %s",
            project_root,
            exc_info=True,
        )
        return None

    try:
        resolved_root = expanded_root.resolve()
    except OSError:
        logger.warning(
            "Could not resolve MCP project root %s",
            project_root,
            exc_info=True,
        )
        return _McpProjectScope(str(expanded_root), False)
    except RuntimeError:
        logger.warning(
            "Could not resolve MCP project root %s",
            project_root,
            exc_info=True,
        )
        return None

    if share_across_worktrees:
        common_dir = find_git_common_dir(resolved_root)
        if common_dir is not None:
            return _McpProjectScope(str(common_dir), True)
    return _McpProjectScope(str(resolved_root), False)


# [해설] 저장된 Git common dir 식별자를 링크를 따라가지 않고(`os.path.abspath`) 정규화. 상대경로면 승인 폐기.
def _normalize_persisted_git_common_dir(project_root: str) -> str | None:
    """Normalize a marked Git identity without following or rediscovering it.

    Args:
        project_root: Persisted Git common-directory path.

    Returns:
        The absolute lexical path, or `None` for an invalid stored identity.
    """
    try:
        expanded_root = Path(project_root).expanduser()
    except (OSError, RuntimeError):
        logger.warning(
            "Could not expand persisted MCP Git identity %s",
            project_root,
            exc_info=True,
        )
        return None
    if not expanded_root.is_absolute():
        logger.warning(
            "Persisted MCP Git identity %s is not absolute; dropping approval",
            project_root,
        )
        return None
    try:
        return os.path.abspath(expanded_root)  # noqa: PTH100  # do not follow links
    except (OSError, RuntimeError, ValueError):
        logger.warning(
            "Could not normalize persisted MCP Git identity %s",
            project_root,
            exc_info=True,
        )
        return None


# [해설] "원격 전용"으로 보는 MCP transport 이름들. 워크트리 간 승인 공유 판단에 사용.
_REMOTE_MCP_TRANSPORTS = frozenset(
    {"http", "sse", "streamable_http", "streamable-http"}
)


# [해설] 서버 정의가 확실히 원격 전용인지: `command`가 없고, URL이 문자열이며 `${...}` 보간이 없고, transport가 원격 계열.
# [해설][주의] 보간 URL은 워크트리별 `.env`에 따라 다른 엔드포인트가 될 수 있어 공유하지 않는다.
def _mcp_server_uses_remote_transport(server: JsonValue) -> bool:
    """Return whether `server` is confidently a remote-only definition.

    Malformed, ambiguous, or environment-dependent definitions stay
    worktree-scoped. A definition containing `command` is never shared even if it
    also contains a remote transport field, and an interpolated URL can resolve to
    different endpoints from different worktree `.env` files.

    Args:
        server: Parsed MCP server definition.

    Returns:
        Whether approvals for the definition may be shared across worktrees.
    """
    if not isinstance(server, dict) or "command" in server:
        return False
    url = server.get("url")
    if not isinstance(url, str) or "${" in url:
        return False
    transport = server.get("type") or server.get("transport")
    return transport is None or (
        isinstance(transport, str) and transport in _REMOTE_MCP_TRANSPORTS
    )


# [해설] 공개 헬퍼: 워크트리 정확 루트 정규화(공유 없음). MCP 관련 외부 모듈이 비교용으로 사용(추정).
def normalize_mcp_project_root(project_root: str | Path | None) -> str | None:
    """Normalize an exact project root for persisted MCP trust comparisons.

    Args:
        project_root: Project root path to normalize.

    Returns:
        The resolved absolute project root (or the expanded, unresolved path when
        `resolve()` raises `OSError`), or `None` when `project_root` is
        unavailable.
    """
    scope = _normalize_mcp_project_scope(project_root, share_across_worktrees=False)
    return scope.identity if scope is not None else None


# [해설] MCP 서버 정의의 안정적 지문(`sha256:` + 키 정렬 JSON). 필드 순서 변경은 재승인을 유발하지 않지만 값 변경은 유발한다.
def fingerprint_mcp_server_config(server: JsonValue) -> str:
    """Return a stable fingerprint for an MCP server definition.

    The contract is a JSON-serializable value (in practice the `dict` parsed
    from `.mcp.json`, though a malformed entry may be any JSON scalar/array); a
    non-serializable input raises `TypeError` from `json.dumps`. `sort_keys=True`
    makes the digest independent of key order, so reordering fields in the config
    does not force a re-prompt.

    Args:
        server: Parsed MCP server config (a JSON-serializable value).

    Returns:
        A SHA-256 fingerprint over the canonical JSON representation.
    """
    encoded = json.dumps(
        server,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


# [해설] 프로젝트 MCP 서버 허용/거부 목록 묶음. 출처는 사용자 소유 설정뿐(홈 config.toml, 전역 `.env`, 셸 env) —
# [해설] 저장소에 커밋된 `.mcp.json`이 스스로를 승인할 수 없게 하는 보안 경계.
# [해설] 호출자: `main.py`(시작 시 프로젝트 MCP 신뢰 판단), `mcp_tools.py`, `mcp_login_service.py`.
# [해설][설계] "거부가 이긴다" 불변식을 `__post_init__`에서 강제.
@dataclass(frozen=True)
class McpServerTrustLists:
    """User-level allow/deny lists for project MCP servers.

    Sourced only from the user's own configuration — the home `config.toml`, the
    global `~/.deepagents/.env`, and shell-exported env — never from a repo, so a
    committed `.mcp.json` cannot self-approve. Persisted approvals for fixed
    remote URLs bind to one validated local Git repository. Local commands and
    interpolated remote URLs bind to the exact resolved worktree. All include the
    server definition's fingerprint. Env-sourced approvals remain explicit
    process-wide name approvals.

    The "reject wins" invariant — a name in both approval and rejection data is
    only rejected — is enforced in `__post_init__`, so every instance is disjoint
    no matter how it was constructed; callers need not pre-subtract.
    """

    enabled: frozenset[str]
    """Env-sourced server names pre-approved for any project config."""

    disabled: frozenset[str]
    """Server names always rejected; reject wins over approvals and over trust."""

    read_error: str | None = field(default=None, compare=False)
    """Non-`None` when the user's `config.toml` existed but its trust policy
    could not be fully read: the file was unreadable/unparseable, its `[mcp]`
    value was not a table, or its `disabled_project_servers` was a wrong type
    that could not be interpreted as a deny list. Callers must treat this as
    fail-closed (do not grant whole-config project trust) and surface it, rather
    than proceeding with a deny list that may not have loaded — use `load_failed`
    for that check. Note the resolved `enabled`/`disabled` sets are not
    necessarily empty here: names from a still-readable source (the env vars)
    continue to apply. Excluded from equality so a failed load still compares
    equal to empty lists for tests that only care about the resolved names."""

    approvals: frozenset[McpProjectServerApproval] = field(
        default_factory=frozenset, kw_only=True
    )
    """Project-scoped approvals loaded from user `config.toml`."""

    legacy_ignored: frozenset[str] = field(
        default_factory=frozenset, compare=False, kw_only=True
    )
    """Names found in a legacy `[mcp].enabled_project_servers` list that this
    build no longer honors. Non-empty means the user relied on the removed flat
    allowlist, so those servers silently stopped loading; callers should surface
    it (a bare `logger.warning` is invisible outside debug mode) so
    non-interactive paths can explain the change. Diagnostic, not resolved
    policy — excluded from equality like `read_error`."""

    legacy_env_ignored: bool = field(default=False, compare=False, kw_only=True)
    """`True` when the removed `DEEPAGENTS_CODE_ENABLED_PROJECT_MCP_SERVERS` env
    var is set. It was renamed to the `DANGEROUSLY_`-prefixed var and is no longer
    read, so its names silently stopped pre-approving. The diagnostic twin of
    `legacy_ignored` for the env surface; callers should surface the rename so the
    change is not silent. Excluded from equality like `read_error`."""

    malformed_approvals: int = field(default=0, compare=False, kw_only=True)
    """Count of `[mcp].enabled_project_server_approvals` rows that were dropped as
    malformed (wrong-typed key, non-table entry, a table missing/blank
    `project_root`/`name`/`fingerprint`, or an invalid Git identity marker).
    Non-zero means a persisted approval
    could not be read, so its server silently re-prompts; callers should surface
    it (a bare `logger.warning` is invisible outside debug mode) for parity with
    `legacy_ignored`. Diagnostic, not resolved policy — excluded from equality."""

    # [해설] disabled에 있는 이름을 enabled와 approvals에서 제거(어떤 순서로 조회해도 거부가 이기게).
    def __post_init__(self) -> None:
        """Enforce reject precedence by stripping disabled names from both sets.

        A rejected name must never survive in `enabled` or `approvals`, whatever
        the caller passed, so a future allow-first consumer can't be tricked
        into loading a denied server. Frozen dataclass, so assign via
        `object.__setattr__`.
        """
        if self.enabled & self.disabled:
            object.__setattr__(self, "enabled", self.enabled - self.disabled)
        if any(approval.name in self.disabled for approval in self.approvals):
            object.__setattr__(
                self,
                "approvals",
                frozenset(
                    approval
                    for approval in self.approvals
                    if approval.name not in self.disabled
                ),
            )

    # [해설][주의] 신뢰 판단 호출자는 반드시 이 값을 확인해 fail-closed 해야 한다(거부 목록이 로드되지 않았을 수 있음).
    @property
    def load_failed(self) -> bool:
        """Whether the user's trust policy failed to load (see `read_error`).

        Callers gating on trust MUST check this and fail closed: a failed load
        means a configured deny may be missing, so whole-config project trust
        must not be honored. Named so the fail-closed contract is discoverable
        rather than resting on every caller remembering the `read_error`
        sentinel.
        """
        return self.read_error is not None

    # [해설] 서버 로드 허용 판정: 빈 이름 거부 → disabled 거부 → env 이름 승인 → 스코프·지문 승인 → (Git 공유 스코프일 때) 레거시 정확 루트 승인.
    def is_enabled(
        self,
        name: str,
        *,
        project_root: str | Path | None,
        server: JsonValue,
    ) -> bool:
        """Return whether `server` is approved by name or scoped fingerprint.

        Args:
            name: MCP server name.
            project_root: Resolved project root for the config that defined it.
            server: Parsed MCP server config for fingerprint comparison.

        Returns:
            `True` when the server is approved and not disabled.
        """
        if not name.strip():
            # A blank name can only come from a malformed config. Fail closed
            # here rather than let `McpProjectServerApproval.create` raise
            # `ValueError` from its non-empty invariant out of the trust filter.
            return False
        # These membership tests use the raw (unstripped) `name`, while the
        # approval path below strips it via `create`. Reject precedence for a
        # whitespace-padded name (e.g. `" docs "` vs `disabled={"docs"}`) does
        # NOT rest on this check — it survives only because `__post_init__`
        # already stripped every disabled name out of `enabled` and `approvals`
        # (it compares the always-stripped `approval.name`). Keep that stripping
        # in sync with this check: a padded name sails past both lines here.
        if name in self.disabled:
            return False
        if name in self.enabled:
            return True
        approval = McpProjectServerApproval.create(
            project_root=project_root, name=name, server=server
        )
        if approval is None:
            return False
        if approval in self.approvals:
            return True
        if not approval.git_common_dir:
            return False

        # [해설][흐름] 레거시 경로: 표식 없는 옛 승인은 원래 워크트리에서만 유효하도록 정확 루트 scope로 한 번 더 비교.
        # Approvals written before remote servers gained a shared Git identity
        # have no marker and remain bound to their original worktree. Honor that
        # exact-root entry there without broadening it to sibling worktrees.
        legacy_scope = _normalize_mcp_project_scope(
            project_root, share_across_worktrees=False
        )
        if legacy_scope is None:
            return False
        legacy_approval = McpProjectServerApproval._create_for_scope(
            scope=legacy_scope, name=name, server=server
        )
        return legacy_approval in self.approvals


# [해설] 쉼표 구분 env를 이름 리스트로. 미설정(None)과 "설정됐지만 빔"([])을 구분해 TOML 폴백 여부를 결정한다.
def _parse_csv_env(name: str) -> list[str] | None:
    """Parse a comma-separated env var into a list of trimmed, non-empty names.

    Returns:
        The parsed list when the variable is set (possibly empty after
            trimming), or `None` when the variable is unset so callers can
            distinguish "unset, fall back to TOML" from "set but empty".
    """
    from deepagents_code.config import active_environment

    raw = active_environment().get(name)
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


# [해설] `[models]`의 모델 spec 값 타입 가드(문자열 아니면 경고 후 None). `ModelConfig.load`에서 사용.
def _toml_model_spec(
    value: object, *, key: str, path: Path, source_label: str | None = None
) -> str | None:
    """Return a `[models]` model spec, or `None` when it is not a string.

    Args:
        value: The raw TOML value.
        key: The key name inside `[models]`, for log context.
        path: The config file the value came from, for log context.
        source_label: Overrides `path` in the log when the value came from a
            merge of both layers, so the message does not name a file that may
            not hold it. See `_effective_source_label`.

    Returns:
        The spec string, or `None` when the value cannot be one.
    """
    if value is None or isinstance(value, str):
        return value
    logger.warning(
        "Ignoring [models].%s in %s: expected a string, got %s",
        key,
        source_label or path,
        type(value).__name__,
    )
    return None


# [해설] `[models.providers]` 테이블 타입 가드(테이블 아니면 경고 후 빈 dict). `ModelConfig.load`에서 사용.
def _toml_providers_table(
    value: object, *, path: Path, source_label: str | None = None
) -> dict[str, Any]:
    """Return the `[models.providers]` table, or an empty one when unusable.

    Args:
        value: The raw TOML value.
        path: The config file the value came from, for log context.
        source_label: Overrides `path` in the log when the value came from a
            merge of both layers. See `_effective_source_label`.

    Returns:
        The providers table, empty when the value is not a table.
    """
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    logger.warning(
        "Ignoring [models].providers in %s: expected a table, got %s",
        source_label or path,
        type(value).__name__,
    )
    return {}


# [해설] `[mcp]` 이름 목록 강제변환: 문자열은 쉼표 분할(env 형식과 동일), 리스트는 문자열만 유지, 그 외 타입은 malformed 플래그.
# [해설] malformed 플래그는 거부 목록을 fail-closed로 처리하기 위해 호출자(`_ranked_mcp_names`)가 사용한다.
def _toml_str_list(
    value: object, *, key: str, config_path: Path
) -> tuple[list[str], bool]:
    """Coerce a raw TOML value into a list of trimmed, non-empty server names.

    A bare string is *split on commas* (e.g. `disabled_project_servers = "a, b"`
    yields `["a", "b"]`), so a scalar written in the TOML parses identically to
    the comma-separated env form in `_parse_csv_env` — the two forms can never
    silently diverge into one bogus `"a, b"` token that matches no server. Non-
    string list elements are dropped (with a log) while the surrounding valid
    names survive. A genuinely wrong type (number, table, bool) cannot be
    interpreted as names at all: it yields an empty list *and* flags `malformed`,
    so a caller enforcing a deny list can fail closed rather than silently drop
    the rejection.

    Args:
        value: The raw value read from the `[mcp]` table (or `None` when the
            key is absent).
        key: The TOML key name, used only for log context.
        config_path: The config file the value came from, for log context.

    Returns:
        `(names, malformed)`. `names` are the trimmed, non-empty server names.
            `malformed` is `True` only when `value` is present but neither a
            string nor a list (so it could not be read as names); it is `False`
            for an absent value, a string, or any list — even one whose non-
            string elements were dropped.
    """
    if value is None:
        return [], False
    if isinstance(value, str):
        # Split on commas so a bare string parses exactly like the env form; a
        # single name with no comma still yields a one-element list.
        return [item.strip() for item in value.split(",") if item.strip()], False
    if not isinstance(value, list):
        logger.warning(
            "[mcp].%s in %s should be a list of strings, got %s; ignoring it",
            key,
            config_path,
            type(value).__name__,
        )
        return [], True
    result: list[str] = []
    discarded = 0
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        else:
            discarded += 1
    if discarded:
        logger.warning(
            "[mcp].%s in %s: ignored %d non-string or empty entr%s",
            key,
            config_path,
            discarded,
            "y" if discarded == 1 else "ies",
        )
    return result, False


# [해설] `[mcp].enabled_project_server_approvals` 배열 파싱. 잘못된 행은 버리고 개수를 돌려줘 UI가 "승인 손실"을 알릴 수 있게 한다.
def _toml_project_server_approvals(
    value: object, *, config_path: Path
) -> tuple[list[McpProjectServerApproval], int]:
    """Parse `[mcp].enabled_project_server_approvals` entries.

    Args:
        value: Raw TOML value from the `[mcp]` table.
        config_path: Config file the value came from, for log context.

    Returns:
        `(approvals, dropped)`: the well-formed project-scoped approvals and the
            count of malformed rows ignored. Dropping is fail-closed for an
            allowlist; the count lets callers surface the loss (a bare
            `logger.warning` is invisible outside debug mode) so a corrupt saved
            approval doesn't just silently re-prompt.
    """
    if value is None:
        return [], 0
    if not isinstance(value, list):
        logger.warning(
            "[mcp].enabled_project_server_approvals in %s should be a list of "
            "tables; ignoring it",
            config_path,
        )
        # Count the whole-key type error as one dropped diagnostic so it is
        # surfaced rather than only logged.
        return [], 1

    approvals: list[McpProjectServerApproval] = []
    dropped = 0
    for item in value:
        if not isinstance(item, dict):
            logger.warning(
                "[mcp].enabled_project_server_approvals in %s ignored a "
                "non-table entry",
                config_path,
            )
            dropped += 1
            continue
        approval = McpProjectServerApproval.from_toml(
            cast("Mapping[str, object]", item)
        )
        if approval is None:
            logger.warning(
                "[mcp].enabled_project_server_approvals in %s ignored a "
                "malformed entry",
                config_path,
            )
            dropped += 1
            continue
        approvals.append(approval)
    return approvals, dropped


# [해설] 한 설정 계층(managed/user)의 `[mcp]` 루트를 resolver 순위값으로 변환. 테이블이 아니면 `Invalid`(프로젝트 설정 불신).
def _ranked_mcp_section(
    data: Mapping[str, Any],
    *,
    rank: int,
    status: ProviderStatus,
    path: Path,
    managed: bool,
) -> RankedProviderValue[Mapping[str, Any]]:
    """Coerce one provider's `[mcp]` root without losing its health.

    Returns:
        A ranked table, `Unset`, or `Invalid` for a shadowing scalar root.
    """
    from deepagents_code.configuration.resolver import RankedProviderValue
    from deepagents_code.configuration.types import Found, Invalid, Unset

    if not status.usable or "mcp" not in data:
        result = Unset()
    else:
        raw = data["mcp"]
        if isinstance(raw, dict):
            result = Found(cast("Mapping[str, Any]", raw))
        else:
            context = f"managed config {path}" if managed else str(path)
            result = Invalid(
                f"[mcp] in {context} must be a table, got {type(raw).__name__}"
            )
            logger.warning(
                "[mcp] in %s should be a table, got %s; treating project "
                "configs as untrusted",
                "managed config" if managed else path,
                type(raw).__name__,
            )
    return RankedProviderValue(rank, True, status, result)


# [해설] 한 계층의 승인 목록을 resolver 복합 값(`{"approvals": [...]}`)으로 변환.
# [해설][주의] managed 계층에서 형식이 틀리면 `Invalid` — 강제되지 않는 관리자 허용 목록으로 진행하지 않는다.
# [해설] managed 계층은 `enabled: []`를 함께 넣어, 명시적 managed 승인 목록이 env 이름 승인(DANGEROUSLY_ env)까지 대체하게 한다.
def _ranked_mcp_approvals(
    section: RankedProviderValue[Mapping[str, Any]],
    *,
    path: Path,
    managed: bool,
) -> tuple[RankedProviderValue[dict[str, Any]], int]:
    """Coerce one file tier's scoped approvals into composite grant leaves.

    Returns:
        The typed provider plus its malformed-row diagnostic count.
    """
    from deepagents_code.configuration.resolver import RankedProviderValue
    from deepagents_code.configuration.types import Found, Invalid, Unset

    section_result = section.result
    if isinstance(section_result, Invalid):
        result = Invalid(section_result.reason)
        return (
            RankedProviderValue(section.rank, section.durable, section.status, result),
            0,
        )
    if not isinstance(section_result, Found):
        return (
            RankedProviderValue(section.rank, section.durable, section.status, Unset()),
            0,
        )
    key = "enabled_project_server_approvals"
    table = cast("Mapping[str, Any]", section_result.value)
    if key not in table:
        return (
            RankedProviderValue(section.rank, section.durable, section.status, Unset()),
            0,
        )
    raw = table[key]
    approvals, dropped = _toml_project_server_approvals(raw, config_path=path)
    if managed and not isinstance(raw, list):
        reason = (
            f"[mcp].{key} in {path} must be a list of approval entries; "
            "refusing to proceed with an unenforced managed allow list"
        )
        logger.warning(
            "Malformed [mcp].%s in managed config %s; treating project "
            "configs as untrusted",
            key,
            path,
        )
        return (
            RankedProviderValue(
                section.rank,
                section.durable,
                section.status,
                Invalid(reason),
            ),
            dropped,
        )
    value = {
        "approvals": approvals,
        **({"enabled": []} if managed else {}),
    }
    return (
        RankedProviderValue(
            section.rank,
            section.durable,
            section.status,
            Found(value),
        ),
        dropped,
    )


# [해설] 한 계층의 이름 목록(거부 목록 등)을 순위값으로 변환. `fail_closed`이면 잘못된 타입을 `Invalid`로 만든다.
def _ranked_mcp_names(
    section: RankedProviderValue[Mapping[str, Any]],
    *,
    key: str,
    path: Path,
    managed: bool,
    fail_closed: bool,
) -> RankedProviderValue[list[str]]:
    """Coerce one file tier's comma/list server names.

    Returns:
        A typed name-list provider. Wrong-typed deny lists are `Invalid`;
        permissive legacy names instead become an empty `Found` value.
    """
    from deepagents_code.configuration.resolver import RankedProviderValue
    from deepagents_code.configuration.types import Found, Invalid, Unset

    section_result = section.result
    if isinstance(section_result, Invalid):
        result = Invalid(section_result.reason)
    elif not isinstance(section_result, Found):
        result = Unset()
    else:
        table = cast("Mapping[str, Any]", section_result.value)
        if key not in table:
            return RankedProviderValue(
                section.rank, section.durable, section.status, Unset()
            )
        names, malformed = _toml_str_list(
            table[key],
            key=key,
            config_path=path,
        )
        if malformed and fail_closed:
            qualifier = "managed " if managed else ""
            reason = (
                f"[mcp].{key} in {path} must be a list of strings; refusing "
                f"to proceed with an unenforced {qualifier}deny list"
            )
            if managed:
                logger.warning(
                    "Malformed [mcp].%s in managed config %s; treating "
                    "project configs as untrusted",
                    key,
                    path,
                )
            result = Invalid(reason)
        else:
            result = Found(names)
    return RankedProviderValue(section.rank, section.durable, section.status, result)


# [해설] MCP 쉼표 목록 env 하나를 순위값으로 변환(`composite`면 승인 해석용 `{"enabled": names}` 형태).
def _ranked_mcp_env_names(
    name: str,
    *,
    rank: int,
    composite: bool,
) -> RankedProviderValue[Any]:
    """Coerce one MCP comma-list environment provider.

    Returns:
        A ranked list, or a composite enabled-name leaf for approval resolution.
    """
    from deepagents_code.configuration.resolver import RankedProviderValue
    from deepagents_code.configuration.types import (
        Found,
        ProviderHealth,
        ProviderStatus,
        Unset,
    )

    names = _parse_csv_env(name)
    status = ProviderStatus(
        f"env ({name})" if names is not None else "environment",
        None,
        ProviderHealth.OK,
    )
    result = (
        Unset() if names is None else Found({"enabled": names} if composite else names)
    )
    return RankedProviderValue(rank, False, status, result)


# [해설] 프로젝트 MCP 서버 허용/거부 목록을 managed·user config.toml과 프로세스 env에서 해석해 `McpServerTrustLists`로 반환.
# [해설] 호출자: `main.py`(시작 시 `.mcp.json` 서버 로드 여부), `mcp_tools.py`, `mcp_login_service.py`.
# [해설][설계][주의] 보안 경계 — 저장소 안의 어떤 파일도 읽지 않는다. 승인은 순위(managed > env > user) 해석, 거부는 합집합(누적).
# [해설] env: `DEEPAGENTS_CODE_DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS`(허용), `DEEPAGENTS_CODE_DISABLED_PROJECT_MCP_SERVERS`(거부).
def load_mcp_server_trust_lists(
    config_path: Path | None = None,
) -> McpServerTrustLists:
    """Load per-server project MCP allow/deny lists from user-level config.

    Security boundary: this reads the `[mcp]` table only from the user-level
    `config.toml` (`DEFAULT_CONFIG_PATH`, i.e. `~/.deepagents/config.toml`) and
    the `DEEPAGENTS_CODE_DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS` /
    `DEEPAGENTS_CODE_DISABLED_PROJECT_MCP_SERVERS` process env vars — never from
    a project's `.mcp.json` or any repo-committed file. There is no
    project-level `config.toml` discovery, so an attacker who commits a
    malicious `.mcp.json` plus an in-repo config cannot pre-approve their own
    servers; the approval must live in the user's home config. This mirrors
    Claude Code's "untrusted folder → only non-checked-in settings" rule.

    Source resolution differs by list, matching each one's security direction:

    - managed config (highest precedence): an administrator `[mcp]` table
        outranks every source below. An explicit managed
        `enabled_project_server_approvals` list replaces both the env-enabled
        names and the user's remembered approvals. Managed denies union with
        the rest.
    - `enabled` (permissive): the env var is an explicit process-wide name
        allowlist.
    - `approvals` (permissive): TOML approvals bind fixed remote URLs to one
        validated local Git repository (shared across its worktrees). Local commands
        and interpolated remote URLs bind to an exact worktree. All include a
        server-definition fingerprint and remain active alongside env-enabled names,
        so setting the process-wide escape hatch does not discard choices remembered
        by the interactive prompt. An explicit managed approvals list is the one
        exception: it replaces both.
        Legacy flat TOML
        `enabled_project_servers` entries are ignored because they cannot be safely
        scoped.
    - `disabled` (restrictive): the env var *unions* with the TOML list — denies
        accumulate and a lower-effort source can never silently empty a deny
        entry set in the other, which would be a fail-open. There is
        deliberately no way to *remove* a configured deny via env.

    Rejection wins: a name appearing in approval and disabled data is reported
    only in `disabled`.

    Args:
        config_path: Config file to read. Defaults to `DEFAULT_CONFIG_PATH`;
            callers should not point this at a project path — doing so would
            defeat the boundary above. Passing a path also excludes managed
            policy from this read, so production callers must pass `None`.

    Returns:
        The resolved `McpServerTrustLists`. A missing file yields empty lists
            (the normal "unset" case). `read_error` is set (so callers can fail
            closed instead of treating a broken config as "nothing denied") when
            the file exists but cannot be read/parsed, when `[mcp]` is not a
            table, or when `disabled_project_servers` is a wrong type that cannot
            be read as a deny list; env-sourced names still apply in that case.
            The same three conditions in the managed file set it too, because a
            deny list an administrator set must never fail open.

    Raises:
        RuntimeError: If required MCP options are missing from the manifest.
    """
    # [해설][흐름] 1) 설정 소스 로드 후 managed/user 각 계층의 `[mcp]` 루트를 순위값으로 변환.
    is_default = config_path is None
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    from deepagents_code.config_manifest import get_option
    from deepagents_code.configuration.resolver import (
        ENVIRONMENT_RANK,
        MANAGED_RANK,
        USER_RANK,
        resolve_ranked,
    )
    from deepagents_code.configuration.service import get_config_sources
    from deepagents_code.configuration.types import Invalid

    # `None` on the default path: that is what includes managed policy.
    sources = get_config_sources(user_path=None if is_default else config_path)
    managed_path = sources.managed.status.path or config_path
    managed_section = _ranked_mcp_section(
        sources.managed.data,
        rank=MANAGED_RANK,
        status=sources.managed.status,
        path=managed_path,
        managed=True,
    )
    user_section = _ranked_mcp_section(
        sources.user.data,
        rank=USER_RANK,
        status=sources.user.status,
        path=config_path,
        managed=False,
    )

    # [해설][흐름] 2) 매니페스트에서 세 옵션(승인/거부/레거시)의 병합 전략을 가져온다(없으면 RuntimeError).
    approvals_option = get_option("mcp.enabled_project_server_approvals")
    disabled_option = get_option("mcp.disabled_project_servers")
    legacy_option = get_option("mcp.enabled_project_servers")
    if approvals_option is None or disabled_option is None or legacy_option is None:
        msg = "MCP trust options are missing from the config manifest"
        raise RuntimeError(msg)

    # [해설][흐름] 3) 승인 해석: managed 승인, DANGEROUSLY_ env 이름, user 승인을 병합 전략대로 resolve.
    managed_approvals, managed_malformed = _ranked_mcp_approvals(
        managed_section,
        path=managed_path,
        managed=True,
    )
    user_approvals, user_malformed = _ranked_mcp_approvals(
        user_section,
        path=config_path,
        managed=False,
    )
    env_approvals = _ranked_mcp_env_names(
        _env_vars.DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS,
        rank=ENVIRONMENT_RANK,
        composite=True,
    )
    resolved_approvals = resolve_ranked(
        (managed_approvals, env_approvals, user_approvals),
        strategy=approvals_option.merge_strategy.value,
    )

    # [해설][흐름] 4) 거부 해석: 세 출처를 fail_closed로 변환 후 resolve(합집합 전략, 추정: 매니페스트 merge_strategy).
    managed_disabled = _ranked_mcp_names(
        managed_section,
        key="disabled_project_servers",
        path=managed_path,
        managed=True,
        fail_closed=True,
    )
    user_disabled = _ranked_mcp_names(
        user_section,
        key="disabled_project_servers",
        path=config_path,
        managed=False,
        fail_closed=True,
    )
    env_disabled = _ranked_mcp_env_names(
        _env_vars.DISABLED_PROJECT_MCP_SERVERS,
        rank=ENVIRONMENT_RANK,
        composite=False,
    )
    resolved_disabled = resolve_ranked(
        (managed_disabled, env_disabled, user_disabled),
        strategy=disabled_option.merge_strategy.value,
    )

    # [해설][흐름] 5) 더 이상 지원하지 않는 평면 `enabled_project_servers` 목록은 무시하고 진단용으로만 수집.
    legacy_provider = _ranked_mcp_names(
        user_section,
        key="enabled_project_servers",
        path=config_path,
        managed=False,
        fail_closed=False,
    )
    resolved_legacy = resolve_ranked(
        (legacy_provider,),
        strategy=legacy_option.merge_strategy.value,
    )
    legacy_ignored = resolved_legacy.value if resolved_legacy is not None else []
    if legacy_ignored:
        logger.warning(
            "[mcp].enabled_project_servers in %s is ignored; run "
            "the project MCP approval prompt again to save "
            "project-scoped approvals",
            config_path,
        )

    # [해설][흐름] 6) 읽기 오류 누적: user/managed 파일 손상·잘못된 타입 → `read_error`(호출자는 fail-closed).
    # Accumulated, not overwritten: both layers can fail, and both errors must
    # reach the user.
    read_errors: list[str] = []
    if not sources.user.status.usable:
        # The file exists but is unreadable/unparseable. Record it so callers
        # fail closed rather than silently proceeding with an empty deny list.
        read_errors.append(
            f"Could not read MCP trust lists from {config_path}: "
            f"{sources.user.status.detail or sources.user.status.health.value}"
        )
        logger.warning(
            "Could not read %s for MCP server trust lists; treating project "
            "configs as untrusted",
            config_path,
        )
    elif isinstance(user_section.result, Invalid):
        read_errors.append(user_section.result.reason)
    elif isinstance(user_disabled.result, Invalid):
        read_errors.append(user_disabled.result.reason)

    managed_status = sources.managed.status
    if not managed_status.usable:
        read_errors.append(
            f"Could not enforce MCP trust lists from {managed_status.path}: "
            f"{managed_status.detail or managed_status.health.value}"
        )
        # A managed file that cannot be read may have carried an explicit
        # approvals list, whose whole purpose is to drop the env bypass. Leaving
        # this false let `DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS` grants return,
        # so corrupting the file converted a managed suppression into a permit.
        # A deny list that cannot be read denies everything.
    elif isinstance(managed_section.result, Invalid):
        read_errors.append(managed_section.result.reason)
    else:
        if isinstance(managed_approvals.result, Invalid):
            read_errors.append(managed_approvals.result.reason)
        if isinstance(managed_disabled.result, Invalid):
            read_errors.append(managed_disabled.result.reason)
    # [해설][흐름] 7) 이름이 바뀐 옛 env(`LEGACY_ENABLED_PROJECT_MCP_SERVERS`) 설정 여부 진단.
    # The old name was renamed to the `DANGEROUSLY_`-prefixed var and is no
    # longer read; flag it set-but-ignored so callers can explain the rename
    # instead of the names silently ceasing to pre-approve.
    from deepagents_code.config import active_environment

    legacy_env_ignored = (
        _env_vars.LEGACY_ENABLED_PROJECT_MCP_SERVERS in active_environment()
    )
    if legacy_env_ignored:
        logger.warning(
            "%s is no longer used; it was renamed to %s",
            _env_vars.LEGACY_ENABLED_PROJECT_MCP_SERVERS,
            _env_vars.DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS,
        )

    # [해설][흐름] 8) 최종 조립: managed가 불량이면 env 이름 승인 제거, 어떤 읽기 오류든 있으면 저장 승인 전부 무효.
    # Process-wide env names and scoped TOML approvals are independent grants.
    # Keep both active so the escape hatch cannot make the interactive prompt's
    # successfully persisted choices ineffective on the next launch.
    approval_value = (
        cast("dict[str, Any]", resolved_approvals.value)
        if resolved_approvals is not None
        else {}
    )
    resolved_enabled = cast("list[str]", approval_value.get("enabled", []))
    resolved_approval_rows = cast(
        "list[McpProjectServerApproval]", approval_value.get("approvals", [])
    )
    # `_ranked_mcp_approvals` propagates a shadowing `[mcp]` root into its own
    # result, so this covers both a wrong-typed approvals list and a malformed
    # `[mcp]` root. Requiring the section itself to be `Found` would let a
    # corrupt managed file convert a suppression into a permit.
    managed_approval_invalid = isinstance(managed_approvals.result, Invalid)
    enabled = frozenset(
        ()
        if not managed_status.usable or managed_approval_invalid
        else resolved_enabled
    )
    read_error = "; ".join(read_errors) if read_errors else None
    approvals = frozenset(() if read_errors else resolved_approval_rows)
    disabled = frozenset(
        cast("list[str]", resolved_disabled.value)
        if resolved_disabled is not None
        else ()
    )
    # Corner: when `read_error` is set because `config.toml` was unreadable,
    # the user-file deny list is lost, so a name that is both TOML-`disabled` and
    # exported in `DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS` would survive here —
    # "reject wins" does not hold in that one corner. It requires a
    # self-contradicting config plus the explicit `DANGEROUSLY_` opt-in, and the
    # read error is surfaced to the user, so it stays an accepted footgun rather
    # than a silent fail-open.
    # Reject precedence is enforced by `McpServerTrustLists.__post_init__`, so no
    # subtraction here.
    return McpServerTrustLists(
        enabled=enabled,
        disabled=disabled,
        approvals=approvals,
        read_error=read_error,
        legacy_ignored=frozenset(legacy_ignored),
        legacy_env_ignored=legacy_env_ignored,
        malformed_approvals=user_malformed + managed_malformed,
    )


# [해설] 대화형 MCP 승인 프롬프트의 "항상 허용" 결과를 사용자 config.toml `[mcp].enabled_project_server_approvals`에 저장.
# [해설] 호출자: `main.py`(프로젝트 MCP 승인 흐름). 서버 정의 지문과 함께 기록하므로 정의가 바뀌면 다시 묻는다.
# [해설] 부수 효과: 레거시 평면 목록에서 해당 이름을 제거(마이그레이션), 캐시 무효화.
def add_enabled_project_mcp_servers(
    names: Iterable[str],
    config_path: Path | None = None,
    *,
    project_root: str | Path | None = None,
    server_configs: Mapping[str, JsonValue] | None = None,
) -> bool:
    """Persist project-scoped MCP server approvals.

    Backs the interactive approval prompt's "always allow" choice: the given
    names are added to the user-level `config.toml` allowlist with each server
    definition's fingerprint. Fixed remote URLs use the local Git repository
    identity and are shared by its linked worktrees. Local commands and
    interpolated remote URLs use the exact worktree root. A different clone or
    changed definition asks again.

    Defaults to the user-level config (`DEFAULT_CONFIG_PATH`), the sole source
    `load_mcp_server_trust_lists` reads the allowlist from — so writing to the
    user's home config is what preserves the read-side trust boundary (a
    committed `.mcp.json` can never self-approve). Any name being persisted is
    also pruned from the deprecated flat `[mcp].enabled_project_servers` key
    (the key is removed once empty), migrating callers off the ignored legacy
    list. The write is atomic (`tempfile.mkstemp` + `Path.replace`) and holds
    `_config_write_lock` across the whole read-modify-write, matching
    `suppress_warning`.

    Args:
        names: Server names to add to the allowlist. Blank/whitespace-only
            names are ignored; a call with no usable names is a no-op success.
        config_path: Config file to write. Defaults to `DEFAULT_CONFIG_PATH`
            (`~/.deepagents/config.toml`). Callers should not point this at a
            project path: the loader only ever reads the user-level config, so
            an allowlist written elsewhere is never honored.
        project_root: Project root whose MCP server definitions were approved.
        server_configs: Current server definitions keyed by server name.

    Returns:
        `True` if the save succeeded (or there was nothing to add), `False` on
            I/O, parse failure, an unknown server name, or missing
            project/server context.
    """
    # [해설][흐름] 1) 이름 정리 → 프로젝트 루트/서버 정의 필수 확인 → 각 이름을 승인 객체로 생성(모르는 이름이면 실패).
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    clean_names = [name.strip() for name in names if name and name.strip()]
    if not clean_names:
        return True

    if project_root is None or server_configs is None:
        logger.error(
            "Cannot save enabled project MCP servers without project root and "
            "server definitions"
        )
        return False

    approvals_to_add: list[McpProjectServerApproval] = []
    for name in clean_names:
        if name not in server_configs:
            logger.error("Cannot save unknown project MCP server %r", name)
            return False
        approval = McpProjectServerApproval.create(
            project_root=project_root,
            name=name,
            server=server_configs[name],
        )
        if approval is None:
            logger.error("Could not normalize project root for MCP server %r", name)
            return False
        approvals_to_add.append(approval)

    # [해설][흐름] 2) 락 안에서 읽기 → 기존 승인과 합집합·정렬(결정적 출력) → 레거시 목록 정리 → 원자적 기록.
    try:
        # Hold the shared lock across read-through-replace: the atomic rename
        # alone only prevents torn writes, not the lost update where a
        # concurrent config.toml writer reads the same snapshot and its
        # `replace()` lands last, silently dropping this approval. See the
        # `_config_write_lock` contract; `suppress_warning` guards the same way.
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)

            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            mcp_section = data.get("mcp")
            if not isinstance(mcp_section, dict):
                mcp_section = {}
            existing, _ = _toml_project_server_approvals(
                mcp_section.get("enabled_project_server_approvals"),
                config_path=config_path,
            )
            merged = set(existing) | set(approvals_to_add)
            mcp_section["enabled_project_server_approvals"] = [
                approval.as_toml() for approval in sorted(merged)
            ]
            legacy, legacy_malformed = _toml_str_list(
                mcp_section.get("enabled_project_servers"),
                key="enabled_project_servers",
                config_path=config_path,
            )
            if legacy and not legacy_malformed:
                migrated = set(clean_names)
                remaining = [name for name in legacy if name not in migrated]
                if remaining:
                    mcp_section["enabled_project_servers"] = remaining
                else:
                    mcp_section.pop("enabled_project_servers", None)
            data["mcp"] = mcp_section

            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        # Matches `suppress_warning`: `TypeError` covers `tomli_w.dump`
        # rejecting a non-serializable payload; `ValueError` covers things like
        # `os.fdopen` on a closed fd. Folding them in keeps the `bool` contract
        # intact so the caller degrades to a "could not remember" warning
        # instead of crashing with a raw traceback.
        logger.exception(
            "Could not save enabled project MCP servers to %s", config_path
        )
        return False
    _invalidate_config_caches(config_path)
    return True


# [해설] ── 이하 `/threads` 선택기 설정(`[threads]`) — `analysis/09-tui-app-commands-acp.md` 참고 ──
# [해설] 스레드 선택기 컬럼 기본 표시 여부.
THREAD_COLUMN_DEFAULTS: dict[str, bool] = {
    "thread_id": False,
    "messages": True,
    "created_at": True,
    "updated_at": True,
    "git_branch": False,
    "cwd": False,
    "initial_prompt": True,
    "agent_name": False,
}
"""Default visibility for thread selector columns."""


# [해설] `[threads]` 설정을 한 번의 파싱으로 모은 묶음(컬럼/상대시간/정렬/범위).
class ThreadConfig(NamedTuple):
    """Coalesced thread-selector configuration read from a single TOML parse."""

    columns: dict[str, bool]
    """Column visibility settings."""

    relative_time: bool
    """Whether to display timestamps as relative time."""

    sort_order: str
    """`'updated_at'` or `'created_at'`."""

    scope: str
    """`'cwd'` (current working directory) or `'all'` (all directories)."""


# [해설] `load_thread_config` 결과 캐시. 무효화: `invalidate_thread_config_cache`(쓰기·`/reload` 시).
_thread_config_cache: ThreadConfig | None = None


# [해설] `/threads` 모달 설정 로드(기본 경로면 캐시). 시작 시 prewarm 워커가 호출해 모달 오픈 시 디스크 I/O를 없앤다.
# [해설] 허용 값만 채택: sort_order ∈ {updated_at, created_at}, scope ∈ {cwd, all}.
def load_thread_config(config_path: Path | None = None) -> ThreadConfig:
    """Load all thread-selector settings from one config file read.

    Returns a cached result when reading the default config path. The
    prewarm worker calls this at startup so subsequent opens of the
    `/threads` modal avoid disk I/O entirely.

    Args:
        config_path: Path to config file.

    Returns:
        Coalesced thread configuration.
    """
    global _thread_config_cache  # noqa: PLW0603  # Module-level cache requires global statement

    use_default = config_path is None
    if use_default and _thread_config_cache is not None:
        return _thread_config_cache

    columns = dict(THREAD_COLUMN_DEFAULTS)
    relative_time = True
    sort_order = "updated_at"
    scope = "cwd"

    # [해설][흐름] 1) 유효 설정 로드 후 필드별 타입·값 검증하며 기본값 위에 덮어쓴다.
    try:
        data, _ = _load_effective_config_data(config_path)
        threads_section = data.get("threads", {})
        if not isinstance(threads_section, dict):
            threads_section = {}

        # columns
        raw_columns = threads_section.get("columns", {})
        if isinstance(raw_columns, dict):
            for key in columns:
                if key in raw_columns and isinstance(raw_columns[key], bool):
                    columns[key] = raw_columns[key]

        # relative_time
        rt_value = threads_section.get("relative_time")
        if isinstance(rt_value, bool):
            relative_time = rt_value

        # sort_order
        so_value = threads_section.get("sort_order")
        if so_value in {"updated_at", "created_at"}:
            sort_order = so_value

        # scope
        scope_value = threads_section.get("scope")
        if scope_value in {"cwd", "all"}:
            scope = scope_value
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning("Could not read thread config; using defaults", exc_info=True)
        # Do not cache on error — allow retry on next call in case the
        # file is fixed or permissions are restored.
        return ThreadConfig(columns, relative_time, sort_order, scope)

    # [해설][흐름] 2) 사용자 계층이 정상일 때만 캐시(열화된 managed-only 결과를 프로세스 수명 동안 캐시하지 않기 위해).
    result = ThreadConfig(columns, relative_time, sort_order, scope)
    # The `except` above no longer fires for a bad user file on the default
    # path: `_load_effective_config_data` logs it and returns managed-only data
    # instead of raising, so that guard stopped protecting the cache. Without
    # this check the degraded result was cached for the process lifetime and
    # survived the user repairing `config.toml`, because nothing on the read
    # path calls `invalidate_thread_config_cache`.
    if use_default and _user_config_layer_usable():
        _thread_config_cache = result
    return result


# [해설] thread 설정 캐시 비우기. `clear_caches`/`_invalidate_config_caches`/thread 저장 함수들이 호출.
def invalidate_thread_config_cache() -> None:
    """Clear the cached `ThreadConfig` so the next load re-reads disk."""
    global _thread_config_cache  # noqa: PLW0603  # Module-level cache requires global statement
    _thread_config_cache = None


# [해설] 컬럼 표시 설정만 로드(캐시 없음).
def load_thread_columns(config_path: Path | None = None) -> dict[str, bool]:
    """Load thread column visibility from config file.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`. Passing a path also
            excludes managed policy from this read, so production callers
            must pass `None`.

    Returns:
        Dict mapping column names to visibility booleans.
    """
    result = dict(THREAD_COLUMN_DEFAULTS)
    try:
        data, _ = _load_effective_config_data(config_path)
        threads = data.get("threads", {})
        columns = threads.get("columns", {}) if isinstance(threads, dict) else {}
        if isinstance(columns, dict):
            for key in result:
                if key in columns and isinstance(columns[key], bool):
                    result[key] = columns[key]
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read thread column config", exc_info=True)
    return result


# [해설] `[threads].columns` 저장(락 + 원자적 쓰기 + 캐시 무효화).
def save_thread_columns(
    columns: dict[str, bool], config_path: Path | None = None
) -> bool:
    """Save thread column visibility to config file.

    Args:
        columns: Dict mapping column names to visibility booleans.
        config_path: Path to config file.

    Returns:
        True if save succeeded, False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)

            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            if "threads" not in data:
                data["threads"] = {}
            data["threads"]["columns"] = columns

            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save thread column preferences")
        return False
    invalidate_thread_config_cache()
    _invalidate_config_caches(config_path)
    return True


# [해설] `[threads].relative_time` 로드(기본 True).
def load_thread_relative_time(config_path: Path | None = None) -> bool:
    """Load the relative-time display preference for thread timestamps.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`. Passing a path also
            excludes managed policy from this read, so production callers
            must pass `None`.

    Returns:
        True if timestamps should display as relative time.
    """
    try:
        data, _ = _load_effective_config_data(config_path)
        threads = data.get("threads", {})
        value = threads.get("relative_time") if isinstance(threads, dict) else None
        if isinstance(value, bool):
            return value
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read thread relative_time config", exc_info=True)
    return True


# [해설] `[threads].relative_time` 저장.
def save_thread_relative_time(enabled: bool, config_path: Path | None = None) -> bool:
    """Save the relative-time display preference for thread timestamps.

    Args:
        enabled: Whether to display relative timestamps.
        config_path: Path to config file.

    Returns:
        True if save succeeded, False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}
            if "threads" not in data:
                data["threads"] = {}
            data["threads"]["relative_time"] = enabled
            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save thread relative_time preference")
        return False
    invalidate_thread_config_cache()
    _invalidate_config_caches(config_path)
    return True


# [해설] `[threads].sort_order` 로드(기본 `updated_at`).
def load_thread_sort_order(config_path: Path | None = None) -> str:
    """Load the sort order preference for the thread selector.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`. Passing a path also
            excludes managed policy from this read, so production callers
            must pass `None`.

    Returns:
        `"updated_at"` or `"created_at"`.
    """
    try:
        data, _ = _load_effective_config_data(config_path)
        threads = data.get("threads", {})
        value = threads.get("sort_order") if isinstance(threads, dict) else None
        if value in {"updated_at", "created_at"}:
            return value
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read thread sort_order config", exc_info=True)
    return "updated_at"


# [해설] ── 시작 승인 모드(`[startup]`) — HITL 승인 정책과 연결, `analysis/04-approval-hitl-security.md` 참고 ──
# [해설] manual=사람 승인, auto=분류기 모델 검토, yolo=검토 없이 실행.
STARTUP_MODE_MANUAL = "manual"
"""Startup approval mode that keeps human-in-the-loop approvals enabled."""

STARTUP_MODE_AUTO = "auto"
"""Startup approval mode that uses classifier-backed action review."""

STARTUP_MODE_YOLO = "yolo"
"""Startup approval mode that executes gated actions without review."""

# [해설] `[startup].mode`에 허용되는 값과, `[startup].recent`로 암묵 복원 가능한 값(yolo 제외 — 위험 모드는 명시 설정만).
VALID_STARTUP_MODES = frozenset(
    {STARTUP_MODE_MANUAL, STARTUP_MODE_AUTO, STARTUP_MODE_YOLO}
)
"""Accepted values for the `[startup].mode` config option."""

RECENT_STARTUP_MODES = frozenset({STARTUP_MODE_MANUAL, STARTUP_MODE_AUTO})
"""Modes the app may restore implicitly from `[startup].recent`."""

# [해설] 저장된 recent=auto를 안내 notice 버전 문제로 복원하지 못했을 때 TUI에 한 번 보여줄 문구와 그 대기 슬롯(프로세스 전역).
_RECENT_AUTO_NOT_RESTORED_NOTICE = (
    "Auto was not restored for this session because its guidance notice is "
    "missing or out of date. Press Shift+Tab to review it and re-enable Auto."
)
"""User-facing copy for a remembered Auto that the notice gate declined."""

_recent_auto_not_restored_notice: str | None = None
"""One-shot TUI notice populated when the notice gate declines a stored Auto."""


# [해설] 대기 중인 "Auto 미복원" 안내를 꺼내고 비운다(one-shot). TUI가 시작 후 호출(추정).
def consume_recent_auto_not_restored_notice() -> str | None:
    """Return and clear the pending not-restored notice, if any."""
    global _recent_auto_not_restored_notice  # noqa: PLW0603

    notice = _recent_auto_not_restored_notice
    _recent_auto_not_restored_notice = None
    return notice


# [해설] 어떤 이유로든 모드를 결정 못 하면 manual로 fail-closed.
DEFAULT_STARTUP_MODE = STARTUP_MODE_MANUAL
"""Fail-closed startup mode.

Returned when no mode resolves, when `[startup]` is absent or is not a table,
when the config is unreadable, and when a stored recent Auto is not restorable.
"""


# [해설] recent 모드 복원 가능 여부: manual은 항상, auto는 현재 버전의 Auto 안내를 본 경우만(`approval_mode.has_auto_mode_notice`).
def is_recent_startup_mode_restorable(mode: str) -> bool:
    """Return whether an app-managed recent mode may be restored.

    Auto restoration requires the current versioned education notice. Manual
    remains safe to restore without one. No caller prompts: `False` means the
    caller falls back to `manual`.

    Args:
        mode: Candidate value from `[startup].recent`.

    Returns:
        Whether startup may restore the mode.
    """
    if mode not in RECENT_STARTUP_MODES:
        return False
    if mode != STARTUP_MODE_AUTO:
        return True

    # Function-local: `approval_mode` imports this module, so a module-level
    # import would close the cycle.
    from deepagents_code.approval_mode import has_auto_mode_notice

    return has_auto_mode_notice()


# [해설] 시작 승인 모드 결정: 명시 `[startup].mode` > `[startup].recent`(manual/auto만, auto는 notice 조건) > manual.
# [해설] 호출자: 시작 시 승인 모드 초기화(`config_manifest`의 introspection이 이 규칙을 미러링).
# [해설][주의] 잘못된 명시 mode는 recent를 보지 않고 곧장 manual — 오타로 더 느슨한 모드가 복원되지 않게.
def load_startup_mode(config_path: Path | None = None) -> str:
    """Load the startup approval mode from config.toml.

    An explicit `[startup].mode` outranks the app-managed `[startup].recent`
    value. An invalid explicit mode fails closed to `manual` and never consults
    `recent`. `recent` restores `manual`, or classifier-backed `auto` once the
    current notice has been shown. Unrestricted `yolo` must stay explicitly
    configured.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`. Passing a path also
            excludes managed policy from this read, so production callers
            must pass `None`.

    Returns:
        `"manual"`, `"auto"`, or `"yolo"`; falls back to `"manual"` when
        unset, unreadable, or invalid.
    """
    # [해설][흐름] 1) `[startup]` 테이블 확인 → 2) 명시 mode 검증 → 3) recent 검증·복원 조건 → 4) 실패 시 manual.
    try:
        data, _ = _load_effective_config_data(config_path)
        startup = data.get("startup")
        if not isinstance(startup, dict):
            return DEFAULT_STARTUP_MODE
        # TOML values carry any type. The isinstance guards here and on
        # `recent` below keep an array or table out of the frozenset membership
        # tests, which would raise `TypeError: unhashable type` — uncaught by
        # the handler below — and crash startup.
        value = startup.get("mode")
        if isinstance(value, str) and value in VALID_STARTUP_MODES:
            return value
        if value is not None:
            logger.warning(
                "Ignoring [startup].mode=%r (expected 'manual', 'auto', or 'yolo')",
                value,
            )
            return DEFAULT_STARTUP_MODE
        recent = startup.get("recent")
        # Re-test membership here so only an invalid value takes the warning
        # below; a valid-but-notice-blocked Auto is a normal fail-closed, and
        # gets its own diagnostic instead of being reported as a config error.
        if isinstance(recent, str) and recent in RECENT_STARTUP_MODES:
            if is_recent_startup_mode_restorable(recent):
                return recent
            # The only exit that discards a *valid* user-earned preference.
            # Without this it is indistinguishable from the feature not working:
            # a notice-version bump silently returns every Auto user to Manual.
            global _recent_auto_not_restored_notice  # noqa: PLW0603

            logger.warning(
                "Not restoring [startup].recent=%r: the Auto notice is missing "
                "or out of date; starting in %s",
                recent,
                DEFAULT_STARTUP_MODE,
            )
            _recent_auto_not_restored_notice = _RECENT_AUTO_NOT_RESTORED_NOTICE
            return DEFAULT_STARTUP_MODE
        if recent is not None:
            logger.warning(
                "Ignoring [startup].recent=%r (expected 'manual' or 'auto')",
                recent,
            )
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read startup mode config", exc_info=True)
    return DEFAULT_STARTUP_MODE


# [해설] 최근 선택한 안전 모드(manual/auto)를 `[startup].recent`에 저장. yolo는 절대 recent로 저장하지 않는다(ValueError).
def save_recent_startup_mode(mode: str, config_path: Path | None = None) -> bool:
    """Save the most recently selected safe startup approval mode.

    Args:
        mode: `"manual"` or `"auto"`.
        config_path: Path to config file.

    Returns:
        `True` when the preference was saved, otherwise `False`.

    Raises:
        ValueError: If `mode` is not `"manual"` or `"auto"`. `yolo` must stay
            explicitly configured, so it is never stored as a recent mode.
    """
    if mode not in RECENT_STARTUP_MODES:
        msg = f"Invalid recent startup mode: {mode!r}"
        raise ValueError(msg)
    return _save_toml_field("startup", "recent", mode, config_path)


# [해설] `[threads].sort_order` 저장(값 검증 후 원자적 쓰기).
def save_thread_sort_order(sort_order: str, config_path: Path | None = None) -> bool:
    """Save the sort order preference for the thread selector.

    Args:
        sort_order: `"updated_at"` or `"created_at"`.
        config_path: Path to config file.

    Returns:
        True if save succeeded, False on I/O error.

    Raises:
        ValueError: If `sort_order` is not a recognised value.
    """
    if sort_order not in {"updated_at", "created_at"}:
        msg = (
            f"Invalid sort_order {sort_order!r}; expected 'updated_at' or 'created_at'"
        )
        raise ValueError(msg)
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}
            if "threads" not in data:
                data["threads"] = {}
            data["threads"]["sort_order"] = sort_order
            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            # [해설][주의] 다른 writer들과 달리 `BaseException`이 아닌 `Exception`만 잡으므로 Ctrl+C 같은 인터럽트 시 임시 파일이 남을 수 있다.
            except Exception:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save thread sort_order preference")
        return False
    invalidate_thread_config_cache()
    _invalidate_config_caches(config_path)
    return True


# [해설] `[threads].scope`(cwd/all) 저장.
def save_thread_scope(scope: str, config_path: Path | None = None) -> bool:
    """Save the directory-scope preference for the thread selector.

    Args:
        scope: `"cwd"` (current working directory) or `"all"` (all directories).
        config_path: Path to config file.

    Returns:
        True if save succeeded, False on I/O error.

    Raises:
        ValueError: If `scope` is not a recognised value.
    """
    if scope not in {"cwd", "all"}:
        msg = f"Invalid scope {scope!r}; expected 'cwd' or 'all'"
        raise ValueError(msg)
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        with _config_write_lock:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if config_path.exists():
                with config_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}
            if "threads" not in data:
                data["threads"] = {}
            data["threads"]["scope"] = scope
            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                # Clean up temp file on any failure, including interrupts.
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        # `TypeError`/`ValueError` cover `tomli_w.dump` rejecting a payload
        # from a pre-existing config that does not round-trip; folding them in
        # keeps the `bool` contract intact for `_persist_scope`'s failure toast.
        logger.exception("Could not save thread scope preference")
        return False
    invalidate_thread_config_cache()
    _invalidate_config_caches(config_path)
    return True


# [해설] `/model` 전환 시 `[models].recent` 저장 — default를 덮어쓰지 않기 위해 분리. 호출자: `app.py`(모델 전환 성공 후).
# [해설] allowlist 위반이면 `ModelNotAllowedError`(`_save_model_field`).
def save_recent_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Update the recently used model in config file.

    Writes to `[models].recent` instead of `[models].default`, so that `/model`
    switches do not overwrite the user's intentional default.

    Args:
        model_spec: The model to save in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.

    Raises:
        ModelNotAllowedError: If `model_spec` is outside the effective
            `models.allowed` policy.

    Note:
        This function does not preserve comments in the config file.
    """  # noqa: DOC502 - propagates from `_save_model_field`
    return _save_model_field("recent", model_spec, config_path)


# [해설] MRU 캐시 파일 경로: `<state_dir>/recent_models.json`(기본 `~/.deepagents/.state`). config.toml과 별도 파일.
def _recent_models_path(state_dir: Path | None = None) -> Path:
    """Resolve the JSON file path for the recent-models MRU cache.

    Args:
        state_dir: Override for the state directory (test hook).

    Returns:
        Absolute path to `recent_models.json` under the chosen state dir.
    """
    return (state_dir or DEFAULT_STATE_DIR) / RECENT_MODELS_FILENAME


# [해설] `/model` 선택기 "Recent" 섹션용 MRU 목록 로드. 파일 문제는 빈 목록, allowlist 밖 항목은 제외, 최대 `RECENT_MODELS_LIMIT`개.
def load_recent_models(state_dir: Path | None = None) -> list[str]:
    """Read the most-recent-first list of `provider:model` specs.

    Missing or malformed files yield an empty list rather than raising; the
    recent section is a non-essential UI affordance and must not block the
    selector from rendering.

    Args:
        state_dir: Override for the state directory (test hook).

    Returns:
        Ordered list of recent `provider:model` specs, most recent first.
            Capped at `RECENT_MODELS_LIMIT` and de-duplicated. Entries outside
            `models.allowed` are dropped, so the result can be shorter than the
            file -- or empty despite a populated cache.
    """
    path = _recent_models_path(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read recent models cache at %s", path, exc_info=True)
        return []
    raw = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    config = ModelConfig.load()
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        if (
            not isinstance(entry, str)
            or ":" not in entry
            or entry in seen
            or not config.is_model_allowed(entry)
        ):
            continue
        seen.add(entry)
        out.append(entry)
        if len(out) >= RECENT_MODELS_LIMIT:
            break
    return out


# [해설] 방금 선택한 spec을 MRU 맨 앞으로(중복 제거, 상한 적용) 원자적으로 기록. 호출자: `app.py`(모델 전환 시 `save_recent_model`과 함께).
# [해설] 정책 밖 spec은 조용히 False(선택기의 정책 안내가 이미 설명).
def touch_recent_model(model_spec: str, state_dir: Path | None = None) -> bool:
    """Promote `model_spec` to the front of the recent-models MRU list.

    Existing entries for the same spec are moved (not duplicated); the list
    is capped at `RECENT_MODELS_LIMIT`. Best-effort: returns `False` on I/O
    error so callers can degrade silently — recents are a nice-to-have, not
    a correctness requirement.

    A spec outside `models.allowed` also returns `False`. That refusal is
    deliberately silent here: the MRU is a derived cache, and the visible
    consequence (the spec not appearing under Recent) is already explained by
    the selector's policy empty state.

    Args:
        model_spec: The `provider:model` string just selected.
        state_dir: Override for the state directory (test hook).

    Returns:
        `True` on success, `False` on I/O error, an invalid spec, or a spec
            outside `models.allowed`.
    """
    if (
        not model_spec
        or ":" not in model_spec
        or not ModelConfig.load().is_model_allowed(model_spec)
    ):
        return False
    existing = load_recent_models(state_dir)
    deduped = [entry for entry in existing if entry != model_spec]
    new_list = [model_spec, *deduped][:RECENT_MODELS_LIMIT]
    path = _recent_models_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"models": new_list}, f)
            Path(tmp_path).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except OSError:
        logger.warning(
            "Could not update recent models cache at %s", path, exc_info=True
        )
        return False
    return True


# [해설] ── 이하 에이전트 선택 기억(`[agents]`) — bare 실행(`-a` 없음) 시 어떤 에이전트로 시작할지 ──
# [해설] `[agents].recent` 저장: 마지막 사용 에이전트 이름.
def save_recent_agent(agent_name: str, config_path: Path | None = None) -> bool:
    """Update the recently used agent in config file.

    Writes to `[agents].recent` so a later bare `deepagents` launch (no
    `-a`) can bring the user back to their last agent instead of the
    default.

    Args:
        agent_name: The agent directory name (e.g., `'coder'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.
    """
    return _save_toml_field("agents", "recent", agent_name, config_path)


# [해설] `[agents].recent` 로드.
def load_recent_agent(config_path: Path | None = None) -> str | None:
    """Read `[agents].recent` from the config file.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        The saved agent name, or `None` if the file or key is missing or
        the file is unreadable.
    """
    return _load_agents_field("recent", config_path)


# [해설] `[agents].default` 저장(`/agents` 선택기의 Ctrl+S). 우선순위: default > recent > `DEFAULT_AGENT_NAME`.
def save_default_agent(agent_name: str, config_path: Path | None = None) -> bool:
    """Update the default agent in config file.

    Writes to `[agents].default`. This is the user's intentional sticky
    default — set via `Ctrl+S` in the `/agents` picker — and takes
    precedence over `[agents].recent` on bare-launch resolution.

    Args:
        agent_name: The agent directory name (e.g., `'coder'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.
    """
    return _save_toml_field("agents", "default", agent_name, config_path)


# [해설] `[agents].default` 삭제 → recent/기본 에이전트로 폴백.
def clear_default_agent(config_path: Path | None = None) -> bool:
    """Remove the default agent from the config file.

    Deletes the `[agents].default` key so that future launches fall back
    to `[agents].recent` and then `DEFAULT_AGENT_NAME`.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        True if the key was removed (or was already absent), False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        with _config_write_lock:
            if not config_path.exists():
                return True

            with config_path.open("rb") as f:
                data = tomllib.load(f)

            agents_section = data.get("agents")
            if not isinstance(agents_section, dict) or "default" not in agents_section:
                return True

            del agents_section["default"]

            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        # See `_save_toml_field` for why `TypeError` / `ValueError` are
        # folded into the bool return contract.
        logger.exception("Could not clear default agent preference")
        return False
    else:
        _invalidate_config_caches(config_path)
        return True


# [해설] `[agents].default` 로드.
def load_default_agent(config_path: Path | None = None) -> str | None:
    """Read `[agents].default` from the config file.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        The saved agent name, or `None` if the file or key is missing or
        the file is unreadable.
    """
    return _load_agents_field("default", config_path)


# [해설] `[agents].<field>` 공통 로더(managed 병합, 공백 제거, 빈 값은 None).
def _load_agents_field(field: str, config_path: Path | None = None) -> str | None:
    """Read `[agents].<field>` from the config file.

    Args:
        field: Key under the `[agents]` table (e.g., `'recent'`, `'default'`).
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`. Passing a path also
            excludes managed policy from this read, so production callers
            must pass `None`.

    Returns:
        The trimmed string value, or `None` if the file, section, or key
        is missing or the file is unreadable.
    """
    try:
        data, _ = _load_effective_config_data(config_path)
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning("Could not read agents.%s from config", field, exc_info=True)
        return None
    agents_section = data.get("agents", {})
    value = agents_section.get(field) if isinstance(agents_section, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
