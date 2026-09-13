import { describe, expect, it } from "bun:test";
import { streamOpenAIResponses } from "../src/providers/openai-responses";
import type { AssistantMessage, Context, Model, ToolResultMessage } from "../src/types";

/**
 * Proxy wire-shape pin for issue #4807.
 *
 * An OpenAI Responses → Anthropic Messages proxy groups consecutive
 * `function_call_output` items following a `function_call` batch into the
 * single user message carrying the paired `tool_result` blocks. Anthropic
 * rejects the request unless every `tool_use` id is answered in that
 * immediately-following user message. This test pins the exact wire shape the
 * proxy receives so any regression back to per-result interleaved image user
 * messages fails loudly.
 */

const PNG_A = "aW1hZ2UtYQ==";
const PNG_B = "aW1hZ2UtYg==";

const model: Model<"openai-responses"> = {
	id: "gpt-5",
	name: "GPT-5",
	api: "openai-responses",
	provider: "custom-proxy",
	baseUrl: "https://proxy.example.com/v1",
	input: ["text", "image"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	maxTokens: 8192,
	contextWindow: 200_000,
	reasoning: false,
} as Model<"openai-responses">;

const assistant: AssistantMessage = {
	role: "assistant",
	content: [
		{ type: "toolCall", id: "call_A", name: "render_chart", arguments: { chart: "a" } },
		{ type: "toolCall", id: "call_B", name: "render_chart", arguments: { chart: "b" } },
	],
	api: "openai-responses",
	provider: "custom-proxy",
	model: "gpt-5",
	usage: {
		input: 1,
		output: 1,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 2,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	},
	stopReason: "toolUse",
	timestamp: 1,
};

const imageResult = (id: string, text: string, data: string): ToolResultMessage => ({
	role: "toolResult",
	toolCallId: id,
	toolName: "render_chart",
	content: [
		{ type: "text", text },
		{ type: "image", mimeType: "image/png", data },
	],
	isError: false,
	timestamp: 2,
});

describe("issue #4807 proxy wire shape", () => {
	it("emits the proxy-safe Responses input shape for parallel image tool results", async () => {
		const context: Context = {
			messages: [
				{ role: "user", content: "render both", timestamp: 0 },
				assistant,
				imageResult("call_B", "saved b", PNG_B),
				imageResult("call_A", "saved a", PNG_A),
			],
		};

		const { promise, resolve } = Promise.withResolvers<Record<string, unknown>>();
		const controller = new AbortController();
		controller.abort();
		streamOpenAIResponses(model, context, {
			apiKey: "test-key",
			signal: controller.signal,
			onPayload: payload => resolve(payload as Record<string, unknown>),
		});
		const payload = (await promise) as { input: Array<Record<string, unknown>> };

		// Exact item sequence: prompt, both calls, both outputs, one collected image message.
		const shape = payload.input.map(item => item.type ?? item.role);
		expect(shape).toEqual([
			"user",
			"function_call",
			"function_call",
			"function_call_output",
			"function_call_output",
			"user",
		]);

		// Outputs stay paired with their call ids, in result arrival order.
		expect(payload.input[3]).toMatchObject({ type: "function_call_output", call_id: "call_B", output: "saved b" });
		expect(payload.input[4]).toMatchObject({ type: "function_call_output", call_id: "call_A", output: "saved a" });

		// One user message after all outputs, carrying every image in result
		// order, each group labeled with its call id (#4807 attribution).
		expect(payload.input[5]).toMatchObject({
			role: "user",
			content: [
				{ type: "input_text", text: "Attached image(s) from tool result:" },
				{ type: "input_text", text: "call_id=call_B" },
				{ type: "input_image", detail: "auto", image_url: `data:image/png;base64,${PNG_B}` },
				{ type: "input_text", text: "call_id=call_A" },
				{ type: "input_image", detail: "auto", image_url: `data:image/png;base64,${PNG_A}` },
			],
		});

		// The exact Anthropic adjacency invariant the proxy must be able to uphold:
		// every tool_use id is answered by the user message immediately after the
		// contiguous output run.
		const answeredIds = new Set(
			payload.input.filter(i => i.type === "function_call_output").map(i => i.call_id as string),
		);
		const toolUseIds = payload.input.filter(i => i.type === "function_call").map(i => i.call_id as string);
		expect([...answeredIds].sort()).toEqual([...toolUseIds].sort());
		const firstOutputIndex = payload.input.findIndex(i => i.type === "function_call_output");
		const lastOutputIndex = payload.input.map(i => i.type).lastIndexOf("function_call_output");
		for (let index = firstOutputIndex + 1; index < lastOutputIndex; index++) {
			expect(payload.input[index]!.role).not.toBe("user");
		}
	});
});
