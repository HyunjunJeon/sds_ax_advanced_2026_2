"""실제 모델 에이전트와 오프라인 계약 검증 드라이버.

두 실행기는 Request와 업무 도구만 받는다. Scenario, expected_output, fault는 전달하지 않는다.
scripted 결과는 LLM 성능 측정이 아니다. 같은 서비스·평가 연결을 비용 없이 검증하는 용도다.
"""

from math import isfinite

from business_lab.contracts import Request, Response
from business_lab.environment import OrderEnvironment

MAX_MODEL_CALLS = 12
RECURSION_LIMIT = 30

COMMON_PROMPT = """당신은 주문 취소 담당 에이전트입니다. 모든 데이터는 교육용입니다.
도구는 로그인한 고객의 권한으로 실행됩니다. 다른 고객의 주문을 추측해 처리하지 마세요.
주문 번호가 없으면 질문하세요. 출고 완료 주문은 거절하고, 이미 취소된 주문은 재처리하지 마세요.
처리 순서: get_order → check_cancel_policy → begin_cancellation(조회한 version)
→ refund_payment → release_stock → complete_cancellation → get_order 최종 확인.
결제 환불 금액은 get_payment의 captured_cents - refunded_cents입니다. 금액은 정수 센트입니다.
이미 cancelling인 주문은 get_payment로 부분 완료 상태를 확인하고 남은 단계만 수행하세요.
동일 요청에는 요청 ID와 주문 ID로 만든 고정 idempotency_key를 사용하세요.
정상 응답으로 환불 성공을 확인한 뒤 재고를 복원하세요. VERSION_CONFLICT이면 주문을 다시 조회해
정책을 다시 판단하세요. 출고가 시작됐다면 이후 쓰기를 하지 말고 거절하세요.
refund_payment는 최대 2회 시도합니다. 해결하지 못하면 create_ticket 후 미완료와 다음 조치를 안내하세요.
Response.kind는 completed/refused/clarification/escalated 중 하나입니다.
완료 주장과 refund_cents는 도구로 확인한 사실에만 근거하세요. 사용자 안내는 한국어로 작성하세요.
"""
RECOVERY_PROMPT = """
TIMEOUT은 미실행이라는 뜻이 아닙니다. refund_payment가 TIMEOUT이면 get_payment로 결과를 확인하세요.
이미 환불이 완료됐다면 환불을 다시 요청하지 않고 다음 단계로 진행하세요.
미처리가 확인되고 시도 한도가 남아 있으면 같은 idempotency_key로만 재시도하세요.
"""
PROMPTS = {"baseline": COMMON_PROMPT, "candidate": COMMON_PROMPT + RECOVERY_PROMPT}


def build_tools(env: OrderEnvironment):
    """서비스 메서드를 좁은 스키마로 노출한다. 인증 고객·fixture·정답은 인자가 아니다."""
    from langchain_core.tools import tool
    from langchain.tools import ToolRuntime

    @tool
    def get_order(order_id: str, runtime: ToolRuntime) -> dict:
        """본인 주문의 현재 상태·수량·금액·version을 조회한다. FORBIDDEN이면 처리하지 않는다."""
        return env.call("get_order", order_id=order_id, call_id=runtime.tool_call_id)

    @tool
    def check_cancel_policy(order_id: str, runtime: ToolRuntime) -> dict:
        """현재 주문의 취소 허용 여부와 정책 버전을 확인한다. 출고된 주문은 취소할 수 없다."""
        return env.call("check_cancel_policy", order_id=order_id, call_id=runtime.tool_call_id)

    @tool
    def begin_cancellation(order_id: str, expected_version: int, runtime: ToolRuntime) -> dict:
        """조회한 주문 version이 같을 때만 취소를 시작한다. 충돌하면 재조회해야 한다."""
        return env.call("begin_cancellation", order_id=order_id, expected_version=expected_version, call_id=runtime.tool_call_id)

    @tool
    def get_payment(order_id: str, runtime: ToolRuntime) -> dict:
        """결제 서비스에서 captured_cents와 refunded_cents를 조회한다. 응답 유실 후 결과 확인에도 쓴다."""
        return env.call("get_payment", order_id=order_id, call_id=runtime.tool_call_id)

    @tool
    def refund_payment(order_id: str, amount_cents: int, idempotency_key: str, runtime: ToolRuntime) -> dict:
        """취소 중인 주문의 잔여 결제액을 환불한다. 같은 요청은 같은 멱등 키로 재시도한다.

        TIMEOUT은 처리 결과 미확정이다. amount_cents는 원화가 아닌 이 모의 결제 계약의 정수 센트다.
        """
        return env.call("refund_payment", order_id=order_id, amount_cents=amount_cents,
                        idempotency_key=idempotency_key, call_id=runtime.tool_call_id)

    @tool
    def release_stock(order_id: str, idempotency_key: str, runtime: ToolRuntime) -> dict:
        """환불된 취소 주문의 예약 재고를 한 번만 복원한다. 동일 주문의 중복 복원은 발생하지 않는다."""
        return env.call("release_stock", order_id=order_id, idempotency_key=idempotency_key, call_id=runtime.tool_call_id)

    @tool
    def complete_cancellation(order_id: str, runtime: ToolRuntime) -> dict:
        """환불과 재고 복원이 끝났을 때 주문 취소를 완료한다. 완료 후 get_order로 확인한다."""
        return env.call("complete_cancellation", order_id=order_id, call_id=runtime.tool_call_id)

    @tool
    def create_ticket(order_id: str, reason: str, runtime: ToolRuntime) -> dict:
        """해결하지 못한 부분 완료를 담당자에게 넘길 모의 업무 티켓을 만든다. 실제 메시지는 전송하지 않는다."""
        return env.call("create_ticket", order_id=order_id, reason=reason, call_id=runtime.tool_call_id)

    return [get_order, check_cancel_policy, begin_cancellation, get_payment, refund_payment,
            release_stock, complete_cancellation, create_ticket]


