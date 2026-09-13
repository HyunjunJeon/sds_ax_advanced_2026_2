import type { FetchImpl, Model } from "../types";

const OPENCODE_GO_ORIGIN = "https://opencode.ai";
const OPENCODE_GO_OPENAI_BASE_PATH = "/zen/go/v1";
const OPENCODE_GO_ANTHROPIC_BASE_PATH = "/zen/go";

export type OpenCodeGoApiFamily = "openai" | "anthropic";

function expectedBasePath(apiFamily: OpenCodeGoApiFamily): string {
	return apiFamily === "openai" ? OPENCODE_GO_OPENAI_BASE_PATH : OPENCODE_GO_ANTHROPIC_BASE_PATH;
}

export function resolveOpenCodeGoSessionId(
	model: Pick<Model, "provider">,
	baseUrl: string | undefined,
	providerSessionId: string | undefined,
	apiFamily: OpenCodeGoApiFamily,
): string | undefined {
	if (model.provider !== "opencode-go" || !baseUrl || !providerSessionId) return undefined;
	try {
		const url = new URL(baseUrl);
		if (url.origin !== OPENCODE_GO_ORIGIN) return undefined;
		if (url.username !== "" || url.password !== "" || url.search !== "" || url.hash !== "") return undefined;
		if (url.pathname.replace(/\/+$/u, "") !== expectedBasePath(apiFamily)) return undefined;
		return providerSessionId;
	} catch {
		return undefined;
	}
}

export function applyOpenCodeGoSessionHeader(
	headers: Record<string, string>,
	sessionId: string | undefined,
): Record<string, string> {
	const normalizedHeaders = new Headers(headers);
	normalizedHeaders.delete("x-opencode-session");
	if (sessionId) normalizedHeaders.set("x-opencode-session", sessionId);
	return Object.fromEntries(normalizedHeaders.entries());
}

export function wrapFetchForOpenCodeGoSession(baseFetch: FetchImpl, sessionId: string | undefined): FetchImpl {
	return Object.assign(
		async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
			if (input instanceof Request) {
				const request = new Request(input, init);
				request.headers.delete("x-opencode-session");
				if (sessionId) request.headers.set("x-opencode-session", sessionId);
				return baseFetch(request);
			}
			const headers = new Headers(init?.headers);
			headers.delete("x-opencode-session");
			if (sessionId) headers.set("x-opencode-session", sessionId);
			return baseFetch(input, { ...init, headers });
		},
		baseFetch.preconnect ? { preconnect: baseFetch.preconnect } : {},
	);
}
