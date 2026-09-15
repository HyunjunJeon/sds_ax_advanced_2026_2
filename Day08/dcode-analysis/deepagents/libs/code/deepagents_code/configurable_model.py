"""Middleware for runtime model selection via LangGraph runtime context.

Allows switching the model per invocation by passing a `CLIContext` via
`context=` on `agent.astream()` / `agent.invoke()` without recompiling
the graph.
"""
# [해설] ── 모듈 개요 ──────────────────────────────────────────────
# [해설] 역할: 그래프를 다시 컴파일하지 않고 "호출 단위로" 모델을 교체하는 미들웨어
# [해설]   `ConfigurableModelMiddleware`를 제공한다. TUI의 `/model` hot-swap이 실제로 반영되는 곳이다.
# [해설] 실행 프로세스: LangGraph 서버 프로세스(에이전트 그래프 내부). `--acp`에서는 in-process.
# [해설]   클라이언트는 `CLIContext`(runtime context)에 `model`/`model_params`만 실어 보내고,
# [해설]   실제 모델 객체 생성(`config.create_model`)은 이 미들웨어가 서버 쪽에서 수행한다.
# [해설] 주요 진입점: `ConfigurableModelMiddleware.wrap_model_call` / `awrap_model_call`.
# [해설] 호출자(생성): `agent.py`(메인 에이전트·서브에이전트), `goal_rubric.py`(루브릭 평가 모델).
# [해설] 부수 역할: 호출 성공 후 `_model_spec`, `_model_params`, `_last_cache_*` 같은 비공개 state 채널을
# [해설]   `Command(update=...)`로 checkpoint에 기록 → `resume_state.py`(resume 시 모델 복원),
# [해설]   `app.py`/`cold_cache.py`(프롬프트 캐시 cold 경고)가 읽는다.
# [해설] 또한 프로바이더별 프롬프트 캐시 힌트(OpenAI `prompt_cache_key`, Fireworks 세션 affinity)를 주입한다.
# [해설][SDK] `langchain.agents.middleware.types.AgentMiddleware`의 `wrap_model_call` 훅,
# [해설]   `deepagents._models.model_matches_spec` / `get_model_identifier`(SDK `libs/deepagents/deepagents/_models.py`).
# [해설] 관련 분석: `analysis/03-config-models-credentials.md`. 공식 문서: `docs_official/sdk/models.md`
# [해설]   "Select a model at runtime"(request.override 권장 패턴), `docs_official/code/providers.md`.

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from deepagents._models import (  # noqa: PLC2701
    get_model_identifier,
    model_matches_spec,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    TracePolicy,
    omit_payload,
)
from langgraph.types import Command

from deepagents_code._cli_context import CLIContextSchema
from deepagents_code.cold_cache import cache_identity_params

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.language_models import BaseChatModel

    from deepagents_code.config import ModelResult


logger = logging.getLogger(__name__)


# [해설] `_apply_overrides*`의 결과 묶음. 하위 handler에 넘길 request와,
# [해설] 호출 성공 후 checkpoint에 남길 메타데이터(spec/params)를 함께 운반한다.
# [해설][설계] `model_params_known=False`이면 `_checkpoint_command`가 `_model_params` 채널을 아예 건드리지 않는다
# [해설]   (None으로 "지우기"와 "모름"을 구분하기 위한 플래그).
@dataclass(frozen=True)
class _ResolvedModelRequest:
    """Model request plus the checkpoint metadata it should persist."""

    request: ModelRequest
    """Request to pass to the downstream model handler."""

    model_spec: str | None
    """Resolved `provider:model` spec to persist for resume, when known."""

    model_params: dict[str, Any] | None = None
    """Invocation params to persist, or `None` to clear checkpointed params."""

    model_params_known: bool = False
    """Whether `model_params` is known and should be written to the checkpoint."""


# [해설] 이번 호출이 향한 엔드포인트(base_url) 식별자를 계산한다 → `_last_cache_endpoint` 채널에 기록.
# [해설] 캐시 cold 경고가 "엔드포인트가 바뀌었는가"를 판단하는 근거. 호출자: `wrap_model_call`(동기),
# [해설] `awrap_model_call`(`asyncio.to_thread`로 오프로드). 반환: `cold_cache.endpoint_cache_identity` 값.
# [해설][주의] 자격증명 저장소 파일을 매번 다시 읽는 블로킹 I/O가 있으므로 서버 이벤트 루프에서 직접 부르면 안 된다.
def _cache_endpoint_identity(
    model_spec: str | None, model_params: Mapping[str, Any] | None = None
) -> str:
    """Resolve the endpoint identity used by a model request for checkpointing.

    Performs blocking filesystem reads: `ModelConfig.load()` is process-cached,
    but `get_base_url` falls through to the credential store for any provider
    with no `config.toml` `base_url` and no base-URL env var *set* -- the
    default for `anthropic` and `openai`, whose env vars are registered in
    `PROVIDER_BASE_URL_ENV` but normally unset -- and that store re-reads its
    file every call. Callers on the blockbuster-guarded server loop must
    therefore invoke this via `asyncio.to_thread` (see
    `ConfigurableModelMiddleware.awrap_model_call`).

    A spec with no `provider:` prefix cannot name an endpoint, so it resolves to
    the same identity as the provider default. That is preferable to returning
    nothing: `_last_cache_endpoint` is written unconditionally alongside the
    spec and timestamp it describes, and a skipped write would leave the
    previous turn's endpoint paired with this turn's spec.

    The provider is normalized exactly as the reader normalizes it
    (`app._cold_cache_warning_for`). `get_kwargs`/`get_base_url` are
    exact-key lookups, so a spec the user spelled `Anthropic:claude-opus-5`
    would resolve `default` here while the reader resolved the real endpoint --
    a disagreement that never self-heals, because both sides keep recomputing
    their own answer, and every send would report `identity_changed`.

    Never raises. Both call sites run *after* `handler()` has returned, so the
    model call is already made and billed; letting a config-shaped surprise
    (a non-string `base_url` from a `class_path` provider that ignores it, say)
    propagate would discard a paid response over a diagnostic value. Failing to
    the provider default is the same degradation an unreadable checkpointed
    endpoint already gets.

    Returns:
        The normalized endpoint identity, or the provider-default identity when
        it cannot be resolved.
    """
    from deepagents_code.cold_cache import endpoint_cache_identity

    # [해설][흐름] 1) `provider:` 접두가 없는 spec은 엔드포인트를 특정할 수 없으므로 기본 식별자로 처리.
    if not model_spec or ":" not in model_spec:
        return endpoint_cache_identity(None)
    from deepagents_code.model_config import ModelConfig

    # [해설][흐름] 2) provider는 읽는 쪽(`app._cold_cache_warning_for`)과 똑같이 strip+lower로 정규화해야 양측 계산이 일치한다.
    raw_provider, _, model_name = model_spec.partition(":")
    provider = raw_provider.strip().lower()
    # [해설][흐름] 3) `ModelConfig.get_effective_kwargs`(config.toml params + 오버라이드)에 base_url이 있으면 그것을,
    # [해설]    kwargs가 dict가 아니면 `ModelConfig.get_base_url`(env·저장 엔드포인트까지 탐색)로 폴백.
    try:
        config = ModelConfig.load()
        kwargs = config.get_effective_kwargs(
            provider,
            model_name=model_name,
            overrides=model_params,
        )
        base_url = (
            kwargs.get("base_url")
            if isinstance(kwargs, dict)
            else config.get_base_url(provider)
        )
    # [해설][설계] 이미 과금된 응답을 진단값 때문에 버리지 않도록 모든 예외를 흡수하고 기본 식별자로 떨어진다.
    except Exception:
        logger.warning(
            "Could not resolve the cache endpoint for %r; recording the "
            "provider default, so an endpoint change may go undetected for "
            "this turn",
            provider,
            exc_info=True,
        )
        return endpoint_cache_identity(None)
    return endpoint_cache_identity(base_url if isinstance(base_url, str) else None)


