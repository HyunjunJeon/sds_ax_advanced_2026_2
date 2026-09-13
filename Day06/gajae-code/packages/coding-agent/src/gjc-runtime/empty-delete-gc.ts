/**
 * Official GC of already-quarantined empty `.gjc-delete-*` receipts.
 * Roots are operator operands — never hardcoded host paths.
 */

import { randomUUID } from "node:crypto";
import type { BigIntStats } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { exactUnlinkDirect, type NativeExactUnlinkResult, snapshotDirectoryTree } from "@gajae-code/natives";

export const EMPTY_DELETE_PREFIX = ".gjc-delete-";
const EMPTY_DELETE_RECEIPT_PATTERN =
	/^\.gjc-delete-session-state-lock-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$/u;
// session-storage uses a GC UUID; managed-session-scope uses stableOperationName's
// SHA-256 plus a numbered attempt. Neither arbitrary labels nor other receipts qualify.
const EMPTY_ARTIFACT_REMOVAL_ROOT_PATTERN =
	/^\.gjc-delete-(?:gc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-artifacts|[0-9a-f]{64}-artifacts-[1-9]\d*)\.removing$/u;
const EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

export interface EmptyDeleteIdentity {
	dev: bigint;
	ino: bigint;
	nlink: bigint;
	size: bigint;
	mtimeNs: bigint;
	parentDev: bigint;
	parentIno: bigint;
	sha256?: string;
}

export interface EmptyDeleteGcRecord {
	root: string;
	path: string;
	action: "would_remove" | "removed" | "kept" | "skipped";
	reason: string;
	identity?: EmptyDeleteIdentity;
	/** A dry-run candidate is an observation, never deletion authority. */
	observationOnly?: boolean;
	/** Retained-object paths a native direct unlink left behind; operator-recoverable only there. */
	retainedPaths?: { detached?: string; successor?: string; placeholder?: string; unknown?: string };
}

export interface EmptyDeleteGcReport {
	dry_run: boolean;
	roots: string[];
	records: EmptyDeleteGcRecord[];
	would_remove: number;
	removed: number;
	kept: number;
	skipped: number;
	errors: string[];
}

export interface EmptyDeleteGcOptions {
	roots: string[];
	prune: boolean;
	/** Disk artifact GC must not remove unrelated file receipts. */
	artifactRootsOnly?: boolean;
}

export interface EmptyDeleteGcDependencies {
	/** Internal seam for deterministic collect-to-prune race tests. */
	collect?: (root: string) => Promise<EmptyDeleteGcRecord[]>;
	exactUnlinkDirect?: typeof exactUnlinkDirect;
	rmdir?: (directory: string) => Promise<void>;
}

function isUnsafeName(name: string): boolean {
	return name.includes("/") || name.includes("\0") || name === "." || name === "..";
}

function identityOf(stat: BigIntStats): Omit<EmptyDeleteIdentity, "parentDev" | "parentIno" | "sha256"> {
	return {
		dev: stat.dev,
		ino: stat.ino,
		nlink: stat.nlink,
		size: stat.size,
		mtimeNs: stat.mtimeNs,
	};
}

/**
 * Extract the operator-recoverable retained paths a native direct unlink can leave behind.
 * Any of them means debris still exists outside the receipt namespace, so the caller
 * records them and fails the run instead of reporting a clean prune.
 */
function retainedPathsOf(
	result: NativeExactUnlinkResult,
): { detached?: string; successor?: string; placeholder?: string; unknown?: string } | undefined {
	if (
		result.detachedPath === undefined &&
		result.retainedSuccessorPath === undefined &&
		result.retainedPlaceholderPath === undefined &&
		result.retainedUnknownPath === undefined
	)
		return undefined;
	return {
		detached: result.detachedPath,
		successor: result.retainedSuccessorPath,
		placeholder: result.retainedPlaceholderPath,
		unknown: result.retainedUnknownPath,
	};
}

function isEmptyDeleteReceiptName(name: string): boolean {
	return EMPTY_DELETE_RECEIPT_PATTERN.test(name);
}

function isEmptyArtifactRemovalRootName(name: string): boolean {
	return EMPTY_ARTIFACT_REMOVAL_ROOT_PATTERN.test(name);
}

function artifactRootReason(file: string, stat: BigIntStats): string {
	if (!stat.isDirectory()) return "not_directory";
	// The native snapshot opens every component with O_NOFOLLOW and enumerates
	// with fdopendir/readdir on the retained directory handle. A root-only snapshot
	// proves zero entries; pathname readdir alone cannot supply that authority.
	const result = snapshotDirectoryTree(file);
	if (!result.ok || !result.snapshot) return `entry_unverifiable: ${result.code ?? "unknown"}`;
	const snapshot = result.snapshot;
	if (snapshot.rootDev !== stat.dev.toString() || snapshot.rootIno !== stat.ino.toString()) return "identity_drift";
	return snapshot.entries.length === 1 && snapshot.entries[0].relativePath === ""
		? "empty_artifact_removal_root"
		: "non_empty";
}

