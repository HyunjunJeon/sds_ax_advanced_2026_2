/**
 * Child-process probe for the config-root workflow-settings migration.
 * Runs `Settings.loadForScope` against the current working directory with an
 * optional `--agent-dir` override, then reports the resulting file state so
 * tests can assert pairing-gate, marker, backup, and migrated-value behavior
 * without depending on host directory state.
 *
 * HOME / GJC_CONFIG_DIR / GJC_CODING_AGENT_DIR are read at module load, so this
 * must run as a child process with the environment set before spawn.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { getAgentDir, getConfigRootDir } from "@gajae-code/utils";
import { YAML } from "bun";
import { Settings, SettingsMigrationTestHooks } from "../../src/config/settings";

const cwd = process.cwd();
const agentDirIndex = process.argv.indexOf("--agent-dir");
const agentDirOverride = agentDirIndex >= 0 ? process.argv[agentDirIndex + 1] : undefined;
// Test seam: when enabled, another process replaces the freshly created
// backup and edits the source at the exact point between the backup identity
// capture and the source re-hash, so the move verification fails while the
// backup pathname no longer holds this run's file. The abort path must never
// unlink the external replacement.
if (process.env.SETTINGS_MIGRATION_TEST_REPLACE_BACKUP === "1") {
	SettingsMigrationTestHooks.afterBackupIdentityCaptured = async (backupPath: string) => {
		const sourcePath = path.resolve(getConfigRootDir(), "settings.json");
		await fs.rm(backupPath, { force: true });
		await fs.writeFile(backupPath, "external-backup-content");
		await fs.writeFile(sourcePath, '{"gjc.ralplan.maxIterations":9}');
	};
}
// Test seam: when enabled, another process publishes a new file at the backup
// pathname while the migration is removing its OWN quarantined copy (after the
// quarantined entry was verified, immediately before the unlink). The removal
// must operate on the private quarantine name and never delete the file
// published at the public pathname. The source edit below guarantees the move
// verification fails so the removal path is reached.
if (process.env.SETTINGS_MIGRATION_TEST_REPLACE_BACKUP_AT_REMOVAL === "1") {
	SettingsMigrationTestHooks.afterBackupIdentityCaptured = async () => {
		await fs.writeFile(path.resolve(getConfigRootDir(), "settings.json"), '{"gjc.ralplan.maxIterations":9}');
	};
	SettingsMigrationTestHooks.beforeQuarantineRemoval = async (backupPath: string) => {
		await fs.writeFile(backupPath, "external-backup-content");
	};
}
// Test seam: when enabled, another process replaces the freshly created
// backup WITHOUT touching the source, so the move succeeds and the outer
// post-copy backup re-hash (against sourceSha256) observes the replacement.
// The mismatch cleanup must quarantine and re-verify before unlinking and
// never delete the external revision.
if (process.env.SETTINGS_MIGRATION_TEST_REPLACE_BACKUP_ONLY === "1") {
	SettingsMigrationTestHooks.afterBackupIdentityCaptured = async (backupPath: string) => {
		await fs.rm(backupPath, { force: true });
		await fs.writeFile(backupPath, "external-backup-content");
	};
}

let loadFailed = false;
try {
	await Settings.loadForScope({ cwd, ...(agentDirOverride ? { agentDir: agentDirOverride } : {}) });
} catch {
	// A settings load can fail (e.g. unavailable settings database) while the
	// workflow migrations already ran; report the migration file state anyway.
	loadFailed = true;
}

const configRoot = getConfigRootDir();
const source = path.resolve(configRoot, "settings.json");
const backup = `${source}.bak`;
const markerPath = `${source}.migrated`;
const effectiveAgentDir = agentDirOverride ? path.resolve(agentDirOverride) : getAgentDir();
const targetConfig = path.resolve(effectiveAgentDir, "config.yml");

const exists = async (target: string): Promise<boolean> => {
	try {
		await fs.lstat(target);
		return true;
	} catch {
		return false;
	}
};

let markerStatus: string | null = null;
if (await exists(markerPath)) {
	try {
		const status = (JSON.parse(await fs.readFile(markerPath, "utf8")) as { status?: unknown }).status;
		markerStatus = typeof status === "string" ? status : null;
	} catch {
		markerStatus = "invalid";
	}
}

const strictInvalidEvidencePath = `${source}.strict-invalid`;
let strictInvalidEvidenceKeys: string[] = [];
let strictInvalidEvidenceMalformed = false;
if (await exists(strictInvalidEvidencePath)) {
	try {
		const parsed = (await JSON.parse(await fs.readFile(strictInvalidEvidencePath, "utf8"))) as {
			malformed?: unknown;
			keys?: unknown;
			key?: unknown;
		};
		if (parsed.malformed === true) strictInvalidEvidenceMalformed = true;
		if (Array.isArray(parsed.keys)) {
			strictInvalidEvidenceKeys = parsed.keys
				.map(entry => (entry && typeof entry === "object" ? (entry as { key?: unknown }).key : undefined))
				.filter((value): value is string => typeof value === "string");
		} else if (typeof parsed.key === "string") {
			strictInvalidEvidenceKeys = [parsed.key];
		}
	} catch {
		// Leave the report empty; the load itself must have survived.
	}
}

let targetValue: unknown = null;
if (await exists(targetConfig)) {
	try {
		const root = YAML.parse(await fs.readFile(targetConfig, "utf8")) as Record<string, unknown> | null | undefined;
		const gjc = root?.gjc as Record<string, unknown> | undefined;
		const ralplan = gjc?.ralplan as Record<string, unknown> | undefined;
		if (ralplan && Object.hasOwn(ralplan, "maxIterations")) targetValue = ralplan.maxIterations;
	} catch {
		// Malformed target YAML: report null; the load itself must have survived.
	}
}

process.stdout.write(
	`${JSON.stringify({
		loadFailed,
		sourceExists: await exists(source),
		backupExists: await exists(backup),
		markerExists: await exists(markerPath),
		markerStatus,
		targetValue,
		strictInvalidEvidenceExists: await exists(strictInvalidEvidencePath),
		strictInvalidEvidenceKeys,
		strictInvalidEvidenceMalformed,
	})}
`,
);
