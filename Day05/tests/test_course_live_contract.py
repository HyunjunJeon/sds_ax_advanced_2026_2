"""실제 create_agent와 가짜 모델을 사용한다. 네트워크 없이 도구 호출 ID와 턴별 대화를 검증한다."""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import pytest

from business_lab.agents import MAX_MODEL_CALLS, make_live_session
from business_lab.contracts import Request
from business_lab.dataset import load_fixtures
from business_lab.environment import OrderEnvironment


class ScriptModel(BaseChatModel):
    responses: list[AIMessage]
    inputs: list = []
    cursor: int = 0

    @property
    def _llm_type(self):
        return "course-test-only"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.inputs.append(list(messages))
        message = self.responses[self.cursor]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name, call_id, args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args}])


def question():
    return call("Response", "question-response", {"kind": "clarification", "message": "주문 번호를 알려주세요.", "order_id": None})


def test_model_keeps_messages_and_tool_call_id_without_golden_leak():
    model = ScriptModel(responses=[question(), call("get_order", "real-tool-call", {"order_id": "order-a"}),
        call("Response", "last-response", {"kind": "refused", "message": "테스트 응답", "order_id": "order-a"})])
    env, usage = OrderEnvironment(load_fixtures()["fx-normal"]), {}
    try:
        invoke = make_live_session(env, "baseline", model, usage=usage)
        first = Request(message="주문 취소 요청", order_id=None, request_id="same-request")
        assert invoke(first).kind == "clarification"
        env.turn_index = 2
        invoke(Request(message="주문 번호는 order-a입니다.", order_id="order-a", request_id="same-request"))
        assert usage["model_calls"] == 3
        assert env.events[0].call_id == "real-tool-call" and env.events[0].turn_index == 2
        human_inputs = [m.content for m in model.inputs[1] if m.type == "human"]
        assert len(human_inputs) == 2 and "주문 취소 요청" in human_inputs[0]
        assert all("expected_output" not in text and "fault" not in text and "fixture_id" not in text for text in human_inputs)
    finally:
        env.close()


def test_model_budget_is_not_reset_for_followup_turn():
    model = ScriptModel(responses=[question(), *[call("get_order", f"read-{i}", {"order_id": "order-a"}) for i in range(MAX_MODEL_CALLS + 2)]])
    env, usage = OrderEnvironment(load_fixtures()["fx-normal"]), {}
    try:
        invoke = make_live_session(env, "baseline", model, usage=usage)
        invoke(Request(message="취소", order_id=None, request_id="request"))
        env.turn_index = 2
        with pytest.raises(RuntimeError, match="ModelBudgetExceeded"):
            invoke(Request(message="order-a", order_id="order-a", request_id="request"))
        assert usage["model_calls"] == MAX_MODEL_CALLS
        assert model.cursor == MAX_MODEL_CALLS
    finally:
        env.close()


@pytest.mark.parametrize("second_cost,expected", [(0.02, 0.03), (0.0, 0.01), (None, None), (float("nan"), None)])
def test_provider_cost_is_summed_across_turns_only_when_every_call_is_measured(second_cost, expected):
    first, second = question(), question()
    first.response_metadata["cost"] = 0.01
    if second_cost is not None:
        second.response_metadata["cost"] = second_cost
    model = ScriptModel(responses=[first, second])
    env, usage = OrderEnvironment(load_fixtures()["fx-normal"]), {}
    try:
        invoke = make_live_session(env, "baseline", model, usage=usage)
        request = Request(message="취소", order_id=None, request_id="cost-test")
        invoke(request)
        env.turn_index = 2
        invoke(request)
        assert usage["cost_usd"] == expected
        # 다음 호출이 실패하면 앞선 성공 호출 비용을 전체 비용으로 보고하면 안 된다.
        with pytest.raises(IndexError):
            invoke(request)
        assert usage["cost_usd"] is None
    finally:
        env.close()
