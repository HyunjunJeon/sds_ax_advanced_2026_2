"""Custom tools for the agent."""

# [해설] 이 모듈의 역할: dcode가 SDK 기본 도구(파일·셸·task) 외에 에이전트에 추가로 주는 "소비자 도구"를 정의한다.
# [해설] - `fetch_url`: URL을 가져와 markdown으로 변환 (SSRF 가드 + DNS pinning + 리다이렉트마다 재검증)
# [해설] - `web_search` / `create_web_search_tool`: Tavily 기반 웹 검색 (전역 키 버전 / 워크스페이스 키 바인딩 버전)
# [해설] - `get_current_thread_id`: 현재 LangGraph `configurable.thread_id` 반환
# [해설] 실행 위치: 서버 프로세스(에이전트 그래프 안의 ToolNode)에서 실제로 실행된다. `--acp`(in-process)면 같은 프로세스.
# [해설] 호출자: `server_graph._build_tools`가 `[fetch_url, get_current_thread_id, (web_search), *MCP]` 목록을 만들어
# [해설] `agent.create_cli_agent(tools=...)`로 넘긴다. `main.py`, `tool_catalog.py`도 도구 목록 표시용으로 import한다.
# [해설] 설계 포인트: 모델이 넘기는 URL은 prompt injection으로 오염될 수 있으므로 사설/루프백/IMDS 주소를 차단한다.
# [해설] 오류는 예외 대신 `{"error": ...}` dict로 돌려 모델이 읽고 대응하게 한다(도구 호출이 그래프를 죽이지 않게).
# [해설] 관련 분석 문서: analysis/02-agent-assembly-sdk-core.md (코드 지도의 tools.py 항목), analysis/04-approval-hitl-security.md
# [해설] 관련 공식 문서: docs_official/sdk/tools.md, docs_official/code/ 의 도구/설정 문서
from __future__ import annotations

import contextlib
import functools
import ipaddress
import logging
import socket
import threading
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urljoin, urlparse

from langchain_core.tools import tool
from langgraph.config import get_config
from pydantic import Field

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from langchain_core.tools import BaseTool
    from tavily import TavilyClient

logger = logging.getLogger(__name__)

# [해설] Tavily 클라이언트 lazy 싱글턴 캐시. `_UNSET` sentinel로 "아직 초기화 안 함"과 "키 없음(None)"을 구분한다.
# [해설] `_get_tavily_client`만 이 전역을 읽고 쓴다.
_UNSET = object()
_tavily_client: TavilyClient | object | None = _UNSET

# [해설][설계] 도구 metadata에 이 키 + 모듈 전용 객체 `_WEB_SEARCH_TOKEN`을 박아 두면,
# [해설] `is_web_search_tool`이 레지스트리 없이도 "웹 검색 도구 변형"을 식별할 수 있다.
# [해설] JSON으로 역직렬화된 MCP metadata는 같은 키를 가질 수는 있어도 같은 객체 identity는 가질 수 없어 위조 불가.
_WEB_SEARCH_MARKER = "deepagents_web_search"
"""Tool-metadata key marking a workspace-bound `web_search` variant.

Read by `is_web_search_tool`, the same way MCP read-only hints are read off
tool metadata, so a variant does not have to be registered anywhere.
"""

_WEB_SEARCH_TOKEN = object()
"""Value `is_web_search_tool` requires under `_WEB_SEARCH_MARKER`.

A module-private object rather than `True` so the marker cannot be forged: MCP
tool metadata is deserialized JSON, which can carry the key but never this
identity. Callers therefore need no separate "is this tool remote?" guard.
"""

# [해설] `fetch_url`이 허용하는 스킴(file://, gopher:// 등 차단)과 리다이렉트 최대 횟수. `_validate_url`, `_fetch_with_redirects`에서 사용.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_MAX_FETCH_REDIRECTS = 5

# Maintainer note: `deepagents-talon` imports `web_search` and `fetch_url`
# directly from this module. Keep their names, signatures, and return/error dict
# shapes stable unless `deepagents-talon` is migrated in the same change.

