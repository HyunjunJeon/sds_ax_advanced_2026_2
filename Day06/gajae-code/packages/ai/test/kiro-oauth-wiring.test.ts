import { afterEach, describe, expect, it, test, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { AuthStorage, SqliteAuthCredentialStore } from "../src/auth-storage";
import { getBundledModel, getBundledModels } from "../src/models";
import { DEFAULT_MODEL_PER_PROVIDER, PROVIDER_DESCRIPTORS } from "../src/provider-models/descriptors";
import { kiroApiStaticModels } from "../src/providers/kiro-api-key";
import type { Context } from "../src/types";
import { getOAuthProviders, refreshOAuthToken } from "../src/utils/oauth";
import * as kiroOAuthModule from "../src/utils/oauth/kiro";

/**
 * End-to-end regression coverage for issue #5064: Kiro OAuth was advertised
 * in the provider list, but the login dispatcher had no `case "kiro"`
 * (`Unknown OAuth provider: kiro`), `mapOptionsForApi` had no case for
 * `kiro-codewhisperer-stream` (`Unhandled API in mapOptionsForApi`), and the
 * bundled model catalog had no `kiro` entries — leaving OAuth-only users
 * without a selectable model.
 */

describe("Kiro OAuth provider advertisement is coherent with wiring", () => {
	test("kiro is advertised in the OAuth provider list", () => {
		const providers = getOAuthProviders();
		const kiro = providers.find(p => p.id === "kiro");
		expect(kiro).toBeDefined();
		expect(kiro?.available).toBe(true);
	});

	test("kiro has a login dispatch case (not Unknown OAuth provider)", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "pi-ai-kiro-login-"));
		try {
			const store = await SqliteAuthCredentialStore.open(path.join(tempDir, "agent.db"));
			const authStorage = new AuthStorage(store);
			try {
				const loginSpy = { called: false as boolean, url: "" as string };
				// Stub the underlying device-code flow (no network) while proving
				// AuthStorage.login("kiro", ...) actually reaches it instead of
				// throwing "Unknown OAuth provider: kiro".
				const loginKiroSpy = vi.spyOn(kiroOAuthModule, "loginKiro").mockImplementation(async options => {
					loginSpy.called = true;
					options.onAuth("https://device.sso.us-east-1.amazonaws.com/", "Enter code: TEST-CODE");
					return { access: "kiro-access-token", refresh: "kiro-refresh-token", expires: Date.now() + 3600_000 };
				});
				try {
					await authStorage.login("kiro", {
						onAuth: info => {
							loginSpy.url = info.url;
						},
						onPrompt: async () => "",
					});
				} finally {
					loginKiroSpy.mockRestore();
				}

				expect(loginSpy.called).toBe(true);
				expect(loginSpy.url).toContain("amazonaws.com");

				const oauth = store.getOAuth("kiro");
				expect(oauth?.access).toBe("kiro-access-token");
			} finally {
				store.close();
			}
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	it("rejects a truly unknown OAuth provider (regression guard)", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "pi-ai-unknown-login-"));
		try {
			const store = await SqliteAuthCredentialStore.open(path.join(tempDir, "agent.db"));
			const authStorage = new AuthStorage(store);
			try {
				const unknownProvider = "definitely-not-a-real-provider" as unknown as Parameters<AuthStorage["login"]>[0];
				await expect(
					authStorage.login(unknownProvider, {
						onAuth: () => {},
						onPrompt: async () => "",
					}),
				).rejects.toThrow(/Unknown OAuth provider/);
			} finally {
				store.close();
			}
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	test("refreshOAuthToken keeps routing kiro through refreshKiroToken (unaffected by login fix)", async () => {
		const refreshed = { access: "refreshed-access", refresh: "refreshed-refresh", expires: Date.now() + 3600_000 };
		const refreshKiroTokenSpy = vi.spyOn(kiroOAuthModule, "refreshKiroToken").mockResolvedValue(refreshed);
		try {
			const result = await refreshOAuthToken("kiro", { access: "a", refresh: "r", expires: Date.now() - 1000 });
			expect(refreshKiroTokenSpy).toHaveBeenCalledTimes(1);
			expect(result).toEqual(refreshed);
		} finally {
			refreshKiroTokenSpy.mockRestore();
		}
	});
});

describe("Kiro model catalog and default model", () => {
	test("bundled models.json has a kiro entry with a valid default model id", () => {
		const models = getBundledModels("kiro");
		expect(models.length).toBeGreaterThan(0);
		expect(models.every(m => m.api === "kiro-codewhisperer-stream")).toBe(true);
		expect(models.every(m => m.provider === "kiro")).toBe(true);

		const defaultModelId = DEFAULT_MODEL_PER_PROVIDER.kiro;
		expect(models.some(m => m.id === defaultModelId)).toBe(true);
	});

	test("default kiro model resolves through getBundledModel", () => {
		const defaultModelId = DEFAULT_MODEL_PER_PROVIDER.kiro;
		const model = getBundledModel("kiro", defaultModelId);
		expect(model).toBeDefined();
		expect(model.api).toBe("kiro-codewhisperer-stream");
	});

	test("bundled catalog matches the static Kiro API-key catalog used for OAuth-only discovery", () => {
		const bundled = new Set(getBundledModels("kiro").map(m => m.id));
		const staticCatalog = kiroApiStaticModels();
		for (const model of staticCatalog) {
			expect(bundled.has(model.id)).toBe(true);
		}
	});

	test("kiro is registered in PROVIDER_DESCRIPTORS with a defaultModel present in its own catalog", () => {
		const descriptor = PROVIDER_DESCRIPTORS.find(d => d.providerId === "kiro");
		expect(descriptor).toBeDefined();
		const staticCatalog = kiroApiStaticModels();
		expect(staticCatalog.some(m => m.id === descriptor?.defaultModel)).toBe(true);
	});
});

describe("OAuth CodeWhisperer transport normalizes the wire model id", () => {
	const originalFetch = globalThis.fetch;

	afterEach(() => {
		globalThis.fetch = originalFetch;
	});

	test("sends the canonical dotted Kiro model id, not the local dashed catalog alias", async () => {
		const { streamKiroCodeWhisperer } = await import("../src/providers/kiro-codewhisperer");
		// The dashed selector as published in models.json (kiroApiStaticModels()
		// registers both the dotted upstream id and, when it differs, a dashed
		// local alias like "claude-haiku-4-5").
		const dashedModel = getBundledModel<"kiro-codewhisperer-stream">("kiro", "claude-haiku-4-5");
		expect(dashedModel).toBeDefined();
		if (!dashedModel) throw new Error("expected a dashed Kiro catalog alias for this regression test");

		globalThis.fetch = (async () => new Response("", { status: 500 })) as unknown as typeof fetch;

		let capturedPayload: unknown;
		const context: Context = { messages: [{ role: "user", content: "hi", timestamp: Date.now() }] };
		const stream = streamKiroCodeWhisperer(dashedModel, context, {
			apiKey: "oauth-bearer-token",
			onPayload: payload => {
				capturedPayload = payload;
			},
		});
		for await (const _event of stream) {
			// drain to completion; only onPayload matters for this test
		}

		const payload = capturedPayload as {
			conversationState: { currentMessage: { userInputMessage: { modelId?: string } } };
		};
		expect(payload.conversationState.currentMessage.userInputMessage.modelId).toBe("claude-haiku-4.5");
	});
});

describe("mapOptionsForApi handles kiro-codewhisperer-stream", () => {
	test("streamSimple does not throw Unhandled API for kiro-codewhisperer-stream", async () => {
		const { stream } = await import("../src/stream");
		const model = getBundledModel("kiro", DEFAULT_MODEL_PER_PROVIDER.kiro);
		const context: Context = { messages: [{ role: "user", content: "hi", timestamp: Date.now() }] };

		// No network credentials configured — this must fail with a Kiro
		// credential/transport error, never "Unhandled API in mapOptionsForApi".
		const events = stream(model, context, { apiKey: undefined });
		let sawError: unknown;
		for await (const event of events) {
			if (event.type === "error") {
				sawError = event.error?.errorMessage;
			}
		}
		expect(String(sawError ?? "")).not.toContain("Unhandled API in mapOptionsForApi");
	});
});

describe("Kiro provider module import boundary (standalone bundle smoke)", () => {
	test("the lazily-loaded kiro-codewhisperer-stream runtime descriptor resolves without a module resolution error", async () => {
		const { PROVIDER_RUNTIME_DESCRIPTORS } = await import("../src/providers/register-builtins");
		const descriptor = PROVIDER_RUNTIME_DESCRIPTORS.find(d => d.api === "kiro-codewhisperer-stream");
		expect(descriptor).toBeDefined();
		const loaded = (await descriptor?.load()) as { stream?: unknown } | undefined;
		expect(typeof loaded?.stream).toBe("function");
	});

	test("streamKiroCodeWhisperer is exported and callable from register-builtins", async () => {
		const { streamKiroCodeWhisperer } = await import("../src/providers/register-builtins");
		expect(typeof streamKiroCodeWhisperer).toBe("function");
	});
});
