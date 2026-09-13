const sdkRunTokenBrand: unique symbol = Symbol("sdkRunToken");

export interface SdkRunCapability {
	readonly [sdkRunTokenBrand]: string;
}

export function createSdkRunCapability(token: string): SdkRunCapability {
	return { [sdkRunTokenBrand]: token };
}

export function readSdkRunCapability(value: unknown): string | undefined {
	if (!value || typeof value !== "object") return undefined;
	const token = (value as Partial<SdkRunCapability>)[sdkRunTokenBrand];
	return typeof token === "string" && token.length > 0 ? token : undefined;
}
