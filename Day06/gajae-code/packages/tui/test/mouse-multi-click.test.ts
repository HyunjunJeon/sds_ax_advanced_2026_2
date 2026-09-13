import { describe, expect, test } from "bun:test";
import { DEFAULT_MULTI_CLICK_INTERVAL_MS, TUI } from "../src/tui";
import { VirtualTerminal } from "./virtual-terminal";

function press(x: number, y: number): string {
	return `\x1b[<0;${x};${y}M`;
}

function drag(x: number, y: number): string {
	return `\x1b[<32;${x};${y}M`;
}

function release(x: number, y: number): string {
	return `\x1b[<0;${x};${y}m`;
}

async function mount(
	lines: string[],
	size: { columns: number; rows: number },
	multiClickIntervalMs?: number,
): Promise<{ terminal: VirtualTerminal; tui: TUI; copied: string[] }> {
	const terminal = new VirtualTerminal(size.columns, size.rows);
	const copied: string[] = [];
	const tui = new TUI(terminal, undefined, {
		enableMouse: true,
		copySelection: text => {
			copied.push(text);
		},
		...(multiClickIntervalMs === undefined ? {} : { multiClickIntervalMs }),
	});
	tui.addChild({ render: () => lines, invalidate: () => {} });
	tui.start();
	await terminal.waitForRender();
	return { terminal, tui, copied };
}

