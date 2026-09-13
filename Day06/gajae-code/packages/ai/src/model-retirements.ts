// Retired from advertised catalogs because Cloud Code Assist rejects live calls.
// Keep the callable alternatives visible: gemini-3.1-pro-low:high and
// gemini-3.7-flash-tiered.
export const RETIRED_MODEL_KEYS = [
	"google-antigravity/gemini-3.1-pro-high",
	"google-antigravity/gemini-3.7-flash-high",
	"google-antigravity/gemini-3.7-flash-low",
	"google-antigravity/gemini-3.7-flash-medium",
] as const;

const RETIRED_MODEL_KEY_SET = new Set<string>(RETIRED_MODEL_KEYS);

export function isRetiredModelKey(provider: string, modelId: string): boolean {
	return RETIRED_MODEL_KEY_SET.has(`${provider}/${modelId}`);
}

export function isRetiredModel(model: { provider: string; id: string }): boolean {
	return isRetiredModelKey(model.provider, model.id);
}
