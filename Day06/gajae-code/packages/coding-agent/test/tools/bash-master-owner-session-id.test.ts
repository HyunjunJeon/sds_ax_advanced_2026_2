import { afterEach, describe, expect, it, vi } from "bun:test";
import type { ToolSession } from "../../src/tools";
import { BashTool } from "../../src/tools/bash";
import { stubBashExecutorSettings } from "../helpers/tool-session-settings";

afterEach(() => {
	vi.restoreAllMocks();
});

/**
 * Issue #5374: `GJC_SESSION_ID` must mean "this session's own id" on the
 * bash tool-env path, and master ownership must travel under its own distinct
 * variable (`GJC_MASTER_OWNER_SESSION_ID`).
 *
 * The direct `gjc sdk spawn` dispatch env previously read
 * `GJC_SESSION_ID: resolvedEnv?.GJC_MASTER_OWNER_SESSION_ID ?? own id`, which
 * overloaded one name with two meanings (master identity vs. own identity).
 */
function createSession(sessionId: string, ownerSessionId?: string): ToolSession {
	return {
		cwd: process.cwd(),
		getSessionFile: () => null,
		getSessionId: () => sessionId,
		getMasterBashCapability: () => "master-capability-fixture",
		...(ownerSessionId === undefined
			? { getMasterOwnerSessionId: () => undefined }
			: { getMasterOwnerSessionId: () => ownerSessionId }),
		settings: {
			has: () => false,
			get: () => undefined,
			getBashInterceptorRules: () => [],
			...stubBashExecutorSettings,
		},
	} as unknown as ToolSession;
}

function echoSessionEnv(): string {
	return 'printf "own=%s owner=%s" "$GJC_SESSION_ID" "$GJC_MASTER_OWNER_SESSION_ID"';
}

function textOf(result: unknown): string {
	if (typeof result === "string") return result;
	const content = (result as { content?: { type: string; text?: string }[] }).content ?? [];
	return content.find(block => block.type === "text")?.text ?? "";
}

describe("issue #5374: session identity on the bash tool-env path", () => {
	it("a master-owned child exposes its own id in GJC_SESSION_ID", async () => {
		const result = await new BashTool(createSession("child-session", "master-owner")).execute("call", {
			command: echoSessionEnv(),
		});
		expect(textOf(result)).toContain("own=child-session");
	});

	it("master ownership travels under GJC_MASTER_OWNER_SESSION_ID", async () => {
		const result = await new BashTool(createSession("child-session", "master-owner")).execute("call", {
			command: echoSessionEnv(),
		});
		expect(textOf(result)).toContain("owner=master-owner");
	});

	it("a master session exposes its own id", async () => {
		const result = await new BashTool(createSession("master-owner", "master-owner")).execute("call", {
			command: echoSessionEnv(),
		});
		expect(textOf(result)).toContain("own=master-owner");
	});
});
