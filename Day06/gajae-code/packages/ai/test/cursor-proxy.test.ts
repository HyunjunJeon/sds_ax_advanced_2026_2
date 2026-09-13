import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { mapH2TransportError } from "../src/providers/cursor";
import { getProxyForUrl } from "../src/utils/proxy";

const PROXY_ENV_KEYS = [
	"PI_PROXY_CURSOR",
	"PI_PROXY",
	"HTTP_PROXY",
	"http_proxy",
	"HTTPS_PROXY",
	"https_proxy",
	"ALL_PROXY",
	"all_proxy",
	"NO_PROXY",
	"no_proxy",
] as const;

type ProxyEnvKey = (typeof PROXY_ENV_KEYS)[number];

function withProxyEnv<T>(values: Partial<Record<ProxyEnvKey, string | undefined>>, fn: () => T): T {
	const previous: Partial<Record<ProxyEnvKey, string | undefined>> = {};
	for (const key of PROXY_ENV_KEYS) previous[key] = process.env[key];

	try {
		for (const key of PROXY_ENV_KEYS) delete process.env[key];
		for (const [key, value] of Object.entries(values) as [ProxyEnvKey, string | undefined][]) {
			if (value !== undefined) process.env[key] = value;
		}
		return fn();
	} finally {
		for (const key of PROXY_ENV_KEYS) {
			const value = previous[key];
			if (value === undefined) delete process.env[key];
			else process.env[key] = value;
		}
	}
}

type ProxyFixtureResult = {
	mode?: string;
	proxyUrl: string;
	connectTarget: string | null;
	targetTlsBytes?: number;
	transportError?: string;
	timeoutError?: string;
	abortError?: string;
	openSocketsAfterCleanup?: number;
	fixtureError?: string;
};

async function runProxyFixture(mode?: "fragmented" | "oversized" | "held"): Promise<ProxyFixtureResult> {
	const fixture = path.join(import.meta.dir, "fixtures", "cursor-proxy-env.ts");
	const env: Record<string, string> = {};
	for (const [key, value] of Object.entries(process.env)) {
		if (value !== undefined) env[key] = value;
	}

	const args = mode ? [process.execPath, fixture, mode] : [process.execPath, fixture];
	const proc = Bun.spawn(args, {
		cwd: path.resolve(import.meta.dir, ".."),
		env,
		stdout: "pipe",
		stderr: "pipe",
	});
	const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
	const exitCode = await proc.exited;
	if (exitCode !== 0) throw new Error(`Cursor proxy fixture failed (${exitCode}): ${stderr}`);
	return JSON.parse(stdout.trim()) as ProxyFixtureResult;
}

describe("Cursor proxy environment resolution", () => {
	it("uses provider, global, protocol, and ALL_PROXY precedence in order", () => {
		const httpsUrl = new URL("https://api.example.com/v1");
		const httpUrl = new URL("http://api.example.com/v1");

		expect(
			withProxyEnv(
				{
					PI_PROXY_CURSOR: "http://provider.proxy:8080",
					PI_PROXY: "http://global.proxy:8080",
					HTTPS_PROXY: "http://https.proxy:8080",
					ALL_PROXY: "http://all.proxy:8080",
				},
				() => getProxyForUrl("cursor", httpsUrl),
			),
		).toBe("http://provider.proxy:8080");

		expect(
			withProxyEnv(
				{
					PI_PROXY: "http://global.proxy:8080",
					HTTPS_PROXY: "http://https.proxy:8080",
					ALL_PROXY: "http://all.proxy:8080",
				},
				() => getProxyForUrl("cursor", httpsUrl),
			),
		).toBe("http://global.proxy:8080");

		expect(
			withProxyEnv(
				{
					HTTPS_PROXY: "http://https.proxy:8080",
					ALL_PROXY: "http://all.proxy:8080",
				},
				() => getProxyForUrl("cursor", httpsUrl),
			),
		).toBe("http://https.proxy:8080");

		expect(withProxyEnv({ ALL_PROXY: "http://all.proxy:8080" }, () => getProxyForUrl("cursor", httpsUrl))).toBe(
			"http://all.proxy:8080",
		);

		expect(
			withProxyEnv({ HTTP_PROXY: "http://http.proxy:8080", ALL_PROXY: "http://all.proxy:8080" }, () =>
				getProxyForUrl("cursor", httpUrl),
			),
		).toBe("http://http.proxy:8080");

		expect(
			withProxyEnv({ https_proxy: "http://lowercase.proxy:8080" }, () => getProxyForUrl("cursor", httpsUrl)),
		).toBe("http://lowercase.proxy:8080");
	});

	it("bypasses proxies for NO_PROXY hosts and local/private targets", () => {
		expect(
			withProxyEnv({ PI_PROXY_CURSOR: "http://proxy.example:8080", NO_PROXY: "api.example.com" }, () =>
				getProxyForUrl("cursor", new URL("https://api.example.com/v1")),
			),
		).toBeUndefined();

		expect(
			withProxyEnv({ PI_PROXY_CURSOR: "http://proxy.example:8080", NO_PROXY: "*" }, () =>
				getProxyForUrl("cursor", new URL("https://anywhere.example/v1")),
			),
		).toBeUndefined();

		expect(
			withProxyEnv({ PI_PROXY_CURSOR: "http://proxy.example:8080" }, () =>
				getProxyForUrl("cursor", new URL("https://localhost:8443/v1")),
			),
		).toBeUndefined();

		expect(
			withProxyEnv({ PI_PROXY_CURSOR: "http://proxy.example:8080" }, () =>
				getProxyForUrl("cursor", new URL("https://192.168.1.20:8443/v1")),
			),
		).toBeUndefined();

		for (const target of [
			"https://0.0.0.0:8443/v1",
			"https://0.255.255.255:8443/v1",
			"https://[::]:8443/v1",
			"https://localhost.localdomain:8443/v1",
		]) {
			expect(
				withProxyEnv({ PI_PROXY_CURSOR: "http://proxy.example:8080" }, () =>
					getProxyForUrl("cursor", new URL(target)),
				),
			).toBeUndefined();
		}
	});

	it("returns undefined when no proxy environment is configured", () => {
		expect(withProxyEnv({}, () => getProxyForUrl("cursor", new URL("https://api.example.com/v1")))).toBeUndefined();
	});
});

