import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as path from "node:path";
import { Agent, type AgentOptions } from "@gajae-code/agent-core";
import { type AssistantMessage, getBundledModel, type Model } from "@gajae-code/ai";
import { AssistantMessageEventStream } from "@gajae-code/ai/utils/event-stream";
import { ModelRegistry } from "@gajae-code/coding-agent/config/model-registry";
import { Settings } from "@gajae-code/coding-agent/config/settings";
import { AgentSession, type AgentSessionEvent } from "@gajae-code/coding-agent/session/agent-session";
import { AuthStorage } from "@gajae-code/coding-agent/session/auth-storage";
import { SessionManager } from "@gajae-code/coding-agent/session/session-manager";
import { TempDir } from "@gajae-code/utils";

const RETRY_AFTER_MS = 120_000;
const RAW_QUOTA_ERROR = "The usage limit has been reached (code=usage_limit_reached)";

type AutoRetryStartEvent = Extract<AgentSessionEvent, { type: "auto_retry_start" }>;

function quotaStream(model: Model): AssistantMessageEventStream {
	const stream = new AssistantMessageEventStream();
	queueMicrotask(() => {
		const message: AssistantMessage = {
			role: "assistant",
			content: [],
			api: model.api,
			provider: model.provider,
			model: model.id,
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "error",
			errorMessage: RAW_QUOTA_ERROR,
			errorStatus: 429,
			timestamp: Date.now(),
			transportFailure: {
				kind: "transport",
				status: 429,
				providerCode: "usage_limit_reached",
				headers: { "retry-after-ms": String(RETRY_AFTER_MS) },
			},
		};
		stream.push({ type: "start", partial: message });
		stream.push({ type: "error", reason: "error", error: message });
	});
	return stream;
}

function lastAssistant(session: AgentSession): AssistantMessage {
	const message = session.agent.state.messages.at(-1);
	if (message?.role !== "assistant") {
		throw new Error("Expected trailing assistant message");
	}
	return message as AssistantMessage;
}

/**
 * When every stored credential is quota-blocked, AuthStorage already computed
 * the unblock instant. The turn must surface that as a "retryable at" signal
 * on the terminal provider error without sleeping until it.
 */
describe("issue #4908 quota exhaustion retryable-at signal", () => {
	let tempDir: TempDir;
	let authStorage: AuthStorage;
	let session: AgentSession | undefined;

	beforeEach(async () => {
		tempDir = TempDir.createSync("@issue-4908-quota-retryable-at-");
		authStorage = await AuthStorage.create(path.join(tempDir.path(), "auth.db"));
		await authStorage.set("anthropic", [{ type: "api_key", key: "anthropic-quota-key" }]);
	});

	afterEach(async () => {
		await session?.dispose();
		authStorage.close();
		tempDir.removeSync();
		vi.restoreAllMocks();
	});

	it("keeps the already-computed unblock instant on the terminal quota error", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5");
		if (!model) throw new Error("Expected bundled Anthropic test model");

		const agent = new Agent({
			getApiKey: provider => `${provider}-test-key`,
			initialState: { model, systemPrompt: ["Test"], tools: [], messages: [] },
			streamFn: ((requested, _context, _options) => quotaStream(requested)) satisfies AgentOptions["streamFn"],
		});
		const settings = Settings.isolated({
			"compaction.enabled": false,
		});
		session = new AgentSession({
			agent,
			sessionManager: SessionManager.inMemory(),
			settings,
			modelRegistry: new ModelRegistry(authStorage),
		});

		const retryStartEvents: AutoRetryStartEvent[] = [];
		session.subscribe(event => {
			if (event.type === "auto_retry_start") retryStartEvents.push(event);
		});

		const before = Date.now();
		await session.prompt("quota exhaustion must keep retryable-at");
		await session.waitForIdle();
		const after = Date.now();

		const message = lastAssistant(session);
		expect(message.stopReason).toBe("error");
		expect(message.errorMessage).toContain(RAW_QUOTA_ERROR);
		expect(message.errorMessage).toMatch(/retryable at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z/);
		const stamped = /retryable at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)/.exec(message.errorMessage ?? "");
		expect(stamped?.[1]).toBeDefined();
		const retryableAt = Date.parse(stamped![1]!);
		expect(retryableAt).toBeGreaterThanOrEqual(before + RETRY_AFTER_MS - 250);
		expect(retryableAt).toBeLessThanOrEqual(after + RETRY_AFTER_MS + 250);
		expect(retryStartEvents).toEqual([]);
	});
});
