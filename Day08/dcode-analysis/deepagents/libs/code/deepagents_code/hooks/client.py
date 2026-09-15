"""Client-side fulfillment for server-owned Hooks v2 interrupts."""

# [해설] 모듈 개요: 서버가 LangGraph `interrupt()`로 보낸 "훅 실행 요청"을 클라이언트에서 실제로 실행하고
# [해설] `Command(resume=...)`에 넣을 resume 값을 만들어 돌려주는 클라이언트 측 이행(fulfillment) 계층이다.
# [해설] 실행 프로세스: 항상 클라이언트(TUI / headless / remote client). `--acp`는 in-process라 같은 프로세스 안에서 돈다.
# [해설] 주요 진입점: `fulfill_hook_interrupt`(interrupt 1건), `fulfill_pending_hook_interrupts`(interrupt id→payload 묶음),
# [해설] `fulfill_hook_invocation`(파싱된 요청 1건), `HookFulfillmentLedger`(중복 실행 방지 장부).
# [해설] 호출자: `hooks/manager.py`의 `HooksManager.fulfill_interrupt` / `HooksManager.fulfill_pending_interrupts`.
# [해설] 그 위에서 `tui/textual_adapter.py`, `client/remote_client.py`, `client/non_interactive.py`가 스트림에서 hook interrupt를 감지하면 부른다.
# [해설] 반대편(서버): `hooks/server_middleware.py`의 `_invoke_hook`이 `build_hook_interrupt_payload`로 요청을 만들고,
# [해설] 여기서 만든 resume 값을 `parse_hook_resume_value`로 검증한다. 직렬화 규약은 `hooks/interrupt.py`.
# [해설][흐름] 서버 interrupt → 클라이언트가 스트림에서 payload 수신 → (여기) snapshot 검증 → `HooksRuntime.invoke`(HookEngine)로
# [해설][흐름] 매칭 핸들러 셸 실행·reduce → `HookInvocationResponse` → resume 값 → 서버 재개.
# [해설] 관련 문서: `analysis/07-mcp-hooks-extensions-plugins.md`(B. 클라이언트 훅 런타임과 서버 이벤트 왕복),
# [해설] 공식 `docs_official/code/hooks.md`(server-owned events round-trip to the client).
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from deepagents_code.hooks.interrupt import (
    build_hook_resume_value,
    parse_hook_interrupt_payload,
)
from deepagents_code.hooks.models.transport import HookInvocationResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from deepagents_code.hooks.models.transport import HookInvocationRequest
    from deepagents_code.hooks.runtime import HooksRuntime

# [해설] 장부 키 = (snapshot_id, invocation_id). invocation_id는 서버가 `_invocation_id`에서 uuid5로 결정적으로 만들므로,
# [해설] 같은 논리 이벤트가 재전달·재생(replay)되면 같은 키가 된다.
_FulfillmentKey = tuple[str, UUID]


