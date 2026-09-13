import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { BlobStore, EphemeralBlobStore, isBlobRef, parseBlobRef } from "../src/session/blob-store";

const roots: string[] = [];
const CANONICAL_HASH = "a".repeat(64);

function makeStore(): { root: string; store: BlobStore } {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "gjc-blob-ref-boundary-"));
	roots.push(root);
	return { root, store: new BlobStore(path.join(root, "blobs")) };
}

afterEach(() => {
	for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true });
});

describe("canonical blob reference boundary", () => {
	test("accepts only an exact lowercase SHA-256 reference", () => {
		const validRef = `blob:sha256:${CANONICAL_HASH}`;
		expect(isBlobRef(validRef)).toBe(true);
		expect(parseBlobRef(validRef)).toBe(CANONICAL_HASH);

		for (const invalidRef of [
			"blob:sha256:",
			"blob:sha256:abc",
			`blob:sha256:${"A".repeat(64)}`,
			`blob:sha256:${CANONICAL_HASH}\n`,
			`blob:sha256:${CANONICAL_HASH}/child`,
			"blob:sha256:../sentinel.txt",
			"blob:sha256:..\\sentinel.txt",
		]) {
			expect(isBlobRef(invalidRef)).toBe(false);
			expect(parseBlobRef(invalidRef)).toBeNull();
		}
	});

	test("valid missing hashes retain null and false degradation", async () => {
		const { store } = makeStore();
		await expect(store.get(CANONICAL_HASH)).resolves.toBeNull();
		expect(store.getSync(CANONICAL_HASH)).toBeNull();
		expect(store.getCheckedSync(CANONICAL_HASH)).toBeNull();
		await expect(store.has(CANONICAL_HASH)).resolves.toBe(false);
	});

	test("disk-backed lookups reject noncanonical names before resolving a path", async () => {
		const { root, store } = makeStore();
		fs.writeFileSync(path.join(root, "sentinel.txt"), "outside");

		await expect(store.get("../sentinel.txt")).resolves.toBeNull();
		expect(store.getSync("../sentinel.txt")).toBeNull();
		expect(store.getCheckedSync("../sentinel.txt")).toBeNull();
		await expect(store.has("../sentinel.txt")).resolves.toBe(false);
	});

	test("ephemeral disk-backed lookup rejects noncanonical cache keys", () => {
		const root = fs.mkdtempSync(path.join(os.tmpdir(), "gjc-blob-ref-ephemeral-"));
		roots.push(root);
		const store = new EphemeralBlobStore(path.join(root, "blobs"));
		fs.writeFileSync(path.join(root, "sentinel.txt"), "outside");

		expect(store.getSync("../sentinel.txt")).toBeNull();
	});
});
