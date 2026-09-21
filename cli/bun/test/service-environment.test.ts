import { expect, test } from "bun:test";
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { runServiceAccount } from "../src/core/service-account";
import { buildChildEnvironment } from "../src/supervision/environment";
const token = `rr_test_${"1".repeat(32)}`;
async function workspace(fn: (root: string, invoke: (args: string[], env?: Record<string, string>) => Promise<{ code: number; stdout: string; stderr: string }>) => Promise<void>) {
  const root = await mkdtemp(join(tmpdir(), "prose-environment-"));
  await mkdir(join(root, "user"));
  await writeFile(join(root, "fixture.json"), JSON.stringify({ credentials: { production: null, staging: null }, storeAvailable: true, exchanges: [] }));
  const invoke = async (args: string[], env: Record<string, string> = {}) => {
    let stdout = "", stderr = "";
    const code = await runCli(args, { processCwd: root, userConfigPath: join(root, "user", "cli.toml"), env: { PROSE_TEST_SERVICE_FIXTURE: join(root, "fixture.json"), ...env }, clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" }, writeStdout: value => { stdout += value; }, writeStderr: value => { stderr += value; } });
    return { code, stdout, stderr };
  };
  try { await fn(root, invoke); } finally { await rm(root, { recursive: true, force: true }); }
}
test("persistent environment sequences consume shared corpus", async () => {
  const corpus = await Bun.file(new URL("../../conformance/runner/service-environment-corpus.json", import.meta.url)).json();
  for (const item of corpus.cases) await workspace(async (root, invoke) => {
    if (item.userConfig !== undefined) await writeFile(join(root, "user", "cli.toml"), item.userConfig);
    for (const step of item.steps) {
      const result = await invoke(step.argv);
      expect(result.code).toBe(step.exitCode);
      expect(JSON.parse(result.stdout)).toMatchObject(step.resultMatches);
      expect(result.stderr).toBe("");
    }
    if (item.preserve !== undefined) expect(await readFile(join(root, "user", "cli.toml"), "utf8")).toBe(item.preserve);
  });
});
test("user selection, override and reset isolate credential routes; project selection cannot redirect", async () => workspace(async (root, invoke) => {
  await mkdir(join(root, ".prose"));
  await writeFile(join(root, ".prose", "cli.toml"), 'service_environment = "staging"\n');
  expect(JSON.parse((await invoke(["cli", "auth", "status", "--json"], { OPENPROSE_STAGING_API_KEY: "malformed" })).stdout)).toMatchObject({ environment: "production", authenticated: false, problem: null });
  expect(JSON.parse((await invoke(["cli", "config", "explain", "--json"])).stdout).code).toBe("CONFIG_INVALID");
  await invoke(["cli", "environment", "use", "staging", "--json"]);
  expect(JSON.parse((await invoke(["cli", "auth", "status", "--json"], { OPENPROSE_API_KEY: "malformed" })).stdout)).toMatchObject({ environment: "staging", authenticated: false, problem: null });
  expect(JSON.parse((await invoke(["--service-environment", "production", "cli", "auth", "status", "--json"])).stdout).environment).toBe("production");
  expect(JSON.parse((await invoke(["cli", "environment", "show", "--json"])).stdout).environment).toBe("staging");
  const human = await invoke(["cli", "org", "list"]);
  expect(human.stderr).toContain("OpenProse staging");
  expect(JSON.parse((await invoke(["--service-environment", "production", "cli", "environment", "reset", "--json"])).stdout).code).toBe("INVOCATION_INVALID");
}));
test("malformed user selection fails before service and unsafe config writes preserve targets", async () => workspace(async (root, invoke) => {
  const path = join(root, "user", "cli.toml");
  await writeFile(path, 'service_environment = "unknown"\n');
  expect(JSON.parse((await invoke(["cli", "auth", "status", "--json"])).stdout).code).toBe("CONFIG_INVALID");
  await rm(path);
  const target = join(root, "target.toml");
  await writeFile(target, '# untouched\n');
  await symlink(target, path);
  expect(JSON.parse((await invoke(["cli", "environment", "use", "staging", "--json"])).stdout).code).toBe("CONFIG_INVALID");
  expect(await readFile(target, "utf8")).toBe('# untouched\n');
}));
test("production requests pin origin and use only production bearer", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = (async (input: unknown, options?: RequestInit) => {
      expect(input).toBe("https://run-prose-production.openprose.workers.dev/organizations");
      expect((options?.headers as Record<string, string>).Authorization).toBe(`Bearer ${token}`);
      return Response.json({ organizations: [] });
    }) as typeof fetch;
    let stdout = "";
    expect(await runServiceAccount("org-list", "json", { env: { OPENPROSE_API_KEY: token, OPENPROSE_STAGING_API_KEY: "malformed" }, writeStdout: value => { stdout += value; }, writeStderr: () => {} })).toBe(0);
    expect(JSON.parse(stdout)).toMatchObject({ environment: "production", problem: null });
  } finally { globalThis.fetch = original; }
});
test("both service credentials resist explicit child reallow", () => {
  const result = buildChildEnvironment({ OPENPROSE_API_KEY: token, OPENPROSE_STAGING_API_KEY: token }, { invocationId: "id", recursionToken: "r", runNonce: "n" }, ["OPENPROSE_API_KEY", "OPENPROSE_STAGING_API_KEY"]);
  expect(result.OPENPROSE_API_KEY).toBeUndefined();
  expect(result.OPENPROSE_STAGING_API_KEY).toBeUndefined();
});