# [해설] 모델 객체가 LangSmith 트레이싱용으로 보고하는 `ls_provider`를 읽는 공통 헬퍼.
# [해설] 아래 `_is_anthropic_model`/`_is_fireworks_model`/`_is_openai_model`의 기반이며, 프로바이더 패키지를
# [해설] import하지 않고도(isinstance 없이) 프로바이더를 판별하려는 목적이다.
def _get_ls_provider(model: object) -> str | None:
    """Return the LangSmith provider name reported by a chat model.

    Returns:
        The `ls_provider` string when the model reports one, otherwise `None`
            (including when `_get_ls_params` is missing, raises, or yields a
            non-string provider).
    """
    try:
        ls_params = model._get_ls_params()  # ty: ignore[unresolved-attribute]
    except (AttributeError, TypeError, RuntimeError, NotImplementedError):
        logger.debug("_get_ls_params raised for %s", type(model).__name__)
        return None
    if isinstance(ls_params, dict):
        provider = ls_params.get("ls_provider")
        if isinstance(provider, str):
            return provider
    return None


# [해설] 교체 대상 모델이 Anthropic인지 판별. `_build_overrides`에서 Anthropic 전용 설정(`cache_control`)
# [해설] 제거 여부를 결정하는 데 쓰인다.
def _is_anthropic_model(model: object) -> bool:
    """Check whether a resolved model reports `'anthropic'` as its provider.

    Uses `_get_ls_params` from `BaseChatModel` to read the provider name.

    Args:
        model: A model instance to inspect.

            Typed as `object` (rather than `BaseChatModel`) so the caller can
            pass any model without an import-time dependency on a specific
            provider package.

    Returns:
        `True` if the model's `ls_provider` is `'anthropic'`.
    """
    return _get_ls_provider(model) == "anthropic"


# [해설] Fireworks 판별. `_build_overrides`에서 세션 affinity 헤더 주입 여부를 결정한다.
def _is_fireworks_model(model: object) -> bool:
    """Check whether a resolved model reports `'fireworks'` as its provider.

    Returns:
        `True` if the model's `ls_provider` is `'fireworks'`.
    """
    return _get_ls_provider(model) == "fireworks"


# [해설] OpenAI(및 OpenAI 호환 프록시·LangSmith gateway 포함) 판별. `prompt_cache_key` 주입 대상 결정용.
# [해설][주의] base URL을 보지 않으므로 알 수 없는 필드를 거부하는 호환 엔드포인트는
# [해설]   `[models].openai_prompt_cache_key = false`로 끄도록 되어 있다.
def _is_openai_model(model: object) -> bool:
    """Check whether a resolved model targets OpenAI's chat/responses API.

    `prompt_cache_key` is an optional, additive OpenAI request field, so it is
    attempted for every model whose LangSmith provider is `'openai'` regardless
    of base URL. `ChatOpenAI` reports `'openai'` for the official API, the
    LangSmith gateway, and other OpenAI-compatible endpoints alike; treating all
    of them as eligible is intentional so the cache-key optimization is not
    silently dropped behind a proxy. Endpoints that reject unknown request
    fields can opt out via the `models.openai_prompt_cache_key` config option.

    Returns:
        `True` if the model reports `'openai'` as its provider.
    """
    return _get_ls_provider(model) == "openai"


# [해설] Anthropic 계열 미들웨어(`AnthropicPromptCachingMiddleware`)가 `model_settings`에 넣는 키 집합.
# [해설] 다른 프로바이더로 교체될 때 `_build_overrides`가 이 키들을 제거한다(예: OpenAI SDK에 넘기면 TypeError).
_ANTHROPIC_ONLY_SETTINGS: set[str] = {"cache_control"}
"""Keys injected by Anthropic-specific middleware (e.g.
`AnthropicPromptCachingMiddleware`) that are not accepted by other providers and
must be stripped on cross-provider swap."""

# [해설] Fireworks 프롬프트 캐시를 같은 세션(스레드)으로 라우팅하기 위한 헤더 이름. 값은 thread_id.
_FIREWORKS_SESSION_AFFINITY_HEADER = "x-session-affinity"
"""Fireworks prompt-cache affinity header populated from the active thread ID."""


# [해설] HTTP 헤더는 대소문자 구분이 없으므로 기존 헤더 존재 여부를 소문자 비교로 검사한다.
def _has_header(headers: Mapping[object, object], target: str) -> bool:
    """Return whether a headers mapping already includes `target`.

    Comparison is case-insensitive; `target` must be supplied in lowercase.

    Returns:
        `True` if a string key case-insensitively equal to `target` is present.
    """
    return any(isinstance(key, str) and key.lower() == target for key in headers)


