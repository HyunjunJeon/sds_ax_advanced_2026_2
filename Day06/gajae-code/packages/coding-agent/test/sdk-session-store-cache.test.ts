import { describe, expect, test } from "bun:test";
import { sdkOnlyStoreMatches } from "../src/sdk/session";

describe("SDK-only reconciliation store cache keying", () => {
	test("matches only when BOTH the session id and the session file are unchanged", () => {
		const cached = { sessionId: "copied-id", sessionFile: "/a/predecessor.json" };
		// Same id, same file: reuse the store.
		expect(sdkOnlyStoreMatches(cached, "copied-id", "/a/predecessor.json")).toBe(true);
		// Same copied id, DIFFERENT file (session_switch/session_branch to a new
		// transcript): must recreate the store, or the successor reads/writes the
		// predecessor's reconciliation file and spuriously replays/conflicts with
		// its keys (review thread P2).
		expect(sdkOnlyStoreMatches(cached, "copied-id", "/b/successor.json")).toBe(false);
		// Different id: recreate.
		expect(sdkOnlyStoreMatches(cached, "other-id", "/a/predecessor.json")).toBe(false);
		// No cached store: recreate.
		expect(sdkOnlyStoreMatches(undefined, "copied-id", "/a/predecessor.json")).toBe(false);
	});

	test("treats a null-to-file session-file transition as a cache miss", () => {
		// A session that had no session file and then acquires one must not reuse
		// the file-less store (review thread P2).
		const cached = { sessionId: "s", sessionFile: undefined };
		expect(sdkOnlyStoreMatches(cached, "s", undefined)).toBe(true);
		expect(sdkOnlyStoreMatches(cached, "s", "/data/s.jsonl")).toBe(false);
	});
});
