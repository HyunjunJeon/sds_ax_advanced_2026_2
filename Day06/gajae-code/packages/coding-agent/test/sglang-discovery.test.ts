import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { UNK_CONTEXT_WINDOW, UNK_MAX_TOKENS } from "@gajae-code/ai";
import { hookFetch, Snowflake } from "@gajae-code/utils";
import { kNoAuth } from "../src/config/model-auth";
import { ModelRegistry } from "../src/config/model-registry";
import { resetSettingsForTest, Settings } from "../src/config/settings";
import { AuthStorage } from "../src/session/auth-storage";

describe("ModelRegistry SGLang Discovery", () => {
	let tempDir: string;
	let modelsJsonPath: string;
	let authStorage: AuthStorage;
	let origApiKey: string | undefined;
	let origBaseUrl: string | undefined;

	beforeEach(async () => {
		resetSettingsForTest();
		tempDir = path.join(os.tmpdir(), `pi-test-sglang-discovery-${Snowflake.next()}`);
		fs.mkdirSync(tempDir, { recursive: true });
		modelsJsonPath = path.join(tempDir, "models.json");
		authStorage = await AuthStorage.create(path.join(tempDir, "testauth.db"));
		origApiKey = process.env.SGLANG_API_KEY ?? Bun.env.SGLANG_API_KEY;
		origBaseUrl = process.env.SGLANG_BASE_URL ?? Bun.env.SGLANG_BASE_URL;
		delete process.env.SGLANG_API_KEY;
		delete Bun.env.SGLANG_API_KEY;
		delete process.env.SGLANG_BASE_URL;
		delete Bun.env.SGLANG_BASE_URL;
	});

	afterEach(() => {
		resetSettingsForTest();
		authStorage.close();
		if (tempDir && fs.existsSync(tempDir)) {
			fs.rmSync(tempDir, { recursive: true });
		}
		if (origApiKey !== undefined) {
			process.env.SGLANG_API_KEY = origApiKey;
			Bun.env.SGLANG_API_KEY = origApiKey;
		} else {
			delete process.env.SGLANG_API_KEY;
			delete Bun.env.SGLANG_API_KEY;
		}
		if (origBaseUrl !== undefined) {
			process.env.SGLANG_BASE_URL = origBaseUrl;
			Bun.env.SGLANG_BASE_URL = origBaseUrl;
		} else {
			delete process.env.SGLANG_BASE_URL;
			delete Bun.env.SGLANG_BASE_URL;
		}
	});

	test("auto-discovers SGLang models and maps context length from max_model_len", async () => {
		using _hook = hookFetch(input => {
			const url = String(input);
			if (url.includes(":30000/v1/models")) {
				return new Response(
					JSON.stringify({
						object: "list",
						data: [
							{
								id: "Qwen3.5-122B-A10B-Q4",
								object: "model",
								max_model_len: 262144,
							},
						],
					}),
					{
						status: 200,
						headers: { "Content-Type": "application/json" },
					},
				);
			}
			return new Response(null, { status: 404 });
		});

		const hasAuthSpy = spyOn(authStorage, "hasAuth").mockReturnValue(false);
		const hasSpy = spyOn(authStorage, "has").mockReturnValue(false);
		const apiKeySpy = spyOn(authStorage, "getApiKey").mockImplementation(async provider => {
			if (provider === "sglang") return undefined;
			return undefined;
		});

		try {
			const registry = new ModelRegistry(authStorage, modelsJsonPath);
			await registry.refresh();

			const allModels = registry.getAll();
			const sglangModel = allModels.find(m => m.provider === "sglang" && m.id === "Qwen3.5-122B-A10B-Q4");
			expect(sglangModel).toBeDefined();
			expect(sglangModel?.contextWindow).toBe(262144);

			const available = registry.getAvailable();
			expect(available.some(m => m.provider === "sglang")).toBe(true);

			const apiKey = await registry.getApiKey(sglangModel!);
			expect(apiKey).toBe(kNoAuth);
		} finally {
			apiKeySpy.mockRestore();
			hasAuthSpy.mockRestore();
			hasSpy.mockRestore();
		}
	});

	test("forwards configured SGLang auth during discovery when configured", async () => {
		authStorage.setRuntimeApiKey("sglang", "test-sglang-key");

		using _hook = hookFetch((input, init) => {
			const url = String(input);
			if (url.includes(":30000/v1/models")) {
				expect(init?.redirect).toBe("error");
				const headers = init?.headers as Headers | Record<string, string> | undefined;
				const authHeader = headers instanceof Headers ? headers.get("Authorization") : headers?.Authorization;
				expect(authHeader).toBe("Bearer test-sglang-key");
				return new Response(
					JSON.stringify({
						object: "list",
						data: [{ id: "authenticated-sglang-model", max_model_len: 131072 }],
					}),
					{
						status: 200,
						headers: { "Content-Type": "application/json" },
					},
				);
			}
			return new Response(null, { status: 404 });
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		await registry.refresh();

		const model = registry.find("sglang", "authenticated-sglang-model");
		expect(model?.provider).toBe("sglang");
		expect(await registry.getApiKey(model!)).toBe("test-sglang-key");
	});

	test("honors a trusted remote SGLANG_BASE_URL when a credential is present", async () => {
		Bun.env.SGLANG_BASE_URL = "https://trusted-sglang.example/v1";
		authStorage.setRuntimeApiKey("sglang", "test-sglang-key");

		using _hook = hookFetch((input, init) => {
			expect(String(input)).toBe("https://trusted-sglang.example/v1/models");
			expect(init?.redirect).toBe("error");
			const headers = init?.headers as Headers | Record<string, string> | undefined;
			const authHeader = headers instanceof Headers ? headers.get("Authorization") : headers?.Authorization;
			expect(authHeader).toBe("Bearer test-sglang-key");
			return new Response(JSON.stringify({ data: [{ id: "remote-sglang-model" }] }), {
				status: 200,
				headers: { "Content-Type": "application/json" },
			});
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		await registry.refreshProvider("sglang", "online");

		expect(registry.find("sglang", "remote-sglang-model")?.baseUrl).toBe("https://trusted-sglang.example/v1");
	});

	test("does not probe a remote SGLANG_BASE_URL without a credential", async () => {
		Bun.env.SGLANG_BASE_URL = "https://sglang.example.test/v1";
		let requestedRemote = false;

		using _hook = hookFetch(input => {
			if (String(input) === "https://sglang.example.test/v1/models") requestedRemote = true;
			return new Response(null, { status: 404 });
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		await registry.refresh();

		expect(requestedRemote).toBe(false);
	});

	test("hardens explicitly configured loopback SGLang discovery", async () => {
		fs.writeFileSync(
			modelsJsonPath,
			JSON.stringify({
				providers: {
					sglang: {
						baseUrl: "http://127.0.0.1:19001/v1",
						api: "openai-completions",
						auth: "none",
						discovery: { type: "sglang" },
					},
				},
			}),
		);

		using _hook = hookFetch((input, init) => {
			if (String(input) !== "http://127.0.0.1:19001/v1/models") return new Response(null, { status: 404 });
			expect(init?.redirect).toBe("error");
			const signal = init?.signal;
			expect(signal).toBeDefined();
			const response = Promise.withResolvers<Response>();
			signal?.addEventListener("abort", () => response.reject(new DOMException("Aborted", "AbortError")), {
				once: true,
			});
			return response.promise;
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		const startedAt = performance.now();
		await registry.refreshProvider("sglang", "online");

		expect(performance.now() - startedAt).toBeLessThan(2_000);
	});

	test("accepts a loopback SGLANG_BASE_URL override for the implicit endpoint", async () => {
		Bun.env.SGLANG_BASE_URL = "http://127.0.0.1:19000/v1";
		const requestedUrls: string[] = [];

		using _hook = hookFetch(input => {
			const url = String(input);
			requestedUrls.push(url);
			if (url.includes(":19000/v1/models")) {
				return new Response(JSON.stringify({ data: [{ id: "custom-port-sglang-model" }] }), { status: 200 });
			}
			return new Response(null, { status: 404 });
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		await registry.refresh();

		expect(registry.find("sglang", "custom-port-sglang-model")).toBeDefined();
		expect(requestedUrls.some(url => url.includes(":30000/v1/models"))).toBe(false);
	});

	test("skips malformed records and bounds SGLang limits to finite positive values", async () => {
		using _hook = hookFetch(input => {
			if (!String(input).includes(":30000/v1/models")) return new Response(null, { status: 404 });
			return new Response(
				'{"data":[{"id":"invalid-limits","max_model_len":1e400,"context_length":-1,"max_tokens":0},{"id":"fractional-limits","max_model_len":1.5,"max_tokens":2.5},{"id":"unsafe-limits","max_model_len":9007199254740992,"max_tokens":9007199254740992},{"id":42},{"id":"valid-model","max_model_len":"131072","max_tokens":"8192"}]}',
				{ status: 200, headers: { "Content-Type": "application/json" } },
			);
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		await registry.refresh();

		expect(registry.find("sglang", "invalid-limits")?.contextWindow).toBe(UNK_CONTEXT_WINDOW);
		expect(registry.find("sglang", "invalid-limits")?.maxTokens).toBe(UNK_MAX_TOKENS);
		expect(registry.find("sglang", "fractional-limits")?.contextWindow).toBe(UNK_CONTEXT_WINDOW);
		expect(registry.find("sglang", "fractional-limits")?.maxTokens).toBe(UNK_MAX_TOKENS);
		expect(registry.find("sglang", "unsafe-limits")?.contextWindow).toBe(UNK_CONTEXT_WINDOW);
		expect(registry.find("sglang", "unsafe-limits")?.maxTokens).toBe(UNK_MAX_TOKENS);
		expect(registry.find("sglang", "42")).toBeUndefined();
		expect(registry.find("sglang", "valid-model")?.contextWindow).toBe(131072);
		expect(registry.find("sglang", "valid-model")?.maxTokens).toBe(8192);
	});

	test("treats an unreachable implicit SGLang server as optional", async () => {
		using _hook = hookFetch((input, init) => {
			if (String(input).includes(":30000/v1/models")) {
				const response = Promise.withResolvers<Response>();
				init?.signal?.addEventListener(
					"abort",
					() => response.reject(new DOMException("The operation was aborted", "AbortError")),
					{ once: true },
				);
				return response.promise;
			}
			return new Response(null, { status: 404 });
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		const startedAt = performance.now();
		await registry.refresh();

		expect(performance.now() - startedAt).toBeLessThan(2_000);
		expect(registry.getAll().some(model => model.provider === "sglang" && model.id === "unreachable")).toBe(false);
	});

	test("suppresses implicit SGLang descriptor discovery when the provider is disabled", async () => {
		await Settings.init({
			inMemory: true,
			overrides: {
				disabledProviders: ["llama.cpp", "lm-studio", "ollama", "omlx", "vllm", "sglang"],
			},
		});
		const requestedUrls: string[] = [];
		using _hook = hookFetch(input => {
			const url = String(input);
			requestedUrls.push(url);
			throw new Error(`Unexpected URL: ${url}`);
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);

		await registry.refresh("online");

		expect(requestedUrls.some(url => url.includes("127.0.0.1:30000"))).toBe(false);
	});

	test("suppresses implicit SGLang discovery when sglang is explicitly configured in models.yml", async () => {
		fs.writeFileSync(
			modelsJsonPath,
			JSON.stringify({
				providers: {
					sglang: {
						baseUrl: "http://127.0.0.1:30000/v1",
						api: "openai-completions",
						auth: "none",
						models: [{ id: "configured-sglang-model" }],
					},
				},
			}),
		);
		const requestedUrls: string[] = [];
		using _hook = hookFetch(input => {
			const url = String(input);
			requestedUrls.push(url);
			if (url === "http://127.0.0.1:30000/v1/models") throw new Error(`Unexpected SGLang discovery request: ${url}`);
			return new Response(null, { status: 404 });
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		expect(registry.getDiscoverableProviders()).not.toContain("sglang");

		await registry.refresh();

		expect(registry.find("sglang", "configured-sglang-model")).toBeDefined();
		expect(requestedUrls).not.toContain("http://127.0.0.1:30000/v1/models");
	});

	test("does not apply oMLX-only reasoning metadata to discovered SGLang models", async () => {
		// The qwen-chat-template / reasoning-effort inference in #discoverOpenAIModelsList is
		// gated on `providerConfig.provider === "omlx"` and intentionally NOT extended to SGLang —
		// SGLang models should discover with plain, non-reasoning compat metadata instead.
		using _hook = hookFetch(input => {
			if (!String(input).includes(":30000/v1/models")) return new Response(null, { status: 404 });
			return new Response(
				JSON.stringify({
					data: [{ id: "Qwen3.6-35B-A3B-8bit", max_model_len: 262144 }],
				}),
				{ status: 200, headers: { "Content-Type": "application/json" } },
			);
		});

		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		await registry.refresh();

		const model = registry.find("sglang", "Qwen3.6-35B-A3B-8bit");
		expect(model?.reasoning).toBeFalsy();
		expect(model?.thinking).toBeUndefined();
		expect(model?.compat).toBeUndefined();
	});
});
