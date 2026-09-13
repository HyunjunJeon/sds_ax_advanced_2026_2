import { describe, expect, test } from "bun:test";

import {
	deriveSessionLifecycleIdempotencyKey,
	type SessionLifecycleClient,
	type SessionLifecycleClientRequestOptions,
	SessionLifecycleService,
} from "../src/sdk/lifecycle/service";

const actor = { id: "42", namespace: "telegram:account-fingerprint" } as const;

function lifecycleClient(
	response: unknown = {
		ok: true,
		result: { sessionId: "broker-session-1", endpoint: { url: "ws://private", token: "secret" } },
	},
) {
	const calls: Array<{
		operation: string;
		input: Record<string, unknown>;
		options: SessionLifecycleClientRequestOptions;
	}> = [];
	const client: SessionLifecycleClient = {
		global: async (operation, input, options) => {
			calls.push({ operation, input, options });
			return response;
		},
	};
	return { service: new SessionLifecycleService(client), calls };
}

describe("SDK-owned Telegram lifecycle integration", () => {
	test("replays one Broker idempotency identity without a daemon control server", async () => {
		const { service, calls } = lifecycleClient();
		const target = { cwd: "/repo" };
		const first = await service.create({ actor, capability: "session.create", requestKey: "telegram:42:17", target });
		const second = await service.create({
			actor,
			capability: "session.create",
			requestKey: "telegram:42:17",
			target,
		});
		expect(calls).toHaveLength(2);
		expect(calls[0]?.options.idempotencyKey).toBe(calls[1]?.options.idempotencyKey);
		expect(calls[0]?.options.idempotencyKey).toBe(
			deriveSessionLifecycleIdempotencyKey(actor, "telegram:42:17", "session.create"),
		);
		expect(first).toEqual({ ok: true, operation: "session.create", result: { sessionId: "broker-session-1" } });
		expect(second).toEqual(first);
	});

	test("projects credential-free outcomes and fails closed on malformed Broker responses", async () => {
		const { service } = lifecycleClient({
			ok: true,
			result: {
				sessionId: "broker-session-2",
				endpoint: { url: "ws://private", token: "secret" },
				lifecycle: { tmuxSession: "gjc-private", sessionStateFile: "/private/state" },
			},
		});
		const outcome = await service.resume({
			actor,
			capability: "session.resume",
			requestKey: "telegram:42:18",
			target: { sessionId: "broker-session-2" },
		});
		expect(JSON.stringify(outcome)).not.toContain("ws://");
		expect(JSON.stringify(outcome)).not.toContain("secret");
		expect(JSON.stringify(outcome)).not.toContain("tmux");
		expect(JSON.stringify(outcome)).not.toContain("sessionStateFile");

		const malformed = lifecycleClient({ ok: true, result: { endpoint: { url: "ws://private", token: "secret" } } });
		const malformedOutcome = await malformed.service.create({
			actor,
			capability: "session.create",
			requestKey: "telegram:42:19",
			target: { cwd: "/repo" },
		});
		expect(malformedOutcome).toMatchObject({
			ok: false,
			certainty: "uncertain",
			error: { code: "malformed_response" },
		});
	});
});
