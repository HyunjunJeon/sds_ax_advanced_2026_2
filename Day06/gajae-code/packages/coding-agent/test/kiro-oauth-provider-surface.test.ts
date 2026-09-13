import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as kiroOAuthModule from "@gajae-code/ai";
import * as kiroLoginModule from "@gajae-code/ai/utils/oauth/kiro";
import { ModelRegistry } from "@gajae-code/coding-agent/config/model-registry";
import { resetSettingsForTest } from "@gajae-code/coding-agent/config/settings";
import { AuthStorage } from "@gajae-code/coding-agent/session/auth-storage";
import { getAgentDbPath, Snowflake } from "@gajae-code/utils";
import { runAuthBrokerCommand } from "../src/cli/auth-broker-cli";

/**
 * Coverage for issue #5064: Kiro OAuth was advertised in every product
 * surface (interactive `/login`, `gjc auth-broker login kiro`) but the
 * underlying `AuthStorage.login()` dispatcher had no `case "kiro"`, so every
 * advertised path reported `Unknown OAuth provider: kiro`. This suite proves
 * the package/direct CLI surface and the bundled model catalog are coherent
 * with the advertised provider list.
 */

describe("Kiro OAuth CLI surface (package/direct)", () => {
	let tempDir = "";
	let originalAgentDir: string | undefined;
	const ORIGINAL_STDOUT_WRITE = process.stdout.write.bind(process.stdout);

	function silenceStdout(): () => string {
		let captured = "";
		process.stdout.write = ((chunk: string | Uint8Array): boolean => {
			captured += typeof chunk === "string" ? chunk : new TextDecoder().decode(chunk);
			return true;
		}) as typeof process.stdout.write;
		return () => captured;
	}

	beforeEach(async () => {
		originalAgentDir = process.env.GJC_AGENT_DIR;
		tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "gjc-kiro-cli-"));
		process.env.GJC_AGENT_DIR = tempDir;
	});

	afterEach(async () => {
		process.stdout.write = ORIGINAL_STDOUT_WRITE;
		if (originalAgentDir === undefined) delete process.env.GJC_AGENT_DIR;
		else process.env.GJC_AGENT_DIR = originalAgentDir;
		await fs.promises.rm(tempDir, { recursive: true, force: true });
	});

	test("kiro passes the auth-broker CLI's known-provider gate", async () => {
		const providers = new Set(kiroOAuthModule.getOAuthProviders().map(p => p.id));
		expect(providers.has("kiro")).toBe(true);
	});

	test("`gjc auth-broker login unknown-provider-xyz` fails with a clear unknown-provider error, not a crash", async () => {
		const restore = silenceStdout();
		try {
			await expect(
				runAuthBrokerCommand({
					action: "login",
					flags: { provider: "unknown-provider-xyz" },
				}),
			).rejects.toThrow(/Unknown OAuth provider/);
		} finally {
			restore();
		}
	});

	test("`gjc auth-broker login kiro` persists credentials in-process, without spawning/resolving @gajae-code/ai/cli", async () => {
		// Regression for issue #5064: runLocalLogin() previously spawned a child
		// process resolved via import.meta.resolve("@gajae-code/ai/cli"), which
		// requires an on-disk node_modules package resolution absent inside a
		// compiled `bun build --compile` binary's $bunfs. It now drives
		// AuthStorage.login() in-process using the same statically-traceable
		// @gajae-code/ai/core import already used elsewhere in auth-broker-cli.ts.
		const restore = silenceStdout();
		const loginKiroSpy = vi.spyOn(kiroLoginModule, "loginKiro").mockImplementation(async options => {
			options.onAuth("https://device.sso.us-east-1.amazonaws.com/", "Enter code: TEST-CODE");
			return { access: "broker-kiro-access", refresh: "broker-kiro-refresh", expires: Date.now() + 3600_000 };
		});
		try {
			await runAuthBrokerCommand({
				action: "login",
				flags: { provider: "kiro" },
			});
			expect(loginKiroSpy).toHaveBeenCalledTimes(1);

			const store = await kiroOAuthModule.SqliteAuthCredentialStore.open(getAgentDbPath());
			try {
				const oauth = store.getOAuth("kiro");
				expect(oauth?.access).toBe("broker-kiro-access");
			} finally {
				store.close();
			}
		} finally {
			loginKiroSpy.mockRestore();
			restore();
		}
	});
});