describe("Cursor HTTP/2 transport error mapping", () => {
	it("maps the unsupported-HTTP/2 proxy failure with actionable transport context", () => {
		const original = Object.assign(new Error("H2 is not supported by this proxy"), {
			code: "ERR_HTTP2_ERROR",
		});
		const baseUrl = "https://api2.cursor.sh";
		const mapped = mapH2TransportError(original, baseUrl) as Error & { cause?: unknown };

		expect(mapped).not.toBe(original);
		expect(mapped).toBeInstanceOf(Error);
		expect(mapped.cause).toBe(original);
		expect(String(mapped)).toContain(baseUrl);
		expect(String(mapped)).toMatch(/ALPN/i);
		expect(String(mapped)).toContain("providers.cursor.baseUrl");
	});

	it("passes through unrelated HTTP/2 and non-HTTP/2 errors unchanged", () => {
		const unrelatedHttp2 = Object.assign(new Error("HTTP/2 protocol error"), { code: "ERR_HTTP2_ERROR" });
		const unrelatedTransport = Object.assign(new Error("H2 is not supported"), { code: "ECONNRESET" });

		expect(mapH2TransportError(unrelatedHttp2, "https://api2.cursor.sh")).toBe(unrelatedHttp2);
		expect(mapH2TransportError(unrelatedTransport, "https://api2.cursor.sh")).toBe(unrelatedTransport);
	});
});

describe("Cursor HTTPS proxy CONNECT transport", () => {
	it("uses HTTPS_PROXY-only through a fake CONNECT proxy instead of direct-connect", async () => {
		const result = await runProxyFixture();
		expect(result.proxyUrl).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
		expect(result.connectTarget).toBe("198.51.100.7:8443");
		expect(result.transportError).toMatch(/403|proxy|connect/i);
	});

	it("parses fragmented bounded CONNECT headers and reaches target TLS", async () => {
		const result = await runProxyFixture("fragmented");
		expect(result.proxyUrl).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
		expect(result.connectTarget).toBe("198.51.100.7:8443");
		expect(result.targetTlsBytes ?? 0).toBeGreaterThan(0);
		expect(result.transportError).toMatch(/TLS|handshake|closed/i);
	});

	it("rejects oversized CONNECT headers before the held-response timeout", async () => {
		const result = await runProxyFixture("oversized");
		expect(result.proxyUrl).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
		expect(result.connectTarget).toBe("198.51.100.7:8443");
		expect(result.transportError).toMatch(/headers too large/i);
	});

	it("cleans up held CONNECT sockets on timeout and abort", async () => {
		const result = await runProxyFixture("held");
		expect(result.proxyUrl).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
		expect(result.connectTarget).toBe("198.51.100.7:8443");
		expect(result.timeoutError).toMatch(/timed out/i);
		expect(result.abortError).toMatch(/aborted/i);
		expect(result.openSocketsAfterCleanup).toBe(0);
	});
});
