import { expect, test } from "bun:test";
import * as path from "node:path";

interface RootManifest {
	scripts: Record<string, string>;
}

test("install:dev builds the native addon before linking the source CLI", async () => {
	const repoRoot = path.resolve(import.meta.dir, "..");
	const manifest = (await Bun.file(path.join(repoRoot, "package.json")).json()) as RootManifest;

	expect(manifest.scripts["install:dev"]?.split(" && ")).toEqual([
		"bun install",
		"bun run build:native",
		"bun --cwd=packages/coding-agent link",
		"bun --cwd=packages/ai link",
		"bun run dev:link",
		"bun packages/coding-agent/src/cli.ts setup defaults",
	]);
});
