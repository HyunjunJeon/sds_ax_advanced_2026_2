import { afterEach, describe, expect, it } from "bun:test";
import { getBundledModel } from "../src/models";
import { streamOpenAICompletions } from "../src/providers/openai-completions";
import { detectOpenAICompat, resolveOpenAICompat } from "../src/providers/openai-completions-compat";
import { completeSimple, streamSimple } from "../src/stream";
import type { Context, Model } from "../src/types";

const originalFetch = global.fetch;

afterEach(() => {
	global.fetch = originalFetch;
});

// ── Wire-capture fetch ───────────────────────────────────────────────────────
// The session-header injection lives in createClient(), which sets the OpenAI
// SDK client's `defaultHeaders`. The SDK then merges those into the real fetch
// `init.headers`. Capturing the outgoing headers here therefore proves the
// header is actually transmitted on the wire, not just stored on an object.

interface CapturedRequest {
	url: string;
	headers: Record<string, string>;
}

function createCapturingFetch(captured: CapturedRequest[]): typeof fetch {
	async function capturingFetch(input: string | URL | Request, init?: RequestInit): Promise<Response> {
		const headers: Record<string, string> = {};
		// Headers can arrive as a Headers instance, a plain record, or on a Request.
		const merge = (h: ConstructorParameters<typeof Headers>[0] | undefined): void => {
			if (!h) return;
			new Headers(h).forEach((value, key) => {
				headers[key.toLowerCase()] = value;
			});
		};
		if (input instanceof Request) merge(input.headers);
		merge(init?.headers);
		captured.push({
			url: input instanceof Request ? input.url : String(input),
			headers,
		});
		const payload = `data: ${JSON.stringify({
			id: "chatcmpl-test",
			object: "chat.completion.chunk",
			created: 0,
			model: "claude-opus-4-8",
			choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
		})}\n\ndata: [DONE]\n\n`;
		return new Response(payload, {
			status: 200,
			headers: { "content-type": "text/event-stream" },
		});
	}
	return Object.assign(capturingFetch, { preconnect: originalFetch.preconnect });
}

function baseContext(): Context {
	return {
		messages: [{ role: "user", content: "hello", timestamp: Date.now() }],
	};
}

// A custom OpenAI-compatible relay model (NOT first-party OpenAI).
function relayModel(compat?: Model<"openai-completions">["compat"]): Model<"openai-completions"> {
	return {
		...getBundledModel("openai", "gpt-4o-mini"),
		provider: "relay",
		id: "claude-opus-4-8",
		api: "openai-completions",
		baseUrl: "https://api.relay.example.com/v1",
		...(compat ? { compat } : {}),
	};
}

function openCodeGoModel(): Model<"openai-completions"> {
	const model = getBundledModel("opencode-go", "kimi-k2.5") as Model<"openai-completions">;
	return {
		...model,
		headers: model.headers ? { ...model.headers } : undefined,
		compat: model.compat ? { ...model.compat } : undefined,
		requestTransform: model.requestTransform
			? {
					...model.requestTransform,
					stripHeaders: model.requestTransform.stripHeaders ? [...model.requestTransform.stripHeaders] : undefined,
					setHeaders: model.requestTransform.setHeaders ? { ...model.requestTransform.setHeaders } : undefined,
					extraBody: model.requestTransform.extraBody ? { ...model.requestTransform.extraBody } : undefined,
				}
			: undefined,
	};
}

function openCodeZenModel(): Model<"openai-completions"> {
	return {
		...openCodeGoModel(),
		provider: "opencode-zen",
		baseUrl: "https://opencode.ai/zen/v1",
	};
}

// ── compat resolution ────────────────────────────────────────────────────────

describe("sendSessionHeaders compat resolution", () => {
	it("defaults to false for a custom relay base URL", () => {
		const detected = detectOpenAICompat(relayModel());
		expect(detected.sendSessionHeaders).toBe(false);
	});

	it("defaults to false for first-party OpenAI", () => {
		const model: Model<"openai-completions"> = {
			...getBundledModel("openai", "gpt-4o-mini"),
			api: "openai-completions",
		};
		expect(detectOpenAICompat(model).sendSessionHeaders).toBe(false);
	});

	it("is enabled when models.yml compat opts in", () => {
		const resolved = resolveOpenAICompat(relayModel({ sendSessionHeaders: true }));
		expect(resolved.sendSessionHeaders).toBe(true);
	});

	it("stays false when compat is present but omits the flag", () => {
		const resolved = resolveOpenAICompat(relayModel({ supportsDeveloperRole: false }));
		expect(resolved.sendSessionHeaders).toBe(false);
	});
});