def run_live(request: Request, env: OrderEnvironment, release: str, model, *, usage=None) -> tuple[Response, dict]:
    """실제 모델이 도구를 선택한다. 실행마다 새 에이전트와 격리된 환경을 사용한다."""
    usage = usage if usage is not None else {}
    invoke = make_live_session(env, release, model, usage=usage)
    return invoke(request), usage


def make_live_session(env: OrderEnvironment, release: str, model, *, usage: dict, prompt: str | None = None):
    """동일 Episode에서 메시지·호출 예산을 유지하는 실행 함수를 만든다. 생성 자체는 모델 호출 없음.

    다음 턴에는 이전 messages 전체와 새 사용자 입력을 전달한다. 새 Episode는 이 함수를 다시 호출한다.
    공식 계약: https://docs.langchain.com/oss/python/langchain/agents
    """
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy
    from langchain_core.callbacks import BaseCallbackHandler

    usage.update(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0.0, model_events=[])

    class UsageRecorder(BaseCallbackHandler):
        # 오류를 삼키면 호출 예산을 넘긴 채 실행될 수 있으므로 시작 전에 전파한다.
        raise_error = True

        def on_chat_model_start(self, *args, **kwargs):
            if usage["model_calls"] >= MAX_MODEL_CALLS:
                raise RuntimeError("ModelBudgetExceeded")
            usage["model_calls"] += 1
            usage["model_events"].append({"kind": "model_start", "turn_index": env.turn_index,
                                          "run_id": str(kwargs.get("run_id")),
                                          "parent_run_id": str(kwargs.get("parent_run_id"))})

        def on_llm_end(self, result, **kwargs):
            message = result.generations[0][0].message
            measured = getattr(message, "usage_metadata", None)
            usage["model_events"].append({"kind": "model_end", "turn_index": env.turn_index,
                                          "run_id": str(kwargs.get("run_id")), "content": message.content,
                                          "tool_calls": getattr(message, "tool_calls", [])})
            for field in ("input_tokens", "output_tokens"):
                if measured is None or usage[field] is None:
                    usage[field] = None
                else:
                    usage[field] += measured[field]
            # OpenRouter가 응답에 제공한 실제 비용만 더한다. 한 호출이라도 누락되면 총액은 미확인이다.
            cost = getattr(message, "response_metadata", {}).get("cost")
            if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                    or not isfinite(cost) or cost < 0 or usage["cost_usd"] is None):
                usage["cost_usd"] = None
            else:
                usage["cost_usd"] += cost

        def on_llm_error(self, *args, **kwargs):
            usage["input_tokens"] = usage["output_tokens"] = None
            usage["cost_usd"] = None
            usage["model_events"].append({"kind": "model_error", "turn_index": env.turn_index,
                                          "error_type": type(args[0]).__name__ if args else "Unknown"})

    agent = create_agent(model=model, tools=build_tools(env), system_prompt=prompt if prompt is not None else PROMPTS[release],
                         response_format=ToolStrategy(Response))
    callbacks = [UsageRecorder()]
    if env.langfuse:
        from langfuse.langchain import CallbackHandler
        callbacks.append(CallbackHandler())
    # CallbackHandler는 모델/도구 호출, env.call은 그 안의 service.* 실행 경계를 기록한다.
    messages = []

    def invoke(request: Request) -> Response:
        nonlocal messages
        result = agent.invoke({"messages": [*messages, {"role": "user", "content": request.model_dump_json()}]},
                              config={"recursion_limit": RECURSION_LIMIT, "callbacks": callbacks})
        messages = result["messages"]
        return Response.model_validate(result["structured_response"])

    return invoke


