import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	BINARY_MANIFEST_FILE,
	BINARY_SHA256_FILE,
	RELEASE_BINARY_NAMES,
	buildReleaseBinariesManifest,
	formatSha256Sums,
	parseSha256Sums,
	writeReleaseBinariesManifest,
} from "./release-binaries-manifest";

describe("release binary checksum manifest", () => {
	test("hashes every published standalone asset and round-trips the sums file", () => {
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gjc-release-binaries-"));
		try {
			for (const name of RELEASE_BINARY_NAMES) {
				fs.writeFileSync(path.join(dir, name), `payload-${name}`);
			}
			const manifest = buildReleaseBinariesManifest({ binDir: dir, tag: "v1.2.3" });
			expect(manifest.schema).toBe("gajae-release-binaries-v1");
			expect(manifest.release_version).toBe("1.2.3");
			expect(manifest.release_channel).toBe("stable");
			expect(manifest.binaries).toHaveLength(RELEASE_BINARY_NAMES.length);
			const sums = formatSha256Sums(manifest);
			expect(parseSha256Sums(sums, "gjc-linux-x64")).toBe(manifest.binaries[0]?.sha256);
			const written = writeReleaseBinariesManifest({ binDir: dir, tag: "v1.2.3" });
			expect(fs.existsSync(path.join(dir, BINARY_MANIFEST_FILE))).toBe(true);
			expect(fs.existsSync(path.join(dir, BINARY_SHA256_FILE))).toBe(true);
			expect(written.manifest.release_version).toBe("1.2.3");
		} finally {
			fs.rmSync(dir, { recursive: true, force: true });
		}
	});

	test("rejects a channel that does not match the tag", () => {
		expect(() =>
			buildReleaseBinariesManifest({ binDir: os.tmpdir(), tag: "v1.2.3", channel: "nightly" }),
		).toThrow("does not match tag");
	});
});
