// The Bun build's own fetch chooses a proxy exactly as the shared vectors say
// (shared/fixtures/transport/proxy-selection.json); the Rust build implements
// the same rules (service/http.rs `proxy_for`). Each vector runs a real Bun
// fetch against a local recording proxy that answers 502. A request that is
// not proxied goes to an `.invalid` name, which never resolves (RFC 6761), so
// no service is ever reached.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const fixture = JSON.parse(readFileSync(join(import.meta.dir, "../../shared/fixtures/transport/proxy-selection.json"), "utf8"));

test.skipIf(process.platform === "win32")("Bun's fetch selects the proxy the shared vectors name", async () => {
  let seen = 0;
  const server = Bun.listen({
    hostname: "127.0.0.1", port: 0,
    socket: {
      data(socket) { seen += 1; socket.write("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"); socket.end(); },
    },
  });
  try {
    for (const item of fixture.cases) {
      const before = seen;
      const env: Record<string, string> = { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: process.env.HOME ?? "/" };
      for (const [name, value] of Object.entries(item.environment as Record<string, string>)) env[name] = value.split("{PORT}").join(String(server.port));
      // Asynchronous, so this process's event loop keeps serving the proxy.
      const child = Bun.spawn([process.execPath, "-e", `try { await fetch(${JSON.stringify(item.url)}, { signal: AbortSignal.timeout(4000) }); } catch {}`], { env, stdout: "ignore", stderr: "ignore" });
      expect({ id: item.id, exit: await child.exited }).toEqual({ id: item.id, exit: 0 });
      expect({ id: item.id, proxied: seen > before }).toEqual({ id: item.id, proxied: item.proxied });
    }
  } finally { server.stop(true); }
}, 120_000);
