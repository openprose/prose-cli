import { describe, expect, test } from "bun:test";
import { chmod, lstat, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import configurationFixture from "../../shared/fixtures/operations/configuration-explanation.json" with { type: "json" };
import { humanHarnessList, runCli, type CliDependencies } from "../src/cli";
import { harnesses } from "../src/core/harnesses";
import { sentinelFixtureImage as sentinelImage } from "./sentinel-fixture";
import { canonicalJson, sha256 } from "../src/core/image";
import { humanRunnerCommand, humanRunnerInvocation, humanSafeMultiline, humanSafeScalar } from "../src/core/output";
import type { RunnerInvocation, RuntimeImageBundle, RuntimeImageManifest } from "../src/core/types";

type JsonRecord = Record<string, unknown>;

interface SentinelMutation {
  purpose?: RuntimeImageManifest["purpose"];
  releaseEligible?: boolean;
  terminalSchemaId?: string;
  rawTerminalSchema?: string;
  mutateTerminalSchema?(schema: JsonRecord): void;
}

async function mutatedSentinelImage(mutation: SentinelMutation): Promise<RuntimeImageBundle> {
  const manifest = structuredClone(sentinelImage.manifest);
  if (mutation.purpose !== undefined) manifest.purpose = mutation.purpose;
  if (mutation.releaseEligible !== undefined) manifest.releaseEligible = mutation.releaseEligible;
  if (mutation.terminalSchemaId !== undefined) manifest.terminalEnvelope.schemaId = mutation.terminalSchemaId;
  const files = new Map(sentinelImage.files);
  const terminalPath = manifest.terminalEnvelope.path;
  let terminalText = mutation.rawTerminalSchema;
  if (terminalText === undefined) {
    const schema = JSON.parse(new TextDecoder().decode(files.get(terminalPath))) as JsonRecord;
    mutation.mutateTerminalSchema?.(schema);
    terminalText = `${JSON.stringify(schema, null, 2)}\n`;
  }
  const terminalBytes = new TextEncoder().encode(terminalText);
  files.set(terminalPath, terminalBytes);
  manifest.terminalEnvelope.sha256 = await sha256(terminalBytes);
  return { manifest, files };
}

function terminalProperties(schema: JsonRecord): Record<string, JsonRecord> {
  return schema.properties as Record<string, JsonRecord>;
}

function fixture(overrides: Partial<CliDependencies> = {}) {
  let stdout = "";
  let stderr = "";
  const invocations: RunnerInvocation[] = [];
  const deps: CliDependencies = {
    env: {},
    processCwd: process.cwd(),
    userConfigPath: "/definitely/absent/openprose-cli.toml",
    clock: {
      now: () => "2026-01-02T03:04:05.000Z",
      monotonicMs: () => 100,
    },
    ids: { invocationId: () => "019b76da-a6c8-7000-8000-000000000001" },
    writeStdout: (text) => { stdout += text; },
    writeStderr: (text) => { stderr += text; },
    imageBundle: sentinelImage,
    observeMockInvocation: (invocation) => { invocations.push(invocation); },
    ...overrides,
  };
  return { deps, stdout: () => stdout, stderr: () => stderr, invocations };
}

function operationFixture() {
  const cwd = process.cwd();
  const userConfigPath = join(cwd, "config", "openprose", "cli.toml");
  const io = fixture({
    env: {
      HOME: join(cwd, "home"),
      XDG_CONFIG_HOME: join(cwd, "config"),
      OPENAI_API_KEY: "must-not-appear-openai",
      ANTHROPIC_API_KEY: "must-not-appear-anthropic",
      OPENPROSE_TOKEN: "must-not-appear-openprose",
    },
    processCwd: cwd,
    userConfigPath,
  });
  return { ...io, cwd, userConfigPath };
}

function expectedConfiguration(cwd: string, userConfigPath: string) {
  return {
    ...configurationFixture,
    cwd: { ...configurationFixture.cwd, value: cwd },
    userConfigPath,
  };
}

describe("CLI behavior", () => {
  test("persists an explicit user harness selection and preserves valid settings", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-harness-use-"));
    try {
      const userConfigPath = join(root, "config", "openprose", "cli.toml");
      const first = fixture({
        env: { HOME: join(root, "home"), XDG_CONFIG_HOME: join(root, "config") },
        processCwd: root,
        userConfigPath,
      });
      expect(await runCli(["--output", "json", "cli", "harness", "use", "claude"], first.deps)).toBe(0);
      expect(JSON.parse(first.stdout())).toEqual({
        schema: "openprose.harness-selection/1",
        harness: "claude",
        scope: "user",
        path: userConfigPath,
        changed: true,
      });
      expect(first.stderr()).toBe("");
      expect(await readFile(userConfigPath, "utf8")).toBe('harness = "claude"\n');

      await writeFile(userConfigPath, 'harness = "claude"\ntimeout = "9m"\n');
      const second = fixture({ processCwd: root, userConfigPath });
      expect(await runCli(["--output", "json", "cli", "harness", "use", "codex"], second.deps)).toBe(0);
      expect(JSON.parse(second.stdout())).toMatchObject({ harness: "codex", changed: true });
      expect(await readFile(userConfigPath, "utf8")).toBe('harness = "codex"\ntimeout = "9m"\n');
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("hardens an existing permissive user configuration directory before selection", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-selection-permissions-"));
    try {
      const parent = join(root, "config", "openprose");
      const userConfigPath = join(parent, "cli.toml");
      await mkdir(parent, { recursive: true });
      await chmod(parent, 0o777);
      const io = fixture({ processCwd: root, userConfigPath });

      expect(await runCli(["--output", "json", "cli", "harness", "use", "codex"], io.deps)).toBe(0);

      expect(io.stderr()).toBe("");
      expect((await lstat(parent)).mode & 0o777).toBe(0o700);
      expect((await lstat(userConfigPath)).mode & 0o777).toBe(0o600);
      expect(await readFile(userConfigPath, "utf8")).toBe('harness = "codex"\n');
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test.each([
    ["prime", "prime-harness-login"],
    ["omp", "omp-harness-login"],
  ] as const)("requires and persists an explicit %s selection bundle without starting a harness", async (harness, authProfile) => {
    const root = await mkdtemp(join(tmpdir(), `openprose-bun-${harness}-selection-`));
    try {
      const userConfigPath = join(root, "config", "openprose", "cli.toml");
      const missing = fixture({
        env: {
          HOME: join(root, "home"),
          XDG_CONFIG_HOME: join(root, "config"),
          PROSE_MODEL: "ambient/not-explicit",
          PROSE_AUTH_PROFILE: authProfile,
        },
        processCwd: root,
        userConfigPath,
      });
      expect(await runCli(["--output", "json", "cli", "harness", "use", harness], missing.deps)).toBe(2);
      expect(JSON.parse(missing.stdout())).toMatchObject({
        code: "INVOCATION_INVALID",
        boundary: "invocation",
        message: "Runner invocation is invalid.",
        action: `Invoke the \`cli harness use ${harness}\` runner operation with both the \`--model\` and \`--auth-profile\` options, then retry.`,
        exitCode: 2,
        retryable: false,
        details: {
          adapterId: `${harness}/rpc`,
          reason: "Prime and OMP selection requires explicit CLI --model and --auth-profile options; inherited configuration does not select a credential route.",
          requiredOptions: ["--model", "--auth-profile"],
        },
      });
      expect(missing.invocations).toHaveLength(0);
      await expect(readFile(userConfigPath, "utf8")).rejects.toMatchObject({ code: "ENOENT" });

      const selected = fixture({ processCwd: root, userConfigPath });
      expect(await runCli([
        "cli", "harness", "use", harness,
        "--model", "openai/gpt-5.4",
        "--auth-profile", authProfile,
        "--json",
      ], selected.deps)).toBe(0);
      expect(JSON.parse(selected.stdout())).toEqual({
        schema: "openprose.harness-selection/1",
        harness,
        scope: "user",
        path: userConfigPath,
        changed: true,
      });
      expect(selected.invocations).toHaveLength(0);
      expect(await readFile(userConfigPath, "utf8")).toBe([
        `auth_profile = ${JSON.stringify(authProfile)}`,
        `harness = ${JSON.stringify(harness)}`,
        'model = "openai/gpt-5.4"',
        "",
      ].join("\n"));

      const savedValuesAreNotExplicit = fixture({ processCwd: root, userConfigPath });
      expect(await runCli([
        "--output", "json", "cli", "harness", "use", harness,
      ], savedValuesAreNotExplicit.deps)).toBe(2);
      expect(JSON.parse(savedValuesAreNotExplicit.stdout())).toMatchObject({ code: "INVOCATION_INVALID" });
      expect(await readFile(userConfigPath, "utf8")).toBe([
        `auth_profile = ${JSON.stringify(authProfile)}`,
        `harness = ${JSON.stringify(harness)}`,
        'model = "openai/gpt-5.4"',
        "",
      ].join("\n"));

      const humanMissing = fixture({ processCwd: root, userConfigPath });
      expect(await runCli(["cli", "harness", "use", harness], humanMissing.deps)).toBe(2);
      expect(humanMissing.stdout()).toBe("");
      expect(humanMissing.stderr()).toContain(`[invocation] INVOCATION_INVALID: Runner invocation is invalid.\n`);
      expect(humanMissing.stderr()).toContain(
        `Action: Use the exact runner invocation ${humanRunnerInvocation()} for runner operations. Invoke the \`cli harness use ${harness}\` runner operation with both the \`--model\` and \`--auth-profile\` options, then retry.\n`,
      );
      expect(humanMissing.stderr()).not.toContain("Source: unavailable");
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("a malformed persisted configuration retains configuration-boundary repair", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-malformed-persisted-selection-"));
    try {
      const userConfigPath = join(root, "config", "openprose", "cli.toml");
      await mkdir(join(root, "config", "openprose"), { recursive: true });
      const original = "harness = [\n";
      await writeFile(userConfigPath, original);
      const io = fixture({
        env: { HOME: join(root, "home"), XDG_CONFIG_HOME: join(root, "config") },
        processCwd: root,
        userConfigPath,
      });
      expect(await runCli(["--output", "json", "cli", "harness", "use", "prime"], io.deps)).toBe(2);
      expect(JSON.parse(io.stdout())).toMatchObject({
        code: "CONFIG_INVALID",
        boundary: "configuration",
        message: "Runner configuration is invalid.",
        action: "Correct or remove the reported configuration source or setting, then invoke the `cli config explain` runner operation to verify the repair.",
      });
      expect(await readFile(userConfigPath, "utf8")).toBe(original);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test.each([
    ["codex", "gpt-5.4", "cached-chatgpt-login"],
    ["claude", "claude-sonnet-4-6", "claude-subscription"],
  ] as const)("persists an explicitly selected supported %s model and auth profile", async (harness, model, authProfile) => {
    const root = await mkdtemp(join(tmpdir(), `openprose-bun-${harness}-explicit-selection-`));
    try {
      const userConfigPath = join(root, "cli.toml");
      const io = fixture({ processCwd: root, userConfigPath });
      expect(await runCli([
        "cli", "harness", "use", harness,
        "--model", model,
        "--auth-profile", authProfile,
        "--json",
      ], io.deps)).toBe(0);
      expect(await readFile(userConfigPath, "utf8")).toBe([
        `auth_profile = ${JSON.stringify(authProfile)}`,
        `harness = ${JSON.stringify(harness)}`,
        `model = ${JSON.stringify(model)}`,
        "",
      ].join("\n"));
      expect(io.invocations).toHaveLength(0);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("clears stale route values on simple installed and hosted harness switches", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-selection-clearing-"));
    try {
      const userConfigPath = join(root, "cli.toml");
      await writeFile(userConfigPath, [
        'harness = "prime"',
        'model = "stale/model"',
        'auth_profile = "openrouter"',
        'timeout = "9m"',
        "",
      ].join("\n"));

      const codex = fixture({ processCwd: root, userConfigPath });
      expect(await runCli(["cli", "harness", "use", "codex", "--json"], codex.deps)).toBe(0);
      expect(await readFile(userConfigPath, "utf8")).toBe('harness = "codex"\ntimeout = "9m"\n');

      await writeFile(userConfigPath, 'harness = "prime"\nmodel = "stale/model"\nauth_profile = "openrouter"\ntimeout = "9m"\n');
      const claude = fixture({ processCwd: root, userConfigPath });
      expect(await runCli(["cli", "harness", "use", "claude", "--json"], claude.deps)).toBe(0);
      expect(await readFile(userConfigPath, "utf8")).toBe('harness = "claude"\ntimeout = "9m"\n');

      await writeFile(userConfigPath, 'harness = "prime"\nmodel = "stale/model"\nauth_profile = "openrouter"\ntimeout = "9m"\n');
      const openprose = fixture({ processCwd: root, userConfigPath });
      expect(await runCli(["cli", "harness", "use", "openprose", "--json"], openprose.deps)).toBe(0);
      expect(await readFile(userConfigPath, "utf8")).toBe('harness = "openprose"\ntimeout = "9m"\n');
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("rejects invalid explicit selection values before changing the saved bundle", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-invalid-selection-"));
    try {
      const userConfigPath = join(root, "cli.toml");
      const original = 'harness = "codex"\ntimeout = "9m"\n';
      await writeFile(userConfigPath, original);

      const invalidProfile = fixture({ processCwd: root, userConfigPath });
      expect(await runCli([
        "cli", "harness", "use", "claude", "--auth-profile", "openrouter", "--json",
      ], invalidProfile.deps)).toBe(2);
      expect(JSON.parse(invalidProfile.stdout())).toMatchObject({ code: "CONFIG_INVALID" });
      expect(await readFile(userConfigPath, "utf8")).toBe(original);

      const invalidOpenProse = fixture({ processCwd: root, userConfigPath });
      expect(await runCli([
        "cli", "harness", "use", "openprose", "--model", "openai/gpt-5.4", "--json",
      ], invalidOpenProse.deps)).toBe(2);
      expect(JSON.parse(invalidOpenProse.stdout())).toMatchObject({ code: "CONFIG_INVALID" });
      expect(await readFile(userConfigPath, "utf8")).toBe(original);

      const invalidOpenProseAuth = fixture({ processCwd: root, userConfigPath });
      expect(await runCli([
        "cli", "harness", "use", "openprose", "--auth-profile", "openai", "--json",
      ], invalidOpenProseAuth.deps)).toBe(2);
      expect(JSON.parse(invalidOpenProseAuth.stdout())).toMatchObject({ code: "CONFIG_INVALID" });
      expect(await readFile(userConfigPath, "utf8")).toBe(original);

      const invalidModel = fixture({ processCwd: root, userConfigPath });
      expect(await runCli([
        "cli", "harness", "use", "prime",
        "--model", "not-qualified",
        "--auth-profile", "prime-harness-login",
        "--json",
      ], invalidModel.deps)).toBe(2);
      expect(JSON.parse(invalidModel.stdout())).toMatchObject({ code: "CONFIG_INVALID" });
      expect(await readFile(userConfigPath, "utf8")).toBe(original);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("malformed harness selection with trailing --json emits one machine error", async () => {
    const io = fixture();
    expect(await runCli([
      "cli", "harness", "use", "prime",
      "--model", "openai/gpt-5.4",
      "--model", "openai/gpt-5.4",
      "--json",
    ], io.deps)).toBe(2);
    expect(io.stderr()).toBe("");
    expect(io.stdout().split("\n").filter(Boolean)).toHaveLength(1);
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.runner-error/1",
      code: "INVOCATION_INVALID",
      boundary: "invocation",
      message: "Runner invocation is invalid.",
      action: "Review the runner syntax with the --help option, place global options before cli, and retry the command.",
      exitCode: 2,
      retryable: false,
      details: { reason: "runner option --model was specified more than once" },
    });
    expect(io.invocations).toHaveLength(0);
  });

  test.each([
    ["project-config", "project-config"],
    ["environment", "environment"],
  ] as const)("refuses a saved selection shadowed by an active %s harness", async (setup, expectedSource) => {
    const root = await mkdtemp(join(tmpdir(), `openprose-bun-shadowed-${setup}-`));
    try {
      const userConfigPath = join(root, "user", "cli.toml");
      await mkdir(join(root, "user"), { recursive: true });
      const original = 'harness = "openprose"\ntimeout = "9m"\n';
      await writeFile(userConfigPath, original);
      if (setup === "project-config") {
        await mkdir(join(root, ".prose"), { recursive: true });
        await writeFile(join(root, ".prose", "cli.toml"), 'harness = "claude"\n');
      }
      const shadowed = fixture({
        processCwd: root,
        userConfigPath,
        env: setup === "environment" ? { PROSE_HARNESS: "claude" } : {},
      });
      expect(await runCli([
        "cli", "harness", "use", "codex", "--json",
      ], shadowed.deps)).toBe(2);
      expect(shadowed.stderr()).toBe("");
      expect(JSON.parse(shadowed.stdout())).toMatchObject({
        schema: "openprose.runner-error/1",
        code: "CONFIG_INVALID",
        details: {
          reason: `Saved default codex would not be active because the higher-precedence ${expectedSource} harness selects claude; remove that override or select the same harness.`,
          selectedHarness: "codex",
          effectiveHarness: "claude",
          effectiveHarnessSource: expectedSource,
        },
      });
      expect(shadowed.invocations).toHaveLength(0);
      expect(await readFile(userConfigPath, "utf8")).toBe(original);

      const matching = fixture({
        processCwd: root,
        userConfigPath,
        env: setup === "environment" ? { PROSE_HARNESS: "claude" } : {},
      });
      expect(await runCli([
        "cli", "harness", "use", "claude", "--json",
      ], matching.deps)).toBe(0);
      expect(JSON.parse(matching.stdout())).toMatchObject({ harness: "claude", changed: true });
      expect(matching.invocations).toHaveLength(0);
      expect(await readFile(userConfigPath, "utf8")).toBe('harness = "claude"\ntimeout = "9m"\n');
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("human harness selection confirms the default and gives the verification command", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-human-harness-use-"));
    try {
      const userConfigPath = join(root, "config", "openprose", "cli.toml");
      const io = fixture({
        env: { HOME: join(root, "home"), XDG_CONFIG_HOME: join(root, "config") },
        processCwd: root,
        userConfigPath,
      });
      expect(await runCli(["cli", "harness", "use", "claude"], io.deps)).toBe(0);
      expect(io.stdout()).toBe([
        "Default harness: claude (updated)",
        "Route: claude-subscription (Claude default; not saved)",
        "Model: harness default (not saved)",
        `Configuration: ${userConfigPath}`,
        `Next: ${humanRunnerCommand("cli doctor")}`,
        "",
      ].join("\n"));
      expect(io.stderr()).toBe("");
      expect(io.stdout()).not.toContain("prose cli");

      const unchanged = fixture({
        env: { HOME: join(root, "home"), XDG_CONFIG_HOME: join(root, "config") },
        processCwd: root,
        userConfigPath,
      });
      expect(await runCli(["cli", "harness", "use", "claude"], unchanged.deps)).toBe(0);
      expect(unchanged.stdout()).toContain("Default harness: claude (already selected)\n");
      expect(unchanged.stdout()).toContain(`Next: ${humanRunnerCommand("cli doctor")}\n`);
      expect(unchanged.stdout()).not.toContain("$PROSE");
      expect(unchanged.stderr()).toBe("");
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("default cancellation keeps idempotent signal handlers installed after repeated delivery", async () => {
    const moduleUrl = new URL("../src/cli.ts", import.meta.url).href;
    const source = [
      `const { defaultDependencies } = await import(${JSON.stringify(moduleUrl)});`,
      'const before = { int: process.listenerCount("SIGINT"), term: process.listenerCount("SIGTERM") };',
      "const dependencies = defaultDependencies();",
      'const installed = { int: process.listenerCount("SIGINT"), term: process.listenerCount("SIGTERM") };',
      'process.emit("SIGINT", "SIGINT");',
      'const afterFirst = { int: process.listenerCount("SIGINT"), term: process.listenerCount("SIGTERM") };',
      'process.emit("SIGINT", "SIGINT");',
      'process.emit("SIGTERM", "SIGTERM");',
      'const afterRepeated = { int: process.listenerCount("SIGINT"), term: process.listenerCount("SIGTERM") };',
      "console.log(JSON.stringify({ before, installed, afterFirst, afterRepeated, aborted: dependencies.cancellationSignal.aborted, reason: dependencies.cancellationSignal.reason }));",
      "process.exit(0);",
    ].join("\n");
    const child = Bun.spawn([process.execPath, "--no-env-file", "--eval", source], {
      cwd: process.cwd(),
      env: { PATH: process.env.PATH, LANG: "C.UTF-8" },
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe",
    });
    const [exit, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ]);
    expect(exit).toBe(0);
    expect(stderr).toBe("");
    const observed = JSON.parse(stdout);
    expect(observed.installed).toEqual({
      int: observed.before.int + 1,
      term: observed.before.term + 1,
    });
    expect(observed.afterFirst).toEqual(observed.installed);
    expect(observed.afterRepeated).toEqual(observed.installed);
    expect(observed).toMatchObject({ aborted: true, reason: "SIGINT" });
  });

  test("the default is OpenProse billed and fails closed without fallback", async () => {
    const io = fixture();
    const exit = await runCli(["run", "example.prose.md"], io.deps);

    expect(exit).toBe(10);
    expect(io.invocations).toHaveLength(0);
    expect(io.stdout()).toBe("");
    expect(io.stderr()).toContain("HOSTED_UNAVAILABLE");
    expect(io.stderr()).toContain("OpenProse-billed");
  });

  test("JSON failure emits one object on stdout and no diagnostic contamination", async () => {
    const io = fixture();
    const exit = await runCli(["--output", "json", "run"], io.deps);
    expect(exit).toBe(10);
    const parsed = JSON.parse(io.stdout());
    expect(parsed).toMatchObject({
      schema: "openprose.runner-result/1",
      error: {
        schema: "openprose.runner-error/1",
        code: "HOSTED_UNAVAILABLE",
        boundary: "hosted-service",
        exitCode: 10,
        retryable: false,
      },
      runnerExitCode: 10,
    });
    expect(io.stdout().endsWith("\n")).toBeTrue();
    expect(io.stdout().split("\n")).toHaveLength(2);
    expect(io.stderr()).toBe("");
  });

  test("JSONL failure has exactly one terminal event", async () => {
    const io = fixture();
    const exit = await runCli(["--output=jsonl", "run"], io.deps);
    expect(exit).toBe(10);
    const records = io.stdout().trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records.at(-1)).toMatchObject({
      type: "runner.failed",
      payload: { kind: "runner.failed", error: { code: "HOSTED_UNAVAILABLE" } },
    });
    expect(records.filter((record) => record.type === "runner.failed" || record.type === "runner.completed")).toHaveLength(1);
    expect(io.stderr()).toBe("");
  });

  test("verbose human runs emit only bounded selection and settlement diagnostics", async () => {
    const modelSecret = "model-secret-must-not-appear";
    const taskSecret = "task-secret-must-not-appear";
    const human = fixture();
    expect(await runCli([
      "--harness", "mock", "--model", modelSecret, "--verbose", "write", taskSecret,
    ], human.deps)).toBe(0);
    expect(human.stderr()).toBe(
      "[openprose:verbose] phase=selection harness=mock transport=deterministic execution=run\n"
      + "[openprose:verbose] phase=settlement status=success exitCode=0\n",
    );
    expect(human.stdout()).not.toContain(modelSecret);
    expect(human.stdout()).not.toContain(taskSecret);

    const machine = fixture();
    expect(await runCli([
      "--harness", "mock", "--verbose", "--output", "json", "write", taskSecret,
    ], machine.deps)).toBe(0);
    expect(machine.stderr()).toBe("");
    expect(machine.stdout().trimEnd().split("\n")).toHaveLength(1);
    expect(JSON.parse(machine.stdout()).runnerExitCode).toBe(0);
  });

  test("explicit mock observes exact task argv and succeeds deterministically", async () => {
    const io = fixture();
    const args = ["--harness", "mock", "--output", "json", "write", "雪 and spaces", "$(not-a-shell)", "--harness", "claude"];
    const exit = await runCli(args, io.deps);
    expect(exit).toBe(0);
    expect(io.invocations).toHaveLength(1);
    expect(io.invocations[0]?.task).toEqual({
      schema: "openprose.task-envelope/1",
      argv: ["prose", "write", "雪 and spaces", "$(not-a-shell)", "--harness", "claude"],
      interactionMode: "non-interactive",
    });
    expect(io.invocations[0]?.runner).toEqual({
      name: "bun",
      version: "0.1.0",
      commit: "development",
    });
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.runner-result/1",
      invocationId: "019b76da-a6c8-7000-8000-000000000001",
      adapter: { id: "mock/in-memory", harnessVersion: "1.0.0" },
      terminal: { classification: "success" },
      semantic: { status: "not-applicable" },
      runner: { name: "bun", version: "0.1.0", commit: "development" },
      billing: { owner: "test-fixture", authCategory: "none-test-only" },
      runnerExitCode: 0,
    });
    expect(io.stderr()).toBe("");
  });

  test("deterministic mock derives its exact sentinel terminal from the verified closed schema", async () => {
    const schemaId = "openprose.sentinel-terminal-envelope/derived-fixture";
    const marker = "OPENPROSE_SCHEMA_DERIVED_MARKER";
    const imageBundle = await mutatedSentinelImage({
      terminalSchemaId: schemaId,
      mutateTerminalSchema: (schema) => {
        terminalProperties(schema).schema!.const = schemaId;
        terminalProperties(schema).marker!.const = marker;
      },
    });
    const io = fixture({ imageBundle });
    expect(await runCli(["--harness", "mock", "--output", "json", "run"], io.deps)).toBe(0);
    const result = JSON.parse(io.stdout());
    expect(result.semantic).toEqual({
      status: "not-applicable",
      terminalSchemaSha256: imageBundle.manifest.terminalEnvelope.sha256,
      terminalEnvelopeDigestSha256: await sha256(canonicalJson({
        schema: schemaId,
        semanticStatus: "not-applicable",
        marker,
      })),
    });
  });

  const sentinelAttacks: Array<[
    string, SentinelMutation, "HARNESS_UNAVAILABLE" | "IMAGE_INVALID", 10 | 20,
  ]> = [
    ["non-sentinel purpose", { purpose: "canonical-language-runtime" }, "HARNESS_UNAVAILABLE", 10],
    ["release-eligible non-sentinel", {
      purpose: "canonical-language-runtime", releaseEligible: true,
    }, "HARNESS_UNAVAILABLE", 10],
    ["open terminal schema", {
      mutateTerminalSchema: (schema) => { schema.additionalProperties = true; },
    }, "IMAGE_INVALID", 20],
    ["malformed terminal schema", { rawTerminalSchema: "{not-json\n" }, "IMAGE_INVALID", 20],
    ["wrong terminal schema ID", {
      mutateTerminalSchema: (schema) => {
        terminalProperties(schema).schema!.const = "openprose.some-other-terminal/1";
      },
    }, "IMAGE_INVALID", 20],
    ["wrong semantic status", {
      mutateTerminalSchema: (schema) => { terminalProperties(schema).semanticStatus!.const = "success"; },
    }, "IMAGE_INVALID", 20],
    ["missing required field", {
      mutateTerminalSchema: (schema) => { schema.required = ["schema", "semanticStatus"]; },
    }, "IMAGE_INVALID", 20],
    ["extra terminal property", {
      mutateTerminalSchema: (schema) => { terminalProperties(schema).unexpected = { const: true }; },
    }, "IMAGE_INVALID", 20],
  ];

  test.each(sentinelAttacks)("rejects %s before the mock start/spawn boundary", async (
    _name, mutation, errorCode, exitCode,
  ) => {
    const imageBundle = await mutatedSentinelImage(mutation);
    const io = fixture({
      imageBundle,
      fakeProcess: {
        executable: "/definitely/absent/openprose-fake-harness",
        scenario: "success",
        observationFile: "/definitely/absent/openprose-observation.json",
      },
    });
    expect(await runCli([
      "--harness", "mock", "--transport", "fake-process", "--output", "json", "run",
    ], io.deps)).toBe(exitCode);
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.runner-result/1",
      languageImage: { sha256: imageBundle.manifest.aggregateSha256.sha256 },
      digests: { deliveredImageSha256: null },
      terminal: { transportCompleted: false, terminalEventObserved: false },
      semantic: { status: "unknown", terminalEnvelopeDigestSha256: null },
      runnerExitCode: exitCode,
      error: { code: errorCode, exitCode },
    });
    if (errorCode === "HARNESS_UNAVAILABLE") {
      const result = JSON.parse(io.stdout());
      expect(result).toMatchObject({
        adapter: { id: "mock/unavailable", harnessVersion: null },
        negotiatedCapabilities: {
          promptPlacement: "unsupported",
          isolation: "unsupported",
          streaming: "unsupported",
          cancellation: "unsupported",
          terminal: "unsupported",
        },
      });
      expect(result.adapter.descriptorDigestSha256).toBe(await sha256("mock/unavailable"));
    }
    expect(io.invocations).toHaveLength(0);
    expect(io.stderr()).toBe("");
  });

  test("doctor and inventory block the mock when a verified image is not the sentinel", async () => {
    const imageBundle = await mutatedSentinelImage({ purpose: "canonical-language-runtime" });
    const listIo = fixture({ imageBundle });
    expect(await runCli([
      "--output", "json", "cli", "harness", "list",
    ], listIo.deps)).toBe(0);
    const listedMock = JSON.parse(listIo.stdout()).harnesses.find(
      (item: { id: string }) => item.id === "mock",
    );
    expect(listedMock).toMatchObject({
      availability: "blocked",
      detectedVersion: null,
      strictWrapperConformant: false,
      admissionBlock: "release-image",
    });

    const io = fixture({ imageBundle });
    expect(await runCli([
      "--harness", "mock", "--output", "json", "cli", "doctor",
    ], io.deps)).toBe(10);
    const doctor = JSON.parse(io.stdout());
    expect(doctor).toMatchObject({
      ready: false,
      selectedHarness: "mock",
      selectedHarnessVersion: null,
      promptPlacement: null,
      isolation: "unsupported",
      problems: [{ code: "HARNESS_UNAVAILABLE", exitCode: 10 }],
    });
    expect(doctor.harnesses.find((item: { id: string }) => item.id === "mock")).toMatchObject({
      availability: "blocked",
      detectedVersion: null,
      strictWrapperConformant: false,
      admissionBlock: "release-image",
    });

    const dryRunIo = fixture({ imageBundle });
    expect(await runCli([
      "--harness", "mock", "--dry-run", "--output", "json", "run", "fixture.prose.md",
    ], dryRunIo.deps)).toBe(10);
    const dryRun = JSON.parse(dryRunIo.stdout());
    expect(dryRun).toMatchObject({
      adapter: { id: "mock/unavailable", harnessVersion: null },
      negotiatedCapabilities: {
        promptPlacement: "unsupported",
        isolation: "unsupported",
        streaming: "unsupported",
        cancellation: "unsupported",
        terminal: "unsupported",
      },
      error: { code: "HARNESS_UNAVAILABLE", exitCode: 10 },
    });
    expect(dryRun.adapter.descriptorDigestSha256).toBe(await sha256("mock/unavailable"));
    expect(dryRunIo.invocations).toHaveLength(0);
  });

  test("invalid sentinel contracts fail as attempted-run results even in mock dry-run mode", async () => {
    const imageBundle = await mutatedSentinelImage({
      mutateTerminalSchema: (schema) => { schema.additionalProperties = true; },
    });
    const io = fixture({ imageBundle });
    expect(await runCli([
      "--harness", "mock", "--dry-run", "--output", "json", "run",
    ], io.deps)).toBe(20);
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.runner-result/1",
      digests: { deliveredImageSha256: null },
      terminal: { transportCompleted: false, terminalEventObserved: false },
      semantic: { status: "unknown", terminalEnvelopeDigestSha256: null },
      runnerExitCode: 20,
      error: { code: "IMAGE_INVALID" },
    });
    expect(io.invocations).toHaveLength(0);
  });

  test("dry-run verifies and negotiates without invoking the mock", async () => {
    const io = fixture();
    const exit = await runCli(["--harness=mock", "--dry-run", "--output=json", "run"], io.deps);
    expect(exit).toBe(0);
    expect(io.invocations).toHaveLength(0);
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.runner-dry-run-report/1",
      wouldStartModel: false,
      selection: { harness: "mock", adapterId: "mock/in-memory" },
      readiness: "ready",
      blockingError: null,
    });
  });

  test.skipIf(process.platform === "win32")("human fake-process stderr escapes controls while machine stderr is unchanged", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-human-stderr-"));
    const fakeHarness = resolve(import.meta.dir, "../../conformance/fake-harness/fake_harness.py");
    const raw = "ordinary line\nhostile \\ \u001b]0;owned\u0007\r\t\u0085\u2028\u2029\n";
    try {
      const source = await readFile(fakeHarness, "utf8");
      const executable = join(root, "hostile-fake-harness.py");
      const injected = source.replace(
        "records = _records()",
        [
          "records = _records()",
          '    os.write(sys.stderr.fileno(), b"ordinary line\\nhostile \\\\ \\x1b]0;owned\\x07\\r\\t\\xc2\\x85\\xe2\\x80\\xa8\\xe2\\x80\\xa9\\n")',
        ].join("\n"),
      );
      expect(injected).not.toBe(source);
      await writeFile(executable, injected, { mode: 0o700 });

      for (const scenario of ["success", "malformed"] as const) {
        const makeIo = () => fixture({
          env: { PATH: process.env.PATH },
          processCwd: root,
          userConfigPath: join(root, "absent.toml"),
          fakeProcess: { executable, scenario, observationFile: join(root, `${scenario}.json`) },
        });
        const human = makeIo();
        expect(await runCli([
          "--harness", "mock", "--transport", "fake-process", "run",
        ], human.deps)).toBe(scenario === "success" ? 0 : 22);
        expect(human.stderr()).toContain(humanSafeMultiline(raw));
        for (const unsafe of ["\u001b", "\u0007", "\r", "\t", "\u0085", "\u2028", "\u2029"]) {
          expect(human.stderr()).not.toContain(unsafe);
        }

        const machine = makeIo();
        expect(await runCli([
          "--harness", "mock", "--transport", "fake-process", "--output", "json", "run",
        ], machine.deps)).toBe(scenario === "success" ? 0 : 22);
        expect(machine.stderr()).toContain(raw);
      }
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test.each([
    ["codex", "exec-json"],
    ["prime", "rpc"],
    ["claude", "print-stream-json"],
    ["omp", "rpc"],
  ])("fails closed when the selected %s/%s executable is unavailable", async (harness, transport) => {
    const io = fixture({
      env: harness === "prime" || harness === "omp" ? { PROSE_AUTH_PROFILE: "openrouter" } : {},
    });
    const configured = harness === "prime" || harness === "omp"
      ? ["--model", "fixture/model"]
      : [];
    const exit = await runCli(["--harness", harness, "--transport", transport, ...configured, "--output", "json", "run"], io.deps);
    expect(exit).toBe(10);
    expect(io.invocations).toHaveLength(0);
    expect(io.stderr()).toBe("");
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.runner-result/1",
      adapter: { id: `${harness}/${transport}`, harnessVersion: null },
      transport,
      terminal: {
        classification: "runner-error",
        transportCompleted: false,
        terminalEventObserved: false,
        exitCode: null,
        signal: null,
      },
      semantic: { status: "unknown", terminalEnvelopeDigestSha256: null },
      billing: { owner: "user-provider", authCategory: "harness-managed" },
      runnerExitCode: 10,
      error: {
        code: "HARNESS_UNAVAILABLE",
        details: {
          adapterId: `${harness}/${transport}`,
        },
      },
    });
  });

  test("the delimiter executes cli as opaque language input rather than a runner operation", async () => {
    const io = fixture();
    const exit = await runCli([
      "--harness", "mock", "--output", "json", "--", "cli", "doctor", "--json",
    ], io.deps);
    expect(exit).toBe(0);
    expect(io.invocations[0]?.task.argv).toEqual(["prose", "cli", "doctor", "--json"]);
    expect(JSON.parse(io.stdout())).toMatchObject({ adapter: { id: "mock/in-memory" } });
  });

  test.each([
    [["--help"], "OpenProse outer runner"],
    [["cli", "--help"], "OpenProse outer runner"],
    [["cli", "harness", "--help"], "OpenProse outer runner"],
    [["cli", "harness", "use", "--help"], "OpenProse outer runner"],
    [["cli", "config", "explain", "--help"], "OpenProse outer runner"],
    [["--cwd", "/definitely/missing", "cli", "auth", "--help"], "OpenProse outer runner"],
    [["--version"], "prose 0.1.0 (bun)"],
    [["cli", "harness", "list"], "openprose"],
    [["--harness", "mock", "cli", "doctor"], "ready"],
    [["--harness", "mock", "cli", "config", "explain"], "flag"],
  ])("supports runner UX for %j", async (args, needle) => {
    const io = fixture();
    expect(await runCli(args, io.deps)).toBe(0);
    expect(io.stdout()).toContain(needle);
    expect(io.stderr()).toBe("");
  });

  test("config explain emits the complete closed source-attributed report without secrets or a harness start", async () => {
    const io = operationFixture();
    expect(await runCli(["--output", "json", "cli", "config", "explain"], io.deps)).toBe(0);
    expect(JSON.parse(io.stdout())).toEqual(expectedConfiguration(io.cwd, io.userConfigPath));
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
    expect(io.stdout()).not.toContain("must-not-appear");
    expect(io.stdout()).not.toContain("prose cli");
  });

  test("harness list emits stable ordering and current executable readiness", async () => {
    const io = operationFixture();
    expect(await runCli(["--output", "json", "cli", "harness", "list"], io.deps)).toBe(0);
    const inventory = JSON.parse(io.stdout());
    expect(inventory.schema).toBe("openprose.harness-list/1");
    expect(inventory.harnesses.map((item: { id: string }) => item.id)).toEqual([
      "openprose", "prime", "omp", "codex", "claude", "agents-sdk", "mock",
    ]);
    for (const id of ["prime", "omp", "codex", "claude"]) {
      expect(inventory.harnesses.find((item: { id: string }) => item.id === id)).toMatchObject({
        id, availability: "missing", detectedVersion: null,
      });
    }
    expect(inventory.harnesses.find((item: { id: string }) => item.id === "omp").runtimePrerequisites).toEqual([{
      runtime: "bun",
      versionRange: ">=1.3.14",
      detectedVersion: null,
      availability: "missing",
      repairCommand: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
    }]);
    expect(inventory.harnesses.find((item: { id: string }) => item.id === "mock")).toMatchObject({
      availability: "available", detectedVersion: "1.0.0", strictWrapperConformant: true,
    });
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
    expect(io.stdout()).not.toContain("must-not-appear");
  });

  test("human harness list marks the default, shows transport readiness, and gives ordered next actions", async () => {
    const io = operationFixture();
    expect(await runCli(["cli", "harness", "list"], io.deps)).toBe(0);
    expect(io.stdout()).toBe([
      "Harnesses:",
      "* openprose availability=not-implemented transport=hosted (selected)",
      "  prime availability=missing transport=rpc",
      "  omp availability=missing transport=rpc",
      "    runtime prerequisite: bun; availability: missing; detected version: missing; required version: 1.3.14 or newer",
      "    runtime repair: npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
      "  codex availability=missing transport=exec-json",
      "  claude availability=missing transport=print-stream-json",
      "  agents-sdk availability=missing transport=jsonl",
      "",
      "Test-only harnesses:",
      "  mock availability=available transport=deterministic,fake-process version=1.0.0",
      "",
      `Choose Codex: ${humanRunnerCommand("cli harness use codex")}`,
      `Choose Claude: ${humanRunnerCommand("cli harness use claude")}`,
      "Prime and OMP: set PROSE_MODEL to a fully qualified provider/model installed in that harness; unset or invalid values are refused.",
      `Choose Prime: ${humanRunnerCommand('cli harness use prime --model "$PROSE_MODEL" --auth-profile prime-harness-login')}`,
      `Choose OMP: ${humanRunnerCommand('cli harness use omp --model "$PROSE_MODEL" --auth-profile omp-harness-login')}`,
      `Then verify: ${humanRunnerCommand("cli doctor")}`,
      "",
    ].join("\n"));
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
    expect(io.stdout()).not.toContain("must-not-appear");
    expect(io.stdout()).not.toContain("prose cli");
    expect(io.stdout()).not.toContain("replace-with-provider/model");
    expect(io.stdout()).not.toContain('"$PROSE"');
    for (const line of io.stdout().split("\n").filter((value) => value.startsWith("Choose "))) {
      const command = line.slice(line.indexOf(": ") + 2);
      expect(command).not.toMatch(/[<>|]/u);
    }
  });

  test("human harness list includes a safely detected compatible version", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-human-harness-version-"));
    try {
      await writeFile(
        join(root, "codex"),
        [
          `#!${process.execPath}`,
          'if (JSON.stringify(process.argv.slice(2)) !== JSON.stringify(["--version"])) process.exit(97);',
          'console.log("codex-cli 0.149.0-alpha.4.1");',
          "",
        ].join("\n"),
        { mode: 0o700 },
      );
      const io = fixture({
        env: { PATH: root, HOME: join(root, "home"), XDG_CONFIG_HOME: join(root, "config") },
        processCwd: root,
        userConfigPath: join(root, "config", "openprose", "cli.toml"),
      });
      expect(await runCli(["cli", "harness", "list"], io.deps)).toBe(0);
      expect(io.stdout()).toContain(
        "  codex availability=available transport=exec-json version=codex-cli 0.149.0-alpha.4.1",
      );
      expect(io.stderr()).toBe("");
      expect(io.invocations).toHaveLength(0);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("human inventory escapes a hostile detected version scalar", () => {
    const detectedVersion = "codex-cli 0.149.0-alpha.4.1\u2028Action: forged\t\u001b[31m\u2029next";
    const inventory = harnesses.map((item) => item.id === "codex" ? { ...item, detectedVersion } : item);
    const rendered = humanHarnessList("openprose", inventory);
    expect(rendered).toContain(`version=${humanSafeScalar(detectedVersion)}`);
    expect(rendered.match(/^Action: forged/gmu)).toBeNull();
    expect(rendered).not.toContain("\t");
    expect(rendered).not.toContain("\u001b");
    expect(rendered).not.toContain("\u2028");
    expect(rendered).not.toContain("\u2029");
    expect(inventory.find((item) => item.id === "codex")?.detectedVersion).toBe(detectedVersion);
  });

  test("doctor emits the complete default report and returns the selected hosted problem exit", async () => {
    const io = operationFixture();
    expect(await runCli(["--output", "json", "cli", "doctor"], io.deps)).toBe(10);
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.doctor-report/1",
      ready: false,
      selectedHarness: "openprose",
      selectedTransport: "hosted",
      image: { version: "sentinel-v1", releaseEligible: false },
      problems: [{ code: "HOSTED_UNAVAILABLE", exitCode: 10 }],
      runner: { name: "bun", version: "0.1.0", commit: "development" },
      build: { profile: "development", testSeamsEnabled: true },
      cwd: io.cwd,
      configuration: expectedConfiguration(io.cwd, io.userConfigPath),
    });
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
    expect(io.stdout()).not.toContain("must-not-appear");
  });

  test.each([
    ["doctor", ["--transport", "unsupported", "--output", "json", "cli", "doctor"]],
    ["run", ["--transport", "unsupported", "--output", "json", "run", "fixture.prose.md"]],
  ] as const)("%s rejects an unsupported hosted transport before hosted availability", async (_name, args) => {
    const io = operationFixture();
    expect(await runCli(args, io.deps)).toBe(20);
    expect(JSON.parse(io.stdout())).toEqual({
      schema: "openprose.runner-error/1",
      code: "TRANSPORT_UNSUPPORTED",
      boundary: "adapter",
      message: "The selected harness does not support the requested transport.",
      action: "Choose a transport listed for this harness by the `cli harness list` runner operation.",
      exitCode: 20,
      retryable: false,
      details: {
        harness: "openprose",
        requested: "unsupported",
        supported: ["hosted"],
      },
    });
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
  });

  test("doctor rejects an installed transport before inventory or version probes", async () => {
    if (process.platform === "win32") return;
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-invalid-transport-"));
    try {
      const bin = join(root, "bin");
      const marker = join(root, "version-probe-must-not-run");
      await mkdir(bin, { recursive: true });
      const codex = join(bin, "codex");
      await writeFile(codex, `#!/bin/sh\n: > ${JSON.stringify(marker)}\nprintf '%s\\n' 'codex-cli 0.149.0-alpha.4.1'\n`);
      await chmod(codex, 0o700);
      const io = fixture({
        env: {
          HOME: join(root, "home"),
          XDG_CONFIG_HOME: join(root, "config"),
          PATH: bin,
        },
        processCwd: root,
        userConfigPath: join(root, "config", "openprose", "cli.toml"),
      });
      expect(await runCli([
        "--harness", "prime",
        "--transport", "exec-json",
        "--model", "fixture/model",
        "--auth-profile", "prime-harness-login",
        "--output", "json",
        "cli", "doctor",
      ], io.deps)).toBe(20);
      expect(JSON.parse(io.stdout())).toEqual({
        schema: "openprose.runner-error/1",
        code: "TRANSPORT_UNSUPPORTED",
        boundary: "adapter",
        message: "The selected harness does not support the requested transport.",
        action: "Choose a transport listed for this harness by the `cli harness list` runner operation.",
        exitCode: 20,
        retryable: false,
        details: {
          harness: "prime",
          requested: "exec-json",
          supported: ["rpc"],
        },
      });
      expect(io.stderr()).toBe("");
      expect(io.invocations).toHaveLength(0);
      await expect(lstat(marker)).rejects.toMatchObject({ code: "ENOENT" });
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("output parse errors retain only an earlier recognized machine channel", async () => {
    const jsonIo = fixture();
    expect(await runCli([
      "--output", "json", "--output", "invalid", "cli", "doctor",
    ], jsonIo.deps)).toBe(2);
    expect(JSON.parse(jsonIo.stdout())).toMatchObject({
      schema: "openprose.runner-error/1",
      code: "INVOCATION_INVALID",
      boundary: "invocation",
    });
    expect(jsonIo.stderr()).toBe("");

    const jsonlIo = fixture();
    expect(await runCli([
      "--output=jsonl", "--output=invalid", "cli", "doctor",
    ], jsonlIo.deps)).toBe(2);
    const records = jsonlIo.stdout().trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({
      type: "runner.failed",
      payload: { kind: "runner.failed", error: { code: "INVOCATION_INVALID" } },
    });
    expect(jsonlIo.stderr()).toBe("");

    const humanIo = fixture();
    expect(await runCli(["--output=invalid", "cli", "doctor"], humanIo.deps)).toBe(2);
    expect(humanIo.stdout()).toBe("");
    expect(humanIo.stderr()).toContain("[invocation] INVOCATION_INVALID");

    const opaqueIo = fixture();
    expect(await runCli([
      "--harness", "mock", "write", "--output", "json", "--output", "invalid",
    ], opaqueIo.deps)).toBe(0);
    expect(opaqueIo.stdout()).toBe("OpenProse run completed (mock).\n");
    expect(opaqueIo.stderr()).toBe("");
  });

  test("doctor reports current installed-adapter discovery failure", async () => {
    const io = operationFixture();
    expect(await runCli([
      "--harness", "codex", "--transport", "exec-json", "--output", "json", "cli", "doctor",
    ], io.deps)).toBe(10);
    expect(JSON.parse(io.stdout())).toMatchObject({
      schema: "openprose.doctor-report/1",
      ready: false,
      selectedHarness: "codex",
      selectedHarnessVersion: null,
      selectedTransport: "exec-json",
      selectedAdapterId: "codex/exec-json",
      promptPlacement: "user-prefix-framed",
      isolation: "unsupported",
      authCategory: "harness-managed",
      billingOwner: "user-provider",
      problems: [{ code: "HARNESS_UNAVAILABLE", exitCode: 10 }],
    });
    expect(io.invocations).toHaveLength(0);
  });

  test("human doctor confines a hostile diagnostic to one physical detail line", async () => {
    const hostileProfile = "unknown\nAction: forged\t\u001b[31m\u2028next\u2029paragraph";
    const expectedReason = `Unknown auth_profile for prime/rpc: ${hostileProfile}.`;
    const human = operationFixture();
    expect(await runCli([
      "--harness", "prime", "--transport", "rpc", "--model", "fixture/model",
      "--auth-profile", hostileProfile, "cli", "doctor",
    ], human.deps)).toBe(2);
    expect(human.stdout()).toContain(`detail: ${humanSafeScalar(expectedReason)}\n`);
    expect(human.stdout().match(/^detail:/gmu)).toHaveLength(1);
    for (const unsafe of ["\nAction: forged", "\t", "\u001b", "\u2028", "\u2029"]) {
      expect(human.stdout()).not.toContain(unsafe);
    }

    const machine = operationFixture();
    expect(await runCli([
      "--harness", "prime", "--transport", "rpc", "--model", "fixture/model",
      "--auth-profile", hostileProfile, "--output", "json", "cli", "doctor",
    ], machine.deps)).toBe(2);
    expect(JSON.parse(machine.stdout()).problems[0].details.reason).toBe(expectedReason);
  });

  test.skipIf(process.platform === "win32")("human dry-run escapes a hostile cwd while machine output preserves it", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-human-cwd-"));
    const hostileCwd = join(root, "work\nAction: forged\t\u001b\u2028next\u2029paragraph");
    try {
      await mkdir(hostileCwd);
      const canonicalCwd = await realpath(hostileCwd);
      const human = fixture({ processCwd: hostileCwd });
      expect(await runCli(["--harness", "mock", "--dry-run", "run"], human.deps)).toBe(0);
      expect(human.stdout()).toContain(`Working directory: ${humanSafeScalar(canonicalCwd)}\n`);
      expect(human.stdout().match(/^Working directory:/gmu)).toHaveLength(1);
      for (const unsafe of ["\nAction: forged", "\t", "\u001b", "\u2028", "\u2029"]) {
        expect(human.stdout()).not.toContain(unsafe);
      }

      const machine = fixture({ processCwd: hostileCwd });
      expect(await runCli([
        "--harness", "mock", "--dry-run", "--output", "json", "run",
      ], machine.deps)).toBe(0);
      expect(JSON.parse(machine.stdout()).cwd).toBe(canonicalCwd);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test.skipIf(process.platform === "win32")("human configuration errors escape a hostile source path and machine output preserves it", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-human-config-"));
    const configRoot = join(root, "config\nAction: forged\t\u001b\u2028next\u2029paragraph");
    const userConfigPath = join(configRoot, "openprose", "cli.toml");
    try {
      await mkdir(join(configRoot, "openprose"), { recursive: true });
      await writeFile(userConfigPath, 'harness = "unsupported"\n');
      const makeIo = () => fixture({
        env: { HOME: join(root, "home"), XDG_CONFIG_HOME: configRoot },
        processCwd: root,
        userConfigPath,
      });
      const human = makeIo();
      expect(await runCli(["cli", "doctor"], human.deps)).toBe(2);
      expect(human.stderr()).toContain(`Source: ${humanSafeScalar(`${userConfigPath}:1`)}\n`);
      expect(human.stderr().match(/^Source:/gmu)).toHaveLength(1);
      expect(human.stderr().match(/^Detail:/gmu)).toHaveLength(1);
      for (const unsafe of ["\nAction: forged", "\t", "\u001b", "\u2028", "\u2029"]) {
        expect(human.stderr()).not.toContain(unsafe);
      }

      const machine = makeIo();
      expect(await runCli(["--output", "json", "cli", "doctor"], machine.deps)).toBe(2);
      expect(JSON.parse(machine.stdout()).details.source).toBe(`${userConfigPath}:1`);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("human doctor exposes a sanitized configuration detail needed to unblock Prime", async () => {
    const io = operationFixture();
    expect(await runCli([
      "--harness", "prime", "--transport", "rpc", "cli", "doctor",
    ], io.deps)).toBe(2);
    expect(io.stdout()).toContain("problem: CONFIG_INVALID — Runner configuration is invalid.");
    expect(io.stdout()).toContain("detail: Prime and OMP require an explicit fully qualified provider/model for functional-alpha execution (`--model` or PROSE_MODEL).");
    expect(io.stdout()).toContain(`action: Use the exact runner invocation ${humanRunnerInvocation()} for runner operations. Correct or remove the reported configuration source or setting, then invoke the \`cli config explain\` runner operation to verify the repair.`);
    expect(io.stdout()).not.toContain("$PROSE");
    expect(io.stderr()).toBe("");
    expect(io.invocations).toHaveLength(0);
  });

  test.each(["status", "login", "logout"])("auth %s defaults to production with hermetic credential store failure", async (command) => {
    const root = await mkdtemp(join(tmpdir(), "prose-account-"));
    try {
      const path = join(root, "service.json");
      await writeFile(path, JSON.stringify({ environment: "production", credential: null, storeAvailable: false, exchanges: [] }));
      const io = operationFixture();
      io.deps.env = { PROSE_TEST_SERVICE_FIXTURE: path };
      expect(await runCli(["--output", "json", "cli", "auth", command], io.deps)).toBe(10);
      expect(JSON.parse(io.stdout())).toMatchObject({ environment: "production", problem: { code: "CREDENTIAL_STORE_UNAVAILABLE" } });
      expect(io.stderr()).toBe("");
      expect(io.invocations).toHaveLength(0);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
});
