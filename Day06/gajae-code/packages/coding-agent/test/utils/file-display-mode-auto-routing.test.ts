import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { resetSettingsForTest, Settings } from "@gajae-code/coding-agent/config/settings";
import type { FileDisplayModeSession } from "@gajae-code/coding-agent/utils/file-display-mode";
import { resolveFileDisplayMode } from "@gajae-code/coding-agent/utils/file-display-mode";

// ─── Env isolation ───────────────────────────────────────────────────────────

let savedGjcVariant: string | undefined;
let savedPiVariant: string | undefined;

beforeEach(() => {
	savedGjcVariant = Bun.env.GJC_EDIT_VARIANT;
	savedPiVariant = Bun.env.PI_EDIT_VARIANT;
	delete Bun.env.GJC_EDIT_VARIANT;
	delete Bun.env.PI_EDIT_VARIANT;
});

afterEach(() => {
	if (savedGjcVariant === undefined) delete Bun.env.GJC_EDIT_VARIANT;
	else Bun.env.GJC_EDIT_VARIANT = savedGjcVariant;
	if (savedPiVariant === undefined) delete Bun.env.PI_EDIT_VARIANT;
	else Bun.env.PI_EDIT_VARIANT = savedPiVariant;
	resetSettingsForTest();
});

// ─── Helpers ─────────────────────────────────────────────────────────────────

function makeSession(options: {
	model?: string;
	readLineNumbers?: boolean;
	readHashLines?: boolean;
	hasEditTool?: boolean;
	editMode?: string;
}): FileDisplayModeSession & { getActiveModelString?: () => string | undefined } {
	const globalSettings: Record<string, unknown> = {};
	if (options.readLineNumbers !== undefined) globalSettings.readLineNumbers = options.readLineNumbers;
	if (options.readHashLines !== undefined) globalSettings.readHashLines = options.readHashLines;
	if (options.editMode !== undefined) globalSettings["edit.mode"] = options.editMode;
	const session: FileDisplayModeSession & { getActiveModelString?: () => string | undefined } = {
		settings: Settings.isolated(globalSettings as never),
		...(options.hasEditTool !== undefined ? { hasEditTool: options.hasEditTool } : {}),
		...(options.model !== undefined ? { getActiveModelString: () => options.model } : {}),
	};
	return session;
}

// ─── Auto routing drives read/search display ─────────────────────────────────

describe("resolveFileDisplayMode under edit.mode auto routing", () => {
	test("hashline-family models keep hash anchors and default line numbers", () => {
		for (const model of [
			"minimax-code/minimax-m2.5",
			"zai/glm-4.7",
			"moonshotai/kimi-k2.5",
			"custom/company-code-model",
			undefined,
		]) {
			const mode = resolveFileDisplayMode(makeSession({ model }));
			expect(mode.hashLines).toBe(true);
			expect(mode.lineNumbers).toBe(true);
		}
	});

	test("apply_patch-family (GPT) models drop hash anchors and default line numbers", () => {
		for (const model of [
			"openai/gpt-5.4",
			"openrouter/openai/gpt-5.4",
			"company-gateway/gpt-5.4",
			"custom/gpt-oss-120b",
		]) {
			const mode = resolveFileDisplayMode(makeSession({ model }));
			expect(mode.hashLines).toBe(false);
			expect(mode.lineNumbers).toBe(false);
		}
	});

	test("replace-family (Claude/DeepSeek/Qwen) models drop hash anchors and default line numbers", () => {
		for (const model of ["anthropic/claude-sonnet-4-6", "deepseek/deepseek-v3.2", "custom/qwen3-coder"]) {
			const mode = resolveFileDisplayMode(makeSession({ model }));
			expect(mode.hashLines).toBe(false);
			expect(mode.lineNumbers).toBe(false);
		}
	});

	test("readLineNumbers: true restores line numbers for non-hashline models", () => {
		const mode = resolveFileDisplayMode(makeSession({ model: "openai/gpt-5.4", readLineNumbers: true }));
		expect(mode.hashLines).toBe(false);
		expect(mode.lineNumbers).toBe(true);
	});

	test("explicit edit.mode: hashline pins anchors even for a GPT model", () => {
		const mode = resolveFileDisplayMode(makeSession({ model: "openai/gpt-5.4", editMode: "hashline" }));
		expect(mode.hashLines).toBe(true);
		expect(mode.lineNumbers).toBe(true);
	});

	test("GJC_EDIT_VARIANT=hashline restores the hashline read/search display (emergency rollback)", () => {
		Bun.env.GJC_EDIT_VARIANT = "hashline";
		for (const model of ["openai/gpt-5.4", "anthropic/claude-sonnet-4-6"]) {
			const mode = resolveFileDisplayMode(makeSession({ model }));
			expect(mode.hashLines).toBe(true);
			expect(mode.lineNumbers).toBe(true);
		}
	});

	test("readHashLines: false suppresses anchors; line numbers then follow readLineNumbers", () => {
		const suppressed = resolveFileDisplayMode(makeSession({ model: "zai/glm-4.7", readHashLines: false }));
		expect(suppressed.hashLines).toBe(false);
		expect(suppressed.lineNumbers).toBe(false);
		const withLineNumbers = resolveFileDisplayMode(
			makeSession({ model: "zai/glm-4.7", readHashLines: false, readLineNumbers: true }),
		);
		expect(withLineNumbers.hashLines).toBe(false);
		expect(withLineNumbers.lineNumbers).toBe(true);
	});

	test("no edit tool suppresses hash anchors in every mode", () => {
		const hashline = resolveFileDisplayMode(makeSession({ model: "zai/glm-4.7", hasEditTool: false }));
		expect(hashline.hashLines).toBe(false);
		const gpt = resolveFileDisplayMode(makeSession({ model: "openai/gpt-5.4", hasEditTool: false }));
		expect(gpt.hashLines).toBe(false);
	});

	test("raw and immutable reads suppress anchors and line numbers", () => {
		const raw = resolveFileDisplayMode(makeSession({ model: "zai/glm-4.7" }), { raw: true });
		expect(raw.hashLines).toBe(false);
		expect(raw.lineNumbers).toBe(false);
		const immutable = resolveFileDisplayMode(makeSession({ model: "zai/glm-4.7" }), { immutable: true });
		expect(immutable.hashLines).toBe(false);
		expect(immutable.lineNumbers).toBe(false);
	});
});