# Module-level lock guarding the urllib3 connection-factory monkeypatch used by
# `_pinned_dns`. The patch is process-global, so serializing fetches keeps
# concurrent calls from clobbering each other's pinned IP set.
_dns_pin_lock = threading.Lock()


# [해설] SSRF 가드가 의도적으로 거부한 경우만 표시하는 예외 타입. `fetch_url`이 이것을 잡아 category="validation" 오류 dict로 바꾼다.
class _UrlValidationError(ValueError):
    """Raised by `_validate_url` for scheme/DNS/SSRF-blocked URLs.

    Distinguishes intentional SSRF-guard rejections from incidental
    `ValueError`s raised elsewhere in the fetch path (e.g., markdown
    conversion).
    """


# [해설][주의] 보안 핵심 함수. `ip.is_global`이 아니면 무조건 차단하므로, 명시 조건(private/loopback...)은 사실상 이중 안전장치다.
# [해설] IPv4-mapped IPv6, 6to4 래핑을 먼저 벗겨서 `::ffff:127.0.0.1` 같은 우회를 막는다. 호출자: `_validate_url`.
def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if `ip` belongs to a non-publicly-routable range.

    Rejects: private (RFC1918/ULA), loopback, link-local (including cloud
    IMDS at `169.254.169.254`), reserved, multicast, unspecified
    (`0.0.0.0`/`::`), and anything `ipaddress` does not consider globally
    routable (catches benchmarking, documentation, and similar ranges the
    explicit predicates miss).

    IPv4-mapped IPv6 (`::ffff:a.b.c.d`) and 6to4 (`2002::/16`) are unwrapped
    to their underlying IPv4 address before the checks so that private
    space tunneled inside an IPv6 wrapper is still caught — e.g.,
    `::ffff:127.0.0.1` and `2002:a9fe:a9fe::1` (6to4 over IMDS) both
    evaluate as blocked.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


