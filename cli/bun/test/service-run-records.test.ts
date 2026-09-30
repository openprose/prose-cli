// Run records: checks the shared corpus (cli/conformance/cases/service/run-records/)
// cannot express: download caps (lowered by the test-seam PROSE_TEST_SERVICE_DOWNLOAD_LIMITS,
// since 64 MiB fixtures are impossible), the 1 MiB inline `run show --file` cap, and the
// projection helpers. The Rust twin is cli/rust/crates/prose-cli/tests/service_run_records.rs.
import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { checkDownloadPaths, filePath, parseLimit, projectRun, shareExpiry, validRunId } from "../src/core/service/run-records";
import { RunnerFailure } from "../src/core/types";

const RUN = `run_${"abc".repeat(21)}a`;
const KEY = "rr_test_0123456789abcdef0123456789abcdef";

function manifest(files: string[]) {
  return { run_id: RUN, created_at: "2026-09-23T20:29:47.788Z", status: "completed", model: "model-luna", files, has_patch: false,
    file_urls: Object.fromEntries(files.map((file) => [file, `https://x/runs/${RUN}/files/${file}?tok=SECRET`])) };
}

async function prose(args: string[], exchanges: unknown[], env: Record<string, string> = {}) {
  let stdout = "";
  let stderr = "";
  const home = mkdtempSync(join(tmpdir(), "prose-run-records-"));
  const fixture = join(home, "fixture.json");
  writeFileSync(fixture, JSON.stringify({ environment: "production", exchanges }));
  const code = await runCli(["--output", "json", "cli", ...args], {
    env: { HOME: home, XDG_STATE_HOME: join(home, "state"), OPENPROSE_API_KEY: KEY, PROSE_TEST_SERVICE_FIXTURE: fixture, ...env },
    processCwd: home, homeDir: home, userConfigPath: join(home, "config", "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { code, stdout, stderr, home, report: stdout === "" ? undefined : JSON.parse(stdout) };
}

const manifestExchange = (files: string[]) => ({ method: "GET", path: `/runs/${RUN}`, status: 200, body: manifest(files) });
const fileExchange = (path: string, bodyText: string) => ({ method: "GET", path: `/runs/${RUN}/files/${path}`, status: 200, bodyText });

describe("Service run records", () => {
  test("a file over the per-file cap is SERVICE_RESPONSE_TOO_LARGE and leaves no marker", async () => {
    const result = await prose(["run", "download", RUN, "--output-dir", "out"], [manifestExchange(["a.txt"]), fileExchange("a.txt", "123456")],
      { PROSE_TEST_SERVICE_DOWNLOAD_LIMITS: "5,1000" });
    expect(result.code).toBe(10);
    expect(result.report.problem.code).toBe("SERVICE_RESPONSE_TOO_LARGE");
    expect(result.report.problem.details.reason).toBe("\"a.txt\" is larger than 5 bytes; the output directory is incomplete and has no .prose-run-manifest.json");
    expect(existsSync(join(result.home, "out", ".prose-run-manifest.json"))).toBe(false);
  });

  test("files over the per-run total are SERVICE_RESPONSE_TOO_LARGE", async () => {
    const result = await prose(["run", "download", RUN, "--output-dir", "out"],
      [manifestExchange(["a.txt", "b.txt"]), fileExchange("a.txt", "123456"), fileExchange("b.txt", "123456")],
      { PROSE_TEST_SERVICE_DOWNLOAD_LIMITS: "100,10" });
    expect(result.code).toBe(10);
    expect(result.report.problem.details.reason).toBe("the run's files exceed 10 bytes in total; the output directory is incomplete and has no .prose-run-manifest.json");
    expect(readFileSync(join(result.home, "out", "a.txt"), "utf8")).toBe("123456");
  });

  test("files exactly at the caps download, and the marker never carries signed URLs", async () => {
    const result = await prose(["run", "download", RUN, "--output-dir", "out"],
      [manifestExchange(["a.txt", "b/c.txt"]), fileExchange("a.txt", "12345"), fileExchange("b/c.txt", "12345")],
      { PROSE_TEST_SERVICE_DOWNLOAD_LIMITS: "5,10" });
    expect(result.code).toBe(0);
    expect(result.report.result).toMatchObject({ fileCount: 2, totalBytes: 10 });
    const marker = readFileSync(join(result.home, "out", ".prose-run-manifest.json"), "utf8");
    expect(marker).not.toContain("tok=");
    expect(JSON.parse(marker).download).toEqual(result.report.result);
    expect(existsSync(join(result.home, "out", ".prose-run-manifest.json.partial"))).toBe(false);
  });

  test("inline run show --file is capped at 1 MiB and names --output-file", async () => {
    // Generated at runtime: a > 1 MiB corpus file would exceed the repository's new-file limit.
    const result = await prose(["run", "show", RUN, "--file", "big.txt"], [manifestExchange(["big.txt"]), fileExchange("big.txt", "x".repeat(1024 * 1024 + 1))]);
    expect(result.code).toBe(10);
    expect(result.report.problem.code).toBe("SERVICE_RESPONSE_TOO_LARGE");
    expect(result.report.problem.details.reason).toBe("\"big.txt\" is larger than 1048576 bytes; pass --output-file FILE to save it");
  });

  test("a non-text file under 1 MiB is summarized, not printed, in human mode", async () => {
    let stdout = "";
    const home = mkdtempSync(join(tmpdir(), "prose-run-records-"));
    const fixture = join(home, "fixture.json");
    writeFileSync(fixture, JSON.stringify({ environment: "production", exchanges: [manifestExchange(["bin"]), { method: "GET", path: `/runs/${RUN}/files/bin`, status: 200, bodyBase64: "G1szMW0=" }] }));
    const code = await runCli(["cli", "run", "show", RUN, "--file", "bin"], {
      env: { HOME: home, XDG_STATE_HOME: join(home, "state"), OPENPROSE_API_KEY: KEY, PROSE_TEST_SERVICE_FIXTURE: fixture },
      processCwd: home, homeDir: home, userConfigPath: join(home, "config", "cli.toml"),
      clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
      writeStdout: (value) => { stdout += value; }, writeStderr: () => {},
    });
    expect(code).toBe(0);
    // ESC [ 3 1 m: a terminal escape is binary here, never echoed raw.
    expect(stdout).not.toContain("\u001b");
    expect(stdout).toStartWith("bin is 5 bytes of binary data (sha256 ");
  });

  test("projection, path and cursor helpers", () => {
    expect(validRunId("run_a-B_1")).toBe(true);
    for (const bad of ["run_", "garbage", "run_a/b", `run_${"a".repeat(129)}`]) expect(validRunId(bad)).toBe(false);
    expect(parseLimit("200")).toBe(200);
    for (const bad of ["0", "201", "-1", "+5", "1e2", "", "0200"]) expect(parseLimit(bad)).toBeUndefined();
    expect(filePath("run_1", "outputs/a b.txt")).toBe("/runs/run_1/files/outputs/a%20b.txt");
    expect(shareExpiry("https://web/share/runs/run_1#tok=abc&exp=1790298569")).toBe("2026-09-25T01:09:29Z");
    expect(shareExpiry("https://web/share/runs/run_1")).toBeUndefined();
    expect(shareExpiry("https://web/x#exp=253402300800")).toBeUndefined();
    const projected = projectRun({ ...manifest(["a"]), customer_id: "cus_x", usage: { input_tokens: 1, output_tokens: 2, extra: 3 }, error: "a\nb" }, true);
    expect(JSON.stringify(projected)).not.toContain("tok=");
    // Run error text prints its mapped words; unknown text the generic message.
    expect(projected).toMatchObject({ error: "The run failed on the service.", usage: { input_tokens: 1, output_tokens: 2 }, files: ["a"] });
    expect(projected.customer_id).toBeUndefined();
    expect(() => checkDownloadPaths(["a", "a/b"])).toThrow(RunnerFailure);
    expect(() => checkDownloadPaths(["a", "a"])).toThrow(RunnerFailure);
    expect(() => checkDownloadPaths([".prose-run-manifest.json"])).toThrow(RunnerFailure);
    expect(() => checkDownloadPaths(["a", "b/c"])).not.toThrow();
  });
});
