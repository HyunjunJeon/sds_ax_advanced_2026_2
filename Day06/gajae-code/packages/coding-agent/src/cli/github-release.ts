/**
 * GitHub release lookup and checksum helpers for binary install/update.
 * End-user updates resolve versions from GitHub, not the npm registry.
 */

import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { UpdateChannel } from "../config/update-channel";

export const RELEASE_REPO = "Yeachan-Heo/gajae-code";
export const GITHUB_API_ORIGIN = "https://api.github.com";
export const GITHUB_RELEASE_DOWNLOAD_ORIGIN = `https://github.com/${RELEASE_REPO}/releases/download`;
export const BINARY_SHA256_ASSET = "gajae-release-binaries.sha256";
export const BINARY_MANIFEST_ASSET = "gajae-release-binaries-v1.json";

const STABLE_VERSION_RE = /^\d+\.\d+\.\d+$/;
const NIGHTLY_VERSION_RE = /^\d+\.\d+\.\d+-nightly\.[0-9]+\.[0-9]+\.g[0-9a-f]+$/;
const TAG_RE = /^v[A-Za-z0-9][A-Za-z0-9._-]*$/;
const SHA256_RE = /^[a-f0-9]{64}$/;

export interface GithubReleaseInfo {
	tag: string;
	version: string;
	channel: UpdateChannel;
	htmlUrl?: string;
	warnings: string[];
}

export type GithubFetch = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

export interface GithubReleaseLookupOptions {
	channel?: UpdateChannel;
	fetchImpl?: GithubFetch;
	lookupEnv?: (name: string) => string | undefined;
	apiOrigin?: string;
	timeoutMs?: number;
	useAmbientToken?: boolean;
}

interface GithubReleaseJson {
	tag_name?: unknown;
	draft?: unknown;
	prerelease?: unknown;
	html_url?: unknown;
}

export function isSafeReleaseTag(tag: string): boolean {
	return TAG_RE.test(tag) && !tag.includes("..") && !tag.includes("/");
}

export function versionFromTag(tag: string): string {
	return tag.startsWith("v") ? tag.slice(1) : tag;
}

export function githubReleaseHeaders(token: string | undefined): Record<string, string> {
	const headers: Record<string, string> = {
		Accept: "application/vnd.github+json",
		"User-Agent": "gjc-update",
		"X-GitHub-Api-Version": "2022-11-28",
	};
	if (token) headers.Authorization = `Bearer ${token}`;
	return headers;
}

function readToken(lookupEnv?: (name: string) => string | undefined): string | undefined {
	const env = lookupEnv ?? ((name: string) => process.env[name]);
	const token = env("GITHUB_TOKEN") || env("GH_TOKEN");
	return token && token.length > 0 ? token : undefined;
}

async function readGithubJson(url: string, options: GithubReleaseLookupOptions): Promise<unknown> {
	const fetchImpl = options.fetchImpl ?? fetch;
	const timeoutMs = options.timeoutMs ?? 20_000;
	const token = options.useAmbientToken ? readToken(options.lookupEnv) : undefined;
	const response = await fetchImpl(url, {
		headers: githubReleaseHeaders(token),
		signal: AbortSignal.timeout(timeoutMs),
		redirect: "follow",
	});
	if (!response.ok) {
		throw new Error(`${url} responded ${response.status}`);
	}
	return await response.json();
}

function parseRelease(value: unknown, expectedChannel: UpdateChannel): GithubReleaseInfo {
	if (typeof value !== "object" || value === null) {
		throw new Error("GitHub release payload was not an object");
	}
	const record = value as GithubReleaseJson;
	if (record.draft === true) throw new Error("Refusing a draft GitHub release");
	const tag = typeof record.tag_name === "string" ? record.tag_name : "";
	if (!isSafeReleaseTag(tag)) throw new Error(`Refusing unsafe GitHub release tag: ${tag || "<empty>"}`);
	const version = versionFromTag(tag);
	if (expectedChannel === "nightly") {
		if (record.prerelease !== true || !NIGHTLY_VERSION_RE.test(version)) {
			throw new Error(`GitHub release ${tag} is not a nightly prerelease`);
		}
	} else if (record.prerelease === true || !STABLE_VERSION_RE.test(version)) {
		throw new Error(`GitHub release ${tag} is not a stable vX.Y.Z release`);
	}
	return {
		tag,
		version,
		channel: expectedChannel,
		htmlUrl: typeof record.html_url === "string" ? record.html_url : undefined,
		warnings: [],
	};
}

