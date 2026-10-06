import {NativeCapture} from "../src/adapters/native-capture";
import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { sentinelFixtureImage as sentinelImage } from "./sentinel-fixture";
import { canonicalJson, sha256, verifyRuntimeImage } from "../src/core/image";
import type { RunnerInvocation } from "../src/core/types";
import { runFakeProcessTransport } from "../src/supervision/fake-transport";
import { probeExecutableVersion, superviseStructuredProcess } from "../src/supervision/process";

const fakeHarness = resolve(import.meta.dir, "../../conformance/fake-harness/fake_harness.py");
const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function fixture(argv = ["prose", "run", "fixture.prose.md"]): Promise<{
  invocation: RunnerInvocation;
  image: Awaited<ReturnType<typeof verifyRuntimeImage>>;
  root: string;
  observation: string;
  descendants: string;
}> {
  const root = await mkdtemp(join(tmpdir(), "openprose-bun-supervision-"));
  roots.push(root);
  const image = await verifyRuntimeImage(sentinelImage);
  const task = { schema: image.manifest.taskEnvelope.schemaId, argv, interactionMode: "non-interactive" as const };
  const invocation: RunnerInvocation = {
    schema: "openprose.runner-invocation/1",
    invocationId: "fixture-invocation-0001",
    cwd: root,
    languageImage: { formatVersion: image.manifest.imageFormatVersion, version: image.manifest.imageVersion, sha256: image.aggregateSha256 },
    runner: { name: "bun", version: "0.1.0", commit: "development" },
    harness: "mock",
    transport: "fake-process",
    recursionToken: "openprose:fixture-invocation-0001",
    task,
    taskDigestSha256: await sha256(canonicalJson(task)),
  };
  return {
    invocation,
    image,
    root,
    observation: join(root, "observation.json"),
    descendants: join(root, "descendants.json"),
  };
}

