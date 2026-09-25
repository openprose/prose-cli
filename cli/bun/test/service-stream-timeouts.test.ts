// Transport.openStream bounds only the wait for response
// headers (stream.connectTimeoutMs) and the gap between chunks
// (stream.idleTimeoutMs), exactly as the Rust port does through ureq
// timeout_connect/timeout_read. It must never bound the whole exchange: a live
// run streams for minutes, and aborting at the connect limit detached every
// run longer than 30 s (and could resubmit once when no event had arrived).
// Fixtures cannot express time, so these tests drive the real fetch path
// against a local Bun.serve with the limits shortened on the Transport.
import { afterAll, describe, expect, test } from "bun:test";
import { PRODUCTION } from "../src/core/service/endpoint";
import { manifest, type Environment } from "../src/core/service/manifest";
import { Transport, type Request } from "../src/core/service/http";
import { RunnerFailure } from "../src/core/types";

const encoder = new TextEncoder();
const never = new Promise<Response>(() => {});

const server = Bun.serve({
  port: 0,
  idleTimeout: 0,
  fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === "/stall-headers") return never;
    if (path === "/heartbeats") {
      // 10 heartbeats 40 ms apart (~400 ms) then one event, then close.
      let sent = 0;
      return new Response(new ReadableStream({
        async pull(controller) {
          await Bun.sleep(40);
          if (sent === 10) {
            controller.enqueue(encoder.encode("event: done\ndata: {}\n\n"));
            controller.close();
            return;
          }
          sent += 1;
          controller.enqueue(encoder.encode(": heartbeat\n\n"));
        },
      }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    if (path === "/silent") {
      return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoder.encode(": hello\n\n")); } }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    return new Response("not found", { status: 404 });
  },
});
afterAll(() => { void server.stop(true); });

const environment: Environment = { ...PRODUCTION, origin: `http://127.0.0.1:${server.port}` };

function transport(connectMs: number, idleMs: number): Transport {
  const value = new Transport(environment, new AbortController());
  value.streamTimeouts = { connectMs, idleMs };
  return value;
}

function request(path: string): Request {
  return { method: "GET", template: "/run/{runId}/events", path, query: [], headers: [], bearer: false, class: "stream" };
}

async function drain(open: Awaited<ReturnType<Transport["openStream"]>>): Promise<string[]> {
  if (open.kind !== "events") return [open.kind];
  const seen: string[] = [];
  for (;;) {
    const item = await open.reader.next();
    if (item.kind === "end") { seen.push(`end:${item.end}`); return seen; }
    seen.push(item.event.event);
  }
}

// The admission runner (cli/ci/run_local.py) isolates the network with a dead
// HTTP(S)_PROXY/ALL_PROXY and an empty NO_PROXY, which Bun reads once at
// startup, so loopback requests would go to the dead proxy. Under such a proxy,
// rerun this file in a child whose NO_PROXY exempts only loopback.
const proxied = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
  .some((name) => (process.env[name] ?? "") !== "");
const loopbackExempt = /(^|,)\s*(127\.0\.0\.1|\*)\s*(,|$)/.test(process.env.NO_PROXY ?? process.env.no_proxy ?? "");

if (proxied && !loopbackExempt) {
  describe("stream transport timeouts (loopback exempt from the isolation proxy)", () => {
    test("every stream timeout case passes against the real fetch path", () => {
      const child = Bun.spawnSync([process.execPath, "--no-env-file", "test", import.meta.path], {
        env: { ...process.env, NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" },
        stdout: "pipe", stderr: "pipe",
      });
      const output = `${child.stdout.toString()}${child.stderr.toString()}`;
      expect(output).toContain(" 4 pass");
      expect(child.exitCode).toBe(0);
    }, 60_000);
  });
} else describe("stream transport timeouts", () => {
  test("defaults come from manifest transportClasses.stream", () => {
    const value = new Transport(environment, new AbortController());
    expect(value.streamTimeouts).toEqual({
      connectMs: Number(manifest.transportClasses.stream?.connectTimeoutMs),
      idleMs: Number(manifest.transportClasses.stream?.idleTimeoutMs),
    });
    expect(value.streamTimeouts).toEqual({ connectMs: 30000, idleMs: 45000 });
  });

  test("the connect timeout does not cut off a stream that keeps sending heartbeats", async () => {
    // ~400 ms of heartbeats with a 100 ms connect and a 300 ms idle budget.
    const seen = await drain(await transport(100, 300).openStream(request("/heartbeats"), undefined));
    expect(seen).toEqual(["done", "end:closed"]);
  });

  test("a stream that goes silent ends idle after the idle timeout", async () => {
    const started = performance.now();
    const seen = await drain(await transport(10_000, 150).openStream(request("/silent"), undefined));
    expect(seen).toEqual(["end:idle"]);
    expect(performance.now() - started).toBeLessThan(3000);
  });

  test("a server that never sends headers is a dropped stream after the connect timeout", async () => {
    const started = performance.now();
    let outcome: string;
    try { outcome = (await transport(150, 10_000).openStream(request("/stall-headers"), undefined)).kind; }
    catch (caught) { outcome = caught instanceof RunnerFailure ? caught.code : String(caught); }
    expect(outcome).toBe("dropped");
    const elapsed = performance.now() - started;
    expect(elapsed).toBeGreaterThanOrEqual(140);
    expect(elapsed).toBeLessThan(3000);
  });
});
