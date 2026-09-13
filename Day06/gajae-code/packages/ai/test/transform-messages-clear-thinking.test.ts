import { describe, expect, it } from "bun:test";
import { transformMessages } from "@gajae-code/ai/providers/transform-messages";
import type { AssistantMessage, Message, Model, ToolResultMessage, UserMessage } from "@gajae-code/ai/types";

// ---------------------------------------------------------------------------
// Issue #4247: replayed thinking blocks emptied by clear_thinking_20251015
// keep their stale signature and 400 every request. These tests verify the
// transform-messages layer drops signed-empty thinking for anthropic-messages
// replay while preserving it for non-Anthropic APIs (OpenAI encrypted reasoning).
// ---------------------------------------------------------------------------

const anthropicModel: Model<"anthropic-messages"> = {
	api: "anthropic-messages",
	provider: "anthropic",
	id: "claude-opus-5",
	name: "Claude Opus 5",
	baseUrl: "https://api.anthropic.com",
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	maxTokens: 8_192,
	contextWindow: 200_000,
	reasoning: true,
};

const openaiResponsesModel: Model<"openai-responses"> = {
	api: "openai-responses",
	provider: "openai",
	id: "o3",
	name: "o3",
	baseUrl: "https://api.openai.com",
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	maxTokens: 8_192,
	contextWindow: 200_000,
	reasoning: true,
};

