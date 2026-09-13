import { describe, expect, test } from "bun:test";
import { BACKGROUND_FOLD_DOUBLE_PRESS_MS, InputController } from "../src/modes/controllers/input-controller";

describe("foreground bash background fold", () => {
	test("requires a second press within the preserved 750ms window", async () => {
		let folds = 0;
		let status = "";
		const controller = new InputController({
			session: {
				hasForegroundBashBackgroundRequestHandler: () => true,
				requestForegroundBashBackground: async () => {
					folds += 1;
					return true;
				},
			},
			showStatus: (message: string) => {
				status = message;
			},
			showWarning: () => {},
		} as never);
		const realNow = Date.now;
		try {
			Date.now = () => 1_000;
			expect(controller.handleForegroundToolBackgroundFold()).toBe(true);
			expect(status).toContain("again");
			Date.now = () => 1_000 + BACKGROUND_FOLD_DOUBLE_PRESS_MS;
			expect(controller.handleForegroundToolBackgroundFold()).toBe(true);
			await Bun.sleep(0);
			expect(folds).toBe(1);
		} finally {
			Date.now = realNow;
		}
	});

	test("claims the fold before a queued microtask can submit steering", async () => {
		const order: string[] = [];
		const controller = new InputController({
			session: {
				hasForegroundBashBackgroundRequestHandler: () => true,
				requestForegroundBashBackground: async () => {
					order.push("request");
					await Bun.sleep(0);
					return true;
				},
			},
			showStatus: () => {},
			showWarning: () => {},
		} as never);
		const realNow = Date.now;
		try {
			Date.now = () => 1_000;
			controller.handleForegroundToolBackgroundFold();
			Date.now = () => 1_000 + BACKGROUND_FOLD_DOUBLE_PRESS_MS;
			queueMicrotask(() => order.push("steer"));
			controller.handleForegroundToolBackgroundFold();
			await Bun.sleep(0);
			expect(order).toEqual(["request", "steer"]);
		} finally {
			Date.now = realNow;
		}
	});

	for (const testCase of [
		{
			label: "synchronous rejection",
			request: () => {
				throw new Error("fold unavailable");
			},
		},
		{
			label: "asynchronous rejection",
			request: async () => {
				throw new Error("fold unavailable");
			},
		},
	]) {
		test(`routes ${testCase.label} through the warning path`, async () => {
			let warnings = 0;
			const controller = new InputController({
				session: {
					hasForegroundBashBackgroundRequestHandler: () => true,
					requestForegroundBashBackground: testCase.request as never,
				},
				showStatus: () => {},
				showWarning: () => {
					warnings += 1;
				},
			} as never);

			const realNow = Date.now;
			try {
				Date.now = () => 1_000;
				controller.handleForegroundToolBackgroundFold();
				controller.handleForegroundToolBackgroundFold();
				await Bun.sleep(0);
				expect(warnings).toBe(1);
			} finally {
				Date.now = realNow;
			}
		});
	}
});
