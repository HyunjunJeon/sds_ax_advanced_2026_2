import { describe, expect, it } from "bun:test";
import type { Context, Model } from "@gajae-code/ai";
import { Effort } from "@gajae-code/ai/model-thinking";
import {
	type GrokCliModelConfig,
	resolveModels,
	supportsReasoningEffort,
} from "../src/defaults/gjc/extensions/grok-cli-vendor/src/models/catalog";
import { sanitizePayload } from "../src/defaults/gjc/extensions/grok-cli-vendor/src/payload/sanitize";
import { streamGrokCli } from "../src/defaults/gjc/extensions/grok-cli-vendor/src/provider/stream";
import {
	assertBundledGrokCliDefaults,
	getBundledGrokBuildExtensionFactory,
	getBundledGrokCliModelDefaults,
} from "../src/defaults/gjc-grok-cli";
import type { ExtensionAPI, ProviderConfig } from "../src/extensibility/extensions";

async function captureGrokBuildProviderConfig(): Promise<ProviderConfig> {
	let providerConfig: ProviderConfig | undefined;
	await getBundledGrokBuildExtensionFactory()({
		registerProvider(name: string, config: ProviderConfig) {
			if (name === "grok-build") providerConfig = config;
		},
		on() {},
		registerCommand() {},
	} as unknown as ExtensionAPI);
	if (!providerConfig) throw new Error("Grok Build provider was not registered");
	return providerConfig;
}

