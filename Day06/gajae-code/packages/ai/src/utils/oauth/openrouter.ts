/** OpenRouter login flow (API key paste against https://openrouter.ai/api/v1). */
import { createApiKeyLogin } from "./api-key-login";

export const loginOpenRouter = createApiKeyLogin({
	providerLabel: "OpenRouter",
	authUrl: "https://openrouter.ai/keys",
	instructions: "Copy your API key from the OpenRouter dashboard",
	promptMessage: "Paste your OpenRouter API key",
	placeholder: "sk-or-v1-...",
	validation: {
		kind: "chat-completions",
		provider: "OpenRouter",
		baseUrl: "https://openrouter.ai/api/v1",
		model: "openrouter/auto",
	},
});
