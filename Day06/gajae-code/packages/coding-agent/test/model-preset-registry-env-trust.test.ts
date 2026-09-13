import { afterEach, describe, expect, setDefaultTimeout, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { registerOwnedDeletionRoot, safeRmSync } from "../../../scripts/safe-cleanup";

const PROBE = path.join(import.meta.dir, "fixtures", "model-preset-registry-env-probe.ts");
const REGISTRY_ENV_KEYS = ["GJC_MODEL_PRESET_REGISTRY_URL", "GJC_MODEL_PRESET_REGISTRY_DISABLED"] as const;
const PROJECT_URL = "https://project-presets.invalid/latest.json";
const OPERATOR_URL = "https://operator-presets.invalid/latest.json";
const USER_URL = "https://user-presets.invalid/latest.json";
const INJECTED_URL = "https://injected-presets.invalid/latest.json";

interface ProbeResult {
	requestedUrl: string | null;
	status: string | null;
	error: string | null;
	defaultUrl: string;
}

interface ScratchLayout {
	root: string;
	project: string;
	home: string;
	agentDir: string;
	forgetDeletionGrant: () => void;
}

const scratchLayouts: ScratchLayout[] = [];
setDefaultTimeout(30_000);

function scratchLayout(projectEnv?: string, homeEnv?: string): ScratchLayout {
	const root = path.join(os.tmpdir(), `gjc-preset-env-trust-${crypto.randomUUID()}`);
	const forgetDeletionGrant = registerOwnedDeletionRoot(root);
	const project = path.join(root, "project");
	const home = path.join(root, "home");
	const agentDir = path.join(root, "agent");
	fs.mkdirSync(project, { recursive: true });
	fs.mkdirSync(home, { recursive: true });
	fs.mkdirSync(agentDir, { recursive: true });
	if (projectEnv !== undefined) fs.writeFileSync(path.join(project, ".env"), projectEnv);
	if (homeEnv !== undefined) fs.writeFileSync(path.join(home, ".env"), homeEnv);
	const layout = { root, project, home, agentDir, forgetDeletionGrant };
	scratchLayouts.push(layout);
	return layout;
}

afterEach(() => {
	for (const layout of scratchLayouts.splice(0)) {
		safeRmSync(layout.root, { recursive: true, force: true });
		layout.forgetDeletionGrant();
	}
});

async function runProbe(
	layout: ScratchLayout,
	overrides: Record<string, string> = {},
	dependencyManifestUrl?: string,
): Promise<ProbeResult> {
	const env: Record<string, string> = {
		PATH: process.env.PATH ?? "/usr/bin:/bin",
		HOME: layout.home,
		USERPROFILE: layout.home,
		TMPDIR: path.join(layout.root, "tmp"),
		XDG_CONFIG_HOME: path.join(layout.root, "xdg-config"),
		XDG_CACHE_HOME: path.join(layout.root, "xdg-cache"),
		XDG_DATA_HOME: path.join(layout.root, "xdg-data"),
		GJC_CODING_AGENT_DIR: layout.agentDir,
	};
	for (const key of ["LANG", "LC_ALL", "TZ"] as const) {
		const value = process.env[key];
		if (value) env[key] = value;
	}
	for (const key of REGISTRY_ENV_KEYS) delete env[key];
	Object.assign(env, overrides);
	fs.mkdirSync(env.TMPDIR, { recursive: true });

	const args = [process.execPath, PROBE];
	if (dependencyManifestUrl !== undefined) args.push(dependencyManifestUrl);
	const proc = Bun.spawn(args, { cwd: layout.project, env, stdout: "pipe", stderr: "pipe" });
	const [stdout, stderr, exitCode] = await Promise.all([
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
		proc.exited,
	]);
	if (exitCode !== 0) throw new Error(`registry env probe failed (${exitCode}): ${stderr}`);
	return JSON.parse(stdout.trim()) as ProbeResult;
}

describe("model preset registry environment trust boundary", () => {
	test("project .env cannot redirect or disable the registry", async () => {
		const result = await runProbe(
			scratchLayout(`GJC_MODEL_PRESET_REGISTRY_URL=${PROJECT_URL}\nGJC_MODEL_PRESET_REGISTRY_DISABLED=1\n`),
		);

		expect(result.requestedUrl).toBe(result.defaultUrl);
		expect(result.status).toBeNull();
		expect(result.error).toContain("304 without a verified cached generation");
	});

	test("explicitly inherited operator variables still redirect and disable", async () => {
		const redirected = await runProbe(scratchLayout(), { GJC_MODEL_PRESET_REGISTRY_URL: OPERATOR_URL });
		expect(redirected.requestedUrl).toBe(OPERATOR_URL);
		expect(redirected.error).toContain("304 without a verified cached generation");

		const disabled = await runProbe(scratchLayout(), { GJC_MODEL_PRESET_REGISTRY_DISABLED: "true" });
		expect(disabled).toMatchObject({ requestedUrl: null, status: "disabled", error: null });
	});

	test("user-owned env configuration remains trusted", async () => {
		const redirected = await runProbe(scratchLayout(undefined, `GJC_MODEL_PRESET_REGISTRY_URL=${USER_URL}\n`));
		expect(redirected.requestedUrl).toBe(USER_URL);

		const shellConfigured = scratchLayout();
		fs.writeFileSync(path.join(shellConfigured.home, ".bashrc"), "export GJC_MODEL_PRESET_REGISTRY_DISABLED=yes\n");
		const disabled = await runProbe(shellConfigured);
		expect(disabled).toMatchObject({ requestedUrl: null, status: "disabled", error: null });
	});

	test("an injected manifest URL retains precedence over trusted environment", async () => {
		const result = await runProbe(
			scratchLayout(undefined, `GJC_MODEL_PRESET_REGISTRY_URL=${USER_URL}\n`),
			{ GJC_MODEL_PRESET_REGISTRY_URL: OPERATOR_URL },
			INJECTED_URL,
		);
		expect(result.requestedUrl).toBe(INJECTED_URL);
	});
});
