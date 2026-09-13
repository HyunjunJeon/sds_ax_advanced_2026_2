import { afterEach, describe, expect, it, vi } from "bun:test";
import { logger } from "@gajae-code/utils";
import { AsyncJobManager, type SubagentRecord } from "../../src/async";
import { Settings } from "../../src/config/settings";
import { mapAgentSessionEventToAcpSessionUpdates } from "../../src/modes/acp/acp-event-mapper";
import type { AgentSessionEvent } from "../../src/session/agent-session";
import type { ToolSession } from "../../src/tools";
import { SubagentTool } from "../../src/tools/subagent";

function createSession(): ToolSession {
	return {
		cwd: "/tmp",
		hasUI: false,
		settings: Settings.isolated({}),
		getSessionFile: () => null,
		getSessionSpawns: () => "*",
		getAgentId: () => "0-Main",
	} as ToolSession;
}

function createManager(): AsyncJobManager {
	const manager = new AsyncJobManager({ onJobComplete: async () => {}, retentionMs: 10_000 });
	AsyncJobManager.setInstance(manager);
	return manager;
}

function runningRecord(subagentId: string, jobId: string): SubagentRecord {
	return {
		subagentId,
		ownerId: "0-Main",
		currentJobId: jobId,
		historicalJobIds: [],
		status: "running",
		sessionFile: null,
		resumable: false,
	};
}

function addRunningChild(
	manager: AsyncJobManager,
	subagentId: string,
): { jobId: string; gate: PromiseWithResolvers<string> } {
	const gate = Promise.withResolvers<string>();
	const jobId = manager.register("task", subagentId, async () => gate.promise, {
		id: `job-${subagentId}`,
		ownerId: "0-Main",
		metadata: { subagent: { id: subagentId, agent: "executor", agentSource: "bundled" } },
	});
	manager.registerSubagentRecord(runningRecord(subagentId, jobId));
	return { jobId, gate };
}

