import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { runServiceAccount } from "../src/core/service-account";
import { buildChildEnvironment } from "../src/supervision/environment";
import { buildInstalledAdapterEnvironment } from "../src/adapters/environment";
import { installedAdapterDefinition } from "../src/adapters/recipes";
const credential = `rr_test_${"1".repeat(32)}`;
const start = { device_code: "private-device-value", user_code: "ABCD-EFGH", verification_uri: "https://github.com/login/device", expires_in: 60, interval: 1 };
async function invoke(fixture: unknown, operation = "status", env: Record<string, string> = {}, args?: string[]) {
  const root = await mkdtemp(join(tmpdir(), "prose-service-test-"));
  const path = join(root, "fixture.json");
  const original = JSON.stringify(fixture);
  await writeFile(path, original);
  let stdout = "", stderr = "";
  try {
    const code = await runCli(args ?? ["cli", ...(operation === "org" ? ["org", "list"] : ["auth", operation]), "--json"], {
      env: { PROSE_TEST_SERVICE_FIXTURE: path, ...env }, processCwd: root, userConfigPath: join(root, "cli.toml"),
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
    const result = await invoke({ credential: null, storeAvailable: false, exchanges: [] }, operation, { OPENPROSE_API_KEY: credential });
    expect(result.report.problem.code).toBe("INVOCATION_INVALID");
    // The refusal names the variable, never a status line.
    expect(result.report.result).toBeNull();
    expect(result.report.problem.details.credentialSource).toBe("environment");
    expect(result.report.problem.details.credentialVariable).toBe("OPENPROSE_API_KEY");
    expect(result.report.problem.action).toContain("OPENPROSE_API_KEY");
  }
});
test("auth status classifies malformed and rejected environment keys with a replace-or-unset Action", async () => {
  // Shared credential classifier, Action by source.
  const malformed = await invoke({ credential: null, storeAvailable: true, exchanges: [] }, "status", { OPENPROSE_API_KEY: "rr_live_" + "1".repeat(32) });
  const rejected = await invoke({ credential: null, storeAvailable: true, exchanges: [{ method: "GET", path: "/organizations", status: 403, body: { error: "Invalid API key." } }] }, "status", { OPENPROSE_API_KEY: credential });
  for (const [result, problem] of [[malformed, "malformed"], [rejected, "rejected"]] as const) {
    expect(result.code).toBe(10);
    expect(result.report.result).toBeNull();
    expect(result.report.problem.code).toBe("SERVICE_AUTH_REQUIRED");
    expect(result.report.problem.details).toMatchObject({ credentialProblem: problem, credentialSource: "environment", credentialVariable: "OPENPROSE_API_KEY" });
    expect(result.report.problem.action).toBe("Replace or unset OPENPROSE_API_KEY, then retry.");
  }
});
test("account verbs read a trailing --output", async () => {
  const result = await invoke({ credential: null, storeAvailable: true, exchanges: [] }, "status", {}, ["cli", "auth", "status", "--output=json"]);
  expect(result.code).toBe(0);
  expect(result.report.schema).toBe("openprose.service-operation/1");
  expect(result.report.operation).toBe("auth.status");
  expect(result.report).not.toHaveProperty("environment");
  expect(result.report).not.toHaveProperty("lane");
});
test("the service key cannot enter a child or adapter environment via explicit allowlist", () => {
  // SPEC: OPENPROSE_API_KEY is filtered from every harness environment.
  const child = buildChildEnvironment({ OPENPROSE_API_KEY: credential, openprose_api_key: credential }, { invocationId: "id", recursionToken: "recursion", runNonce: "nonce" }, ["OPENPROSE_API_KEY", "openprose_api_key"]);
  expect(Object.values(child)).not.toContain(credential);
  const definition = installedAdapterDefinition("agents-sdk/jsonl");
  const adapter = buildInstalledAdapterEnvironment({ definition, ambient: { OPENAI_API_KEY: "fixture", OPENPROSE_API_KEY: credential }, credentialGroup: "openai-api-key", additionalNames: ["OPENPROSE_API_KEY"] });
  expect(Object.values(adapter)).not.toContain(credential);
});
test("transport pins origin and bearer and strips extra remote fields", async () => {
  const originalFetch = globalThis.fetch;
  let stdout = "";
  try {
    globalThis.fetch = (async (input: unknown, options?: RequestInit) => {
      expect(input).toBe("https://run-prose-production.openprose.workers.dev/organizations");
      expect(options?.redirect).toBe("manual");
      expect((options?.headers as Record<string, string>).Authorization).toBe(`Bearer ${credential}`);
      expect(options?.signal).toBeInstanceOf(AbortSignal);
      return Response.json({ organizations: [{ id: "org", slug: "org", name: "Org", api_key: credential }] });
    }) as unknown as typeof fetch;
    // A public build ignores the developer endpoint variable: the origin stays production.
    const code = await runServiceAccount("org-list", "json", { env: { OPENPROSE_API_KEY: credential, OPENPROSE_API_URL: "https://example.invalid" }, writeStdout: (value) => { stdout += value; }, writeStderr: () => {} });
    expect(code).toBe(0);
    expect(JSON.parse(stdout).result.organizations).toEqual([{ id: "org", slug: "org", name: "Org" }]);
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
      await runServiceAccount("auth-status", "json", { env: { OPENPROSE_API_KEY: credential }, writeStdout: (value) => { stdout += value; }, writeStderr: () => {} });
      expect(JSON.parse(stdout).problem.code).toBe(expected);
      expect(stdout).not.toContain(credential);
    }
  } finally { globalThis.fetch = originalFetch; }
});
test("organization group help is the generated topic; org list help is its own topic", async () => {
  let topic = "";
  await runCli(["cli", "org", "--help"], {
    env: {}, processCwd: "/absent", clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { topic += value; }, writeStderr: () => {},
  });
  expect(topic).toStartWith("Usage: prose [GLOBAL OPTIONS] cli org <COMMAND>");
  for (const args of [["cli", "org", "list", "--help"]]) {
    let stdout = "";
    const code = await runCli(args, {
      env: {}, processCwd: "/absent", clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
      writeStdout: (value) => { stdout += value; }, writeStderr: () => {},
    });
    expect(code).toBe(0);
    expect(stdout).toStartWith("Usage: prose [GLOBAL OPTIONS] cli org list [--json]");
    expect(stdout).not.toContain("OpenProse outer runner");
    expect(stdout).toContain("Device flow: ");
  }
});
