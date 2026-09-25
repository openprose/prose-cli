import { describe, expect, test } from "bun:test";
import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runCli, type CliDependencies } from "../src/cli";
import { failure } from "../src/core/errors";
import { canonicalJson, sha256 } from "../src/core/image";
import { formatHumanError } from "../src/core/output";
import type { RunnerInvocation } from "../src/core/types";
import deterministicMockDescriptor from "../../shared/fixtures/transport/deterministic-mock-adapter.json" with { type: "json" };
import { sentinelFixtureImage } from "./sentinel-fixture";

const cliRoot = resolve(import.meta.dir, "../..");
const schemasRoot = resolve(cliRoot, "shared/schemas");
const ajv = new Ajv2020({ allErrors: true, strict: true, strictTypes: false, strictRequired: false });
addFormats(ajv);
ajv.addKeyword({ keyword: "x-openprose-volatile", schemaType: "boolean", valid: true });
for (const name of [
  "common",
  "adapter-diagnostic",
  "transport-diagnostic",
  "native-configuration",
  "native-limits",
  "native-output-limits",
  "native-failure",
  "runner-error",
  // runner-error's `details.planned` is `service-operation.schema.json#/$defs/plannedRequest`
  //; without it Ajv throws "can't resolve reference" and no test in this file runs.
  // cli/shared/tests/test_service_contract.py (BunSchemaRegistrationTest) keeps this list
  // closed under `$ref` in the shared-contracts gate.
  "service-operation",
  "runner-invocation",
  "runner-result",
  "normalized-event",
  "runner-dry-run-report",
]) {
  ajv.addSchema(JSON.parse(readFileSync(resolve(schemasRoot, `${name}.schema.json`), "utf8")));
}

const validateInvocation = ajv.getSchema("https://schemas.openprose.org/cli/v1/runner-invocation.schema.json")!;
const validateAdapterDiagnostic = ajv.getSchema("https://schemas.openprose.org/cli/v1/adapter-diagnostic.schema.json")!;
const validateResult = ajv.getSchema("https://schemas.openprose.org/cli/v1/runner-result.schema.json")!;
const validateEvent = ajv.getSchema("https://schemas.openprose.org/cli/v1/normalized-event.schema.json")!;
const validateDryRun = ajv.getSchema("https://schemas.openprose.org/cli/v1/runner-dry-run-report.schema.json")!;

function fixture() {
  let stdout = "";
  let stderr = "";
  const invocations: RunnerInvocation[] = [];
  const deps: CliDependencies = {
    env: {},
    processCwd: process.cwd(),
    userConfigPath: "/definitely/absent/openprose-cli.toml",
    clock: { now: () => "2025-01-01T00:00:00Z", monotonicMs: () => 0 },
    ids: { invocationId: () => "fixture-invocation-0001" },
    writeStdout: (text) => { stdout += text; },
    writeStderr: (text) => { stderr += text; },
    imageBundle: sentinelFixtureImage,
    observeMockInvocation: (invocation) => { invocations.push(invocation); },
  };
  return { deps, stdout: () => stdout, stderr: () => stderr, invocations };
}

function expectValid(validate: typeof validateResult, value: unknown): void {
  expect(validate(value), JSON.stringify(validate.errors)).toBeTrue();
}