# [해설] Fireworks 요청에 `x-session-affinity` 헤더와 `prompt_cache_key`를 (없을 때만) 채워 넣는다.
# [해설] 호출자: `_build_overrides`. 반환 None = 변경 없음(원본 request 유지).
def _with_fireworks_session_settings(
    model_settings: dict[str, Any], thread_id: str
) -> dict[str, Any] | None:
    """Return model settings with Fireworks session settings added if needed.

    Existing settings are preserved and never overwritten. Missing
    `x-session-affinity` headers are populated directly so Fireworks can route
    the conversation to the prompt-cache session for the active thread.

    Returns:
        A new `model_settings` dict with the missing session settings added, or
            `None` when nothing needed adding or `extra_headers` is present but
            not a mapping (leaving the request untouched).
    """
    # [해설][흐름] 1) `extra_headers`를 복사해 정규화. Mapping이 아니면 건드리지 않고 경고 후 포기.
    raw_headers = model_settings.get("extra_headers")
    if raw_headers is None:
        headers: dict[object, object] = {}
    elif isinstance(raw_headers, Mapping):
        headers = dict(raw_headers)
    else:
        logger.warning(
            "Cannot inject Fireworks session settings because extra_headers is %s",
            type(raw_headers).__name__,
        )
        return None

    # [해설][흐름] 2) 사용자가 이미 affinity 헤더를 준 경우 사용자 의도를 존중해 `prompt_cache_key`도 추가하지 않는다.
    updated: dict[str, Any] = {}
    has_session_affinity = _has_header(headers, _FIREWORKS_SESSION_AFFINITY_HEADER)
    if "prompt_cache_key" not in model_settings and not has_session_affinity:
        updated["prompt_cache_key"] = thread_id

    # [해설][흐름] 3) affinity 헤더가 없을 때만 thread_id로 채운다. 기존 키는 절대 덮어쓰지 않는다.
    if not has_session_affinity:
        headers[_FIREWORKS_SESSION_AFFINITY_HEADER] = thread_id
        updated["extra_headers"] = headers

    if not updated:
        return None
    return {**model_settings, **updated}


# [해설] OpenAI 요청에 `prompt_cache_key=thread_id`를 추가(같은 스레드의 요청이 같은 캐시로 가도록).
# [해설] 모델 생성자 `model_kwargs`나 호출별 `model_settings`에 사용자가 넣은 키가 있으면 그대로 둔다.
# [해설] 호출자: `_build_overrides`(opt-out 판단은 호출자 책임).
def _with_openai_prompt_cache_key(
    model: object, model_settings: dict[str, Any], thread_id: str
) -> dict[str, Any] | None:
    """Return model settings with an OpenAI `prompt_cache_key` added if needed.

    Adds `thread_id` as a top-level `prompt_cache_key` when the model and the
    current invocation settings do not already carry one. Callers decide
    eligibility (provider check + `models.openai_prompt_cache_key` opt-out)
    before invoking this helper.

    A user-supplied `prompt_cache_key` is always preserved, whether it was
    configured on the model (`model_kwargs`) or supplied for this invocation
    (`model_settings`).

    Returns:
        A new `model_settings` dict with `prompt_cache_key` added, or `None` when
            a key is already present on the model or in the settings (nothing to
            add).
    """
    model_kwargs = getattr(model, "model_kwargs", None)
    if model_kwargs is not None and not isinstance(model_kwargs, Mapping):
        # A non-mapping `model_kwargs` cannot carry a user-supplied key, so it is
        # treated as "no key present" and injection proceeds. Trace the anomaly
        # since a real `ChatOpenAI` always exposes a mapping here.
        logger.debug(
            "Ignoring non-mapping model_kwargs (%s) when checking for a "
            "user-supplied prompt_cache_key",
            type(model_kwargs).__name__,
        )
    if "prompt_cache_key" in model_settings or (
        isinstance(model_kwargs, Mapping) and "prompt_cache_key" in model_kwargs
    ):
        return None
    return {**model_settings, "prompt_cache_key": thread_id}


# [해설] `[models].openai_prompt_cache_key` 설정(기본 on)을 미들웨어 생성 시 1회만 읽는다.
# [해설] 호출자: `ConfigurableModelMiddleware.__init__`. 실제 조회는 `config.is_openai_prompt_cache_key_enabled`.
# [해설][설계] 실패 시 fail-open(True)이지만, 이벤트 루프 블로킹 위반(`BlockingError`)만은 재발생시켜 회귀를 드러낸다.
def _resolve_openai_prompt_cache_key_enabled() -> bool:
    """Resolve the `models.openai_prompt_cache_key` opt-out (default on).

    Called once when `ConfigurableModelMiddleware` is constructed. The read is
    kept off the blockbuster-guarded server loop by the caller: on the server
    path `create_cli_agent` runs inside `asyncio.to_thread` (see
    `server_graph._make_graphs`), so the synchronous `config.toml` read happens
    on a worker thread.

    On an unexpected failure this defaults to enabled: breaking agent
    construction over a config hiccup is worse than injecting the key, and the
    ordinary failure modes (a missing or corrupt `config.toml`) are already
    absorbed by `load_config_toml`. The trade-off is real, not cosmetic — a user
    who opted out *because their endpoint 400s on unknown request fields* would
    then see that per-request failure rather than a benign extra key — so the
    fallback logs at `warning` (not `debug`) to leave a breadcrumb.

    `BlockingError` is deliberately excluded from the fail-open: it signals a
    real blocking-I/O-on-the-event-loop regression (construction moved back onto
    the guarded loop), and swallowing it would mask that bug *and* silently
    defeat the opt-out. It is re-raised so the violation surfaces loudly. It is
    matched by class name because `blockbuster` is not a runtime dependency of
    this package (it is supplied by the langgraph runtime), so it cannot be
    imported here for an `isinstance` check.

    Returns:
        `True` when injection is enabled (the default), `False` when the opt-out
            is set.
    """
    try:
        from deepagents_code.config import is_openai_prompt_cache_key_enabled

        return is_openai_prompt_cache_key_enabled()
    except Exception as exc:
        # [해설][주의] `blockbuster` 패키지를 import할 수 없어 MRO의 클래스 이름으로 매칭한다.
        if any(cls.__name__ == "BlockingError" for cls in type(exc).__mro__):
            raise
        logger.warning(
            "Could not resolve models.openai_prompt_cache_key; defaulting to ON "
            "(an opt-out you set may not take effect)",
            exc_info=True,
        )
        return True


