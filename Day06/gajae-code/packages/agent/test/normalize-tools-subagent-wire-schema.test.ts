import { describe, expect, it } from "bun:test";
import { AppendOnlyContextManager } from "@gajae-code/agent-core/append-only-context";
import * as z from "zod/v4";
import { toolWireSchema } from "../../ai/src/utils/schema/wire";
import { normalizeTools } from "../src/agent-loop";
import type { AgentContext, AgentTool } from "../src/types";

// Issue #4837: canonical sub-sessions run with intent tracing off
// (`resolveIntentTracingEnabled` forces `_i` omission for task-spawned role
// agents). `normalizeTools` used to leave a live ZodObject as
// `tool.parameters` whenever no intent injection was needed, so any JSON
// round-trip downstream (append-only stable-prefix cloning, fork-seed
// snapshots) reduced the schema to a bare `{def, type}` object with no
// `properties`/`required`. Providers then advertised tools with no parameters
// and the model omitted required arguments — every subagent `bash` call failed
// with `command: Invalid input: expected string, received undefined` while the
// parent session's identical call worked (its intent injection converted Zod
// to the wire schema first).
//
// The regression contract: whatever the intent-tracing mode, a Zod-authored
// tool must reach the provider boundary as a wire JSON schema whose declared
// parameters survive a JSON round-trip.

const bashLikeSchema = z.object({
	command: z.string().describe("command to execute"),
	timeout: z.number().default(300).describe("timeout in seconds").optional(),
});

function makeBashLikeTool(): AgentTool {
	return {
		name: "bash",
		description: "bash tool",
		parameters: bashLikeSchema,
		async execute() {
			return { content: [{ type: "text", text: "ok" }] };
		},
	} as unknown as AgentTool;
}

function makeContext(tools: AgentTool[]): AgentContext {
	return { systemPrompt: ["sys"], messages: [], tools };
}

/** Wire conversion the provider layer performs on whatever it receives. */
function providerViewOfParameters(parameters: unknown): { properties?: Record<string, unknown>; required?: string[] } {
	return toolWireSchema({ name: "bash", description: "", parameters } as never);
}

describe("normalizeTools preserves zod parameter contracts on subagent paths (#4837)", () => {
	it("converts zod parameters to a wire schema even when intent injection is off", () => {
		const tools = normalizeTools([makeBashLikeTool()], false)!;
		const parameters = tools[0]!.parameters as Record<string, unknown>;

		// No live Zod instance may reach the provider boundary: it has no
		// JSON-serializable parameter contract.
		expect(typeof (parameters as { parse?: unknown }).parse).not.toBe("function");
		const view = providerViewOfParameters(parameters);
		expect(Object.keys(view.properties ?? {})).toContain("command");
		expect(view.required).toContain("command");
	});

	it("keeps declared parameters after an append-only stable-prefix round-trip (subagent intent mode)", () => {
		const context = makeContext([makeBashLikeTool()]);
		const manager = new AppendOnlyContextManager();
		const built = manager.build(context, { intentTracing: false });

		const tool = built.tools![0]!;
		const view = providerViewOfParameters(tool.parameters);
		expect(Object.keys(view.properties ?? {})).toContain("command");
		expect(view.required).toContain("command");
		// No `_i` leaks into a subagent schema: intent tracing stays off.
		expect(Object.keys(view.properties ?? {})).not.toContain("_i");
	});

	it("keeps declared parameters after an append-only stable-prefix round-trip (parent intent mode)", () => {
		const context = makeContext([makeBashLikeTool()]);
		const manager = new AppendOnlyContextManager();
		const built = manager.build(context, { intentTracing: true });

		const tool = built.tools![0]!;
		const view = providerViewOfParameters(tool.parameters);
		expect(Object.keys(view.properties ?? {})).toContain("command");
		expect(view.required).toContain("command");
		// Parent mode still injects the intent field.
		expect(Object.keys(view.properties ?? {})).toContain("_i");
	});

	it("still injects the intent field when intent tracing is on", () => {
		const tools = normalizeTools([makeBashLikeTool()], true)!;
		const view = providerViewOfParameters(tools[0]!.parameters);
		expect(Object.keys(view.properties ?? {})).toContain("_i");
		expect(Object.keys(view.properties ?? {})).toContain("command");
	});

	it("leaves non-zod (plain JSON schema) parameters untouched when intent injection is off", () => {
		const plainSchema = {
			type: "object",
			properties: { command: { type: "string" } },
			required: ["command"],
		};
		const tool = {
			name: "bash",
			description: "bash tool",
			parameters: plainSchema,
			async execute() {
				return { content: [] };
			},
		} as unknown as AgentTool;
		const tools = normalizeTools([tool], false)!;
		expect(tools[0]!.parameters).toBe(plainSchema);
	});

	it("does not inject intent for tools that omit it (function intent policy)", () => {
		const tool = makeBashLikeTool();
		(tool as { intent?: unknown }).intent = (args: { command?: string }) => `Run ${args.command ?? "command"}`;
		const tools = normalizeTools([tool], true)!;
		const view = providerViewOfParameters(tools[0]!.parameters);
		expect(Object.keys(view.properties ?? {})).not.toContain("_i");
		expect(Object.keys(view.properties ?? {})).toContain("command");
	});
});
