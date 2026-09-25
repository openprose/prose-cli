// Transport.download (run download, run show --file) must bound
// both the wait for response headers (download.connectTimeoutMs) and the gap
// between body chunks (download.idleTimeoutMs), exactly as the Rust port does
// through ureq timeout_connect/timeout_read. Fixtures cannot stall, so these
// tests drive the real fetch path against a local Bun.serve that stalls, with
// the timeouts shortened on the Transport instance.
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
    if (path === "/stall-body") {
      return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoder.encode("abc")); } }), { status: 200 });
    }
    if (path === "/stall-error-body") {
      return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoder.encode("{")); } }), { status: 500 });
    }
    if (path === "/slow-body") {
      let sent = 0;
      return new Response(new ReadableStream({
        async pull(controller) {
          await Bun.sleep(40);
          if (sent === 8) { controller.close(); return; }
          sent += 1;
          controller.enqueue(encoder.encode("x"));
        },
      }), { status: 200 });
    }
    return new Response("not found", { status: 404 });
  },
});
afterAll(() => { void server.stop(true); });

const environment: Environment = { ...PRODUCTION, origin: `http://127.0.0.1:${server.port}` };

function transport(connectMs: number, idleMs: number, cancellation = new AbortController()): Transport {
  const value = new Transport(environment, cancellation);
  value.downloadTimeouts = { connectMs, idleMs };
  return value;
}

function request(path: string): Request {
  return { method: "GET", template: "/runs/{runId}/files/{path}", path, query: [], headers: [], bearer: false, class: "download" };
}

async function code(promise: Promise<unknown>): Promise<string> {
  try { await promise; return "OK"; }
  catch (caught) { return caught instanceof RunnerFailure ? caught.code : `THREW ${String(caught)}`; }
}

// The admission runner (cli/ci/run_local.py) isolates the network with a dead
// HTTP(S)_PROXY/ALL_PROXY and an empty NO_PROXY. Bun reads those once at
// startup and would send even these loopback requests to the dead proxy, so
// every case would fail (or pass vacuously) at the proxy before any timeout is
// exercised. Under such a proxy, rerun this file in a child whose NO_PROXY
// exempts only loopback, and require that child to pass.
const proxied = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
  .some((name) => (process.env[name] ?? "") !== "");
const loopbackExempt = /(^|,)\s*(127\.0\.0\.1|\*)\s*(,|$)/.test(process.env.NO_PROXY ?? process.env.no_proxy ?? "");

if (proxied && !loopbackExempt) {
  describe("download transport timeouts (loopback exempt from the isolation proxy)", () => {
    test("every timeout case passes against the real fetch path", () => {
      const child = Bun.spawnSync([process.execPath, "--no-env-file", "test", import.meta.path], {
        env: { ...process.env, NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" },
        stdout: "pipe", stderr: "pipe",
      });
      const output = `${child.stdout.toString()}${child.stderr.toString()}`;
      expect(output).toContain(" 6 pass");
      expect(child.exitCode).toBe(0);
    }, 60_000);
  });
} else describe("download transport timeouts", () => {
  test("defaults come from manifest transportClasses.download", () => {
    const value = new Transport(environment, new AbortController());
    expect(value.downloadTimeouts).toEqual({
      connectMs: Number(manifest.transportClasses.download?.connectTimeoutMs),
      idleMs: Number(manifest.transportClasses.download?.idleTimeoutMs),
    });
    expect(value.downloadTimeouts).toEqual({ connectMs: 30000, idleMs: 45000 });
  });

  test("a server that never sends headers is SERVICE_UNAVAILABLE after the connect timeout", async () => {
    const started = performance.now();
    expect(await code(transport(150, 10_000).download(request("/stall-headers"), undefined, { write() {} }, 1 << 20, "run.download"))).toBe("SERVICE_UNAVAILABLE");
    const elapsed = performance.now() - started;
    expect(elapsed).toBeGreaterThanOrEqual(140);
    expect(elapsed).toBeLessThan(3000);
  });

  test("a body that stalls mid-stream is SERVICE_UNAVAILABLE after the idle timeout", async () => {
    const written: number[] = [];
    const started = performance.now();
    expect(await code(transport(10_000, 150).download(request("/stall-body"), undefined, { write(bytes) { written.push(bytes.length); } }, 1 << 20, "run.download"))).toBe("SERVICE_UNAVAILABLE");
    expect(written).toEqual([3]);
    expect(performance.now() - started).toBeLessThan(3000);
  });

  test("a stalled non-2xx error body is SERVICE_UNAVAILABLE, not a hang", async () => {
    expect(await code(transport(10_000, 150).download(request("/stall-error-body"), undefined, { write() {} }, 1 << 20, "run.download"))).toBe("SERVICE_UNAVAILABLE");
  });

  test("the connect timeout does not cut off a body that keeps flowing", async () => {
    // 8 chunks 40 ms apart (~320 ms total) with a 100 ms connect and 200 ms idle budget.
    const result = await transport(100, 200).download(request("/slow-body"), undefined, { write() {} }, 1 << 20, "run.download");
    expect(result.status).toBe(200);
    expect(result.bytes).toBe(8);
  });

  test("cancellation during a stall is CANCELLED", async () => {
    const cancellation = new AbortController();
    setTimeout(() => cancellation.abort(), 100);
    expect(await code(transport(10_000, 10_000, cancellation).download(request("/stall-body"), undefined, { write() {} }, 1 << 20, "run.download"))).toBe("CANCELLED");
  });
});
