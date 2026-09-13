import { afterEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { getBundledModel } from "@gajae-code/ai";
import { ModelRegistry } from "@gajae-code/coding-agent/config/model-registry";
import { Settings } from "@gajae-code/coding-agent/config/settings";
import { createAgentSession } from "@gajae-code/coding-agent/sdk";
import { AuthStorage } from "@gajae-code/coding-agent/session/auth-storage";
import { SessionManager } from "@gajae-code/coding-agent/session/session-manager";
import { TempDir } from "@gajae-code/utils";

const originalFetch = global.fetch;
const originalOpenAIBaseUrl = Bun.env.OPENAI_BASE_URL;

let tempDir: TempDir | undefined;
let authStorage: AuthStorage | undefined;
let session: Awaited<ReturnType<typeof createAgentSession>>["session"] | undefined;

function restoreOpenAIBaseUrl(): void {
	if (originalOpenAIBaseUrl === undefined) delete Bun.env.OPENAI_BASE_URL;
	else Bun.env.OPENAI_BASE_URL = originalOpenAIBaseUrl;
}

afterEach(async () => {
	vi.restoreAllMocks();
	global.fetch = originalFetch;
	restoreOpenAIBaseUrl();
	await session?.dispose();
	session = undefined;
	authStorage?.close();
	authStorage = undefined;
	await tempDir?.remove();
	tempDir = undefined;
});

describe("direct SDK image tool settings", () => {
	it("passes the owning credential scope through a wrapped image tool", async () => {
		const imageModel = getBundledModel("openai", "gpt-image-2");
		if (!imageModel) throw new Error("Expected bundled OpenAI image model");

		tempDir = TempDir.createSync("@sdk-image-settings-");
		const cwd = tempDir.path();
		authStorage = await AuthStorage.create(path.join(cwd, "auth.db"));
		const modelRegistry = new ModelRegistry(authStorage);
		vi.spyOn(modelRegistry, "getAvailable").mockReturnValue([imageModel]);
		const credentialSessionId = "sdk-image-credential-scope";
		const credentialScopes: Array<string | undefined> = [];
		vi.spyOn(modelRegistry, "getApiKey").mockImplementation(async (_model, sessionId) => {
			credentialScopes.push(sessionId);
			return sessionId === credentialSessionId ? "sdk-image-key" : undefined;
		});

		const response = new Response(
			JSON.stringify({
				output: [
					{
						type: "image_generation_call",
						result: Buffer.from("fake-webp").toString("base64"),
						status: "completed",
					},
				],
			}),
			{ status: 200, headers: { "content-type": "application/json" } },
		);
		global.fetch = vi.fn(async () => response.clone()) as unknown as typeof fetch;
		delete Bun.env.OPENAI_BASE_URL;

		const created = await createAgentSession({
			cwd,
			agentDir: cwd,
			authStorage,
			modelRegistry,
			credentialSessionId,
			settings: Settings.isolated({ modelRoles: { image: `${imageModel.provider}/${imageModel.id}` } }),
			model: imageModel,
			sessionManager: SessionManager.inMemory(cwd),
			disableExtensionDiscovery: true,
			extensions: [],
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
		});
		session = created.session;

		const imageTool = session.agent.state.tools.find(tool => tool.name === "generate_image");
		expect(imageTool).toBeDefined();
		const result = await imageTool!.execute("sdk-image-call", { subject: "a cat" });
		for (const imagePath of result.details?.imagePaths ?? []) await fs.rm(imagePath, { force: true });

		expect(result.details?.provider).toBe("openai");
		expect(credentialScopes.length).toBeGreaterThan(0);
		expect(credentialScopes.every(scope => scope === credentialSessionId)).toBe(true);
	}, 30_000);
});