# [해설] `request.runtime.context`(LangGraph runtime context)를 `CLIContextSchema`로 해석.
# [해설] 클라이언트가 `context=`로 보낸 `model`/`model_params`/`thread_id`/`profile_overrides`가 여기서 나온다.
# [해설] 형태가 맞지 않으면 None → 오버라이드 없이 원래 모델로 진행. 정의: `_cli_context.py`.
def _get_context(request: ModelRequest) -> CLIContextSchema | None:
    """Return runtime context when it matches the CLI context shape."""
    runtime = request.runtime
    if runtime is None:
        return None

    return CLIContextSchema.from_payload(runtime.context)


# [해설] 모델 객체로부터 resume용 `provider:model` spec을 역산한다(checkpoint `_model_spec` 기록용).
# [해설] 우선순위: `ModelResult`(생성 시 메타데이터) → `runtime_state`의 provider/model과 이름 일치 →
# [해설] `ls_provider` + 모델 식별자 → `runtime_state` 값 → None.
# [해설][주의] `runtime_state`는 프로세스 전역이며 서버 서브프로세스에서는 `/model`로 갱신되지 않는다(`_build_overrides` 주석 참고).
def _model_spec_from_model(
    model: BaseChatModel, model_result: ModelResult | None = None
) -> str | None:
    """Return a resumable `provider:model` spec for a model object."""
    if model_result is not None:
        return f"{model_result.provider}:{model_result.model_name}"
    model_name = get_model_identifier(model)
    from deepagents_code.config import runtime_state

    settings_provider = runtime_state.model_provider or ""
    settings_model = runtime_state.model_name or ""
    if settings_provider and settings_model and model_name == settings_model:
        return f"{settings_provider}:{settings_model}"
    provider = _get_ls_provider(model)
    if provider and model_name:
        return f"{provider}:{model_name}"
    if settings_provider and settings_model:
        return f"{settings_provider}:{settings_model}"
    return None


# [해설] 런타임 교체로 `create_model`이 돌려준 결과가 있으면 그 spec을, 없으면 모델 메타데이터로 추정한다.
def _model_spec_from_result(
    model_result: ModelResult | None, model: BaseChatModel
) -> str | None:
    """Return the resolved spec from `create_model`, falling back to model metadata."""
    if model_result is not None and model_result.provider and model_result.model_name:
        return f"{model_result.provider}:{model_result.model_name}"
    return _model_spec_from_model(model)


# [해설] 실제 request 오버라이드를 조립하는 공통 로직(동기/비동기 경로가 공유).
# [해설] 입력: 원 request, CLI context, (교체 시) `ModelResult`. 출력: `request.override(...)` 결과 또는 원본.
# [해설][SDK] `ModelRequest.override`는 불변 request의 복사본을 만든다(langchain agents middleware).
def _build_overrides(
    request: ModelRequest,
    ctx: CLIContextSchema,
    model_result: ModelResult | None,
    *,
    openai_prompt_cache_key: bool,
) -> ModelRequest:
    """Build the overridden request from a (possibly resolved) model result.

    Holds the post-construction logic shared by the sync and async override
    paths: applying the model swap, merging `model_params`, stripping
    Anthropic-only settings on a cross-provider swap, and patching the
    `### Model Identity` system-prompt section. The only thing that differs
    between the two callers is how `model_result` is produced (a direct
    `create_model` call vs. an `asyncio.to_thread` offload).

    Args:
        request: The incoming model request from the middleware chain.
        ctx: Runtime CLI context carrying the requested overrides.
        model_result: The resolved model result from `create_model`, or `None`
            when no model swap was requested.
        openai_prompt_cache_key: Whether OpenAI `prompt_cache_key` injection is
            enabled (the resolved `models.openai_prompt_cache_key` opt-out).

    Returns:
        The original request when no overrides apply, otherwise a new request
            with overrides applied via `request.override()`.
    """
    # [해설][흐름] 1) 모델 교체: `create_model`로 만든 새 모델 객체를 overrides["model"]에 넣는다.
    overrides: dict[str, Any] = {}

    new_model = model_result.model if model_result is not None else None
    if new_model is not None:
        overrides["model"] = new_model

    # [해설][흐름] 2) 파라미터 병합: `model_params`를 기존 `model_settings` 위에 얕게(shallow) 덮어쓴다.
    # Param merge
    model_params = ctx.model_params
    if model_params:
        overrides["model_settings"] = {**request.model_settings, **model_params}

    # [해설][흐름] 3) thread_id가 있으면 프로바이더별 프롬프트 캐시 라우팅 힌트 주입(Fireworks 또는 OpenAI 중 하나만).
    # Inject the provider's prompt-cache routing hint from the active thread.
    # Only one provider path applies per call; both share the fetch/guard/log
    # tail below. `overrides.get` is side-effect-free, so resolving `settings`
    # before the provider check is equivalent to doing it inside each branch.
    effective_model = new_model if new_model is not None else request.model
    if ctx.thread_id:
        settings = overrides.get("model_settings", request.model_settings)
        if _is_fireworks_model(effective_model):
            # Fireworks has no opt-out gate. The classifier is provider-only
            # (like the OpenAI one), so this does not *verify* a fixed endpoint;
            # it rests on the assumption that `ChatFireworks` in practice targets
            # Fireworks' hosted API, where unknown-field rejection is not the
            # concern it is for the broadened, proxy-reachable OpenAI path below.
            updated_settings = _with_fireworks_session_settings(settings, ctx.thread_id)
            injected = "Fireworks session settings"
        elif _is_openai_model(effective_model):
            if openai_prompt_cache_key:
                updated_settings = _with_openai_prompt_cache_key(
                    effective_model, settings, ctx.thread_id
                )
                injected = "OpenAI prompt_cache_key"
            else:
                # Opt-out fired: leave the request untouched but log it so a user
                # verifying `models.openai_prompt_cache_key=false` sees a positive
                # signal rather than having to infer it from an absent log line.
                updated_settings = None
                injected = ""
                logger.debug("Skipped OpenAI prompt_cache_key (opt-out)")
        else:
            updated_settings = None
            injected = ""
        if updated_settings is not None:
            overrides["model_settings"] = updated_settings
            # The thread ID is a sensitive session identifier, so it is kept out
            # of the log line; the line firing at all confirms injection ran.
            logger.debug("Injected %s", injected)

    # [해설][흐름] 4) 적용할 오버라이드가 없으면 원본 request를 그대로 반환(복사 비용 없음).
    if not overrides:
        return request

    # [해설][흐름] 5) Anthropic → 타 프로바이더 교체 시 Anthropic 전용 키(`_ANTHROPIC_ONLY_SETTINGS`) 제거.
    # When switching away from Anthropic, strip provider-specific settings
    # that would cause errors on other providers (e.g. cache_control passed
    # to the OpenAI SDK raises TypeError).
    if new_model is not None and not _is_anthropic_model(new_model):
        settings = overrides.get("model_settings", request.model_settings)
        dropped = settings.keys() & _ANTHROPIC_ONLY_SETTINGS
        if dropped:
            logger.debug(
                "Stripped Anthropic-only settings %s for non-Anthropic model",
                dropped,
            )
            overrides["model_settings"] = {
                k: v for k, v in settings.items() if k not in dropped
            }

    # [해설][흐름] 6) 시스템 프롬프트의 `### Model Identity` 절을 새 모델 기준으로 재작성.
    # [해설]    정규식·빌더는 `agent.py`의 `MODEL_IDENTITY_RE`, `build_model_identity_section`.
    # Patch the Model Identity section in the system prompt so the new model
    # sees its own name/provider/context-limit, not the original's.
    # Read metadata from `model_result`, not the process-wide runtime state:
    # the middleware runs in the server subprocess, whose state is not updated
    # by `/model`.
    if model_result is not None and request.system_prompt:
        from deepagents_code.agent import (
            MODEL_IDENTITY_RE,
            build_model_identity_section,
        )

        prompt = request.system_prompt
        new_identity = build_model_identity_section(
            model_result.model_name,
            provider=model_result.provider,
            context_limit=model_result.context_limit,
            unsupported_modalities=model_result.unsupported_modalities,
        )
        patched = MODEL_IDENTITY_RE.sub(new_identity, prompt, count=1)
        if patched != prompt:
            overrides["system_prompt"] = patched
        # [해설][주의] 템플릿 변경으로 정규식이 어긋나면 새 모델이 옛 모델 이름을 자기 정체성으로 보게 되므로 경고만 남긴다.
        elif "### Model Identity" in prompt:
            logger.warning(
                "System prompt contains '### Model Identity' but regex "
                "did not match; identity section was NOT updated for "
                "model '%s'. The regex may be out of sync with the "
                "prompt template.",
                model_result.model_name,
            )

    return request.override(**overrides)


