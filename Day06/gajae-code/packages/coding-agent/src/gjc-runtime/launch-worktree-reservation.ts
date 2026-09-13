/**
 * Cross-process reservation for a deterministic launch-worktree path.
 *
 * The durable session index is the long-lived ownership record, but a newly
 * planned worktree has no index row yet. This reservation closes that interval:
 * it is acquired before preparation and removed after `host_registered` is
 * durably appended to the index.
 */

import { createHash, randomUUID } from "node:crypto";
import type { Dirent } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { resolveEquivalentPath } from "@gajae-code/utils";
import {
	type FileLockGcObservation,
	processStartTime,
	readFileLockObservationForGc,
	removeFileLockDirForGc,
} from "../config/file-lock";
import { processIncarnation } from "../sdk/broker/process-incarnation";

const RESERVATION_VERSION = 1;
const RESERVATION_DIRECTORY = "launch-worktree-reservations";

interface LaunchWorktreeReservationRecord {
	version: typeof RESERVATION_VERSION;
	worktreePath: string;
	pid: number;
	start_time?: string;
	processIncarnation: string | null;
	timestamp: number;
	reservationId: string;
}

export interface LaunchWorktreeReservation {
	release(): Promise<void>;
}

function canonicalWorktreePath(worktreePath: string): string {
	return resolveEquivalentPath(path.resolve(worktreePath));
}

function reservationRoot(agentDir: string): string {
	return path.join(agentDir, "sdk", "sessions", RESERVATION_DIRECTORY);
}

function launchWorktreeReservationDirectory(agentDir: string, worktreePath: string): string {
	const digest = createHash("sha256").update(canonicalWorktreePath(worktreePath)).digest("hex");
	return path.join(reservationRoot(agentDir), `${digest}.lock`);
}

/** Test seam for seeding a dead-owner reservation at the exact production path. */
export function launchWorktreeReservationDirectoryForTest(agentDir: string, worktreePath: string): string {
	return launchWorktreeReservationDirectory(agentDir, worktreePath);
}

function isErrno(error: unknown, code: string): boolean {
	return typeof error === "object" && error !== null && "code" in error && error.code === code;
}

function isReservationRecord(value: unknown): value is LaunchWorktreeReservationRecord {
	if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
	const record = value as Partial<LaunchWorktreeReservationRecord>;
	return (
		record.version === RESERVATION_VERSION &&
		typeof record.worktreePath === "string" &&
		record.worktreePath.length > 0 &&
		typeof record.pid === "number" &&
		Number.isSafeInteger(record.pid) &&
		record.pid > 0 &&
		(record.start_time === undefined || (typeof record.start_time === "string" && record.start_time.length > 0)) &&
		(record.processIncarnation === null ||
			(typeof record.processIncarnation === "string" && record.processIncarnation.length > 0)) &&
		typeof record.timestamp === "number" &&
		Number.isFinite(record.timestamp) &&
		typeof record.reservationId === "string" &&
		record.reservationId.length > 0
	);
}

async function readReservation(lockDir: string): Promise<LaunchWorktreeReservationRecord | null> {
	let value: unknown;
	try {
		value = await Bun.file(path.join(lockDir, "info")).json();
	} catch (error) {
		if (isErrno(error, "ENOENT") || error instanceof SyntaxError) return null;
		throw error;
	}
	return isReservationRecord(value) ? value : null;
}

function ownerDefinitelyExited(record: LaunchWorktreeReservationRecord): boolean {
	try {
		process.kill(record.pid, 0);
	} catch (error) {
		return isErrno(error, "ESRCH");
	}
	if (record.processIncarnation !== null) {
		const actualIncarnation = processIncarnation(record.pid);
		if (actualIncarnation !== undefined) return actualIncarnation !== record.processIncarnation;
	}
	if (record.start_time) {
		const actualStartTime = processStartTime(record.pid);
		if (actualStartTime !== null) return actualStartTime !== record.start_time;
	}
	return false;
}

