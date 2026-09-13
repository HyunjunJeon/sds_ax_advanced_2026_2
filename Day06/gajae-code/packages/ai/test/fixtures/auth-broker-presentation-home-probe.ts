// Reports which config root the auth-broker presentation sidecar lands under
// when the resolved home changes after this module loaded. Spawned as its own
// process with HOME=<first home>, so any import-time module state is captured
// under the first home; `os.homedir()` is then mocked to the second home, which
// is how a home becomes visible only after module load (#4761). The store is
// constructed *after* the switch with no explicit `presentationPath`, so its
// default sidecar must follow the config root that is current at construction
// (#4786) instead of the one captured when the module first loaded.
import { vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { getConfigRootDir } from "@gajae-code/utils";
import { AuthBrokerClient } from "../../src/auth-broker/client";
import { RemoteAuthCredentialStore } from "../../src/auth-broker/remote-store";
import { REMOTE_REFRESH_SENTINEL } from "../../src/auth-storage";

const secondHome = process.env.GJC_PROBE_SECOND_HOME;
if (!secondHome) throw new Error("GJC_PROBE_SECOND_HOME is required");

// The value the import-time module constant would have captured, derived here
// before the home changes so the two roots can be told apart in the report.
const importTimeConfigRoot = getConfigRootDir();

const now = Date.now();
const fetchImpl = (async (input: string | URL | Request) => {
	const url = String(input);
	if (url.endsWith("/v1/snapshot")) {
		return new Response(
			JSON.stringify({
				generation: 1,
				generatedAt: now,
				serverNowMs: now,
				refresher: { enabled: false, intervalMs: 0, skewMs: 0, nextSweepInMs: Number.MAX_SAFE_INTEGER },
				credentials: [
					{
						id: 1,
						provider: "anthropic",
						credential: {
							type: "oauth",
							access: "access-probe",
							refresh: REMOTE_REFRESH_SENTINEL,
							expires: now + 60_000,
							accountId: "account-probe",
							email: "probe@example.com",
						},
						identityKey: "probe-identity",
						rotatesInMs: 60_000,
					},
				],
			}),
			{ status: 200, headers: { "content-type": "application/json", etag: '"1"' } },
		);
	}
	return new Response("not found", { status: 404 });
}) as unknown as typeof fetch;

vi.spyOn(os, "homedir").mockReturnValue(secondHome);

const client = new AuthBrokerClient({ url: "http://broker.test", token: "probe-bearer", fetchImpl, maxRetries: 0 });
const store = new RemoteAuthCredentialStore({ client, streamSnapshots: false });
await store.refreshSnapshot();
store.recordCredentialHealth("anthropic", 1, {
	status: "failed",
	reason: "probe bearer rejected",
	checkedAt: now,
	retainUntil: now + 60_000,
});
await store.flushPresentationPersistence();

const sidecarUnder = (root: string): boolean => fs.existsSync(path.join(root, "auth-broker-presentations.json"));
const configRootAfterHomeChange = getConfigRootDir();
store.close();

console.log(
	JSON.stringify({
		importTimeConfigRoot,
		configRootAfterHomeChange,
		sidecarUnderImportTimeRoot: sidecarUnder(importTimeConfigRoot),
		sidecarUnderCurrentRoot: sidecarUnder(configRootAfterHomeChange),
	}),
);
