import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { runCli } from "../src/cli";
import boundaries from "../../shared/fixtures/account-global-boundaries.json" with { type: "json" };

test("account commands reject execution-only globals without touching their credential fixture", async () => {
  const root = await mkdtemp(join(tmpdir(), "prose-account-boundaries-"));
  const fixture = join(root, "service.json");
  const original = "malformed fixture must not be read";
  await writeFile(fixture, original);
  try {
    for (const command of boundaries.commands) for (const prefix of boundaries.deniedPrefixes) {
      let stdout = "", stderr = "";
      const code = await runCli(["--output", "json", ...prefix, ...command], {
        env: { PROSE_TEST_SERVICE_FIXTURE: fixture }, processCwd: root,
        userConfigPath: join(root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 },
        ids: { invocationId: () => "account-boundary" },
        writeStdout: text => { stdout += text; }, writeStderr: text => { stderr += text; },
      });
      const report = JSON.parse(stdout);
      expect(code, JSON.stringify({ command, prefix })).toBe(boundaries.expected.exitCode);
      expect(report.schema).toBe(boundaries.expected.schema);
      expect(report.problem.code).toBe(boundaries.expected.problemCode);
      expect(report.result).toBeNull();
      expect(stderr).toBe(boundaries.expected.stderr);
      expect(await readFile(fixture, "utf8")).toBe(original);
    }
    await writeFile(fixture, JSON.stringify({ credential: null, storeAvailable: true, exchanges: [] }));
    for (const prefix of boundaries.allowedPrefixes) {
      let stdout = "", stderr = "";
      const code = await runCli([...prefix, "cli", "auth", "status"], {
        env: { PROSE_TEST_SERVICE_FIXTURE: fixture }, processCwd: root,
        userConfigPath: join(root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 },
        ids: { invocationId: () => "account-boundary" },
        writeStdout: text => { stdout += text; }, writeStderr: text => { stderr += text; },
      });
      expect(code).toBe(0);
      expect(JSON.parse(stdout).result.authenticated).toBeFalse();
      expect(stderr).toBe("");
    }
  } finally { await rm(root, { recursive: true }); }
});