# [해설] 런타임 교체용 `create_model` 추가 인자: CLI `--max-retries`와 `--profile-override`를
# [해설] 교체 후에도 유지하기 위함(공식 문서 "Profile overrides ... `/model` hot-swap 이후에도 유지").
def _model_creation_kwargs(
    ctx: CLIContextSchema, cli_max_retries: int | None
) -> dict[str, Any]:
    """Build constructor kwargs needed for a runtime model switch.

    Returns:
        Keyword arguments for `create_model`.
    """
    kwargs: dict[str, Any] = {}
    if cli_max_retries is not None:
        kwargs["cli_max_retries"] = cli_max_retries
    if ctx.profile_overrides:
        kwargs["profile_overrides"] = ctx.profile_overrides
    return kwargs


# [해설] (동기 경로) runtime context를 읽어 모델 교체/파라미터 병합을 수행하고 checkpoint 메타데이터를 반환.
# [해설] 호출자: `ConfigurableModelMiddleware.wrap_model_call`. 호출: `config.create_model`, `_build_overrides`.
# [해설] 오류 정책: 정책 차단(`ModelNotAllowedError`)은 항상 전파, 일반 설정 오류는 strict일 때만 전파(아니면 현 모델로 계속).
def _apply_overrides(
    request: ModelRequest,
    *,
    openai_prompt_cache_key: bool,
    cli_max_retries: int | None,
    strict_model_resolution: bool = False,
    construction_model_result: ModelResult | None = None,
) -> _ResolvedModelRequest:
    """Apply model/param overrides and return checkpoint persistence metadata.

    Reads `'model'` and `'model_params'` from `runtime.context` and, when
    present, swaps the model and/or merges extra settings into the request.
    On a cross-provider swap away from Anthropic, Anthropic-only settings
    (e.g. `cache_control`) are stripped. The `### Model Identity` section
    in the system prompt is also patched to reflect the new model.

    Args:
        request: The incoming model request from the middleware chain.
        openai_prompt_cache_key: The resolved `models.openai_prompt_cache_key`
            opt-out, threaded through to `_build_overrides`.
        cli_max_retries: Explicit CLI retry count retained across model switches.
        strict_model_resolution: Whether model construction failures should propagate.
        construction_model_result: Construction-time workspace model metadata.

    Returns:
        The request to send downstream plus the actual model spec and user-supplied
            model params that should be recorded for resume.

    Raises:
        ModelNotAllowedError: If runtime context requests a blocked model.
        ModelConfigError: If strict resolution is enabled and construction fails.
    """
    # [해설][흐름] 1) CLI context가 없으면 오버라이드 없이 현재 모델 spec만 계산해 반환.
    ctx = _get_context(request)
    if ctx is None:
        return _ResolvedModelRequest(
            request, _model_spec_from_model(request.model, construction_model_result)
        )

    # [해설][흐름] 2) context의 모델이 현재 request 모델과 다를 때만 새 모델을 생성(같으면 재생성 비용 회피).
    # [해설][SDK] `model_matches_spec`은 `provider:model` spec과 모델 객체의 식별자를 비교한다.
    model_result = None
    model = ctx.model
    if model and not model_matches_spec(request.model, model):
        from deepagents_code.config import create_model
        from deepagents_code.model_config import ModelConfigError, ModelNotAllowedError

        logger.debug("Overriding model to %s", model)
        model_kwargs = _model_creation_kwargs(ctx, cli_max_retries)
        # [해설][흐름] 3) 모델 생성. 이 안에서 자격증명 해석·allowlist 정책 검사가 일어난다(`config.create_model`).
        try:
            model_result = create_model(model, **model_kwargs)
        except ModelNotAllowedError:
            # `ModelNotAllowedError` is a `ModelConfigError`; without this
            # clause the handler below would swallow a policy denial, log it,
            # and silently continue on the *current* model while the UI
            # reported a switch. Not redundant -- do not remove.
            raise
        except ModelConfigError:
            if strict_model_resolution:
                raise
            logger.exception(
                "Failed to resolve runtime model override '%s'; "
                "continuing with current model",
                model,
            )
            # `model_params_known=False` deliberately: the override never
            # reached `_build_overrides`, so which params are in effect is
            # exactly what this path does not know. Writing the default `None`
            # instead would clear the checkpoint's params while the app still
            # holds its override, and the cold-cache identity check would then
            # compare a populated map against `None` on every send -- a
            # permanent, false "the model changed".
            return _ResolvedModelRequest(
                request,
                _model_spec_from_model(request.model, construction_model_result),
                model_params_known=False,
            )

    # [해설][흐름] 4) 오버라이드 조립 후 checkpoint에 기록할 spec/params(런타임 오버라이드만)를 함께 반환.
    updated = _build_overrides(
        request, ctx, model_result, openai_prompt_cache_key=openai_prompt_cache_key
    )
    params = dict(ctx.model_params) if ctx.model_params else None
    return _ResolvedModelRequest(
        updated,
        _model_spec_from_result(model_result, updated.model),
        params,
        model_params_known=True,
    )


