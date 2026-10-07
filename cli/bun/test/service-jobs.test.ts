// Service jobs unit and process-level checks. Service behavior is pinned
// by cli/conformance/cases/service/jobs/ (shared with Rust).
import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { asInteger, INTERVAL_MAX, INTERVAL_MIN, jobsLine, projectJob, projectJobs, SPEC_MAX_BYTES } from "../src/core/service/jobs";
import { RunnerFailure } from "../src/core/types";

const KEY = "rr_test_0123456789abcdef0123456789abcdef";
const TID = "3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10";
const SECRET = "whsec_unit_test_secret_value";

function code(run: () => unknown): string | undefined {
  try { run(); return undefined; } catch (caught) { return caught instanceof RunnerFailure ? caught.code : "THREW"; }
}

async function job(args: string[], options: { files?: Record<string, string>; fixture?: unknown; human?: boolean } = {}) {
  let stdout = "";
  let stderr = "";
  const home = mkdtempSync(join(tmpdir(), "prose-jobs-"));
  for (const [name, content] of Object.entries(options.files ?? {})) writeFileSync(join(home, name), content);
  const env: Record<string, string> = { HOME: home, XDG_STATE_HOME: join(home, "state"), HTTPS_PROXY: "http://127.0.0.1:9", OPENPROSE_API_KEY: KEY };
  if (options.fixture !== undefined) {
    writeFileSync(join(home, "fixture.json"), JSON.stringify(options.fixture));
    env.PROSE_TEST_SERVICE_FIXTURE = join(home, "fixture.json");
  }
  const mode = options.human === true ? [] : ["--output", "json"];
  const exit = await runCli([...mode, "cli", "job", ...args], {
    env, processCwd: home, homeDir: home, userConfigPath: join(home, "config", "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { exit, stdout, stderr, report: options.human === true ? undefined : JSON.parse(stdout) as Record<string, any> };
}

describe("Service job projections", () => {
  test("integers follow Number.isSafeInteger", () => {
    expect(asInteger(86400)).toBe(86400);
    expect(asInteger(60.5)).toBeUndefined();
    expect(asInteger("60")).toBeUndefined();
    expect(asInteger(2 ** 60)).toBeUndefined();
    expect([INTERVAL_MIN, INTERVAL_MAX, SPEC_MAX_BYTES]).toEqual([60, 2_678_400, 65_536]);
  });

  test("jobs keep their public snake_case fields only and sanitize free text", () => {
    expect(projectJob({ id: TID, type: "webhook", createdAt: 1, internalRef: "opaque-1", adopted: true, contextRepositoryId: 7, contextRepository: { id: 1 }, lastError: "line\nbreak", name: null }))
      .toEqual({ id: TID, type: "webhook", created_at: 1, created_at_iso: "1970-01-01T00:00:00.001Z", last_error: "line break", name: null });
    // Every epoch-ms time gains an additive `<name>_iso`, null when the time is null.
    expect(projectJob({ id: TID, type: "webhook", createdAt: 1, nextFireAt: null })).toMatchObject({ next_fire_at: null, next_fire_at_iso: null });
    expect(code(() => projectJob({ id: "x", type: "webhook", createdAt: 1 }))).toBe("SERVICE_PROTOCOL_INVALID");
    expect(code(() => projectJob({ id: TID, type: "webhook" }))).toBe("SERVICE_PROTOCOL_INVALID");
  });

  test("the job limit comes from trigger_limit, never the legacy max_triggers", () => {
    const limit = (body: Record<string, unknown>) => {
      const { limit, max } = projectJobs({ triggers: [], ...body } as never);
      return { limit, max };
    };
    expect(limit({ max_triggers: 5, trigger_limit: { kind: "limited", max: 3, extra: 1 } })).toEqual({ limit: { kind: "limited", max: 3 }, max: 3 });
    expect(limit({ max_triggers: 5, trigger_limit: { kind: "limited", max: 0 } })).toEqual({ limit: { kind: "limited", max: 0 }, max: 0 });
    expect(limit({ max_triggers: 5, trigger_limit: { kind: "unlimited", max: 9 } })).toEqual({ limit: { kind: "unlimited" }, max: null });
    expect(limit({ max_triggers: 5, trigger_limit: { kind: "unavailable" } })).toEqual({ limit: { kind: "unavailable" }, max: null });
    // An unknown kind is not guessed.
    expect(limit({ max_triggers: 5, trigger_limit: { kind: "metered", max: 7 } })).toEqual({ limit: { kind: "unavailable" }, max: null });
    // An older service without trigger_limit: its max_triggers is the limit.
    expect(limit({ max_triggers: 5 })).toEqual({ limit: { kind: "limited", max: 5 }, max: 5 });
    expect(limit({ max_triggers: 5, trigger_limit: null })).toEqual({ limit: { kind: "limited", max: 5 }, max: 5 });
    expect(limit({})).toEqual({ limit: { kind: "unavailable" }, max: null });
    expect(limit({ max_triggers: "5" })).toEqual({ limit: { kind: "unavailable" }, max: null });
    for (const bad of [
      { kind: "limited" }, { kind: "limited", max: null }, { kind: "limited", max: -1 }, { kind: "limited", max: 1.5 }, { kind: "limited", max: "3" },
      {}, { kind: 1 }, { kind: null }, "limited", [], 3,
    ]) expect(code(() => projectJobs({ triggers: [], max_triggers: 5, trigger_limit: bad } as never))).toBe("SERVICE_PROTOCOL_INVALID");
  });

  test("the Jobs line names the limit's kind", () => {
    expect(jobsLine(2, { kind: "limited", max: 3 })).toBe("Jobs: 2 of 3 allowed\n");
    expect(jobsLine(2, { kind: "unlimited" })).toBe("Jobs: 2 (unlimited)\n");
    expect(jobsLine(2, { kind: "unavailable" })).toBe("Jobs: 2 (limit unavailable; try again)\n");
  });
});

describe("Service job local checks send nothing", () => {
  // HTTPS_PROXY points at a closed port: any request would be SERVICE_UNAVAILABLE.
  test("oversize, non-object and out-of-range specs are INVOCATION_INVALID", async () => {
    const big = await job(["create", "--spec-file", "spec.json", "--yes"], { files: { "spec.json": `{"name":"${"x".repeat(SPEC_MAX_BYTES)}"}` } });
    expect(big.exit).toBe(2);
    expect(big.report!.problem.code).toBe("INVOCATION_INVALID");
    const array = await job(["create", "--spec-file", "spec.json", "--yes"], { files: { "spec.json": "[]" } });
    expect(array.report!.problem.details.reason).toBe('--spec-file "spec.json" must contain a JSON object');
    const bom = await job(["create", "--spec-file", "spec.json", "--yes"], { files: { "spec.json": "﻿{}" } });
    expect(bom.report!.problem.details.reason).toBe('--spec-file "spec.json" is not valid JSON');
    for (const interval of ["59", "2678401", "86400.5", "\"86400\"", "1e400"]) {
      const result = await job(["create", "--spec-file", "spec.json", "--yes"], { files: { "spec.json": `{"type":"schedule","interval_seconds":${interval}}` } });
      expect(result.exit).toBe(2);
      expect(result.report!.problem.code).toBe("INVOCATION_INVALID");
    }
    const configure = await job(["configure", TID, "--config-file", "c.json", "--yes"], { files: { "c.json": "{}" } });
    expect(configure.report!.problem.details.reason).toStartWith("the configuration needs interval_seconds");
  });

  test("detach without --yes plans the encoded DELETE; attach refuses --model and unpinned refs", async () => {
    const detach = await job(["contract", "detach", TID, "exowner1/probe@0123456789abcdef"]);
    expect(detach.exit).toBe(2);
    expect(detach.report!.problem.code).toBe("CONFIRMATION_REQUIRED");
    expect(detach.report!.problem.details.plannedRequest).toMatchObject({ method: "DELETE", description: "Detach a program from a job." });
    expect(detach.report!.problem.details.plannedRequest).not.toHaveProperty("path");
    const model = await job(["contract", "attach", TID, "exowner1/probe@0123456789abcdef", "--model", "model-luna", "--yes"]);
    expect(model.report!.problem.code).toBe("INVOCATION_INVALID");
    const unpinned = await job(["contract", "attach", TID, "exowner1/probe", "--yes"]);
    expect(unpinned.report!.problem.details.reason).toContain("must be pinned as OWNER/SLUG@REV");
  });
});

describe("Service job secrets", () => {
  test("human rotate-secret warns on stderr without the secret", async () => {
    const fixture = {
      environment: "production", credentials: { production: KEY }, storeAvailable: true,
      exchanges: [{ method: "POST", path: `/triggers/${TID}/rotate-secret`, status: 200, body: { status: {}, endpoint: `/webhooks/triggers/${TID}`, signing_secret: SECRET } }],
    };
    const result = await job(["rotate-secret", TID, "--yes"], { fixture, human: true });
    expect(result.exit).toBe(0);
    // Readable lines with the absolute endpoint URL and a Next: command.
    expect(result.stdout).toContain(`  signing secret: ${SECRET}`);
    expect(result.stdout).toContain(`  endpoint URL: https://run-prose-production.openprose.workers.dev/webhooks/triggers/${TID}`);
    expect(result.stdout).toContain(`Next: prose cli job deliveries ${TID}`);
    expect(result.stderr).toContain("printed on stdout is shown only once");
    expect(result.stderr).not.toContain(SECRET);
  });
});
