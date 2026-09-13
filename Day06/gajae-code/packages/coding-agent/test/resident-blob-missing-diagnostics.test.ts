import { afterEach, describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { logger } from "@gajae-code/utils";
import { EphemeralBlobStore, ResidentBlobMissingError, resolveTextBlobSync } from "../src/session/blob-store";
import {
	assertResidentReferencesResolvableForTests,
	type FileEntry,
	materializeResidentEntriesForPersistenceForTests,
	materializeResidentEntriesThrowingForTests,
	residentBlobSentinelForTests,
} from "../src/session/session-manager";

// A missing resident blob is fail-closed: it aborts rather than leaking a
// `blob:sha256:` ref. #4670 stopped the demotion salvage from taking that path
// and gave the placeholder substitution its own report. The remaining throw
// sites were still the least observable failure in the session layer: the error
// dropped the session binding it already held, and the throw emitted nothing,
// while the *non-fatal* legacy image resolvers warn on every miss.

const tmpRoots: string[] = [];

function makeStore(): EphemeralBlobStore {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gjc-resident-diag-"));
	tmpRoots.push(dir);
	return new EphemeralBlobStore(path.join(dir, "resident"));
}

const MISSING_REF = `blob:sha256:${"0".repeat(64)}`;
const MISSING_HASH = "0".repeat(64);

afterEach(() => {
	vi.restoreAllMocks();
	for (const dir of tmpRoots.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

describe("ResidentBlobMissingError carries its session binding", () => {
	test("names the session and transcript it was constructed with", () => {
		const error = new ResidentBlobMissingError(
			MISSING_HASH,
			"text",
			"session-abc",
			"/tmp/sessions/session-abc.jsonl",
		);

		expect(error.message).toContain(`Missing resident text blob: ${MISSING_HASH}`);
		expect(error.message).toContain("session-abc");
		expect(error.message).toContain("/tmp/sessions/session-abc.jsonl");
		// Fields stay addressable for structured consumers.
		expect(error.sessionId).toBe("session-abc");
		expect(error.sessionFile).toBe("/tmp/sessions/session-abc.jsonl");
	});

	test("stays readable when the binding is unavailable", () => {
		const error = new ResidentBlobMissingError(MISSING_HASH, "imageData");

		expect(error.message).toBe(`Missing resident imageData blob: ${MISSING_HASH}`);
		expect(error.sessionId).toBeUndefined();
		expect(error.sessionFile).toBeUndefined();
	});

	test("a resolver throw inherits the binding the caller supplied", () => {
		const store = makeStore();

		try {
			resolveTextBlobSync(store, MISSING_REF, { sessionId: "s-1", sessionFile: "/tmp/s-1.jsonl" });
			throw new Error("expected ResidentBlobMissingError");
		} catch (error) {
			expect(error).toBeInstanceOf(ResidentBlobMissingError);
			expect((error as ResidentBlobMissingError).message).toContain("s-1");
			expect((error as ResidentBlobMissingError).message).toContain("/tmp/s-1.jsonl");
		}
	});
});

describe("the fail-closed resident path leaves a record", () => {
	test("a placeholder substitution stays silent on the fail-closed channel", () => {
		const store = makeStore();
		const errorSpy = vi.spyOn(logger, "error").mockImplementation(() => {});
		const entry = {
			type: "message",
			id: "placeholder-entry",
			parentId: null,
			timestamp: new Date(0).toISOString(),
			message: {
				role: "user",
				content: [{ type: "text", text: residentBlobSentinelForTests("text", MISSING_REF) }],
				timestamp: 0,
			},
		};

		const [materialized] = materializeResidentEntriesForPersistenceForTests([entry], store);

		// The placeholder is self-evidencing in the transcript and #4670 already
		// reports it through `onResidentBlobMissing`; it must not also be logged
		// as a fail-closed abort.
		expect(JSON.stringify(materialized)).toContain("Session resident text blob missing");
		expect(errorSpy).not.toHaveBeenCalled();
	});

	test("a fail-closed materialize throw leaves one record carrying the session binding", () => {
		const store = makeStore();
		const errorSpy = vi.spyOn(logger, "error").mockImplementation(() => {});
		const entry = {
			type: "message",
			id: "materialize-entry",
			parentId: null,
			timestamp: new Date(0).toISOString(),
			message: {
				role: "user",
				content: [{ type: "text", text: residentBlobSentinelForTests("text", MISSING_REF) }],
				timestamp: 0,
			},
		} as unknown as FileEntry;

		expect(() =>
			materializeResidentEntriesThrowingForTests([entry], store, store, {
				sessionId: "s-materialize",
				sessionFile: "/tmp/s-materialize.jsonl",
			}),
		).toThrow("Missing resident text blob");
		const reported = errorSpy.mock.calls.filter(
			([message]) => message === "Resident blob missing on a fail-closed path",
		);
		expect(reported).toHaveLength(1);
		expect(reported[0]![1]).toMatchObject({
			phase: "materialize",
			kind: "text",
			hash: MISSING_HASH,
			sessionId: "s-materialize",
			sessionFile: "/tmp/s-materialize.jsonl",
		});
	});
});

describe("a corrupted resident reference on a fail-closed path", () => {
	function stageEntryWithRef(ref: string): FileEntry {
		return {
			type: "message",
			id: "corrupt-entry",
			parentId: null,
			timestamp: new Date(0).toISOString(),
			message: {
				role: "user",
				content: [{ type: "text", text: residentBlobSentinelForTests("text", ref) }],
				timestamp: 0,
			},
		} as unknown as FileEntry;
	}

	test("fails closed naming the session and leaves a record", () => {
		const store = makeStore();
		const errorSpy = vi.spyOn(logger, "error").mockImplementation(() => {});

		let thrown: unknown;
		try {
			assertResidentReferencesResolvableForTests([stageEntryWithRef("blob:not-a-sha-ref")], store, store, {
				sessionId: "s-corrupt",
				sessionFile: "/tmp/s-corrupt.jsonl",
			});
		} catch (error) {
			thrown = error;
		}

		expect(thrown).toBeInstanceOf(Error);
		expect((thrown as Error).message).toContain("invalid blob reference");
		expect((thrown as Error).message).toContain("s-corrupt");
		expect((thrown as Error).message).toContain("/tmp/s-corrupt.jsonl");
		const reported = errorSpy.mock.calls.filter(
			([message]) => message === "Resident blob reference invalid on a fail-closed path",
		);
		expect(reported).toHaveLength(1);
		expect(reported[0]![1]).toMatchObject({
			phase: "stage-verify",
			kind: "text",
			sessionId: "s-corrupt",
			sessionFile: "/tmp/s-corrupt.jsonl",
		});
	});

	test("bounds an unbounded corrupted ref in the record", () => {
		const store = makeStore();
		const errorSpy = vi.spyOn(logger, "error").mockImplementation(() => {});
		const hugeRef = `blob:${"x".repeat(4096)}`;

		expect(() => assertResidentReferencesResolvableForTests([stageEntryWithRef(hugeRef)], store)).toThrow(
			"invalid blob reference",
		);
		const reported = errorSpy.mock.calls.filter(
			([message]) => message === "Resident blob reference invalid on a fail-closed path",
		);
		expect(reported).toHaveLength(1);
		expect((reported[0]![1] as { ref: string }).ref.length).toBeLessThanOrEqual(97);
	});
});