describe("subagent await progress consumer failure", () => {
	afterEach(() => {
		vi.useRealTimers();
		AsyncJobManager.resetForTests();
	});

	it("contains a throwing consumer on the liveness timer instead of escaping it", async () => {
		vi.useFakeTimers();
		const manager = createManager();
		const tool = new SubagentTool(createSession());
		const { jobId, gate } = addRunningChild(manager, "0-ThrowOnTick");
		const observer = vi.fn();
		// Containment is only honest if the failure is still reported, so the
		// diagnostic is part of the contract rather than an implementation detail.
		const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
		const controller = new AbortController();
		const awaiting = tool.execute(
			"await-throw-on-tick",
			{ action: "await", ids: ["0-ThrowOnTick"], timeout_ms: 3_600_000, heartbeat_ms: 500 },
			controller.signal,
			() => {
				observer();
				// Fail on a timer tick, not on the synchronous initial emission.
				if (observer.mock.calls.length === 2) throw new Error("consumer failed");
			},
		);

		try {
			await Promise.resolve();
			// A timer callback runs outside the wait's promise chain. Without
			// containment at the emission site the throw escapes `setInterval`
			// as an uncaught exception and the interval keeps rethrowing.
			expect(() => vi.advanceTimersByTime(500 * 40)).not.toThrow();
			expect(observer).toHaveBeenCalledTimes(2);
			expect(warn).toHaveBeenCalledTimes(1);
			expect(warn.mock.calls[0]?.[0]).toContain("streaming disabled");
			expect(String(warn.mock.calls[0]?.[1]?.error)).toContain("consumer failed");

			// The wait is untouched and still owns the child's delivery.
			expect(manager.getJob(jobId)?.status).toBe("running");
			controller.abort();
			await awaiting;
			// And it still releases everything on the way out.
			expect(manager.getJob(jobId)?.status).toBe("running");
			expect(manager.isDeliverySuppressed(jobId, manager.getJob(jobId)?.generation)).toBe(false);
		} finally {
			controller.abort();
			gate.resolve("done");
			await awaiting.catch(() => {});
			await manager.getJob(jobId)?.promise;
			await manager.dispose({ timeoutMs: 100 });
			warn.mockRestore();
		}
	});

	it("contains a throwing consumer on the initial emission and still releases the wait", async () => {
		vi.useFakeTimers();
		const manager = createManager();
		const tool = new SubagentTool(createSession());
		const { jobId, gate } = addRunningChild(manager, "0-ThrowOnInitial");
		const observer = vi.fn(() => {
			throw new Error("initial consumer failed");
		});
		const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});

		// The initial emission runs before the wait's guarded span, so an
		// uncontained throw here leaks the timer, the watch registration and the
		// terminal-wait handle all at once.
		const awaiting = tool.execute(
			"await-throw-on-initial",
			{ action: "await", ids: ["0-ThrowOnInitial"], timeout_ms: 5_000 },
			undefined,
			observer,
		);
		await Promise.resolve();
		vi.advanceTimersByTime(5_000);
		const result = await awaiting;

		expect(result.details?.awaitOutcome).toBe("timed_out");
		vi.advanceTimersByTime(180_000);
		expect(observer).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(manager.getJob(jobId)?.status).toBe("running");
		expect(manager.isDeliverySuppressed(jobId, manager.getJob(jobId)?.generation)).toBe(false);

		gate.resolve("done");
		await manager.getJob(jobId)?.promise;
		await manager.dispose({ timeoutMs: 100 });
		warn.mockRestore();
	});

	it("contains a rejected initial emission without affecting the wait", async () => {
		vi.useFakeTimers();
		const manager = createManager();
		const tool = new SubagentTool(createSession());
		const { jobId, gate } = addRunningChild(manager, "0-RejectOnInitial");
		const observer = vi.fn(() => Promise.reject(new Error("initial rejection")));
		const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
		const controller = new AbortController();
		const awaiting = tool.execute(
			"await-reject-on-initial",
			{ action: "await", ids: ["0-RejectOnInitial"], timeout_ms: 3_600_000, heartbeat_ms: 500 },
			controller.signal,
			observer,
		);

		await Promise.resolve();
		await Promise.resolve();
		vi.advanceTimersByTime(60_000);
		expect(observer).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(warn.mock.calls[0]?.[1]?.error).toBe("initial rejection");
		expect(manager.getJob(jobId)?.status).toBe("running");

		controller.abort();
		await awaiting;
		gate.resolve("done");
		await manager.getJob(jobId)?.promise;
		await manager.dispose({ timeoutMs: 100 });
		warn.mockRestore();
	});

	it("contains a rejected timer emission and stops later progress", async () => {
		vi.useFakeTimers();
		const manager = createManager();
		const tool = new SubagentTool(createSession());
		const { jobId, gate } = addRunningChild(manager, "0-RejectOnTick");
		let calls = 0;
		const observer = vi.fn(() => {
			calls += 1;
			return calls === 2 ? Promise.reject(new Error("timer rejection")) : undefined;
		});
		const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
		const controller = new AbortController();
		const awaiting = tool.execute(
			"await-reject-on-tick",
			{ action: "await", ids: ["0-RejectOnTick"], timeout_ms: 3_600_000, heartbeat_ms: 500 },
			controller.signal,
			observer,
		);

		await Promise.resolve();
		vi.advanceTimersByTime(15_000);
		await Promise.resolve();
		await Promise.resolve();
		vi.advanceTimersByTime(60_000);
		expect(observer).toHaveBeenCalledTimes(2);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(warn.mock.calls[0]?.[1]?.error).toBe("timer rejection");

		controller.abort();
		await awaiting;
		gate.resolve("done");
		await manager.getJob(jobId)?.promise;
		await manager.dispose({ timeoutMs: 100 });
		warn.mockRestore();
	});

	it("contains a hostile non-stringifiable thrown value", async () => {
		vi.useFakeTimers();
		const manager = createManager();
		const tool = new SubagentTool(createSession());
		const { jobId, gate } = addRunningChild(manager, "0-HostileThrow");
		const hostile = Object.create(null) as { toString?: () => string };
		hostile.toString = () => {
			throw new Error("formatting escaped");
		};
		const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
		const controller = new AbortController();
		const awaiting = tool.execute(
			"await-hostile-throw",
			{ action: "await", ids: ["0-HostileThrow"], timeout_ms: 3_600_000 },
			controller.signal,
			() => {
				throw hostile;
			},
		);

		await Promise.resolve();
		expect(warn).toHaveBeenCalledTimes(1);
		expect(warn.mock.calls[0]?.[1]?.error).toBe("[unprintable thrown value]");
		expect(manager.getJob(jobId)?.status).toBe("running");

		controller.abort();
		await awaiting;
		gate.resolve("done");
		await manager.getJob(jobId)?.promise;
		await manager.dispose({ timeoutMs: 100 });
		warn.mockRestore();
	});

	it("preserves sibling and future waits after one consumer rejects", async () => {
		const delivered: string[] = [];
		const manager = new AsyncJobManager({
			onJobComplete: async jobId => {
				delivered.push(jobId);
			},
			retentionMs: 10_000,
		});
		AsyncJobManager.setInstance(manager);
		const tool = new SubagentTool(createSession());
		const first = addRunningChild(manager, "0-SharedWait");
		const failedController = new AbortController();
		const siblingUpdates = vi.fn();
		const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
		const failedAwait = tool.execute(
			"await-shared-failed",
			{ action: "await", ids: ["0-SharedWait"], timeout_ms: 3_600_000 },
			failedController.signal,
			() => Promise.reject(new Error("isolated rejection")),
		);
		const siblingAwait = tool.execute(
			"await-shared-sibling",
			{ action: "await", ids: ["0-SharedWait"], timeout_ms: 3_600_000 },
			undefined,
			siblingUpdates,
		);

		await Promise.resolve();
		await Promise.resolve();
		failedController.abort();
		await failedAwait;
		expect(manager.isDeliverySuppressed(first.jobId, manager.getJob(first.jobId)?.generation)).toBe(true);
		first.gate.resolve("shared done");
		const siblingResult = await siblingAwait;
		expect(siblingResult.details?.awaitOutcome).toBe("completed");
		expect(delivered).toEqual([]);
		expect(siblingUpdates).toHaveBeenCalled();

		const future = addRunningChild(manager, "0-FutureWait");
		const futureUpdates = vi.fn();
		const futureAwait = tool.execute(
			"await-future",
			{ action: "await", ids: ["0-FutureWait"], timeout_ms: 3_600_000 },
			undefined,
			futureUpdates,
		);
		future.gate.resolve("future done");
		const futureResult = await futureAwait;
		expect(futureResult.details?.awaitOutcome).toBe("completed");
		expect(futureUpdates).toHaveBeenCalled();
		expect(delivered).toEqual([]);
		expect(warn).toHaveBeenCalledTimes(1);

		await manager.dispose({ timeoutMs: 100 });
		warn.mockRestore();
	});

	it("streams a readable summary so ACP does not fall back to a serialized receipt", async () => {
		const manager = createManager();
		const tool = new SubagentTool(createSession());
		const { jobId, gate } = addRunningChild(manager, "0-AcpText");
		const updates: Array<{ content: Array<{ type: string; text?: string }> }> = [];
		const awaiting = tool.execute(
			"await-acp-text",
			{ action: "await", ids: ["0-AcpText"], timeout_ms: 50 },
			undefined,
			update => updates.push(update as { content: Array<{ type: string; text?: string }> }),
		);
		const partialResult = updates[0];
		await awaiting;

		const streamed = partialResult?.content.find(part => part.type === "text");
		expect(streamed?.text).toContain("0-AcpText");

		const notifications = mapAgentSessionEventToAcpSessionUpdates(
			{
				type: "tool_execution_update",
				toolCallId: "call-1",
				toolName: "subagent",
				args: { action: "await", ids: ["0-AcpText"] },
				partialResult,
			} as AgentSessionEvent,
			"session-1",
		);
		const update = notifications[0]?.update as { content?: Array<{ type: string; content?: { text?: string } }> };
		const texts = (update.content ?? [])
			.filter(item => item.type === "content")
			.map(item => item.content?.text ?? "");
		expect(texts.some(text => text.includes("0-AcpText"))).toBe(true);
		// An empty text block makes extractStructuredText fail, so the mapper
		// dumps the whole result object as the human-readable content instead.
		expect(texts.some(text => text.includes('"subagents"'))).toBe(false);

		gate.resolve("done");
		await manager.getJob(jobId)?.promise;
		await manager.dispose({ timeoutMs: 100 });
	});
});
