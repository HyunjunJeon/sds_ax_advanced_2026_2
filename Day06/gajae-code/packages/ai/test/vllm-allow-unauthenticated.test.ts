import { describe, expect, test } from "bun:test";
import { PROVIDER_DESCRIPTORS } from "../src/provider-models/descriptors";

function requireDescriptor(providerId: string) {
	const descriptor = PROVIDER_DESCRIPTORS.find(d => d.providerId === providerId);
	if (!descriptor) throw new Error(`descriptor not found for providerId "${providerId}"`);
	return descriptor;
}

describe("vllm descriptor allowUnauthenticated", () => {
	test("vllm descriptor allows unauthenticated runtime discovery", () => {
		const vllm = requireDescriptor("vllm");

		expect(vllm.allowUnauthenticated).toBe(true);
	});

	test("vllm descriptor matches lm-studio's local-provider allowUnauthenticated intent", () => {
		const vllm = requireDescriptor("vllm");
		const lmStudio = requireDescriptor("lm-studio");

		expect(vllm.allowUnauthenticated).toBe(lmStudio.allowUnauthenticated);
		expect(lmStudio.allowUnauthenticated).toBe(true);
	});

	test("vllm catalog discovery remains unauthenticated-friendly", () => {
		const vllm = requireDescriptor("vllm");

		expect(vllm.catalogDiscovery?.allowUnauthenticated).toBe(true);
	});
});
