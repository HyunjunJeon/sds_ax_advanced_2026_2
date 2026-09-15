"""Model-node retry middleware for the coding agent.

Wraps only the agent model node (not the whole agent turn) so transient model
connection failures are retried without re-running completed tool calls. Retry
counts are attached to constructed models upstream so runtime model switches
carry their provider-specific budget into each request. This module owns the
retry policy: which errors are transient, the backoff curve, and the user-facing
status surfaced while retrying.

Why not LangChain's `ModelRetryMiddleware`: it reads its retry count once at
construction, so it can't honor the provider-specific budget we stamp on each
model for runtime switches. It sleeps between attempts without saying
anything, which in a streaming terminal just looks frozen. When the budget
runs out it hands back an `AIMessage` containing the error text, so a dead
provider ends the turn disguised as a model answer. Its retry check only
inspects the raised exception, missing transport faults wrapped in exception
groups, and it jitters at 25% while ignoring `Retry-After` headers. None of
that is a knob; fixing any of it means overriding the whole loop, so we own
the loop here.
"""

# [해설] 이 모듈의 역할: 모델 호출 재시도 정책의 단일 소유자. (1) 어떤 오류가 일시적인가(분류), (2) 백오프 곡선과
# [해설] Retry-After 존중, (3) 재시도 중 사용자에게 보일 상태/스트림 이벤트(model_attempt, model_retry) 생성과 검증.
# [해설] 실행 프로세스: 둘 다.
# [해설]   - 서버: CodeModelRetryMiddleware(agent.py 메인 에이전트, goal_rubric.py), 보조 모델 호출용
# [해설]     retry_model_call/aretry_model_call(offload_middleware.py 요약, auto_mode.py 분류기).
# [해설]   - 클라이언트: *_from_event 검증 함수와 마커 문자열(TUI/headless 렌더러가 커스텀 스트림 이벤트를 해석할 때, 추정).
# [해설][설계] 재시도는 "모델 노드"만 감싼다 → 이미 끝난 도구 호출을 다시 실행하지 않는다.
# [해설] 모델별 재시도 예산은 모델 생성 시 MODEL_RETRIES_ATTR 속성으로 스탬프되고(config.py), 요청마다 읽는다.
# [해설] 중첩 재시도 곱셈을 막기 위해 SDK 자체 재시도는 꺼 두는 것이 전제다(analysis/03 참고).
# [해설] 관련 분석 문서: analysis/03-config-models-credentials.md / 관련 공식 문서: docs_official/code/configuration.md(추정)
from __future__ import annotations

import logging
import math
import random
import time
import uuid
from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, TracePolicy, omit_payload
from langchain_core.callbacks import BaseCallbackManager
from langchain_core.exceptions import ModelError
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.errors import GraphBubbleUp
from langgraph.pregel._messages import (  # noqa: PLC2701  # not publicly re-exported
    StreamMessagesHandler,
)

from deepagents_code.config import (
    DEFAULT_MODEL_RETRIES,
    MODEL_RETRIES_ATTR,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.callbacks import BaseCallbackHandler
    from langgraph.pregel.protocol import StreamChunk

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODEL_RETRIES",
    "INTERRUPTED_TOOL_OUTPUT",
    "CodeModelRetryMiddleware",
    "aretry_model_call",
    "build_attempt_event",
    "build_retry_event",
    "format_retry_status",
    "legacy_retry_index",
    "model_attempt_from_event",
    "model_retry_from_event",
    "retry_model_call",
    "retry_status_from_event",
]

# [해설] 백오프 파라미터: 첫 지연 0.2초, 배수 2, 지연 상한 10초, Retry-After 상한 60초, jitter ±10%.
# [해설] 예: attempt 0→0.2s, 1→0.4s, 2→0.8s ... 6부터 10s에 고정(jitter는 상한 적용 "후"라 최대 11s 가능).
# Tuned for interactive use: quick first retry, tight cap, modest jitter.
_INITIAL_DELAY_SECONDS = 0.2
_BACKOFF_FACTOR = 2.0
_MAX_DELAY_SECONDS = 10.0
_MAX_RETRY_AFTER_SECONDS = 60.0
_JITTER_FRACTION = 0.1
# [해설] 상태 코드 기반 재시도 대상: 408(Request Timeout), 409(Conflict), 429(Rate limit) + 아래 5xx 범위.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
# Provider-SDK error classes that name a transient failure, keyed by the root
# package that owns the name. The package is part of the key on purpose: these
# are generic words, and matching a bare class name would classify any
# dependency's identically-named error as transient -- the same rigor the
# httpcore/aiohttp checks in `_is_http_transport_error` already apply.
# [해설] 상태 코드가 없는 SDK 예외 중 일시적 오류로 인정할 (최상위 패키지, 클래스명) 쌍.
# [해설] _is_transient_sdk_error가 예외 MRO 전체와 비교한다(서브클래스도 매칭).
_TRANSIENT_SDK_EXC_NAMES = frozenset(
    {
        ("anthropic", "APIConnectionError"),
        ("anthropic", "APIConnectionTimeoutError"),
        ("anthropic", "APITimeoutError"),
        ("botocore", "ConnectionClosedError"),
        ("botocore", "ConnectTimeoutError"),
        ("botocore", "EndpointConnectionError"),
        ("botocore", "ReadTimeoutError"),
        # `google.api_core` statuses are read by `_google_api_core_status_code`
        # first; these cover the subclasses raised without a numeric code.
        ("google", "Aborted"),
        ("google", "DeadlineExceeded"),
        ("google", "ResourceExhausted"),
        ("google", "ServiceUnavailable"),
        ("openai", "APIConnectionError"),
        ("openai", "APITimeoutError"),
        ("urllib3", "ConnectTimeoutError"),
        ("urllib3", "ReadTimeoutError"),
        ("websockets", "ConnectionClosedError"),
    }
)

