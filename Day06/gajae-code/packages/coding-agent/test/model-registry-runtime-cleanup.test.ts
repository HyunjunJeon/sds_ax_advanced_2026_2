import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type AssistantMessageEventStream, clearCustomApis, getCustomApi } from "@gajae-code/ai";
import { ModelRegistry, type ProviderConfigInput } from "@gajae-code/coding-agent/config/model-registry";
import { AuthStorage } from "@gajae-code/coding-agent/session/auth-storage";
import { Snowflake } from "@gajae-code/utils";

describe("ModelRegistry runtime source cleanup", () => {
	let tempDir: string;
	let modelsJsonPath: string;
	let authStorage: AuthStorage;

	const sourceId = "ext://runtime-cleanup";
	const baseModel: NonNullable<ProviderConfigInput["models"]>[number] = {
		id: "runtime-model",
		name: "Runtime Model",
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 128000,
		maxTokens: 8192,
	};

	const streamSimple: NonNullable<ProviderConfigInput["streamSimple"]> = () =>
		({}) as unknown as AssistantMessageEventStream;

	beforeEach(async () => {
		tempDir = path.join(os.tmpdir(), `pi-test-model-registry-runtime-cleanup-${Snowflake.next()}`);
		fs.mkdirSync(tempDir, { recursive: true });
		modelsJsonPath = path.join(tempDir, "models.json");
		authStorage = await AuthStorage.create(path.join(tempDir, "testauth.db"));
	});

	afterEach(() => {
		clearCustomApis();
		authStorage.close();
		if (tempDir && fs.existsSync(tempDir)) {
			fs.rmSync(tempDir, { recursive: true, force: true });
		}
	});

	test("clearSourceRegistrations removes runtime overlays and fallback auth for that source", () => {
		const registry = new ModelRegistry(authStorage, modelsJsonPath);
		const config: ProviderConfigInput = {
			baseUrl: "https://runtime.example.com/v1",
			apiKey: "RUNTIME_KEY",
			api: "custom-runtime-cleanup-api",
			streamSimple,
			models: [baseModel],
		};

		registry.registerProvider("runtime-provider", config, sourceId);

		expect(registry.find("runtime-provider", "runtime-model")).toBeDefined();
		expect(registry.authStorage.hasAuth("runtime-provider")).toBe(true);
		expect(getCustomApi("custom-runtime-cleanup-api")).toBeDefined();

		registry.clearSourceRegistrations(sourceId);

		expect(registry.find("runtime-provider", "runtime-model")).toBeUndefined();
		expect(registry.authStorage.hasAuth("runtime-provider")).toBe(false);
		expect(getCustomApi("custom-runtime-cleanup-api")).toBeUndefined();
	});

	test("shared AuthStorage keeps registry config and fallback ownership isolated", async () => {
		const secondModelsJsonPath = path.join(tempDir, "models-second.json");
		await Bun.write(
			modelsJsonPath,
			JSON.stringify({
				providers: {
					"first-provider": {
						baseUrl: "https://first.example.com/v1",
						api: "openai-responses",
						apiKey: "first-key",
						models: [{ id: "first-model" }],
					},
				},
			}),
		);
		await Bun.write(
			secondModelsJsonPath,
			JSON.stringify({
				providers: {
					"second-provider": {
						baseUrl: "https://second.example.com/v1",
						api: "openai-responses",
						apiKey: "second-key",
						models: [{ id: "second-model" }],
					},
				},
			}),
		);

		const firstRegistry = new ModelRegistry(authStorage, modelsJsonPath, undefined, { automaticRefresh: false });
		const secondRegistry = new ModelRegistry(authStorage, secondModelsJsonPath, undefined, {
			automaticRefresh: false,
		});
		try {
			expect(await firstRegistry.getApiKeyForProvider("first-provider")).toBe("first-key");
			expect(await secondRegistry.getApiKeyForProvider("second-provider")).toBe("second-key");

			// A static reload removes only the reloading registry's owned config key.
			await Bun.write(modelsJsonPath, JSON.stringify({ providers: {} }));
			const updatedAt = new Date(Date.now() + 1000);
			fs.utimesSync(modelsJsonPath, updatedAt, updatedAt);
			await firstRegistry.refresh("offline");
			expect(await secondRegistry.getApiKeyForProvider("second-provider")).toBe("second-key");

			// Clearing the shared active config surface must leave each registry's
			// fallback resolver available until that registry is disposed.
			authStorage.clearConfigApiKeys();
			expect(await secondRegistry.getApiKeyForProvider("second-provider")).toBe("second-key");
			firstRegistry.dispose();
			expect(await secondRegistry.getApiKeyForProvider("second-provider")).toBe("second-key");
		} finally {
			firstRegistry.dispose();
			secondRegistry.dispose();
		}
	});

	test("shared AuthStorage keeps same-provider credentials owner-bound", async () => {
		const secondModelsJsonPath = path.join(tempDir, "models-second.json");
		const provider = "shared-provider";
		const modelId = "shared-model";
		await Bun.write(
			modelsJsonPath,
			JSON.stringify({
				providers: {
					[provider]: {
						baseUrl: "https://first.example.com/v1",
						api: "openai-responses",
						apiKey: "first-key",
						models: [{ id: modelId }],
					},
				},
			}),
		);
		await Bun.write(
			secondModelsJsonPath,
			JSON.stringify({
				providers: {
					[provider]: {
						baseUrl: "https://second.example.com/v1",
						api: "openai-responses",
						apiKey: "second-key",
						models: [{ id: modelId }],
					},
				},
			}),
		);

		const firstRegistry = new ModelRegistry(authStorage, modelsJsonPath, undefined, { automaticRefresh: false });
		const secondRegistry = new ModelRegistry(authStorage, secondModelsJsonPath, undefined, {
			automaticRefresh: false,
		});
		try {
			const firstModel = firstRegistry.find(provider, modelId);
			const secondModel = secondRegistry.find(provider, modelId);
			expect(firstModel?.baseUrl).toBe("https://first.example.com/v1");
			expect(secondModel?.baseUrl).toBe("https://second.example.com/v1");
			expect(await firstRegistry.getApiKeyForProvider(provider)).toBe("first-key");
			expect(await secondRegistry.getApiKeyForProvider(provider)).toBe("second-key");

			const firstSearch = firstRegistry.getActiveSearchModelContext(firstModel!);
			const secondSearch = secondRegistry.getActiveSearchModelContext(secondModel!);
			expect((await firstSearch.resolveCredentials!({})).apiKey).toBe("first-key");
			expect((await secondSearch.resolveCredentials!({})).apiKey).toBe("second-key");

			await firstRegistry.refresh("offline");
			expect(await firstRegistry.getApiKeyForProvider(provider)).toBe("first-key");
			expect(await secondRegistry.getApiKeyForProvider(provider)).toBe("second-key");
			await secondRegistry.refresh("offline");
			expect(await firstRegistry.getApiKeyForProvider(provider)).toBe("first-key");
			expect(await secondRegistry.getApiKeyForProvider(provider)).toBe("second-key");
			authStorage.clearConfigApiKeys();
			expect(await firstRegistry.getApiKeyForProvider(provider)).toBe("first-key");
			expect(await secondRegistry.getApiKeyForProvider(provider)).toBe("second-key");

			firstRegistry.dispose();
			expect(await secondRegistry.getApiKeyForProvider(provider)).toBe("second-key");
			expect((await secondSearch.resolveCredentials!({})).apiKey).toBe("second-key");
		} finally {
			firstRegistry.dispose();
			secondRegistry.dispose();
		}
	});
});
