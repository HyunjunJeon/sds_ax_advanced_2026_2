import { type AddressInfo, createServer, type Socket } from "node:net";
import { connectProxiedSocket, getProxyForUrl } from "../../src/utils/proxy";

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
type FixtureMode = "forbidden" | "fragmented" | "oversized" | "held";

const mode = (process.argv[2] as FixtureMode | undefined) ?? "forbidden";
const targetUrl = "https://198.51.100.7:8443";
const openSockets = new Set<Socket>();
let connectTarget: string | null = null;
let targetTlsBytes = 0;

const previousProxyEnv: Partial<Record<ProxyEnvKey, string | undefined>> = {};
for (const key of PROXY_ENV_KEYS) previousProxyEnv[key] = process.env[key];

function clearProxyEnv(): void {
	for (const key of PROXY_ENV_KEYS) delete process.env[key];
}

function restoreProxyEnv(): void {
	for (const key of PROXY_ENV_KEYS) {
		const value = previousProxyEnv[key];
		if (value === undefined) delete process.env[key];
		else process.env[key] = value;
	}
}

async function sendFragments(socket: Socket, response: Buffer, fragmentSize: number): Promise<void> {
	for (let offset = 0; offset < response.length && !socket.destroyed; offset += fragmentSize) {
		socket.write(response.subarray(offset, offset + fragmentSize));
		const { promise, resolve } = Promise.withResolvers<void>();
		setImmediate(resolve);
		await promise;
	}
}

const proxyServer = createServer(socket => {
	openSockets.add(socket);
	let request = "";
	let responded = false;

	const respondForbidden = () => {
		if (responded || socket.destroyed) return;
		responded = true;
		socket.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
	};

	const respondFragmented = async () => {
		if (responded || socket.destroyed) return;
		responded = true;
		const response = Buffer.from(
			"HTTP/1.1 200 Connection Established\r\nProxy-Agent: local-fixture\r\n\r\n",
			"latin1",
		);
		await sendFragments(socket, response, 3);
		// The client must cross the fragmented CONNECT response before it can send
		// its target TLS ClientHello. Close that local tunnel to force a deterministic
		// target-TLS failure without requiring a brittle certificate fixture.
		if (!socket.destroyed) socket.setTimeout(1_500, () => socket.destroy());
	};

	const respondOversized = async () => {
		if (responded || socket.destroyed) return;
		responded = true;
		const prefix = Buffer.from("HTTP/1.1 200 Connection Established\r\nX-Padding: ", "latin1");
		const padding = Buffer.alloc(64 * 1024, 0x78);
		await sendFragments(socket, Buffer.concat([prefix, padding]), 1_024);
		if (!socket.destroyed) socket.setTimeout(1_500, () => socket.destroy());
	};

	socket.on("error", () => {});
	socket.on("data", chunk => {
		if (responded) {
			if (mode === "fragmented" && targetTlsBytes === 0 && chunk.length > 0) {
				targetTlsBytes = chunk.length;
				socket.destroy();
			}
			return;
		}
		request += chunk.toString("latin1");
		const headerEnd = request.indexOf("\r\n\r\n");
		if (headerEnd < 0) return;
		const firstLine = request.slice(0, headerEnd).split("\r\n", 1)[0] ?? "";
		const [method, authority] = firstLine.split(" ");
		if (method === "CONNECT") connectTarget = authority ?? null;

		if (mode === "fragmented") void respondFragmented();
		else if (mode === "oversized") void respondOversized();
		else if (mode === "held") responded = true;
		else respondForbidden();
	});
	socket.on("close", () => openSockets.delete(socket));
});

const { promise: listening, resolve: resolveListening, reject: rejectListening } = Promise.withResolvers<void>();
proxyServer.once("error", rejectListening);
proxyServer.listen({ host: "127.0.0.1", port: 0 }, () => resolveListening());
await listening;

const address = proxyServer.address();
if (!address || typeof address === "string") throw new Error("fake proxy did not expose an address");
const proxyUrl = `http://127.0.0.1:${(address as AddressInfo).port}`;

async function closeProxyServer(): Promise<void> {
	for (const socket of openSockets) socket.destroy();
	if (!proxyServer.listening) return;
	const { promise, resolve } = Promise.withResolvers<void>();
	proxyServer.close(() => resolve());
	await promise;
}

async function connectError(options: { signal?: AbortSignal; timeoutMs?: number }): Promise<string> {
	try {
		const socket = await connectProxiedSocket(proxyUrl, targetUrl, options);
		socket.destroy();
		return "resolved unexpectedly";
	} catch (error) {
		return String(error);
	}
}

async function waitForSocketCleanup(): Promise<number> {
	const deadline = Date.now() + 500;
	while (openSockets.size > 0 && Date.now() < deadline) {
		const { promise, resolve } = Promise.withResolvers<void>();
		setTimeout(() => resolve(), 5);
		await promise;
	}
	return openSockets.size;
}

clearProxyEnv();
process.env.HTTPS_PROXY = proxyUrl;
let result: Record<string, unknown>;
try {
	const selectedProxy = getProxyForUrl("cursor", new URL(targetUrl));
	if (selectedProxy !== proxyUrl)
		throw new Error(`expected HTTPS_PROXY to be selected, got ${selectedProxy ?? "none"}`);

	if (mode === "fragmented") {
		const transportError = await connectError({ timeoutMs: 1_000 });
		result = { mode, proxyUrl, connectTarget, targetTlsBytes, transportError };
	} else if (mode === "oversized") {
		const transportError = await connectError({ timeoutMs: 1_000 });
		result = { mode, proxyUrl, connectTarget, transportError };
	} else if (mode === "held") {
		const timeoutError = await connectError({ timeoutMs: 150 });
		const abortController = new AbortController();
		const abortPromise = connectError({ signal: abortController.signal });
		setTimeout(() => abortController.abort(), 50);
		const abortError = await abortPromise;
		const openSocketsAfterCleanup = await waitForSocketCleanup();
		result = { mode, proxyUrl, connectTarget, timeoutError, abortError, openSocketsAfterCleanup };
	} else {
		const transportError = await connectError({ timeoutMs: 500 });
		result = { proxyUrl, connectTarget, transportError };
	}
} catch (error) {
	result = { mode, proxyUrl, connectTarget, fixtureError: String(error) };
} finally {
	restoreProxyEnv();
	await closeProxyServer();
}

console.log(JSON.stringify(result));
