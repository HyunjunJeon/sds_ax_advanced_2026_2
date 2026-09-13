import * as path from "node:path";

export const SESSION_STAGING_DIRNAME = ".staging" as const;

/** True when a path is an immediate child of the reserved session staging directory. */
export function isStagedSessionPath(filePath: string): boolean {
	return path.basename(path.dirname(path.resolve(filePath))) === SESSION_STAGING_DIRNAME;
}