async function removeReservation(
	lockDir: string,
	record: LaunchWorktreeReservationRecord,
): Promise<"removed" | "kept"> {
	// Capture the exact on-disk identity BEFORE authorizing removal: the generic
	// GC remover only deletes a tree whose pre-verdict identity it was handed,
	// so a successor reservation published at the same path is never reaped.
	let observation: FileLockGcObservation | null;
	try {
		observation = await readFileLockObservationForGc(lockDir);
	} catch {
		return "kept";
	}
	if (!observation) return "kept";
	let onDisk: unknown;
	try {
		onDisk = JSON.parse(observation.bytes);
	} catch {
		return "kept";
	}
	if (
		!isReservationRecord(onDisk) ||
		onDisk.reservationId !== record.reservationId ||
		onDisk.pid !== record.pid ||
		onDisk.timestamp !== record.timestamp ||
		canonicalWorktreePath(onDisk.worktreePath) !== canonicalWorktreePath(record.worktreePath)
	)
		return "kept";
	const result = await removeFileLockDirForGc(lockDir, observation.info, observation.identity);
	return result === "removed" ? "removed" : "kept";
}

async function writeReservation(lockDir: string, record: LaunchWorktreeReservationRecord): Promise<void> {
	try {
		await Bun.write(path.join(lockDir, "info"), JSON.stringify(record));
	} catch (error) {
		await fs.rm(lockDir, { recursive: true, force: true }).catch(() => undefined);
		throw error;
	}
}

async function tryPublishReservation(lockDir: string, record: LaunchWorktreeReservationRecord): Promise<boolean> {
	const pendingDir = `${lockDir}.pending.${process.pid}.${randomUUID()}`;
	await fs.mkdir(pendingDir, { mode: 0o700 });
	try {
		await writeReservation(pendingDir, record);
		try {
			await fs.rename(pendingDir, lockDir);
			return true;
		} catch (error) {
			if (isErrno(error, "EEXIST") || isErrno(error, "ENOTEMPTY")) return false;
			if (isErrno(error, "EPERM")) {
				try {
					await fs.stat(lockDir);
					return false;
				} catch (statError) {
					if (!isErrno(statError, "ENOENT")) throw statError;
				}
			}
			throw error;
		}
	} finally {
		await fs.rm(pendingDir, { recursive: true, force: true }).catch(() => undefined);
	}
}

/**
 * Atomically claim `worktreePath`, returning `null` only when another live or
 * unverified launch already owns the same deterministic target. A dead or
 * PID-reused owner is reclaimed before retrying the exclusive directory create.
 */
export async function reserveLaunchWorktree(
	agentDir: string,
	worktreePath: string,
): Promise<LaunchWorktreeReservation | null> {
	const canonicalPath = canonicalWorktreePath(worktreePath);
	const lockDir = launchWorktreeReservationDirectory(agentDir, canonicalPath);
	await fs.mkdir(path.dirname(lockDir), { recursive: true, mode: 0o700 });
	const ownStartTime = processStartTime(process.pid) ?? undefined;
	const ownIncarnation = processIncarnation(process.pid) ?? null;

	for (let attempt = 0; attempt < 2; attempt += 1) {
		const record: LaunchWorktreeReservationRecord = {
			version: RESERVATION_VERSION,
			worktreePath: canonicalPath,
			pid: process.pid,
			...(ownStartTime ? { start_time: ownStartTime } : {}),
			processIncarnation: ownIncarnation,
			timestamp: Date.now(),
			reservationId: randomUUID(),
		};
		if (await tryPublishReservation(lockDir, record)) {
			return {
				async release(): Promise<void> {
					await removeReservation(lockDir, record);
				},
			};
		}

		const existing = await readReservation(lockDir);
		if (
			!existing ||
			canonicalWorktreePath(existing.worktreePath) !== canonicalPath ||
			!ownerDefinitelyExited(existing)
		)
			return null;
		if ((await removeReservation(lockDir, existing)) !== "removed") return null;
	}

	return null;
}

/** Remove the transient reservation only after the matching worktree is indexed. */
export async function releaseLaunchWorktreeReservationAfterRegistration(
	agentDir: string,
	worktreePath: string,
): Promise<void> {
	const canonicalPath = canonicalWorktreePath(worktreePath);
	let entries: Dirent[];
	try {
		entries = await fs.readdir(reservationRoot(agentDir), { withFileTypes: true });
	} catch (error) {
		if (isErrno(error, "ENOENT")) return;
		throw error;
	}
	for (const entry of entries) {
		if (!entry.isDirectory() || !entry.name.endsWith(".lock")) continue;
		const lockDir = path.join(reservationRoot(agentDir), entry.name);
		const record = await readReservation(lockDir);
		if (!record || canonicalWorktreePath(record.worktreePath) !== canonicalPath) continue;
		await removeReservation(lockDir, record);
	}
}
