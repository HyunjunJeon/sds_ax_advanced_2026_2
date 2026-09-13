/**
 * Policy-focused red-team coverage for the steer-triggered Bash fold.
 *
 * The real dogfood divergence was specific to `finish_tools`: a post-grace
 * steer must fold the foreground Bash without killing it, while the agent loop
 * still applies the configured policy to sibling tool calls at the boundary.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as path from "node:path";
import { createMockModel, type MockModel, registerMockApi } from "@gajae-code/ai/providers/mock";
import { TempDir } from "@gajae-code/utils";
import { AsyncJobManager } from "../src/async";
import { resetSettingsForTest, Settings } from "../src/config/settings";
import { type CreateAgentSessionResult, createAgentSession } from "../src/sdk";
import { AuthStorage } from "../src/session/auth-storage";
import { SessionManager } from "../src/session/session-manager";
import { BashTool, STEER_FOLD_GRACE_MS } from "../src/tools/bash";
import { createSteerHarness, type SteerHarness, textOf, turnContext } from "./helpers/steer-fold-harness";

async function waitFor(predicate: () => boolean, timeoutMs = 10_000): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return;
		await Bun.sleep(10);
	}
	throw new Error("Timed out waiting for steer-fold state");
}

function toolResultText(messages: readonly { role?: string; content?: unknown }[]): string {
	return messages
		.filter(message => message.role === "toolResult")
		.map(message => JSON.stringify(message.content))
		.join("\n");
}

describe("steer fold policy red team", () => {
	let tempDir: TempDir | undefined;
	let created: CreateAgentSessionResult | undefined;
	let authStorage: AuthStorage | undefined;
	let harness: SteerHarness | undefined;

	beforeEach(() => {
		resetSettingsForTest();
		tempDir = TempDir.createSync("@gjc-steer-fold-policy-redteam-");
	});

	afterEach(async () => {
		await harness?.manager.dispose();
		await created?.session.dispose();
		authStorage?.close();
		tempDir?.removeSync();
		harness = undefined;
		created = undefined;
		authStorage = undefined;
		tempDir = undefined;
		AsyncJobManager.resetForTests();
		resetSettingsForTest();
	});

	async function createLiveSession(mock: MockModel, policy: "abort_tools" | "finish_tools"): Promise<void> {
		if (!tempDir) throw new Error("Expected a temporary directory");
		await Settings.init({ inMemory: true, cwd: tempDir.path() });
		registerMockApi();
		authStorage = await AuthStorage.create(path.join(tempDir.path(), "auth.db"));
		authStorage.setRuntimeApiKey(mock.model.provider, "test-key");
		created = await createAgentSession({
			cwd: tempDir.path(),
			agentDir: tempDir.path(),
			sessionManager: SessionManager.create(tempDir.path(), tempDir.path()),
			authStorage,
			settings: Settings.isolated({
				"async.enabled": true,
				"bash.autoBackground.enabled": false,
				"compaction.enabled": false,
				busyPromptMode: "steer",
				toolInterruptPolicy: policy,
			}),
			model: mock.model,
			toolNames: ["bash"],
			disableExtensionDiscovery: true,
			extensions: [],
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			sdkHostModeSupported: false,
			notificationHostModeSupported: false,
		});
	}

	for (const policy of ["finish_tools", "abort_tools"] as const) {
		test(`post-grace steer folds the long Bash and ${policy} ${
			policy === "finish_tools" ? "finishes" : "skips"
		} its sibling at the boundary`, async () => {
			const mock = createMockModel({
				responses: [
					{
						content: [
							{
								type: "toolCall",
								name: "bash",
								arguments: {
									command: "printf 'policy-long-start\\n'; sleep 5; printf 'policy-long-finished\\n'",
									timeout: 30,
								},
							},
							{
								type: "toolCall",
								name: "bash",
								arguments: { command: "sleep 0.2; printf 'policy-sibling-finished\\n'", timeout: 30 },
							},
						],
					},
					{ content: ["boundary steer handled"] },
					{ content: ["fold wake handled"] },
				],
			});
			await createLiveSession(mock, policy);
			const session = created?.session;
			if (!session) throw new Error("Expected a live session");

			const toolResults: string[] = [];
			const unsubscribe = session.subscribe(event => {
				if (event.type === "tool_execution_end") toolResults.push(JSON.stringify(event.result));
			});
			try {
				const run = session.prompt("run the parallel Bash batch");
				await waitFor(() => session.hasForegroundBashBackgroundRequestHandler());
				await Bun.sleep(STEER_FOLD_GRACE_MS + 150);
				await session.steer("policy boundary steer");
				await run;

				const folded = toolResults.find(result => result.includes("Folded into background job"));
				expect(folded).toBeDefined();
				expect(folded).toContain("because a user steer arrived");
				expect(session.drainableQueuedMessageCount).toBe(0);
				expect(session.agent.hasQueuedSteering()).toBe(false);

				const steerCall = mock.calls.find(call =>
					JSON.stringify(call.context.messages).includes("policy boundary steer"),
				);
				if (!steerCall) throw new Error("Expected the steer to reach the next model call at the tool boundary");
				const boundaryResults = toolResultText(steerCall.context.messages);
				if (policy === "finish_tools") {
					expect(toolResults.some(result => result.includes("policy-sibling-finished"))).toBe(true);
					expect(boundaryResults).toContain("policy-sibling-finished");
					expect(boundaryResults).not.toContain("Skipped due to queued user message");
				} else {
					expect(toolResults.some(result => result.includes("policy-sibling-finished"))).toBe(false);
					expect(boundaryResults).toContain("Skipped due to queued user message");
				}
			} finally {
				unsubscribe();
			}
		}, 30_000);
	}

	test("finish_tools early steer stays foreground and is delivered at the normal boundary", async () => {
		const mock = createMockModel({
			responses: [
				{
					content: [
						{
							type: "toolCall",
							name: "bash",
							arguments: {
								command: "printf 'early-policy-start\\n'; sleep 2.6; printf 'early-policy-finished\\n'",
								timeout: 30,
							},
						},
					],
				},
				{ content: ["early boundary steer handled"] },
			],
		});
		await createLiveSession(mock, "finish_tools");
		const session = created?.session;
		if (!session) throw new Error("Expected a live session");

		const toolResults: string[] = [];
		const unsubscribe = session.subscribe(event => {
			if (event.type === "tool_execution_end") toolResults.push(JSON.stringify(event.result));
		});
		try {
			const run = session.prompt("run the early-steer Bash");
			await waitFor(() => session.hasForegroundBashBackgroundRequestHandler());
			await Bun.sleep(100);
			await session.steer("early policy boundary steer");
			await run;

			expect(toolResults.some(result => result.includes("early-policy-finished"))).toBe(true);
			expect(toolResults.some(result => result.includes("Folded into background job"))).toBe(false);
			expect(toolResults.some(result => result.includes("Tool execution was aborted"))).toBe(false);
			const steerCall = mock.calls.find(call =>
				JSON.stringify(call.context.messages).includes("early policy boundary steer"),
			);
			if (!steerCall) throw new Error("Expected the early steer to reach the normal tool boundary");
			expect(toolResultText(steerCall.context.messages)).toContain("early-policy-finished");
			expect(session.drainableQueuedMessageCount).toBe(0);
			expect(session.agent.hasQueuedSteering()).toBe(false);
		} finally {
			unsubscribe();
		}
	}, 20_000);

	test("finish_tools with busyPromptMode=queue never folds a post-grace steer", async () => {
		if (!tempDir) throw new Error("Expected a temporary directory");
		harness = createSteerHarness(tempDir.path(), { busyPromptMode: "queue" });
		const resultPromise = new BashTool(harness.session).execute(
			"finish-tools-queue",
			{ command: "sleep 2.5; printf 'queue-policy-finished\\n'", timeout: 30 },
			undefined,
			undefined,
			turnContext(),
		);
		await Bun.sleep(STEER_FOLD_GRACE_MS + 100);
		harness.steer("queued policy steer");
		const result = await resultPromise;

		expect(result.details?.async).toBeUndefined();
		expect(result.details?.foldReason).toBeUndefined();
		expect(textOf(result)).toContain("queue-policy-finished");
		expect(harness.folds).toEqual([]);
		expect(harness.hasQueuedSteering()).toBe(true);
	}, 10_000);

	test("finish_tools chord fold remains chord after a later steer and cannot refold", async () => {
		if (!tempDir) throw new Error("Expected a temporary directory");
		harness = createSteerHarness(tempDir.path());
		const resultPromise = new BashTool(harness.session).execute(
			"finish-tools-chord-then-steer",
			{ command: "sleep 2.5; printf 'chord-policy-finished\\n'", timeout: 30 },
			undefined,
			undefined,
			turnContext(),
		);
		await waitFor(() => harness?.session.hasForegroundBashBackgroundRequestHandler?.() === true);
		expect(await harness.session.requestForegroundBashBackground?.("chord")).toBe(true);
		const result = await resultPromise;
		const jobId = result.details?.async?.jobId;
		if (!jobId) throw new Error("Expected a chord-folded background job");
		const job = harness.manager.getJob(jobId);
		if (!job) throw new Error("Expected the chord-folded job to remain registered");

		harness.steer("late steer after chord");
		await Bun.sleep(50);
		expect(await harness.session.requestForegroundBashBackground?.("steer")).toBe(false);
		expect(result.details?.foldReason).toBe("chord");
		expect(job.metadata?.foldReason).toBe("chord");
		expect(harness.folds).toEqual([{ jobId, generation: job.generation, reason: "chord" }]);
		await job.promise;
	}, 10_000);
});
