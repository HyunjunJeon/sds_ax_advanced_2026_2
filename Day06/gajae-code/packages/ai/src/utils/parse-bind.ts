/**
 * Shared `host:port` parser used by the auth-broker and auth-gateway boot
 * paths. Centralized so the two servers can't drift on what they accept (the
 * gateway used to silently allow empty hostnames; this fixes it).
 */
import { isIP } from "node:net";

export interface ParsedBind {
	hostname: string;
	port: number;
}

function parsePort(raw: string, bind: string): number {
	if (!/^\d+$/.test(raw)) {
		throw new Error(`Invalid bind '${bind}'; port must be an integer.`);
	}
	const port = Number.parseInt(raw, 10);
	if (!Number.isFinite(port) || port < 0 || port > 65535) {
		throw new Error(`Invalid bind '${bind}'; port out of range.`);
	}
	return port;
}

/**
 * Parse a `host:port` (or bare `port`, which assumes loopback) string.
 *
 * Accepts:
 *   - `"4000"`            → `127.0.0.1:4000`
 *   - `"0.0.0.0:4000"`    → as written
 *   - `"[::1]:4000"`      → as written (brackets retained, Bun handles them)
 *
 * Rejects:
 *   - empty input
 *   - empty hostname (`":4000"`)
 *   - non-integer / out-of-range port
 */
export function parseBind(raw: string): ParsedBind {
	const trimmed = raw.trim();
	if (trimmed.length === 0) {
		throw new Error("Invalid bind; expected 'host:port' or 'port'.");
	}
	if (/^\d+$/.test(trimmed)) {
		return { hostname: "127.0.0.1", port: parsePort(trimmed, raw) };
	}
	const lastColon = trimmed.lastIndexOf(":");
	if (lastColon < 0) {
		throw new Error(`Invalid bind '${raw}'; expected 'host:port' or 'port'.`);
	}
	const hostPart = trimmed.slice(0, lastColon);
	const portPart = trimmed.slice(lastColon + 1);
	if (hostPart.length === 0) {
		throw new Error(`Invalid bind '${raw}'; host must not be empty.`);
	}
	return { hostname: hostPart, port: parsePort(portPart, raw) };
}

/** True for loopback-only hostnames the auth servers may bind without credentials. */
export function isLoopbackHostname(hostname: string): boolean {
	const normalized = hostname
		.trim()
		.toLowerCase()
		.replace(/^\[|\]$/g, "");
	if (normalized === "localhost" || normalized === "::1") return true;
	// Strict numeric IPv4 loopback literals only: a bare prefix match would
	// accept attacker-controlled names like `127.evil.example`, and short/hex
	// IPv4 forms (`127.1`, `0x7f.1`) are rejected by the parser anyway.
	return isIP(normalized) === 4 && normalized.split(".")[0] === "127";
}

/**
 * Fail closed when an unauthenticated auth server (empty bearer token set)
 * would bind a non-loopback address: that exposes credential operations to the
 * network with no proof of possession.
 */
export function assertAuthenticatedOrLoopback(bind: ParsedBind, bearerTokenCount: number, serverName: string): void {
	if (bearerTokenCount > 0) return;
	if (isLoopbackHostname(bind.hostname)) return;
	throw new Error(
		`${serverName} refuses to bind ${bind.hostname}:${bind.port} without bearer tokens; unauthenticated mode is loopback-only.`,
	);
}
