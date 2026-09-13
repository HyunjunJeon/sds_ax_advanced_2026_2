import { describe, expect, test, vi } from "bun:test";
import * as nodeFs from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { LoadContext } from "../capability/types";
import { getEmbeddedDefaultGjcSkills } from "../defaults/gjc-defaults";
import { buildSkillPromptMessage } from "../extensibility/skills";
import { SKILL_FRONTMATTER_SCAN_BYTES, SKILL_FRONTMATTER_SCAN_TOTAL_BYTES, scanSkillDescriptorsFromDir } from "./index";

function makeContext(root: string): LoadContext {
	return { cwd: root, home: root, repoRoot: null };
}

describe("skill descriptors", () => {
	test("frontmatter scanning is bounded and does not read the body", async () => {
		const root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? "/tmp", "gjc-skill-descriptor-"));
		try {
			const skillDir = path.join(root, "bounded");
			await fs.mkdir(skillDir, { recursive: true });
			const bodyMarker = "BODY_MARKER_MUST_NOT_BE_SCANNED";
			const body = "x".repeat(SKILL_FRONTMATTER_SCAN_BYTES) + bodyMarker;
			await fs.writeFile(
				path.join(skillDir, "SKILL.md"),
				`---\nname: bounded\ndescription: bounded scan\n---\n${body}`,
			);

			// Discovery reads the validated descriptor rather than reopening with Bun.file.
			// Observe only the handles this scan opens: a FileHandle prototype spy is
			// process-wide, so unrelated reads from concurrently running tests
			// interleave and make global counts flaky.
			const reads: Array<unknown[]> = [];
			const probe = await fs.open(path.join(skillDir, "SKILL.md"), "r");
			const fileHandlePrototype = Object.getPrototypeOf(probe) as fs.FileHandle;
			await probe.close();
			const originalOpen = nodeFs.promises.open;
			type OpenArgs = Parameters<typeof nodeFs.promises.open>;
			const openSpy = vi.spyOn(nodeFs.promises, "open").mockImplementation((async (...args: OpenArgs) => {
				const handle = await originalOpen(...args);
				const skillPath = args[0];
				if (typeof skillPath === "string" && skillPath.endsWith("SKILL.md")) {
					const originalRead = handle.read.bind(handle);
					handle.read = (async (...readArgs: never[]) => {
						reads.push(readArgs as unknown[]);
						return originalRead(...readArgs);
					}) as typeof handle.read;
				}
				return handle;
			}) as typeof nodeFs.promises.open);
			const readFileSpy = vi.spyOn(fileHandlePrototype, "readFile");
			try {
				const result = await scanSkillDescriptorsFromDir(makeContext(root), {
					dir: root,
					providerId: "test",
					level: "project",
				});
				expect(result.items).toHaveLength(1);
				expect(Object.hasOwn(result.items[0]?.metadata ?? {}, "content")).toBe(false);
				expect(JSON.stringify(result.items[0]?.metadata)).not.toContain(bodyMarker);
				const boundedReads = reads.filter(
					call => call[1] === 0 && call[2] === SKILL_FRONTMATTER_SCAN_BYTES && call[3] === 0,
				);
				expect(boundedReads).toHaveLength(1);
				const oversizedReads = reads.filter(
					call => typeof call[2] === "number" && call[2] > SKILL_FRONTMATTER_SCAN_BYTES,
				);
				expect(oversizedReads).toHaveLength(0);
				expect(readFileSpy).not.toHaveBeenCalled();
			} finally {
				openSpy.mockRestore();
				readFileSpy.mockRestore();
			}
		} finally {
			await fs.rm(root, { recursive: true, force: true });
		}
	});

	test("unterminated frontmatter stops at the total scan cap", async () => {
		const root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? "/tmp", "gjc-skill-unterminated-"));
		try {
			const skillDir = path.join(root, "unterminated");
			await fs.mkdir(skillDir, { recursive: true });
			await fs.writeFile(
				path.join(skillDir, "SKILL.md"),
				`---\nname: unterminated\ndescription: no closing delimiter\n${"x".repeat(SKILL_FRONTMATTER_SCAN_TOTAL_BYTES * 32)}`,
			);
			const result = await scanSkillDescriptorsFromDir(makeContext(root), {
				dir: root,
				providerId: "test",
				level: "project",
			});
			expect(result.items).toHaveLength(0);
			expect((result.warnings ?? []).some(warning => warning.includes("scan cap"))).toBe(true);
		} finally {
			await fs.rm(root, { recursive: true, force: true });
		}
	});
	test("bundled skill prompt injection is byte-identical through the lazy catalog", async () => {
		const embedded = getEmbeddedDefaultGjcSkills().find(skill => skill.name === "ralplan");
		if (!embedded) throw new Error("ralplan bundled skill missing");
		const legacyContent = embedded.content;
		const legacy = await buildSkillPromptMessage(
			{ ...embedded, content: legacyContent, loadContent: undefined },
			"example task",
		);
		const lazy = await buildSkillPromptMessage({ ...embedded, content: undefined }, "example task");
		expect(lazy.message).toBe(legacy.message);
		expect(lazy.details).toEqual(legacy.details);
	});
});
