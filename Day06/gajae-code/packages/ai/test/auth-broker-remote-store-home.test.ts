import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

/**
 * The auth-broker presentation sidecar default is built from the trusted
 * config root, which is call-time state since #4761/#4772: the resolver
 * re-derives the home on every access. A module-level constant derived from it
 * at import time keeps pointing at the home that was in effect when the module
 * first loaded, so a process whose home is established or changed after load
 * reads and writes one logical profile through two different roots (#4786).
 *
 * The regression runs out of process (like `trusted-home-resolution.test.ts`)
 * so the import-time capture happens under a controlled first home and the
 * post-load switch to a second home is exactly the defect condition. Both
 * homes are temp directories, so the buggy lane never touches real user state.
 */
const PROBE = path.join(import.meta.dir, "fixtures", "auth-broker-presentation-home-probe.ts");

interface ProbeResult {
	importTimeConfigRoot: string;
	configRootAfterHomeChange: string;
	sidecarUnderImportTimeRoot: boolean;
	sidecarUnderCurrentRoot: boolean;
}

async function probe(firstHome: string, secondHome: string): Promise<ProbeResult> {
	const env: Record<string, string> = {};
	for (const [key, value] of Object.entries(process.env)) {
		if (value !== undefined) env[key] = value;
	}
	delete env.GJC_CODING_AGENT_DIR;
	delete env.PI_CODING_AGENT_DIR;
	delete env.GJC_CONFIG_DIR;
	delete env.PI_CONFIG_DIR;
	delete env.XDG_DATA_HOME;
	delete env.XDG_STATE_HOME;
	delete env.XDG_CACHE_HOME;
	// Only the platform-authoritative variable selects the home; the opposite
	// one is cleared so an inherited value cannot shadow it. The second home
	// arrives through an `os.homedir()` mock in the fixture.
	const homeKey = process.platform === "win32" ? "USERPROFILE" : "HOME";
	const unusedHomeKey = process.platform === "win32" ? "HOME" : "USERPROFILE";
	env[homeKey] = firstHome;
	delete env[unusedHomeKey];
	env.GJC_PROBE_SECOND_HOME = secondHome;

	const proc = Bun.spawn([process.execPath, PROBE], {
		cwd: firstHome,
		env,
		stdout: "pipe",
		stderr: "pipe",
	});
	const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
	const exitCode = await proc.exited;
	if (exitCode !== 0) throw new Error(`probe failed (${exitCode}): ${stderr}`);
	return JSON.parse(stdout.trim()) as ProbeResult;
}

describe("auth-broker presentation sidecar follows the call-time home (#4786)", () => {
	const tempDirs: string[] = [];

	function tempDir(): string {
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gjc-auth-broker-home-"));
		tempDirs.push(dir);
		return dir;
	}

	afterEach(() => {
		for (const dir of tempDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
	});

	it("writes the default sidecar under the config root current at construction, not at import", async () => {
		const firstHome = tempDir();
		const secondHome = tempDir();
		const result = await probe(firstHome, secondHome);

		expect(result.importTimeConfigRoot).toBe(path.join(firstHome, ".gjc"));
		expect(result.configRootAfterHomeChange).toBe(path.join(secondHome, ".gjc"));
		expect(result.sidecarUnderImportTimeRoot).toBe(false);
		expect(result.sidecarUnderCurrentRoot).toBe(true);
	});
});
