// Published-results unit tests (projection and argument helpers).
// Behavior is pinned by the shared corpus cli/conformance/cases/service/results/;
// these mirror the Rust unit tests in prose-runner-core/src/service/results.rs.
import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import {
  command, publicManifest, publicProgram, publication, rawText, validProgramRef, validPublicationId, validRunId, validTimestamp,
} from "../src/core/service/results";
import { PRODUCTION } from "../src/core/service/endpoint";
import { RunnerFailure } from "../src/core/types";

function reason(run: () => unknown): string {
  try { run(); } catch (caught) {
    if (caught instanceof RunnerFailure) return String(caught.details?.reason);
    throw caught;
  }
  throw new Error("expected a failure");
}

const summary = {
  id: "AbCdEf012_-x", published_at: "2026-09-23T20:30:00.000Z", run_id: "run_abc", program_ref: "Owner/demo@0123456789abcdef",
  created_at: "2026-09-23T20:26:28.686Z", model: "model-sol", status: "completed", extra: "dropped",
};

describe("published results", () => {
  test("public program arguments reject revisions and bad names", () => {
    expect(publicProgram("Owner/demo")).toEqual(["Owner", "demo"]);
    expect(reason(() => publicProgram("Owner/demo@0123456789abcdef"))).toContain("without @REV");
    // A bare SLUG is the caller's own program.
    expect(publicProgram("demo")).toEqual(["", "demo"]);
    expect(() => publicProgram("demo@0123456789abcdef")).toThrow();
    for (const bad of ["Demo", "Owner/Demo", "-x/demo", "a/b/c", ""]) expect(() => publicProgram(bad)).toThrow();
  });

  test("identifiers follow the schema predicates", () => {
    expect(validRunId("run_abc-_9")).toBe(true);
    for (const bad of ["run_", "abc", "run_a/b"]) expect(validRunId(bad)).toBe(false);
    expect(validPublicationId("AbCdEf012_-x")).toBe(true);
    expect(validPublicationId("a/b") || validPublicationId("")).toBe(false);
    expect(validTimestamp("2026-09-23T20:26:28.686Z")).toBe(true);
    expect(validTimestamp("2026-09-23 20:26:28Z") || validTimestamp("2026-09-23TZ")).toBe(false);
    expect(validProgramRef("Owner/demo@0123456789abcdef")).toBe(true);
    expect(validProgramRef("Owner/demo")).toBe(false);
  });

  test("publications are projected onto the closed shape", () => {
    const value = publication(summary, "publication");
    expect(Object.keys(value)).toHaveLength(7);
    expect(value.extra).toBeUndefined();
    expect(reason(() => publication({ ...summary, status: "error" }, "results[3]"))).toBe("the service returned an invalid results[3].status");
  });

  test("manifests drop signed urls and keep spec_commit", () => {
    const value = publicManifest({
      run_id: "run_abc", status: "completed", model: "model-sol", program_ref: "Owner/demo@0123456789abcdef",
      created_at: "2026-09-23T20:26:28.686Z", usage: { input_tokens: 1, output_tokens: 2 }, price_cents: 4,
      files: ["outputs/result.json"], has_patch: false, spec_commit: "abc",
      file_urls: { "outputs/result.json": "https://example.invalid/?tok=secret" },
    });
    expect(value.file_urls).toBeUndefined();
    expect(value.spec_commit).toBe("abc");
    expect(() => publicManifest({ run_id: "run_abc" })).toThrow();
  });

  test("raw text matches the common text predicate", () => {
    expect(rawText("{\"a\":1}\n\t\r")).toBe(true);
    expect(rawText("\u001b[31m") || rawText("\u007f")).toBe(false);
  });

  test("publish without --yes sends nothing and returns the plan", async () => {
    let stdout = "";
    const home = mkdtempSync(join(tmpdir(), "prose-results-"));
    const code = await runCli(["--output", "json", "cli", "result", "publish", "demo", "--run", "run_abc"], {
      env: { HOME: home, XDG_STATE_HOME: join(home, "state") }, processCwd: home, homeDir: home,
      userConfigPath: join(home, "config", "cli.toml"),
      clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
      writeStdout: (value) => { stdout += value; }, writeStderr: () => {},
    });
    expect(code).toBe(2);
    const document = JSON.parse(stdout) as { problem: { code: string; details: { plannedRequest: { path: string; bodyBytes: number } } } };
    expect(document.problem.code).toBe("CONFIRMATION_REQUIRED");
    expect(document.problem.details.plannedRequest).not.toHaveProperty("path");
    expect(document.problem.details.plannedRequest.bodyBytes).toBe('{"run_id":"run_abc"}'.length);
  });

  test("follow-up commands name no service and keep the output mode", () => {
    expect(command({ environment: PRODUCTION, mode: "human" }, "run list")).toBe("prose cli run list");
    expect(command({ environment: PRODUCTION, mode: "json" }, "run list")).toBe("prose --output json cli run list");
  });
});
