const AWS_REGION_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/**
 * Fail closed before an AWS region is interpolated into a credential-bearing
 * service authority. Region names are one lowercase ASCII DNS label; dots,
 * URL delimiters, Unicode, whitespace, and controls must never reach URL
 * parsing as part of that label.
 */
export function assertAwsRegionLabel(region: unknown): asserts region is string {
	const match = typeof region === "string" ? AWS_REGION_LABEL.exec(region) : undefined;
	if (!match || match[0] !== region) {
		throw new Error("Invalid AWS region: expected a lowercase ASCII DNS label.");
	}
}