describe("multi-click selection", () => {
	test("double click copies the word under the cursor without a drag", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo charlie"], { columns: 30, rows: 1 });

		// Two presses on the same cell inside the escalation window, each with its
		// own release — exactly what a terminal emits for a real double click.
		terminal.sendInput(press(9, 1));
		terminal.sendInput(release(9, 1));
		terminal.sendInput(press(9, 1));
		terminal.sendInput(release(9, 1));
		await terminal.waitForRender();

		expect(copied).toEqual(["bravo"]);
		tui.stop();
	});

	test("triple click copies the whole row after the word it escalated through", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		for (let click = 0; click < 3; click++) {
			terminal.sendInput(press(3, 1));
			terminal.sendInput(release(3, 1));
		}
		await terminal.waitForRender();

		// Every release copies what is selected at that moment, so escalating past
		// the word stage also copies the word. The row lands on the clipboard last.
		expect(copied).toEqual(["alpha", "alpha bravo"]);
		tui.stop();
	});

	test("a fourth click stays on the row instead of cycling back to a character", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		for (let click = 0; click < 4; click++) {
			terminal.sendInput(press(3, 1));
			terminal.sendInput(release(3, 1));
		}
		await terminal.waitForRender();

		expect(copied).toEqual(["alpha", "alpha bravo", "alpha bravo"]);
		tui.stop();
	});

	test("a single click still copies nothing", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("double click selects a separator run when the cursor is on whitespace", async () => {
		const { terminal, tui, copied } = await mount(["alpha   bravo"], { columns: 30, rows: 1 });

		for (let click = 0; click < 2; click++) {
			terminal.sendInput(press(7, 1));
			terminal.sendInput(release(7, 1));
		}
		await terminal.waitForRender();

		expect(copied).toEqual(["   "]);
		tui.stop();
	});

	test("double click keeps a path intact and stops at bracket separators", async () => {
		const { terminal, tui, copied } = await mount(["run(src/lib/tui.ts)"], { columns: 30, rows: 1 });

		for (let click = 0; click < 2; click++) {
			terminal.sendInput(press(8, 1));
			terminal.sendInput(release(8, 1));
		}
		await terminal.waitForRender();

		expect(copied).toEqual(["src/lib/tui.ts"]);
		tui.stop();
	});

	test("double click snaps to whole wide graphemes", async () => {
		const { terminal, tui, copied } = await mount(["한글 텍스트"], { columns: 30, rows: 1 });

		// Column 2 lands on the trailing cell of the first wide grapheme.
		for (let click = 0; click < 2; click++) {
			terminal.sendInput(press(2, 1));
			terminal.sendInput(release(2, 1));
		}
		await terminal.waitForRender();

		expect(copied).toEqual(["한글"]);
		tui.stop();
	});

	test("dragging after a double click extends by whole words in both directions", async () => {
		for (const direction of ["forward", "backward"] as const) {
			const { terminal, tui, copied } = await mount(["alpha bravo charlie"], { columns: 30, rows: 1 });

			terminal.sendInput(press(9, 1));
			terminal.sendInput(release(9, 1));
			terminal.sendInput(press(9, 1));
			// Land mid-word: the far end must still snap out to the word boundary.
			terminal.sendInput(drag(direction === "forward" ? 15 : 3, 1));
			terminal.sendInput(release(direction === "forward" ? 15 : 3, 1));
			await terminal.waitForRender();

			expect(copied).toEqual([direction === "forward" ? "bravo charlie" : "alpha bravo"]);
			tui.stop();
		}
	});

	test("dragging after a triple click extends by whole rows", async () => {
		const { terminal, tui, copied } = await mount(["alpha", "bravo", "charlie"], { columns: 30, rows: 3 });

		for (let click = 0; click < 3; click++) {
			terminal.sendInput(press(3, 1));
			terminal.sendInput(release(3, 1));
		}
		terminal.sendInput(press(3, 1));
		terminal.sendInput(drag(2, 2));
		terminal.sendInput(release(2, 2));
		await terminal.waitForRender();

		expect(copied.at(-1)).toBe("alpha\nbravo");
		tui.stop();
	});

	test("a press outside the escalation window starts a fresh character selection", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 }, 5);

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await Bun.sleep(20);
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("a repeat press on a different cell starts a fresh character selection", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		terminal.sendInput(press(9, 1));
		terminal.sendInput(release(9, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("a wheel notch between presses cancels escalation", async () => {
		const { terminal, tui, copied } = await mount(
			Array.from({ length: 12 }, (_value, index) => `line-${index}`),
			{ columns: 30, rows: 5 },
		);

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		terminal.sendInput("\x1b[<64;10;2M");
		await terminal.flush();
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("a drag between clicks cancels escalation", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		terminal.sendInput(press(3, 1));
		terminal.sendInput(drag(4, 1));
		terminal.sendInput(release(4, 1));
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual(["ph"]);
		tui.stop();
	});

	test("a release without a matching press cancels escalation", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		terminal.sendInput(release(3, 1));
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("a duplicate press without release cancels escalation", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		terminal.sendInput(press(3, 1));
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("a duplicate press after triple click cancels the clamped line chain", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		for (let click = 0; click < 3; click++) {
			terminal.sendInput(press(3, 1));
			terminal.sendInput(release(3, 1));
		}
		terminal.sendInput(press(3, 1));
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual(["alpha", "alpha bravo"]);
		tui.stop();
	});

	test("a long-held first press does not start a repeat-click window on release", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 }, 5);

		terminal.sendInput(press(3, 1));
		await Bun.sleep(20);
		terminal.sendInput(release(3, 1));
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("stop and restart clear pending click escalation", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		tui.stop();
		tui.start();
		await terminal.waitForRender();
		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("repeat clicks compare transcript coordinates after scrolling", async () => {
		const { terminal, tui, copied } = await mount(
			Array.from({ length: 12 }, (_value, index) => `line-${index}`),
			{ columns: 30, rows: 5 },
		);

		terminal.sendInput(press(3, 1));
		terminal.sendInput(release(3, 1));
		terminal.sendInput("\x1b[<64;10;2M");
		await terminal.flush();
		terminal.sendInput(press(3, 4));
		terminal.sendInput(release(3, 4));
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("multiClickIntervalMs: 0 disables escalation entirely", async () => {
		const { terminal, tui, copied } = await mount(["alpha bravo"], { columns: 30, rows: 1 }, 0);

		for (let click = 0; click < 3; click++) {
			terminal.sendInput(press(3, 1));
			terminal.sendInput(release(3, 1));
		}
		await terminal.waitForRender();

		expect(copied).toEqual([]);
		tui.stop();
	});

	test("double click never splits an emoji grapheme from either occupied cell", async () => {
		for (const x of [2, 3]) {
			const { terminal, tui, copied } = await mount(["A👩‍💻B"], { columns: 30, rows: 1 });

			for (let click = 0; click < 2; click++) {
				terminal.sendInput(press(x, 1));
				terminal.sendInput(release(x, 1));
			}
			await terminal.waitForRender();

			expect(copied).toEqual(["A👩‍💻B"]);
			expect(copied[0]).toContain("👩‍💻");
			tui.stop();
		}
	});

	test("a word selection is painted before the button is released", async () => {
		const { terminal, tui } = await mount(["alpha bravo"], { columns: 30, rows: 1 });

		terminal.sendInput(press(9, 1));
		terminal.sendInput(release(9, 1));
		await terminal.waitForRender();
		terminal.clearWriteLog();

		terminal.sendInput(press(9, 1));
		await terminal.waitForRender();

		expect(terminal.getWriteLog().join("")).toContain("\x1b[7m");
		tui.stop();
	});

	test("the default escalation window is a human double-click interval", () => {
		expect(DEFAULT_MULTI_CLICK_INTERVAL_MS).toBe(400);
	});
});
