import { describe, expect, test, vi } from "bun:test";
import * as fsSync from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { logger } from "@gajae-code/utils";

import { daemonPaths } from "../src/sdk/bus/daemon-paths";
import {
	ADOPTION_INTENT_FILENAME_SUFFIX,
	type AdoptionIntentFileHandle,
	type AdoptionIntentFs,
	adoptionIntentFilePath,
	buildAdoptionIntent,
	DEFAULT_ADOPTION_INTENT_TTL_MS,
	pendingTopicFilePath,
	type TelegramAdoptionIntent,
	TelegramAdoptionIntentStore,
	type TelegramAdoptionTarget,
} from "../src/sdk/bus/telegram-adoption-intent";

const AGENT = "/virtual/agent";
const TARGET: TelegramAdoptionTarget = { kind: "existing_path", path: "/repo" };

function reservation(overrides: Partial<TelegramAdoptionIntent> = {}): TelegramAdoptionIntent {
	return {
		...buildAdoptionIntent({
			providerRequestKey: "telegram:42:17",
			topicId: 17,
			chatId: "42",
			target: TARGET,
			now: 1_000,
		}),
		...overrides,
	};
}

class FakeFs implements AdoptionIntentFs {
	readonly files = new Map<string, string>();
	readonly modes = new Map<string, number>();
	readonly dirs = new Set<string>();
	readonly syncedFiles = new Set<string>();
	readonly syncedDirs = new Set<string>();
	writeError: Error | undefined;
	unlinkError: Error | undefined;
	unlinkErrorFile: string | undefined;
	chmodError: Error | undefined;
	chmodErrorFile: string | undefined;
	syncError: Error | undefined;
	syncErrorFile: string | undefined;
	async mkdir(directory: string, options: { recursive: true; mode: number }): Promise<void> {
		this.dirs.add(directory);
		this.modes.set(directory, options.mode);
	}
	async chmod(target: string, mode: number): Promise<void> {
		if (this.chmodError && this.chmodErrorFile === target) throw this.chmodError;
		this.modes.set(target, mode);
	}
	async readFile(file: string, _encoding: "utf8"): Promise<string> {
		const value = this.files.get(file);
		if (value === undefined) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
		return value;
	}
	async writeFile(file: string, data: string, options: { mode: number }): Promise<void> {
		if (this.writeError) throw this.writeError;
		this.files.set(file, data);
		this.modes.set(file, options.mode);
	}
	async rename(from: string, to: string): Promise<void> {
		const value = this.files.get(from);
		if (value === undefined) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
		const mode = this.modes.get(from);
		this.files.delete(from);
		this.modes.delete(from);
		this.files.set(to, value);
		if (mode !== undefined) this.modes.set(to, mode);
	}
	async unlink(file: string): Promise<void> {
		if (this.unlinkError && this.unlinkErrorFile === file) throw this.unlinkError;
		if (!this.files.delete(file)) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
	}
	async readdir(directory: string): Promise<readonly string[]> {
		if (!this.dirs.has(directory)) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
		const prefix = `${directory}${path.sep}`;
		return [...this.files.keys()]
			.filter(file => file.startsWith(prefix) && !file.slice(prefix.length).includes(path.sep))
			.map(file => file.slice(prefix.length));
	}
	async open(file: string): Promise<AdoptionIntentFileHandle> {
		if (!this.files.has(file) && !this.dirs.has(file)) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
		return {
			sync: async () => {
				if (this.syncError && this.syncErrorFile === file) throw this.syncError;
				if (this.dirs.has(file)) this.syncedDirs.add(file);
				else this.syncedFiles.add(file);
			},
			close: async () => undefined,
		};
	}
}

function store(fake: FakeFs, now = 1_000): TelegramAdoptionIntentStore {
	return new TelegramAdoptionIntentStore({ agentDir: AGENT, fs: fake, now: () => now });
}

function seedLegacyIntent(fake: FakeFs): string {
	const file = path.join(daemonPaths(AGENT).dir, "legacy-intent.adoption-intent.json");
	fake.dirs.add(daemonPaths(AGENT).dir);
	fake.files.set(
		file,
		JSON.stringify({
			version: 1,
			intent: {
				intendedSessionId: "legacy-session",
				topicId: 17,
				chatId: "42",
				target: TARGET,
				createdAt: 1_000,
				expiresAt: 2_000,
			},
		}),
	);
	return file;
}