# [해설] 5xx 서버 오류 범위 [500, 600). _direct_model_error_retryability에서 사용.
_HTTP_SERVER_ERROR_FLOOR = 500
_HTTP_SERVER_ERROR_CEILING = 600
# [해설] 이벤트 숫자가 비정상일 때 쓰는 원인 없는 상태 문구(retry_status_from_event).
_RETRY_STATUS_FALLBACK = "Retrying model request"
# [해설] 아래 상수는 CodeModelRetryMiddleware의 wrap_model_call/awrap_model_call이 _delay_budget_guard에 넘기는 누적 한도.
# Total sleep the interactive model node may spend across one call's retries.
# Per-delay caps bound nothing (see `_delay_budget_guard`): five honoured
# `Retry-After` hints of `_MAX_RETRY_AFTER_SECONDS` each would stall a turn for
# five minutes behind a spinner. One full honoured hint still fits.
_MAX_INTERACTIVE_TOTAL_DELAY_SECONDS = 60.0
# [해설] 아래 문자열들은 "시도 대체(supersession)" 상황을 모든 클라이언트 표면이 같은 문구로 표시하도록 모아 둔 것.
# [해설] INTERRUPTED_TOOL_OUTPUT: 재시도로 대체된 시도에서 실행되지 못한 tool call에 붙이는 합성 출력(추정: 클라이언트 렌더러가 사용).
# What the product says when an attempt is superseded. Every surface renders
# some part of this set, so the wording lives with the event builders rather
# than being spelled once per client.
INTERRUPTED_TOOL_OUTPUT = "Model response interrupted before tool execution"
"""Synthetic tool output for a call superseded before the tool ran."""
# [해설] RETRY_BOUNDARY_LINE: 실패한 시도의 부분 출력과 재생된 출력 사이 구분선.
RETRY_BOUNDARY_LINE = (
    "--- connection dropped; the output above is incomplete — retrying ---"
)
"""Rule printed between a failed attempt's partial output and its replay."""
RETRY_MARKER_FALLBACK = (
    "Connection dropped; the partial response above is incomplete. Retrying."
)
"""Retry marker for a payload whose attempt counts are unusable."""
TERMINAL_ATTEMPT_MARKER = (
    "The model request failed; the partial response above is incomplete."
)
"""Marker for partial output left behind by an exhausted retry budget."""
# [해설] model_attempt 이벤트의 유효 phase와 call_id 검증 규칙(최대 64자, 영숫자/-/_). 서버는 uuid4().hex(32자)를 쓴다.
_ATTEMPT_PHASES = frozenset({"start", "complete"})
_CALL_ID_MAX_LENGTH = 64
_CALL_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


# [해설] google.api_core 예외인지 MRO의 모듈명으로 판정(패키지 import 없이)하고 정수 code를 꺼낸다.
def _google_api_core_status_code(exc: Exception) -> int | None:
    """Return a numeric Google API Core status without importing its package."""
    if not any(
        base.__module__ == "google.api_core.exceptions" for base in type(exc).__mro__
    ):
        return None
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


# [해설] 한 번의 모델 시도가 LangGraph `messages` 스트림으로 청크를 내보냈는지 추적한다.
# [해설] 결과(has_streamed)는 재시도 이벤트의 output_may_have_started로 전달되어, 클라이언트가 부분 출력을 "불완전"으로 표시하게 한다.
class _MessageStreamTracker:
    """Track whether a model attempt emitted output to the message stream."""

    def __init__(self) -> None:
        self.has_streamed = False
        self._tracked: list[tuple[StreamMessagesHandler, StreamMessagesHandler]] = []

    # [해설] 콜백 매니저 사본에서 StreamMessagesHandler만 추적용 복제본으로 바꿔 끼운다. 교체 대상이 없으면 None.
    # [해설][주의] StreamMessagesHandler는 langgraph private 모듈(langgraph.pregel._messages)에서 import — 버전 변경에 취약.
    def callbacks_with_tracked_messages(
        self, callbacks: BaseCallbackManager
    ) -> BaseCallbackManager | None:
        replacements: dict[int, StreamMessagesHandler] = {}

        # [해설] 추적 핸들러의 출력 콜백: 플래그를 먼저 세운 뒤 원래 핸들러의 stream으로 전달.
        def forward(source: StreamMessagesHandler, chunk: StreamChunk) -> None:
            # Flag first: a writer that raises part-way through has still put
            # the chunk beyond our control, so the client must be told output
            # may have escaped even though the consumer never saw the chunk.
            self.has_streamed = True
            source.stream(chunk)

        # [해설] 같은 원본 핸들러는 한 번만 복제(handlers와 inheritable_handlers에 같은 객체가 있을 수 있음). seen(중복 제거 ID)도 복사.
        def replace(handler: BaseCallbackHandler) -> BaseCallbackHandler:
            if not isinstance(handler, StreamMessagesHandler):
                return handler
            key = id(handler)
            if key not in replacements:
                tracked = type(handler)(
                    lambda chunk, source=handler: forward(source, chunk),
                    handler.subgraphs,
                    parent_ns=handler.parent_ns,
                )
                tracked.seen.update(handler.seen)
                replacements[key] = tracked
                self._tracked.append((handler, tracked))
            return replacements[key]

        # [해설] 얕은 copy 후 handler 리스트만 새로 만든다 → 원본 콜백 매니저는 변경되지 않는다.
        tracked_callbacks = copy(callbacks)
        tracked_callbacks.handlers = [replace(item) for item in callbacks.handlers]
        tracked_callbacks.inheritable_handlers = [
            replace(item) for item in callbacks.inheritable_handlers
        ]
        return tracked_callbacks if replacements else None

    # [해설] 추적 핸들러에서 쌓인 seen ID를 원본에 되돌려, 이후 스트리밍에서 같은 메시지가 중복 전송되지 않게 한다.
    def merge_seen(self) -> None:
        """Merge tracked de-duplication IDs into the original handlers."""
        for source, tracked in self._tracked:
            source.seen.update(tracked.seen)


# [해설] handler 실행 동안 var_child_runnable_config(contextvar)의 callbacks를 추적 버전으로 바꿔치기하는 컨텍스트.
# [해설] 하위 모델 호출은 이 contextvar의 config를 상속하므로 추적 핸들러를 통해 스트리밍하게 된다.
@contextmanager
def _track_message_streams(
    tracker: _MessageStreamTracker,
) -> Iterator[_MessageStreamTracker]:
    # Every early return below leaves `tracker.has_streamed` permanently
    # `False`, which makes `output_may_have_started` permanently `False` and
    # silently disables the supersession marking this module exists to provide:
    # a retried attempt's partial output is then appended with no boundary. Say
    # so, at a level matched to how expected the cause is.
    # [해설][흐름] 1) 실행 중인 LangGraph config가 없으면 추적 불가 → 그대로 진행.
    try:
        from langgraph.config import get_config

        config = get_config()
    except RuntimeError:
        logger.debug(
            "No runnable config in scope; model attempts cannot detect streamed "
            "output, so a retry may append after unmarked partial output",
            exc_info=True,
        )
        yield tracker
        return

    # [해설][흐름] 2) callbacks가 BaseCallbackManager가 아니면 추적 불가(경고).
    callbacks = config.get("callbacks")
    if not isinstance(callbacks, BaseCallbackManager):
        logger.warning(
            "Runnable config carries %s under 'callbacks' rather than a "
            "BaseCallbackManager; retry supersession cannot be detected",
            type(callbacks).__name__,
        )
        yield tracker
        return
    # [해설][흐름] 3) messages 스트림 핸들러가 없으면(해당 stream mode 미사용) 추적 불필요.
    tracked_callbacks = tracker.callbacks_with_tracked_messages(callbacks)
    if tracked_callbacks is None:
        # Routine when nothing consumes the `messages` stream mode; also what a
        # renamed or restructured `StreamMessagesHandler` would look like.
        logger.debug(
            "No message-stream handler attached; retry supersession cannot be "
            "detected for this model call"
        )
        yield tracker
        return

    # [해설][흐름] 4) contextvar 교체 → yield → 복원 + seen 병합.
    tracked_config = config.copy()
    tracked_config["callbacks"] = tracked_callbacks
    token = var_child_runnable_config.set(tracked_config)
    try:
        yield tracker
    finally:
        var_child_runnable_config.reset(token)
        tracker.merge_seen()


