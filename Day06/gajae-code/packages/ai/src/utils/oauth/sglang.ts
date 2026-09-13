/**
 * SGLang login flow.
 *
 * SGLang is commonly self-hosted with an OpenAI-compatible API at a local base URL.
 * Some deployments require a bearer token, others allow unauthenticated access.
 *
 * This flow stores an API-key-style credential used by `/login` and auth storage.
 */

import type { OAuthController, OAuthProvider } from "./types";

const PROVIDER_ID: OAuthProvider = "sglang";
const AUTH_URL = "https://docs.sglang.io/docs/advanced_features/server_arguments.html";
const DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:30000/v1";
/**
 * Login to SGLang.
 *
 * Opens SGLang OpenAI-compatible auth docs, prompts for a bearer token,
 * and returns a stored key value. Local no-auth servers need no login.
 */
export async function loginSglang(options: OAuthController): Promise<string> {
	if (!options.onPrompt) {
		throw new Error(`${PROVIDER_ID} login requires onPrompt callback`);
	}
	options.onAuth?.({
		url: AUTH_URL,
		instructions: `Paste the API key configured with SGLang's --api-key option. Local no-auth servers at ${DEFAULT_LOCAL_BASE_URL} are discovered automatically and do not need /login.`,
	});
	const apiKey = await options.onPrompt({
		message: "Paste your SGLang API key",
		placeholder: "SGLang API key",
		allowEmpty: false,
	});
	if (options.signal?.aborted) {
		throw new Error("Login cancelled");
	}
	const trimmed = apiKey.trim();
	if (!trimmed) {
		throw new Error("SGLang API key is required; local no-auth servers are discovered automatically");
	}
	return trimmed;
}
