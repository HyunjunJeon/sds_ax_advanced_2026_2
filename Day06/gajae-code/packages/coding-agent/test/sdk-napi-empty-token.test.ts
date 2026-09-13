import { expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { NotificationServer } from "../../natives/native/index.js";

async function open(endpoint: string, token: string): Promise<WebSocket> {
	const ws = new WebSocket(`${endpoint}/?token=${token}`);
	const { promise, resolve, reject } = Promise.withResolvers<void>();
	ws.addEventListener("open", () => resolve(), { once: true });
	ws.addEventListener("error", () => reject(new Error("websocket connection failed")), { once: true });
	await promise;
	return ws;
}

test("compiled NotificationServer rejects an empty token without publishing an endpoint", async () => {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "gjc-sdk-empty-token-"));
	const rejectedSessionId = `empty-${process.pid}-${Date.now()}`;
	const rejectedEndpoint = path.join(root, "sdk", `${rejectedSessionId}.json`);
	const rejected = new NotificationServer(rejectedSessionId, "", root, true);
	const ordinarySessionId = `ordinary-${process.pid}-${Date.now()}`;
	const ordinaryToken = "ordinary-opaque-token";
	const ordinary = new NotificationServer(ordinarySessionId, ordinaryToken, root, true);
	let ws: WebSocket | undefined;

	try {
		await expect(rejected.start()).rejects.toThrow("bind failed: notification server token must not be empty");
		await expect(fs.stat(rejectedEndpoint)).rejects.toMatchObject({ code: "ENOENT" });

		const endpoint = await ordinary.start();
		ws = await open(endpoint.url, ordinaryToken);
		expect(endpoint.sessionId).toBe(ordinarySessionId);
		expect(endpoint.port).toBeGreaterThan(0);
	} finally {
		ws?.close();
		await rejected.stopAndWait();
		await ordinary.stopAndWait();
		await fs.rm(root, { recursive: true, force: true });
	}
});