# [해설] 제공자별로 다른 위치의 HTTP 상태 코드를 찾아낸다: exc.status_code(OpenAI/Anthropic 계열), google code,
# [해설] exc.response.status_code(httpx), botocore의 response dict ResponseMetadata.HTTPStatusCode, exc.http_status.
# [해설] bool은 int의 서브클래스라 명시적으로 배제한다.
def _extract_status_code(exc: Exception) -> int | None:
    """Return an HTTP status carried by a provider error, if any."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status

    google_status = _google_api_core_status_code(exc)
    if google_status is not None:
        return google_status

    response = getattr(exc, "response", None)
    if response is not None:
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int) and not isinstance(response_status, bool):
            return response_status
        if isinstance(response, dict):
            metadata = response.get("ResponseMetadata")
            if isinstance(metadata, dict):
                response_status = metadata.get("HTTPStatusCode")
                if isinstance(response_status, int) and not isinstance(
                    response_status, bool
                ):
                    return response_status

    http_status = getattr(exc, "http_status", None)
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        return http_status

    return None


# [해설] 오류 응답의 Retry-After 헤더를 초 단위로 해석(숫자 또는 HTTP-date). 60초 상한, 0 이하·비정상 값은 None.
def _retry_after_seconds(exc: Exception) -> float | None:
    """Return a capped `Retry-After` response delay, if present."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        # httpx/requests headers are case-insensitive; a plain dict is not, so
        # fall back to the canonical casing rather than miss the hint.
        raw = headers.get("retry-after")
        if raw is None:
            raw = headers.get("Retry-After")
    except (AttributeError, TypeError):
        logger.debug("Retry-After lookup failed on %s headers", type(exc).__name__)
        return None
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        # Ignoring a provider's pacing hint can escalate a rate limit into a
        # ban, so an unusable value is worth a trace.
        logger.debug("Ignoring unusable Retry-After value %r", raw)
        return None

    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            logger.debug("Ignoring unparseable Retry-After value %r", raw)
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()

    if not math.isfinite(seconds):
        return None
    if seconds <= 0:
        # A zero or already-elapsed hint carries no wait information. Returning
        # it verbatim would skip the sleep entirely and let the whole budget
        # burn in a tight loop, so fall back to the exponential curve.
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


# [해설] 지수 백오프 계산: min(initial * factor^attempt, max_delay) 후 ±jitter. 음수는 0으로 클램프.
def _backoff_delay(
    attempt: int,
    *,
    initial: float,
    factor: float,
    max_delay: float,
    jitter: bool,
) -> float:
    """Return a capped exponential delay, with optional post-cap jitter."""
    delay = min(initial * (factor**attempt), max_delay)
    if jitter and delay > 0:
        jitter_amount = delay * _JITTER_FRACTION
        delay = max(0.0, delay + random.uniform(-jitter_amount, jitter_amount))  # noqa: S311  # backoff jitter, not security-sensitive
    return delay


# [해설] 모듈 기본 파라미터로 _backoff_delay 호출(attempt는 0부터).
def _compute_backoff_delay(attempt: int) -> float:
    """Return the configured backoff after a zero-indexed attempt."""
    return _backoff_delay(
        attempt,
        initial=_INITIAL_DELAY_SECONDS,
        factor=_BACKOFF_FACTOR,
        max_delay=_MAX_DELAY_SECONDS,
        jitter=True,
    )


# [해설] Retry-After가 있으면 그것을, 없으면 로컬 지수 백오프를 사용.
def _retry_delay_seconds(attempt: int, exc: Exception) -> float:
    """Return a provider-directed or local backoff delay for one failure."""
    retry_after = _retry_after_seconds(exc)
    return retry_after if retry_after is not None else _compute_backoff_delay(attempt)


# [해설] 모델 객체에 스탬프된 MODEL_RETRIES_ATTR(0 이상 int)를 읽고, 없거나 잘못되면 fallback.
def _model_max_retries(model: object, fallback: int) -> int:
    """Return valid retry metadata attached to `model`, or `fallback`."""
    raw_retries = getattr(model, MODEL_RETRIES_ATTR, None)
    if (
        isinstance(raw_retries, int)
        and not isinstance(raw_retries, bool)
        and raw_retries >= 0
    ):
        return raw_retries
    return fallback


# [해설] 예외 MRO의 각 클래스에 대해 (최상위 패키지, 클래스명)이 _TRANSIENT_SDK_EXC_NAMES에 있는지 확인.
def _is_transient_sdk_error(exc: Exception) -> bool:
    """Return whether any base class is a known transient provider-SDK error."""
    return any(
        (base.__module__.partition(".")[0], base.__name__) in _TRANSIENT_SDK_EXC_NAMES
        for base in type(exc).__mro__
    )


# [해설] httpx(Timeout/Network/RemoteProtocol), httpcore(ReadError/RemoteProtocolError), aiohttp 전송 길이 오류를
# [해설] 일시적 전송 실패로 판정. httpx는 없을 수도 있어 지연 import.
def _is_http_transport_error(exc: BaseException) -> bool:
    """Return whether `exc` is a transient HTTP response transport failure."""
    # Optional dependency: httpx ships with the HTTP-based providers but keep the
    # import lazy so classification never forces it at startup.
    httpx_transient: tuple[type[BaseException], ...] = ()
    try:
        import httpx
    except ImportError:
        # Raised for a genuinely absent httpx and for a broken sub-import
        # (h11, certifi). The latter silently disables the classification this
        # module exists for, so leave a trace.
        logger.debug(
            "httpx unavailable; its transport errors will not be classified "
            "as retryable",
            exc_info=True,
        )
    else:
        # Deliberately narrower than `TransportError`, whose subclasses include
        # permanent faults: `UnsupportedProtocol` (a mistyped base_url scheme),
        # `LocalProtocolError` (a malformed request), and `ProxyError` (a
        # misconfigured proxy). Retrying those burns the whole budget on an
        # error that was knowable on the first attempt.
        httpx_transient = (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        )

    if isinstance(exc, httpx_transient):
        return True

    error_type = type(exc)
    if error_type.__module__.startswith("httpcore") and error_type.__name__ in {
        "ReadError",
        "RemoteProtocolError",
    }:
        return True
    return (
        error_type.__module__ == "aiohttp.http_exceptions"
        and error_type.__name__ == "TransferEncodingError"
        and "Not enough data to satisfy transfer length header" in str(exc)
    )


