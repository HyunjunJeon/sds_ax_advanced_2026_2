import { describe, expect, it } from "bun:test";
import {
	BROKER_RUNTIME_CLOSE_CAPABILITY_FIELD,
	redactBrokerRuntimeCapabilities,
	redactObservedRequestContent,
} from "../src/sdk/host/control/runtime-gate";

/**
 * Diagnostic observers receive the request SHAPE, never caller content. A
 * spawned child's seed task travels through `turn.prompt` `input.text`, so an
 * observer that saw the raw frame would receive the task verbatim.
 */
describe("observed request redaction", () => {
	it("strips prompt text while preserving operation and correlation fields", () => {
		const frame = {
			type: "control_request",
			id: "req-1",
			operation: "turn.prompt",
			input: { text: "seed-task-plaintext", clientRef: "ref-1" },
		};
		const observed = redactObservedRequestContent(frame) as {
			input: Record<string, unknown>;
			operation: string;
			id: string;
		};
		expect(JSON.stringify(observed)).not.toContain("seed-task-plaintext");
		expect(observed.input.text).toBe("[redacted 19 chars]");
		expect(observed.input.clientRef).toBe("[redacted 5 chars]");
		expect(observed.operation).toBe("turn.prompt");
		expect(observed.id).toBe("[redacted 5 chars]");
	});

	it("is an allowlist: unlisted and nested fields cannot leak", () => {
		// The exact invariant fields plus a field this module has never heard of.
		const frame = {
			type: "control_request",
			operation: "session.spawn",
			input: {
				task: "seed-task-plaintext",
				masterCapability: "capability-plaintext",
				idempotencyKey: "idem-plaintext",
				somethingNobodyListed: "future-field-plaintext",
				nested: { task: "nested-task-plaintext" },
				clientRef: "ref-1",
			},
		};
		const observed = redactObservedRequestContent(frame) as { input: Record<string, unknown> };
		const rendered = JSON.stringify(observed);
		for (const secret of [
			"seed-task-plaintext",
			"capability-plaintext",
			"idem-plaintext",
			"future-field-plaintext",
			"nested-task-plaintext",
		]) {
			expect(rendered).not.toContain(secret);
		}
		expect(observed.input.clientRef).toBe("[redacted 5 chars]");
	});

	it("redacts every caller-content field without inventing absent ones", () => {
		const frame = {
			type: "control_request",
			operation: "skill.invoke",
			input: { name: "demo", args: { secret: "arg-content" }, answer: "answer-content", images: [1, 2] },
		};
		const observed = redactObservedRequestContent(frame) as { input: Record<string, unknown> };
		const rendered = JSON.stringify(observed);
		expect(rendered).not.toContain("arg-content");
		expect(rendered).not.toContain("answer-content");
		expect(observed.input.images).toBe("[redacted 2 items]");
		expect(observed.input.name).toBe("[redacted 4 chars]");
	});

	it("redacts caller content at FRAME level, in the production frame shape", () => {
		// SdkClient puts idempotencyKey at the TOP level, not inside input. An
		// input-only redactor left the real production shape untouched.
		const frame = {
			type: "control_request",
			id: "req-9",
			operation: "session.spawn",
			idempotencyKey: "idem-plaintext-secret",
			expectedRevision: "rev-plaintext",
			text: "top-level-task-secret",
			args: { nested: "top-level-nested-secret" },
			input: { task: "input-task-secret", clientRef: "ref-9" },
		};
		const observed = redactObservedRequestContent(frame);
		const rendered = JSON.stringify(observed);
		for (const secret of [
			"idem-plaintext-secret",
			"rev-plaintext",
			"top-level-task-secret",
			"top-level-nested-secret",
			"input-task-secret",
		]) {
			expect(rendered).not.toContain(secret);
		}
		// Structural routing survives so instrumentation stays useful.
		expect(observed.type).toBe("control_request");
		expect(observed.id).toBe("[redacted 5 chars]");
		expect(observed.operation).toBe("session.spawn");
		expect((observed.input as Record<string, unknown>).clientRef).toBe("[redacted 5 chars]");
	});

	it("keeps confirm only when it is the structural boolean", () => {
		const real = redactObservedRequestContent({
			type: "control_request",
			operation: "context.clear",
			confirm: true,
			input: {},
		});
		expect(real.confirm).toBe(true);
		// A caller-authored string under a structural key is content, not routing.
		const forged = redactObservedRequestContent({
			type: "control_request",
			operation: "context.clear",
			confirm: "CONFIRM_STRING_SECRET",
			input: {},
		});
		expect(JSON.stringify(forged)).not.toContain("CONFIRM_STRING_SECRET");
	});

	it("treats a non-record input as content rather than nothing to redact", () => {
		for (const payload of ["RAW_INPUT_SECRET", ["ARRAY_SECRET"], 42]) {
			const observed = redactObservedRequestContent({
				type: "query_request",
				id: "q-1",
				query: "turn.result",
				input: payload,
			});
			const rendered = JSON.stringify(observed);
			expect(rendered).not.toContain("RAW_INPUT_SECRET");
			expect(rendered).not.toContain("ARRAY_SECRET");
			expect(observed.query).toBe("turn.result");
		}
	});

	it("leaves content-free frames untouched and composes with capability redaction", () => {
		const plain = { type: "control_request", operation: "turn.abort", input: { mode: "turn" } };
		// Structural-only frames survive by value (the frame is rebuilt, not aliased).
		expect(redactObservedRequestContent(plain)).toEqual(plain);
		const closeFrame = {
			type: "control_request",
			operation: "session.close",
			input: { sessionId: "child", [BROKER_RUNTIME_CLOSE_CAPABILITY_FIELD]: "broker-only" },
		};
		const observed = redactObservedRequestContent(redactBrokerRuntimeCapabilities(closeFrame)) as {
			input: Record<string, unknown>;
		};
		expect(JSON.stringify(observed)).not.toContain("broker-only");
		expect(observed.input.sessionId).toBe("[redacted 5 chars]");
	});

	it("does not trust allowlisted scalar names or preserve caller-chosen values", () => {
		const frame = {
			type: "control_request",
			id: "id-secret",
			operation: "operation-secret",
			query: "query-secret",
			confirm: "confirm-secret",
			input: {
				clientRef: "client-ref-secret",
				commandId: "command-secret",
				turnId: "turn-secret",
				sessionId: "session-secret",
				expectedSessionId: "expected-session-secret",
				mode: "mode-secret",
				scope: "scope-secret",
				kind: "kind-secret",
				level: "level-secret",
				on: "on-secret",
				confirm: "input-confirm-secret",
				name: "name-secret",
				op: "op-secret",
				id: "input-id-secret",
			},
		};
		const rendered = JSON.stringify(redactObservedRequestContent(frame));
		for (const secret of [
			"id-secret",
			"operation-secret",
			"query-secret",
			"confirm-secret",
			"client-ref-secret",
			"command-secret",
			"turn-secret",
			"session-secret",
			"expected-session-secret",
			"mode-secret",
			"scope-secret",
			"kind-secret",
			"level-secret",
			"on-secret",
			"input-confirm-secret",
			"name-secret",
			"op-secret",
			"input-id-secret",
		]) {
			expect(rendered).not.toContain(secret);
		}
	});

	it("keeps only bounded protocol literals in scalar telemetry", () => {
		const observed = redactObservedRequestContent({
			type: "control_request",
			operation: "turn.abort",
			confirm: true,
			input: { mode: "terminal", scope: "owned", kind: "prompt", level: "high", on: false },
		});
		expect(observed).toMatchObject({
			type: "control_request",
			operation: "turn.abort",
			confirm: true,
			input: { mode: "terminal", scope: "owned", kind: "prompt", level: "high", on: false },
		});
	});

	it("bounds marker diagnostics for oversized caller values", () => {
		const observed = redactObservedRequestContent({
			type: "control_request",
			operation: "turn.prompt",
			input: { text: "x".repeat(100_000) },
		});
		expect((observed.input as Record<string, unknown>).text).toBe("[redacted 4096+ chars]");
		expect(JSON.stringify(observed).length).toBeLessThan(256);
	});
});
