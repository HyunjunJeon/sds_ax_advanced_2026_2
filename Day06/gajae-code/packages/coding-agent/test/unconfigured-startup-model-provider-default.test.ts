import { describe, expect, test } from "bun:test";
import { DEFAULT_MODEL_PER_PROVIDER, type Model } from "@gajae-code/ai/core";
import { orderByProviderDefaultFirst } from "@gajae-code/coding-agent/sdk/session";

function model(provider: string, id: string): Model {
	return {
		provider,
		id,
		name: `${provider}/${id}`,
		api: "anthropic-messages",
		baseUrl: `https://${provider}.example.com`,
		reasoning: false,
		input: ["text"],
		cost: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 200000,
		maxTokens: 8192,
	} as Model;
}

const anthropicDefault = DEFAULT_MODEL_PER_PROVIDER.anthropic;

describe("unconfigured startup model prefers a curated provider default", () => {
	test("hoists the provider default ahead of an earlier retired catalog entry", () => {
		// A withdrawn model sorts first because its ID carries an older date
		// suffix. Picking the first credentialed candidate would start the
		// session on a model the provider no longer serves.
		const candidates = [model("anthropic", "claude-3-5-sonnet-20240620"), model("anthropic", anthropicDefault)];

		const ordered = orderByProviderDefaultFirst(candidates);

		expect(ordered[0]?.provider).toBe("anthropic");
		expect(ordered[0]?.id).toBe(anthropicDefault);
	});

	test("hoists across providers when the first entry belongs to another provider", () => {
		const candidates = [
			model("amazon-bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"),
			model("anthropic", anthropicDefault),
		];

		const ordered = orderByProviderDefaultFirst(candidates);

		expect(ordered[0]?.provider).toBe("anthropic");
		expect(ordered[0]?.id).toBe(anthropicDefault);
	});

	test("uses explicit provider priority before curated default table order", () => {
		const candidates = [
			model("anthropic", anthropicDefault),
			model("amazon-bedrock", DEFAULT_MODEL_PER_PROVIDER["amazon-bedrock"]),
		];

		const ordered = orderByProviderDefaultFirst(candidates, ["amazon-bedrock", "anthropic"]);

		expect(ordered).toEqual([candidates[1], candidates[0]]);
	});

	test("preserves every candidate so credential scanning still sees the full set", () => {
		const candidates = [
			model("anthropic", "claude-3-5-sonnet-20240620"),
			model("anthropic", anthropicDefault),
			model("amazon-bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"),
		];

		const ordered = orderByProviderDefaultFirst(candidates);

		expect(ordered).toHaveLength(candidates.length);
		expect(new Set(ordered.map(m => `${m.provider}/${m.id}`))).toEqual(
			new Set(candidates.map(m => `${m.provider}/${m.id}`)),
		);
	});

	test("preserves a custom provider when its flattened key collides with a provider default", () => {
		const candidates = [
			model("openrouter/openai", "gpt-5.4"),
			model("openrouter", DEFAULT_MODEL_PER_PROVIDER.openrouter),
		];

		const ordered = orderByProviderDefaultFirst(candidates);

		expect(ordered).toEqual([candidates[1], candidates[0]]);
	});

	test("keeps catalog order when no candidate is a curated provider default", () => {
		const candidates = [
			model("anthropic", "claude-3-5-sonnet-20240620"),
			model("anthropic", "claude-3-5-sonnet-20241022"),
		];

		const ordered = orderByProviderDefaultFirst(candidates);

		expect(ordered.map(m => m.id)).toEqual(["claude-3-5-sonnet-20240620", "claude-3-5-sonnet-20241022"]);
	});

	test("returns an empty list unchanged", () => {
		expect(orderByProviderDefaultFirst([])).toEqual([]);
	});
});
