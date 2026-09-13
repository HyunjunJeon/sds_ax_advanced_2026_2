import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import * as nodePath from "node:path";
import { YAML } from "bun";
import { buildHermesSetupSpec, runHermesSetup } from "../src/setup/hermes-setup";

let tempRoot: string | undefined;

async function freshRoot(): Promise<string> {
	tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "gjc-hermes-agent-dir-"));
	return tempRoot;
}

describe("gjc setup hermes --coding-agent-dir", () => {
	afterEach(cleanup);
	async function cleanup(): Promise<void> {
		if (tempRoot) {
			await fs.rm(tempRoot, { recursive: true, force: true });
			tempRoot = undefined;
		}
	}

	it("renders GJC_CODING_AGENT_DIR next to GJC_COORDINATOR_MCP_STATE_ROOT when set", async () => {
		const root = await freshRoot();
		const agentDir = path.join(root, "agent-home");
		const stateRoot = path.join(root, "state");
		const result = await runHermesSetup({
			json: true,
			root: [root],
			stateRoot,
			codingAgentDir: agentDir,
		});

		const preview = result.previews.find(entry => entry.path.endsWith(".yaml"))?.content ?? "";
		expect(preview).toContain(`GJC_COORDINATOR_MCP_STATE_ROOT: ${stateRoot}`);
		expect(preview).toContain(`GJC_CODING_AGENT_DIR: ${agentDir}`);
	});

	it("omits GJC_CODING_AGENT_DIR when the flag is unset", async () => {
		const root = await freshRoot();
		const result = await runHermesSetup({ json: true, root: [root] });

		const preview = result.previews.find(entry => entry.path.endsWith(".yaml"))?.content ?? "";
		expect(preview).not.toContain("GJC_CODING_AGENT_DIR");
		expect(preview).not.toContain("GJC_COORDINATOR_MCP_STATE_ROOT");
	});

	it("keeps the two variables independent: setting one never renders the other", async () => {
		const root = await freshRoot();
		const agentOnly = await runHermesSetup({
			json: true,
			root: [root],
			codingAgentDir: path.join(root, "agent-home"),
		});
		const agentOnlyPreview = agentOnly.previews.find(entry => entry.path.endsWith(".yaml"))?.content ?? "";
		expect(agentOnlyPreview).toContain("GJC_CODING_AGENT_DIR");
		expect(agentOnlyPreview).not.toContain("GJC_COORDINATOR_MCP_STATE_ROOT");

		const stateOnly = await runHermesSetup({
			json: true,
			root: [root],
			stateRoot: path.join(root, "state"),
		});
		const stateOnlyPreview = stateOnly.previews.find(entry => entry.path.endsWith(".yaml"))?.content ?? "";
		expect(stateOnlyPreview).toContain("GJC_COORDINATOR_MCP_STATE_ROOT");
		expect(stateOnlyPreview).not.toContain("GJC_CODING_AGENT_DIR");
	});

	it("participates in the setup signature so --check detects drift", async () => {
		const root = await freshRoot();
		const profileDir = path.join(root, "profile");
		await runHermesSetup({
			install: true,
			root: [root],
			profileDir,
			codingAgentDir: path.join(root, "agent-a"),
		});
		expect(
			await runHermesSetup({
				check: true,
				root: [root],
				profileDir,
				codingAgentDir: path.join(root, "agent-a"),
			}),
		).toMatchObject({ ok: true, check: { mismatches: [] } });

		// Same flags but a different agent dir is a different managed signature.
		const drifted = await runHermesSetup({
			check: true,
			root: [root],
			profileDir,
			codingAgentDir: path.join(root, "agent-b"),
		});
		expect(drifted.ok).toBe(false);
	});

	it("preserves an existing GJC_CODING_AGENT_DIR on install when the flag is omitted", async () => {
		const root = await freshRoot();
		const profileDir = path.join(root, "profile");
		const operatorDir = path.join(root, "operator-agent");
		await runHermesSetup({ install: true, root: [root], profileDir, codingAgentDir: operatorDir });

		// Re-install without the flag: the managed value must survive.
		await runHermesSetup({ install: true, root: [root], profileDir });
		const preserved = await readServerEnv(profileDir);
		expect(preserved.GJC_CODING_AGENT_DIR).toBe(operatorDir);

		// Idempotent: a third install without the flag still keeps it.
		await runHermesSetup({ install: true, root: [root], profileDir });
		expect((await readServerEnv(profileDir)).GJC_CODING_AGENT_DIR).toBe(operatorDir);
	});

	it("overrides the managed value when --coding-agent-dir is provided", async () => {
		const root = await freshRoot();
		const profileDir = path.join(root, "profile");
		await runHermesSetup({
			install: true,
			root: [root],
			profileDir,
			codingAgentDir: path.join(root, "agent-old"),
		});
		const agentNew = path.join(root, "agent-new");
		await runHermesSetup({ install: true, root: [root], profileDir, codingAgentDir: agentNew });

		expect((await readServerEnv(profileDir)).GJC_CODING_AGENT_DIR).toBe(agentNew);
		// The overridden value itself survives a later flag-less re-install.
		await runHermesSetup({ install: true, root: [root], profileDir });
		expect((await readServerEnv(profileDir)).GJC_CODING_AGENT_DIR).toBe(agentNew);
	});
	it("does not preserve a foreign GJC_CODING_AGENT_DIR from an unmanaged block under --force", async () => {
		const root = await freshRoot();
		const configPath = path.join(root, "config.yaml");
		await Bun.write(
			configPath,
			YAML.stringify({
				mcp_servers: {
					gjc_coordinator: {
						command: "custom",
						env: { GJC_CODING_AGENT_DIR: path.join(root, "foreign-agent") },
					},
				},
			}),
		);

		await runHermesSetup({ install: true, force: true, root: [root], target: configPath });

		const parsed = YAML.parse(await Bun.file(configPath).text()) as {
			mcp_servers: Record<string, { env?: Record<string, string> }>;
		};
		// Preserve is scoped to blocks GJC signed itself; --force re-renders from
		// flags alone instead of adopting an untrusted env value.
		expect(parsed.mcp_servers.gjc_coordinator?.env?.GJC_CODING_AGENT_DIR).toBeUndefined();
	});

	it("refuses a tampered managed block instead of preserving the edited value", async () => {
		const root = await freshRoot();
		const profileDir = path.join(root, "profile");
		await runHermesSetup({
			install: true,
			root: [root],
			profileDir,
			codingAgentDir: path.join(root, "agent-a"),
		});
		const configPath = path.join(profileDir, "config.yaml");
		const tamperedConfig = (await Bun.file(configPath).text()).replace(
			`GJC_CODING_AGENT_DIR: ${path.join(root, "agent-a")}`,
			"GJC_CODING_AGENT_DIR: relative-injection",
		);
		await Bun.write(configPath, tamperedConfig);

		await expect(runHermesSetup({ install: true, root: [root], profileDir })).rejects.toThrow(
			"has GJC managed markers but its setup signature does not match",
		);
		// Fail-closed: the refused install leaves the tampered bytes untouched.
		expect(await Bun.file(configPath).text()).toBe(tamperedConfig);
	});

	it("normalizes the rendered value to an absolute resolved path", () => {
		const root = "/tmp/gjc-hermes-abs";
		const spec = buildHermesSetupSpec({
			root: ["/tmp/gjc-hermes-roots"],
			codingAgentDir: path.join(root, "nested", ".."),
		});
		expect(spec.codingAgentDir).toBe("/tmp/gjc-hermes-abs");
	});

	it("rejects relative paths", () => {
		const root = "/tmp/gjc-hermes-roots";
		expect(() => buildHermesSetupSpec({ root: [root], codingAgentDir: "relative/agent" })).toThrow(
			/--coding-agent-dir must be an absolute path/,
		);
		expect(() => buildHermesSetupSpec({ root: [root], codingAgentDir: "~/gjc-agent" })).toThrow(
			/--coding-agent-dir must be an absolute path/,
		);
	});

	it("refuses home, filesystem root, and /home like --root does", () => {
		const root = "/tmp/gjc-hermes-roots";
		const home = path.resolve(os.homedir());
		const cases = ["/", "/home", home, `${path.sep}`];
		for (const value of cases) {
			expect(() => buildHermesSetupSpec({ root: [root], codingAgentDir: value }), `value=${value}`).toThrow(
				/Refusing broad Hermes coding agent dir/,
			);
		}
	});

	it("accepts Windows drive and UNC spellings as absolute on win32 path rules", () => {
		const root = "/tmp/gjc-hermes-roots";
		// path.isAbsolute is the platform gate; on POSIX hosts the win32 check is
		// asserted directly against the same predicate the validator uses.
		const winDrive = "C:\\gjc\\agent";
		const winUnc = "\\\\server\\share\\gjc-agent";
		expect(nodePath.win32.isAbsolute(winDrive)).toBe(true);
		expect(nodePath.win32.isAbsolute(winUnc)).toBe(true);
		// A POSIX host still rejects them because they are not absolute here.
		if (process.platform !== "win32") {
			expect(() => buildHermesSetupSpec({ root: [root], codingAgentDir: winDrive })).toThrow(
				/--coding-agent-dir must be an absolute path/,
			);
		}
	});

	async function readServerEnv(profileDir: string): Promise<Record<string, string>> {
		const configPath = path.join(profileDir, "config.yaml");
		const parsed = YAML.parse(await Bun.file(configPath).text()) as {
			mcp_servers: Record<string, { env?: Record<string, string> }>;
		};
		return parsed.mcp_servers.gjc_coordinator?.env ?? {};
	}
});
