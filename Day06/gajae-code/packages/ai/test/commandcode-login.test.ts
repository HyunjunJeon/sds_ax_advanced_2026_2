import { describe, expect, it, vi } from "bun:test";
import { commandCodeModelManagerOptions } from "../src/provider-models/openai-compat";
import { getEnvApiKey } from "../src/stream";
import { getOAuthProviders } from "../src/utils/oauth";
import { loginCommandCode } from "../src/utils/oauth/commandcode";

const INFERENCE_URL = "https://api.commandcode.ai/provider/v1/chat/completions";
const DASHBOARD_URL = "https://commandcode.ai/studio/#api-keys";

function modelsResponse(body: unknown, status = 200): Response {
	return new Response(typeof body === "string" ? body : JSON.stringify(body), {
		status,
		headers: { "Content-Type": "application/json" },
	});
}

describe("Command Code GOAT login", () => {
	it("opens the dashboard, prompts for a key, and verifies inference entitlement", async () => {
		const auth = vi.fn();
		const progress = vi.fn();
		const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
			expect(String(input)).toBe(INFERENCE_URL);
			expect(init?.method).toBe("POST");
			expect(init?.headers).toEqual({
				"Content-Type": "application/json",
				Authorization: "Bearer cmd-test-key",
			});
			expect(JSON.parse(String(init?.body))).toEqual({
				model: "zai-org/GLM-5.3",
				messages: [{ role: "user", content: "ping" }],
				max_tokens: 1,
				temperature: 0,
			});
			return modelsResponse({ choices: [{ message: { content: "pong" } }] });
		});

		const key = await loginCommandCode({
			onAuth: auth,
			onProgress: progress,
			onPrompt: async prompt => {
				expect(prompt).toEqual({
					message: "Paste your Command Code API key",
					placeholder: "cmd-...",
				});
				return "  cmd-test-key  ";
			},
			fetch: fetchMock as unknown as typeof fetch,
		});

		expect(key).toBe("cmd-test-key");
		expect(auth).toHaveBeenCalledWith({
			url: DASHBOARD_URL,
			instructions: "Create or copy your Command Code API key",
		});
		expect(progress).toHaveBeenCalledWith("Verifying Command Code inference entitlement...");
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it("rejects control characters before making a request", async () => {
		const fetchMock = vi.fn();
		await expect(
			loginCommandCode({ onPrompt: async () => "cmd\tunsafe", fetch: fetchMock as unknown as typeof fetch }),
		).rejects.toThrow(/control characters/);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it("requires a non-empty key and does not call the endpoint", async () => {
		const fetchMock = vi.fn();
		await expect(
			loginCommandCode({
				onPrompt: async () => " \t",
				fetch: fetchMock as unknown as typeof fetch,
			}),
		).rejects.toThrow("API key is required");
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it("requires an onPrompt callback", async () => {
		await expect(loginCommandCode({})).rejects.toThrow("Command Code GOAT login requires onPrompt callback");
	});

	it("cancels after prompting without sending the key", async () => {
		const controller = new AbortController();
		const fetchMock = vi.fn();
		await expect(
			loginCommandCode({
				onPrompt: async () => {
					controller.abort();
					return "cmd-cancelled";
				},
				fetch: fetchMock as unknown as typeof fetch,
				signal: controller.signal,
			}),
		).rejects.toThrow("Login cancelled");
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it("rejects a non-catalog response without leaking the key or control bytes", async () => {
		const key = "cmd-secret-key";
		const body = `{"error":"invalid key ${key}","Authorization":"Bearer ${key}"}\u001b[31m${"x".repeat(5000)}`;
		const fetchMock = vi.fn(async () => modelsResponse(body, 401));

		let message = "";
		try {
			await loginCommandCode({ onPrompt: async () => key, fetch: fetchMock as unknown as typeof fetch });
		} catch (error) {
			message = error instanceof Error ? error.message : String(error);
		}

		expect(message).toContain("Command Code GOAT API key validation failed (401)");
		expect(message).not.toContain(key);
		expect(message).not.toMatch(/[\x00-\x1f\x7f-\x9f]/u);
		expect(message.length).toBeLessThan(400);
	});

	it("wraps network failures without leaking key or control bytes", async () => {
		const key = "cmd-network-secret";
		const fetchMock = vi.fn(async () => {
			throw new Error(`request failed for ${key}\n\u001b[31m`);
		});

		let errorMessage = "";
		try {
			await loginCommandCode({ onPrompt: async () => key, fetch: fetchMock as unknown as typeof fetch });
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : String(error);
		}
		expect(errorMessage).toContain("Command Code GOAT API key validation failed");
		expect(errorMessage).not.toContain(key);
		expect(errorMessage).not.toMatch(/[\x00-\x1f\x7f-\x9f]/u);
		expect(errorMessage.length).toBeLessThan(400);
	});
});

describe("Command Code GOAT fresh descriptor", () => {
	it("routes Claude-named models through the OpenAI-compatible gateway", async () => {
		const realFetch = globalThis.fetch;
		globalThis.fetch = (async () =>
			new Response(JSON.stringify({ data: [{ id: "claude-opus-5.5" }, { id: "zai-org/GLM-5.3" }] }), {
				status: 200,
			})) as unknown as typeof fetch;
		try {
			const models = await commandCodeModelManagerOptions({ apiKey: "cmd-test-key" }).fetchDynamicModels?.();
			expect(models?.find(model => model.id === "claude-opus-5.5")).toMatchObject({
				api: "anthropic-messages",
				baseUrl: "https://api.commandcode.ai/provider",
			});
			expect(models?.find(model => model.id === "zai-org/GLM-5.3")).toMatchObject({
				api: "openai-completions",
				baseUrl: "https://api.commandcode.ai/provider/v1",
				compat: { maxTokensField: "max_tokens" },
			});
		} finally {
			globalThis.fetch = realFetch;
		}
	});
});

describe("Command Code GOAT OAuth registry", () => {
	it("exposes the canonical provider id", () => {
		expect(getOAuthProviders()).toContainEqual({
			id: "commandcode-goat",
			name: "Command Code GOAT",
			available: true,
		});
	});
});

describe("Command Code GOAT environment credentials", () => {
	it("resolves CMD_API_KEY for the canonical provider id", () => {
		const previous = Bun.env.CMD_API_KEY;
		try {
			Bun.env.CMD_API_KEY = "cmd-env-key";
			expect(getEnvApiKey("commandcode-goat")).toBe("cmd-env-key");
		} finally {
			if (previous === undefined) delete Bun.env.CMD_API_KEY;
			else Bun.env.CMD_API_KEY = previous;
		}
	});
});
