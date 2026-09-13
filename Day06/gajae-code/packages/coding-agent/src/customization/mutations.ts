/**
 * Safe mutations for the `/extensions` umbrella customization surface
 * (issue #4291). Every operation goes through the same canonical
 * loaders/writers the runtime and CLI use — no parallel state model.
 *
 * - Skills: enable/disable via the authoritative `setNativeSkillEnabled`
 *   policy contract (the caller persists the returned `disabledExtensions`
 *   list through Settings); remove targets the exact discovered SKILL.md
 *   path identity with bundled-name and symlink protection.
 * - MCPs: enable/disable via the `enabled` flag and remove via the canonical
 *   config writer (atomic write, cache invalidation included).
 * - Hooks: remove only, by exact discovered path; enable/disable is not part
 *   of the canonical hook contract and is rejected by the UI with a
 *   diagnostic.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { setNativeSkillEnabled } from "../extensibility/skill-management";
import {
	readMCPConfigFile,
	removeMCPServer,
	setServerDisabled,
	writeMCPConfigFile,
} from "../runtime-mcp/config-writer";
import { CANONICAL_GJC_WORKFLOW_SKILLS } from "../skill-state/canonical-skills";

const BUNDLED_SKILL_NAMES: ReadonlySet<string> = new Set(CANONICAL_GJC_WORKFLOW_SKILLS);

export type MutationResult = { ok: true } | { ok: false; reason: string };

// ---------------------------------------------------------------------------
// Skills
// ---------------------------------------------------------------------------

/**
 * Toggle a native skill through the authoritative policy contract. Returns
 * the updated `disabledExtensions` list; the caller persists it via Settings.
 * Bundled workflow skill names cannot be disabled.
 */
export function setSkillEnabled(
	name: string,
	enabled: boolean,
	disabledExtensions: string[],
): MutationResult & { disabledExtensions?: string[] } {
	if (!enabled && BUNDLED_SKILL_NAMES.has(name)) {
		return { ok: false, reason: `"${name}" is a protected bundled workflow skill name` };
	}
	return { ok: true, disabledExtensions: setNativeSkillEnabled(name, enabled, disabledExtensions) };
}

/**
 * Remove a native skill by its exact discovered SKILL.md path identity.
 * Fails when the discovered file is absent, refuses symlinked directories or
 * files, and never removes protected bundled workflow skill names.
 */
export async function removeSkill(record: { name: string; path: string }): Promise<MutationResult> {
	if (BUNDLED_SKILL_NAMES.has(record.name)) {
		return { ok: false, reason: `"${record.name}" is a protected bundled workflow skill name` };
	}
	const dir = path.dirname(record.path);
	try {
		const fileStat = await fs.lstat(record.path);
		if (fileStat.isSymbolicLink())
			return { ok: false, reason: `refusing to remove symlinked skill file: ${record.path}` };
		if (!fileStat.isFile())
			return { ok: false, reason: `discovered skill file is not a regular file: ${record.path}` };
		const dirStat = await fs.lstat(dir);
		if (dirStat.isSymbolicLink())
			return { ok: false, reason: `refusing to remove symlinked skill directory: ${dir}` };
		if (!dirStat.isDirectory()) return { ok: false, reason: `not a skill directory: ${dir}` };
	} catch {
		return { ok: false, reason: `discovered skill file is absent: ${record.path}` };
	}
	await fs.rm(dir, { recursive: true, force: false });
	return { ok: true };
}

// ---------------------------------------------------------------------------
// MCPs
// ---------------------------------------------------------------------------

/** Toggle a server through the canonical disabledServers denylist. */
export async function setMcpServerEnabled(
	mcpConfigPath: string,
	name: string,
	enabled: boolean,
	disabledExtensions: string[] = [],
): Promise<MutationResult> {
	const config = await readMCPConfigFile(mcpConfigPath).catch(() => null);
	if (!config) return { ok: false, reason: `${mcpConfigPath} is malformed; fix or remove it first` };
	const server = config.mcpServers?.[name];
	if (!server) return { ok: false, reason: `server "${name}" not found in ${mcpConfigPath}` };
	if (enabled && server.enabled === false) {
		const updatedServer = { ...server };
		delete updatedServer.enabled;
		await writeMCPConfigFile(mcpConfigPath, {
			...config,
			mcpServers: { ...config.mcpServers, [name]: updatedServer },
		});
	}
	await setServerDisabled(mcpConfigPath, name, !enabled);
	const id = `mcp:${name}`;
	const nextDisabledExtensions = enabled
		? disabledExtensions.filter(entry => entry !== id)
		: disabledExtensions.includes(id)
			? disabledExtensions
			: [...disabledExtensions, id];
	return { ok: true, disabledExtensions: nextDisabledExtensions } as MutationResult & {
		disabledExtensions: string[];
	};
}

/** Remove a server via the canonical config writer. */
export async function removeMcpServerEntry(mcpConfigPath: string, name: string): Promise<MutationResult> {
	try {
		await removeMCPServer(mcpConfigPath, name);
		return { ok: true };
	} catch (error) {
		return { ok: false, reason: (error as Error).message };
	}
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

/**
 * Remove a hook by its exact discovered path. Refuses symlinks, non-files,
 * and paths that do not exist (no silent success).
 */
export async function removeHookFile(hookPath: string): Promise<MutationResult> {
	try {
		const stat = await fs.lstat(hookPath);
		if (stat.isSymbolicLink()) return { ok: false, reason: `refusing to remove symlinked hook: ${hookPath}` };
		if (!stat.isFile()) return { ok: false, reason: `not a hook file: ${hookPath}` };
		await fs.rm(hookPath, { force: false });
		return { ok: true };
	} catch (error) {
		return { ok: false, reason: (error as Error).message };
	}
}