# [해설] `_apply_overrides`의 비동기판. 차이는 `create_model`을 `asyncio.to_thread`로 오프로드한다는 점 하나뿐.
# [해설] 서버의 blockbuster 가드된 이벤트 루프에서 설정 파일·자격증명 파일 읽기가 블로킹 오류를 내지 않도록 하기 위함.
# [해설][주의] 두 함수 본문이 거의 복제되어 있으므로 한쪽만 수정하면 동작이 갈라진다.
async def _apply_overrides_async(
    request: ModelRequest,
    *,
    openai_prompt_cache_key: bool,
    cli_max_retries: int | None,
    strict_model_resolution: bool = False,
    construction_model_result: ModelResult | None = None,
) -> _ResolvedModelRequest:
    """Async variant of `_apply_overrides` that offloads model construction.

    Args:
        request: The incoming model request from the middleware chain.
        openai_prompt_cache_key: The resolved `models.openai_prompt_cache_key`
            opt-out, threaded through to `_build_overrides`.
        cli_max_retries: Explicit CLI retry count retained across model switches.
        strict_model_resolution: Whether model construction failures should propagate.
        construction_model_result: Construction-time workspace model metadata.

    Returns:
        The request to send downstream plus the actual model spec and user-supplied
            model params that should be recorded for resume.

    Raises:
        ModelNotAllowedError: If runtime context requests a blocked model.
        ModelConfigError: If strict resolution is enabled and construction fails.
    """
    ctx = _get_context(request)
    if ctx is None:
        return _ResolvedModelRequest(
            request, _model_spec_from_model(request.model, construction_model_result)
        )

    model_result = None
    model = ctx.model
    if model and not model_matches_spec(request.model, model):
        from deepagents_code.config import create_model
        from deepagents_code.model_config import ModelConfigError, ModelNotAllowedError

        logger.debug("Overriding model to %s", model)
        model_kwargs = _model_creation_kwargs(ctx, cli_max_retries)
        try:
            # [해설][흐름] 모델 생성(블로킹 I/O 포함)을 워커 스레드에서 수행.
            model_result = await asyncio.to_thread(
                create_model,
                model,
                **model_kwargs,
            )
        except ModelNotAllowedError:
            # `ModelNotAllowedError` is a `ModelConfigError`; without this
            # clause the handler below would swallow a policy denial, log it,
            # and silently continue on the *current* model while the UI
            # reported a switch. Not redundant -- do not remove.
            raise
        except ModelConfigError:
            if strict_model_resolution:
                raise
            logger.exception(
                "Failed to resolve runtime model override '%s'; "
                "continuing with current model",
                model,
            )
            # `model_params_known=False` deliberately: the override never
            # reached `_build_overrides`, so which params are in effect is
            # exactly what this path does not know. Writing the default `None`
            # instead would clear the checkpoint's params while the app still
            # holds its override, and the cold-cache identity check would then
            # compare a populated map against `None` on every send -- a
            # permanent, false "the model changed".
            return _ResolvedModelRequest(
                request,
                _model_spec_from_model(request.model, construction_model_result),
                model_params_known=False,
            )

    updated = _build_overrides(
        request, ctx, model_result, openai_prompt_cache_key=openai_prompt_cache_key
    )
    params = dict(ctx.model_params) if ctx.model_params else None
    return _ResolvedModelRequest(
        updated,
        _model_spec_from_result(model_result, updated.model),
        params,
        model_params_known=True,
    )


# [해설] checkpoint에 기록할 요청 시작 시각(UTC ISO). 캐시 cold 경과시간 계산의 기준점.
def _utc_now_iso() -> str:
    """Return the current UTC time in checkpoint-safe ISO format."""
    return datetime.now(UTC).isoformat()