describe("bundled Grok CLI defaults", () => {
	it("loads the shipped vendor defaults without filesystem path discovery", async () => {
		await expect(assertBundledGrokCliDefaults()).resolves.toBeUndefined();
		expect(typeof getBundledGrokBuildExtensionFactory()).toBe("function");
		expect(getBundledGrokCliModelDefaults()).toContain("grok-composer-2.5-fast");
	});

	it("registers Grok 4.5 with verified model metadata and documented effort cap", async () => {
		const previousGrokCliModels = process.env.GJC_GROK_CLI_MODELS;
		delete process.env.GJC_GROK_CLI_MODELS;
		try {
			const model = resolveModels().find(candidate => candidate.id === "grok-4.5");

			expect(model).toEqual({
				id: "grok-4.5",
				name: "Grok 4.5",
				reasoning: true,
				input: ["text", "image"],
				cost: { input: 2, output: 6, cacheRead: 0.5, cacheWrite: 0 },
				contextWindow: 500_000,
				maxTokens: 30_000,
				maxReasoningEffort: Effort.High,
			});
			expect(supportsReasoningEffort("grok-build/grok-4.5")).toBe(true);

			const providerConfig = await captureGrokBuildProviderConfig();
			const registeredModel = providerConfig?.models?.find(candidate => candidate.id === "grok-4.5");
			expect(registeredModel?.thinking).toEqual({
				minLevel: Effort.Low,
				maxLevel: Effort.High,
				mode: "effort",
			});
		} finally {
			if (previousGrokCliModels === undefined) {
				delete process.env.GJC_GROK_CLI_MODELS;
			} else {
				process.env.GJC_GROK_CLI_MODELS = previousGrokCliModels;
			}
		}
	});

	it("maps official Grok 4.5 aliases to canonical metadata and effort limits", async () => {
		const previousGrokCliModels = process.env.GJC_GROK_CLI_MODELS;
		const aliases = ["grok-4.5-latest", "grok-build-latest"];
		process.env.GJC_GROK_CLI_MODELS = aliases.join(",");
		try {
			const expectedMetadata = {
				name: "Grok 4.5",
				reasoning: true,
				input: ["text", "image"],
				cost: { input: 2, output: 6, cacheRead: 0.5, cacheWrite: 0 },
				contextWindow: 500_000,
				maxTokens: 30_000,
				maxReasoningEffort: Effort.High,
			} satisfies Omit<GrokCliModelConfig, "id">;
			expect(resolveModels()).toEqual(aliases.map(id => ({ id, ...expectedMetadata })));
			for (const alias of aliases) {
				expect(supportsReasoningEffort(`grok-build/${alias}`)).toBe(true);
			}

			const providerConfig = await captureGrokBuildProviderConfig();
			expect(
				providerConfig.models?.map(model => ({
					id: model.id,
					thinking: model.thinking,
				})),
			).toEqual(
				aliases.map(id => ({
					id,
					thinking: {
						minLevel: Effort.Low,
						maxLevel: Effort.High,
						mode: "effort",
					},
				})),
			);
		} finally {
			if (previousGrokCliModels === undefined) {
				delete process.env.GJC_GROK_CLI_MODELS;
			} else {
				process.env.GJC_GROK_CLI_MODELS = previousGrokCliModels;
			}
		}
	});

	it("keeps registered and sanitized effort ceilings aligned for older and family-variant models", async () => {
		const previousGrokCliModels = process.env.GJC_GROK_CLI_MODELS;
		process.env.GJC_GROK_CLI_MODELS = "grok-4.3,grok-4.5-preview";
		try {
			const providerConfig = await captureGrokBuildProviderConfig();
			const modelById = new Map(providerConfig.models?.map(model => [model.id, model]));

			expect(modelById.get("grok-4.3")?.thinking).toEqual({
				minLevel: Effort.Low,
				maxLevel: Effort.High,
				mode: "effort",
			});
			expect(modelById.get("grok-4.5-preview")?.thinking).toEqual({
				minLevel: Effort.Low,
				maxLevel: Effort.High,
				mode: "effort",
			});
			expect(modelById.get("grok-4.3")?.compat).toEqual({ supportsReasoningEffort: true });
			expect(modelById.get("grok-4.5-preview")?.compat).toEqual({ supportsReasoningEffort: true });
			expect(
				sanitizePayload({ reasoning: { effort: Effort.XHigh } }, "grok-4.3", undefined, process.cwd()).reasoning,
			).toEqual({ effort: Effort.High });
			expect(
				sanitizePayload({ reasoning: { effort: Effort.XHigh } }, "grok-4.5-preview", undefined, process.cwd())
					.reasoning,
			).toEqual({ effort: Effort.High });
		} finally {
			if (previousGrokCliModels === undefined) {
				delete process.env.GJC_GROK_CLI_MODELS;
			} else {
				process.env.GJC_GROK_CLI_MODELS = previousGrokCliModels;
			}
		}
	});

	it("declares effort support only for catalog-supported Grok models", async () => {
		const previousGrokCliModels = process.env.GJC_GROK_CLI_MODELS;
		delete process.env.GJC_GROK_CLI_MODELS;
		try {
			const providerConfig = await captureGrokBuildProviderConfig();
			const modelById = new Map(providerConfig.models?.map(model => [model.id, model]));

			expect(modelById.get("grok-4.6")?.compat).toEqual({ supportsReasoningEffort: true });
			expect(modelById.get("grok-4.5")?.compat).toEqual({ supportsReasoningEffort: true });
			expect(modelById.get("grok-build")?.compat).toBeUndefined();
			expect(modelById.get("grok-composer-2.5-fast")?.compat).toBeUndefined();
		} finally {
			if (previousGrokCliModels === undefined) {
				delete process.env.GJC_GROK_CLI_MODELS;
			} else {
				process.env.GJC_GROK_CLI_MODELS = previousGrokCliModels;
			}
		}
	});

	it("keeps xhigh in the full Grok Build request and strips unsupported models safely", async () => {
		const providerConfig = await captureGrokBuildProviderConfig();
		const capturePayload = async (modelId: string): Promise<Record<string, unknown>> => {
			const definition = providerConfig.models?.find(model => model.id === modelId);
			if (!definition) throw new Error(`Grok Build test model ${modelId} was not registered`);
			const model = {
				...definition,
				api: "openai-responses",
				baseUrl: providerConfig.baseUrl ?? "https://cli-chat-proxy.grok.com/v1",
				provider: "grok-build",
			} as Model<"openai-responses">;
			const controller = new AbortController();
			controller.abort();
			const { promise, resolve } = Promise.withResolvers<Record<string, unknown>>();
			streamGrokCli(
				model,
				{
					messages: [{ role: "user", content: "hello", timestamp: 0 }],
					systemPrompt: [],
				} as unknown as Context,
				{
					apiKey: "test-key",
					reasoning: Effort.XHigh,
					signal: controller.signal,
					onPayload: payload => {
						resolve(payload as Record<string, unknown>);
					},
				},
			);
			return promise;
		};

		const supportedPayload = await capturePayload("grok-4.6");
		expect(supportedPayload.reasoning).toEqual({ effort: Effort.XHigh, summary: "auto" });
		expect(sanitizePayload(supportedPayload, "grok-4.6", undefined, process.cwd()).reasoning).toEqual({
			effort: Effort.XHigh,
		});

		const unsupportedPayload = await capturePayload("grok-build");
		expect(unsupportedPayload.reasoning).toBeUndefined();
		expect(sanitizePayload(unsupportedPayload, "grok-build", undefined, process.cwd()).reasoning).toBeUndefined();
	});
});
