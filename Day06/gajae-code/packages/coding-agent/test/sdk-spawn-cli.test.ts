import { describe, expect, it } from "bun:test";
import {
	renderSpawnTable,
	runSdkSpawn,
	SdkMasterCliError,
	safeSpawnRender,
	selectNewestMasterAttestationEpoch,
} from "@gajae-code/coding-agent/sdk/cli/master-cli";
import type { IndexedSession } from "../src/sdk/broker/session-index";
import { SdkClientError } from "../src/sdk/client/client";

const task = "secret-task-fixture";
const capability = "secret-capability-fixture";

const masterEnv = { GJC_MASTER_CAPABILITY: capability, GJC_MASTER_OWNER_SESSION_ID: "master-cli-owner" };
const epoch = async () => "epoch-cli";

describe("gjc sdk spawn CLI", () => {
	const attestation = (
		attestationEpoch: string,
		launchProcessIncarnation: string,
		ownerSessionId = "master-cli-owner",
	) => ({
		version: 2 as const,
		ownerSessionId,
		launchPid: 42,
		launchProcessIncarnation,
		role: "master" as const,
		attestationEpoch,
	});
	const row = (
		indexSeq: number,
		epoch: string,
		endpointGeneration: number,
		live = true,
		processIncarnation = "process-42",
		sessionId = "master-cli-owner",
		ownerSessionId = "master-cli-owner",
	): IndexedSession => ({
		sessionId,
		locator: { cwd: "/repo", worktreeRoot: "/repo", stateRoot: "/state" },
		endpointGeneration,
		pid: 42,
		processIncarnation,
		live,
		indexSeq,
		identityProvenance: "composite",
		ambiguous: false,
		terminal: false,
		masterRole: attestation(epoch, processIncarnation, ownerSessionId),
	});

	it("selects the older live epoch when a newer crashed master registration remains", () => {
		const rows: IndexedSession[] = [
			row(1, "older-live-epoch", 0),
			row(20, "older-live-epoch", 1),
			row(21, "crashed-newer-epoch", 0, false),
			row(22, "crashed-newer-epoch", 1, false),
		];

		expect(selectNewestMasterAttestationEpoch(rows, "master-cli-owner")).toBe("older-live-epoch");
	});

	it("selects the live resumed master incarnation when a stale identity is returned first", () => {
		const priorProcess = "process-prior";
		const resumedProcess = "process-resumed";
		const rows: IndexedSession[] = [
			row(30, "prior-epoch", 1, false, priorProcess),
			row(29, "prior-epoch", 0, false, priorProcess),
			row(31, "resumed-epoch", 0, true, resumedProcess),
			row(32, "resumed-epoch", 1, true, resumedProcess),
		];

		expect(selectNewestMasterAttestationEpoch(rows, "master-cli-owner")).toBe("resumed-epoch");

		const successorRows = [
			row(40, "lineage-epoch", 0, true, "process-42", "master-cli-successor", "master-cli-owner"),
			row(41, "lineage-epoch", 1, true, "process-42", "master-cli-successor", "master-cli-owner"),
		];
		expect(selectNewestMasterAttestationEpoch(successorRows, "master-cli-owner")).toBe("lineage-epoch");
	});

	it("requires --cwd and --prompt", async () => {
		await expect(
			runSdkSpawn({ prompt: task }, { env: masterEnv, resolveAttestationEpoch: epoch }),
		).rejects.toMatchObject({
			code: "invalid_input",
			exitCode: 2,
		});
		await expect(
			runSdkSpawn({ cwd: "/tmp" }, { env: masterEnv, resolveAttestationEpoch: epoch }),
		).rejects.toMatchObject({
			code: "invalid_input",
			exitCode: 2,
		});
	});

	it("refuses without a live master context", async () => {
		await expect(
			runSdkSpawn({ cwd: "/tmp", prompt: task }, { env: {}, resolveAttestationEpoch: epoch }),
		).rejects.toMatchObject({ code: "master_context_required" });
		await expect(
			runSdkSpawn({ cwd: "/tmp", prompt: task }, { env: masterEnv, resolveAttestationEpoch: async () => undefined }),
		).rejects.toMatchObject({ code: "master_context_required" });
	});

	it("dispatches with a fresh idempotency key and the transient capability", async () => {
		const keys: string[] = [];
		const inputs: Record<string, unknown>[] = [];
		const dispatch = async (_agentDir: string, input: Record<string, unknown>, idempotencyKey: string) => {
			keys.push(idempotencyKey);
			inputs.push(input);
			return {
				ok: true,
				result: {
					code: "spawn_accepted",
					claimId: "claim-1",
					sessionId: "child-1",
					substrateKind: "tmux",
					seed: {
						phase: "accepted",
						clientRef: "ref-1",
						commandId: "cmd-1",
						turnId: "turn-1",
						status: "accepted",
					},
				},
			};
		};
		const deps = { env: masterEnv, resolveAttestationEpoch: epoch, dispatch };
		const first = await runSdkSpawn({ cwd: "/tmp", prompt: task, agentDir: "/tmp/agent" }, deps);
		const second = await runSdkSpawn({ cwd: "/tmp", prompt: task, agentDir: "/tmp/agent" }, deps);
		expect(keys).toHaveLength(2);
		expect(keys[0]).not.toBe(keys[1]);
		expect(inputs[0]).toMatchObject({
			task,
			masterCapability: capability,
			ownerSessionId: "master-cli-owner",
			attestationEpoch: "epoch-cli",
			cwd: "/tmp",
		});
		expect(first.exitCode).toBe(0);
		expect(second.rendered.code).toBe("spawn_accepted");
	});

	it("joins an existing claim when re-run with an explicit idempotency key", async () => {
		const idempotencyKey = "spawn-join-key";
		const keys: string[] = [];
		const inputs: Record<string, unknown>[] = [];
		const claims = new Map<string, string>();
		let launches = 0;
		const dispatch = async (_agentDir: string, input: Record<string, unknown>, key: string) => {
			keys.push(key);
			inputs.push(input);
			const existing = claims.get(key);
			if (existing) return { ok: true, result: { code: "spawn_replayed", sessionId: existing } };
			launches += 1;
			const sessionId = `child-${launches}`;
			claims.set(key, sessionId);
			return { ok: true, result: { code: "spawn_accepted", sessionId } };
		};
		const dependencies = { env: masterEnv, resolveAttestationEpoch: epoch, dispatch };
		const args = { cwd: "/tmp", prompt: task, idempotencyKey };
		const accepted = await runSdkSpawn(args, dependencies);
		const replay = await runSdkSpawn(args, dependencies);
		expect(keys).toEqual([idempotencyKey, idempotencyKey]);
		expect(inputs[1]).toEqual(inputs[0]);
		expect(accepted.rendered).toMatchObject({ code: "spawn_accepted", sessionId: "child-1" });
		expect(replay.rendered).toMatchObject({ code: "spawn_replayed", sessionId: "child-1" });
		expect(launches).toBe(1);
	});

	it("renders the replay key when session.spawn is uncertain after send", async () => {
		const idempotencyKey = "spawn-retry-key";
		let dispatchedKey: string | undefined;
		const result = await runSdkSpawn(
			{ cwd: "/tmp", prompt: task, idempotencyKey },
			{
				env: masterEnv,
				resolveAttestationEpoch: epoch,
				dispatch: async (_agentDir, _input, key) => {
					dispatchedKey = key;
					throw new SdkClientError("uncertain_after_send", "spawn response was lost after send");
				},
			},
		);
		expect(dispatchedKey).toBe(idempotencyKey);
		expect(result).toEqual({
			rendered: {
				code: "uncertain_after_send",
				idempotencyKey,
				error: { code: "uncertain_after_send", message: "spawn response was lost after send" },
			},
			exitCode: 1,
		});
		expect(renderSpawnTable(result.rendered)).toContain(`Retry idempotency key: ${idempotencyKey}`);
	});

	it("renders only safe fields for every outcome and never echoes input", async () => {
		const outcomes: unknown[] = [
			{
				ok: true,
				result: {
					code: "spawn_accepted",
					claimId: "claim-1",
					sessionId: "child-1",
					substrateKind: "headless",
					seed: { phase: "accepted", clientRef: "ref-1", status: "accepted" },
					task,
					masterCapability: capability,
				},
			},
			{ ok: false, error: { code: "spawn_in_progress", message: "session.spawn is dispatching" } },
			{ ok: false, error: { code: "terminal_uncertain", message: "session.spawn outcome is uncertain" } },
			{ ok: false, error: { code: "idempotency_conflict", message: "idempotency key conflicts" } },
		];
		for (const outcome of outcomes) {
			const { rendered, exitCode } = safeSpawnRender(outcome);
			const text = `${JSON.stringify(rendered)}\n${renderSpawnTable(rendered)}`;
			expect(text).not.toContain(task);
			expect(text).not.toContain(capability);
			if ((outcome as { ok: boolean }).ok) {
				expect(exitCode).toBe(0);
				// Unsafe extra fields are dropped by the allowlist projection.
				expect(Object.keys(rendered).sort()).toEqual(["claimId", "code", "seed", "sessionId", "substrateKind"]);
			} else {
				expect(exitCode).toBe(1);
				expect(rendered.error?.code).toBeDefined();
			}
		}
	});

	it("exposes typed errors for scripting", () => {
		const error = new SdkMasterCliError("master_context_required", "no master", 1);
		expect(error.code).toBe("master_context_required");
		expect(error.exitCode).toBe(1);
	});

	it("resolves relative cwd before broker dispatch", async () => {
		let dispatchedCwd: unknown;
		await runSdkSpawn(
			{ cwd: ".", prompt: task },
			{
				env: masterEnv,
				resolveAttestationEpoch: epoch,
				dispatch: async (_agentDir, input) => {
					dispatchedCwd = input.cwd;
					return { ok: true, result: { code: "spawn_accepted" } };
				},
			},
		);
		expect(dispatchedCwd).toBe(process.cwd());
	});
});
