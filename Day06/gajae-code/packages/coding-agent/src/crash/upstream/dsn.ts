/**
 * Sentry DSNs are configuration, but accepting a malformed one can turn an
 * opt-in crash relay into an unexpected network destination. Keep parsing
 * deliberately narrow and reconstruct public-key-bearing forms only at the
 * envelope boundary.
 */
export interface SentryDsn {
	readonly publicKey: string;
	readonly host: string;
	readonly projectId: string;
	readonly envelopeUrl: string;
}

const PUBLIC_KEY = /^[a-zA-Z0-9]+$/;
const PROJECT_ID = /^[0-9]+$/;

/** Recover the protocol and optional path prefix from the canonical endpoint. */
function endpointParts(dsn: SentryDsn): { protocol: string; prefix: string } | undefined {
	let endpoint: URL;
	try {
		endpoint = new URL(dsn.envelopeUrl);
	} catch {
		return undefined;
	}

	const segments = endpoint.pathname.split("/").filter(Boolean);
	if (
		segments.length < 3 ||
		segments.at(-3) !== "api" ||
		segments.at(-2) !== dsn.projectId ||
		segments.at(-1) !== "envelope"
	)
		return undefined;
	return {
		protocol: endpoint.protocol,
		prefix: segments
			.slice(0, -3)
			.map(segment => `/${segment}`)
			.join(""),
	};
}

/**
 * Parse only the public-key DSN form used by the relay. Secrets, query values,
 * fragments, and plaintext public destinations are all refused before a URL is
 * ever retained as a network target.
 */
export function parseSentryDsn(raw: string): SentryDsn | undefined {
	let url: URL;
	try {
		url = new URL(raw);
	} catch {
		return undefined;
	}

	if (url.protocol !== "https:" && url.protocol !== "http:") return undefined;
	if (url.protocol === "http:" && url.hostname !== "localhost" && url.hostname !== "127.0.0.1") return undefined;
	if (!url.username || !PUBLIC_KEY.test(url.username) || url.password || url.search || url.hash || !url.host)
		return undefined;

	const segments = url.pathname.split("/").filter(Boolean);
	const projectId = segments.at(-1);
	if (!projectId || !PROJECT_ID.test(projectId)) return undefined;
	const prefix = segments
		.slice(0, -1)
		.map(segment => `/${segment}`)
		.join("");

	return {
		publicKey: url.username,
		host: url.host,
		projectId,
		envelopeUrl: `${url.origin}${prefix}/api/${projectId}/envelope/`,
	};
}

/** Reconstruct the DSN required in the envelope header, nowhere else. */
export function toDsnString(dsn: SentryDsn): string {
	const parts = endpointParts(dsn);
	if (!parts) return "";
	return `${parts.protocol}//${dsn.publicKey}@${dsn.host}${parts.prefix}/${dsn.projectId}`;
}

/** Omit the public key from diagnostics so configuration can be logged safely. */
export function redactDsn(dsn: SentryDsn): string {
	const parts = endpointParts(dsn);
	if (!parts) return "<invalid dsn>";
	return `${parts.protocol}//${dsn.host}/${dsn.projectId}`;
}
