import { describe, expect, it, setDefaultTimeout } from "bun:test";
import * as path from "node:path";

/**
 * Production contention is bounded at five seconds; measured probes finish in about 5.03s,
 * so the child deadline and contended assertions retain bounded startup/scheduling headroom.
 */
const PROBE_TIMEOUT_MS = 10_000;
const CONTENDED_WALL_LIMIT_MS = 7_000;
const CONTENDED_ATTEMPT_LIMIT = 64;
/**
 * What the CPU bound actually protects: a contended acquisition must back off,
 * not hot-spin. It therefore has to scale with the five-second acquisition
 * deadline — a fixed 750ms allowance was written against the old two-second
 * deadline and left a correctly backing-off child (measured ~0.77s of CPU
 * across ~5s of waiting) failing on a loaded host. A hot spin still bursts
 * this budget, because it would burn CPU for the whole deadline.
 */
const CONTENDED_CPU_LIMIT_MS = 1_800;
setDefaultTimeout(15_000);

interface ProbeResult {
	scenario: string;
	entered: boolean;
	attempts: number;
	wallMs: number;
	cpuMs: number;
	error: {
		name: string;
		message: string;
		lockPath: string;
		reason: string;
		attempts: number;
		elapsedMs: number;
		cause: string;
	} | null;
	remaining: string | null;
	lockFile: string;
}

const probe = path.join(import.meta.dir, "fixtures", "session-state-lock-debris-probe.ts");

async function runProbe(scenario: string, native = false): Promise<ProbeResult> {
	const child = Bun.spawn([process.execPath, probe, scenario], {
		cwd: path.resolve(import.meta.dir, "../../.."),
		env: { ...process.env, NO_COLOR: "1", GJC_SESSION_LOCK_PROBE_NATIVE: native ? "1" : undefined },
		stdout: "pipe",
		stderr: "pipe",
	});
	const timedOut = Symbol("timed-out");
	const exit = await Promise.race([child.exited, Bun.sleep(PROBE_TIMEOUT_MS).then(() => timedOut)]);
	if (exit === timedOut) {
		child.kill("SIGKILL");
		await child.exited;
		throw new Error(`lock debris probe timed out: ${scenario}`);
	}
	if (typeof exit !== "number") throw new Error(`lock debris probe returned an invalid exit status: ${scenario}`);
	const [stdout, stderr] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text()]);
	if (exit !== 0) throw new Error(`lock debris probe failed (${exit}): ${stderr}`);
	return JSON.parse(stdout) as ProbeResult;
}

describe("session-state lock debris subprocess contract", () => {
	it("reclaims a stale malformed record only with local dead-owner provenance", async () => {
		const result = await runProbe("malformed-dead");
		expect(result.entered).toBe(true);
		expect(result.error).toBeNull();
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it("fails quickly with a typed exact-path diagnostic for unknown provenance", async () => {
		const result = await runProbe("unknown-provenance");
		expect(result.entered).toBe(false);
		expect(result.error).toMatchObject({
			name: "SessionStateLockUnavailableError",
			lockPath: result.lockFile,
			reason: "lock_owner_record_unprovenanced",
		});
		expect(result.error?.message).toContain(result.lockFile);
		expect(result.error?.message).toContain("lock_owner_record_unprovenanced");
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it("identity-bound reclaims an old empty legacy lock directory", async () => {
		const result = await runProbe("old-empty-directory");
		expect(result.entered).toBe(true);
		expect(result.error).toBeNull();
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it("preserves a plausible live owner and bounds attempts, CPU, and wall time", async () => {
		const result = await runProbe("plausible-live");
		expect(result.entered).toBe(false);
		expect(result.error).toMatchObject({
			lockPath: result.lockFile,
			reason: "lock_owner_live_or_unverifiable",
		});
		expect(result.remaining).toContain('"token":"plausible-live"');
		expect(result.attempts).toBeGreaterThan(1);
		expect(result.attempts).toBeLessThanOrEqual(CONTENDED_ATTEMPT_LIMIT);
		expect(result.cpuMs).toBeLessThan(CONTENDED_CPU_LIMIT_MS);
		expect(result.wallMs).toBeLessThan(CONTENDED_WALL_LIMIT_MS);
	});

	it("uses the same wall-time deadline for a contended transition claim", async () => {
		const result = await runProbe("plausible-live-transition");
		expect(result.entered).toBe(false);
		expect(result.error).toMatchObject({
			lockPath: `${result.lockFile}.transition`,
			reason: "transition_claim_timeout",
		});
		expect(result.cpuMs).toBeLessThan(CONTENDED_CPU_LIMIT_MS);
		expect(result.wallMs).toBeLessThan(CONTENDED_WALL_LIMIT_MS);
	});

	it("reclaims an empty released transition tombstone on POSIX", async () => {
		const result = await runProbe("released-transition-tombstone");
		expect(result.entered).toBe(true);
		expect(result.error).toBeNull();
		expect(result.attempts).toBeLessThanOrEqual(2);
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it.skipIf(process.platform === "win32")("reclaims released tombstone with production native cleanup", async () => {
		const result = await runProbe("released-transition-tombstone", true);
		expect(result.entered).toBe(true);
		expect(result.error).toBeNull();
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it("reclaims a dead transition claim with in-process identity bindings", async () => {
		const result = await runProbe("stale-dead-transition");
		expect(result.entered).toBe(true);
		expect(result.error).toBeNull();
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it("reclaims a dead transition claim on every platform", async () => {
		const result = await runProbe("stale-dead-transition", true);
		expect(result.entered).toBe(true);
		expect(result.error).toBeNull();
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it("never deletes a live successor that replaces stale regular debris", async () => {
		const result = await runProbe("race-replacement");
		expect(result.entered).toBe(false);
		expect(result.error?.reason).toBe("lock_owner_live_or_unverifiable");
		expect(result.remaining).toContain('"token":"race-successor"');
		expect(result.wallMs).toBeLessThan(CONTENDED_WALL_LIMIT_MS);
	});

	it("never deletes a successor that replaces an old empty directory", async () => {
		const result = await runProbe("old-empty-directory-replacement");
		expect(result.entered).toBe(false);
		expect(result.error?.reason).toBe("lock_owner_live_or_unverifiable");
		expect(result.remaining).toContain('"token":"directory-successor"');
		expect(result.wallMs).toBeLessThan(CONTENDED_WALL_LIMIT_MS);
	});

	it("accepts the production native durable detach receipt for an old empty directory", async () => {
		const result = await runProbe("old-empty-directory", true);
		expect(result.entered).toBe(true);
		expect(result.error).toBeNull();
		expect(result.wallMs).toBeLessThan(3_000);
	});

	it("treats the production native directory-to-file replacement code as identity change", async () => {
		const result = await runProbe("old-empty-directory-replacement", true);
		expect(result.entered).toBe(false);
		expect(result.error).toMatchObject({
			lockPath: result.lockFile,
			reason: "lock_owner_live_or_unverifiable",
		});
		expect(result.remaining).toContain('"token":"directory-successor"');
		expect(result.wallMs).toBeLessThan(CONTENDED_WALL_LIMIT_MS);
	});
});