# [해설][흐름] SSRF 가드: 1) 스킴 검사 2) hostname 존재 검사 3) IDNA 인코딩 4) DNS 해석 5) 해석된 모든 IP가 공인 주소인지 검사.
# [해설] 하나라도 차단 대역이면 전체 거부(여러 A 레코드 중 하나만 사설이어도 거부). 반환한 IP 목록은 `_pinned_dns`에 넘겨
# [해설] 실제 연결을 그 IP로 고정한다(DNS rebinding TOCTOU 방어). 호출자: `_fetch_with_redirects`(각 hop마다).
def _validate_url(url: str) -> list[str]:
    """Reject URLs that target private/internal/metadata addresses.

    Resolves the URL's hostname and rejects any URL whose hostname resolves
    to a private, loopback, link-local (includes cloud IMDS at
    `169.254.169.254`), reserved, multicast, or unspecified IP — including
    such addresses wrapped in IPv4-mapped IPv6 (`::ffff:...`) or 6to4
    (`2002::/16`). This is the SSRF guard required because the URL is
    supplied by an LLM agent and may originate from prompt-injected content.

    Note:
        This function resolves DNS once. The HTTP client must be pinned to
        the returned IP list (see `_pinned_dns`) to close the TOCTOU window
        against attacker-controlled DNS (rebinding).

    Args:
        url: Candidate URL to validate.

    Returns:
        The list of validated IP strings the hostname resolves to.

            Callers should pin the outgoing connection to one of these IPs.

    Raises:
        _UrlValidationError: If the URL is malformed, uses a disallowed
            scheme, fails DNS resolution, or resolves to a blocked address.
    """
    # [해설][흐름] 1) 스킴 허용 목록 검사
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        msg = f"URL scheme not allowed: {parsed.scheme!r} (must be http or https)"
        raise _UrlValidationError(msg)

    # [해설][흐름] 2) hostname 없음(예: "http:///path") 거부
    hostname = parsed.hostname
    if not hostname:
        msg = "URL is missing a hostname"
        raise _UrlValidationError(msg)

    # [해설][흐름] 3) 국제화 도메인을 ASCII(punycode)로 변환. `_fetch_with_redirects`도 같은 방식으로 인코딩해 pin 키가 일치해야 한다.
    try:
        encoded_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        msg = f"Could not encode hostname {hostname!r} as IDNA: {exc}"
        raise _UrlValidationError(msg) from exc

    # [해설][흐름] 4) DNS 해석 (TCP 스트림 기준). 실패는 _UrlValidationError로 변환
    try:
        infos = socket.getaddrinfo(
            encoded_hostname,
            None,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        msg = f"Could not resolve hostname {hostname!r}: {exc}"
        raise _UrlValidationError(msg) from exc

    # [해설][흐름] 5) 해석된 모든 주소를 검사. 하나라도 차단이면 즉시 예외
    validated_ips: list[str] = []
    for info in infos:
        # `sockaddr[0]` may include an IPv6 scope id (`fe80::1%eth0`); strip
        # it before parsing so `ipaddress.ip_address` never raises.
        raw_ip = str(info[4][0]).split("%", 1)[0]
        ip = ipaddress.ip_address(raw_ip)
        if _is_blocked_ip(ip):
            logger.warning(
                "SSRF guard blocked URL %r: hostname %r resolves to %s",
                url,
                hostname,
                ip,
            )
            msg = (
                f"URL hostname {hostname!r} resolves to blocked address {ip} "
                "(private, loopback, link-local, reserved, or non-global range)"
            )
            raise _UrlValidationError(msg)
        validated_ips.append(raw_ip)

    if not validated_ips:
        msg = f"Hostname {hostname!r} resolved to no addresses"
        raise _UrlValidationError(msg)

    return validated_ips


# [해설][설계] requests → urllib3의 `create_connection`을 일시적으로 monkeypatch해, 검증된 hostname에 대한 연결만
# [해설] 검증된 IP로 강제한다. TLS SNI/Host 헤더는 원래 hostname을 유지하므로 인증서 검증은 그대로 동작한다(추정).
# [해설][주의] 패치가 프로세스 전역이라 `_dns_pin_lock`으로 직렬화한다 → 동시 fetch_url 호출은 순차 실행된다(처리량 저하 감수).
@contextlib.contextmanager
def _pinned_dns(hostname: str, allowed_ips: list[str]) -> Iterator[None]:
    """Force outgoing urllib3 connections for `hostname` to use `allowed_ips`.

    Patches `urllib3.util.connection.create_connection` for the duration of
    the context so that `requests` cannot re-resolve `hostname` to a
    different IP than the one `_validate_url` vetted (defends against DNS
    rebinding TOCTOU). The patch is process-global, so the module lock
    serializes concurrent fetches.

    Args:
        hostname: The exact hostname (already IDNA-encoded by the caller)
            whose resolution must be pinned.
        allowed_ips: The IPs `_validate_url` confirmed are safe to connect
            to. Tried in order; the first that accepts the connection wins.
    """
    from urllib3.util import connection as urllib3_connection

    with _dns_pin_lock:
        original = urllib3_connection.create_connection

        def patched(
            address: tuple[str, int], *args: Any, **kwargs: Any
        ) -> socket.socket:
            host, port = address[0], address[1]
            # [해설] 다른 호스트로의 연결(예: 무관한 커넥션)은 원래 함수로 통과시킨다.
            if host != hostname:
                return original(address, *args, **kwargs)
            # [해설] 검증된 IP들을 순서대로 시도하고 첫 성공 연결을 반환. 모두 실패하면 마지막 OSError를 올린다.
            last_exc: OSError | None = None
            for ip in allowed_ips:
                try:
                    return original((ip, port), *args, **kwargs)
                except OSError as exc:
                    last_exc = exc
            assert last_exc is not None  # noqa: S101  # loop body guarantees this
            raise last_exc

        urllib3_connection.create_connection = patched  # ty: ignore[invalid-assignment]  # signature matches at runtime
        # [해설][주의] finally에서 반드시 원래 함수로 복원 — 복원하지 않으면 이후 모든 urllib3 연결이 오염된다.
        try:
            yield
        finally:
            urllib3_connection.create_connection = original


# [해설] markdownify가 RecursionError로 실패할 때 쓰는 대체 텍스트 추출기. `_html_to_markdown_content`에서만 사용.
# [해설] 신뢰할 수 없는 페이지의 script/style 내용을 본문으로 내보내지 않도록 깊이 카운터로 건너뛴다.
class _TextExtractor(HTMLParser):
    """Extract text content from HTML as a markdownify fallback.

    The character data inside raw-text elements (`script`, `style`,
    `noscript`, `template`) is skipped so the fallback never emits
    JavaScript or CSS source from the fetched (untrusted) page as page
    content.
    """

    # Tags whose character data is never page content. Suppressed via an
    # explicit allowlist of skipped tags rather than trying to detect script
    # payloads after the fact.
    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],  # noqa: ARG002  # required by HTMLParser override
    ) -> None:
        """Enter a raw-text element so its data is skipped."""
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """Leave a raw-text element."""
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        """Collect non-empty, whitespace-collapsed text outside skipped tags."""
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    def get_text(self) -> str:
        """Return extracted text fragments separated by blank lines."""
        return "\n\n".join(self.parts)


