// Service jobs unit and process-level checks. Service behavior is pinned
// by cli/conformance/cases/service/jobs/ (shared with Rust).
import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { asInteger, INTERVAL_MAX, INTERVAL_MIN, projectJob, SPEC_MAX_BYTES } from "../src/core/service/jobs";
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

  test("detach without --yes plans the encoded DELETE; attach refuses conflicting options and unpinned refs", async () => {
    const detach = await job(["contract", "detach", TID, "exowner1/probe@0123456789abcdef"]);
    expect(detach.exit).toBe(2);
    expect(detach.report!.problem.code).toBe("CONFIRMATION_REQUIRED");
    expect(detach.report!.problem.details.plannedRequest).toMatchObject({ method: "DELETE", description: "Detach a program from a job." });
    expect(detach.report!.problem.details.plannedRequest).not.toHaveProperty("path");
    for (const [extra, reason] of [
      [["--repo", "exowner1/app", "--clear-repo"], "--repo and --clear-repo cannot be combined"],
      [["--commit-output", "exowner1/app", "--clear-commit-output"], "--commit-output and --clear-commit-output cannot be combined"],
      [["--input", "topic=cats", "--clear-input", "topic"], '--input "topic" and --clear-input "topic" cannot be combined: --input sets the input, --clear-input removes it'],
      [["--clear-input", "bad\tname"], '--clear-input "bad\\tname" must be an input name of 1 to 128 characters without control characters'],
      [["--repo", "exowner1/app", "--commit-output", "exowner1/other"], "--commit-output exowner1/other must also be given as --repo exowner1/other[@BRANCH]"],
      [["--replace", "exowner1/probe"], "--replace: program reference"],
      [["--file", "a.md", "--clear-files"], "--file and --clear-files cannot be combined"],
      [["--file", "dir/x=a.md"], '--file "dir/x=a.md": the file name must be 1 to 256 characters without /, \\ or control characters; give it as NAME=PATH'],
      [["--file", "a.md", "--file", "a.md=a.md"], '--file name "a.md" was given more than once; name each file uniquely with NAME=PATH'],
      [["--file", "missing.md"], '--file "missing.md"'],
      [["--file", "dir/"], '--file "dir/" is not a readable file'],
      [["--file", "../.."], '--file "../..": the file name must be'],
    ] as const) {
      const refused = await job(["contract", "attach", TID, "exowner1/probe@0123456789abcdef", ...extra, "--yes"], { files: { "a.md": "a\n" } });
      expect(refused.exit).toBe(2);
      expect(refused.report!.problem.details.reason).toStartWith(reason);
    }
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

describe("Service job contract settings", () => {
  const REF = "exowner1/probe@0123456789abcdef";
  const contractsPath = `/triggers/${TID}/contracts`;
  const webhook = (deliveryMode: string) => ({
    method: "GET", path: `/triggers/${TID}`, status: 200,
    body: { trigger: { id: TID, type: "webhook", createdAt: 1 }, status: { deliveryMode } },
  });
  const listing = (contracts: unknown[]) => ({ method: "GET", path: contractsPath, status: 200, body: { contracts, max_contracts: 5 } });
  const bound = (extra: Record<string, unknown>) => ({
    program_ref: REF, owner: "exowner1", slug: "probe", rev_id: "0123456789abcdef", content: "# private", is_platform_default: false,
    enabled: true, bound_at: 1, inputs: { keep: "1", count: 3 }, model: null, effective_model: "model-luna", reasoning_effort: null,
    repositories: [{ url: "https://github.com/exowner1/app", branch: "main" }],
    output: { type: "commit", repository: "https://github.com/exowner1/app" }, ...extra,
  });
  const fixture = (exchanges: unknown[]) => ({ environment: "production", credentials: { production: KEY }, storeAvailable: true, exchanges });
  const post = (expectedBody: unknown, status = 201, body: unknown = { bound: REF }) => ({ method: "POST", path: contractsPath, expectedBody, status, body });

  test("a merging service gets a new repository with a null branch and the output that committed elsewhere cleared", async () => {
    const result = await job(["contract", "attach", TID, REF, "--repo", "exowner1/other", "--yes"], {
      fixture: fixture([webhook("test"), listing([bound({ environment: null, files: [] })]),
        post({ program_ref: REF, replace_program_ref: REF, repository_url: "https://github.com/exowner1/other", repository_branch: null, output: null })]),
    });
    expect(result.exit).toBe(0);
  });

  test("an older service gets the saved settings merged in full, non-string inputs unchanged", async () => {
    const result = await job(["contract", "attach", TID, REF, "--commit-output", "exowner1/app@out", "--clear-input", "keep", "--yes"], {
      fixture: fixture([webhook("test"), listing([bound({})]),
        post({ program_ref: REF, repository_url: "https://github.com/exowner1/app", repository_branch: "main", inputs: { count: 3 }, output: { type: "commit", repository: "https://github.com/exowner1/app", branch: "out" } })]),
    });
    expect(result.exit).toBe(0);
  });

  test("a commit output needs a repository the runs read", async () => {
    const result = await job(["contract", "attach", TID, REF, "--commit-output", "exowner1/app", "--yes"], {
      fixture: fixture([webhook("test"), listing([])]),
    });
    expect(result.exit).toBe(2);
    expect(result.report!.problem.details.reason).toStartWith("--commit-output exowner1/app must also be given as --repo");
  });

  test("a live job's refusal names switching it to test delivery", async () => {
    const result = await job(["contract", "attach", TID, REF, "--environment", "linux", "--yes"], {
      fixture: fixture([webhook("live"), listing([]), post({ program_ref: REF, environment: "linux" }, 400, { error: "execution configuration is immutable" })]),
    });
    expect(result.exit).toBe(10);
    const problem = result.report!.problem;
    expect(problem.code).toBe("SERVICE_REQUEST_REJECTED");
    expect(problem.details.suggestedArgv).toEqual(["--output", "json", "cli", "job", "update", TID, "--spec-file", "-", "--yes"]);
    expect(problem.details.suggestedStdin).toBe('{"delivery_mode":"test"}\n');
    expect(problem.action).toEndWith(`The job delivers live; to change its binding, switch it to test delivery first: \`prose --output json cli job update ${TID} --spec-file - --yes\` with details.suggestedStdin on standard input.`);
  });

  test("the preview plan digests the merged body, summarizes it and quotes the binding as it will run", async () => {
    const quote = {
      method: "GET", path: "/run/quote", status: 200, body: { hold: { hold_usd: "0.06", ttl_seconds: 900 } },
      query: { program_ref: REF, reasoning_effort: "low", job_type: "webhook" },
    };
    const result = await job(["contract", "attach", TID, REF, "--input", "topic=cats", "--reasoning-effort", "low", "--preview"], {
      fixture: fixture([webhook("test"), listing([]), quote]),
    });
    expect(result.exit).toBe(0);
    expect(result.report!.result.plannedRequest.summary).toEqual({ programRef: REF, reasoning_effort: "low", inputKeys: ["topic"] });
    expect(result.report!.result.plannedRequest.quote).toEqual({ hold: { hold_usd: "0.06", hold_cents: 6, ttl_seconds: 900 } });
  });

  test("a server-merge plan quotes the saved settings with a cleared repository removed", async () => {
    const quote = {
      method: "GET", path: "/run/quote", status: 200, body: { hold: { hold_usd: "0.06", ttl_seconds: 900 } },
      query: { program_ref: REF, model: "model-sol", environment: "linux", job_type: "webhook" },
    };
    const result = await job(["contract", "attach", TID, REF, "--clear-repo", "--model", "model-sol", "--preview"], {
      fixture: fixture([webhook("test"), listing([bound({ environment: "linux" })]), quote]),
    });
    expect(result.exit).toBe(0);
    expect(result.report!.result.plannedRequest.quote.hold.hold_usd).toBe("0.06");
  });

  test("files replace the stored set; clearing an unbound program's files sends nothing", async () => {
    const sent = await job(["contract", "attach", TID, REF, "--file", "a.md", "--clear-files", "--yes"], { files: { "a.md": "a" } });
    expect(sent.exit).toBe(2);
    const unbound = await job(["contract", "attach", TID, REF, "--clear-files", "--yes"], {
      fixture: fixture([webhook("test"), listing([]), post({ program_ref: REF })]),
    });
    expect(unbound.exit).toBe(0);
    const older = await job(["contract", "attach", TID, REF, "--file", "a.md", "--yes"], {
      files: { "a.md": "hi\n" }, human: true,
      fixture: fixture([webhook("test"), listing([bound({})]),
        post({ program_ref: REF, repository_url: "https://github.com/exowner1/app", repository_branch: "main", inputs: { keep: "1", count: 3 }, output: { type: "commit", repository: "https://github.com/exowner1/app" }, files: { "a.md": "aGkK" } })]),
    });
    expect(older.exit).toBe(0);
    expect(older.stdout).toContain("  settings: repository https://github.com/exowner1/app@main; commit output https://github.com/exowner1/app; inputs count, keep; files a.md\n");
  });

  test("--replace onto another program that is already bound is refused before the binding is sent", async () => {
    const other = "exowner1/probe@fedcba9876543210";
    const result = await job(["contract", "attach", TID, other, "--replace=" + REF, "--yes"], {
      fixture: fixture([webhook("test"), listing([bound({}), bound({ program_ref: other })])]),
    });
    expect(result.exit).toBe(2);
    expect(result.report!.problem.details.reason).toBe(`${other} is already bound to job ${TID}; --replace would reset its settings. Change it in place without --replace, or detach ${REF} first`);
    expect(result.report!.problem.details.suggestedArgv).toEqual(["--output", "json", "cli", "job", "contract", "attach", TID, other, "--yes"]);
  });

  test("a move to another revision is a full replace: saved settings and environment are sent, files are noted", async () => {
    const next = "exowner1/probe@fedcba9876543210";
    const result = await job(["contract", "attach", TID, next, "--replace", REF, "--yes"], {
      human: true,
      fixture: fixture([webhook("test"), listing([bound({ environment: "linux", files: [{ name: "a.md", size: 1 }] })]),
        { method: "POST", path: contractsPath, status: 201, body: { bound: next }, expectedBody: {
          program_ref: next, replace_program_ref: REF, environment: "linux", repository_url: "https://github.com/exowner1/app", repository_branch: "main",
          inputs: { keep: "1", count: 3 }, output: { type: "commit", repository: "https://github.com/exowner1/app" },
        } }]),
    });
    expect(result.exit).toBe(0);
    expect(result.stderr).toBe("note: stored files are not carried to the new revision; pass --file to attach them\n");
  });

  test("an object-valued saved input is SERVICE_PROTOCOL_INVALID and nothing is sent", async () => {
    const result = await job(["contract", "attach", TID, REF, "--yes"], { fixture: fixture([listing([bound({ environment: null, inputs: { a: [1] } })])]) });
    expect(result.exit).toBe(10);
    expect(result.report!.problem.details.reason).toBe("unexpected job response: contracts[0].inputs.a");
  });

  test("the live hint is added only for an immutable-configuration refusal", async () => {
    const result = await job(["contract", "attach", TID, REF, "--environment", "linux", "--yes"], {
      fixture: fixture([webhook("live"), listing([]), post({ program_ref: REF, environment: "linux" }, 400, { error: "environment is not offered" })]),
    });
    expect(result.exit).toBe(10);
    expect(result.report!.problem.details).not.toHaveProperty("suggestedStdin");
  });
});