describe("direct process supervision", () => {
  test.each(["success", "delay", "stderr", "fragmented", "crlf"])("accepts the %s fake stream", async (scenario) => {
    const input = await fixture();
    const result = await runFakeProcessTransport(input.invocation, input.image, { PATH: process.env.PATH }, 2_000, {
      executable: fakeHarness,
      scenario,
      delayMs: 25,
      observationFile: input.observation,
    });
    expect(result.error).toBeNull();
    expect(result.exitCode).toBe(0);
    expect(result.harnessVersion).toBe("1.0.0");
    expect(result.terminalEnvelope).toMatchObject({ semanticStatus: "not-applicable" });
    expect(result.events.map((event) => event.type)).toEqual(["session.started", "assistant.message", "session.completed"]);
    expect(result.deliveredImageSha256).toBe(input.image.aggregateSha256);
    if (scenario === "stderr") expect(result.stderr).toContain("stderr remains separate");
    else expect(result.stderr).toBe("");
  });

  test.each([
    ["malformed", "PROTOCOL_MALFORMED"],
    ["truncated", "PROTOCOL_TRUNCATED"],
    ["eof-without-terminal", "PROTOCOL_TRUNCATED"],
    ["nonzero", "PROTOCOL_TRUNCATED"],
    ["terminal-nonzero", "HARNESS_FAILED"],
    ["duplicate-terminal", "PROTOCOL_MALFORMED"],
    ["reordered", "PROTOCOL_MALFORMED"],
  ])("classifies %s without accepting nominal process exit", async (scenario, code) => {
    const input = await fixture();
    const result = await runFakeProcessTransport(input.invocation, input.image, { PATH: process.env.PATH }, 2_000, {
      executable: fakeHarness,
      scenario,
      observationFile: input.observation,
    });
    expect(result.error?.code as string | undefined).toBe(code);
    if (scenario === "eof-without-terminal") {
      expect(result.error?.details?.transportDiagnostic).toEqual({
        schema: "openprose.transport-diagnostic/1",
        reason: "lifecycle-rejection",
      });
    }
  });

  test("preserves the natural zero exit when malformed output races process reaping", async () => {
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const input = await fixture();
      const result = await runFakeProcessTransport(input.invocation, input.image, { PATH: process.env.PATH }, 2_000, {
        executable: fakeHarness,
        scenario: "malformed",
        observationFile: input.observation,
      });
      expect(result.error?.code).toBe("PROTOCOL_MALFORMED");
      expect(result.exitCode).toBe(0);
      expect(result.signal).toBeNull();
    }
  });

  test.skipIf(process.platform === "win32")("preserves a malformed harness natural exit within bounded settlement", async () => {
    const input = await fixture();
    const scriptPath = join(input.root, "malformed-natural-exit.ts");
    const script = [
      'import { writeSync } from "node:fs";',
      'writeSync(1, "{malformed\\n");',
      "process.exit(0);",
    ].join("\n");
    await writeFile(scriptPath, script);
    const started = performance.now();
    const result = await superviseStructuredProcess({
      executable: process.execPath,
      argv: ["--no-env-file", `--config=${resolve(import.meta.dir, "../config/empty-bunfig.toml")}`, scriptPath],
      cwd: input.root,
      environment: { PATH: process.env.PATH },
      invocationId: input.invocation.invocationId,
      recursionToken: input.invocation.recursionToken,
      runNonce: "malformed-natural-exit-nonce",
      startupTimeoutMs: 1_000,
      runTimeoutMs: 2_000,
      graceMs: 50,
      hardKillAfterMs: 500,
    });
    expect(result.error?.code).toBe("PROTOCOL_MALFORMED");
    expect(result.exitCode).toBe(0);
    expect(result.signal).toBeNull();
    expect(result.cancellationReason).toBeNull();
    expect(processExists(result.pid)).toBeFalse();
    expect(processExists(-result.processGroupId!)).toBeFalse();
    expect(performance.now() - started).toBeLessThan(1_000);
  });

  test.skipIf(process.platform === "win32")("forces bounded cleanup when a malformed harness does not exit naturally", async () => {
    const input = await fixture();
    const scriptPath = join(input.root, "malformed-held-open.ts");
    // A requested 100 ms child sleep can exceed the supervisor's 250 ms
    // settlement window when the child is descheduled. Cover the two defined
    // paths separately: immediate natural exit above, deliberately retained
    // process here. No runtime deadline or cleanup assertion is relaxed.
    await writeFile(scriptPath, [
      'import { writeSync } from "node:fs";',
      "setInterval(() => {}, 10_000);",
      'writeSync(1, "{malformed\\n");',
    ].join("\n"));
    const started = performance.now();
    const result = await superviseStructuredProcess({
      executable: process.execPath,
      argv: ["--no-env-file", `--config=${resolve(import.meta.dir, "../config/empty-bunfig.toml")}`, scriptPath],
      cwd: input.root,
      environment: { PATH: process.env.PATH },
      invocationId: input.invocation.invocationId,
      recursionToken: input.invocation.recursionToken,
      runNonce: "malformed-held-open-nonce",
      startupTimeoutMs: 1_000,
      runTimeoutMs: 2_000,
      graceMs: 50,
      hardKillAfterMs: 500,
    });
    try {
      expect(result.error?.code).toBe("PROTOCOL_MALFORMED");
      expect(result.exitCode).toBeNull();
      expect(result.signal).toBe("SIGTERM");
      expect(result.cancellationReason).toBeNull();
      expect(processExists(result.pid)).toBeFalse();
      expect(processExists(-result.processGroupId!)).toBeFalse();
      expect(performance.now() - started).toBeGreaterThanOrEqual(200);
      expect(performance.now() - started).toBeLessThan(1_000);
    } finally {
      // A broken cleanup implementation must fail without leaking this
      // deliberately persistent test process into later tests.
      await requirePidExitAfterSettlement(result.pid, 500);
    }
  });

  test("preserves shell metacharacters only as task JSON data", async () => {
    const marker = "must-not-exist";
    const input = await fixture(["prose", "write", `$(touch ${marker})`, "a;b", "x|y", "*", "snow 雪"]);
    const result = await runFakeProcessTransport(input.invocation, input.image, { PATH: process.env.PATH }, 2_000, {
      executable: fakeHarness,
      observationFile: input.observation,
    });
    expect(result.error).toBeNull();
    const observation = JSON.parse(await readFile(input.observation, "utf8"));
    const task = JSON.parse(Buffer.from(observation.task.bytesBase64, "base64").toString("utf8"));
    expect(task.argv).toEqual(input.invocation.task.argv);
    expect(observation.argv).not.toContain("sh");
    expect(await Bun.file(join(input.root, marker)).exists()).toBeFalse();
  });

  test("passes only the recursion/audit metadata to the fake harness", async () => {
    const input = await fixture();
    const result = await runFakeProcessTransport(input.invocation, input.image, {
      PATH: process.env.PATH,
      OPENAI_API_KEY: "secret-openai",
      ANTHROPIC_API_KEY: "secret-anthropic",
      PROSE_TOKEN: "secret-openprose",
      RANDOM_VALUE: "ambient",
    }, 2_000, { executable: fakeHarness, observationFile: input.observation });
    expect(result.error).toBeNull();
    const observation = JSON.parse(await readFile(input.observation, "utf8"));
    expect(observation.environment).toEqual({
      OPENPROSE_INVOCATION_ID: input.invocation.invocationId,
      OPENPROSE_RECURSION_TOKEN: input.invocation.recursionToken,
      OPENPROSE_RUN_NONCE: `nonce:${input.invocation.invocationId}`,
    });
  });

  test("distinguishes startup deadline from bounded-run cancellation", async () => {
    const startup = await fixture();
    const startupResult = await runFakeProcessTransport(startup.invocation, startup.image, { PATH: process.env.PATH }, 2_000, {
      executable: fakeHarness,
      scenario: "delay",
      delayMs: 200,
      startupTimeoutMs: 30,
      observationFile: startup.observation,
    });
    expect(startupResult.error?.code).toBe("STARTUP_TIMEOUT");

    const bounded = await fixture();
    const boundedResult = await runFakeProcessTransport(bounded.invocation, bounded.image, { PATH: process.env.PATH }, 40, {
      executable: fakeHarness,
      scenario: "delay",
      delayMs: 200,
      startupTimeoutMs: 1_000,
      observationFile: bounded.observation,
    });
    expect(boundedResult.error?.code).toBe("CANCELLED");
    expect(boundedResult.cancellationReason).toBe("timeout");
  });

  test.skipIf(process.platform === "win32")("kills a cancellation-resistant child and grandchild in the owned group", async () => {
    const input = await fixture();
    const result = await runFakeProcessTransport(input.invocation, input.image, { PATH: process.env.PATH }, 5_000, {
      executable: fakeHarness,
      scenario: "descendant",
      cancelAfterMs: 300,
      observationFile: input.observation,
      descendantPidFile: input.descendants,
    });
    expect(result.error?.code).toBe("CANCELLED");
    expect(result.cleanupVerified).toBeFalse();
    const identities = JSON.parse(await readFile(input.descendants, "utf8"));
    expect(identities.runNonce).toBe(`nonce:${input.invocation.invocationId}`);
    expect(identities.processGroupId).toBe(result.processGroupId);
    for (const pid of [identities.childPid, identities.grandchildPid]) {
      await requirePidExitAfterSettlement(pid, 2_000);
    }
  });

  test.skipIf(process.platform === "win32")(
    "fails bounded descendant verification only after leak cleanup succeeds",
    async () => {
      const child = Bun.spawn(
        [Bun.which("python3") ?? "python3", "-c", "import time; time.sleep(30)"],
        {
          stdin: "ignore",
          stdout: "ignore",
          stderr: "ignore",
        },
      );
      const childExited = child.exited;

      try {
        await expect(requirePidExitAfterSettlement(child.pid, 50)).rejects.toThrow(
          `owned descendant ${child.pid} remained alive after process settlement`,
        );
        await childExited;
        expect(processExists(child.pid)).toBeFalse();
      } finally {
        if (processExists(child.pid)) {
          try {
            process.kill(child.pid, "SIGKILL");
          } catch {
            // The fixture already exited.
          }
        }
        await childExited;
        if (!(await waitForPidExit(child.pid, 2_000))) {
          throw new Error(`deadline fixture ${child.pid} survived failure-safe cleanup`);
        }
      }
    },
    5_000,
  );

  test("rejects unknown raw event fields and redacts diagnostics across the process boundary", async () => {
    const input = await fixture();
    const script = join(input.root, "custom-harness.ts");
    await writeFile(script, [
      'console.error("diagnostic custom-fixture-secret TEST_API_KEY=custom-fixture-secret");',
      'console.log(JSON.stringify({schema:"openprose.fake-harness-event/1",type:"session.started",sessionId:"fake-session-0001",harnessVersion:"1.0.0",unexpected:true}));',
    ].join("\n"));
    const result = await superviseStructuredProcess({
      executable: process.execPath,
      argv: [script],
      cwd: input.root,
      environment: { PATH: process.env.PATH, TEST_API_KEY: "custom-fixture-secret" },
      invocationId: input.invocation.invocationId,
      recursionToken: input.invocation.recursionToken,
      runNonce: "custom-nonce",
      startupTimeoutMs: 1_000,
      runTimeoutMs: 2_000,
      graceMs: 100,
      hardKillAfterMs: 1_000,
    });
    expect(result.error?.code).toBe("PROTOCOL_MALFORMED");
    expect(result.stderr).not.toContain("custom-fixture-secret");
    expect(result.stderr).toContain("[REDACTED]");
  });

  test("settles cancellation exactly once when a terminal record races a still-running process", async () => {
    const input = await fixture();
    const script = join(input.root, "terminal-then-wait.ts");
    await writeFile(script, [
      'console.log(JSON.stringify({schema:"openprose.fake-harness-event/1",type:"session.started",sessionId:"fake-session-0001",harnessVersion:"1.0.0"}));',
      'console.log(JSON.stringify({schema:"openprose.fake-harness-event/1",type:"assistant.message",sessionId:"fake-session-0001",text:"terminal race"}));',
      'console.log(JSON.stringify({schema:"openprose.fake-harness-event/1",type:"session.completed",sessionId:"fake-session-0001",terminalEnvelope:{schema:"openprose.sentinel-terminal-envelope/1",semanticStatus:"not-applicable",marker:"OPENPROSE_SENTINEL_TERMINAL_V1"}}));',
      "await Bun.sleep(10_000);",
    ].join("\n"));
    const result = await superviseStructuredProcess({
      executable: process.execPath,
      argv: [script],
      cwd: input.root,
      environment: { PATH: process.env.PATH },
      invocationId: input.invocation.invocationId,
      recursionToken: input.invocation.recursionToken,
      runNonce: "terminal-race-nonce",
      startupTimeoutMs: 1_000,
      runTimeoutMs: 2_000,
      graceMs: 50,
      hardKillAfterMs: 1_000,
      cancelAfterMs: 100,
    });
    expect(result.error?.code).toBe("CANCELLED");
    expect(result.terminalEnvelope).toMatchObject({ semanticStatus: "not-applicable" });
    expect(result.events.filter((event) => event.type === "session.completed")).toHaveLength(1);
    expect(result.cleanupVerified).toBeFalse();
  });

  test.skipIf(process.platform === "win32")("bounded-awaits child status when both output pipes close before process exit", async () => {
    const input = await fixture();
    const script = [
      "import json, os, time",
      "print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)",
      "print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','sessionId':'fake-session-0001','terminalEnvelope':{'schema':'openprose.sentinel-terminal-envelope/1','semanticStatus':'not-applicable','marker':'OPENPROSE_SENTINEL_TERMINAL_V1'}}), flush=True)",
      "os.close(1)",
      "os.close(2)",
      "time.sleep(0.15)",
      "os._exit(0)",
    ].join("\n");
    const started = performance.now();
    const result = await superviseStructuredProcess({
      executable: Bun.which("python3") ?? "python3",
      argv: ["-c", script],
      cwd: input.root,
      environment: { PATH: process.env.PATH },
      invocationId: input.invocation.invocationId,
      recursionToken: input.invocation.recursionToken,
      runNonce: "delayed-exit-status-nonce",
      startupTimeoutMs: 1_000,
      runTimeoutMs: 2_000,
      graceMs: 50,
      hardKillAfterMs: 500,
    });
    expect(result.error).toBeNull();
    expect(result.exitCode).toBe(0);
    expect(result.terminalEventObserved).toBeTrue();
    expect(performance.now() - started).toBeGreaterThanOrEqual(100);
    expect(performance.now() - started).toBeLessThan(1_000);
  });

  test.skipIf(process.platform === "win32")("does not claim detached setsid descendants are contained", async () => {
    const input = await fixture();
    const identityPath = join(input.root, "setsid-escape.json");
    const script = [
      "import json, os, signal, sys, time",
      "pid = os.fork()",
      "if pid == 0:",
      "    os.setsid()",
      "    for fd in (0, 1, 2):",
      "        try: os.close(fd)",
      "        except OSError: pass",
      "    with open(sys.argv[1], 'x', encoding='utf-8') as target:",
      "        json.dump({'nonce':'setsid-escape-v1','pid':os.getpid(),'pgid':os.getpgrp()}, target)",
      "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
      "    time.sleep(30)",
      "    os._exit(0)",
      "deadline = time.monotonic() + 2",
      "while not os.path.exists(sys.argv[1]):",
      "    if time.monotonic() >= deadline: raise RuntimeError('escape identity timeout')",
      "    time.sleep(0.01)",
      "print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)",
      "print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','sessionId':'fake-session-0001','terminalEnvelope':{'schema':'openprose.sentinel-terminal-envelope/1','semanticStatus':'not-applicable','marker':'OPENPROSE_SENTINEL_TERMINAL_V1'}}), flush=True)",
    ].join("\n");
    let identity: { nonce: string; pid: number; pgid: number } | null = null;
    try {
      const result = await superviseStructuredProcess({
        executable: Bun.which("python3") ?? "python3",
        argv: ["-c", script, identityPath],
        cwd: input.root,
        environment: { PATH: process.env.PATH },
        invocationId: input.invocation.invocationId,
        recursionToken: input.invocation.recursionToken,
        runNonce: "setsid-supervisor-nonce",
        startupTimeoutMs: 2_000,
        runTimeoutMs: 3_000,
        graceMs: 50,
        hardKillAfterMs: 500,
      });
      identity = JSON.parse(await readFile(identityPath, "utf8"));
      if (identity === null) throw new Error("setsid fixture identity was absent");
      expect(identity.nonce).toBe("setsid-escape-v1");
      expect(identity.pgid).toBe(identity.pid);
      expect(processExists(identity.pid)).toBeTrue();
      expect(result.error).toBeNull();
      expect(result.cleanupVerified).toBeFalse();
    } finally {
      if (
        identity !== null
        && Number.isSafeInteger(identity.pid)
        && identity.pid > 1
        && identity.pgid === identity.pid
        && identity.pid !== process.pid
      ) {
        try { process.kill(identity.pid, "SIGKILL"); } catch { /* already gone */ }
        if (!(await waitForPidExit(identity.pid, 2_000))) {
          throw new Error(`setsid fixture ${identity.pid} did not settle after cleanup`);
        }
      }
    }
  });

  test.skipIf(process.platform === "win32")("fails bounded when an exited harness leaves structured pipes with a setsid descendant", async () => {
    const input = await fixture();
    const identityPath = join(input.root, "retained-structured-pipe.json");
    const script = retainedPipeProgram([
      "print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)",
      "print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','sessionId':'fake-session-0001','terminalEnvelope':{'schema':'openprose.sentinel-terminal-envelope/1','semanticStatus':'not-applicable','marker':'OPENPROSE_SENTINEL_TERMINAL_V1'}}), flush=True)",
    ]);
    let escapedPid: number | null = null;
    const started = performance.now();
    try {
      const result = await superviseStructuredProcess({
        executable: Bun.which("python3") ?? "python3",
        argv: ["-c", script, identityPath],
        cwd: input.root,
        environment: { PATH: process.env.PATH },
        invocationId: input.invocation.invocationId,
        recursionToken: input.invocation.recursionToken,
        runNonce: "retained-structured-pipe-nonce",
        startupTimeoutMs: 1_000,
        // This guard is not the boundary under test. The original process
        // exits naturally; the fixed post-exit reader-settlement deadline must
        // classify the retained descendant pipes within the assertion below.
        runTimeoutMs: 2_000,
        graceMs: 50,
        hardKillAfterMs: 500,
      });
      escapedPid = JSON.parse(await readFile(identityPath, "utf8")).pid;
      expect(result.error?.code).toBe("PROCESS_CLEANUP_FAILED");
      expect(result.terminalEventObserved).toBeTrue();
      expect(processExists(escapedPid!)).toBeTrue();
      expect(performance.now() - started).toBeLessThan(1_000);
    } finally {
      await killEscapedPid(escapedPid);
    }
  });

  test.skipIf(process.platform === "win32")("reports retained pipes instead of startup timeout after the original PID exits", async () => {
    const input = await fixture();
    const identityPath = join(input.root, "retained-silent-pipe.json");
    const script = retainedPipeProgram([]);
    let escapedPid: number | null = null;
    const started = performance.now();
    try {
      const result = await superviseStructuredProcess({
        executable: Bun.which("python3") ?? "python3",
        argv: ["-c", script, identityPath],
        cwd: input.root,
        environment: { PATH: process.env.PATH },
        invocationId: input.invocation.invocationId,
        recursionToken: input.invocation.recursionToken,
        runNonce: "retained-silent-pipe-nonce",
        // Leave process startup outside the sub-second retained-pipe oracle.
        // The original process exits without output, so post-exit settlement,
        // not the startup timer, must provide the classification below.
        startupTimeoutMs: 2_000,
        runTimeoutMs: 2_000,
        graceMs: 50,
        hardKillAfterMs: 500,
      });
      escapedPid = JSON.parse(await readFile(identityPath, "utf8")).pid;
      expect(result.error?.code).toBe("PROCESS_CLEANUP_FAILED");
      expect(processExists(escapedPid!)).toBeTrue();
      expect(performance.now() - started).toBeLessThan(1_000);
    } finally {
      await killEscapedPid(escapedPid);
    }
  });

  test.skipIf(process.platform === "win32")("fails bounded on malformed output even when a setsid descendant retains both pipes", async () => {
    const input = await fixture();
    const identityPath = join(input.root, "retained-malformed-pipe.json");
    const script = retainedPipeProgram([
      "print('{malformed', flush=True)",
      "print('retained diagnostic', file=sys.stderr, flush=True)",
      "time.sleep(30)",
    ]);
    let escapedPid: number | null = null;
    const started = performance.now();
    try {
      const result = await superviseStructuredProcess({
        executable: Bun.which("python3") ?? "python3",
        argv: ["-c", script, identityPath],
        cwd: input.root,
        environment: { PATH: process.env.PATH },
        invocationId: input.invocation.invocationId,
        recursionToken: input.invocation.recursionToken,
        runNonce: "retained-malformed-pipe-nonce",
        startupTimeoutMs: 1_000,
        runTimeoutMs: 2_000,
        graceMs: 50,
        hardKillAfterMs: 500,
      });
      escapedPid = JSON.parse(await readFile(identityPath, "utf8")).pid;
      expect(result.error?.code).toBe("PROTOCOL_MALFORMED");
      expect(result.exitCode).toBeNull();
      expect(result.signal).toBe("SIGTERM");
      expect(processExists(escapedPid!)).toBeTrue();
      expect(performance.now() - started).toBeLessThan(1_500);
    } finally {
      await killEscapedPid(escapedPid);
    }
  });

  test.skipIf(process.platform === "win32")("fails a version probe bounded when a setsid descendant retains its selected output pipe", async () => {
    const input = await fixture();
    const identityPath = join(input.root, "retained-version-pipe.json");
    const script = retainedPipeProgram(["print('codex-cli 0.149.0-alpha.4.1', flush=True)"]);
    let escapedPid: number | null = null;
    const started = performance.now();
    try {
      await expect(probeExecutableVersion(
        Bun.which("python3") ?? "python3",
        input.root,
        { PATH: process.env.PATH },
        undefined,
        500,
        /^codex-cli 0\.149\.0-alpha\.4\.1$/u,
        process.platform,
        ["-c", script, identityPath],
      )).rejects.toMatchObject({ code: "PROCESS_CLEANUP_FAILED" });
      escapedPid = JSON.parse(await readFile(identityPath, "utf8")).pid;
      expect(processExists(escapedPid!)).toBeTrue();
      expect(performance.now() - started).toBeLessThan(1_500);
    } finally {
      if (escapedPid === null && await Bun.file(identityPath).exists()) {
        escapedPid = JSON.parse(await readFile(identityPath, "utf8")).pid;
      }
      await killEscapedPid(escapedPid);
    }
  });
});

