import { afterEach, describe, expect, type Mock, spyOn, test } from "bun:test";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { AuthStorage } from "@gajae-code/ai";
import { AsyncJobManager } from "@gajae-code/coding-agent/async";
import { Settings } from "@gajae-code/coding-agent/config/settings";
import { initializeExtensions } from "@gajae-code/coding-agent/modes/runtime-init";
import { createAgentSession } from "@gajae-code/coding-agent/sdk";
import { SessionManager } from "@gajae-code/coding-agent/session/session-manager";
import {
	registerOwnedRegistration,
	registerTerminalTurnScope,
	resetTerminalAbortRegistriesForTests,
} from "@gajae-code/coding-agent/session/terminal-abort";
import { z } from "zod/v4";

describe("custom tool lifecycle error boundaries", () => {
	const temporaryDirectories: string[] = [];
	const authStorages = new Set<AuthStorage>();

	afterEach(async () => {
		for (const storage of authStorages) storage.close();
		authStorages.clear();
		await Promise.all(
			temporaryDirectories.splice(0).map(directory => rm(directory, { recursive: true, force: true })),
		);
	});

	test("hostile custom-tool onSession errors remain non-fatal", async () => {
		const root = await mkdtemp(path.join(os.tmpdir(), "gajae-sdk-hostile-on-session-"));
		temporaryDirectories.push(root);
		const cwd = path.join(root, "project");
		const agentDir = path.join(root, "agent");
		await Promise.all([mkdir(cwd), mkdir(agentDir)]);
		const authStorage = await AuthStorage.create(path.join(agentDir, "auth.db"));
		authStorages.add(authStorage);

		const hostileError = new Proxy(Object.create(null), {
			getPrototypeOf() {
				throw new Error("prototype trap");
			},
			get() {
				throw new Error("property trap");
			},
		});
		const runtimeErrors: unknown[] = [];
		const { session } = await createAgentSession({
			cwd,
			agentDir,
			authStorage,
			settings: Settings.isolated(),
			sessionManager: SessionManager.inMemory(cwd),
			disableExtensionDiscovery: true,
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			customTools: [
				{
					name: "hostile-on-session",
					label: "Hostile onSession",
					description: "Throws a hostile proxy from onSession.",
					parameters: z.object({}),
					execute: async () => ({ content: [{ type: "text" as const, text: "ok" }] }),
					onSession: () => {
						throw hostileError;
					},
				},
			],
		});

		try {
			await initializeExtensions(session, {
				reportSendError: () => {},
				reportRuntimeError: error => runtimeErrors.push(error),
			});
			expect(runtimeErrors).toEqual([]);
		} finally {
			await session.dispose();
		}
	});

	test("denied owned-completion deliveries never allocate an async artifact", async () => {
		// A large owned job completing while a scope:"owned" abort is settling its
		// gate must be classified BEFORE formatting: the denied delivery is
		// dropped at flush, so allocating an artifact for it would leave the
		// stopped job's output in an unreferenced artifact (review thread P2).
		const root = await mkdtemp(path.join(os.tmpdir(), "gajae-sdk-owned-artifact-"));
		temporaryDirectories.push(root);
		const cwd = path.join(root, "project");
		const agentDir = path.join(root, "agent");
		await Promise.all([mkdir(cwd), mkdir(agentDir)]);
		const authStorage = await AuthStorage.create(path.join(agentDir, "auth.db"));
		authStorages.add(authStorage);
		const sessionManager = SessionManager.inMemory(cwd);
		const settings = Settings.isolated({
			"async.enabled": true,
			"bash.autoBackground.enabled": true,
		});
		const { session } = await createAgentSession({
			cwd,
			agentDir,
			authStorage,
			settings,
			sessionManager,
			disableExtensionDiscovery: true,
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			customTools: [],
		});
		resetTerminalAbortRegistriesForTests();
		let allocateSpy: Mock<typeof sessionManager.allocateArtifactPath> | undefined;
		try {
			const manager = AsyncJobManager.instance();
			expect(manager).toBeDefined();
			const endpointId = sessionManager.getSessionId?.() ?? "local";
			// A scope:"owned" abort installs a disabled owned-completion policy.
			registerTerminalTurnScope({
				lineageIdHash: "owned-artifact-lineage",
				promptAttemptEpoch: 7,
				ownedCompletionPolicy: "disabled",
			});
			const gate = Promise.withResolvers<string>();
			const jobId = manager!.register("bash", "owned artifact job", () => gate.promise);
			const generation = manager!.getJob(jobId)?.generation;
			registerOwnedRegistration(
				{
					endpointId,
					endpointGeneration: 1,
					lineageIdHash: "owned-artifact-lineage",
					promptAttemptEpoch: 7,
					jobId,
					jobGeneration: generation as string,
				},
				{ isJobTerminal: () => false },
			);
			allocateSpy = spyOn(sessionManager, "allocateArtifactPath");
			// A LARGE result (> the 12k inline cap) would normally allocate an
			// artifact; the denied envelope must use only the inline preview.
			gate.resolve("x".repeat(20_000));
			const deadline = Date.now() + 15_000;
			while (manager!.getJob(jobId)?.status !== "completed") {
				if (Date.now() > deadline) throw new Error("Timed out waiting for the owned job to complete");
				await Bun.sleep(20);
			}
			expect(allocateSpy).not.toHaveBeenCalled();
		} finally {
			allocateSpy?.mockRestore();
			await session.dispose();
			AsyncJobManager.setInstance(undefined);
		}
	});
});