# [해설] 예외 "하나"의 직접 신호로 재시도 여부를 판정한다. True/False는 확정, None은 "하위(그룹 멤버·원인 체인)를 더 보라".
# [해설][흐름] 판정 순서: ModelError.is_retryable → httpx 전송 오류 → HTTP 상태 코드 → SDK 클래스명 → (raised일 때만) 표준 Timeout/ConnectionError.
def _direct_model_error_retryability(
    exc: BaseException, *, raised: bool
) -> bool | None:
    """Classify one exception before inspecting any wrapped failures.

    Args:
        exc: The exception to classify.
        raised: Whether `exc` is the exception the model call actually raised,
            rather than one reached through a group member or a cause chain.

    Returns:
        Whether the exception is retryable, or `None` when it has no direct
        signal and its group members or chain should be inspected.
    """
    if isinstance(exc, ModelError):
        return exc.is_retryable

    if _is_http_transport_error(exc):
        return True

    if not isinstance(exc, Exception):
        return None

    # A status-bearing provider error is decided solely by its code: retry only
    # 408/409/429/5xx, and never fall through to broader heuristics for a 4xx
    # that would otherwise be misclassified as a bare connection error.
    status = _extract_status_code(exc)
    if status is not None:
        return status in _RETRYABLE_STATUS_CODES or (
            _HTTP_SERVER_ERROR_FLOOR <= status < _HTTP_SERVER_ERROR_CEILING
        )

    if _is_transient_sdk_error(exc):
        return True

    # Stdlib transport faults raised directly (rare, but cheap to cover). This
    # heuristic alone is deliberately confined to the raised exception: Python
    # sets `__context__` on anything raised inside an `except` block, so
    # honouring it here would make a permanent failure that merely surfaced
    # while handling a timeout look transient and burn the whole budget on it.
    #
    # The checks above are not confined that way, and the asymmetry is chosen,
    # not an oversight. `TimeoutError`/`ConnectionError` are broad -- every
    # `asyncio.wait_for` deadline and every socket fault in the process is one
    # -- whereas a package-qualified SDK class or an httpx transport error is
    # narrow enough that finding one in the context chain really does mean the
    # call died in transport and an SDK re-raised inside its `except`. That
    # wrap-and-reraise shape is the common one, so those stay trusted through
    # `__context__` (see `test_predicate_retries_transport_error_in_context_chain`).
    if raised and isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return None


# [해설] 예외 트리 전체(ExceptionGroup 멤버, __cause__/__context__)를 DFS로 훑어 일시적 실패가 하나라도 있으면 True.
# [해설] 호출자: _retry_call/_aretry_call, _log_give_up.
def _is_retryable_model_error(exc: Exception) -> bool:
    """Return whether a model error tree contains a transient failure.

    Descends into `BaseExceptionGroup` members and the cause chain, so a
    transport fault wrapped by an async task group is still found. An exception
    that classifies either way decides for its own branch and is not descended
    through, which keeps a definite `ModelError.is_retryable` verdict (an
    authentication failure, say) authoritative over whatever it happens to
    wrap.

    The stock retry check stops at the raised exception, so it would miss a
    `httpx.ConnectError` wrapped in an `ExceptionGroup`; this walk catches it.
    """
    # [해설] (예외, 직접 raise된 것인지) 스택. id 기반 seen으로 순환 체인을 방지.
    pending: list[tuple[BaseException, bool]] = [(exc, True)]
    seen: set[int] = set()
    while pending:
        current, raised = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        retryable = _direct_model_error_retryability(current, raised=raised)
        if retryable is not None:
            # [해설] 확정 판정: True면 즉시 반환, False면 이 가지는 더 내려가지 않는다(예: 인증 실패 ModelError가 감싼 것 무시).
            if retryable:
                return True
            continue
        if isinstance(current, BaseExceptionGroup):
            pending.extend((member, False) for member in current.exceptions)
        # [해설] 원인 체인은 __cause__가 있으면 그것만, 없으면 __context__를 따라간다.
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append((cause, False))
    return False


# [해설] 재시도 대기 중 표시할 짧은 상태 문구. build_retry_event의 message와 retry_status_from_event가 사용.
def format_retry_status(attempt: int, max_retries: int) -> str:
    """Return the concise user-facing status shown during a retry backoff.

    Carries no trailing ellipsis: the TUI spinner appends its own. Names no
    cause either, because a retry may be a rate limit or a 5xx rather than a
    dropped connection.

    Args:
        attempt: The 1-indexed retry number about to be attempted.
        max_retries: The configured maximum retry count.

    Returns:
        A short status line, e.g. `"Retrying model request 1/5"`.
    """
    return f"Retrying model request {attempt}/{max_retries}"


# [해설] 재시도 중단 사유를 로그 레벨로 구분: 비일시적(info), 예산 소진(error), 예산 0(warning).
def _log_give_up(exc: Exception, attempts: int, max_retries: int) -> None:
    """Log why the retry loop stopped before re-raising."""
    if not _is_retryable_model_error(exc):
        # `info`, not `debug`: a fault in this module's own instrumentation
        # (a `StreamMessagesHandler` signature change, say) surfaces here
        # classified as non-transient, and would otherwise reach the user as an
        # unexplained provider error with no traceback at default log levels.
        logger.info(
            "Model call failed with a non-transient %s; not retrying",
            type(exc).__name__,
            exc_info=exc,
        )
    elif max_retries:
        logger.error(
            "Model call failed after %d attempts (retry budget %d exhausted): %s",
            attempts,
            max_retries,
            type(exc).__name__,
            exc_info=exc,
        )
    else:
        logger.warning(
            "Model call failed with a transient %s but retries are disabled "
            "(retry budget 0)",
            type(exc).__name__,
            exc_info=exc,
        )