async function removeEmptyArtifactRoot(
	record: EmptyDeleteGcRecord,
	rmdir: (directory: string) => Promise<void>,
): Promise<void> {
	try {
		const expected = record.identity;
		if (!expected) {
			record.action = "kept";
			record.reason = "identity_missing";
			return;
		}
		const parent = await fs.lstat(path.dirname(record.path), { bigint: true });
		const stat = await fs.lstat(record.path, { bigint: true });
		if (
			!parent.isDirectory() ||
			parent.dev !== expected.parentDev ||
			parent.ino !== expected.parentIno ||
			!stat.isDirectory() ||
			stat.dev !== expected.dev ||
			stat.ino !== expected.ino
		) {
			record.action = "kept";
			record.reason = "identity_drift";
			return;
		}
		const reason = artifactRootReason(record.path, stat);
		if (reason !== "empty_artifact_removal_root") {
			record.action = "kept";
			record.reason = reason;
			return;
		}
		// Never recursive: a writer winning after the empty check makes this fail closed.
		await rmdir(record.path);
		record.action = "removed";
	} catch (error) {
		record.action = "kept";
		record.reason = `entry_remove_failed: ${error instanceof Error ? error.message : String(error)}`;
	}
}

function sameRootIdentity(left: BigIntStats, right: BigIntStats): boolean {
	return left.isDirectory() && right.isDirectory() && left.dev === right.dev && left.ino === right.ino;
}

function rootRaceRecord(root: string): EmptyDeleteGcRecord[] {
	return [{ root, path: root, action: "skipped", reason: "root_race" }];
}

export async function collectEmptyDeleteReceipts(
	root: string,
	deps: { lstat?: (file: string, options: { bigint: true }) => Promise<BigIntStats> } = {},
): Promise<EmptyDeleteGcRecord[]> {
	const lstatFile = deps.lstat ?? ((file: string) => fs.lstat(file, { bigint: true }));
	const records: EmptyDeleteGcRecord[] = [];
	const resolvedRoot = path.resolve(root);
	let rootStat: BigIntStats;
	try {
		rootStat = await lstatFile(resolvedRoot, { bigint: true });
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") {
			return [{ root, path: root, action: "skipped", reason: "missing_root" }];
		}
		throw error;
	}
	if (rootStat.isSymbolicLink()) {
		return [{ root, path: root, action: "skipped", reason: "symlink_root" }];
	}
	if (!rootStat.isDirectory()) {
		return [{ root, path: root, action: "skipped", reason: "not_directory" }];
	}
	let names: string[];
	try {
		names = await fs.readdir(resolvedRoot);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") {
			return rootRaceRecord(root);
		}
		throw error;
	}
	// Enumeration is the first operation that can observe a replacement root. Do not
	// attribute any names from that enumeration until the original root identity is
	// proved to still own the pathname.
	try {
		const enumeratedRoot = await lstatFile(resolvedRoot, { bigint: true });
		if (!sameRootIdentity(rootStat, enumeratedRoot)) return rootRaceRecord(root);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return rootRaceRecord(root);
		throw error;
	}
	for (const name of names) {
		if (isUnsafeName(name) || (!isEmptyDeleteReceiptName(name) && !isEmptyArtifactRemovalRootName(name))) continue;
		const file = path.join(resolvedRoot, name);
		let stat: BigIntStats;
		try {
			stat = await lstatFile(file, { bigint: true });
		} catch (error) {
			// ENOENT is an ordinary race with concurrent cleanup, but silently dropping the
			// entry would shrink the report without trace; record it as a raced candidate so
			// discovery stays fail-closed. Any other stat failure is a hard discovery error.
			if ((error as NodeJS.ErrnoException).code === "ENOENT") {
				records.push({ root, path: file, action: "skipped", reason: "raced" });
				continue;
			}
			throw error;
		}
		if (stat.isSymbolicLink()) {
			records.push({ root, path: file, action: "kept", reason: "symlink" });
			continue;
		}
		if (isEmptyArtifactRemovalRootName(name)) {
			const reason = artifactRootReason(file, stat);
			records.push({
				root,
				path: file,
				action: reason === "empty_artifact_removal_root" ? "would_remove" : "kept",
				reason,
				identity: { ...identityOf(stat), parentDev: rootStat.dev, parentIno: rootStat.ino },
			});
			continue;
		}
		if (!stat.isFile()) {
			records.push({ root, path: file, action: "kept", reason: "not_regular" });
			continue;
		}
		if (stat.size !== 0n) {
			records.push({ root, path: file, action: "kept", reason: "non_empty" });
			continue;
		}
		if (stat.nlink !== 1n) {
			records.push({ root, path: file, action: "kept", reason: "nlink" });
			continue;
		}
		records.push({
			root,
			path: file,
			action: "would_remove",
			reason: "empty_delete_receipt",
			identity: {
				...identityOf(stat),
				parentDev: rootStat.dev,
				parentIno: rootStat.ino,
				sha256: EMPTY_FILE_SHA256,
			},
		});
	}
	// The candidate lstat calls above are pathname operations. Revalidate once more
	// before returning so a replaced root can never leak its external candidate paths
	// (or their identities) into a GC report.
	try {
		const finalRoot = await lstatFile(resolvedRoot, { bigint: true });
		if (!sameRootIdentity(rootStat, finalRoot)) return rootRaceRecord(root);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return rootRaceRecord(root);
		throw error;
	}
	return records;
}