# [해설] HTML→markdown 변환. 깊게 중첩된 악성/병리적 HTML이 markdownify 재귀 한도를 넘으면 `_TextExtractor`로 폴백하고,
# [해설] 그것마저 실패하면 빈 문자열. 즉 어떤 입력에도 도구가 예외로 죽지 않게 한다. 호출자: `fetch_url`.
def _html_to_markdown_content(html: str, markdownify: Callable[[str], str]) -> str:
    """Convert HTML to markdown, falling back to plain text on recursion.

    Args:
        html: Raw HTML to convert.
        markdownify: The `markdownify.markdownify` callable, injected so this
            module avoids an eager top-level import of the optional dependency.

    Returns:
        Markdown content, or text extracted from the HTML if markdown
        conversion exceeds the recursion limit. Returns an empty string if
        the text-extraction fallback itself fails.
    """
    try:
        return markdownify(html)
    except RecursionError:
        logger.warning(
            "markdownify hit recursion depth; falling back to text extraction",
            exc_info=True,
        )

    # Best-effort plain-text extraction. Guard it so a failure here (e.g. the
    # same pathological input that exhausted markdownify's recursion) cannot
    # re-introduce the uncaught crash this fallback exists to prevent.
    try:
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
    except Exception:  # fallback is best-effort; must never propagate
        logger.warning("text-extraction fallback failed", exc_info=True)
        return ""
    return parser.get_text()


# [해설] Tavily 키 미설정 시 모델에게 보여줄 오류 payload. 기본/워크스페이스 두 변형이 동일하게 실패하도록 공유한다.
def _missing_tavily_key_error(query: object) -> dict[str, object]:
    """Return the payload the model sees when no Tavily key is configured.

    Shared by the built-in and workspace-bound variants: `is_web_search_tool`
    treats them as one tool, so they have to fail identically.

    Returns:
        Error payload naming the env var to set.
    """
    return {
        "error": "Tavily API key not configured. "
        "Please set TAVILY_API_KEY environment variable.",
        "query": query,
    }


# [해설] 선택 의존성(tavily, requests, markdownify) 미설치 시 오류 payload.
def _missing_package_error(exc: ImportError) -> dict[str, str]:
    """Return the payload the model sees when an optional package is absent.

    Returns:
        Error payload naming the missing package.
    """
    return {"error": f"Required package not installed: {exc.name}."}