# [해설] 동기 공통 재시도 루프. 총 시도 횟수 = max_retries + 1.
# [해설] 호출자: retry_model_call(보조 모델), CodeModelRetryMiddleware.wrap_model_call.
def _retry_call[ResultT](
    call: Callable[[], ResultT],
    *,
    max_retries: int,
    on_retry: Callable[[int, int, Exception], None],
    retry_guard: Callable[[Exception, int, float], bool] | None = None,
) -> ResultT:
    """Run one synchronous call under the shared retry policy.

    Returns:
        The successful call result.

    Raises:
        GraphBubbleUp: If the graph signals control flow.
        RuntimeError: If the retry loop exits unexpectedly.
    """
    # [해설][흐름] 1) 시도 실행. 성공하면 즉시 반환.
    for attempt in range(max_retries + 1):
        try:
            return call()
        # [해설] GraphBubbleUp(interrupt, Command 등 LangGraph 제어 흐름)은 오류가 아니므로 절대 재시도하지 않는다.
        except GraphBubbleUp:
            raise
        except Exception as exc:  # classified by _is_retryable_model_error
            # Settle eligibility before consulting the guard. A guard that ran
            # first would blame the delay budget for an error that was never
            # going to be retried, and would skip the exhausted-budget log
            # entirely.
            # [해설][흐름] 2) 재시도 자격 판정: 비일시적이거나 마지막 시도였으면 로그 후 원래 예외 재발생.
            if not _is_retryable_model_error(exc) or attempt >= max_retries:
                _log_give_up(exc, attempt + 1, max_retries)
                # Re-raise, don't convert to an `AIMessage`: a dead provider
                # should end the turn as an error, not as a reply the model
                # never made.
                raise
            # Drawn once: the backoff carries jitter, so re-deriving it for the
            # guard would authorise one delay and then sleep a different one.
            # [해설][흐름] 3) 지연 한 번 계산(jitter 때문에 guard와 sleep이 같은 값을 쓰도록).
            delay = _retry_delay_seconds(attempt, exc)
            # [해설][흐름] 4) guard(누적 지연 예산)가 거부하면 원래 예외를 그대로 올린다.
            if retry_guard is not None and not retry_guard(exc, attempt + 1, delay):
                raise
            # [해설][흐름] 5) 재시도 알림(on_retry: 로그·스트림 이벤트) 후 sleep.
            on_retry(attempt + 1, max_retries, exc)
            if delay:
                time.sleep(delay)
    msg = "Unexpected: retry loop completed without returning"
    raise RuntimeError(msg)


# [해설] _retry_call의 비동기 버전(asyncio.sleep 사용). 호출자: aretry_model_call, CodeModelRetryMiddleware.awrap_model_call.
async def _aretry_call[ResultT](
    call: Callable[[], Awaitable[ResultT]],
    *,
    max_retries: int,
    on_retry: Callable[[int, int, Exception], None],
    retry_guard: Callable[[Exception, int, float], bool] | None = None,
) -> ResultT:
    """Run one asynchronous call under the shared retry policy.

    Returns:
        The successful call result.

    Raises:
        GraphBubbleUp: If the graph signals control flow.
        RuntimeError: If the retry loop exits unexpectedly.
    """
    import asyncio

    for attempt in range(max_retries + 1):
        try:
            return await call()
        except GraphBubbleUp:
            raise
        except Exception as exc:  # classified by _is_retryable_model_error
            # Settle eligibility before consulting the guard. A guard that ran
            # first would blame the delay budget for an error that was never
            # going to be retried, and would skip the exhausted-budget log
            # entirely.
            if not _is_retryable_model_error(exc) or attempt >= max_retries:
                _log_give_up(exc, attempt + 1, max_retries)
                # Always re-raise (see `_retry_call`).
                raise
            # Drawn once: the backoff carries jitter, so re-deriving it for the
            # guard would authorise one delay and then sleep a different one.
            delay = _retry_delay_seconds(attempt, exc)
            if retry_guard is not None and not retry_guard(exc, attempt + 1, delay):
                raise
            on_retry(attempt + 1, max_retries, exc)
            if delay:
                await asyncio.sleep(delay)
    msg = "Unexpected: retry loop completed without returning"
    raise RuntimeError(msg)


# [해설] 보조 모델 재시도용 on_retry: 매 시도의 예외 타입·상태 코드를 경고 로그로 남긴다(스트림 이벤트 없음).
def _log_auxiliary_retry(attempt: int, max_retries: int, exc: Exception) -> None:
    """Log one auxiliary-model retry.

    Only the final exception survives to be re-raised, so an attempt logged
    without its cause is unrecoverable: five 429s and a 429 followed by four
    connection resets are indistinguishable after the fact.
    """
    logger.warning(
        "Auxiliary model call failed with %s (status %s); retrying %d/%d",
        type(exc).__name__,
        _extract_status_code(exc),
        attempt,
        max_retries,
        exc_info=exc,
    )


# [해설] 보조 모델의 재시도 예산. 스탬프가 없으면 0이 아니라 DEFAULT_MODEL_RETRIES로 폴백하고 경고.
def _auxiliary_max_retries(model: object) -> int:
    """Return the auxiliary retry budget for `model`, defaulting when unstamped.

    A model that never passed through `create_model` carries no
    `MODEL_RETRIES_ATTR`. Defaulting that case to zero would make every
    auxiliary wrapper a silent passthrough -- and because
    `_install_summary_model_retries` replaces LangChain's unconditional
    three-attempt `with_retry`, compaction summarization would quietly drop to
    a single attempt. Fall back to the normal budget and say so, since a
    retry-less summarizer is invisible at runtime.

    Returns:
        The attached budget, or `DEFAULT_MODEL_RETRIES` when there is none.
    """
    resolved = _model_max_retries(model, -1)
    if resolved >= 0:
        return resolved
    logger.warning(
        "Model %s carries no dcode retry metadata; auxiliary calls fall back "
        "to %d retries and its own SDK retry loop may still be active",
        type(model).__name__,
        DEFAULT_MODEL_RETRIES,
    )
    return DEFAULT_MODEL_RETRIES


# [해설] 누적 sleep 한도를 지키는 guard 클로저를 만든다. spent는 guard 인스턴스마다 독립이므로,
# [해설] 호출자가 모델 호출 1회마다 새 guard를 만들어 "호출 단위" 예산이 된다. None이면 무제한.
def _delay_budget_guard(
    max_total_delay: float | None,
    *,
    label: str = "Auxiliary model",
) -> Callable[[Exception, int, float], bool]:
    """Build a guard that keeps total retry sleep within `max_total_delay`.

    Callers that run under an enclosing deadline cannot afford an honoured
    `Retry-After` of up to `_MAX_RETRY_AFTER_SECONDS`: the sleep outlives the
    deadline, the task is cancelled mid-wait, and the real provider error is
    replaced by an unrelated `TimeoutError`. Refusing the retry surfaces the
    genuine cause instead, and avoids retrying a rate limit early.

    The budget is cumulative, not per-delay. Capping each wait in isolation
    bounds nothing: five waits that each clear a 5s ceiling still spend 25s,
    which is exactly how a 20s classifier deadline was overrun by the retries
    meant to fit inside it.

    Args:
        max_total_delay: Cumulative sleep ceiling, or `None` to honour the full
            policy.
        label: Sentence-leading subject for the refusal log, so an interactive
            stall reads differently from an auxiliary one.

    Returns:
        A `retry_guard` callable for the shared retry loops.
    """
    spent = 0.0

    def guard(exc: Exception, attempt: int, delay: float) -> bool:  # noqa: ARG001
        nonlocal spent
        if max_total_delay is None:
            return True
        if spent + delay <= max_total_delay:
            spent += delay
            return True
        logger.warning(
            "%s retries would wait %.1fs past the total delay budget of "
            "%.1fs; surfacing %s instead",
            label,
            spent + delay - max_total_delay,
            max_total_delay,
            type(exc).__name__,
        )
        return False

    return guard


