import { describe, expect, it } from "bun:test";
import { runHermesSetup } from "../src/setup/hermes-setup";

const ROOT = process.cwd();

function renderedConfig(result: Awaited<ReturnType<typeof runHermesSetup>>): string {
	const preview = result.previews?.find(entry => entry.path.endsWith(".yaml"));
	return preview?.content ?? "";
}

function operatorInstructions(result: Awaited<ReturnType<typeof runHermesSetup>>): string {
	const preview = result.previews?.find(entry => entry.path.includes("operator-instructions"));
	return preview?.content ?? "";
}

async function render(flags: Parameters<typeof runHermesSetup>[0] = {}) {
	return await runHermesSetup({ root: [ROOT], profile: "test", repo: "repo", json: true, ...flags });
}

describe("gjc setup hermes --require-worktree", () => {
	it("renders the requirement into the coordinator config", async () => {
		const result = await render({ requireWorktree: true });

		expect(renderedConfig(result)).toContain('GJC_COORDINATOR_MCP_REQUIRE_WORKTREE: "true"');
	});

	it("leaves the requirement out by default", async () => {
		const result = await render();

		// Turning this on for every generated setup would refuse creations from
		// existing controllers that do not name a worktree yet.
		expect(renderedConfig(result)).not.toContain("GJC_COORDINATOR_MCP_REQUIRE_WORKTREE");
	});

	it("still selects worktree mode so the requirement is satisfiable", async () => {
		const result = await render({ requireWorktree: true });

		expect(renderedConfig(result)).toContain("GJC_COORDINATOR_MCP_SESSION_COMMAND: gjc --worktree");
	});

	it("refuses the contradictory combination with --no-worktree", async () => {
		// Requiring a per-session worktree from an in-place coordinator would
		// refuse every session it ever creates, so fail at setup instead.
		await expect(render({ requireWorktree: true, noWorktree: true })).rejects.toThrow(/--require-worktree/);
	});

	it("refuses to mix the requirement with an explicit session command", async () => {
		await expect(render({ requireWorktree: true, sessionCommand: "gjc --worktree pinned" })).rejects.toThrow(
			/session-command/,
		);
	});

	it("teaches controllers to name a worktree per task", async () => {
		const instructions = operatorInstructions(await render());

		// The argument is useless if the generated guidance never mentions it.
		expect(instructions).toContain("worktree");
		expect(instructions).toContain("worktree_in_use");
		expect(instructions).toContain("worktree_required");
	});

	it("teaches controllers to filter list_sessions on registered", async () => {
		const instructions = operatorInstructions(await render());

		// A controller that reuses an unregistered entry gets not_found from every
		// session-scoped tool, so the marker has to reach the generated guidance.
		expect(instructions).toContain("registered: true");
		expect(instructions).toContain("not_found");
		expect(instructions).toContain("register_session");
	});
});