function retainedPipeProgram(parentLines: readonly string[]): string {
  return [
    "import json, os, signal, sys, time",
    "pid = os.fork()",
    "if pid == 0:",
    "    os.setsid()",
    "    with open(sys.argv[1], 'x', encoding='utf-8') as target:",
    "        json.dump({'pid':os.getpid(),'pgid':os.getpgrp()}, target)",
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
    "    time.sleep(30)",
    "    os._exit(0)",
    "deadline = time.monotonic() + 2",
    "while not os.path.exists(sys.argv[1]):",
    "    if time.monotonic() >= deadline: raise RuntimeError('retained-pipe identity timeout')",
    "    time.sleep(0.01)",
    ...parentLines,
  ].join("\n");
}

async function killEscapedPid(pid: number | null): Promise<void> {
  if (pid === null || !Number.isSafeInteger(pid) || pid <= 1 || pid === process.pid) return;
  try { process.kill(pid, "SIGKILL"); } catch { /* already gone */ }
  if (!(await waitForPidExit(pid, 2_000))) {
    throw new Error(`escaped fixture ${pid} did not settle after cleanup`);
  }
}

function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (caught) {
    return (caught as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

async function waitForPidExit(pid: number, timeoutMs: number): Promise<boolean> {
  const deadline = performance.now() + timeoutMs;
  while (processExists(pid) && performance.now() < deadline) await Bun.sleep(10);
  return !processExists(pid);
}

async function requirePidExitAfterSettlement(pid: number, timeoutMs: number): Promise<void> {
  if (await waitForPidExit(pid, timeoutMs)) return;
  try { process.kill(pid, "SIGKILL"); } catch { /* already gone */ }
  const cleanupVerified = await waitForPidExit(pid, 2_000);
  if (!cleanupVerified) {
    throw new Error(`owned descendant ${pid} exceeded its death deadline and leak cleanup failed`);
  }
  throw new Error(`owned descendant ${pid} remained alive after process settlement`);
}

test("native capture retains a parsed rejected record without synthetic completion",async()=>{
 const input=await fixture(),path=join(input.root,"native.jsonl");
 const capture=new NativeCapture(path,["fixture-secret"]);
 const result=await superviseStructuredProcess({
 executable:Bun.which("python3")??"python3",argv:["-c",`print('{"type":"unexpected","text":"fixture-secret"}')`],
 cwd:input.root,environment:{PATH:process.env.PATH},invocationId:input.invocation.invocationId,
 recursionToken:input.invocation.recursionToken,runNonce:"capture-rejected",startupTimeoutMs:1000,
 runTimeoutMs:2000,graceMs:50,hardKillAfterMs:500,onNativeRecord:record=>capture.write(record),
 });
 capture.close();
 expect(result.error?.code).toBe("PROTOCOL_MALFORMED");
 expect(JSON.parse(await readFile(path,"utf8"))).toEqual({type:"unexpected",text:"[REDACTED]"});
 expect(result.events.some(event=>event.type==="session.completed")).toBeFalse();
});
