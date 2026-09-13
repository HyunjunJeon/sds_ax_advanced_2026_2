import { describe, expect, test } from "bun:test";
import { AuthStorage, SqliteAuthCredentialStore } from "../src/auth-storage";

describe("AuthStorage disabled cause safety", () => {
	test("scrubs and bounds disabled causes before persistence and inventory", async () => {
		const store = await SqliteAuthCredentialStore.open(":memory:");
		store.saveOAuth("anthropic", {
			access: "access",
			refresh: "refresh",
			expires: Date.now() + 60_000,
			email: "operator@example.com",
		});
		const storage = new AuthStorage(store);
		await storage.reload();
		try {
			const id = storage.listCredentialInventory("anthropic")[0]!.id;
			const secret = "super-secret-token";
			storage.disableCredentialById(
				id,
				`Authorization=Bearer ${secret} token=${secret} https://example.test/path?access_token=${secret} ${"x".repeat(400)}`,
			);
			const cause = storage.listCredentialInventory("anthropic")[0]!.disabledCause ?? "";
			expect(cause).not.toContain(secret);
			expect(cause).not.toContain("access_token=");
			expect(cause.length).toBeLessThanOrEqual(240);
			expect(cause).toContain("[redacted]");
		} finally {
			storage.close();
		}
	});
});
