import { createApiKeyLogin } from "./api-key-login";

export const loginCommandCode = createApiKeyLogin({
	providerLabel: "Command Code GOAT",
	authUrl: "https://commandcode.ai/studio/#api-keys",
	instructions: "Create or copy your Command Code API key",
	promptMessage: "Paste your Command Code API key",
	placeholder: "cmd-...",
	validationProgressMessage: "Verifying Command Code inference entitlement...",
	validation: {
		kind: "chat-completions",
		provider: "Command Code GOAT",
		baseUrl: "https://api.commandcode.ai/provider/v1",
		model: "zai-org/GLM-5.3",
		requireInferenceResponse: true,
	},
});
