import { expect, test } from "bun:test";
import { ACP_SESSION_RECONNECT, AcpSdkAdapter } from "../src/sdk/acp";
import { HEARTBEAT_TTL_MS } from "../src/sdk/bus/daemon-paths";
import { expectedBackoffs } from "./helpers/fake-sdk-transport";

test("ACP session reconnect budget outlives the host heartbeat TTL", () => {
	const backoffs = expectedBackoffs(ACP_SESSION_RECONNECT);
	const totalBudgetMs = backoffs.reduce((total, backoff) => total + backoff, 0);
	// The host drops a session whose client has not ponged within HEARTBEAT_TTL_MS,
	// so a shorter client budget makes every host-reaped stall unrecoverable.
	expect(totalBudgetMs).toBeGreaterThan(HEARTBEAT_TTL_MS);
	// Recovery must stay prompt: no single sleep may swallow the whole TTL.
	expect(Math.max(...backoffs)).toBe(ACP_SESSION_RECONNECT.reconnectMaxBackoffMs);
	expect(ACP_SESSION_RECONNECT.reconnectMaxBackoffMs).toBeLessThan(HEARTBEAT_TTL_MS);
});

test("AcpSdkAdapter requires an explicit Broker client or SessionRouter", async () => {
	expect(() => new AcpSdkAdapter({})).toThrow("exactly one Broker client or SessionRouter");
	await expect(AcpSdkAdapter.connect({})).rejects.toMatchObject({ code: "invalid_input" });
});