describe("Kiro model catalog reachable through ModelRegistry (interactive/model-picker surface)", () => {
	let tempDir: string;
	let modelsJsonPath: string;
	let authStorage: AuthStorage;
	let previousPresetRegistryDisabled: string | undefined;

	beforeEach(async () => {
		resetSettingsForTest();
		previousPresetRegistryDisabled = Bun.env.GJC_MODEL_PRESET_REGISTRY_DISABLED;
		Bun.env.GJC_MODEL_PRESET_REGISTRY_DISABLED = "true";
		tempDir = path.join(os.tmpdir(), `pi-test-kiro-model-registry-${Snowflake.next()}`);
		fs.mkdirSync(tempDir, { recursive: true });
		modelsJsonPath = path.join(tempDir, "models.json");
		authStorage = await AuthStorage.create(path.join(tempDir, "testauth.db"));
	});

	afterEach(() => {
		resetSettingsForTest();
		authStorage.close();
		if (previousPresetRegistryDisabled === undefined) delete Bun.env.GJC_MODEL_PRESET_REGISTRY_DISABLED;
		else Bun.env.GJC_MODEL_PRESET_REGISTRY_DISABLED = previousPresetRegistryDisabled;
		if (tempDir && fs.existsSync(tempDir)) fs.rmSync(tempDir, { recursive: true });
	});

	test("kiro's default model is discoverable through the registry (package + interactive model list)", () => {
		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		try {
			const model = registry.find("kiro", "auto");
			expect(model).toBeDefined();
			expect(model?.api).toBe("kiro-codewhisperer-stream");
			expect(model?.provider).toBe("kiro");
		} finally {
			registry.dispose();
		}
	});
});

describe("Kiro standalone/import boundary smoke", () => {
	test("@gajae-code/ai exports the Kiro OAuth login/refresh entry points used by every advertised path", () => {
		expect(typeof kiroOAuthModule.getOAuthProviders).toBe("function");
		const kiro = kiroOAuthModule.getOAuthProviders().find(p => p.id === "kiro");
		expect(kiro?.available).toBe(true);
	});

	test("compiled `gjc auth-broker login kiro` reaches the real AWS SSO OIDC device-code flow, not a module resolution error (issue #5064)", async () => {
		// This is a live-binary regression test, not a mock: it builds the real
		// `dist/gjc` compiled binary and runs `auth-broker login kiro` inside a
		// scratch $bunfs process. Before this fix, `runLocalLogin()` spawned a
		// child process resolved via `import.meta.resolve("@gajae-code/ai/cli")`,
		// which requires an on-disk `node_modules` package resolution absent
		// inside a compiled binary's `$bunfs`, and crashed immediately with a
		// module-resolution error before ever reaching AWS. The fix drives
		// AuthStorage.login() in-process, so the compiled binary now reaches the
		// real device-code registration/authorization calls exactly like the
		// dev/source binary does. No AWS credentials or secrets are used or
		// required — the process is killed once it reaches the "Waiting for
		// authorization..." step (network-pending), well before any token would
		// ever be issued.
		const repoRoot = path.resolve(import.meta.dir, "../../..");
		const binaryPath = path.join(
			repoRoot,
			`packages/coding-agent/dist/gjc${process.platform === "win32" ? ".exe" : ""}`,
		);
		const buildProc = Bun.spawn(["bun", "run", "build"], {
			cwd: path.join(repoRoot, "packages/coding-agent"),
			stdout: "pipe",
			stderr: "pipe",
		});
		const [buildExit, buildStdout, buildStderr] = await Promise.all([
			buildProc.exited,
			new Response(buildProc.stdout).text(),
			new Response(buildProc.stderr).text(),
		]);
		expect(`${buildExit}\n${buildStdout}\n${buildStderr}`).toStartWith("0\n");

		const agentDir = fs.mkdtempSync(path.join(os.tmpdir(), "gjc-kiro-standalone-"));
		try {
			const proc = Bun.spawn([binaryPath, "auth-broker", "login", "kiro"], {
				cwd: agentDir,
				stdin: "ignore",
				stdout: "pipe",
				stderr: "pipe",
				env: { ...process.env, GJC_AGENT_DIR: agentDir },
			});
			const stdoutPromise = new Response(proc.stdout).text();
			const stderrPromise = new Response(proc.stderr).text();
			// The device-code flow prints "Waiting for authorization..." only after
			// successfully completing client registration and device authorization
			// over the real network \u2014 proof the compiled binary resolved and
			// executed the login path, not just an early module-resolution crash.
			// Kill after a bounded wait so the test never depends on a token ever
			// being issued.
			await Bun.sleep(15_000);
			proc.kill();
			await proc.exited;
			const [stdout, stderr] = await Promise.all([stdoutPromise, stderrPromise]);

			expect(stdout).not.toContain("Cannot find package");
			expect(stdout).not.toContain("Cannot find module");
			expect(stdout).not.toContain("Unknown OAuth provider");
			expect(stderr).not.toContain("Cannot find package");
			expect(stderr).not.toContain("Cannot find module");
			expect(stdout).toContain("Registering client with AWS SSO OIDC");
		} finally {
			fs.rmSync(agentDir, { recursive: true, force: true });
		}
	}, 120_000);
});
