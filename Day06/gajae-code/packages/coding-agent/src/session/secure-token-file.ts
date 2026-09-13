import * as fs from "node:fs/promises";
import * as path from "node:path";
import { isEnoent } from "@gajae-code/utils";

function isPosix(): boolean {
	return process.platform !== "win32";
}

async function validateDirectory(directory: string): Promise<void> {
	await fs.mkdir(directory, { recursive: true, mode: 0o700 });
	const stat = await fs.lstat(directory);
	if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Credential token directory is unsafe.");
	if (isPosix()) {
		if (typeof process.getuid === "function" && stat.uid !== process.getuid()) {
			throw new Error("Credential token directory has an unexpected owner.");
		}
		if ((stat.mode & 0o077) !== 0) await fs.chmod(directory, 0o700);
		const repaired = await fs.lstat(directory);
		if ((repaired.mode & 0o077) !== 0) throw new Error("Credential token directory is not owner-only.");
	}
}

async function validateTokenPath(file: string): Promise<boolean> {
	let stat: Awaited<ReturnType<typeof fs.lstat>>;
	try {
		stat = await fs.lstat(file);
	} catch (error) {
		if (isEnoent(error)) return false;
		throw error;
	}
	if (stat.isSymbolicLink() || !stat.isFile()) throw new Error("Credential token path is unsafe.");
	if (isPosix()) {
		if (typeof process.getuid === "function" && stat.uid !== process.getuid()) {
			throw new Error("Credential token file has an unexpected owner.");
		}
		if ((stat.mode & 0o077) !== 0) await fs.chmod(file, 0o600);
		const repaired = await fs.lstat(file);
		if ((repaired.mode & 0o077) !== 0) throw new Error("Credential token file is not owner-only.");
	}
	return true;
}

export async function readSecureTokenFile(file: string): Promise<string | null> {
	await validateDirectory(path.dirname(file));
	if (!(await validateTokenPath(file))) return null;
	const handle = await fs.open(file, "r");
	try {
		const opened = await handle.stat();
		if (!opened.isFile()) throw new Error("Credential token path is unsafe.");
		const value = (await handle.readFile("utf8")).trim();
		return value || null;
	} finally {
		await handle.close();
	}
}

export async function writeSecureTokenFile(file: string, token: string): Promise<void> {
	await validateDirectory(path.dirname(file));
	if (await validateTokenPath(file)) {
		await fs.writeFile(file, token, { mode: 0o600 });
	} else {
		await fs.writeFile(file, token, { flag: "wx", mode: 0o600 });
	}
	if (isPosix()) await fs.chmod(file, 0o600);
}

export async function createSecureTokenFileExclusive(file: string, token: string): Promise<boolean> {
	await validateDirectory(path.dirname(file));
	try {
		await fs.writeFile(file, token, { flag: "wx", mode: 0o600 });
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "EEXIST") {
			await validateTokenPath(file);
			return false;
		}
		throw error;
	}
	if (isPosix()) await fs.chmod(file, 0o600);
	return true;
}