describe("shared runner contracts", () => {
  test("invocation and configuration errors retain distinct frozen machine identities", () => {
    const invocation = failure("INVOCATION_INVALID");
    const configuration = failure("CONFIG_INVALID");
    expect(invocation.toJSON()).toMatchObject({
      code: "INVOCATION_INVALID",
      boundary: "invocation",
      message: "Runner invocation is invalid.",
      exitCode: 2,
      retryable: false,
    });
    expect(configuration.toJSON()).toMatchObject({
      code: "CONFIG_INVALID",
      boundary: "configuration",
      message: "Runner configuration is invalid.",
      exitCode: 2,
      retryable: false,
    });
  });

  test("Claude authentication recovery uses the admitted noninteractive command", () => {
    const auth = failure("HARNESS_NEEDS_AUTH");
    expect(auth.action).toContain("for Claude, run `claude auth login`");
    expect(auth.action).not.toContain("enter `/login`");
    expect(auth.toJSON().action).toBe(auth.action);
    const human = formatHumanError(auth.toJSON());
    expect(human).toContain("for Claude, run `claude auth login`");
    expect(human).not.toContain("enter `/login`");
  });

  test("Prime parser diagnostics use the closed shared schema", () => {
    const diagnostic = {
      schema: "openprose.adapter-diagnostic/1",
      adapterId: "prime/rpc",
      stage: "prime-lifecycle",
      phase: "await-text-delta-or-end",
      counters: {
        acceptedRecords: 13,
        thinkingDeltas: 3,
        textDeltas: 1,
        saturated: false,
      },
    };
    expectValid(validateAdapterDiagnostic, diagnostic);
    expectValid(validateAdapterDiagnostic, { ...diagnostic, phase: "await-thinking-or-text-start" });
    expect(validateAdapterDiagnostic({ ...diagnostic, phase: "await-thinking-start" })).toBeFalse();
    expect(validateAdapterDiagnostic({ ...diagnostic, candidate: "secret" })).toBeFalse();
  });

  test("uses the frozen deterministic mock descriptor without implementation-local drift", async () => {
    expect(await sha256(canonicalJson(deterministicMockDescriptor)))
      .toBe("65849560c167c1ea98d8e6b46c7703b6ceb4b18dc2564c48de5cbba099576c4a");
    const io = fixture();
    expect(await runCli(["--harness=mock", "--output=json", "run"], io.deps)).toBe(0);
    expect(JSON.parse(io.stdout())).toMatchObject({
      adapter: {
        harnessVersion: "1.0.0",
        descriptorDigestSha256: "65849560c167c1ea98d8e6b46c7703b6ceb4b18dc2564c48de5cbba099576c4a",
      },
      negotiatedCapabilities: { cancellation: "unsupported" },
    });
  });

  test("mock result and internal invocation validate and carry reproducible digests", async () => {
    const io = fixture();
    expect(await runCli([
      "--harness", "mock", "--output", "json", "run", "path with spaces/example.prose.md",
      "--model", "雪", "", "line\nbreak", ";$(touch nope)",
    ], io.deps)).toBe(0);
    const result = JSON.parse(io.stdout());
    const invocation = io.invocations[0]!;
    expectValid(validateResult, result);
    expectValid(validateInvocation, invocation);
    expect(result.digests.taskSha256).toBe(await sha256(canonicalJson(invocation.task)));
    expect(result.digests.invocationSha256).toBe(await sha256(canonicalJson(invocation)));
    expect(result.semantic.terminalEnvelopeDigestSha256)
      .toBe("637e07e3440bf91dff3928331aa30935b4277bf18d35ccd11e9e2934dcbe6342");
    expect(io.stderr()).toBe("");
  });

  test("hosted-unavailable is a schema-valid attempted-run result with exact taxonomy", async () => {
    const io = fixture();
    expect(await runCli(["--output", "json", "run", "fixture.prose.md"], io.deps)).toBe(10);
    const result = JSON.parse(io.stdout());
    expectValid(validateResult, result);
    expect(result).toMatchObject({
      billing: { owner: "openprose", authCategory: "openprose-account" },
      runnerExitCode: 10,
      error: {
        code: "HOSTED_UNAVAILABLE",
        action: "To use the hosted service, run `cli run submit FILE --preview`; running programs on this machine needs a local harness (`cli harness list`).",
        details: { suggestedArgv: ["--output", "json", "cli", "run", "submit", "fixture.prose.md", "--preview"] },
      },
    });
    expect(io.invocations).toHaveLength(0);
    expect(io.stderr()).toBe("");
  });

  test("success JSONL validates event-by-event and hashes every preterminal event with LF", async () => {
    const io = fixture();
    expect(await runCli(["--harness=mock", "--output=jsonl", "run"], io.deps)).toBe(0);
    const records = io.stdout().trimEnd().split("\n").map((line) => JSON.parse(line));
    for (const record of records) expectValid(validateEvent, record);
    expect(records.map((record) => record.type)).toEqual([
      "runner.started", "harness.started", "harness.completed", "runner.completed",
    ]);
    const result = records.at(-1)!.payload.result;
    const expectedDigest = await sha256(records.slice(0, -1).map((record) => `${canonicalJson(record)}\n`).join(""));
    expect(result.digests.normalizedEventsSha256).toBe(expectedDigest);
  });

  test("failure JSONL is exactly one schema-valid terminal event", async () => {
    const io = fixture();
    expect(await runCli(["--output=jsonl", "run", "example.prose.md"], io.deps)).toBe(10);
    const records = io.stdout().trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records).toHaveLength(1);
    expectValid(validateEvent, records[0]);
    expect(records[0].type).toBe("runner.failed");
  });

  test("dry-run validates its separate no-execution report", async () => {
    const io = fixture();
    expect(await runCli(["--harness=mock", "--dry-run", "--output=json", "run"], io.deps)).toBe(0);
    expectValid(validateDryRun, JSON.parse(io.stdout()));
    expect(io.invocations).toHaveLength(0);
  });
});
