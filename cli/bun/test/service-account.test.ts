import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { runServiceAccount } from "../src/core/service-account";
import { buildChildEnvironment } from "../src/supervision/environment";
const credential = `rr_test_${"1".repeat(32)}`;
const start = { device_code: "private-device-value", user_code: "ABCD-EFGH", verification_uri: "https://github.com/login/device", expires_in: 60, interval: 1 };
async function invoke(fixture: unknown, operation = "status", env: Record<string, string> = {}, args?: string[]) {
  const root = await mkdtemp(join(tmpdir(), "prose-service-test-"));
  const path = join(root, "fixture.json");
  const original = JSON.stringify(fixture);
  await writeFile(path, original);
  let stdout = "", stderr = "";
  try {
    const code = await runCli(args ?? ["--service-environment", "staging", "cli", ...(operation === "org" ? ["org", "list"] : ["auth", operation]), "--json"], {
      env: { PROSE_TEST_SERVICE_FIXTURE: path, ...env }, processCwd: root,
      clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test-invocation" },
      writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
    });
    expect(await readFile(path, "utf8")).toBe(original);
    expect(stdout + stderr).not.toContain(credential);
    expect(stdout + stderr).not.toContain(start.device_code);
    return { code, report: JSON.parse(stdout), stdout, stderr };
  } finally { await rm(root, { recursive: true, force: true }); }
}
function loginFixture(overrides: Record<string, unknown>, polls: unknown[] = []) {
  return { credential: null, storeAvailable: true, exchanges: [
    { method: "POST", path: "/auth/device", status: 200, body: { ...start, ...overrides } },
    ...polls.map((body) => ({ method: "POST", path: "/auth/device/poll", status: 200, body })),
  ] };
}
test("device flow stops before a sleep crosses expiry", async () => {
  expect((await invoke(loginFixture({ expires_in: 1 }), "login")).report.problem.code).toBe("DEVICE_AUTH_EXPIRED");
});
test("slow_down changes interval and respects deadline", async () => {
  expect((await invoke(loginFixture({ expires_in: 7 }, [{ status: "slow_down" }]), "login")).report.problem.code).toBe("DEVICE_AUTH_EXPIRED");
});
test("device flow rejects malicious user code before output", async () => {
  const result = await invoke(loginFixture({ user_code: "bad\nhttps://evil.invalid" }), "login");
  expect(result.report.problem.code).toBe("SERVICE_PROTOCOL_INVALID");
  expect(result.stderr).toBe("");
});
test("org projection rejects unknown roles and credential reflection", async () => {
  for (const row of [{ id: "org", slug: "org", name: "Org", role: "root" }, { id: "org", slug: "org", name: credential }]) {
    const result = await invoke({ credential, storeAvailable: true, exchanges: [{ method: "GET", path: "/organizations", status: 200, body: { organizations: [row] } }] }, "org");
    expect(result.report.problem.code).toBe("SERVICE_PROTOCOL_INVALID");
  }
});
test("login rejects production or malformed credential without reflection", async () => {
  const result = await invoke(loginFixture({}, [{ status: "complete", api_key: "rr_live_secret-must-not-escape" }]), "login");
  expect(result.report.problem.code).toBe("SERVICE_PROTOCOL_INVALID");
  expect(result.stdout + result.stderr).not.toContain("rr_live_secret");
});
test("environment login/logout cannot mutate credentials", async () => {
  for (const operation of ["login", "logout"]) {
    const result = await invoke({ credential: null, storeAvailable: false, exchanges: [] }, operation, { OPENPROSE_STAGING_API_KEY: credential });
    expect(result.report.problem.code).toBe("INVOCATION_INVALID");
  }
});
test("staging selection cannot launch language or unrelated operations", async () => {
  for (const args of [["run", "program.prose"], ["cli", "doctor"], ["--", "cli", "auth", "status"]]) {
    const result = await invoke({}, "status", {}, ["--output", "json", "--service-environment", "staging", ...args]);
    expect(result.report.code).toBe("INVOCATION_INVALID");
  }
});
test("staging token cannot enter child environment via explicit allowlist", () => {
  expect(buildChildEnvironment({ OPENPROSE_STAGING_API_KEY: credential }, { invocationId: "id", recursionToken: "recursion", runNonce: "nonce" }, ["OPENPROSE_STAGING_API_KEY"]).OPENPROSE_STAGING_API_KEY).toBeUndefined();
});
test("transport pins origin and bearer and strips extra remote fields", async () => {
  const originalFetch = globalThis.fetch;
  let stdout = "";
  try {
    globalThis.fetch = (async (input: unknown, options?: RequestInit) => {
      expect(input).toBe("https://run-prose-staging.openprose.workers.dev/organizations");
      expect(options?.redirect).toBe("error");
      expect((options?.headers as Record<string, string>).Authorization).toBe(`Bearer ${credential}`);
      expect(options?.signal).toBeInstanceOf(AbortSignal);
      return Response.json({ organizations: [{ id: "org", slug: "org", name: "Org", api_key: credential }] });
    }) as unknown as typeof fetch;
    const code = await runServiceAccount("org-list", "json", { env: { OPENPROSE_STAGING_API_KEY: credential }, writeStdout: (value) => { stdout += value; }, writeStderr: () => {} });
    expect(code).toBe(0);
    expect(JSON.parse(stdout).organizations).toEqual([{ id: "org", slug: "org", name: "Org" }]);
    expect(stdout).not.toContain(credential);
  } finally { globalThis.fetch = originalFetch; }
});
test("HTTP failures remain typed with empty or HTML bodies; successful bodies remain bounded", async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const [status, body, expected] of [
      [401, "", "SERVICE_AUTH_REQUIRED"],
      [503, "<html>unavailable</html>", "SERVICE_UNAVAILABLE"],
      [200, "x".repeat(65_537), "SERVICE_PROTOCOL_INVALID"],
    ] as const) {
      let stdout = "";
      globalThis.fetch = (async () => new Response(body, { status })) as unknown as typeof fetch;
      await runServiceAccount("auth-status", "json", { env: { OPENPROSE_STAGING_API_KEY: credential }, writeStdout: (value) => { stdout += value; }, writeStderr: () => {} });
      expect(JSON.parse(stdout).problem.code).toBe(expected);
      expect(stdout).not.toContain(credential);
    }
  } finally { globalThis.fetch = originalFetch; }
});
test("organization help stays in runner namespace", async () => {
  for (const args of [["cli", "org", "--help"], ["cli", "org", "list", "--help"]]) {
    let stdout = "";
    const code = await runCli(args, {
      env: {}, processCwd: "/absent", clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
      writeStdout: (value) => { stdout += value; }, writeStderr: () => {},
    });
    expect(code).toBe(0);
    expect(stdout).toContain("OpenProse outer runner");
  }
});
