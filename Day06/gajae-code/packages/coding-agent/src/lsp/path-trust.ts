import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { CONFIG_DIR_NAME, pathIsWithin, relativePathEscapesRoot } from "@gajae-code/utils";

function normalizePathForComparison(candidate: string): string {
	const resolved = path.resolve(candidate);
	return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function pathIsLexicallyWithin(root: string, candidate: string): boolean {
	const relative = path.relative(normalizePathForComparison(root), normalizePathForComparison(candidate));
	return !relativePathEscapesRoot(relative);
}

function canonicalPath(candidate: string): string {
	try {
		return fs.realpathSync(candidate);
	} catch {
		return path.resolve(candidate);
	}
}

function canonicalParentPath(candidate: string): string {
	const resolved = path.resolve(candidate);
	return path.join(canonicalPath(path.dirname(resolved)), path.basename(resolved));
}

function isDirectory(candidate: string): boolean {
	try {
		return fs.statSync(candidate).isDirectory();
	} catch {
		return false;
	}
}

function pathMatchesStop(candidate: string, stopPaths: ReadonlySet<string>): boolean {
	return (
		stopPaths.has(normalizePathForComparison(candidate)) ||
		stopPaths.has(normalizePathForComparison(canonicalPath(candidate)))
	);
}

function findProjectTrustRoot(start: string, stopPaths: ReadonlySet<string>): string | undefined {
	const fallback = path.resolve(start);
	let nearestConfigRoot: string | undefined;
	let current = fallback;
	for (;;) {
		if (pathMatchesStop(current, stopPaths)) {
			return current === fallback ? undefined : (nearestConfigRoot ?? fallback);
		}
		if (fs.existsSync(path.join(current, ".git"))) return current;
		if (nearestConfigRoot === undefined && isDirectory(path.join(current, CONFIG_DIR_NAME))) {
			nearestConfigRoot = current;
		}
		const parent = path.dirname(current);
		if (parent === current) return nearestConfigRoot ?? fallback;
		current = parent;
	}
}

function isProjectMarkerRoot(root: string): boolean {
	return fs.existsSync(path.join(root, ".git")) || isDirectory(path.join(root, CONFIG_DIR_NAME));
}

/**
 * Spellings of `candidate` that differ only in how HOME is named: the path as
 * given, plus the same path with any ancestor that is HOME under another name
 * replaced by canonical HOME. The suffix beneath HOME is kept as spelled, and an
 * alias that lives inside a project (`repo/home-link -> HOME`) is project
 * content, not a HOME spelling, so it is never expanded.
 */
function candidateSpellings(candidate: string, canonicalHome: string, projectRoot: string | undefined): string[] {
	const resolved = path.resolve(candidate);
	const spellings = [resolved];
	const normalizedHome = normalizePathForComparison(canonicalHome);
	let current = path.dirname(resolved);
	for (;;) {
		if (
			normalizePathForComparison(current) !== normalizedHome &&
			(projectRoot === undefined || !pathIsLexicallyWithin(projectRoot, current)) &&
			normalizePathForComparison(canonicalPath(current)) === normalizedHome
		) {
			spellings.push(path.join(canonicalHome, path.relative(current, resolved)));
		}
		const parent = path.dirname(current);
		if (parent === current) return spellings;
		current = parent;
	}
}

export function isProjectControlledPath(candidate: string, cwd: string): boolean {
	const home = os.homedir();
	const canonicalHome = canonicalPath(home);
	const stopPaths = new Set([path.resolve(home), canonicalHome].map(normalizePathForComparison));
	const lexicalTrustRoot = findProjectTrustRoot(cwd, stopPaths);
	// A trust root outside HOME must not claim user executables inside HOME
	// (e.g. ~/.gjc/bin); a root under HOME still owns what it contains. Each check
	// judges home scope on the same view of the path it inspects, without
	// dereferencing the candidate's final component: mixing views let a project
	// link into HOME, or a HOME link into the project, escape rejection. HOME may
	// be spelled lexically or canonically on either side, so the lexical check
	// runs over every HOME-alias spelling of both the candidate and the root.
	if (lexicalTrustRoot !== undefined) {
		const trustRootIsHomeScoped = pathIsWithin(canonicalHome, lexicalTrustRoot);
		// A root under HOME may legitimately be reached through a HOME alias, and a
		// bare fallback root (no project marker) owns nothing on its own; a real
		// project root outside HOME owns every alias directory it contains.
		const aliasBoundary =
			!trustRootIsHomeScoped && isProjectMarkerRoot(lexicalTrustRoot) ? lexicalTrustRoot : undefined;
		const rootSpellings = candidateSpellings(lexicalTrustRoot, canonicalHome, undefined);
		const spellings = candidateSpellings(candidate, canonicalHome, aliasBoundary);
		// Every spelling names the same location, so home scope is a property of
		// the location: one spelling under HOME makes all of them HOME-scoped.
		const candidateIsHomeScoped = spellings.some(
			spelling => pathIsLexicallyWithin(home, spelling) || pathIsLexicallyWithin(canonicalHome, spelling),
		);
		if (
			(!candidateIsHomeScoped || trustRootIsHomeScoped) &&
			spellings.some(spelling => rootSpellings.some(root => pathIsLexicallyWithin(root, spelling)))
		) {
			return true;
		}
	}

	const canonicalCandidate = canonicalPath(candidate);
	const canonicalCandidateParent = canonicalParentPath(candidate);
	const parentIsHomeScoped = pathIsLexicallyWithin(canonicalHome, canonicalCandidateParent);
	const targetIsHomeScoped = pathIsWithin(canonicalHome, canonicalCandidate);
	const canonicalTrustRoots = new Set<string>();
	if (lexicalTrustRoot !== undefined) canonicalTrustRoots.add(canonicalPath(lexicalTrustRoot));
	const canonicalTrustRoot = findProjectTrustRoot(canonicalPath(cwd), stopPaths);
	if (canonicalTrustRoot !== undefined) canonicalTrustRoots.add(canonicalPath(canonicalTrustRoot));
	for (const trustRoot of canonicalTrustRoots) {
		const trustRootIsHomeScoped = pathIsLexicallyWithin(canonicalHome, trustRoot);
		const parentOwned =
			pathIsLexicallyWithin(trustRoot, canonicalCandidateParent) && (!parentIsHomeScoped || trustRootIsHomeScoped);
		const targetOwned = pathIsWithin(trustRoot, canonicalCandidate) && (!targetIsHomeScoped || trustRootIsHomeScoped);
		if (parentOwned || targetOwned) return true;
	}
	return false;
}