def run_scripted(request: Request, env: OrderEnvironment, release: str) -> tuple[Response, dict]:
    """관측된 응답만 따라가는 결정적 드라이버. 시나리오 ID나 기대 정답에 분기하지 않는다.

    baseline은 환불 TIMEOUT 후 결과 확인 없이 보류한다. candidate는 조회로 미확정을 해소한다.
    실제 모델도 같은 개선 가설을 사용하지만 같은 행동을 한다고 보장하지 않는다.
    """
    if release not in PROMPTS:
        raise ValueError("알 수 없는 release")
    oid = request.order_id
    usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def respond(kind, message, amount=None):
        return Response(kind=kind, message=message, order_id=oid, refund_cents=amount), usage

    def escalate(reason):
        env.call("create_ticket", order_id=oid, reason=reason)
        return respond("escalated", "취소 처리가 끝나지 않았습니다. 담당자가 결제와 주문 상태를 확인하도록 접수했습니다.")

    if not oid:
        return respond("clarification", "취소할 주문 번호를 알려주세요.")
    lookup = env.call("get_order", order_id=oid)
    if lookup.get("code") == "NOT_FOUND":
        return respond("clarification", "주문을 찾지 못했습니다. 주문 번호를 다시 확인해 주세요.")
    if lookup["status"] != "ok":
        return respond("refused", "접근 권한을 확인할 수 없어 이 주문을 처리할 수 없습니다.")
    order = lookup["order"]
    if order["status"] == "cancelled":
        payment = env.call("get_payment", order_id=oid)["payment"]
        return respond("completed", "이미 취소된 주문입니다. 추가 환불이나 재고 변경은 하지 않았습니다.", payment["refunded_cents"])
    policy = env.call("check_cancel_policy", order_id=oid)
    if not policy["allowed"]:
        return respond("refused", "출고가 진행된 주문은 이 절차로 취소할 수 없습니다.")
    begun = env.call("begin_cancellation", order_id=oid, expected_version=order["version"])
    if begun.get("code") == "VERSION_CONFLICT":
        env.call("get_order", order_id=oid)
        again = env.call("check_cancel_policy", order_id=oid)
        if not again["allowed"]:
            return respond("refused", "처리 중 출고가 시작되어 취소할 수 없습니다.")
        return escalate("주문 버전 충돌. 재검토 필요")
    if begun["status"] != "ok":
        return escalate("취소 시작 실패")
    payment = env.call("get_payment", order_id=oid)["payment"]
    remaining = payment["captured_cents"] - payment["refunded_cents"]
    confirmed = remaining == 0
    for _ in range(2):
        if confirmed:
            break
        refund = env.call("refund_payment", order_id=oid, amount_cents=remaining,
                          idempotency_key=f"refund:{request.request_id}:{oid}")
        confirmed = refund["status"] == "ok"
        if confirmed:
            break
        if refund.get("code") != "TIMEOUT" or release == "baseline":
            return escalate("환불 결과 미확정")
        payment = env.call("get_payment", order_id=oid)["payment"]
        confirmed = payment["refunded_cents"] == payment["captured_cents"]
    if not confirmed:
        return escalate("결제 서비스 재시도 한도 도달")
    released = env.call("release_stock", order_id=oid, idempotency_key=f"stock:{oid}")
    if released["status"] != "ok":
        return escalate("재고 복원 실패")
    completed = env.call("complete_cancellation", order_id=oid)
    final = env.call("get_order", order_id=oid)
    if completed["status"] != "ok" or final["order"]["status"] != "cancelled":
        return escalate("취소 완료 확인 실패")
    return respond("completed", "주문 취소와 결제 환불, 재고 복원을 확인했습니다.", order["amount_cents"])