# [해설] 한 클라이언트 세션 동안 훅 이행 결과를 기억하는 중복 제거 장부. `hooks/runtime.py`의 `HooksRuntime.fulfillments`로 1개 생성된다.
# [해설][설계] 왜 필요한가: LangGraph는 resume 시 노드를 처음부터 다시 실행하므로 같은 interrupt가 다시 올 수 있고,
# [해설] 스트림 재연결·동시 전달로 같은 요청이 겹칠 수 있다. 셸 훅은 부작용이 있으므로 "정확히 한 번" 실행되도록 결과를 공유한다.
# [해설][주의] `_completed`는 세션 동안 계속 쌓이며 비우는 코드가 이 파일에는 없다(세션 수명에 묶임).
@dataclass(slots=True)
class HookFulfillmentLedger:
    """Deduplicate hook fulfillment for one client session."""

    _in_flight: dict[_FulfillmentKey, asyncio.Task[HookInvocationResponse]] = field(
        default_factory=dict
    )
    _completed: dict[_FulfillmentKey, HookInvocationResponse] = field(
        default_factory=dict
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # [해설] 완료된 결과가 있으면 즉시 반환, 진행 중이면 같은 Task를 기다리고, 없으면 새 Task를 만든다.
    # [해설][설계] 락은 장부 조회/등록에만 잡고 실제 실행 대기는 락 밖에서 한다(긴 훅 실행 동안 다른 키가 막히지 않게).
    # [해설] `asyncio.shield`: 한 호출자가 취소돼도 공유 Task 자체는 취소되지 않아 다른 대기자가 결과를 받을 수 있다.
    async def fulfill(
        self,
        key: _FulfillmentKey,
        operation: Callable[[], Awaitable[HookInvocationResponse]],
    ) -> HookInvocationResponse:
        """Return one shared result for concurrent and repeated delivery."""
        async with self._lock:
            completed = self._completed.get(key)
            if completed is not None:
                return completed
            task = self._in_flight.get(key)
            if task is None:
                task = asyncio.create_task(self._run(key, operation))
                self._in_flight[key] = task
        return await asyncio.shield(task)

    # [해설] 실제 실행 래퍼. 성공 시 `_completed`에 저장, 실패(취소 포함 BaseException) 시 in-flight만 지워서
    # [해설] 다음 전달 때 재시도가 가능하게 한다(실패 결과는 캐시하지 않음).
    async def _run(
        self,
        key: _FulfillmentKey,
        operation: Callable[[], Awaitable[HookInvocationResponse]],
    ) -> HookInvocationResponse:
        try:
            result = await operation()
        except BaseException:
            async with self._lock:
                self._in_flight.pop(key, None)
            raise
        async with self._lock:
            self._completed[key] = result
            self._in_flight.pop(key, None)
        return result


# [해설] 파싱·검증된 `HookInvocationRequest` 1건을 실행해 resume dict를 반환한다. `fulfill_hook_interrupt`가 호출한다.
# [해설][흐름] 1) snapshot_id 일치 확인 → 2) 장부로 중복 제거하며 `runtime.invoke` 실행 → 3) presenter로 결과 표시 → 4) resume 값 직렬화.
async def fulfill_hook_invocation(
    runtime: HooksRuntime,
    request: HookInvocationRequest,
) -> dict[str, object]:
    """Execute a server-owned hook request and return a resume payload.

    Args:
        runtime: Session-scoped client Hooks runtime.
        request: Validated invocation request from the server.

    Returns:
        JSON-compatible resume value for `Command(resume=...)`.

    Raises:
        ValueError: If the request snapshot does not match this session.
    """
    # [해설][흐름] 1) 스냅샷 검증: 서버 요청의 snapshot_id는 턴 시작 시 클라이언트가 context(`hooks_snapshot_id`)로 보낸 값이다.
    # [해설] `/reload`로 훅 설정이 바뀌어 스냅샷이 달라졌다면 옛 설정 기준 요청을 새 설정으로 실행하지 않도록 즉시 거부한다.
    # [해설][주의] 여기서의 `ValueError`는 "neutral decision"으로 강등되지 않고 호출자로 전파된다.
    if request.snapshot_id != runtime.snapshot_id:
        msg = (
            f"Hook snapshot mismatch: request {request.snapshot_id} != "
            f"runtime {runtime.snapshot_id}"
        )
        raise ValueError(msg)

    # [해설][흐름] 2) 실제 실행 클로저: `HooksRuntime.invoke` → `HookEngine.run`(매칭 핸들러 병렬 실행 + `reduce_hook_results`)으로
    # [해설] 이벤트별 decision을 얻고, presenter가 진단/메시지를 UI에 렌더링한다. 응답에는 서버가 교차 검증할 id 두 개를 되돌려 담는다.
    async def execute() -> HookInvocationResponse:
        decision = await runtime.invoke(request.invocation)
        runtime.presenter.present_decision(decision)
        return HookInvocationResponse(
            protocol_version=1,
            invocation_id=request.invocation_id,
            snapshot_id=request.snapshot_id,
            decision=decision,
        )

    # [해설][흐름] 3) 장부를 통해 실행 — 같은 (snapshot_id, invocation_id)는 한 번만 셸 명령을 실행하고 이후엔 캐시 결과를 반환.
    response = await runtime.fulfillments.fulfill(
        (request.snapshot_id, request.invocation_id),
        execute,
    )
    return build_hook_resume_value(response)


# [해설] 원시 interrupt 값 1건을 받아 hook interrupt면 이행하고 아니면 `None`을 반환한다.
# [해설] `HooksManager.fulfill_interrupt`가 호출하며, `None`이면 호출자가 "파싱 실패" RuntimeError로 바꾼다.
# [해설] `parse_hook_interrupt_payload`는 `type == "hook_invocation"` 판별자로 HITL 승인 interrupt 등 다른 interrupt와 구분한다.
async def fulfill_hook_interrupt(
    runtime: HooksRuntime,
    interrupt_value: object,
) -> dict[str, object] | None:
    """Fulfill a raw interrupt value when it is a hook invocation.

    Args:
        runtime: Session-scoped client Hooks runtime.
        interrupt_value: Raw LangGraph interrupt payload.

    Returns:
        Resume value for hook interrupts, otherwise `None`.
    """
    request = parse_hook_interrupt_payload(interrupt_value)
    if request is None:
        return None
    return await fulfill_hook_invocation(runtime, request)


# [해설] 여러 interrupt(id→payload)를 한꺼번에 이행해 interrupt id별 resume 맵을 만든다. `HooksManager.fulfill_pending_interrupts`가 호출
# [해설] (headless `client/non_interactive.py`의 `_fulfill_pending_hook_interrupts` 경로 등).
# [해설][설계] 순차 실행이다: 병렬 핸들러 실행은 한 이벤트 내부(`HookEngine.run`)에서만 일어나고, interrupt 간에는 순서를 보존한다.
# [해설][주의] 하나라도 hook interrupt가 아니면 RuntimeError — 호출자는 hook interrupt만 골라서 넘겨야 한다.
async def fulfill_pending_hook_interrupts(
    runtime: HooksRuntime,
    pending: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Fulfill pending hook interrupts into a resume map keyed by interrupt id.

    Args:
        runtime: Session-scoped client Hooks runtime.
        pending: Mapping of LangGraph interrupt id to raw interrupt payload.

    Returns:
        Resume values ready for `Command(resume=...)`.

    Raises:
        RuntimeError: If a payload is not a valid hook interrupt.
    """
    resumes: dict[str, dict[str, object]] = {}
    for interrupt_id, payload in pending.items():
        resume_value = await fulfill_hook_interrupt(runtime, payload)
        if resume_value is None:
            msg = f"Failed to parse hook interrupt {interrupt_id}"
            raise RuntimeError(msg)
        resumes[interrupt_id] = resume_value
    return resumes
