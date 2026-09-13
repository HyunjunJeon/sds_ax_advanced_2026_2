#!/usr/bin/env bun

/**
 * Checks spoofed external tool versions against their latest upstream releases.
 *
 * We impersonate several external tools (Claude Code, Gemini CLI) via
 * User-Agent and attribution headers. When these tools release new versions,
 * the upstream service may start rejecting or deprioritizing older versions —
 * Anthropic in particular gates newer models behind a minimum client version,
 * so a stale `claude-cli/<version>` surfaces as an HTTP 400 on a model that
 * otherwise works. This script detects drift so we can bump before users hit it.
 *
 * Usage:
 *   bun scripts/check-spoofed-versions.ts           # check and report
 *   bun scripts/check-spoofed-versions.ts --update  # update source in-place
 */

import * as path from "node:path";

const REPO_ROOT = path.join(import.meta.dir, "..");

interface VersionCheck {
	/** Human label for the report. */
	name: string;
	/** Repo-relative source file holding the spoofed version literal. */
	file: string;
	/** Must capture the bare semver in group 1. */
	sourcePattern: RegExp;
	/** Resolves the newest upstream version, or null when every source failed. */
	fetchLatest: () => Promise<string | null>;
}

const SEMVER_RE = /(\d+\.\d+\.\d+)/;

const USER_AGENT = "gajae-code/version-check";

/** Fetch latest non-prerelease tag from a GitHub repo. */
async function fetchLatestGitHubRelease(repo: string): Promise<string | null> {
	try {
		// /releases/latest only returns non-prerelease, non-draft releases
		const res = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, {
			headers: { Accept: "application/vnd.github+json", "User-Agent": USER_AGENT },
		});
		if (!res.ok) return null;
		const data = (await res.json()) as { tag_name?: string };
		return data.tag_name ? (SEMVER_RE.exec(data.tag_name)?.[1] ?? null) : null;
	} catch {
		return null;
	}
}

/**
 * Anthropic's native-installer release channel. `stable` deliberately lags the
 * npm package during a staged rollout, so `latest` is the channel that matches
 * what a freshly updated Claude Code actually reports.
 */
const CLAUDE_RELEASE_CHANNEL =
	"https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/latest";

const CLAUDE_NPM_DIST_TAG = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest";

async function fetchClaudeReleaseChannel(): Promise<string | null> {
	try {
		const res = await fetch(CLAUDE_RELEASE_CHANNEL, { headers: { "User-Agent": USER_AGENT } });
		if (!res.ok) return null;
		return SEMVER_RE.exec((await res.text()).trim())?.[1] ?? null;
	} catch {
		return null;
	}
}

async function fetchClaudeNpmVersion(): Promise<string | null> {
	try {
		const res = await fetch(CLAUDE_NPM_DIST_TAG, { headers: { "User-Agent": USER_AGENT } });
		if (!res.ok) return null;
		const data = (await res.json()) as { version?: string };
		return data.version ? (SEMVER_RE.exec(data.version)?.[1] ?? null) : null;
	} catch {
		return null;
	}
}

function compareSemver(a: string, b: string): number {
	const left = a.split(".").map(Number);
	const right = b.split(".").map(Number);
	for (let i = 0; i < 3; i++) {
		const diff = (left[i] ?? 0) - (right[i] ?? 0);
		if (diff !== 0) return diff;
	}
	return 0;
}

/** Highest of the candidates, so one lagging or unavailable source cannot pin us back. */
function highest(candidates: ReadonlyArray<string | null>): string | null {
	const known = candidates.filter((value): value is string => value !== null);
	if (known.length === 0) return null;
	return known.reduce((best, value) => (compareSemver(value, best) > 0 ? value : best));
}

const checks: VersionCheck[] = [
	{
		name: "Claude Code",
		file: "packages/ai/src/providers/anthropic.ts",
		sourcePattern: /claudeCodeVersion\s*=\s*"(\d+\.\d+\.\d+)"/,
		fetchLatest: async () => highest(await Promise.all([fetchClaudeReleaseChannel(), fetchClaudeNpmVersion()])),
	},
	{
		name: "Gemini CLI",
		file: "packages/ai/src/providers/google-gemini-headers.ts",
		sourcePattern: /DEFAULT_GEMINI_CLI_VERSION\s*=\s*"(\d+\.\d+\.\d+)"/,
		fetchLatest: () => fetchLatestGitHubRelease("google-gemini/gemini-cli"),
	},
];

async function run() {
	const doUpdate = process.argv.includes("--update");
	const pending = new Map<string, string>();
	let anyDrift = false;
	let anyChecked = false;

	for (const check of checks) {
		const absolute = path.join(REPO_ROOT, check.file);
		const source = pending.get(absolute) ?? (await Bun.file(absolute).text());
		const match = check.sourcePattern.exec(source);
		if (!match?.[1]) {
			console.error(`[WARN] Could not extract current ${check.name} version from ${check.file}`);
			continue;
		}

		const current = match[1];
		const latest = await check.fetchLatest();

		if (!latest) {
			console.error(`[FAIL] Could not fetch latest ${check.name} version`);
			continue;
		}

		anyChecked = true;

		if (compareSemver(current, latest) >= 0) {
			console.log(`[OK]   ${check.name}: ${current} (up to date)`);
			continue;
		}

		console.log(`[DRIFT] ${check.name}: ${current} -> ${latest}`);
		anyDrift = true;

		if (doUpdate) {
			pending.set(absolute, source.replace(match[0], match[0].replace(current, latest)));
			console.log(`       Updated in source.`);
		}
	}

	for (const [absolute, source] of pending) {
		await Bun.write(absolute, source);
		console.log(`\nWrote updates to ${path.relative(process.cwd(), absolute)}`);
	}

	if (!anyChecked) {
		console.error("\nNo version checks succeeded. Cannot verify freshness.");
		process.exit(1);
	}

	if (anyDrift && !doUpdate) {
		console.log("\nRun with --update to apply version bumps.");
		process.exit(1);
	}
}

run();
