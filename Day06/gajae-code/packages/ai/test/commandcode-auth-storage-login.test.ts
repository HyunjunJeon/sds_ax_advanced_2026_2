import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

import { AuthStorage, SqliteAuthCredentialStore } from "../src/auth-storage";

const INFERENCE_URL = "https://api.commandcode.ai/provider/v1/chat/completions";

function modelsResponse(body: unknown, status = 200): Response {
	return new Response(typeof body === "string" ? body : JSON.stringify(body), {
		status,
		headers: { "Content-Type": "application/json" },
	});
}

describe("Command Code GOAT AuthStorage login", () => {
	let tempDir = "";
	let store: SqliteAuthCredentialStore | undefined;

	afterEach(async () => {
		store?.close();
		store = undefined;
		if (tempDir) {
			await fs.rm(tempDir, { recursive: true, force: true });
			tempDir = "";
		}
	});

	it("persists a validated API key under the canonical provider id", async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "gjc-commandcode-login-"));
		const dbStore = await SqliteAuthCredentialStore.open(path.join(tempDir, "agent.db"));
		store = dbStore;
		const authStorage = new AuthStorage(dbStore);
		const fetchMock = async (input: string | URL, init?: RequestInit): Promise<Response> => {
			expect(String(input)).toBe(INFERENCE_URL);
			expect(init?.method).toBe("POST");
			expect(init?.headers).toEqual({
				"Content-Type": "application/json",
				Authorization: "Bearer cmd-storage-key",
			});
			return modelsResponse({ choices: [{ message: { content: "pong" } }] });
		};

		await authStorage.login("commandcode-goat", {
			onAuth: () => {},
			onPrompt: async () => " cmd-storage-key ",
			fetch: fetchMock as unknown as typeof fetch,
		});

		expect(dbStore.getApiKey("commandcode-goat")).toBe("cmd-storage-key");
		expect(dbStore.listAuthCredentials("commandcode-goat")).toHaveLength(1);
	});

	it("keeps the existing credential when models validation fails", async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "gjc-commandcode-login-"));
		const dbStore = await SqliteAuthCredentialStore.open(path.join(tempDir, "agent.db"));
		store = dbStore;
		const authStorage = new AuthStorage(dbStore);
		await authStorage.set("commandcode-goat", { type: "api_key", key: "cmd-existing-key" });
		const fetchMock = async (): Promise<Response> =>
			modelsResponse('{"error":"invalid api key cmd-new-key","Authorization":"Bearer cmd-new-key"}', 401);

		await expect(
			authStorage.login("commandcode-goat", {
				onAuth: () => {},
				onPrompt: async () => "cmd-new-key",
				fetch: fetchMock as unknown as typeof fetch,
			}),
		).rejects.toThrow(/Command Code GOAT API key validation failed \(401\)/);

		expect(dbStore.getApiKey("commandcode-goat")).toBe("cmd-existing-key");
		expect(dbStore.listAuthCredentials("commandcode-goat")).toHaveLength(1);
	});
});