# [해설] 전역 `web_search`용 Tavily 클라이언트를 최초 호출 시 1회 생성. 키는 `deepagents_code.config.credentials`에서 읽는다.
# [해설][주의] 한 번 None으로 캐시되면 프로세스 재시작 전까지 키를 새로 설정해도 반영되지 않는다(추정: 테스트에서 리셋).
def _get_tavily_client() -> TavilyClient | None:
    """Get or initialize the lazy Tavily client singleton.

    Returns:
        TavilyClient instance, or None if API key is not configured.
    """
    global _tavily_client  # noqa: PLW0603  # Module-level cache requires global statement
    if _tavily_client is not _UNSET:
        return _tavily_client  # ty: ignore[invalid-return-type]  # narrowed by sentinel check

    from deepagents_code.config import credentials

    if credentials.has_tavily:
        from tavily import TavilyClient as _TavilyClient

        _tavily_client = _TavilyClient(api_key=credentials.tavily_api_key)
    else:
        _tavily_client = None
    return _tavily_client


# [해설] 워크스페이스 자격증명에 묶인 `web_search` 변형을 만든다. 호출자: `server_graph._build_tools`(Tavily 키가 있을 때).
# [해설] `functools.wraps(web_search)`로 시그니처/독스트링을 복사해 스키마가 전역 버전과 완전히 같다.
# [해설] 반환 도구의 metadata에 `_WEB_SEARCH_MARKER`를 넣어 `is_web_search_tool`이 식별하게 한다.
def create_web_search_tool(api_key: str) -> BaseTool:
    """Bind web search to one workspace credential.

    The schema is taken from `web_search` via `functools.wraps` so the built-in
    and workspace-bound variants can never present different arguments. The two
    also have to fail the same way: `is_web_search_tool` treats them as one, so
    a missing package or an unusable key must return the payload the model can
    act on rather than raising.

    Returns:
        Workspace-bound web search tool.
    """
    # Built on first use and reused: a per-call client would open a fresh
    # connection pool and repeat the TLS handshake for every search.
    client: TavilyClient | None = None

    # [해설] 클로저 변수 `client`를 첫 호출 때만 생성해 재사용(연결 풀·TLS 핸드셰이크 재사용).
    @tool("web_search")
    @functools.wraps(web_search)
    def workspace_web_search(**kwargs: Any) -> object:
        nonlocal client
        if not api_key:
            return _missing_tavily_key_error(kwargs.get("query"))
        if client is None:
            try:
                from tavily import TavilyClient as _TavilyClient

                client = _TavilyClient(api_key=api_key)
            except ImportError as exc:
                return _missing_package_error(exc)
        return _search_with_tavily(client, **kwargs)

    workspace_web_search.metadata = {
        **(workspace_web_search.metadata or {}),
        _WEB_SEARCH_MARKER: _WEB_SEARCH_TOKEN,
    }
    return workspace_web_search


# [해설] 전역 `web_search` 객체이거나 마커 토큰을 가진 변형이면 True. 시스템 프롬프트의 웹 검색 가이드 삽입 여부 판단 등에 쓰인다(추정: agent.py `_WEB_SEARCH_TOOL_GUIDANCE`).
def is_web_search_tool(candidate: object) -> bool:
    """Return whether `candidate` is a built-in or workspace-bound search tool.

    Returns:
        `True` for the module-level tool or any variant the factory marked.
    """
    if candidate is web_search:
        return True
    metadata = getattr(candidate, "metadata", None) or {}
    return metadata.get(_WEB_SEARCH_MARKER) is _WEB_SEARCH_TOKEN


# [해설] LangSmith/MCP 도구와 연계할 때 모델이 현재 스레드 ID를 알 수 있게 하는 도구. LangGraph `get_config()`로 실행 중 config를 읽는다.
@tool
def get_current_thread_id() -> str:
    """Get the current Deep Agents thread ID for LangSmith or MCP tooling.

    Returns:
        The current `configurable.thread_id`, or an explanatory message if missing.
    """
    thread_id = get_config().get("configurable", {}).get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    return "No current thread ID is available."


