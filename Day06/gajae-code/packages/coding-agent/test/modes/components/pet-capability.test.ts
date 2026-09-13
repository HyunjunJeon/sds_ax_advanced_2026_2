import { afterEach, describe, expect, it, vi } from "bun:test";
import {
	getPetPixelProtocol,
	getPetRenderProtocol,
	PET_CAPABILITY_SETTLE_MS,
	setVerifiedItermPetAvailability,
	warnWhenPetCapabilitySettled,
} from "@gajae-code/coding-agent/modes/components/pet-capability";
import { ImageProtocol, setTerminalImageProtocol, TERMINAL } from "@gajae-code/tui";

const originalProtocol = TERMINAL.imageProtocol;
const multiplexerEnvKeys = [
	"TMUX",
	"TMUX_PANE",
	"STY",
	"ZELLIJ",
	"GJC_TMUX_LAUNCHED",
	"TERM",
	"PI_FORCE_IMAGE_PROTOCOL",
	"GJC_FORCE_IMAGE_PROTOCOL",
] as const;
const originalMultiplexerEnv = new Map(multiplexerEnvKeys.map(key => [key, Bun.env[key]] as const));

const multiplexerCases = [
	["tmux", { TMUX: "/tmp/tmux-1000/default,1,0" }],
	["screen", { STY: "1234.pts-0.host" }],
	["zellij", { ZELLIJ: "session" }],
] as const;

function setForcedProtocol(protocol: "kitty" | "sixel", multiplexerEnv: Readonly<Record<string, string>>): void {
	for (const key of multiplexerEnvKeys) delete Bun.env[key];
	Bun.env.PI_FORCE_IMAGE_PROTOCOL = protocol;
	for (const [key, value] of Object.entries(multiplexerEnv)) Bun.env[key] = value;
	setTerminalImageProtocol(protocol === "kitty" ? ImageProtocol.Kitty : ImageProtocol.Sixel);
}

afterEach(() => {
	setVerifiedItermPetAvailability(undefined);
	setTerminalImageProtocol(originalProtocol);
	for (const key of multiplexerEnvKeys) {
		const value = originalMultiplexerEnv.get(key);
		if (value === undefined) delete Bun.env[key];
		else Bun.env[key] = value;
	}
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe("getPetPixelProtocol", () => {
	it("requires an active iTerm capability reply instead of trusting direct or forwarded markers", () => {
		setTerminalImageProtocol(ImageProtocol.Iterm2);

		expect(getPetPixelProtocol()).toBeNull();
		expect(getPetRenderProtocol()).toBe("text");

		setVerifiedItermPetAvailability({ available: false, mode: "direct", reason: "probe-timeout", epoch: 1 });
		expect(getPetPixelProtocol()).toBeNull();

		setVerifiedItermPetAvailability({ available: true, mode: "direct", epoch: 2 });
		expect(getPetPixelProtocol()).toBe("iterm");
		expect(getPetRenderProtocol()).toBe("iterm");
	});

	it.each([
		[ImageProtocol.Kitty, "kitty"],
		[ImageProtocol.Sixel, "sixel"],
	] as const)("keeps %s precedence over verified iTerm", (imageProtocol, expected) => {
		for (const key of multiplexerEnvKeys) delete Bun.env[key];
		setVerifiedItermPetAvailability({ available: true, mode: "direct", epoch: 1 });
		setTerminalImageProtocol(imageProtocol);

		expect(getPetPixelProtocol()).toBe(expected);
	});

	for (const [name, multiplexerEnv] of multiplexerCases) {
		it(`uses text cells for forced Kitty inside ${name}`, () => {
			setForcedProtocol("kitty", multiplexerEnv);

			expect(getPetPixelProtocol()).toBeNull();
			expect(getPetRenderProtocol()).toBe("text");
		});

		it(`keeps forced Sixel available inside ${name}`, () => {
			setForcedProtocol("sixel", multiplexerEnv);

			expect(getPetPixelProtocol()).toBe("sixel");
			expect(getPetRenderProtocol()).toBe("sixel");
		});
	}
});

describe("warnWhenPetCapabilitySettled", () => {
	it("warns immediately when no probe can change the answer", () => {
		const onUnavailable = vi.fn();

		warnWhenPetCapabilitySettled({ probePending: false, onUnavailable });

		expect(onUnavailable).toHaveBeenCalledTimes(1);
	});

	it("never warns when the pending probe enables graphics before the deadline", () => {
		vi.useFakeTimers();
		setTerminalImageProtocol(null);
		const onUnavailable = vi.fn();

		const dispose = warnWhenPetCapabilitySettled({ probePending: true, onUnavailable });
		try {
			// Startup ordering: no warning may fire while the probe is in flight.
			expect(onUnavailable).not.toHaveBeenCalled();

			// The probe succeeds (e.g. Windows Terminal answering XTSMGRAPHICS).
			setTerminalImageProtocol(ImageProtocol.Sixel);
			vi.advanceTimersByTime(PET_CAPABILITY_SETTLE_MS * 2);

			expect(onUnavailable).not.toHaveBeenCalled();
		} finally {
			dispose();
		}
	});

	it("does not warn when the settle deadline selects the text-cell fallback", () => {
		vi.useFakeTimers();
		setTerminalImageProtocol(null);
		const onUnavailable = vi.fn();

		const dispose = warnWhenPetCapabilitySettled({ probePending: true, onUnavailable });
		try {
			expect(onUnavailable).not.toHaveBeenCalled();

			vi.advanceTimersByTime(PET_CAPABILITY_SETTLE_MS * 2);

			expect(onUnavailable).not.toHaveBeenCalled();
		} finally {
			dispose();
		}
	});

	it("stays silent when disposed before settlement", () => {
		vi.useFakeTimers();
		setTerminalImageProtocol(null);
		const onUnavailable = vi.fn();

		const dispose = warnWhenPetCapabilitySettled({ probePending: true, onUnavailable });
		dispose();
		vi.advanceTimersByTime(PET_CAPABILITY_SETTLE_MS * 2);

		expect(onUnavailable).not.toHaveBeenCalled();
	});
});