export async function fetchGithubChannelRelease(options: GithubReleaseLookupOptions = {}): Promise<GithubReleaseInfo> {
	const channel = options.channel ?? "stable";
	const apiOrigin = options.apiOrigin ?? GITHUB_API_ORIGIN;
	if (channel === "nightly") {
		const url = `${apiOrigin}/repos/${RELEASE_REPO}/releases?per_page=40`;
		const payload = await readGithubJson(url, options);
		if (!Array.isArray(payload)) {
			throw new Error(`${url} did not return a release list`);
		}
		for (const entry of payload) {
			if (typeof entry !== "object" || entry === null) continue;
			const record = entry as GithubReleaseJson;
			if (record.draft === true || record.prerelease !== true) continue;
			const tag = typeof record.tag_name === "string" ? record.tag_name : "";
			if (!isSafeReleaseTag(tag)) continue;
			const version = versionFromTag(tag);
			if (!NIGHTLY_VERSION_RE.test(version)) continue;
			return {
				tag,
				version,
				channel,
				htmlUrl: typeof record.html_url === "string" ? record.html_url : undefined,
				warnings: [],
			};
		}
		throw new Error(
			"The nightly channel has no published GitHub prerelease yet; it is populated by the scheduled nightly workflow.",
		);
	}
	const url = `${apiOrigin}/repos/${RELEASE_REPO}/releases/latest`;
	const payload = await readGithubJson(url, options);
	return parseRelease(payload, "stable");
}

export function parseChecksumForAsset(text: string, assetName: string): string | undefined {
	for (const rawLine of text.split(/\r?\n/)) {
		const line = rawLine.trim();
		if (!line || line.startsWith("#")) continue;
		const match = line.match(/^([a-fA-F0-9]{64}) [ *](.+)$/);
		if (!match) continue;
		const name = path.posix.basename(match[2]!.replace(/^\.\//, ""));
		if (name === assetName) return match[1]!.toLowerCase();
	}
	return undefined;
}

export function parseManifestChecksum(payload: unknown, assetName: string): string | undefined {
	if (typeof payload !== "object" || payload === null) return undefined;
	const binaries = (payload as { binaries?: unknown }).binaries;
	if (!Array.isArray(binaries)) return undefined;
	for (const entry of binaries) {
		if (typeof entry !== "object" || entry === null) continue;
		const record = entry as { name?: unknown; sha256?: unknown };
		if (record.name === assetName && typeof record.sha256 === "string" && SHA256_RE.test(record.sha256)) {
			return record.sha256;
		}
	}
	return undefined;
}

export function sha256Buffer(bytes: Uint8Array): string {
	return createHash("sha256").update(bytes).digest("hex");
}

export async function sha256File(filePath: string): Promise<string> {
	const bytes = await fs.promises.readFile(filePath);
	return sha256Buffer(bytes);
}

export async function fetchOptionalText(
	url: string,
	options: { fetchImpl?: GithubFetch; lookupEnv?: (name: string) => string | undefined; timeoutMs?: number } = {},
): Promise<string | undefined> {
	const fetchImpl = options.fetchImpl ?? fetch;
	const response = await fetchImpl(url, {
		headers: githubReleaseHeaders(undefined),
		signal: AbortSignal.timeout(options.timeoutMs ?? 20_000),
		redirect: "follow",
	});
	if (response.status === 404) return undefined;
	if (!response.ok) throw new Error(`${url} responded ${response.status}`);
	return await response.text();
}

export async function verifyDownloadedBinaryChecksum(options: {
	tag: string;
	assetName: string;
	filePath: string;
	fetchImpl?: GithubFetch;
	lookupEnv?: (name: string) => string | undefined;
	downloadOrigin?: string;
}): Promise<"verified"> {
	const origin = options.downloadOrigin ?? GITHUB_RELEASE_DOWNLOAD_ORIGIN;
	const sumsUrl = `${origin}/${options.tag}/${BINARY_SHA256_ASSET}`;
	const manifestUrl = `${origin}/${options.tag}/${BINARY_MANIFEST_ASSET}`;
	const actual = await sha256File(options.filePath);
	const sums = await fetchOptionalText(sumsUrl, options);
	if (sums !== undefined) {
		const expected = parseChecksumForAsset(sums, options.assetName);
		if (!expected || expected !== actual) {
			throw new Error(
				`Checksum mismatch for ${options.assetName}: expected ${expected ?? "<missing>"}, got ${actual}`,
			);
		}
		return "verified";
	}
	const manifestText = await fetchOptionalText(manifestUrl, options);
	if (manifestText !== undefined) {
		let payload: unknown;
		try {
			payload = JSON.parse(manifestText);
		} catch {
			throw new Error(`Release checksum manifest ${BINARY_MANIFEST_ASSET} was not valid JSON`);
		}
		const expected = parseManifestChecksum(payload, options.assetName);
		if (!expected || expected !== actual) {
			throw new Error(
				`Checksum mismatch for ${options.assetName}: expected ${expected ?? "<missing>"}, got ${actual}`,
			);
		}
		return "verified";
	}
	throw new Error(`Release ${options.tag} has no checksum assets; refusing to install an unsigned binary`);
}
