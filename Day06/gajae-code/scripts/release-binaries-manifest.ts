#!/usr/bin/env bun
/**
 * Build the installer/update checksum assets for a directory of standalone
 * GJC binaries. Writes `gajae-release-binaries-v1.json` and
 * `gajae-release-binaries.sha256` next to those binaries.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { createHash } from "node:crypto";

export const BINARY_MANIFEST_FILE = "gajae-release-binaries-v1.json";
export const BINARY_SHA256_FILE = "gajae-release-binaries.sha256";

export const RELEASE_BINARY_NAMES = [
	"gjc-linux-x64",
	"gjc-linux-arm64",
	"gjc-darwin-arm64",
	"gjc-darwin-x64",
	"gjc-windows-x64.exe",
] as const;

export type ReleaseBinaryName = (typeof RELEASE_BINARY_NAMES)[number];

export interface ReleaseBinaryRecord {
	name: ReleaseBinaryName;
	sha256: string;
	size: number;
}

export interface ReleaseBinariesManifest {
	schema: "gajae-release-binaries-v1";
	schema_version: 1;
	release_version: string;
	release_channel: "stable" | "nightly";
	tag: string;
	binaries: ReleaseBinaryRecord[];
}

const SHA256_RE = /^[a-f0-9]{64}$/;
const STABLE_VERSION_RE = /^\d+\.\d+\.\d+$/;
const NIGHTLY_VERSION_RE = /^\d+\.\d+\.\d+-nightly\.[0-9]+\.[0-9]+\.g[0-9a-f]+$/;

export function sha256File(filePath: string): string {
	const bytes = fs.readFileSync(filePath);
	return createHash("sha256").update(bytes).digest("hex");
}

export function parseReleaseTag(tag: string): { tag: string; version: string; channel: "stable" | "nightly" } {
	if (!tag.startsWith("v") || tag.includes("/") || tag.includes("..")) {
		throw new Error(`Invalid release tag: ${tag}`);
	}
	const version = tag.slice(1);
	if (STABLE_VERSION_RE.test(version)) return { tag, version, channel: "stable" };
	if (NIGHTLY_VERSION_RE.test(version)) return { tag, version, channel: "nightly" };
	throw new Error(`Release tag ${tag} is not a stable or nightly GJC version`);
}

export function buildReleaseBinariesManifest(options: {
	binDir: string;
	tag: string;
	channel?: "stable" | "nightly";
}): ReleaseBinariesManifest {
	const parsed = parseReleaseTag(options.tag);
	const channel = options.channel ?? parsed.channel;
	if (channel !== parsed.channel) {
		throw new Error(`Channel ${channel} does not match tag ${options.tag}`);
	}
	const binaries: ReleaseBinaryRecord[] = [];
	for (const name of RELEASE_BINARY_NAMES) {
		const filePath = path.join(options.binDir, name);
		if (!fs.existsSync(filePath)) {
			throw new Error(`Missing release binary ${name} in ${options.binDir}`);
		}
		const stat = fs.statSync(filePath);
		if (!stat.isFile() || stat.size <= 0) {
			throw new Error(`Release binary ${name} is empty or not a file`);
		}
		const digest = sha256File(filePath);
		if (!SHA256_RE.test(digest)) throw new Error(`Invalid digest for ${name}`);
		binaries.push({ name, sha256: digest, size: stat.size });
	}
	return {
		schema: "gajae-release-binaries-v1",
		schema_version: 1,
		release_version: parsed.version,
		release_channel: channel,
		tag: parsed.tag,
		binaries,
	};
}

export function formatSha256Sums(manifest: ReleaseBinariesManifest): string {
	return `${manifest.binaries.map(entry => `${entry.sha256}  ${entry.name}`).join("\n")}\n`;
}

export function writeReleaseBinariesManifest(options: {
	binDir: string;
	tag: string;
	channel?: "stable" | "nightly";
}): { manifestPath: string; sha256Path: string; manifest: ReleaseBinariesManifest } {
	const manifest = buildReleaseBinariesManifest(options);
	const manifestPath = path.join(options.binDir, BINARY_MANIFEST_FILE);
	const sha256Path = path.join(options.binDir, BINARY_SHA256_FILE);
	fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
	fs.writeFileSync(sha256Path, formatSha256Sums(manifest));
	return { manifestPath, sha256Path, manifest };
}

export function parseSha256Sums(text: string, assetName: string): string | undefined {
	for (const rawLine of text.split(/\r?\n/)) {
		const line = rawLine.trim();
		if (!line || line.startsWith("#")) continue;
		const match = line.match(/^([a-fA-F0-9]{64})  [ *]?(.+)$/);
		if (!match) continue;
		const name = path.posix.basename(match[2]!.replace(/^\.\//, ""));
		if (name === assetName) return match[1]!.toLowerCase();
	}
	return undefined;
}

if (import.meta.main) {
	const args = process.argv.slice(2);
	const binDirIndex = args.indexOf("--bin-dir");
	const tagIndex = args.indexOf("--tag");
	const channelIndex = args.indexOf("--channel");
	if (binDirIndex < 0 || tagIndex < 0 || !args[binDirIndex + 1] || !args[tagIndex + 1]) {
		throw new Error("usage: bun scripts/release-binaries-manifest.ts --bin-dir <dir> --tag <vX.Y.Z> [--channel stable|nightly]");
	}
	const channelArg = channelIndex >= 0 ? args[channelIndex + 1] : undefined;
	if (channelArg !== undefined && channelArg !== "stable" && channelArg !== "nightly") {
		throw new Error(`Invalid --channel ${channelArg}`);
	}
	const channel: "stable" | "nightly" | undefined = channelArg;
	const result = writeReleaseBinariesManifest({
		binDir: args[binDirIndex + 1]!,
		tag: args[tagIndex + 1]!,
		...(channel ? { channel } : {}),
	});
	process.stdout.write(`wrote ${result.manifestPath}\nwrote ${result.sha256Path}\n`);
}
