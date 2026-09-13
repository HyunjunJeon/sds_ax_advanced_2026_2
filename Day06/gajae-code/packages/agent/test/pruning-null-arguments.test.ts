import { describe, expect, it } from "bun:test";
import type { ToolResultMessage } from "@gajae-code/ai";
import type { SessionEntry, SessionMessageEntry } from "../src/compaction/entries";
import { DEFAULT_PRUNE_CONFIG, pruneAssistantToolArguments } from "../src/compaction/pruning";
import { applyToolOutputPrune as pruneToolOutputs } from "./pruning-test-utils";

/**
 * A persisted assistant `toolCall` block can carry `arguments: null`. Live
 * sessions on disk contain thousands of them, every one paired with a
 * `message.content.N.arguments` cold-spill payload ref: the eviction pass moved
 * the real arguments to a blob and the sentinel that should have replaced them
 * did not survive persistence. Reloading such a session must not crash the
 * pruning pass — `args.path` on a null `arguments` throws a TypeError that
 * surfaces to the user as `null is not an object (evaluating 'args.path')` and
 * kills the turn.
 */

let idCounter = 0;

function assistantCallEntry(callId: string, toolName: string, args: unknown): SessionEntry {
	idCounter++;
	return {
		type: "message",
		id: `a-${idCounter}`,
		parentId: null,
		timestamp: new Date(idCounter).toISOString(),
		message: {
			role: "assistant",
			content: [{ type: "toolCall", id: callId, name: toolName, arguments: args }],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "m",
			stopReason: "toolUse",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			timestamp: idCounter,
		},
	} as SessionEntry;
}

function toolResultEntry(callId: string, toolName: string, sizeChars = 8000, isError = false): SessionMessageEntry {
	idCounter++;
	return {
		type: "message",
		id: `r-${idCounter}`,
		parentId: null,
		timestamp: new Date(idCounter).toISOString(),
		message: {
			role: "toolResult",
			toolCallId: callId,
			toolName,
			content: [{ type: "text", text: `result-${callId} ${"x ".repeat(Math.floor(sizeChars / 2))}` }],
			isError,
			timestamp: idCounter,
		} as ToolResultMessage,
	} as SessionMessageEntry;
}

function pair(
	entries: SessionEntry[],
	callId: string,
	toolName: string,
	args: unknown,
	sizeChars = 8000,
): SessionMessageEntry {
	entries.push(assistantCallEntry(callId, toolName, args));
	const result = toolResultEntry(callId, toolName, sizeChars);
	entries.push(result);
	return result;
}

describe("pruning tolerates persisted null tool arguments", () => {
	it("does not throw when an edit-class call carries null arguments", () => {
		const entries: SessionEntry[] = [];
		pair(entries, "c1", "write", null);
		expect(() => pruneAssistantToolArguments(entries, { ...DEFAULT_PRUNE_CONFIG, protectTokens: 0 })).not.toThrow();
	});

	it("does not throw when tool-output pruning indexes a null-argument call", () => {
		const entries: SessionEntry[] = [];
		pair(entries, "c1", "write", null);
		pair(entries, "c2", "bash", null);
		pair(entries, "c3", "search", null);
		expect(() =>
			pruneToolOutputs(entries, {
				protectTokens: 0,
				minimumSavings: 0,
				protectedTools: ["skill", "read"],
				staleOverridableTools: ["read"],
			}),
		).not.toThrow();
	});

	it("still prunes a real stale edit when a null-argument call sits in the same history", () => {
		const entries: SessionEntry[] = [];
		pair(entries, "c0", "write", null);
		const staleEdit = assistantCallEntry("c1", "edit", {
			path: "src/a.ts",
			input: "x".repeat(2_000),
		});
		entries.push(staleEdit, toolResultEntry("c1", "edit", 100));
		pair(entries, "c2", "write", { path: "src/a.ts", content: "new" }, 100);

		const result = pruneAssistantToolArguments(entries, {
			protectTokens: 0,
			minimumSavings: 0,
			protectedTools: ["skill", "read"],
			staleOverridableTools: ["read"],
		});

		expect(result.argumentPrunedCount).toBe(1);
		expect(result.prunedEntries.map(entry => entry.id)).toEqual([staleEdit.id]);
	});
});
