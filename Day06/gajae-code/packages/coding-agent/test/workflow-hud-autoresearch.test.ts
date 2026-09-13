import { describe, expect, it } from "bun:test";
import { buildAutoresearchHudSummary } from "../src/skill-state/workflow-hud";

describe("autoresearch HUD summary", () => {
	it("shows phase only for zero-experiment missions", () => {
		const summary = buildAutoresearchHudSummary({ phase: "research" });
		expect(summary.chips?.map(chip => chip.label)).toEqual(["phase"]);
	});

	it("shows tally and warning failures", () => {
		const summary = buildAutoresearchHudSummary({
			phase: "research",
			experimentStatuses: ["keep", "discard", "crash", "checks_failed"],
		});
		expect(summary.chips).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ label: "exp", value: "1/4" }),
				expect.objectContaining({ label: "crash", value: "1", severity: "warning" }),
				expect.objectContaining({ label: "checks_failed", value: "1", severity: "warning" }),
			]),
		);
	});

	it("renders verdict and preserves mission summary", () => {
		const summary = buildAutoresearchHudSummary({ phase: "verdict", verdict: "keep", slug: "hud" });
		expect(summary.summary).toBe("autoresearch mission hud");
		expect(summary.chips).toEqual(
			expect.arrayContaining([expect.objectContaining({ label: "verdict", value: "keep" })]),
		);
	});
});
