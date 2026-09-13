import * as nodeWorkerThreads from "node:worker_threads";

type BootstrapRequest = { type: "start"; key: string; token: string; ready: SharedArrayBuffer } | { type: "close" };

const parentPort = nodeWorkerThreads.parentPort;
if (!parentPort) throw new Error("Coordinator sidecar bootstrap worker requires a parent port.");

let server: Bun.Server<undefined> | undefined;
let key: string | undefined;
let token: string | undefined;
let consumed = false;

parentPort.on("message", (request: BootstrapRequest) => {
	if (request.type === "close") {
		void server?.stop(true);
		server = undefined;
		parentPort?.close();
		return;
	}
	if (server) return;
	key = request.key;
	token = request.token;
	const ready = new Int32Array(request.ready);
	try {
		server = Bun.serve({
			hostname: "127.0.0.1",
			port: 0,
			fetch(request) {
				const url = new URL(request.url);
				if (request.method !== "GET" || url.pathname !== `/bootstrap/${token}`)
					return new Response("Not found", { status: 404 });
				if (consumed || !key) return new Response("Gone", { status: 410 });
				consumed = true;
				const response = new Response(key, { headers: { "cache-control": "no-store" } });
				key = undefined;
				token = undefined;
				setTimeout(() => {
					void server?.stop(true);
					server = undefined;
					parentPort?.close();
				}, 0);
				return response;
			},
		});
		Atomics.store(ready, 0, server.port ?? -1);
	} catch {
		Atomics.store(ready, 0, -1);
	}
	Atomics.notify(ready, 0);
});