# [해설] 스트리밍하지 않는 보조 모델 호출(동기)을 재시도 정책으로 실행. 호출자: offload_middleware.py(요약 모델 등).
# [해설] call은 매 시도마다 새로 호출할 수 있는 callable이어야 한다.
def retry_model_call[ResultT](
    model: object,
    call: Callable[[], ResultT],
    *,
    max_total_delay: float | None = None,
) -> ResultT:
    """Run a non-streaming auxiliary model call with its configured retry budget.

    Args:
        model: Model carrying dcode retry metadata when dcode owns its SDK retries.
        call: Fresh invocation callable to run for each attempt.
        max_total_delay: Total time this caller can spend sleeping between
            attempts, for callers running under an enclosing deadline. `None`
            honours the full policy.

    Returns:
        The successful call result.
    """
    return _retry_call(
        call,
        max_retries=_auxiliary_max_retries(model),
        on_retry=_log_auxiliary_retry,
        retry_guard=_delay_budget_guard(max_total_delay),
    )


# [해설] 비동기 보조 모델 호출용. 호출자: auto_mode.py(Auto 승인 분류기, 마감시간 안에 맞추려 max_total_delay 지정).
async def aretry_model_call[ResultT](
    model: object,
    call: Callable[[], Awaitable[ResultT]],
    *,
    max_total_delay: float | None = None,
) -> ResultT:
    """Run an asynchronous auxiliary model call with its configured retry budget.

    Args:
        model: Model carrying dcode retry metadata when dcode owns its SDK retries.
        call: Fresh async invocation callable to run for each attempt.
        max_total_delay: Total time this caller can spend sleeping between
            attempts, for callers running under an enclosing deadline. `None`
            honours the full policy.

    Returns:
        The successful call result.
    """
    return await _aretry_call(
        call,
        max_retries=_auxiliary_max_retries(model),
        on_retry=_log_auxiliary_retry,
        retry_guard=_delay_budget_guard(max_total_delay),
    )


# [해설] 신뢰할 수 없는 model_retry 이벤트에서 (attempt, max_retries)를 1 ≤ attempt ≤ max_retries 범위로 검증.
def retry_counts_from_event(
    event: Mapping[Any, object],
) -> tuple[int, int] | None:
    """Validate the attempt counters of an untrusted `model_retry` payload.

    Every surface that renders a retry needs the same two numbers under the
    same range, so the check lives once with the producer rather than being
    re-derived per surface with drifting strictness.

    Args:
        event: Custom-stream payload, not trusted to hold sane numbers.

    Returns:
        The `(attempt, max_retries)` pair, or `None` when either is unusable.
    """
    attempt = event.get("attempt")
    max_retries = event.get("max_retries")
    if (
        isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and isinstance(max_retries, int)
        and not isinstance(max_retries, bool)
        and 1 <= attempt <= max_retries
    ):
        return (attempt, max_retries)
    return None


# [해설] 클라이언트(TUI 스피너·headless)가 표시할 상태 문구를 이벤트에서 안전하게 재구성.
def retry_status_from_event(event: Mapping[Any, object]) -> str:
    """Return retry status text for an untrusted `model_retry` payload.

    Both the TUI and the headless client render this status line, so its
    validation lives with the producer rather than being written twice with
    different strictness.

    Args:
        event: Custom-stream payload, not trusted to hold sane numbers.

    Returns:
        The validated status line, or a cause-free fallback for malformed data.
    """
    counts = retry_counts_from_event(event)
    if counts is None:
        logger.warning("Ignoring malformed model_retry payload: %r", dict(event))
        return _RETRY_STATUS_FALLBACK
    return format_retry_status(*counts)


# [해설] 채팅 영역에 넣을 재시도 마커. 이벤트의 message 문자열(마크업 위험)은 쓰지 않고 숫자만으로 재생성.
def retry_marker_from_event(event: Mapping[Any, object]) -> str:
    """Build the in-chat retry marker from validated numeric fields only.

    The event's own `message` field is untrusted render text, so the marker is
    re-derived from `attempt`/`max_retries` and never parses markup out of it.

    Always returns a marker. By the time this is called the partial reply has
    already been finalized and detached from the stream, so returning nothing
    would leave a truncated answer in the chat that reads as a complete one,
    followed by a second full answer, with nothing saying the first was cut off.
    Unusable numbers cost the "1/5" suffix, not the marker -- the same way
    `retry_status_from_event` degrades to a cause-free status line.

    Args:
        event: Custom-stream payload, not trusted to hold sane numbers.

    Returns:
        The marker line, counted when the numbers allow it.
    """
    counts = retry_counts_from_event(event)
    if counts is None:
        logger.warning(
            "Unusable retry counts in model_retry payload; marking the "
            "superseded reply without them"
        )
        return RETRY_MARKER_FALLBACK
    attempt, max_retries = counts
    return (
        "Connection dropped; the partial response above is incomplete. "
        f"Retrying {attempt}/{max_retries}."
    )


# [해설] call_id가 없는 구버전 서버 이벤트의 중복 판별 키로 attempt 번호를 사용.
def legacy_retry_index(event: Mapping[Any, object]) -> int:
    """Identity fallback for a `model_retry` payload that names no attempt.

    A producer that predates attempt lifecycle events carries no `call_id`, so
    a consumer cannot tell a second retry of one call from a redelivery of the
    same event by correlation. The retry counter it does carry is enough: two
    retries of one call always differ, while a redelivery does not.

    Args:
        event: Custom-stream payload, not trusted to hold sane numbers.

    Returns:
        The payload's retry counter when it is a usable int, else `-1`.
    """
    attempt = event.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        return attempt
    return -1


