import { describe, expect, it } from "bun:test";
import { getBundledModel } from "../src/models";
import { streamOpenAICompletions } from "../src/providers/openai-completions";
import type { AssistantMessageEvent, Context, FetchImpl, Model } from "../src/types";

const model = getBundledModel("openai", "gpt-4o-mini") as Model<"openai-completions">;
const context: Context = {
	messages: [{ role: "user", content: "hello", timestamp: 1 }],
};

function chunk(
	finishReason: string | null,
	delta: Record<string, unknown> = {},
	usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number },
): Record<string, unknown> {
	return {
		id: `chatcmpl-${finishReason ?? "delta"}`,
		object: "chat.completion.chunk",
		created: 1,
		model: model.id,
		choices: [{ index: 0, delta, finish_reason: finishReason }],
		...(usage ? { usage } : {}),
	};
}

function sse(events: Array<Record<string, unknown> | "[DONE]">): Response {
	return new Response(
		`${events.map(event => `data: ${typeof event === "string" ? event : JSON.stringify(event)}`).join("\n\n")}\n\n`,
		{
			status: 200,
			headers: { "content-type": "text/event-stream" },
		},
	);
}

function sequenceFetch(
	attempts: Array<Array<Record<string, unknown> | "[DONE]">>,
	calls: { count: number },
): FetchImpl {
	const fetchImpl = async (): Promise<Response> => {
		const events = attempts[calls.count];
		calls.count += 1;
		if (!events) throw new Error("Unexpected extra request");
		return sse(events);
	};
	return Object.assign(fetchImpl, { preconnect: fetch.preconnect });
}

async function drainEvents(options: Parameters<typeof streamOpenAICompletions>[2]): Promise<{
	events: AssistantMessageEvent[];
	result: Awaited<ReturnType<ReturnType<typeof streamOpenAICompletions>["result"]>>;
}> {
	const stream = streamOpenAICompletions(model, context, options);
	const events: AssistantMessageEvent[] = [];
	for await (const event of stream) events.push(event);
	return { events, result: await stream.result() };
}

