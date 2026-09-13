import { afterEach, describe, expect, it } from "bun:test";
import { getTelemetryStatus, TELEMETRY_KILL_SWITCH_ENV } from "../src/telemetry/control";
import {
	resetTelemetryTransportForTest,
	sendTelemetryEvent,
	TELEMETRY_MAX_IN_FLIGHT,
} from "../src/telemetry/transport";

const installId = "123e4567-e89b-42d3-a456-426614174000";
const event = {
	schemaVersion: 1 as const,
	event: "update_check_completed" as const,
	installId,
	occurredAt: "2026-08-28T17:00:00.000Z",
	channel: "stable" as const,
	result: "up_to_date" as const,
};
const disabledSettings = { get: (_path: "telemetry.enabled") => false };
const enabledSettings = { get: (_path: "telemetry.enabled") => true };

afterEach(async () => {
	delete process.env[TELEMETRY_KILL_SWITCH_ENV];
	await resetTelemetryTransportForTest();
});

describe("telemetry controls", () => {
	it("is disabled by default and reports the setting state", () => {
		const status = getTelemetryStatus(disabledSettings);
		expect(status).toEqual({ enabled: false, disabledByKillSwitch: false, reason: "setting_disabled" });
	});

	it("cannot be enabled while the emergency kill switch is set", () => {
		process.env[TELEMETRY_KILL_SWITCH_ENV] = "1";
		const status = getTelemetryStatus(enabledSettings);
		expect(status).toEqual({ enabled: false, disabledByKillSwitch: true, reason: "kill_switch" });
	});
});

describe("bounded nonblocking telemetry transport", () => {
	it("returns immediately without invoking fetch when disabled", () => {
		let calls = 0;
		const started = performance.now();
		sendTelemetryEvent(disabledSettings, event, {
			fetchImpl: async () => {
				calls++;
				return new Response(null, { status: 204 });
			},
		});
		expect(performance.now() - started).toBeLessThan(50);
		expect(calls).toBe(0);
	});

	it("swallows offline failures and limits concurrent requests", async () => {
		const pending: Array<() => void> = [];
		let calls = 0;
		const fetchImpl = () => {
			calls++;
			return new Promise<Response>((_, reject) => pending.push(() => reject(new Error("offline"))));
		};
		for (let index = 0; index < TELEMETRY_MAX_IN_FLIGHT + 1; index++)
			sendTelemetryEvent(enabledSettings, event, { fetchImpl });
		expect(calls).toBe(TELEMETRY_MAX_IN_FLIGHT);
		for (const reject of pending) reject();
		await new Promise(resolve => setTimeout(resolve, 0));
	});

	it("does not reset the concurrency cap while requests are still settling", async () => {
		const release: Array<() => void> = [];
		let calls = 0;
		const fetchImpl = () => {
			calls++;
			return new Promise<Response>(resolve => release.push(() => resolve(new Response(null, { status: 204 }))));
		};
		for (let index = 0; index < TELEMETRY_MAX_IN_FLIGHT; index++)
			sendTelemetryEvent(enabledSettings, event, { fetchImpl });

		let resetFinished = false;
		const reset = resetTelemetryTransportForTest().then(() => {
			resetFinished = true;
		});
		await Bun.sleep(0);
		expect(resetFinished).toBe(false);
		sendTelemetryEvent(enabledSettings, event, { fetchImpl });
		expect(calls).toBe(TELEMETRY_MAX_IN_FLIGHT);
		for (const resolve of release) resolve();
		await reset;
	});
});
