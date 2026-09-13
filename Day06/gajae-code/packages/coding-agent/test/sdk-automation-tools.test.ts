import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentTool } from "@gajae-code/agent-core";
import { Settings } from "@gajae-code/coding-agent/config/settings";
import type { CustomTool } from "@gajae-code/coding-agent/extensibility/custom-tools/types";
import { type AutomationTools, createAgentSession } from "@gajae-code/coding-agent/sdk";
import { SessionManager } from "@gajae-code/coding-agent/session/session-manager";
import { z } from "zod/v4";

const automationSchema = z.object({
	action: z.enum(["ping", "wait"]),
});

function externalTool(
	name: "browser" | "computer",
	calls: string[],
	onWaitStarted?: (signal: AbortSignal | undefined) => void,
): AgentTool<typeof automationSchema> {
	return {
		name,
		label: `External ${name}`,
		description: `Host-owned ${name} transport`,
		parameters: automationSchema,
		strict: true,
		async execute(_toolCallId, params, signal) {
			calls.push(`${name}:${params.action}`);
			if (signal?.aborted) throw new Error(`${name} transport aborted`);
			if (params.action === "wait") {
				const pending = Promise.withResolvers<void>();
				const abort = () => {
					calls.push(`${name}:aborted`);
					pending.reject(new Error(`${name} transport aborted`));
				};
				signal?.addEventListener("abort", abort, { once: true });
				onWaitStarted?.(signal);
				try {
					await pending.promise;
				} finally {
					signal?.removeEventListener("abort", abort);
				}
			}
			return { content: [{ type: "text", text: `${name} external result` }] };
		},
	};
}

describe("createAgentSession external automation tools", () => {
	const tempDirs: string[] = [];

	afterEach(() => {
		for (const dir of tempDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
	});

	function sessionOptions() {
		const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "gjc-sdk-automation-"));
		tempDirs.push(tempDir);
		return {
			cwd: tempDir,
			agentDir: tempDir,
			sessionManager: SessionManager.inMemory(tempDir),
			settings: Settings.isolated({ "browser.enabled": false, "computer.alwaysOn": false }),
			disableExtensionDiscovery: true,
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			skipPythonPreflight: true,
			toolNames: ["browser", "computer"],
		};
	}

	it("materializes host browser and computer backends as built-ins and forwards cancellation", async () => {
		const calls: string[] = [];
		const waitStarted = Promise.withResolvers<AbortSignal | undefined>();
		const browser = externalTool("browser", calls, waitStarted.resolve);
		const computer = externalTool("computer", calls);
		const automationTools: AutomationTools = { browser, computer };
		const { session } = await createAgentSession({
			...sessionOptions(),
			automationTools,
		});

		try {
			const materializedBrowser = session.getToolByName("browser");
			const materializedComputer = session.getToolByName("computer");
			expect(materializedBrowser?.label).toBe("External browser");
			expect(materializedBrowser?.description).toBe("Host-owned browser transport");
			expect(materializedBrowser?.parameters).toBe(automationSchema);
			expect(materializedBrowser?.loadMode).toBe("discoverable");
			expect(materializedComputer?.label).toBe("External computer");

			const browserResult = await materializedBrowser!.execute("browser-ping", { action: "ping" });
			const computerResult = await materializedComputer!.execute("computer-ping", { action: "ping" });
			expect(browserResult.content).toEqual([{ type: "text", text: "browser external result" }]);
			expect(computerResult.content).toEqual([{ type: "text", text: "computer external result" }]);

			const controller = new AbortController();
			const pending = materializedBrowser!.execute("browser-wait", { action: "wait" }, controller.signal);
			expect(await waitStarted.promise).toBe(controller.signal);
			controller.abort();
			await expect(pending).rejects.toBe(controller.signal.reason);
			expect(calls).toEqual(["browser:ping", "computer:ping", "browser:wait", "browser:aborted"]);
		} finally {
			await session.dispose();
		}
	}, 60_000);

	it("keeps host automation in built-in discovery and activation", async () => {
		const calls: string[] = [];
		const { session } = await createAgentSession({
			...sessionOptions(),
			settings: Settings.isolated({
				"browser.enabled": false,
				"computer.alwaysOn": false,
				"tools.discoveryMode": "all",
			}),
			toolNames: undefined,
			automationTools: {
				browser: externalTool("browser", calls),
				computer: externalTool("computer", calls),
			},
		});

		try {
			expect(session.getActiveToolNames()).not.toContain("browser");
			expect(session.getActiveToolNames()).not.toContain("computer");
			const discoverable = session.getDiscoverableTools({ source: "builtin" });
			expect(discoverable).toEqual(
				expect.arrayContaining([
					expect.objectContaining({ name: "browser", source: "builtin" }),
					expect.objectContaining({ name: "computer", source: "builtin" }),
				]),
			);

			expect(await session.activateDiscoveredTools(["browser", "computer"])).toEqual(["browser", "computer"]);
			await session.getToolByName("computer")!.execute("computer-after-activation", { action: "ping" });
			expect(calls).toEqual(["computer:ping"]);
		} finally {
			await session.dispose();
		}
	}, 60_000);

	it("rejects a same-name custom tool before session construction", async () => {
		const calls: string[] = [];
		const customBrowser: CustomTool<typeof automationSchema> = {
			name: "browser",
			label: "Custom browser",
			description: "Must not overwrite the external built-in",
			parameters: automationSchema,
			async execute() {
				return { content: [{ type: "text", text: "custom" }] };
			},
		};

		await expect(
			createAgentSession({
				...sessionOptions(),
				automationTools: { browser: externalTool("browser", calls) },
				customTools: [customBrowser],
			}),
		).rejects.toThrow("SDK automation tools cannot collide with custom tools: browser");
		expect(calls).toEqual([]);
	});
});
