import { expect, test } from "bun:test";
import { NotificationServer } from "../../natives/native/index.js";
import { ACP_SESSION_RECONNECT, AcpSdkAdapter } from "../src/sdk/acp";
import { SdkClient } from "../src/sdk/client";
import { SessionSdkHost } from "../src/sdk/host";

test("SDK-RPC-provider-conflict: real ACP clients race atomically for one provider lease", async () => {
	const server = new NotificationServer(`acp-race-${Date.now()}`, "token", `/tmp/acp-race-${Date.now()}`, true);
	let onFrame: ((connectionId: string, frame: Record<string, unknown>) => void) | undefined;
	const installedDefinitions = new Map<string, unknown>();

	const host = new SessionSdkHost({
		sessionId: "s",
		stateRoot: "/tmp",
		token: "token",
		sendFrame: (connectionId, frame) => {
			server.sendTo(connectionId, JSON.stringify(frame));
			return "written";
		},
		onFrame: handler => {
			onFrame = handler;
			return () => {
				onFrame = undefined;
			};
		},
		installProviderDefinitions: (capability, definitions) => installedDefinitions.set(capability, definitions),
	});
	server.onSdkFrame((_error, event) => {
		if (event) onFrame?.(event.connectionId, JSON.parse(event.json) as Record<string, unknown>);
	});
	server.onConnectionClose((_error, connectionId) => {
		if (connectionId) host.handleDisconnect(connectionId);
	});
	await host.start();
	const endpoint = await server.start();
	const winnerProvider = { capability: "ui", definitions: [{ name: "winner-select" }] };
	const loserProvider = { capability: "ui", definitions: [{ name: "loser-select" }] };
	const winner = new AcpSdkAdapter({
		client: new SdkClient(endpoint.url, "token", { ...ACP_SESSION_RECONNECT }),
		providers: [winnerProvider],
	});
	const loser = new AcpSdkAdapter({
		client: new SdkClient(endpoint.url, "token", { ...ACP_SESSION_RECONNECT }),
		providers: [loserProvider],
	});

	try {
		const settled = await Promise.allSettled([winner.start(), loser.start()]);
		expect(settled).toHaveLength(2);
		for (const result of settled)
			expect(result).toMatchObject({ status: "rejected", reason: { code: "operation_prohibited" } });
		expect(winner.leaseIds.size).toBe(0);
		expect(loser.leaseIds.size).toBe(0);
		expect(installedDefinitions.size).toBe(0);
	} finally {
		await winner.close();
		await loser.close();
		server.stop();
		await host.stop();
	}
});