# [해설] 서버→클라이언트 커스텀 스트림 payload 생성: {"type": "model_retry", attempt, max_retries, message,
# [해설] (선택) call_id, failed_attempt, output_may_have_started}. 호출자: CodeModelRetryMiddleware._emit_retry_status.
def build_retry_event(
    attempt: int,
    max_retries: int,
    *,
    call_id: str | None = None,
    failed_attempt: int | None = None,
    output_may_have_started: bool = False,
) -> dict[str, object]:
    """Build the custom-stream payload announcing a model retry.

    Args:
        attempt: The 1-indexed retry number about to be attempted.
        max_retries: The configured maximum retry count.
        call_id: Opaque ID correlating every attempt of one model call. Omit
            for producers that predate attempt lifecycle events.
        failed_attempt: The 0-indexed attempt being superseded. Required to
            carry `call_id`.
        output_may_have_started: Whether the superseded attempt may have put
            message output beyond server control. Conservative by design: the
            tracker flags before forwarding a chunk.

    Returns:
        A stream-writer payload consumed by the client renderers.

    Raises:
        ValueError: If only one of `call_id` and `failed_attempt` is given.
    """
    # [해설] 상관 필드는 둘 다 있거나 둘 다 없어야 한다(구버전 호환 형태와 신버전 형태만 허용).
    if (call_id is None) != (failed_attempt is None):
        msg = "call_id and failed_attempt must be provided together"
        raise ValueError(msg)
    event: dict[str, object] = {
        "type": "model_retry",
        "attempt": attempt,
        "max_retries": max_retries,
        "message": format_retry_status(attempt, max_retries),
    }
    if call_id is not None:
        event["call_id"] = call_id
        event["failed_attempt"] = failed_attempt
        event["output_may_have_started"] = output_may_have_started
    return event


# [해설] 시도 경계 이벤트 {"type": "model_attempt", phase(start/complete), call_id, attempt(0부터)}.
def build_attempt_event(call_id: str, attempt: int, *, phase: str) -> dict[str, object]:
    """Build the custom-stream payload marking one model attempt boundary.

    Args:
        call_id: Opaque ID shared by every attempt of one model call.
        attempt: The 0-indexed attempt whose boundary is marked.
        phase: `"start"` before the handler runs, `"complete"` after it
            returns successfully.

    Returns:
        A stream-writer payload consumed by the client renderers.

    Raises:
        ValueError: If `phase` is not a known lifecycle phase.
    """
    if phase not in _ATTEMPT_PHASES:
        msg = f"phase must be one of {sorted(_ATTEMPT_PHASES)}, got {phase!r}"
        raise ValueError(msg)
    return {
        "type": "model_attempt",
        "phase": phase,
        "call_id": call_id,
        "attempt": attempt,
    }