// ── end-to-end wire transmission ─────────────────────────────────────────────

describe("session headers on the wire (streamOpenAICompletions)", () => {
	it("preserves provider conversation authority through streamSimple dispatch", async () => {
		const captured: CapturedRequest[] = [];
		global.fetch = createCapturingFetch(captured);
		await streamSimple(openCodeGoModel(), baseContext(), {
			apiKey: "test-key",
			sessionId: "generic-cache-key",
			providerSessionId: "opaque-stream-session",
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers["x-opencode-session"]).toBe("opaque-stream-session");
	});

	it("preserves provider conversation authority through completeSimple maintenance dispatch", async () => {
		const captured: CapturedRequest[] = [];
		global.fetch = createCapturingFetch(captured);
		const result = await completeSimple(openCodeGoModel(), baseContext(), {
			apiKey: "test-key",
			sessionId: "generic-cache-key",
			providerSessionId: "opaque-maintenance-session",
		});

		expect(result.stopReason).toBe("stop");
		expect(captured).toHaveLength(1);
		expect(captured[0].headers["x-opencode-session"]).toBe("opaque-maintenance-session");
	});

	it("sends OpenCode Go's required conversation header without changing auth or user-agent", async () => {
		const captured: CapturedRequest[] = [];
		await streamOpenAICompletions(openCodeGoModel(), baseContext(), {
			apiKey: "test-key",
			sessionId: "cache-affinity-must-not-win",
			providerSessionId: "01990dc9-e005-7000-8000-000000000001",
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers["x-opencode-session"]).toBe("01990dc9-e005-7000-8000-000000000001");
		expect(captured[0].headers.authorization).toBe("Bearer test-key");
		expect(captured[0].headers["user-agent"]).toBe("OpenAI/JS 6.49.0");
	});

	it("keeps the OpenCode Go session header stable across turns", async () => {
		const captured: CapturedRequest[] = [];
		const fetch = createCapturingFetch(captured);
		const sessionId = "01990dc9-e005-7000-8000-000000000002";

		await streamOpenAICompletions(openCodeGoModel(), baseContext(), {
			apiKey: "first-credential",
			providerSessionId: sessionId,
			fetch,
		}).result();
		await streamOpenAICompletions(
			openCodeGoModel(),
			{ messages: [{ role: "user", content: "different turn and prompt", timestamp: Date.now() }] },
			{
				apiKey: "rotated-credential",
				providerSessionId: sessionId,
				fetch,
			},
		).result();

		expect(captured.map(request => request.headers["x-opencode-session"])).toEqual([sessionId, sessionId]);
	});

	it("protects the OpenCode Go session identity from header overrides and request transforms", async () => {
		const captured: CapturedRequest[] = [];
		const model = openCodeGoModel();
		model.headers = { "X-OpenCode-Session": "model-override" };
		model.requestTransform = {
			stripHeaders: ["x-opencode-session"],
			setHeaders: { "X-OpenCode-Session": "transform-override" },
		};
		const sessionId = "01990dc9-e005-7000-8000-000000000008";

		await streamOpenAICompletions(model, baseContext(), {
			apiKey: "test-key",
			providerSessionId: sessionId,
			headers: { "X-OPENCODE-SESSION": "options-override" },
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers["x-opencode-session"]).toBe(sessionId);
	});

	it("uses distinct OpenCode Go headers for distinct conversations", async () => {
		const captured: CapturedRequest[] = [];
		const fetch = createCapturingFetch(captured);
		const firstSessionId = "01990dc9-e005-7000-8000-000000000003";
		const secondSessionId = "01990dc9-e005-7000-8000-000000000004";

		await streamOpenAICompletions(openCodeGoModel(), baseContext(), {
			apiKey: "test-key",
			providerSessionId: firstSessionId,
			fetch,
		}).result();
		await streamOpenAICompletions(openCodeGoModel(), baseContext(), {
			apiKey: "test-key",
			providerSessionId: secondSessionId,
			fetch,
		}).result();

		expect(captured.map(request => request.headers["x-opencode-session"])).toEqual([firstSessionId, secondSessionId]);
		expect(firstSessionId).not.toBe(secondSessionId);
	});

	it("reuses the OpenCode Go session header for OpenAI SDK retries", async () => {
		const captured: CapturedRequest[] = [];
		let attempt = 0;
		const retryingFetch = Object.assign(
			async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
				const headers: Record<string, string> = {};
				new Headers(input instanceof Request ? input.headers : undefined).forEach((value, key) => {
					headers[key.toLowerCase()] = value;
				});
				new Headers(init?.headers).forEach((value, key) => {
					headers[key.toLowerCase()] = value;
				});
				captured.push({ url: input instanceof Request ? input.url : String(input), headers });
				attempt += 1;
				if (attempt === 1) {
					return Response.json({ error: { message: "retry", type: "server_error" } }, { status: 500 });
				}
				return createCapturingFetch([])(input, init);
			},
			{ preconnect: originalFetch.preconnect },
		);
		const sessionId = "01990dc9-e005-7000-8000-000000000005";

		await streamOpenAICompletions(openCodeGoModel(), baseContext(), {
			apiKey: "test-key",
			providerSessionId: sessionId,
			requestMaxRetries: 1,
			fetch: retryingFetch,
		}).result();

		expect(captured).toHaveLength(2);
		expect(captured.map(request => request.headers["x-opencode-session"])).toEqual([sessionId, sessionId]);
	});

	it("reuses the OpenCode Go session header for replay-safe stream retries", async () => {
		const captured: CapturedRequest[] = [];
		let attempt = 0;
		const replayingFetch = Object.assign(
			async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
				const headers: Record<string, string> = {};
				new Headers(input instanceof Request ? input.headers : undefined).forEach((value, key) => {
					headers[key.toLowerCase()] = value;
				});
				new Headers(init?.headers).forEach((value, key) => {
					headers[key.toLowerCase()] = value;
				});
				captured.push({ url: input instanceof Request ? input.url : String(input), headers });
				attempt += 1;
				const finishReason = attempt === 1 ? "network_error" : "stop";
				const payload = `data: ${JSON.stringify({
					id: `chatcmpl-${attempt}`,
					object: "chat.completion.chunk",
					created: 0,
					model: "kimi-k2.5",
					choices: [{ index: 0, delta: {}, finish_reason: finishReason }],
				})}\n\ndata: [DONE]\n\n`;
				return new Response(payload, {
					status: 200,
					headers: { "content-type": "text/event-stream" },
				});
			},
			{ preconnect: originalFetch.preconnect },
		);
		const sessionId = "01990dc9-e005-7000-8000-000000000009";

		await streamOpenAICompletions(openCodeGoModel(), baseContext(), {
			apiKey: "test-key",
			providerSessionId: sessionId,
			requestMaxRetries: 0,
			streamMaxRetries: 1,
			providerRetryWait: async () => {},
			fetch: replayingFetch,
		}).result();

		expect(captured).toHaveLength(2);
		expect(captured.map(request => request.headers["x-opencode-session"])).toEqual([sessionId, sessionId]);
	});

	it("strips the reserved OpenCode Go header from every unauthorized route", async () => {
		const captured: CapturedRequest[] = [];
		const fetch = createCapturingFetch(captured);
		const zen = openCodeZenModel();
		const relay = relayModel();
		const mislabeledRelay = openCodeGoModel();
		mislabeledRelay.baseUrl = "https://api.relay.example.com/v1";
		const sameOriginSuffix = openCodeGoModel();
		sameOriginSuffix.baseUrl = "https://opencode.ai/zen/go/v1/relay";
		const queryRouted = openCodeGoModel();
		queryRouted.baseUrl = "https://opencode.ai/zen/go/v1?route=relay";
		for (const model of [zen, relay, mislabeledRelay, sameOriginSuffix, queryRouted]) {
			model.headers = { "X-OpenCode-Session": "model-injection" };
			model.requestTransform = { setHeaders: { "x-opencode-session": "transform-injection" } };
		}

		await streamOpenAICompletions(zen, baseContext(), {
			apiKey: "test-key",
			providerSessionId: "01990dc9-e005-7000-8000-000000000006",
			headers: { "X-OPENCODE-SESSION": "options-injection" },
			fetch,
		}).result();
		await streamOpenAICompletions(relay, baseContext(), {
			apiKey: "test-key",
			providerSessionId: "01990dc9-e005-7000-8000-000000000007",
			headers: { "X-OPENCODE-SESSION": "options-injection" },
			fetch,
		}).result();
		await streamOpenAICompletions(mislabeledRelay, baseContext(), {
			apiKey: "test-key",
			providerSessionId: "01990dc9-e005-7000-8000-000000000010",
			headers: { "X-OPENCODE-SESSION": "options-injection" },
			fetch,
		}).result();
		await streamOpenAICompletions(sameOriginSuffix, baseContext(), {
			apiKey: "test-key",
			providerSessionId: "01990dc9-e005-7000-8000-000000000011",
			headers: { "X-OPENCODE-SESSION": "options-injection" },
			fetch,
		}).result();
		await streamOpenAICompletions(queryRouted, baseContext(), {
			apiKey: "test-key",
			providerSessionId: "01990dc9-e005-7000-8000-000000000012",
			headers: { "X-OPENCODE-SESSION": "options-injection" },
			fetch,
		}).result();

		expect(captured).toHaveLength(5);
		expect(captured.every(request => request.headers["x-opencode-session"] === undefined)).toBe(true);
		expect(captured[4].url).toContain("route=relay");
	});

	it("does not treat a generic cache session ID as OpenCode Go conversation authority", async () => {
		const captured: CapturedRequest[] = [];
		const model = openCodeGoModel();
		model.headers = { "X-OpenCode-Session": "model-injection" };
		model.requestTransform = { setHeaders: { "x-opencode-session": "transform-injection" } };

		await streamOpenAICompletions(model, baseContext(), {
			apiKey: "test-key",
			sessionId: "prompt-derived-cache-key",
			headers: { "X-OPENCODE-SESSION": "options-injection" },
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers["x-opencode-session"]).toBeUndefined();
	});

	it("transmits session_id + x-session-id when flag is ON and sessionId is present", async () => {
		const captured: CapturedRequest[] = [];
		await streamOpenAICompletions(relayModel({ sendSessionHeaders: true }), baseContext(), {
			apiKey: "test-key",
			sessionId: "conv-abc-123",
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers.session_id).toBe("conv-abc-123");
		expect(captured[0].headers["x-session-id"]).toBe("conv-abc-123");
	});

	it("omits session headers when flag is OFF (default behavior unchanged)", async () => {
		const captured: CapturedRequest[] = [];
		await streamOpenAICompletions(relayModel(), baseContext(), {
			apiKey: "test-key",
			sessionId: "conv-abc-123",
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers.session_id).toBeUndefined();
		expect(captured[0].headers["x-session-id"]).toBeUndefined();
	});

	it("omits session headers when flag is ON but sessionId is empty", async () => {
		const captured: CapturedRequest[] = [];
		await streamOpenAICompletions(relayModel({ sendSessionHeaders: true }), baseContext(), {
			apiKey: "test-key",
			sessionId: "",
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers.session_id).toBeUndefined();
		expect(captured[0].headers["x-session-id"]).toBeUndefined();
	});

	it("does not overwrite a caller-supplied session_id header (options.headers precedence)", async () => {
		const captured: CapturedRequest[] = [];
		await streamOpenAICompletions(relayModel({ sendSessionHeaders: true }), baseContext(), {
			apiKey: "test-key",
			sessionId: "derived-session",
			headers: { session_id: "user-pinned" },
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		// User-supplied header wins; the auto-injected x-session-id still fills the gap.
		expect(captured[0].headers.session_id).toBe("user-pinned");
		expect(captured[0].headers["x-session-id"]).toBe("derived-session");
	});

	it("does not overwrite a session_id baked into model.headers (models.yml headers precedence)", async () => {
		const captured: CapturedRequest[] = [];
		const model = relayModel({ sendSessionHeaders: true });
		model.headers = { session_id: "config-pinned" };
		await streamOpenAICompletions(model, baseContext(), {
			apiKey: "test-key",
			sessionId: "derived-session",
			fetch: createCapturingFetch(captured),
		}).result();

		expect(captured).toHaveLength(1);
		expect(captured[0].headers.session_id).toBe("config-pinned");
		expect(captured[0].headers["x-session-id"]).toBe("derived-session");
	});
});
