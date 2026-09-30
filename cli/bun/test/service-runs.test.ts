// Service run lifecycle: unit checks plus process-level checks the shared
// corpus cannot hold (inputs over 1 MiB generated at runtime, journal modes).
// Behavior is pinned by cli/conformance/cases/service/runs/; the Rust twins are
// in cli/rust/crates/prose-cli/tests/service_runs.rs and runs.rs unit tests.
import { describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { Transport } from "../src/core/service/http";
import { Context } from "../src/core/service/index";
import { operation, parseService } from "../src/core/service/manifest";
import { PRODUCTION } from "../src/core/service/endpoint";
import { __test, execute } from "../src/core/service/runs";

const KEY = "rr_test_0123456789abcdef0123456789abcdef";
const SESSION = "5f0c8a1e-3b2d-4c6e-9f70-1a2b3c4d5e6f";
const RUN = `run_${"4f1c2d3e4f5a6b7c".repeat(4)}`;
const MEBIBYTE = 1024 * 1024;

async function cli(args: string[], setup: (home: string) => void, fixture?: unknown) {
  let stdout = "";
  let stderr = "";
  const home = mkdtempSync(join(tmpdir(), "prose-runs-"));
  setup(home);
  const env: Record<string, string> = { HOME: home, XDG_STATE_HOME: join(home, "state"), OPENPROSE_API_KEY: KEY };
  if (fixture !== undefined) {
    writeFileSync(join(home, "fixture.json"), JSON.stringify(fixture));
    env.PROSE_TEST_SERVICE_FIXTURE = join(home, "fixture.json");
  }
  const code = await runCli(args, {
    env, processCwd: home, homeDir: home, userConfigPath: join(home, "config", "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { code, stdout, stderr, home };
}

const submit = (...extra: string[]) => ["--output", "json", "cli", "run", "submit", ...extra];
const empty = { environment: "production", exchanges: [] };

describe("Service run lifecycle", () => {
  test("validators and sanitizers match the Rust product", () => {
    expect(__test.validRunId("run_abc-DEF_1")).toBe(true);
    expect(__test.validRunId("run_") || __test.validRunId("abc") || __test.validRunId("run_a b")).toBe(false);
    expect(__test.parseWait(undefined)).toBe(1_800_000);
    expect(__test.parseWait("90s")).toBe(90_000);
    expect(__test.parseWait("6h")).toBe(21_600_000);
    for (const bad of ["7h", "0s", "10", "1d", "s", "-1m", "1.5h"]) expect(() => __test.parseWait(bad)).toThrow();
    expect(__test.parseAfter("42")).toBe(42);
    expect(() => __test.parseAfter("-1")).toThrow();
    expect(() => __test.parseAfter("12345678901")).toThrow();
    expect(__test.cleanLine("a\nb\u007fc", 10)).toBe("a b c");
    expect(__test.cleanText("a\nb\u001bc", 10)).toBe("a\nb c");
    expect(__test.cleanLine("héllo", 2)).toBe("hé");
    expect(__test.unrecognizedName("Commit-Done")).toBe("commit_done");
    expect(__test.unrecognizedName("")).toBe("unknown");
    expect(__test.terminalText("x\u009by")).toBe("x y");
  });

  test("the terminal projection drops signed URLs and normalizes files", () => {
    const run = __test.projectTerminal({
      run_id: "run_1", status: "completed", files: { "b.txt": "x", "a.txt": "y", "../bad": "" },
      file_urls: { "a.txt": "https://x?tok=secret" }, price_cents: 2, session: "s",
      usage: { input_tokens: 1, output_tokens: 2 }, output: { type: "commit" },
    });
    expect(run.files).toEqual(["a.txt", "b.txt"]);
    expect(run.cancelled).toBe(false);
    expect("file_urls" in run || "output" in run || "session" in run).toBe(false);
  });

  test("events project to closed shapes", () => {
    expect(__test.projectEvent("status", { status: "history_truncated", message: "gap" })).toEqual(["history_truncated", { message: "gap" }]);
    expect(__test.projectEvent("agent_activity", { kind: "tool_start", turn: 999, agent: "Agent 2", call_id: "c 1" }))
      .toEqual(["agent_activity", { kind: "tool_start", agent: "Agent 2" }]);
    expect(__test.projectEvent("browser_live_view", { url: "https://secret" })).toEqual(["unrecognized", { name: "browser_live_view" }]);
  });

  test("an input file over 1 MiB is rejected before any request", async () => {
    const result = await cli(submit("p.prose.md", "--input", "notes=@big.txt", "--yes"), (home) => {
      writeFileSync(join(home, "p.prose.md"), "Reply ok.\n");
      writeFileSync(join(home, "big.txt"), "x".repeat(MEBIBYTE + 1));
    }, empty);
    expect(result.code).toBe(2);
    const problem = JSON.parse(result.stdout).problem;
    expect(problem.code).toBe("INVOCATION_INVALID");
    expect(problem.details.reason).toBe("--input notes file \"big.txt\" is larger than 1048576 bytes");
  });

  test("a program file over 1 MiB is rejected before any request", async () => {
    const result = await cli(submit("big.prose.md", "--yes"), (home) => {
      writeFileSync(join(home, "big.prose.md"), "x".repeat(MEBIBYTE + 1));
    }, empty);
    expect(result.code).toBe(2);
    expect(JSON.parse(result.stdout).problem.details.reason).toBe("program file \"big.prose.md\" is larger than 1048576 bytes");
  });

  test("an input file of exactly 1 MiB is planned", async () => {
    const result = await cli(submit("p.prose.md", "--input", "notes=@max.txt", "--preview"), (home) => {
      writeFileSync(join(home, "p.prose.md"), "Reply ok.\n");
      writeFileSync(join(home, "max.txt"), "y".repeat(MEBIBYTE));
    }, { environment: "production", exchanges: [{ method: "GET", path: "/run/quote", status: 200, body: { hold: { hold_usd: "1.02", ttl_seconds: 900 }, pricing_policy_id: "p" } }] });
    expect(result.code).toBe(0);
    const planned = JSON.parse(result.stdout).result.plannedRequest;
    expect(planned.bodyBytes).toBeGreaterThan(MEBIBYTE);
    expect(planned.quote.hold.hold_usd).toBe("1.02");
  });

  test("submit --detach closes its event stream so the process can exit", async () => {
    // Bun keeps a process alive while a fetch body is readable: without the
    // abort, a real `submit --detach` only exited when the run finished
    // (observed against the hosted service, 2026-09-24). Pinned on the invocation's controller.
    const home = mkdtempSync(join(tmpdir(), "prose-runs-detach-"));
    writeFileSync(join(home, "p.prose.md"), "Reply ok.\n");
    const args = ["run", "submit", "p.prose.md", "--yes", "--detach"];
    const command = parseService(args, args);
    if (command.kind !== "invoke") throw new Error("expected an invocation");
    const environment = PRODUCTION;
    const transport = Transport.withFixture(environment, {
      environment: "production", ids: [SESSION], exchanges: [{
        method: "POST", path: "/run", query: { live: "1", session: SESSION }, status: 200,
        sse: { end: "idle", frames: [{ id: "1", event: "status", data: { type: "status", status: "running", run_id: RUN, sequence: 1 } }] },
      }],
    });
    const context = new Context(command.invocation, operation("run.submit")!, environment, "json", transport, {
      env: { OPENPROSE_API_KEY: KEY, XDG_STATE_HOME: join(home, "state") }, processCwd: home, homeDir: home,
      writeStdout: () => {}, writeStderr: () => {},
    });
    const result = await execute(context) as { detached: boolean; runId: string };
    expect(result).toMatchObject({ detached: true, runId: RUN });
    expect(transport.cancellation.signal.aborted).toBe(true);
  });

  test.skipIf(process.platform === "win32")("the run journal is private and holds no credential", async () => {
    const frame = (sequence: number, kind: string, extra: Record<string, unknown>) =>
      ({ id: String(sequence), event: kind, data: { type: kind, run_id: RUN, sequence, ...extra } });
    const result = await cli(submit("p.prose.md", "--yes"), (home) => writeFileSync(join(home, "p.prose.md"), "Reply ok.\n"), {
      environment: "production", ids: [SESSION], exchanges: [{
        method: "POST", path: "/run", query: { live: "1", session: SESSION }, status: 200,
        sse: { end: "close", frames: [frame(1, "status", { status: "running" }), frame(2, "run_complete", { status: "completed", files: [] })] },
      }],
    });
    expect(result.code).toBe(0);
    const runs = join(result.home, "state", "openprose", "cli", "production", "runs");
    const entry = join(runs, `${SESSION}.json`);
    expect(statSync(runs).mode & 0o777).toBe(0o700);
    expect(statSync(join(result.home, "state", "openprose")).mode & 0o777).toBe(0o700);
    expect(statSync(entry).mode & 0o777).toBe(0o600);
    const text = readFileSync(entry, "utf8");
    expect(text.includes("rr_test_")).toBe(false);
    expect(JSON.parse(text)).toMatchObject({ runId: RUN, lastSequence: 2 });
  });
});