# [해설] 전역 Tavily 키 기반 웹 검색 함수. `@tool` 데코레이터가 없으므로 그대로는 plain 함수이고,
# [해설] LangChain이 tools 목록에서 callable을 도구로 변환한다(추정). `Annotated[..., Field(description)]`가 인자 스키마 설명이 된다.
# [해설][주의] 파일 상단 maintainer note대로 `deepagents-talon`이 이 이름/시그니처를 직접 import하므로 바꾸면 안 된다.
def web_search(  # noqa: ANN201  # Return type depends on dynamic tool configuration
    query: Annotated[
        str,
        Field(description="The search query (be specific and detailed)."),
    ],
    max_results: Annotated[
        int,
        Field(description="Number of results to return."),
    ] = 5,
    topic: Annotated[
        Literal["general", "news", "finance"],
        Field(
            description=(
                'Search topic type: "general" for most queries, "news" for '
                'current events, or "finance".'
            )
        ),
    ] = "general",
    include_raw_content: Annotated[
        bool,
        Field(
            description=(
                "Include full page content (uses more tokens). Prefer `fetch_url` "
                "for a single URL."
            )
        ),
    ] = False,
):
    """Search the web for current information.

    Returns:
        Search hits with title, URL, snippet, and score.
    """
    client = _get_tavily_client()
    if client is None:
        return _missing_tavily_key_error(query)
    return _search_with_tavily(
        client,
        query=query,
        max_results=max_results,
        topic=topic,
        include_raw_content=include_raw_content,
    )


# [해설] 두 web_search 변형이 공유하는 실제 검색 실행부. 네트워크/Tavily 예외를 `{"error": ..., "query": ...}` dict로 번역한다.
# [해설] import를 함수 안에서 하는 이유: tavily/requests는 선택 의존성이라 모듈 import 시점 실패를 피하기 위함.
def _search_with_tavily(
    client: TavilyClient,
    *,
    query: str,
    max_results: int,
    topic: Literal["general", "news", "finance"],
    include_raw_content: bool,
) -> object:
    """Execute a Tavily search with the standard error translation.

    Returns:
        Search hits or a translated error payload.
    """
    try:
        import requests
        from tavily import (
            BadRequestError,
            InvalidAPIKeyError,
            MissingAPIKeyError,
            UsageLimitExceededError,
        )
        from tavily.errors import ForbiddenError, TimeoutError as TavilyTimeoutError
    except ImportError as exc:
        return _missing_package_error(exc)

    try:
        return client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
    except (
        requests.exceptions.RequestException,
        ValueError,
        TypeError,
        # Tavily-specific exceptions
        BadRequestError,
        ForbiddenError,
        InvalidAPIKeyError,
        MissingAPIKeyError,
        TavilyTimeoutError,
        UsageLimitExceededError,
    ) as e:
        return {"error": f"Web search error: {e!s}", "query": query}