# [해설] call_id를 길이·문자 집합으로 검증. 원격 이벤트 데이터가 렌더링/키로 쓰이기 전에 걸러낸다.
def _validated_call_id(value: object) -> str | None:
    """Return `value` as a correlation ID, or `None` when it is untrusted."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _CALL_ID_MAX_LENGTH
        or any(char not in _CALL_ID_CHARS for char in value)
    ):
        return None
    return value


# [해설] model_retry 이벤트의 상관 필드 검증. 셋 다 없으면 구버전 이벤트(None), 일부만/형식 오류면 경고 후 None.
def model_retry_from_event(event: Mapping[Any, object]) -> dict[str, object] | None:
    """Return validated retry-correlation fields from an untrusted event."""
    call_id = _validated_call_id(event.get("call_id"))
    failed_attempt = event.get("failed_attempt")
    visible = event.get("output_may_have_started")
    if call_id is None and failed_attempt is None and visible is None:
        return None
    if (
        call_id is None
        or not isinstance(failed_attempt, int)
        or isinstance(failed_attempt, bool)
        or failed_attempt < 0
        or not isinstance(visible, bool)
    ):
        logger.warning("Ignoring malformed model_retry correlation fields")
        return None
    return {
        "call_id": call_id,
        "failed_attempt": failed_attempt,
        "output_may_have_started": visible,
    }


# [해설] model_attempt 이벤트 검증. 알 수 없는 phase는 버려 신버전 서버가 구버전 클라이언트를 깨지 않게 한다.
def model_attempt_from_event(
    event: Mapping[Any, object],
) -> dict[str, object] | None:
    """Return a validated `model_attempt` payload from an untrusted event.

    Remote and local consumers receive lifecycle events from the same custom
    stream as provider-shaped data, so every field is structurally validated
    before use. Unknown fields are ignored and unknown phases are dropped, so
    a newer server never breaks an older client.

    Args:
        event: Custom-stream payload, not trusted to hold sane values.

    Returns:
        A dict with `type`, `phase`, `call_id`, and `attempt`, or `None` for
        malformed data.
    """
    phase = event.get("phase")
    call_id = _validated_call_id(event.get("call_id"))
    attempt = event.get("attempt")
    if (
        not isinstance(phase, str)
        or phase not in _ATTEMPT_PHASES
        or call_id is None
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 0
    ):
        logger.warning("Ignoring malformed model_attempt lifecycle fields")
        return None
    return {
        "type": "model_attempt",
        "phase": phase,
        "call_id": call_id,
        "attempt": attempt,
    }


# [해설] 메인 에이전트 모델 노드용 재시도 미들웨어. agent.py가 `CodeModelRetryMiddleware(max_retries=model_retries)`로
# [해설] 미들웨어 스택에 추가하고, goal_rubric.py의 루브릭 평가 에이전트도 사용한다.
# [해설][흐름] 호출 1회 = call_id 1개. 시도마다 model_attempt start/complete, 재시도 시 model_retry 이벤트를 스트림에 쓴다.
class CodeModelRetryMiddleware(AgentMiddleware):
    """Retry transient model-node failures without replaying completed tools.

    Emits `model_attempt` start/complete lifecycle events around every handler
    invocation, correlated by one `call_id` per model call, so clients can
    reconcile output from a superseded attempt when a transient failure is
    retried after streaming began.
    """

    # [해설] 트레이싱 시 이 미들웨어 입력 페이로드는 생략(omit_payload) — 모델 요청 전체가 트레이스에 중복 기록되는 것을 피함(추정).
    trace_policy = TracePolicy(process_inputs=omit_payload)

    # [해설] 시작 시 예산(fallback)과 스트림 가시성 플래그를 검증·저장. 실제 예산은 요청의 모델 스탬프가 우선한다.
    def __init__(
        self,
        *,
        max_retries: int = DEFAULT_MODEL_RETRIES,
        stream_output_is_visible: bool = True,
    ) -> None:
        """Initialize the middleware with the resolved retry count.

        Args:
            max_retries: Startup fallback for retry attempts after the initial
                call. `0` disables retries unless the request's runtime-selected
                model carries a different provider-specific budget.
            stream_output_is_visible: Whether message-stream chunks emitted by
                this model reach a user-visible consumer; it decides the
                `output_may_have_started` supersession flag on retry events.
                Keep `True` unless the entire nested stream is filtered before
                rendering.

        Raises:
            TypeError: If `max_retries` or `stream_output_is_visible` has the
                wrong type.
            ValueError: If `max_retries` is negative.
        """
        # `True >= 0` passes and `range(True + 1)` runs two attempts, so an
        # unchecked bool reads as a budget of one retry.
        if isinstance(max_retries, bool):
            msg = f"max_retries must be an int, got {type(max_retries).__name__}"
            raise TypeError(msg)
        if max_retries < 0:
            msg = "max_retries must be >= 0"
            raise ValueError(msg)
        if not isinstance(stream_output_is_visible, bool):
            msg = (
                "stream_output_is_visible must be a bool, got "
                f"{type(stream_output_is_visible).__name__}"
            )
            raise TypeError(msg)
        self.max_retries = max_retries
        self.stream_output_is_visible = stream_output_is_visible

    # [해설] request.runtime.stream_writer(LangGraph custom 스트림)로 이벤트 전송. writer가 없으면 무시,
    # [해설] writer 오류는 실행을 실패시키지 않고 경고만(단 GraphBubbleUp은 전파).
    @staticmethod
    def _emit_stream_event(request: ModelRequest, event: dict[str, object]) -> None:
        writer = getattr(getattr(request, "runtime", None), "stream_writer", None)
        if writer is None:
            return
        try:
            writer(event)
        except GraphBubbleUp:
            # LangGraph control flow must not be mistaken for a writer fault.
            raise
        except Exception:
            # These events are the only signal that a pause is a retry and the
            # only correlation a client has between chunks and attempts, so
            # losing one must be visible in the logs without failing the run.
            logger.warning(
                "Failed to emit %s stream event", event["type"], exc_info=True
            )

    # [해설] 재시도 직전: model_retry 이벤트 생성(failed_attempt = attempt-1) + 원인 포함 경고 로그 + 스트림 전송.
    # [해설] output_may_have_started는 실패한 시도가 청크를 흘렸고 그 출력이 사용자에게 보이는 경우에만 True.
    def _emit_retry_status(
        self,
        request: ModelRequest,
        attempt: int,
        max_retries: int,
        exc: Exception,
        call_id: str,
        has_streamed: bool,
    ) -> None:
        event = build_retry_event(
            attempt,
            max_retries,
            call_id=call_id,
            failed_attempt=attempt - 1,
            output_may_have_started=has_streamed and self.stream_output_is_visible,
        )
        # The user-facing event stays deliberately vague, but the log must name
        # the cause: only the last exception is re-raised, so an attempt logged
        # without its type and status leaves no way to tell a run of rate
        # limits from a run of connection resets.
        logger.warning(
            "Model call failed with %s (status %s); %s",
            type(exc).__name__,
            _extract_status_code(exc),
            event["message"],
            exc_info=exc,
        )
        self._emit_stream_event(request, event)

    # [해설] 요청의 모델 객체 스탬프를 매 요청 읽는다 → `/model` 전환 후에도 새 모델의 예산이 적용된다.
    def _request_max_retries(self, request: ModelRequest) -> int:
        # A `/model` switch stamps its own budget on the constructed model;
        # that wins over the startup fallback, so read it per request.
        return _model_max_retries(getattr(request, "model", None), self.max_retries)

    # [해설] 동기 모델 노드 래퍼. 스트리밍이 이미 시작된 뒤에도 재시도한다(클라이언트가 이벤트로 부분 출력을 정리).
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Retry a synchronous model-node call, even after streamed output.

        Returns:
            The successful model response.
        """
        # [해설][흐름] 1) 이번 호출의 예산, 추적기, call_id, 현재 시도 번호(0) 준비.
        max_retries = self._request_max_retries(request)
        stream_tracker = _MessageStreamTracker()
        call_id = uuid.uuid4().hex
        current_attempt = 0

        # [해설][흐름] 2) 시도 함수: 새 추적기 → start 이벤트 → 스트림 추적하며 handler 실행 → complete 이벤트.
        # [해설] handler가 예외를 던지면 complete 이벤트는 나가지 않는다(클라이언트는 이어지는 model_retry로 대체를 인지).
        def call() -> ModelResponse:
            nonlocal stream_tracker
            stream_tracker = _MessageStreamTracker()
            self._emit_stream_event(
                request, build_attempt_event(call_id, current_attempt, phase="start")
            )
            with _track_message_streams(stream_tracker):
                result = handler(request)
            self._emit_stream_event(
                request,
                build_attempt_event(call_id, current_attempt, phase="complete"),
            )
            return result

        # [해설][흐름] 3) 재시도 콜백: 실패한 시도의 has_streamed로 이벤트를 보낸 뒤 current_attempt를 다음 번호로 올린다.
        def on_retry(attempt: int, budget: int, exc: Exception) -> None:
            nonlocal current_attempt
            self._emit_retry_status(
                request, attempt, budget, exc, call_id, stream_tracker.has_streamed
            )
            current_attempt = attempt

        # [해설][흐름] 4) 공통 루프 실행. 대화형 누적 지연 한도 60초 guard로 사용자가 스피너 뒤에서 오래 멈추지 않게 한다.
        return _retry_call(
            call,
            max_retries=max_retries,
            on_retry=on_retry,
            retry_guard=_delay_budget_guard(
                _MAX_INTERACTIVE_TOTAL_DELAY_SECONDS, label="Interactive model"
            ),
        )

    # [해설] wrap_model_call의 비동기 버전(서버 그래프의 기본 실행 경로).
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Retry an asynchronous model-node call, even after streamed output.

        Returns:
            The successful model response.
        """
        max_retries = self._request_max_retries(request)
        stream_tracker = _MessageStreamTracker()
        call_id = uuid.uuid4().hex
        current_attempt = 0

        async def call() -> ModelResponse:
            nonlocal stream_tracker
            stream_tracker = _MessageStreamTracker()
            self._emit_stream_event(
                request, build_attempt_event(call_id, current_attempt, phase="start")
            )
            with _track_message_streams(stream_tracker):
                result = await handler(request)
            self._emit_stream_event(
                request,
                build_attempt_event(call_id, current_attempt, phase="complete"),
            )
            return result

        def on_retry(attempt: int, budget: int, exc: Exception) -> None:
            nonlocal current_attempt
            self._emit_retry_status(
                request, attempt, budget, exc, call_id, stream_tracker.has_streamed
            )
            current_attempt = attempt

        return await _aretry_call(
            call,
            max_retries=max_retries,
            on_retry=on_retry,
            retry_guard=_delay_budget_guard(
                _MAX_INTERACTIVE_TOTAL_DELAY_SECONDS, label="Interactive model"
            ),
        )
