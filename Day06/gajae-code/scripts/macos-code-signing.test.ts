import { describe, expect, test } from "bun:test";
import {
	GJC_MACOS_CODE_SIGNING_IDENTIFIER,
	GJC_MACOS_DESIGNATED_REQUIREMENT,
	buildMacOSAdHocCodeSignCommand,
	signMacOSBinary,
} from "./macos-code-signing";

describe("macOS GJC code signing", () => {
	test("uses a stable identifier requirement instead of the binary CDHash", () => {
		const command = buildMacOSAdHocCodeSignCommand("/tmp/gjc.req", "/tmp/gjc");

		expect(command).toEqual([
			"codesign",
			"--force",
			"--sign",
			"-",
			"--identifier",
			GJC_MACOS_CODE_SIGNING_IDENTIFIER,
			"--requirements",
			"/tmp/gjc.req",
			"/tmp/gjc",
		]);
		expect(GJC_MACOS_DESIGNATED_REQUIREMENT).toBe(
			`designated => identifier "${GJC_MACOS_CODE_SIGNING_IDENTIFIER}"`,
		);
	});

	test("removes the temporary requirement after signing succeeds", async () => {
		let command: string[] | undefined;
		let requirement: string | undefined;
		await signMacOSBinary("/tmp/gjc", async value => {
			command = value;
			requirement = await Bun.file(value[7]!).text();
		});

		expect(command?.[5]).toBe(GJC_MACOS_CODE_SIGNING_IDENTIFIER);
		expect(requirement).toBe(`${GJC_MACOS_DESIGNATED_REQUIREMENT}\n`);
		expect(command?.[7] ? await Bun.file(command[7]).exists() : true).toBe(false);
	});

	test("removes the temporary requirement when signing fails", async () => {
		let requirementPath: string | undefined;
		await expect(
			signMacOSBinary("/tmp/gjc", async command => {
				requirementPath = command[7];
				throw new Error("codesign failed");
			}),
		).rejects.toThrow("codesign failed");

		expect(requirementPath ? await Bun.file(requirementPath).exists() : true).toBe(false);
	});
});