describe("OpenAI completions finish_reason=network_error replay", () => {
	it("retries a replay-safe first chunk and publishes only the successful attempt", async () => {
		const calls = { count: 0 };
		const waits: number[] = [];
		const { events, result } = await drainEvents({
			apiKey: "test-key",
			requestMaxRetries: 0,
			streamMaxRetries: 2,
			providerRetryWait: async delayMs => {
				waits.push(delayMs);
			},
			fetch: sequenceFetch(
				[
					[chunk("network_error", {}, { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 }), "[DONE]"],
					[
						chunk(null, { role: "assistant", content: "ok" }),
						chunk("stop", {}, { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 }),
						"[DONE]",
					],
				],
				calls,
			),
		});

		expect(calls.count).toBe(2);
		expect(waits).toEqual([2000]);
		expect(events.filter(event => event.type === "start")).toHaveLength(1);
		expect(
			events.filter(event => event.type === "text_delta").map(event => event.type === "text_delta" && event.delta),
		).toEqual(["ok"]);
		expect(result.stopReason).toBe("stop");
		expect(result.content).toEqual([{ type: "text", text: "ok" }]);
		expect(result.usage.input).toBe(2);
		expect(result.usage.output).toBe(1);
		expect(result.usage.totalTokens).toBe(3);
		expect(result.usage.cost.input).toBeCloseTo((model.cost.input / 1_000_000) * 2);
		expect(result.usage.cost.output).toBeCloseTo(model.cost.output / 1_000_000);
		expect(result.responseId).toBe("chatcmpl-delta");
	});

	it("does not retry after visible text has been emitted", async () => {
		const calls = { count: 0 };
		const { events, result } = await drainEvents({
			apiKey: "test-key",
			requestMaxRetries: 0,
			streamMaxRetries: 3,
			providerRetryWait: async () => {
				throw new Error("retry wait must not run");
			},
			fetch: sequenceFetch([[chunk(null, { content: "partial" }), chunk("network_error"), "[DONE]"]], calls),
		});

		expect(calls.count).toBe(1);
		expect(events.some(event => event.type === "text_delta")).toBe(true);
		expect(result.stopReason).toBe("error");
		expect(result.content).toEqual([{ type: "text", text: "partial" }]);
		expect(result.errorMessage).toContain("Provider finish_reason: network_error");
	});

	it("does not retry after a tool-call delta has been emitted", async () => {
		const calls = { count: 0 };
		const { result } = await drainEvents({
			apiKey: "test-key",
			requestMaxRetries: 0,
			streamMaxRetries: 3,
			providerRetryWait: async () => {
				throw new Error("retry wait must not run");
			},
			fetch: sequenceFetch(
				[
					[
						chunk(null, {
							tool_calls: [
								{ index: 0, id: "call-1", type: "function", function: { name: "read", arguments: "{" } },
							],
						}),
						chunk("network_error"),
						"[DONE]",
					],
				],
				calls,
			),
		});

		expect(calls.count).toBe(1);
		expect(result.stopReason).toBe("error");
		expect(result.content[0]).toMatchObject({ type: "toolCall", id: "call-1", name: "read" });
	});

	it("stops after the configured retry budget is exhausted", async () => {
		const calls = { count: 0 };
		const waits: number[] = [];
		const failure = [chunk("network_error"), "[DONE]"] as Array<Record<string, unknown> | "[DONE]">;
		const { result } = await drainEvents({
			apiKey: "test-key",
			requestMaxRetries: 0,
			streamMaxRetries: 2,
			providerRetryWait: async delayMs => {
				waits.push(delayMs);
			},
			fetch: sequenceFetch([failure, failure, failure], calls),
		});

		expect(calls.count).toBe(3);
		expect(waits).toEqual([2000, 4000]);
		expect(result.stopReason).toBe("error");
		expect(result.errorMessage).toContain("Provider finish_reason: network_error");
	});

	it("honors caller cancellation during retry backoff", async () => {
		const calls = { count: 0 };
		const controller = new AbortController();
		const { result } = await drainEvents({
			apiKey: "test-key",
			requestMaxRetries: 0,
			streamMaxRetries: 3,
			signal: controller.signal,
			providerRetryWait: async (_delayMs, signal) => {
				expect(signal).toBe(controller.signal);
				controller.abort();
			},
			fetch: sequenceFetch([[chunk("network_error"), "[DONE]"]], calls),
		});

		expect(calls.count).toBe(1);
		expect(result.stopReason).toBe("aborted");
		expect(result.errorMessage).toContain("Request was aborted");
	});

	it("leaves replay and exact error reporting to managed fallback", async () => {
		const calls = { count: 0 };
		const { result } = await drainEvents({
			apiKey: "test-key",
			requestMaxRetries: 0,
			streamMaxRetries: 3,
			fallbackManaged: true,
			providerRetryWait: async () => {
				throw new Error("retry wait must not run");
			},
			fetch: sequenceFetch([[chunk("network_error"), "[DONE]"]], calls),
		});

		expect(calls.count).toBe(1);
		expect(result.stopReason).toBe("error");
		expect(result.errorMessage).toContain("Provider finish_reason: network_error");
	});

	it("does not broaden retry classification to provider-specific finish reason variants", async () => {
		const calls = { count: 0 };
		const { result } = await drainEvents({
			apiKey: "test-key",
			requestMaxRetries: 0,
			streamMaxRetries: 3,
			providerRetryWait: async () => {
				throw new Error("retry wait must not run");
			},
			fetch: sequenceFetch([[chunk("network-error"), "[DONE]"]], calls),
		});

		expect(calls.count).toBe(1);
		expect(result.stopReason).toBe("error");
		expect(result.errorMessage).toContain("Provider finish_reason: network-error");
	});
});