function seedLegacyPendingTopic(fake: FakeFs, filename = "17.pending-topic.json"): string {
	const file = path.join(daemonPaths(AGENT).dir, filename);
	fake.dirs.add(daemonPaths(AGENT).dir);
	fake.files.set(
		file,
		JSON.stringify({
			version: 1,
			pendingTopic: { topicId: 17, chatId: "42", createdAt: 1_000, expiresAt: 2_000 },
		}),
	);
	return file;
}

describe("Telegram provider-local adoption reservations", () => {
	test("persists providerRequestKey before create and exposes no SessionId mapping", async () => {
		const fake = new FakeFs();
		const intents = store(fake);
		await intents.put(reservation());
		const raw = fake.files.get(adoptionIntentFilePath(AGENT, "telegram:42:17"))!;
		const parsed = JSON.parse(raw) as { intent: Record<string, unknown> };
		expect(parsed.intent.providerRequestKey).toBe("telegram:42:17");
		expect(parsed.intent.sessionId).toBeUndefined();
		expect(intents.bySession("broker-session-1")).toBeUndefined();
		expect(fake.modes.get(daemonPaths(AGENT).dir)).toBe(0o700);
		expect(fake.modes.get(adoptionIntentFilePath(AGENT, "telegram:42:17"))).toBe(0o600);
	});

	test("CAS-binds only the returned Broker SessionId", async () => {
		const fake = new FakeFs();
		const intents = store(fake);
		await intents.put(reservation());
		expect(await intents.bindSession("telegram:42:17", "broker-session-1")).toBe(true);
		expect(intents.bySession("broker-session-1")?.providerRequestKey).toBe("telegram:42:17");
		expect(intents.bySession("telegram:42:17")).toBeUndefined();
		expect(await intents.bindSession("telegram:42:17", "broker-session-1")).toBe(true);
		expect(await intents.bindSession("telegram:42:17", "different-session")).toBe(false);
	});

	test("concurrent topic claims fail closed across different provider requests", async () => {
		const fake = new FakeFs();
		const intents = store(fake);
		expect(intents.tryClaim(17, "telegram:42:17")).toBe(true);
		expect(intents.tryClaim(17, "telegram:42:18")).toBe(false);
		expect(intents.tryClaim(17, "telegram:42:17")).toBe(true);
		intents.releaseClaim(17, "telegram:42:17");
		expect(intents.tryClaim(17, "telegram:42:18")).toBe(true);
	});

	test("rehydrates provider reservations across Telegram restart", async () => {
		const fake = new FakeFs();
		const writer = store(fake);
		await writer.put(reservation({ providerRequestKey: "telegram:42:19", topicId: 19 }));
		await writer.bindSession("telegram:42:19", "broker-session-19");
		const reader = store(fake);
		expect(reader.byProviderRequestKey("telegram:42:19")).toBeUndefined();
		expect(await reader.rehydrate()).toBe(1);
		expect(reader.bySession("broker-session-19")?.topicId).toBe(19);
	});

	test("migrates a v1 intent sidecar to the provider-keyed v2 record", async () => {
		const fake = new FakeFs();
		const legacyFile = seedLegacyIntent(fake);
		const intents = store(fake);
		const destination = adoptionIntentFilePath(AGENT, "legacy:v1:legacy-session");

		expect(await intents.rehydrate()).toBe(1);
		expect(intents.byProviderRequestKey("legacy:v1:legacy-session")?.topicId).toBe(17);
		expect(fake.files.has(legacyFile)).toBe(false);
		expect(JSON.parse(fake.files.get(destination) ?? "{}")).toMatchObject({
			version: 2,
			intent: { providerRequestKey: "legacy:v1:legacy-session" },
		});
	});

	test("logs a bounded diagnostic and retains a v1 intent sidecar when migration write fails", async () => {
		const fake = new FakeFs();
		const legacyFile = seedLegacyIntent(fake);
		const intents = store(fake);
		fake.writeError = new Error(`migration write failed: ${"x".repeat(300)}`);
		const warning = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			expect(await intents.rehydrate()).toBe(1);
			expect(intents.byProviderRequestKey("legacy:v1:legacy-session")?.topicId).toBe(17);
			expect(fake.files.get(legacyFile)).toContain('"version":1');
			expect(fake.files.has(adoptionIntentFilePath(AGENT, "legacy:v1:legacy-session"))).toBe(false);
			expect(warning).toHaveBeenLastCalledWith(
				"notifications: Telegram adoption sidecar migration failed; retaining legacy sidecar",
				expect.objectContaining({ sidecar: "intent", stage: "write" }),
			);
			const error = warning.mock.calls[0]?.[1]?.error;
			expect(typeof error).toBe("string");
			expect((error as string).length).toBeLessThanOrEqual(256);
		} finally {
			warning.mockRestore();
		}
	});

	test("logs and retains a v1 intent sidecar when migration source unlink fails", async () => {
		const fake = new FakeFs();
		const legacyFile = seedLegacyIntent(fake);
		const intents = store(fake);
		fake.unlinkErrorFile = legacyFile;
		fake.unlinkError = new Error("migration unlink failed");
		const warning = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			expect(await intents.rehydrate()).toBe(1);
			expect(fake.files.get(legacyFile)).toContain('"version":1');
			expect(
				JSON.parse(fake.files.get(adoptionIntentFilePath(AGENT, "legacy:v1:legacy-session")) ?? "{}"),
			).toMatchObject({ version: 2 });
			expect(warning).toHaveBeenLastCalledWith(
				"notifications: Telegram adoption sidecar migration failed; retaining legacy sidecar",
				expect.objectContaining({ sidecar: "intent", stage: "unlink", error: "migration unlink failed" }),
			);
		} finally {
			warning.mockRestore();
		}
	});

	test("migrates a v1 pending-topic sidecar to the canonical v2 record", async () => {
		const fake = new FakeFs();
		seedLegacyPendingTopic(fake);
		const intents = store(fake);
		const destination = pendingTopicFilePath(AGENT, 17);

		expect(await intents.rehydrate()).toBe(1);
		expect(intents.hasPendingTopic(17, "42")).toBe(true);
		expect(JSON.parse(fake.files.get(destination) ?? "{}")).toMatchObject({
			version: 2,
			pendingTopic: { topicId: 17 },
		});
	});

	test("retains the migrated v2 pending-topic sidecar when post-rename chmod fails", async () => {
		const fake = new FakeFs();
		const file = seedLegacyPendingTopic(fake);
		const intents = store(fake);
		fake.chmodErrorFile = file;
		fake.chmodError = new Error("pending migration post-rename chmod failed");
		const warning = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			expect(await intents.rehydrate()).toBe(1);
			expect(intents.hasPendingTopic(17, "42")).toBe(true);
			expect(fake.files.size).toBe(1);
			expect(JSON.parse(fake.files.get(file) ?? "{}")).toMatchObject({ version: 2 });
			expect(fake.modes.get(file)).toBe(0o600);
			expect(warning).toHaveBeenLastCalledWith(
				"notifications: Telegram adoption sidecar migration durability is uncertain; retained migrated v2 sidecar",
				expect.objectContaining({
					sidecar: "pending_topic",
					stage: "durability",
					error: "pending migration post-rename chmod failed",
				}),
			);
		} finally {
			warning.mockRestore();
		}
	});

	test("retains the migrated v2 pending-topic sidecar when parent-directory sync fails", async () => {
		const fake = new FakeFs();
		const file = seedLegacyPendingTopic(fake);
		const intents = store(fake);
		fake.syncErrorFile = daemonPaths(AGENT).dir;
		fake.syncError = new Error("pending migration parent-directory sync failed");
		const warning = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			expect(await intents.rehydrate()).toBe(1);
			expect(intents.hasPendingTopic(17, "42")).toBe(true);
			expect(fake.files.size).toBe(1);
			expect(JSON.parse(fake.files.get(file) ?? "{}")).toMatchObject({ version: 2 });
			expect(fake.modes.get(file)).toBe(0o600);
			expect(warning).toHaveBeenLastCalledWith(
				"notifications: Telegram adoption sidecar migration durability is uncertain; retained migrated v2 sidecar",
				expect.objectContaining({
					sidecar: "pending_topic",
					stage: "durability",
					error: "pending migration parent-directory sync failed",
				}),
			);
		} finally {
			warning.mockRestore();
		}
	});

	test("logs and retains a v1 pending-topic sidecar when migration write fails", async () => {
		const fake = new FakeFs();
		const legacyFile = seedLegacyPendingTopic(fake);
		const intents = store(fake);
		fake.writeError = new Error("pending migration write failed");
		const warning = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			expect(await intents.rehydrate()).toBe(1);
			expect(intents.hasPendingTopic(17, "42")).toBe(true);
			expect(fake.files.get(legacyFile)).toContain('"version":1');
			expect(JSON.parse(fake.files.get(pendingTopicFilePath(AGENT, 17)) ?? "{}")).toMatchObject({ version: 1 });
			expect(warning).toHaveBeenLastCalledWith(
				"notifications: Telegram adoption sidecar migration failed; retaining legacy sidecar",
				expect.objectContaining({
					sidecar: "pending_topic",
					stage: "write",
					error: "pending migration write failed",
				}),
			);
		} finally {
			warning.mockRestore();
		}
	});

	test("logs and retains a v1 pending-topic sidecar when migration source unlink fails", async () => {
		const fake = new FakeFs();
		const legacyFile = seedLegacyPendingTopic(fake, "017.pending-topic.json");
		const intents = store(fake);
		fake.unlinkErrorFile = legacyFile;
		fake.unlinkError = new Error("pending migration unlink failed");
		const warning = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			expect(await intents.rehydrate()).toBe(1);
			expect(fake.files.get(legacyFile)).toContain('"version":1');
			expect(JSON.parse(fake.files.get(pendingTopicFilePath(AGENT, 17)) ?? "{}")).toMatchObject({ version: 2 });
			expect(warning).toHaveBeenLastCalledWith(
				"notifications: Telegram adoption sidecar migration failed; retaining legacy sidecar",
				expect.objectContaining({
					sidecar: "pending_topic",
					stage: "unlink",
					error: "pending migration unlink failed",
				}),
			);
		} finally {
			warning.mockRestore();
		}
	});

	test("pending topic authorization remains separate from adoption reservation", async () => {
		const fake = new FakeFs();
		const intents = store(fake);
		await intents.putPendingTopic({ topicId: 77, chatId: "42", createdAt: 1_000, expiresAt: 2_000 });
		expect(intents.hasPendingTopic(77, "42")).toBe(true);
		expect(fake.files.has(pendingTopicFilePath(AGENT, 77))).toBe(true);
		expect(intents.bySession("77")).toBeUndefined();
	});

	test("definite cleanup removes the provider reservation without touching the user topic", async () => {
		const fake = new FakeFs();
		const intents = store(fake);
		await intents.put(reservation());
		expect(fake.files.has(adoptionIntentFilePath(AGENT, "telegram:42:17"))).toBe(true);
		await intents.remove("telegram:42:17");
		expect(intents.byProviderRequestKey("telegram:42:17")).toBeUndefined();
		expect(fake.files.has(adoptionIntentFilePath(AGENT, "telegram:42:17"))).toBe(false);
	});

	test("sidecar contains no endpoint credentials or lifecycle process details", async () => {
		const fake = new FakeFs();
		const intents = store(fake);
		await intents.put({
			...reservation(),
			token: "secret-token",
			endpoint: "ws://private",
			tmuxSession: "private",
		} as never);
		const raw = fake.files.get(adoptionIntentFilePath(AGENT, "telegram:42:17"))!;
		expect(raw).not.toContain("secret-token");
		expect(raw).not.toContain("endpoint");
		expect(raw).not.toContain("tmux");
	});

	test("build helper preserves the ten-minute default and provider key", () => {
		const built = buildAdoptionIntent({
			providerRequestKey: "telegram:42:20",
			topicId: 20,
			chatId: "42",
			target: TARGET,
			now: 100,
		});
		expect(built.providerRequestKey).toBe("telegram:42:20");
		expect(built.expiresAt - built.createdAt).toBe(DEFAULT_ADOPTION_INTENT_TTL_MS);
		expect(ADOPTION_INTENT_FILENAME_SUFFIX).toBe(".adoption-intent.json");
	});

	test("real filesystem sidecars survive rehydrate", async () => {
		const agentDir = fsSync.mkdtempSync(path.join(os.tmpdir(), "gjc-adoption-provider-key-"));
		try {
			const writer = new TelegramAdoptionIntentStore({ agentDir, now: () => 1_000 });
			await writer.put(reservation({ providerRequestKey: "telegram:42:21", topicId: 21 }));
			const reader = new TelegramAdoptionIntentStore({ agentDir, now: () => 1_000 });
			expect(await reader.rehydrate()).toBe(1);
			expect(reader.byProviderRequestKey("telegram:42:21")?.topicId).toBe(21);
		} finally {
			fsSync.rmSync(agentDir, { recursive: true, force: true });
		}
	});
});