# [해설] 이번 호출이 "실제로" 사용한 캐시 관련 파라미터(config.toml params + 런타임 오버라이드)를 계산한다.
# [해설] 결과는 `_last_cache_params` 채널로 간다(`_model_params`와 분리 — 아래 docstring 참조).
# [해설] 호출자: `wrap_model_call`/`awrap_model_call`. 호출: `ModelConfig.get_effective_kwargs`,
# [해설] `config._compose_openai_reasoning_effort`, `cold_cache.cache_identity_params`.
def _effective_cache_params(
    model_spec: str | None, runtime_overrides: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Resolve the cache-identity params a request actually runs with.

    Mirrors the app-side comparison: the cold-cache check reads configured
    provider/per-model `params` (via `ModelConfig.get_effective_kwargs`) plus
    runtime overrides. If the checkpoint stores only the runtime overrides, a
    user with a configured `prompt_cache_retention` and no session override
    records `None` here while the reader sees `{"prompt_cache_retention": ...}`,
    so the next turn compares unequal and reports a false `identity_changed`
    every turn. Persisting the effective params makes both sides match.

    The result is projected through `cache_identity_params` and recorded in the
    dedicated `_last_cache_params` channel rather than `_model_params`: the
    latter is read back on resume as per-session runtime overrides, so storing
    the merged config there would pin every configured knob (temperature,
    headers, ...) into old threads and silently override newer config.

    Performs blocking config reads; async callers should offload.

    Args:
        model_spec: `provider:model` spec for the call.
        runtime_overrides: Per-request params (the middleware's `model_params`).

    Returns:
        Cache-identity projection of the effective kwargs (without `base_url`),
        or `None` when no identity keys are present.
    """
    # [해설][흐름] 1) provider를 알 수 없는 spec이면 런타임 오버라이드만 식별 파라미터로 투영.
    if not model_spec or ":" not in model_spec:
        overrides = dict(runtime_overrides) if runtime_overrides else None
        return cache_identity_params(overrides, model_spec=model_spec) or None
    from deepagents_code.config import _compose_openai_reasoning_effort
    from deepagents_code.model_config import ModelConfig

    _, _, model_name = model_spec.partition(":")
    provider = model_spec.split(":", 1)[0].strip().lower()
    # [해설][흐름] 2) 설정 파일 기준 유효 kwargs 계산. 실패 시 경고 후 1)과 같은 폴백.
    try:
        config = ModelConfig.load()
        kwargs = config.get_effective_kwargs(
            provider,
            model_name=model_name,
            overrides=runtime_overrides,
        )
    except Exception:
        logger.warning(
            "Could not resolve effective cache params for %r; recording only "
            "runtime overrides, so a configured cache param may read as a "
            "spurious identity change until the next turn",
            model_spec,
            exc_info=True,
        )
        overrides = dict(runtime_overrides) if runtime_overrides else None
        return cache_identity_params(overrides, model_spec=model_spec) or None
    if not isinstance(kwargs, dict):
        overrides = dict(runtime_overrides) if runtime_overrides else None
        return cache_identity_params(overrides, model_spec=model_spec) or None
    # [해설][흐름] 3) OpenAI reasoning effort 구성 규칙을 생성자와 동일하게 적용해 읽는 쪽 계산과 일치시킨다.
    # Match constructor precedence when config uses native nested reasoning.
    overrides = runtime_overrides or {}
    kwargs = _compose_openai_reasoning_effort(
        provider, kwargs, overrides.get("reasoning_effort"), overrides.get("reasoning")
    )
    # `base_url` is tracked separately as the endpoint identity; keeping it out
    # of the params avoids a double-counted identity change.
    result = cache_identity_params(
        {k: v for k, v in kwargs.items() if k != "base_url"}, model_spec=model_spec
    )
    return result or None


# [해설] 모델 호출이 끝난 뒤 checkpoint에 쓸 비공개 state 업데이트(`Command(update=...)`)를 만든다.
# [해설] 기록 채널: `_last_model_request_at`, `_last_cache_model_spec`, `_last_cache_endpoint`, `_model_spec`,
# [해설] `_model_params`, `_last_cache_params`. 소비자: `resume_state.py`(모델 복원), `app.py`/`cold_cache.py`(cold 경고).
# [해설][설계] handler() 성공 후에만 호출되므로 이 Command의 존재 자체가 "성공한 호출" 표식이 된다.
def _checkpoint_command(
    resolved: _ResolvedModelRequest,
    request_started_at: str,
    cache_endpoint: str,
    cache_params: dict[str, Any] | None = None,
) -> Command[Any]:
    """Build the private resume-state update for a completed model call.

    Args:
        resolved: The request as actually sent, after override resolution.
        request_started_at: UTC ISO timestamp captured before the model call.
            It only reaches a checkpoint because this runs after `handler()`
            returned, which is what makes it a successful-call marker.
        cache_endpoint: Endpoint identity for `resolved.model_spec`, from
            `_cache_endpoint_identity`. Passed in rather than resolved here so
            the async caller can keep its blocking config/credential reads off
            the event loop.
        cache_params: Cache-identity projection of the effective params for
            this call, from `_effective_cache_params`. Passed in for the same
            offloading reason as `cache_endpoint`. When `None` and
            `resolved.model_params_known` is true, falls back to the identity
            projection of the runtime overrides.

    Returns:
        Command carrying cache timing and effective model metadata.
    """
    update: dict[str, Any] = {}
    # Use the resolved spec, not `_apply_overrides`'s `ctx.model`: when an
    # override fails with `ModelConfigError`, `_apply_overrides` falls back to
    # the original model while `ctx.model` still names the rejected override.
    #
    # The timestamp is written only alongside a known spec. The three are one
    # fact -- when the cache was warmed, for which model, and against which
    # endpoint -- and a timestamp without an identity would read back as a
    # permanent "model changed", warning on every send with copy that names a
    # change that never happened. For the same reason the endpoint is written
    # unconditionally here: a skipped write would leave the *previous* turn's
    # endpoint describing this turn's spec and timestamp.
    if resolved.model_spec:
        update["_last_model_request_at"] = request_started_at
        update["_last_cache_model_spec"] = resolved.model_spec
        update["_last_cache_endpoint"] = cache_endpoint
        update["_model_spec"] = resolved.model_spec
    else:
        # The previous turn's timestamp stays in place, so the next cold-cache
        # age is computed against an older request than the one just made.
        logger.debug(
            "Not recording prompt-cache state: no model spec could be derived from %s",
            type(resolved.request.model).__name__,
        )
    if resolved.model_params_known:
        # `_model_params` stays the *runtime overrides only*: resume reads it
        # back as per-session overrides for `_switch_model`, so storing the
        # merged config there would pin provider defaults (temperature, max
        # retries, headers, ...) into old threads and silently override newer
        # config on resume.
        update["_model_params"] = resolved.model_params
        # The cold-cache identity projection goes to its own channel. It must
        # include configured provider params -- not just the session override
        # above -- or the reader's effective-params comparison reports a false
        # `identity_changed` on every turn for anyone with a configured cache
        # knob.
        update["_last_cache_params"] = (
            cache_params
            if cache_params is not None
            else (
                cache_identity_params(
                    resolved.model_params, model_spec=resolved.model_spec
                )
                or None
            )
        )
    return Command(update=update)


# [해설] 런타임 모델 교체 미들웨어 본체. `agent.py`가 메인 에이전트·서브에이전트 미들웨어 스택에,
# [해설] `goal_rubric.py`가 루브릭 평가 에이전트에 넣는다.
# [해설] 흐름: runtime context 읽기 → 필요 시 모델 생성·교체 → handler 호출 → checkpoint 메타데이터 Command 반환.
# [해설][SDK] `ExtendedModelResponse(model_response, command)`로 모델 응답과 state 업데이트를 함께 돌려준다.
class ConfigurableModelMiddleware(AgentMiddleware):
    """Swap the model or per-call settings from `runtime.context`.

    Reads two optional keys from the runtime context dict:

    - `'model'` — a `provider:model` spec (e.g. `"openai:gpt-5"`).
        When present and different from the current model, the request is
        re-routed to the new model.
    - `'model_params'` — a dict of extra model settings (e.g.
        `{"temperature": 0}`) that are shallow-merged into the
        request's `model_settings`.

    This middleware is typically the outermost layer so it intercepts every
    model call before provider-specific middleware (like
    `AnthropicPromptCachingMiddleware`) runs.
    """

    # [해설] 트레이스에 훅 입력(프롬프트 등 대용량/민감 payload)을 남기지 않도록 기본 정책을 지정.
    trace_policy = TracePolicy(process_inputs=omit_payload)
    """Omit hook inputs from traces by default; set a `TracePolicy` to override."""

    # [해설] 생성자: 설정값을 인스턴스에 고정. `persist_model_state=False`는 서브에이전트용(부모 스레드 resume state 소유 X).
    # [해설] `environ`은 워크스페이스별 환경변수 스냅샷으로, 모델 교체 시 `config.use_environment`로 적용된다.
    def __init__(
        self,
        *,
        persist_model_state: bool = True,
        openai_prompt_cache_key: bool | None = None,
        cli_max_retries: int | None = None,
        strict_model_resolution: bool = False,
        environ: Mapping[str, str] | None = None,
        model_result: ModelResult | None = None,
    ) -> None:
        """Initialize the middleware.

        Args:
            persist_model_state: Whether completed calls should write private
                resume metadata. Subagent instances disable this because they do
                not own the parent thread's resume state.
            openai_prompt_cache_key: Whether to inject a per-thread OpenAI
                `prompt_cache_key`. Left as `None` (the default) it is resolved
                once here from `models.openai_prompt_cache_key` and cached, so no
                per-call read happens. The one-time `config.toml` read assumes
                current callers construct the middleware off the
                blockbuster-guarded server loop (the server path offloads
                `create_cli_agent` via `asyncio.to_thread`); if that assumption
                is ever broken the read would trip `BlockingError`, which
                `_resolve_openai_prompt_cache_key_enabled` re-raises rather than
                masks. Pass an explicit bool to bypass the config read (mainly
                for tests).
            cli_max_retries: Explicit `--max-retries` value to retain across
                runtime model switches.
            strict_model_resolution: Whether invalid runtime model overrides should
                fail the call instead of falling back to the construction-time model.
            environ: Workspace environment retained for lazy model switches.
            model_result: Construction-time workspace model metadata.
        """
        self._persist_model_state = persist_model_state
        self._environ = environ
        self._model_result = model_result
        self._cli_max_retries = cli_max_retries
        self._strict_model_resolution = strict_model_resolution
        # [해설] OpenAI prompt_cache_key opt-out은 여기서 1회만 해석하고 캐시한다(호출마다 config.toml을 읽지 않음).
        self._openai_prompt_cache_key = (
            _resolve_openai_prompt_cache_key_enabled()
            if openai_prompt_cache_key is None
            else openai_prompt_cache_key
        )

    # [해설] 동기 모델 호출 훅. 호출자: LangChain agent 런타임(`invoke` 경로).
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Apply runtime overrides and delegate to the next handler.

        Returns:
            The downstream response plus a private resume-state update when the
            completed call has model metadata to checkpoint.
        """
        from deepagents_code.config import use_environment

        # [해설][흐름] 1) 워크스페이스 환경(`self._environ`)을 적용한 상태에서 오버라이드 해석·모델 생성.
        with use_environment(self._environ):
            resolved = _apply_overrides(
                request,
                openai_prompt_cache_key=self._openai_prompt_cache_key,
                cli_max_retries=self._cli_max_retries,
                strict_model_resolution=self._strict_model_resolution,
                construction_model_result=self._model_result,
            )
            # [해설][흐름] 2) 호출 직전 시각을 잡고 하위 handler(다음 미들웨어→실제 모델) 실행.
            request_started_at = _utc_now_iso()
            response = handler(resolved.request)
            # [해설][흐름] 3) 서브에이전트 등 state 비소유 인스턴스는 응답만 반환.
            if not self._persist_model_state:
                return response
            # [해설][흐름] 4) 캐시 식별 정보(엔드포인트/유효 파라미터) 계산 후 checkpoint Command를 붙여 반환.
            cache_endpoint = (
                _cache_endpoint_identity(resolved.model_spec, resolved.model_params)
                if resolved.model_params is not None
                else _cache_endpoint_identity(resolved.model_spec)
            )
            cache_params = _effective_cache_params(
                resolved.model_spec, resolved.model_params
            )
        command = _checkpoint_command(
            resolved,
            request_started_at,
            cache_endpoint,
            cache_params,
        )
        return ExtendedModelResponse(model_response=response, command=command)

    # [해설] 비동기 모델 호출 훅(비동기 `astream` 실행 경로 — LangGraph 서버에서 주로 이 경로를 탄다(추정)).
    # [해설] 동기판과 같은 순서지만 블로킹 I/O(모델 생성, 설정·자격증명 읽기)를 모두 `asyncio.to_thread`로 뺀다.
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Apply runtime overrides and delegate to the next async handler.

        Returns:
            The downstream response plus a private resume-state update when the
            completed call has model metadata to checkpoint.
        """
        from deepagents_code.config import use_environment

        # [해설][흐름] 1) 환경 적용 + 오버라이드 해석(모델 생성은 스레드 오프로드).
        with use_environment(self._environ):
            resolved = await _apply_overrides_async(
                request,
                openai_prompt_cache_key=self._openai_prompt_cache_key,
                cli_max_retries=self._cli_max_retries,
                strict_model_resolution=self._strict_model_resolution,
                construction_model_result=self._model_result,
            )
            # [해설][흐름] 2) 하위 handler await.
            request_started_at = _utc_now_iso()
            response = await handler(resolved.request)
            if not self._persist_model_state:
                return response
            # Offloaded: `_cache_endpoint_identity` and `_effective_cache_params`
            # read the config and credential store, which `blockbuster` rejects on
            # the server event loop.
            cache_endpoint = (
                await asyncio.to_thread(
                    _cache_endpoint_identity,
                    resolved.model_spec,
                    resolved.model_params,
                )
                if resolved.model_params is not None
                else await asyncio.to_thread(
                    _cache_endpoint_identity, resolved.model_spec
                )
            )
            cache_params = await asyncio.to_thread(
                _effective_cache_params,
                resolved.model_spec,
                resolved.model_params,
            )
        # [해설][흐름] 3) checkpoint Command 조립(순수 계산이라 이벤트 루프에서 수행해도 됨).
        command = _checkpoint_command(
            resolved, request_started_at, cache_endpoint, cache_params
        )
        return ExtendedModelResponse(model_response=response, command=command)
