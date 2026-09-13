import { describe, expect, test } from "bun:test";
import { controlRequestFrame, operatorAbortBrokerRequest } from "../src/sdk/cli/session-cli";

describe("sdk session raw control envelope", () => {
	test("routes confirmed operator terminal aborts through the dedicated broker envelope", () => {
		const input = { mode: "terminal", scope: "owned", operator: true };
		expect(
			operatorAbortBrokerRequest("session-1", "turn.abort", input, {
				confirm: true,
				idempotencyKey: "gajae-abort-test-key",
			}),
		).toEqual({
			sessionId: "session-1",
			operation: "turn.abort",
			input,
			confirm: true,
		});
		expect(input).toEqual({ mode: "terminal", scope: "owned", operator: true });
	});

	test("does not route ordinary terminal aborts through broker operator authority", () => {
		expect(
			operatorAbortBrokerRequest(
				"session-1",
				"turn.abort",
				{ mode: "terminal", scope: "turn" },
				{
					confirm: true,
					idempotencyKey: "ordinary-key",
				},
			),
		).toBeUndefined();
	});

	test("allows the terminal abort scope to default on the broker route", () => {
		expect(
			operatorAbortBrokerRequest(
				"session-1",
				"turn.abort",
				{ mode: "terminal", operator: true },
				{
					confirm: true,
					idempotencyKey: "default-scope-key",
				},
			),
		).toEqual({
			sessionId: "session-1",
			operation: "turn.abort",
			input: { mode: "terminal", operator: true },
			confirm: true,
		});
	});

	test("omits an absent idempotency key for ordinary controls", () => {
		expect(controlRequestFrame("thinking.cycle", {}, { confirm: false })).toEqual({
			type: "control_request",
			operation: "thinking.cycle",
			input: {},
			confirm: false,
		});
	});
});
