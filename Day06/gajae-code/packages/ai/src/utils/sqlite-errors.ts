/** Return whether an error carries a SQLite result code. */
export function isSqliteError(error: unknown): boolean {
	if (!error || typeof error !== "object") return false;
	const code = (error as { code?: unknown }).code;
	return typeof code === "string" && code.startsWith("SQLITE_");
}

/** Return whether an error is one of SQLite's explicit database-corruption classes. */
export function isSqliteCorruptionError(error: unknown): boolean {
	if (!isSqliteError(error)) return false;
	const code = (error as { code?: unknown }).code;
	return code === "SQLITE_CORRUPT" || code === "SQLITE_NOTADB";
}
