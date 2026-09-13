import { describe, expect, it, vi } from "bun:test";
import type { ExtensionRunner } from "@gajae-code/coding-agent/extensibility/extensions/runner";
import type { RegisteredTool } from "@gajae-code/coding-agent/extensibility/extensions/types";
import { RegisteredToolAdapter } from "@gajae-code/coding-agent/extensibility/extensions/wrapper";

function createRegisteredTool(overrides: Partial<RegisteredTool["definition"]> = {}): RegisteredTool {
	return {
		extensionPath: "/test/extension.ts",
		definition: {
			name: "test-tool",
			label: "Test tool",
			description: "Test tool definition",
			parameters: {} as never,
			execute: async () => ({ content: [{ type: "text", text: "ok" }], details: {} }),
			...overrides,
		},
	};
}

const runner = { createContext: () => ({}) } as ExtensionRunner;

describe("RegisteredToolAdapter", () => {
	it("installs renderer adapters before proxying renderer properties", () => {
		const renderCall = vi.fn(() => ({}) as never);
		const renderResult = vi.fn(() => ({}) as never);
		const adapter = new RegisteredToolAdapter(createRegisteredTool({ renderCall, renderResult }), runner);
		const args = { value: 1 };
		const result = { content: [{ type: "text" as const, text: "ok" }], details: {} };
		const options = { expanded: true, isPartial: false, spinnerFrame: 0, ignored: true };
		const theme = {} as never;

		const renderCallDescriptor = Object.getOwnPropertyDescriptor(adapter, "renderCall");
		const renderResultDescriptor = Object.getOwnPropertyDescriptor(adapter, "renderResult");
		expect(renderCallDescriptor).toMatchObject({ value: expect.any(Function), writable: true });
		expect(renderResultDescriptor).toMatchObject({ value: expect.any(Function), writable: true });
		expect(() => adapter.renderCall?.(args, options, theme)).not.toThrow();
		expect(() => adapter.renderResult?.(result, options, theme, args)).not.toThrow();
		expect(renderCall).toHaveBeenCalledWith(args, options, theme);
		expect(renderResult).toHaveBeenCalledWith(
			result,
			{ expanded: true, isPartial: false, spinnerFrame: 0 },
			theme,
			args,
		);
	});

	it("does not expose renderer methods when the definition omits them", () => {
		const adapter = new RegisteredToolAdapter(createRegisteredTool(), runner);

		expect(adapter.renderCall).toBeUndefined();
		expect(adapter.renderResult).toBeUndefined();
	});
});
