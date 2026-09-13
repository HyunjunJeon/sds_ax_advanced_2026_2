import * as fs from "node:fs";

/** Test-only inspection of a session-owned SDK discovery record. */
export function readTestSdkEndpoint(file: string): { url: string; token: string; pid?: number; stale?: boolean } {
	const raw = JSON.parse(fs.readFileSync(file, "utf8")) as {
		url?: unknown;
		token?: unknown;
		pid?: unknown;
		stale?: unknown;
	};
	if (typeof raw.url !== "string" || typeof raw.token !== "string")
		throw new Error(`invalid SDK endpoint file: ${file}`);
	return {
		url: raw.url,
		token: raw.token,
		pid: typeof raw.pid === "number" ? raw.pid : undefined,
		stale: raw.stale === true,
	};
}
