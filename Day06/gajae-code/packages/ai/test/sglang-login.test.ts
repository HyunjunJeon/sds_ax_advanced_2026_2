import { describe, expect, it } from "bun:test";
import { loginSglang } from "../src/utils/oauth/sglang";

describe("SGLang login", () => {
	it("stores a trimmed API key and opens the current official server-argument docs", async () => {
		let authUrl = "";
		let allowEmpty: boolean | undefined;
		const apiKey = await loginSglang({
			onAuth: info => {
				authUrl = info.url;
			},
			onPrompt: async prompt => {
				allowEmpty = prompt.allowEmpty;
				return "  test-sglang-key  ";
			},
		});

		expect(authUrl).toBe("https://docs.sglang.io/docs/advanced_features/server_arguments.html");
		expect(allowEmpty).toBe(false);
		expect(apiKey).toBe("test-sglang-key");
	});

	it("rejects an empty value instead of persisting a no-auth sentinel", async () => {
		await expect(
			loginSglang({
				onAuth: () => {},
				onPrompt: async () => "   ",
			}),
		).rejects.toThrow("SGLang API key is required");
	});
});
