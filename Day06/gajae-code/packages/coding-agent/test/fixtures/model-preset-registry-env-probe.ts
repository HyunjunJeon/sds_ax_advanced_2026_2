import {
	DEFAULT_MODEL_PRESET_REGISTRY_URL,
	refreshModelPresetRegistry,
} from "@gajae-code/coding-agent/config/model-preset-registry";

let requestedUrl: string | null = null;
const dependencyManifestUrl = process.argv[2] || undefined;
let status: string | null = null;
let error: string | null = null;

try {
	const result = await refreshModelPresetRegistry({
		manifestUrl: dependencyManifestUrl,
		fetch: (async input => {
			requestedUrl = String(input);
			return new Response(null, { status: 304 });
		}) as typeof fetch,
	});
	status = result.status;
} catch (cause) {
	error = cause instanceof Error ? cause.message : String(cause);
}

console.log(JSON.stringify({ requestedUrl, status, error, defaultUrl: DEFAULT_MODEL_PRESET_REGISTRY_URL }));
