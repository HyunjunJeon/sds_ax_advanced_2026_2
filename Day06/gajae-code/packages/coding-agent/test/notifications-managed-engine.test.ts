import { describe, expect, test } from "bun:test";
import { ManagedNotificationDaemon } from "../src/sdk/bus/managed-daemon";

class TestDaemon extends ManagedNotificationDaemon {
	constructor() {
		super({ now: Date.now });
	}
	public render(frame: Record<string, unknown>) {
		return this.renderFrame(frame);
	}
}

describe("shared managed notification presentation engine", () => {
	test("owns the rate-limit pool outside presentation adapters", () => {
		const daemon = new TestDaemon();
		expect(daemon.pool.pending).toBe(0);
	});

	test("renders internal frames through shared renderer", () => {
		const daemon = new TestDaemon();
		const send = daemon.render({ type: "turn_stream", sessionId: "s1", phase: "finalized", text: "**done**" });
		expect(send?.method).toBe("sendMessage");
		expect(send?.lane).toBe("finalized");
		expect(send?.text).toContain("done");
	});
});
