import { describe, expect, test } from "bun:test";
import hostedDryRunFixture from "../../shared/fixtures/dx/dry-run-default-hosted.json" with { type: "json" };
import mockDryRunFixture from "../../shared/fixtures/dx/dry-run-mock.json" with { type: "json" };
import { runCli, type CliDependencies } from "../src/cli";
import type { RunnerInvocation } from "../src/core/types";
import { sentinelFixtureImage } from "./sentinel-fixture";

function fixture() {
  let stdout = "";
  let stderr = "";
  const invocations: RunnerInvocation[] = [];
  const dependencies: CliDependencies = {
    env: {},
    processCwd: process.cwd(),
    userConfigPath: "/definitely/absent/openprose-cli.toml",
    clock: {
      now: () => "2025-01-01T00:00:00Z",
      monotonicMs: () => 0,
    },
    ids: { invocationId: () => "fixture-invocation-0001" },
    writeStdout: (text) => { stdout += text; },
    writeStderr: (text) => { stderr += text; },
    imageBundle: sentinelFixtureImage,
    observeMockInvocation: (invocation) => { invocations.push(invocation); },
  };
  return { dependencies, stdout: () => stdout, stderr: () => stderr, invocations };
}

function withWorkspace(frozen: unknown): unknown {
  if (Array.isArray(frozen)) return frozen.map(withWorkspace);
  if (frozen !== null && typeof frozen === "object") {
    return Object.fromEntries(Object.entries(frozen).map(([key, value]) => [key, withWorkspace(value)]));
  }
  return frozen === "{{WORKSPACE}}" ? process.cwd() : frozen;
}

describe("frozen Phase-7 developer-experience parity", () => {
  test.each([
    {
      name: "default hosted",
      argv: ["--output", "json", "--dry-run", "write", "fixture request"],
      exitCode: 10,
      expected: hostedDryRunFixture,
    },
    {
      name: "explicit mock",
      argv: ["--harness", "mock", "--output", "json", "--dry-run", "write", "fixture request"],
      exitCode: 0,
      expected: mockDryRunFixture,
    },
  ])("matches the exact $name dry-run authority without starting a harness", async ({ argv, exitCode, expected }) => {
    const io = fixture();
    expect(await runCli(argv, io.dependencies)).toBe(exitCode);
    expect(JSON.parse(io.stdout())).toEqual(withWorkspace(expected));
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
  });

  test("hosted JSONL refusal emits only the canonical failed terminal", async () => {
    const io = fixture();
    expect(await runCli(["--output", "jsonl", "run", "fixture.prose.md"], io.dependencies)).toBe(10);
    const records = io.stdout().trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records.map((record) => record.type)).toEqual(["runner.failed"]);
    expect(records[0].payload).toMatchObject({
      kind: "runner.failed",
      error: {
        code: "HOSTED_UNAVAILABLE",
        action: "To use the hosted service, run `cli run submit FILE --preview`; running programs on this machine needs a local harness (`cli harness list`).",
      },
    });
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
  });

  test("mock JSONL success preserves the opaque task and has one final completed terminal", async () => {
    const io = fixture();
    expect(await runCli([
      "--harness", "mock", "--output", "jsonl", "run", "fixture.prose.md",
    ], io.dependencies)).toBe(0);
    const records = io.stdout().trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records.map((record) => record.type)).toEqual([
      "runner.started", "harness.started", "harness.completed", "runner.completed",
    ]);
    expect(records.filter((record) => record.type === "runner.completed" || record.type === "runner.failed")).toHaveLength(1);
    expect(records.at(-1)?.payload.result).toMatchObject({
      adapter: { id: "mock/in-memory" },
      semantic: { status: "not-applicable" },
      billing: { owner: "test-fixture", authCategory: "none-test-only" },
      runnerExitCode: 0,
    });
    expect(io.invocations).toHaveLength(1);
    expect(io.invocations[0]?.task).toEqual({
      schema: "openprose.task-envelope/1",
      argv: ["prose", "run", "fixture.prose.md"],
      interactionMode: "non-interactive",
    });
    expect(io.stderr()).toBe("");
  });
});