export async function runEmptyDeleteGc(
	options: EmptyDeleteGcOptions,
	deps: EmptyDeleteGcDependencies = {},
): Promise<EmptyDeleteGcReport> {
	const report: EmptyDeleteGcReport = {
		dry_run: !options.prune,
		roots: options.roots,
		records: [],
		would_remove: 0,
		removed: 0,
		kept: 0,
		skipped: 0,
		errors: [],
	};
	const collect = deps.collect ?? collectEmptyDeleteReceipts;
	const unlink = deps.exactUnlinkDirect ?? exactUnlinkDirect;
	for (const root of options.roots) {
		let records: EmptyDeleteGcRecord[];
		try {
			records = await collect(root);
		} catch (error) {
			report.errors.push(`${root}: ${error instanceof Error ? error.message : String(error)}`);
			continue;
		}
		for (const record of records) {
			if (
				options.artifactRootsOnly &&
				record.path !== root &&
				!isEmptyArtifactRemovalRootName(path.basename(record.path))
			)
				continue;
			if (record.action === "would_remove" && !options.prune) record.observationOnly = true;
			if (
				record.action === "skipped" &&
				(record.reason === "raced" ||
					record.reason === "root_race" ||
					(record.path === root &&
						(record.reason === "missing_root" ||
							record.reason === "symlink_root" ||
							record.reason === "not_directory")))
			) {
				report.errors.push(`${record.path}: ${record.reason}`);
			}
			if (
				record.action === "would_remove" &&
				options.prune &&
				isEmptyArtifactRemovalRootName(path.basename(record.path))
			) {
				await removeEmptyArtifactRoot(record, deps.rmdir ?? fs.rmdir);
				if (record.reason.startsWith("entry_remove_failed:"))
					report.errors.push(`${record.path}: ${record.reason}`);
			} else if (record.action === "would_remove" && options.prune) {
				try {
					if (!record.identity) {
						record.action = "kept";
						record.reason = "identity_missing";
					} else {
						const result = unlink(record.path, {
							...record.identity,
							quarantineName: `.gjc-delete-gc-${randomUUID()}.json`,
						});
						const retainedPaths = retainedPathsOf(result);
						if (result.ok) {
							record.action = "removed";
						} else if (result.code === "not_found") {
							record.action = "skipped";
							record.reason = "gone";
						} else if (result.code === "identity_mismatch") {
							record.action = "kept";
							record.reason = "identity_drift";
						} else {
							record.action = "kept";
							record.reason = `unlink_failed:${result.code ?? "unknown"}`;
							report.errors.push(`${record.path}: ${record.reason}`);
						}
						// Objects the native layer retained (post-detach failure or a successor at
						// the private quarantine name) are recoverable only at those paths; surface
						// them and fail the run instead of silently stranding debris.
						if (retainedPaths !== undefined) {
							record.retainedPaths = retainedPaths;
							record.reason = result.ok ? `retained:${result.code ?? "cleanup_pending"}` : record.reason;
							report.errors.push(
								`${record.path}: retained debris requires operator recovery: ${JSON.stringify(retainedPaths)}`,
							);
						}
					}
				} catch (error) {
					if ((error as NodeJS.ErrnoException).code === "ENOENT") {
						record.action = "skipped";
						record.reason = "gone";
					} else {
						record.action = "kept";
						record.reason = `unlink_failed:${error instanceof Error ? error.message : String(error)}`;
						report.errors.push(`${record.path}: ${record.reason}`);
					}
				}
			}
			report.records.push(record);
			if (record.action === "would_remove") report.would_remove += 1;
			else if (record.action === "removed") report.removed += 1;
			else if (record.action === "skipped") report.skipped += 1;
			else report.kept += 1;
		}
	}
	return report;
}