const usage = {
	input: 0,
	output: 0,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 0,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

const user: UserMessage = { role: "user", content: "go", timestamp: Date.now() };

function assistantTurn(
	content: AssistantMessage["content"],
	activeModel: Model<AssistantMessage["api"]>,
): AssistantMessage {
	return {
		role: "assistant",
		content: [...content, { type: "toolCall", id: "toolu_1", name: "bash", arguments: { command: "echo hi" } }],
		api: activeModel.api,
		provider: activeModel.provider,
		model: activeModel.id,
		usage,
		stopReason: "toolUse",
		timestamp: Date.now(),
	};
}

function toolResult(): ToolResultMessage {
	return {
		role: "toolResult",
		toolCallId: "toolu_1",
		toolName: "bash",
		content: [{ type: "text", text: "hi" }],
		isError: false,
		timestamp: Date.now(),
	} as ToolResultMessage;
}

const SIGNED_FULL = { type: "thinking" as const, thinking: "real reasoning", thinkingSignature: "sig_full" };
const SIGNED_EMPTY = { type: "thinking" as const, thinking: "", thinkingSignature: "sig_empty" };
const UNSIGNED_EMPTY = { type: "thinking" as const, thinking: "", thinkingSignature: undefined };
const UNSIGNED_FULL = { type: "thinking" as const, thinking: "unsigned reasoning", thinkingSignature: undefined };

function thinkingBlocks(messages: Message[]): Array<{ thinking: string; signature?: string }> {
	const blocks: Array<{ thinking: string; signature?: string }> = [];
	for (const msg of messages) {
		if (msg.role !== "assistant") continue;
		for (const block of msg.content) {
			if (block.type === "thinking") {
				blocks.push({ thinking: block.thinking, signature: block.thinkingSignature });
			}
		}
	}
	return blocks;
}

describe("transform-messages clear_thinking signed-empty blocks (#4247)", () => {
	it("drops signed-empty thinking for anthropic-messages historical replay", () => {
		const messages: Message[] = [
			user,
			assistantTurn([SIGNED_EMPTY], anthropicModel),
			toolResult(),
			{ ...user, content: "again", timestamp: Date.now() + 1 },
			assistantTurn([SIGNED_FULL], anthropicModel),
			toolResult(),
		];

		const result = transformMessages(messages, anthropicModel);
		const blocks = thinkingBlocks(result);

		// Historical signed-empty dropped; latest signed-full preserved.
		expect(blocks).toHaveLength(1);
		expect(blocks[0].signature).toBe("sig_full");
	});

	it("preserves signed-empty thinking for openai-responses replay (encrypted reasoning)", () => {
		const messages: Message[] = [
			user,
			assistantTurn([SIGNED_EMPTY], openaiResponsesModel),
			toolResult(),
			{ ...user, content: "again", timestamp: Date.now() + 1 },
			assistantTurn([SIGNED_FULL], openaiResponsesModel),
			toolResult(),
		];

		const result = transformMessages(messages, openaiResponsesModel);
		const blocks = thinkingBlocks(result);

		// Both preserved: OpenAI encrypted reasoning allows signed-empty blocks.
		expect(blocks).toHaveLength(2);
		expect(blocks.some(b => b.signature === "sig_empty")).toBe(true);
		expect(blocks.some(b => b.signature === "sig_full")).toBe(true);
	});

	it("preserves valid signed thinking with non-empty text for anthropic-messages", () => {
		const messages: Message[] = [user, assistantTurn([SIGNED_FULL], anthropicModel), toolResult()];

		const result = transformMessages(messages, anthropicModel);
		const blocks = thinkingBlocks(result);

		expect(blocks).toHaveLength(1);
		expect(blocks[0].thinking).toBe("real reasoning");
		expect(blocks[0].signature).toBe("sig_full");
	});

	it("drops unsigned-empty thinking for anthropic-messages historical replay", () => {
		const messages: Message[] = [
			user,
			assistantTurn([UNSIGNED_EMPTY], anthropicModel),
			toolResult(),
			{ ...user, content: "again", timestamp: Date.now() + 1 },
			assistantTurn([SIGNED_FULL], anthropicModel),
			toolResult(),
		];

		const result = transformMessages(messages, anthropicModel);
		// Historical unsigned-empty is dropped; only latest SIGNED_FULL survives.
		expect(thinkingBlocks(result)).toHaveLength(1);
	});

	it("converts unsigned non-empty thinking to text for cross-model anthropic replay", () => {
		const messages: Message[] = [user, assistantTurn([UNSIGNED_FULL], openaiResponsesModel), toolResult()];

		const result = transformMessages(messages, anthropicModel);
		const blocks = thinkingBlocks(result);

		// Cross-model: unsigned thinking degrades to text, no native thinking block.
		expect(blocks).toHaveLength(0);
		expect(
			result.some(
				msg =>
					msg.role === "assistant" &&
					msg.content.some(b => b.type === "text" && (b as { text: string }).text === "unsigned reasoning"),
			),
		).toBe(true);
	});

	it("drops all signed-empty blocks across multiple historical turns for anthropic-messages", () => {
		const messages: Message[] = [
			user,
			assistantTurn([SIGNED_EMPTY], anthropicModel),
			toolResult(),
			{ ...user, content: "turn 2", timestamp: Date.now() + 1 },
			assistantTurn([SIGNED_EMPTY, SIGNED_FULL], anthropicModel),
			toolResult(),
			{ ...user, content: "turn 3", timestamp: Date.now() + 2 },
			assistantTurn([SIGNED_FULL], anthropicModel),
			toolResult(),
		];

		const result = transformMessages(messages, anthropicModel);
		const blocks = thinkingBlocks(result);

		// Two SIGNED_FULL blocks survive (one historical, one latest); two SIGNED_EMPTY dropped.
		expect(blocks).toHaveLength(2);
		expect(blocks.every(b => b.signature === "sig_full")).toBe(true);
		expect(blocks.some(b => b.signature === "sig_empty")).toBe(false);
	});

	it("handles whitespace-only signed thinking as empty for anthropic-messages historical replay", () => {
		const whitespaceSigned = { type: "thinking" as const, thinking: "   \n\t  ", thinkingSignature: "sig_ws" };
		const messages: Message[] = [
			user,
			assistantTurn([whitespaceSigned], anthropicModel),
			toolResult(),
			{ ...user, content: "again", timestamp: Date.now() + 1 },
			assistantTurn([SIGNED_FULL], anthropicModel),
			toolResult(),
		];

		const result = transformMessages(messages, anthropicModel);
		// Whitespace-only signed block dropped as empty; latest SIGNED_FULL survives.
		expect(thinkingBlocks(result)).toHaveLength(1);
		expect(thinkingBlocks(result)[0].signature).toBe("sig_full");
	});

	it("preserves whitespace-only signed thinking for openai-responses (encrypted reasoning)", () => {
		const whitespaceSigned = { type: "thinking" as const, thinking: "   \n\t  ", thinkingSignature: "sig_ws" };
		const messages: Message[] = [user, assistantTurn([whitespaceSigned], openaiResponsesModel), toolResult()];

		const result = transformMessages(messages, openaiResponsesModel);
		const blocks = thinkingBlocks(result);

		expect(blocks).toHaveLength(1);
		expect(blocks[0].signature).toBe("sig_ws");
	});
});
