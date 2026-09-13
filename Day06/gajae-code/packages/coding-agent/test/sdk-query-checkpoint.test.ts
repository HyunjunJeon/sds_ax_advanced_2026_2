import { describe, expect, it } from "bun:test";
import { isCheckpointRecord, type SdkCheckpointRecord, TARGET_PAGE_BYTES } from "../src/sdk/host/query/handlers.js";
import {
	CURSOR_TTL_MS,
	CursorRegistry,
	QueryHandlers,
	RevisionStore,
	verifyCursor,
} from "../src/sdk/host/query/index.js";

interface CheckpointResult {
	checkpointToken: string;
	checkpoint: SdkCheckpointRecord;
	revisionId: string;
	issuedAt: number;
	expiresAt: number;
}

const entries = (count: number): unknown[] =>
	Array.from({ length: count }, (_, index) => ({ id: `e${index}`, role: "user", body: `entry-${index}` }));

const largeEntries = (count: number): unknown[] =>
	Array.from({ length: count }, (_, index) => ({
		id: `e${index}`,
		role: "user",
		body: `entry-${index}-${"x".repeat(20_000)}`,
	}));

function surface(
	transcript: unknown[],
	watermark: SdkCheckpointRecord = { revision: transcript.length, generation: 0, seq: 0, idle: true },
) {
	return {
		getTranscriptEntries: () => transcript,
		getContextSnapshot: () => ({}),
		getGoalState: () => [],
		getTodoState: () => [],
		getDiff: () => [],
		getUsage: () => ({}),
		getModels: () => [],
		getSkillState: () => [],
		getActiveProviders: () => [],
		getGates: () => [],
		getConfigItems: () => [],
		getSessionMetadata: () => ({}),
		getStats: () => ({}),
		getBranchCandidates: () => [],
		getLastAssistant: () => ({}),
		getCapabilities: () => ({}),
		getAuthProviders: () => [],
		getTools: () => [],
		getQueueMessages: () => [],
		getExtensions: () => [],
		getJobs: () => [],
		// Q30 atomic capture: entries + event-ring watermark + idle in one
		// synchronous call.
		getCheckpointSnapshot: () => ({ entries: transcript, watermark }),
	};
}

function harness(transcript: unknown[], watermark?: SdkCheckpointRecord, now?: () => number) {
	const store = new RevisionStore("s1", now);
	const cursors = new CursorRegistry("token", store, now);
	return {
		store,
		cursors,
		handlers: new QueryHandlers(surface(transcript, watermark), "s1", store, cursors),
	};
}

function pageOf(response: unknown): { items: unknown[]; complete: boolean; cursor?: string } {
	const page = (response as { page?: unknown } | undefined)?.page;
	if (!page || typeof page !== "object") return { items: [], complete: true };
	const record = page as { items?: unknown; complete?: unknown; continuationCursor?: unknown };
	return {
		items: Array.isArray(record.items) ? record.items : [],
		complete: record.complete === true,
		cursor: typeof record.continuationCursor === "string" ? record.continuationCursor : undefined,
	};
}