# [해설] 모델이 호출하는 URL 가져오기 도구. `_fetch_with_redirects`(SSRF 검증 포함)로 받아 markdown으로 변환한다.
# [해설] 오류는 category(validation / redirects / network)를 붙인 dict로 반환한다. HTTP 4xx/5xx는 `raise_for_status`의
# [해설] `HTTPError`(RequestException 하위)로 network 범주가 된다.
# [해설][주의] 결과가 크면 SDK `FilesystemMiddleware.wrap_tool_call`이 large_tool_results로 퇴출한다(analysis/02 C절).
def fetch_url(
    url: Annotated[
        str,
        Field(description="The URL to fetch (must be a valid HTTP/HTTPS URL)."),
    ],
    timeout: Annotated[
        int,
        Field(description="Request timeout in seconds."),
    ] = 30,
) -> dict[str, Any]:
    """Fetch a URL and return the page content as markdown.

    Returns:
        Fetched page markdown plus status metadata.
    """
    try:
        import requests
        from markdownify import markdownify
    except ImportError as exc:
        return _missing_package_error(exc)

    try:
        response = _fetch_with_redirects(url, timeout=timeout)
    except _UrlValidationError as e:
        return {
            "error": f"Fetch URL error: {e!s}",
            "url": url,
            "category": "validation",
        }
    except requests.exceptions.TooManyRedirects as e:
        return {"error": f"Fetch URL error: {e!s}", "url": url, "category": "redirects"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Fetch URL error: {e!s}", "url": url, "category": "network"}

    # [해설][주의] content-type 검사 없이 `response.text`를 HTML로 취급한다. JSON/텍스트도 markdownify를 통과한다.
    markdown_content = _html_to_markdown_content(response.text, markdownify)
    if not markdown_content:
        logger.warning(
            "fetch_url produced empty content for %s (status %s)",
            response.url,
            response.status_code,
        )
    return {
        "url": str(response.url),
        "markdown_content": markdown_content,
        "status_code": response.status_code,
        "content_length": len(markdown_content),
    }


# [해설][설계] requests의 자동 리다이렉트를 끄고(`allow_redirects=False`) 직접 따라간다. 자동으로 따라가면
# [해설] 공개 URL → 302 → `http://169.254.169.254/` 같은 리다이렉트 기반 SSRF를 막을 수 없기 때문.
# [해설] 각 hop마다 `_validate_url` + `_pinned_dns`를 다시 적용한다. 최대 `_MAX_FETCH_REDIRECTS + 1`회 요청.
def _fetch_with_redirects(url: str, *, timeout: int) -> Any:  # noqa: ANN401  # requests.Response, but kept dynamic to avoid eager import
    """Fetch `url`, re-validating each redirect hop against the SSRF guard.

    Each hop is validated by `_validate_url` and its connection pinned to
    the validated IP via `_pinned_dns`. Caps at `_MAX_FETCH_REDIRECTS`
    redirects (so up to `_MAX_FETCH_REDIRECTS + 1` total hops counting the
    initial request). Network/HTTP errors propagate as
    `requests.exceptions.RequestException` (or its subclasses).

    Args:
        url: Initial URL to fetch.
        timeout: Per-request timeout in seconds.

    Returns:
        The final `requests.Response` for the non-redirect terminal hop.

    Raises:
        _UrlValidationError: If any hop fails SSRF validation or returns a
            3xx without a `Location` header.
        requests.exceptions.TooManyRedirects: If the redirect cap is exceeded.
    """
    import requests

    current_url = url
    session = requests.Session()
    # DNS pinning only protects the direct target connection. Environment
    # proxies resolve the target separately, so they must be disabled here.
    # [해설][주의] HTTP(S)_PROXY 환경변수를 무시한다. 프록시가 대상 호스트를 따로 해석하면 DNS pinning이 무력화되기 때문.
    # [해설] 따라서 사내 프록시 환경에서는 fetch_url이 직접 연결을 시도한다.
    session.trust_env = False
    for _hop in range(_MAX_FETCH_REDIRECTS + 1):
        validated_ips = _validate_url(current_url)
        hostname = urlparse(current_url).hostname
        # `_validate_url` raises if hostname is missing, so this is non-None.
        assert hostname is not None  # noqa: S101  # invariant from _validate_url
        encoded_hostname = hostname.encode("idna").decode("ascii")

        with _pinned_dns(encoded_hostname, validated_ips):
            response = session.get(
                current_url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; DeepAgents/1.0)"},
                allow_redirects=False,
            )

        # 300-399 covers every redirect class. `requests.Response.is_redirect`
        # also checks for a `Location` header, which would hide malformed 3xx
        # responses — so we check the raw status code instead.
        # [해설] 리다이렉트: Location이 없으면 거부, 상대 경로면 `urljoin`으로 절대 URL화한 뒤 루프 처음에서 재검증.
        if 300 <= response.status_code < 400:  # noqa: PLR2004  # HTTP redirect class
            location = response.headers.get("Location")
            if not location:
                msg = (
                    f"Redirect response (status {response.status_code}) at "
                    f"{current_url!r} is missing a Location header"
                )
                raise _UrlValidationError(msg)
            current_url = urljoin(current_url, location)
            continue

        # [해설] 최종(비-리다이렉트) 응답. 4xx/5xx면 HTTPError가 올라가 `fetch_url`의 network 범주로 처리된다.
        response.raise_for_status()
        return response

    msg = f"Exceeded {_MAX_FETCH_REDIRECTS} redirects starting from {url!r}"
    raise requests.exceptions.TooManyRedirects(msg)
