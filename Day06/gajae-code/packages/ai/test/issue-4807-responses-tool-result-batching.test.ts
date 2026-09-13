import { describe, expect, it } from "bun:test";
import { convertCodexResponsesMessages } from "../src/providers/openai-codex-responses";
import { streamOpenAIResponses } from "../src/providers/openai-responses";
import { appendResponsesToolResultMessages } from "../src/providers/openai-responses-shared";
import type { AssistantMessage, Context, Model, ToolResultMessage } from "../src/types";

// Fixed synthetic fixtures — no real prompts, secrets, or image payloads.
const PNG_A = "aW1hZ2UtYQ==";
const PNG_B = "aW1hZ2UtYg==";

function makeVisionModel(): Model<"openai-responses"> {
	return {
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
}

function makeAssistant(): AssistantMessage {
	return {
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
}

function makeImageResult(id: string, text: string, data: string): ToolResultMessage {
	return {
		role: "toolResult",
		toolCallId: id,
		toolName: "render_chart",
		content: [
			{ type: "text", text },
			{ type: "image", mimeType: "image/png", data },
		],
		isError: false,
		timestamp: 2,
	};
}

async function captureInput(messages: Context["messages"]): Promise<Array<Record<string, unknown>>> {
	const { promise, resolve } = Promise.withResolvers<Record<string, unknown>>();
	const controller = new AbortController();
	controller.abort();
	streamOpenAIResponses(
		makeVisionModel(),
		{ messages },
		{
			apiKey: "test-key",
			signal: controller.signal,
			onPayload: payload => resolve(payload as Record<string, unknown>),
		},
	);
	const payload = (await promise) as { input: Array<Record<string, unknown>> };
	return payload.input;
}

function isOutputItem(item: Record<string, unknown>): boolean {
	return item.type === "function_call_output" || item.type === "custom_tool_call_output";
}

/**
 * Simulate the Anthropic Messages translation an OpenAI Responses proxy performs:
 * `function_call` items become `tool_use` blocks in one assistant message and the
 * contiguous run of `function_call_output` items following them becomes the
 * `tool_result` blocks of the immediately-next user message. Anthropic requires
 * every `tool_use` id to appear in that user message; a user message interleaved
 * between the outputs splits the run and triggers the deterministic 400 of #4807.
 */
function translateToAnthropic(input: Array<Record<string, unknown>>): {
	ok: boolean;
	unansweredToolUseIds: string[];
} {
	const unansweredToolUseIds: string[] = [];
	let pendingToolUseIds: string[] = [];
	let collectedOutputIds = new Set<string>();
	let turnOpen = false;
	// A proxy groups the contiguous output run into the immediately following
	// user message as `tool_result` blocks. Any boundary item (user message or
	// other non-output item) closes the turn: every pending tool_use must have
	// been answered in the run directly before it, exactly like Anthropic.
	const closeTurn = (): void => {
		for (const id of pendingToolUseIds) {
			if (!collectedOutputIds.has(id)) unansweredToolUseIds.push(id);
		}
		pendingToolUseIds = [];
		collectedOutputIds = new Set();
		turnOpen = false;
	};
	for (const item of input) {
		if (item.type === "function_call" || item.type === "custom_tool_call") {
			pendingToolUseIds.push(item.call_id as string);
			turnOpen = true;
			continue;
		}
		if (isOutputItem(item)) {
			collectedOutputIds.add(item.call_id as string);
			continue;
		}
		if (turnOpen) closeTurn();
	}
	closeTurn();
	return { ok: unansweredToolUseIds.length === 0, unansweredToolUseIds };
}

describe("issue #4807: parallel image tool results keep tool_result adjacency", () => {
	it("keeps both outputs contiguous and collects images into one user message (two parallel calls)", async () => {
		const input = await captureInput([
			{ role: "user", content: "render both", timestamp: 0 },
			makeAssistant(),
			makeImageResult("call_B", "saved b", PNG_B),
			makeImageResult("call_A", "saved a", PNG_A),
		]);

		const callIds = input.filter(i => i.type === "function_call").map(i => i.call_id);
		expect(callIds).toEqual(["call_A", "call_B"]);

		const outputs = input.filter(isOutputItem);
		expect(outputs.map(o => o.call_id)).toEqual(["call_B", "call_A"]);

		const userMessages = input.filter(i => i.role === "user");
		expect(userMessages).toHaveLength(2); // original prompt + collected images

		const imageMessage = userMessages.at(-1) as { content: Array<Record<string, unknown>> };
		expect(imageMessage.content[0]).toMatchObject({ type: "input_text" });
		expect(imageMessage.content.filter(c => c.type === "input_image")).toHaveLength(2);
		// Each image group is labeled with its call id (#4807 attribution).
		expect(imageMessage.content[1]).toMatchObject({ type: "input_text", text: "call_id=call_B" });
		expect(imageMessage.content[2]).toMatchObject({ image_url: `data:image/png;base64,${PNG_B}` });
		expect(imageMessage.content[3]).toMatchObject({ type: "input_text", text: "call_id=call_A" });
		expect(imageMessage.content[4]).toMatchObject({ image_url: `data:image/png;base64,${PNG_A}` });

		// The images land strictly after every output of the turn.
		const lastOutputIndex = Math.max(...outputs.map(o => input.indexOf(o)));
		const imageMessageIndex = input.indexOf(imageMessage);
		expect(imageMessageIndex).toBeGreaterThan(lastOutputIndex);
	});

	it("preserves results arriving in reverse call order with matching ids", async () => {
		const input = await captureInput([
			{ role: "user", content: "render both", timestamp: 0 },
			makeAssistant(),
			makeImageResult("call_B", "saved b", PNG_B),
			makeImageResult("call_A", "saved a", PNG_A),
		]);

		const outputs = input.filter(isOutputItem);
		// Result arrival order is preserved verbatim; ids stay paired.
		expect(outputs.map(o => o.call_id)).toEqual(["call_B", "call_A"]);
		expect(outputs.map(o => o.output)).toEqual(["saved b", "saved a"]);
	});

	it("keeps every function/custom output contiguous before any user image item", async () => {
		const customAssistant: AssistantMessage = {
			...makeAssistant(),
			content: [
				{ type: "toolCall", id: "call_A", name: "render_chart", arguments: { chart: "a" } },
				{
					type: "toolCall",
					id: "call_C",
					name: "apply_patch",
					arguments: { input: "*** Begin Patch" },
					customWireName: "apply_patch",
				},
			],
		};
		const input = await captureInput([
			{ role: "user", content: "run both", timestamp: 0 },
			customAssistant,
			makeImageResult("call_A", "saved a", PNG_A),
			{
				role: "toolResult",
				toolCallId: "call_C",
				toolName: "apply_patch",
				content: [{ type: "text", text: "Done!" }],
				isError: false,
				timestamp: 2,
			} satisfies ToolResultMessage,
		]);

		const types = input.map(i => i.type ?? i.role);
		expect(types).toEqual([
			"user",
			"function_call",
			"custom_tool_call",
			"function_call_output",
			"custom_tool_call_output",
			"user",
		]);
		const outputItems = input.filter(isOutputItem);
		expect(outputItems.map(o => o.call_id)).toEqual(["call_A", "call_C"]);
		// No user-role item separates any two outputs.
		const firstUserAfterOutputs = input.findIndex(
			(item, index) => index > input.indexOf(outputItems[0]!) && item.role === "user",
		);
		for (const output of outputItems) {
			expect(input.indexOf(output)).toBeLessThan(firstUserAfterOutputs);
		}
	});

	it("leaves text-only consecutive tool results unchanged (no image user message)", async () => {
		const input = await captureInput([
			{ role: "user", content: "read both", timestamp: 0 },
			makeAssistant(),
			{
				role: "toolResult",
				toolCallId: "call_B",
				toolName: "read",
				content: [{ type: "text", text: "result b" }],
				isError: false,
				timestamp: 2,
			} satisfies ToolResultMessage,
			{
				role: "toolResult",
				toolCallId: "call_A",
				toolName: "read",
				content: [{ type: "text", text: "result a" }],
				isError: false,
				timestamp: 3,
			} satisfies ToolResultMessage,
			{ role: "user", content: "thanks", timestamp: 4 },
		]);

		const types = input.map(i => i.type ?? i.role);
		expect(types).toEqual([
			"user",
			"function_call",
			"function_call",
			"function_call_output",
			"function_call_output",
			"user",
		]);
		expect(input.filter(i => isOutputItem(i)).map(o => o.output)).toEqual(["result b", "result a"]);
		expect(
			input.some(
				i =>
					i.role === "user" &&
					Array.isArray(i.content) &&
					(i.content as Array<Record<string, unknown>>).some(c => c.type === "input_image"),
			),
		).toBe(false);
	});

	it("keeps a single image-bearing result valid", async () => {
		const singleCallAssistant: AssistantMessage = {
			...makeAssistant(),
			content: [{ type: "toolCall", id: "call_A", name: "render_chart", arguments: { chart: "a" } }],
		};
		const input = await captureInput([
			{ role: "user", content: "render one", timestamp: 0 },
			singleCallAssistant,
			makeImageResult("call_A", "saved a", PNG_A),
		]);

		const types = input.map(i => i.type ?? i.role);
		expect(types).toEqual(["user", "function_call", "function_call_output", "user"]);
		const outputItem = input.find(isOutputItem) as { call_id: string; output: string };
		expect(outputItem.call_id).toBe("call_A");
		expect(outputItem.output).toBe("saved a");
		const imageMessage = input.at(-1) as { content: Array<Record<string, unknown>> };
		expect(imageMessage.content.filter(c => c.type === "input_image")).toHaveLength(1);
		expect(imageMessage.content[1]).toMatchObject({ type: "input_text", text: "call_id=call_A" });
		expect(imageMessage.content[2]).toMatchObject({ image_url: `data:image/png;base64,${PNG_A}` });
	});

	it("no longer produces the deterministic Anthropic 400 on proxy replay", async () => {
		const input = await captureInput([
			{ role: "user", content: "render both", timestamp: 0 },
			makeAssistant(),
			makeImageResult("call_B", "saved b", PNG_B),
			makeImageResult("call_A", "saved a", PNG_A),
		]);

		const { ok, unansweredToolUseIds } = translateToAnthropic(input);
		expect(unansweredToolUseIds).toEqual([]);
		expect(ok).toBe(true);
	});

	it("proxy simulator rejects the legacy interleaved image shape (negative fixture)", () => {
		// The exact pre-#4807 encoder output: a standalone image user message
		// splits the sibling outputs, so the proxy's grouped user message answers
		// only call_B and Anthropic rejects the replay for call_A.
		const legacyInput: Array<Record<string, unknown>> = [
			{ role: "user", content: "render both" },
			{ type: "function_call", call_id: "call_A" },
			{ type: "function_call", call_id: "call_B" },
			{ type: "function_call_output", call_id: "call_B", output: "saved b" },
			{ role: "user", content: [{ type: "input_image", image_url: `data:image/png;base64,${PNG_B}` }] },
			{ type: "function_call_output", call_id: "call_A", output: "saved a" },
			{ role: "user", content: [{ type: "input_image", image_url: `data:image/png;base64,${PNG_A}` }] },
		];
		const { ok, unansweredToolUseIds } = translateToAnthropic(legacyInput);
		expect(ok).toBe(false);
		expect(unansweredToolUseIds).toEqual(["call_A"]);
	});

	it("helper batches multiple results with ids preserved and custom calls intact", () => {
		const messages: unknown[] = [];
		const model = makeVisionModel();
		const knownCallIds = new Set(["call_A", "call_C"]);
		const customCallIds = new Set(["call_C"]);
		appendResponsesToolResultMessages(
			messages as never,
			[
				makeImageResult("call_A", "saved a", PNG_A),
				{
					role: "toolResult",
					toolCallId: "call_C",
					toolName: "apply_patch",
					content: [{ type: "text", text: "patched" }],
					isError: false,
					timestamp: 3,
				} satisfies ToolResultMessage,
			],
			model,
			false,
			knownCallIds,
			customCallIds,
		);

		// The image-bearing batch emits outputs first, then exactly one trailing
		// user message carrying the collected image blocks.
		expect(messages.map(m => (m as { type?: string; role?: string }).type ?? "user")).toEqual([
			"function_call_output",
			"custom_tool_call_output",
			"user",
		]);
		expect((messages[0] as { call_id: string }).call_id).toBe("call_A");
		expect((messages[1] as { call_id: string }).call_id).toBe("call_C");
		expect((messages[1] as { output: string }).output).toBe("patched");
		// Only one trailing user message carries the collected images.
		const userMessages = messages.filter(m => (m as { role?: string }).role === "user");
		expect(userMessages).toHaveLength(1);
		const imageMessage = userMessages[0] as { content: Array<Record<string, unknown>> };
		expect(imageMessage.content.filter(c => c.type === "input_image")).toHaveLength(1);
	});

	it("Codex converter flushes batched outputs before the next user message, not at the end", () => {
		const codexModel = {
			...makeVisionModel(),
			api: "openai-codex-responses",
			provider: "openai-codex",
		} as unknown as Model<"openai-codex-responses">;
		const codexAssistant: AssistantMessage = {
			...makeAssistant(),
			api: "openai-codex-responses",
			provider: "openai-codex",
		};
		const items = convertCodexResponsesMessages(codexModel, {
			messages: [
				{ role: "user", content: "render both", timestamp: 0 },
				codexAssistant,
				makeImageResult("call_B", "saved b", PNG_B),
				makeImageResult("call_A", "saved a", PNG_A),
				{ role: "user", content: "next turn", timestamp: 5 },
			],
		} as Context);

		// Outputs (and the collected image user message) must land directly after
		// the assistant tool-call turn — never behind the following user turn.
		const kinds = items.map(
			item => (item as { type?: string; role?: string }).type ?? (item as { role: string }).role,
		);
		expect(kinds).toEqual([
			"user",
			"function_call",
			"function_call",
			"function_call_output",
			"function_call_output",
			"user", // collected images
			"user", // next turn
		]);
	});
});