describe("SDK session.checkpoint (Q30) replay authority", () => {
	it("mints a signed, pinned checkpoint cursor with TTL/issued/expires metadata", async () => {
		const { handlers } = harness(entries(4));
		const response = await handlers.dispatch({ query: "session.checkpoint", id: "q30", connectionId: "c" });
		expect(response.ok).toBe(true);
		const result = response.result as CheckpointResult;
		// Head degrades to the live transcript count when the host publishes no
		// atomic checkpoint snapshot.
		expect(result.checkpoint).toEqual({ revision: 4, generation: 0, seq: 0, idle: true });
		expect(result.revisionId).toBe("1");
		expect(result.issuedAt).toEqual(expect.any(Number));
		expect(result.expiresAt - result.issuedAt).toBe(CURSOR_TTL_MS);

		// The checkpointToken IS the signed cursor: verifiable with the session
		// token, pinned to the exact transcript:default revision, carrying the
		// watermark and TTL metadata inside the signed envelope.
		const envelope = verifyCursor(result.checkpointToken, "token");
		expect(envelope).toBeDefined();
		expect(envelope).toMatchObject({
			sessionId: "s1",
			resource: "transcript",
			revision: "1",
			direction: "forward",
			position: { offset: 0, selector: { queryId: "Q01" } },
			highWatermark: { revision: 4, generation: 0, seq: 0, idle: true },
		});
		expect(envelope?.issuedAt).toBe(result.issuedAt);
		expect(envelope?.expiresAt).toBe(result.expiresAt);
	});

	it("replays exactly the pinned snapshot revision (append-during-checkpoint excluded)", async () => {
		const transcript = entries(4);
		const { handlers } = harness(transcript);
		const checkpoint = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;

		// The live transcript mutates after the grant; replay must read the
		// pinned snapshot, never a fresh unlocked revision.
		transcript.push({ id: "e4", role: "user", body: "entry-4" });
		const replay = await handlers.dispatch({
			query: "transcript.list",
			cursor: checkpoint.checkpointToken,
			connectionId: "c",
		});
		expect(replay).toMatchObject({ ok: true, page: { items: entries(4), complete: true, revision: "1" } });
	});

	it("consumes a saved checkpointToken directly on Q01 (resume) without re-minting", async () => {
		const transcript = entries(3);
		const { handlers } = harness(transcript);
		const checkpoint = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		transcript.push({ id: "e3", role: "user", body: "entry-3" });
		const resume = await handlers.dispatch({
			query: "transcript.list",
			input: { checkpointToken: checkpoint.checkpointToken },
			connectionId: "c",
		});
		expect(resume).toMatchObject({ ok: true, page: { items: entries(3), complete: true } });
	});

	it("does not alias duplicate grants for the same checkpoint", async () => {
		const { handlers } = harness(entries(3));
		const first = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		const second = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		expect(first.checkpointToken).not.toBe(second.checkpointToken);

		const firstReplay = await handlers.dispatch({
			query: "transcript.list",
			cursor: first.checkpointToken,
			connectionId: "c",
		});
		const secondReplay = await handlers.dispatch({
			query: "transcript.list",
			cursor: second.checkpointToken,
			connectionId: "c",
		});
		expect(firstReplay).toMatchObject({ ok: true, page: { complete: true } });
		expect(secondReplay).toMatchObject({ ok: true, page: { complete: true } });
	});

	it("keeps the checkpoint connection-owned: a new connection must reissue, never echo", async () => {
		const { handlers, cursors, store } = harness(entries(2));
		const first = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c1" }))
			.result as CheckpointResult;
		expect(cursors.size).toBe(1);
		expect(store.pinnedCount).toBe(1);
		// Q30 transfers the signed source claim into a fresh connection-owned grant.
		const second = (
			await handlers.dispatch({
				query: "session.checkpoint",
				input: { checkpointToken: first.checkpointToken },
				connectionId: "c2",
			})
		).result as CheckpointResult;
		expect(second.checkpointToken).not.toBe(first.checkpointToken);
		expect(second.revisionId).toBe(first.revisionId);
		expect(cursors.size).toBe(1);
		expect(store.pinnedCount).toBe(1);
		expect(
			await handlers.dispatch({
				query: "transcript.list",
				cursor: first.checkpointToken,
				connectionId: "c1",
			}),
		).toMatchObject({ ok: false, error: { code: "cursor_expired", restartQuery: true } });
		expect(
			await handlers.dispatch({
				query: "transcript.list",
				cursor: second.checkpointToken,
				connectionId: "c2",
			}),
		).toMatchObject({ ok: true, page: { complete: true } });
		expect(cursors.size).toBe(0);
		expect(store.pinnedCount).toBe(0);
	});

	it("keeps cursor and revision-pin counts bounded across repeated exchanges", async () => {
		const { handlers, cursors, store } = harness(entries(2));
		let token = (
			(await handlers.dispatch({ query: "session.checkpoint", connectionId: "c0" })).result as CheckpointResult
		).checkpointToken;
		for (let index = 1; index <= 64; index++) {
			const response = await handlers.dispatch({
				query: "session.checkpoint",
				input: { checkpointToken: token },
				connectionId: `c${index}`,
			});
			expect(response.ok).toBe(true);
			token = (response.result as CheckpointResult).checkpointToken;
			expect(cursors.size).toBe(1);
			expect(store.pinnedCount).toBe(1);
		}
	});

	it("sweeps expired target grants before exchange capacity accounting", async () => {
		let now = 1_000;
		const { handlers, cursors, store } = harness(entries(2), undefined, () => now);
		for (let index = 0; index < 32; index++) {
			expect(await handlers.dispatch({ query: "session.checkpoint", connectionId: "target" })).toMatchObject({
				ok: true,
			});
		}
		now += CURSOR_TTL_MS - 1;
		const source = (
			(await handlers.dispatch({ query: "session.checkpoint", connectionId: "source" })).result as CheckpointResult
		).checkpointToken;
		expect(cursors.size).toBe(33);
		expect(store.pinnedCount).toBe(33);
		now += 2;
		const exchanged = await handlers.dispatch({
			query: "session.checkpoint",
			input: { checkpointToken: source },
			connectionId: "target",
		});
		expect(exchanged.ok).toBe(true);
		expect(cursors.size).toBe(1);
		expect(store.pinnedCount).toBe(1);
		expect(
			await handlers.dispatch({
				query: "transcript.list",
				cursor: (exchanged.result as CheckpointResult).checkpointToken,
				connectionId: "target",
			}),
		).toMatchObject({ ok: true, page: { complete: true } });
	});

	it("rolls back the source cursor and pin when replacement grant fails", async () => {
		const { handlers, cursors, store } = harness(entries(2));
		const source = (
			(await handlers.dispatch({ query: "session.checkpoint", connectionId: "c1" })).result as CheckpointResult
		).checkpointToken;
		let failReplacement = true;
		const originalTransferPin = store.transferPin.bind(store);
		store.transferPin = (sourceCursorId, replacementCursorId) => {
			if (failReplacement) {
				failReplacement = false;
				throw new Error("replacement pin failed");
			}
			originalTransferPin(sourceCursorId, replacementCursorId);
		};
		expect(
			await handlers.dispatch({
				query: "session.checkpoint",
				input: { checkpointToken: source },
				connectionId: "c2",
			}),
		).toMatchObject({ ok: false, error: { code: "internal", message: "replacement pin failed" } });
		expect(cursors.size).toBe(1);
		expect(store.pinnedCount).toBe(1);
		expect(await handlers.dispatch({ query: "transcript.list", cursor: source, connectionId: "c1" })).toMatchObject({
			ok: true,
			page: { complete: true },
		});
		expect(cursors.size).toBe(0);
		expect(store.pinnedCount).toBe(0);
	});

	it("releases an exchanged cursor whose checkpoint watermark is invalid", async () => {
		const { handlers, cursors, store } = harness(entries(2));
		const revision = await store.createRevision("transcript", "default", entries(2));
		const source = await cursors.grant(
			"c1",
			{
				cursorVersion: 1,
				protocolMajor: 3,
				sessionId: "s1",
				resource: "transcript",
				revision,
				highWatermark: { revision: 2, generation: 0, seq: 0 },
				purpose: "checkpoint",
				position: { offset: 0, selector: { queryId: "Q01" } },
				direction: "forward",
				pageShape: { targetBytes: TARGET_PAGE_BYTES },
			},
			"transcript",
			"default",
		);
		expect(
			await handlers.dispatch({
				query: "session.checkpoint",
				input: { checkpointToken: source },
				connectionId: "c2",
			}),
		).toMatchObject({ ok: false, error: { code: "invalid_input" } });
		expect(cursors.size).toBe(0);
		expect(store.pinnedCount).toBe(0);
	});

	it("rejects a tampered signed cursor (the pinned position cannot be rewound)", async () => {
		const { handlers } = harness(entries(4));
		const checkpoint = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		// Rewriting the pinned offset inside the signed envelope invalidates the
		// MAC, so a client can never replay before the checkpoint position.
		const forged = checkpoint.checkpointToken.replace('"offset":0', '"offset":9');
		expect(forged).not.toBe(checkpoint.checkpointToken);
		expect(verifyCursor(forged, "token")).toBeUndefined();
		const replay = await handlers.dispatch({
			query: "transcript.list",
			cursor: forged,
			connectionId: "c",
		});
		expect(replay).toMatchObject({ ok: false, error: { code: "invalid_cursor" } });
	});

	it("cleans up the exact active cursor and pin when its signing authority becomes invalid", async () => {
		const key = new Uint8Array([1, 2, 3, 4]);
		const store = new RevisionStore("s1");
		const cursors = new CursorRegistry(key, store);
		const handlers = new QueryHandlers(surface(entries(2)), "s1", store, cursors);
		const source = (
			(await handlers.dispatch({ query: "session.checkpoint", connectionId: "c1" })).result as CheckpointResult
		).checkpointToken;
		expect(cursors.size).toBe(1);
		expect(store.pinnedCount).toBe(1);
		key.fill(9);
		expect(
			await handlers.dispatch({
				query: "session.checkpoint",
				input: { checkpointToken: source },
				connectionId: "c2",
			}),
		).toMatchObject({ ok: false, error: { code: "invalid_cursor", restartQuery: true } });
		expect(cursors.size).toBe(0);
		expect(store.pinnedCount).toBe(0);
	});

	it("rejects empty, whitespace, and non-string checkpointToken inputs", async () => {
		const { handlers } = harness(entries(2));
		for (const bad of ["", "   ", 42, null]) {
			const response = await handlers.dispatch({
				query: "transcript.list",
				input: { checkpointToken: bad },
				connectionId: "c",
			});
			expect(response, `token=${String(bad)}`).toMatchObject({ ok: false, error: { code: "invalid_input" } });
		}
	});

	it("rejects checkpointToken on every query except transcript.list and session.checkpoint", async () => {
		const { handlers } = harness(entries(2));
		for (const query of ["transcript.body", "resource.body", "context.get", "todo.list"]) {
			const response = await handlers.dispatch({
				query,
				input: { checkpointToken: "signed-cursor" },
				connectionId: "c",
			});
			expect(response, query).toMatchObject({ ok: false, error: { code: "invalid_input" } });
		}
	});

	it("enforces checkpointToken and cursor mutual exclusion on Q01", async () => {
		const { handlers } = harness(entries(2));
		const both = await handlers.dispatch({
			query: "transcript.list",
			input: { checkpointToken: "signed-cursor" },
			cursor: "continuation",
			connectionId: "c",
		});
		expect(both).toMatchObject({ ok: false, error: { code: "invalid_input" } });
	});

	it("rejects an ordinary continuation cursor when supplied as Q01 checkpointToken", async () => {
		const { handlers } = harness(largeEntries(20));
		const first = pageOf(await handlers.dispatch({ query: "transcript.list", connectionId: "c" }));
		expect(first.complete).toBe(false);
		expect(first.cursor).toBeDefined();
		const rejected = await handlers.dispatch({
			query: "transcript.list",
			input: { checkpointToken: first.cursor },
			connectionId: "c",
		});
		expect(rejected).toMatchObject({ ok: false, error: { code: "invalid_input" } });
	});

	it("rejects empty top-level cursors instead of silently dropping them", async () => {
		const { handlers } = harness(entries(2));
		const response = await handlers.dispatch({ query: "transcript.list", cursor: "", connectionId: "c" });
		expect(response).toMatchObject({ ok: false, error: { code: "invalid_input" } });
	});

	it("rejects any input or cursor on Q30 itself (request shape per contract)", async () => {
		const { handlers } = harness(entries(2));
		const withInput = await handlers.dispatch({
			query: "session.checkpoint",
			input: { foo: 1 },
			connectionId: "c",
		});
		expect(withInput).toMatchObject({ ok: false, error: { code: "invalid_request" } });
		const withCursor = await handlers.dispatch({
			query: "session.checkpoint",
			cursor: "not-a-cursor",
			connectionId: "c",
		});
		expect(withCursor).toMatchObject({ ok: false, error: { code: "invalid_request" } });
	});

	it("captures the exact event-ring watermark atomically with the snapshot", async () => {
		const watermark: SdkCheckpointRecord = { revision: 4, generation: 3, seq: 42, idle: false };
		const { handlers } = harness(entries(4), watermark);
		const checkpoint = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		expect(checkpoint.checkpoint).toEqual(watermark);
		const envelope = verifyCursor(checkpoint.checkpointToken, "token");
		expect(envelope?.highWatermark).toEqual(watermark);
	});

	it("fails explicitly when the host has no atomic checkpoint provider", async () => {
		const store = new RevisionStore("s1");
		const { getCheckpointSnapshot: _omitted, ...withoutAtomicCheckpoint } = surface(entries(2));
		const handlers = new QueryHandlers(withoutAtomicCheckpoint, "s1", store, new CursorRegistry("token", store));
		expect(await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" })).toMatchObject({
			ok: false,
			error: { code: "unavailable" },
		});
	});

	it("honors the cursor TTL for checkpoint tokens and reissues after expiry", async () => {
		let now = 1_000;
		const store = new RevisionStore("s1", () => now);
		const cursors = new CursorRegistry("token", store, () => now);
		const handlers = new QueryHandlers(surface(entries(2)), "s1", store, cursors);
		const checkpoint = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		expect(checkpoint.issuedAt).toBe(1_000);
		expect(checkpoint.expiresAt).toBe(1_000 + CURSOR_TTL_MS);
		now += CURSOR_TTL_MS + 1;
		const replay = await handlers.dispatch({
			query: "transcript.list",
			cursor: checkpoint.checkpointToken,
			connectionId: "c",
		});
		expect(replay).toMatchObject({ ok: false, error: { code: "cursor_expired", restartQuery: true } });
		const reissued = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		expect(reissued.checkpointToken).not.toBe(checkpoint.checkpointToken);
		expect(
			await handlers.dispatch({
				query: "transcript.list",
				cursor: reissued.checkpointToken,
				connectionId: "c",
			}),
		).toMatchObject({ ok: true, page: { complete: true } });
	});

	it("keeps the checkpoint revision pinned through append churn (eviction resistance)", async () => {
		const transcript = entries(1);
		const { handlers } = harness(transcript);
		const checkpoint = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		// Nine further revisions on transcript:default would evict an unpinned
		// first revision (MAX_REVISIONS_PER_RESOURCE=8), but the pinned
		// checkpoint revision must survive and stay replayable.
		for (let index = 0; index < 9; index++) {
			transcript.push({ id: `churn-${index}`, role: "user", body: `churn-${index}` });
			await handlers.dispatch({ query: "transcript.list", connectionId: "c" });
		}
		const replay = await handlers.dispatch({
			query: "transcript.list",
			cursor: checkpoint.checkpointToken,
			connectionId: "c",
		});
		expect(replay).toMatchObject({ ok: true, page: { items: entries(1), complete: true } });
	});

	it("paginates the pinned snapshot with continuation cursors", async () => {
		const { handlers } = harness(largeEntries(20));
		const checkpoint = (await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" }))
			.result as CheckpointResult;
		const first = pageOf(
			await handlers.dispatch({
				query: "transcript.list",
				cursor: checkpoint.checkpointToken,
				connectionId: "c",
			}),
		);
		expect(first.complete).toBe(false);
		expect(first.cursor).toBeDefined();
		const second = pageOf(
			await handlers.dispatch({
				query: "transcript.list",
				cursor: first.cursor,
				connectionId: "c",
			}),
		);
		expect(second.complete).toBe(true);
		expect([...first.items, ...second.items]).toHaveLength(20);
		expect(second.items[0]).toMatchObject({ id: `e${first.items.length}` });
	});

	it("honors the per-connection cursor budget", async () => {
		const { handlers, cursors, store } = harness(entries(1));
		const tokens: string[] = [];
		for (let index = 0; index < 32; index++) {
			const response = await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" });
			expect(response.ok, `grant ${index + 1}`).toBe(true);
			tokens.push((response.result as CheckpointResult).checkpointToken);
		}
		const exceeded = await handlers.dispatch({ query: "session.checkpoint", connectionId: "c" });
		expect(exceeded).toMatchObject({ ok: false, error: { code: "snapshot_capacity_exceeded" } });
		const exchanged = await handlers.dispatch({
			query: "session.checkpoint",
			input: { checkpointToken: tokens[0] },
			connectionId: "c",
		});
		expect(exchanged.ok).toBe(true);
		expect(cursors.size).toBe(32);
		expect(store.pinnedCount).toBe(32);
	});

	it("enforces installed-query authority for session.checkpoint", async () => {
		const store = new RevisionStore("s1");
		const query = new QueryHandlers(
			{ ...surface(entries(2)), installedQueries: new Set(["transcript.list"]) },
			"s1",
			store,
			new CursorRegistry("token", store),
		);
		const rejected = await query.dispatch({ query: "session.checkpoint", connectionId: "c" });
		expect(rejected).toMatchObject({
			ok: false,
			error: { code: "operation_not_session_owned" },
		});
		const advertised = new QueryHandlers(
			{ ...surface(entries(2)), installedQueries: new Set(["transcript.list", "session.checkpoint"]) },
			"s1",
			store,
			new CursorRegistry("token", store),
		);
		expect(await advertised.dispatch({ query: "session.checkpoint", connectionId: "c" })).toMatchObject({
			ok: true,
		});
	});

	it("validates checkpoint records strictly", () => {
		expect(isCheckpointRecord({ revision: 4, generation: 2, seq: 40, idle: true })).toBe(true);
		expect(isCheckpointRecord({ revision: -1, generation: 0, seq: 0 })).toBe(false);
		expect(isCheckpointRecord({ revision: 1, generation: 0 })).toBe(false);
		expect(isCheckpointRecord({ revision: 1, generation: 0, seq: 0, idle: true, extra: 1 })).toBe(false);
		expect(isCheckpointRecord({ revision: 1.5, generation: 0, seq: 0 })).toBe(false);
		expect(isCheckpointRecord({ revision: 1, generation: 0, seq: 0 })).toBe(false);
	});
});
