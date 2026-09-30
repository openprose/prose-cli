import { afterEach, describe, expect, test } from "bun:test";
import { chmod, link, lstat, mkdtemp, mkdir, readdir, rename, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { createConnection, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import {
  preparePrimeRecovery,
  recoverPrimeOwnedService,
  settlePrimeOwnedService,
} from "../src/adapters/prime-owned-service";
import { runInstalledAdapter } from "../src/adapters/runner";
import { runCli, type CliDependencies } from "../src/cli";
import { canonicalJson, sha256, verifyRuntimeImage } from "../src/core/image";
import type { RunnerInvocation, TaskEnvelope } from "../src/core/types";
import taskFixture from "../../shared/fixtures/transport/sentinel-task.json" with { type: "json" };
import primeScenario from "../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json" with { type: "json" };
import { sentinelFixtureImage } from "./sentinel-fixture";

const roots: string[] = [];
const servers: Server[] = [];
const sockets = new Set<Socket>();
const policy = { ioTimeoutMs: 80, shutdownDeadlineMs: 160, pollIntervalMs: 10, maximumFrameBytes: 4_096 };

afterEach(async () => {
  for (const socket of sockets) socket.destroy();
  sockets.clear();
  await Promise.all(servers.splice(0).map((server) => server.listening
    ? new Promise<void>((resolve) => server.close(() => resolve()))
    : Promise.resolve()));
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function ownedPaths(): Promise<{ directory: string; socketPath: string }> {
  const root = await mkdtemp(join(tmpdir(), "openprose-prime-owned-"));
  roots.push(root);
  const directory = join(root, "owned");
  await mkdir(directory, { mode: 0o700 });
  await chmod(directory, 0o700);
  return { directory, socketPath: join(directory, "prime.sock") };
}

async function listen(
  socketPath: string,
  input: {
    hello?: unknown;
    rawHello?: string;
    response?: (command: Record<string, unknown>) => unknown;
    keepListening?: boolean;
    silent?: boolean;
  } = {},
): Promise<{ server: Server; commands: Record<string, unknown>[]; connections: Set<Socket> }> {
  const commands: Record<string, unknown>[] = [];
  const connections = new Set<Socket>();
  const server = createServer((socket: Socket) => {
    connections.add(socket);
    sockets.add(socket);
    socket.once("close", () => {
      connections.delete(socket);
      sockets.delete(socket);
    });
    if (!input.silent) {
      const hello = input.rawHello ?? JSON.stringify(input.hello ?? {
        type: "daemon_hello",
        socketPath,
        protocol: { name: "prime-agent.daemon", version: 7 },
        appVersion: "0.7.0",
        clientId: "fixture-daemon",
        serverCapabilities: [],
      });
      socket.write(`${hello}\n`);
    }
    let buffered = "";
    socket.on("data", async (chunk) => {
      buffered += chunk.toString("utf8");
      const newline = buffered.indexOf("\n");
      if (newline < 0) return;
      const command = JSON.parse(buffered.slice(0, newline)) as Record<string, unknown>;
      commands.push(command);
      const id = command.id;
      const response = input.response?.(command) ?? {
        type: "response",
        id,
        command: "shutdown",
        success: true,
      };
      if (!input.keepListening) {
        await unlink(socketPath);
        server.close();
      }
      socket.end(`${JSON.stringify(response)}\n`);
    });
  });
  servers.push(server);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  return { server, commands, connections };
}

async function closeOwnedFixtureService(
  server: Server,
  connections: Set<Socket>,
  socketPath: string,
): Promise<void> {
  if (server.listening) {
    const closed = new Promise<void>((resolve) => server.close(() => resolve()));
    for (const socket of connections) socket.destroy();
    await closed;
  }
  await unlink(socketPath).catch(() => undefined);
}

describe.skipIf(process.platform === "win32")("Prime owned harness service settlement", () => {
  test("accepts an absent provider-free fake socket and removes no unrelated path", async () => {
    const paths = await ownedPaths();
    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy })).resolves.toBeUndefined();
  });

  test("validates the hello, sends the exact protocol-v7 shutdown envelope, and waits for non-listening", async () => {
    const paths = await ownedPaths();
    const fake = await listen(paths.socketPath);
    await settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy });
    expect(fake.commands).toHaveLength(1);
    const envelope = fake.commands[0]!;
    expect(envelope).toEqual({
      type: "command",
      id: expect.any(String),
      protocol: { name: "prime-agent.daemon", version: 7 },
      clientId: "openprose-wrapper",
      command: { id: envelope.id, type: "shutdown", force: true },
    });
    expect((envelope.id as string).length).toBeLessThanOrEqual(96);
  });

  test("exactly matches an admitted Prime 0.8.1 daemon app version", async () => {
    const paths = await ownedPaths();
    await listen(paths.socketPath, {
      hello: {
        type: "daemon_hello",
        socketPath: paths.socketPath,
        protocol: { name: "prime-agent.daemon", version: 7 },
        appVersion: "0.8.1",
      },
    });
    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.8.1", policy })).resolves.toBeUndefined();
  });

  test.each([
    ["malformed hello", { rawHello: "not-json" }],
    ["wrong protocol", { hello: { type: "daemon_hello", socketPath: "unused", protocol: { name: "other", version: 7 } } }],
    ["wrong socket path", { hello: { type: "daemon_hello", socketPath: "/tmp/unowned.sock", protocol: { name: "prime-agent.daemon", version: 7 }, appVersion: "0.7.0" } }],
    ["wrong app version", { hello: { type: "daemon_hello", socketPath: "__ACTUAL__", protocol: { name: "prime-agent.daemon", version: 7 }, appVersion: "9.9.9" } }],
  ])("rejects %s without sending shutdown", async (_label, behavior) => {
    const paths = await ownedPaths();
    const resolved = JSON.parse(JSON.stringify(behavior).replace("__ACTUAL__", paths.socketPath));
    const fake = await listen(paths.socketPath, resolved);
    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy })).rejects.toMatchObject({
      code: "PROCESS_CLEANUP_FAILED",
    });
    expect(fake.commands).toHaveLength(0);
  });

  test("rejects a bad shutdown response", async () => {
    const paths = await ownedPaths();
    await listen(paths.socketPath, {
      response: () => ({ type: "response", id: "wrong", command: "shutdown", success: true }),
    });
    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy })).rejects.toMatchObject({
      code: "PROCESS_CLEANUP_FAILED",
    });
  });

  test("rejects a silent service within the bounded I/O deadline", async () => {
    const paths = await ownedPaths();
    await listen(paths.socketPath, { silent: true });
    const started = performance.now();
    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy })).rejects.toMatchObject({
      code: "PROCESS_CLEANUP_FAILED",
    });
    expect(performance.now() - started).toBeLessThan(800);
  });

  test("destroys a timed-out connection against a saturated listener backlog", async () => {
    const paths = await ownedPaths();
    const fixtureRoot = dirname(paths.directory);
    const ready = join(fixtureRoot, "backlog.ready");
    const fixture = join(fixtureRoot, "backlog.ts");
    await writeFile(fixture, [
      'import { createServer } from "node:net";',
      "const [socketPath, ready] = process.argv.slice(2);",
      "const server = createServer();",
      "server.listen({ path: socketPath, backlog: 0 }, async () => {",
      "  await Bun.write(ready!, 'ready');",
      "  while (true) {}",
      "});",
    ].join("\n"), { mode: 0o600 });
    const child = Bun.spawn([process.execPath, fixture, paths.socketPath, ready], {
      stdin: "ignore",
      stdout: "ignore",
      stderr: "ignore",
    });
    let filler: Socket | undefined;
    try {
      for (let index = 0; index < 200 && !(await Bun.file(ready).exists()); index += 1) await Bun.sleep(5);
      expect(await Bun.file(ready).exists()).toBeTrue();
      filler = createConnection(paths.socketPath);
      await new Promise<void>((resolveConnect, reject) => {
        filler!.once("connect", resolveConnect);
        filler!.once("error", reject);
      });
      const started = performance.now();
      await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy })).rejects.toMatchObject({
        code: "PROCESS_CLEANUP_FAILED",
      });
      expect(performance.now() - started).toBeLessThan(800);
      expect((await lstat(paths.directory)).isDirectory()).toBeTrue();
      expect((await lstat(paths.socketPath)).isSocket()).toBeTrue();
    } finally {
      filler?.destroy();
      child.kill();
      await child.exited;
    }
  });

  test("does not mistake a refused existing socket for listener disappearance", async () => {
    const paths = await ownedPaths();
    const staleLink = join(dirname(paths.directory), "stale-prime.sock");
    const server = createServer();
    await new Promise<void>((resolveListen, reject) => {
      server.once("error", reject);
      server.listen(paths.socketPath, resolveListen);
    });
    await link(paths.socketPath, staleLink);
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
    await rename(staleLink, paths.socketPath);
    expect((await lstat(paths.socketPath)).isSocket()).toBeTrue();

    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy })).rejects.toMatchObject({
      code: "PROCESS_CLEANUP_FAILED",
    });
    expect((await lstat(paths.directory)).isDirectory()).toBeTrue();
    expect((await lstat(paths.socketPath)).isSocket()).toBeTrue();
  });

  test("rejects a successful response while the exact socket remains listening", async () => {
    const paths = await ownedPaths();
    await listen(paths.socketPath, { keepListening: true });
    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: "0.7.0", policy })).rejects.toMatchObject({
      code: "PROCESS_CLEANUP_FAILED",
    });
  });

  test("fails closed for a non-socket in the private directory", async () => {
    const paths = await ownedPaths();
    await writeFile(paths.socketPath, "not a socket", { mode: 0o600 });
    await expect(settlePrimeOwnedService({ ...paths, detectedVersion: null, policy })).rejects.toMatchObject({
      code: "PROCESS_CLEANUP_FAILED",
    });
  });

  test("scrubs sensitive files before exposing a handle and recovers only its exact service", async () => {
    const root = await mkdtemp("/tmp/op-pr-recovery-");
    roots.push(root);
    const directory = join(root, "openprose-prime-Fixture1");
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o700);
    const socketPath = join(directory, "prime.sock");
    await writeFile(join(directory, "runtime-image.bin"), "image-secret", { mode: 0o600 });
    await writeFile(join(directory, "task.json"), "task-secret", { mode: 0o600 });
    const config = join(directory, "credential-config");
    await mkdir(config, { mode: 0o700 });
    const nested = join(config, "cache");
    await mkdir(nested, { mode: 0o700 });
    await writeFile(join(nested, "credentials.json"), "credential-secret", { mode: 0o600 });
    const outside = join(root, "outside-secret");
    await writeFile(outside, "must-survive", { mode: 0o600 });
    await symlink(outside, join(config, "outside-link"));
    await listen(socketPath);

    const recovery = await preparePrimeRecovery({
      temporaryRoot: root,
      directory,
      socketPath,
      detectedVersion: "0.7.0",
    });
    expect(recovery).not.toBeNull();
    expect(recovery!.handle).toStartWith("prime-v1.openprose-prime-Fixture1.");
    expect((await readdir(directory)).sort()).toEqual([".openprose-prime-cleanup.json", "prime.sock"]);
    expect(await Bun.file(join(directory, "runtime-image.bin")).exists()).toBeFalse();
    expect(await Bun.file(join(directory, "task.json")).exists()).toBeFalse();
    expect(await Bun.file(config).exists()).toBeFalse();
    expect(await Bun.file(outside).text()).toBe("must-survive");

    let stdout = "";
    let stderr = "";
    const dependencies: CliDependencies = {
      env: { OPENAI_API_KEY: "must-not-leak-from-cleanup", PATH: join(root, "hostile-bin") },
      processCwd: "/configuration-is-not-required-for-cleanup",
      recoveryTemporaryRoot: root,
      clock: { now: () => "2026-01-02T03:04:05.000Z", monotonicMs: () => 0 },
      ids: { invocationId: () => "cleanup-operation" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: (text) => { stderr += text; },
    };
    const hostileBin = join(root, "hostile-bin");
    const hostileMarker = join(root, "ambient-prose-was-run");
    await mkdir(hostileBin, { mode: 0o700 });
    await writeFile(join(hostileBin, "prose"), `#!/bin/sh\nprintf attacked > ${hostileMarker}\n`, { mode: 0o700 });
    expect(await runCli(["--output", "json", "cli", "cleanup", "prime", recovery!.handle], dependencies)).toBe(0);
    expect(JSON.parse(stdout)).toEqual({
      schema: "openprose.prime-cleanup/1",
      adapterId: "prime/rpc",
      cleanupHandle: recovery!.handle,
      status: "cleaned",
      serviceSettlement: "verified",
      sensitiveFilesRemoved: true,
      directoryRemoved: true,
    });
    expect(stdout).not.toContain(root);
    expect(stdout).not.toContain("must-not-leak-from-cleanup");
    expect(stderr).toBe("");
    expect(await Bun.file(hostileMarker).exists()).toBeFalse();
    expect(await Bun.file(directory).exists()).toBeFalse();
  });

  test("a failed final rmdir leaves a private ticket that makes retry complete", async () => {
    const root = await mkdtemp("/tmp/op-pr-recovery-");
    roots.push(root);
    const directory = join(root, "openprose-prime-Fixture3");
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o700);
    const socketPath = join(directory, "prime.sock");
    const outside = join(root, "outside-late-entry-target");
    await writeFile(outside, "must-survive", { mode: 0o600 });
    await listen(socketPath);
    const recovery = await preparePrimeRecovery({ temporaryRoot: root, directory, socketPath, detectedVersion: "0.7.0" });
    expect(recovery).not.toBeNull();
    await expect(recoverPrimeOwnedService({
      temporaryRoot: root,
      handle: recovery!.handle,
      policy,
      testBeforeFinalDirectoryRemoval: async (ownedDirectory) => {
        await writeFile(join(ownedDirectory, "injected-finalizer-blocker"), "retry", { mode: 0o600 });
        await symlink(outside, join(ownedDirectory, "injected-finalizer-symlink"));
      },
    })).rejects.toMatchObject({
      code: "PROCESS_CLEANUP_FAILED",
      details: {
        cleanupHandle: recovery!.handle,
        cleanupArgv: ["cli", "cleanup", "prime", recovery!.handle],
      },
    });
    expect(await Bun.file(join(directory, ".openprose-prime-cleanup.json")).exists()).toBeFalse();
    expect((await readdir(root)).filter((name) => name.startsWith(".openprose-prime-cleanup-"))).toHaveLength(1);

    await recoverPrimeOwnedService({ temporaryRoot: root, handle: recovery!.handle, policy });
    expect(await Bun.file(directory).exists()).toBeFalse();
    expect(await Bun.file(outside).text()).toBe("must-survive");
    expect((await readdir(root)).filter((name) => name.startsWith(".openprose-prime-cleanup-"))).toHaveLength(0);
  });

  test("applies the recovery entry budget while streaming and never follows links", async () => {
    const root = await mkdtemp("/tmp/op-pr-recovery-");
    roots.push(root);
    const directory = join(root, "openprose-prime-Fixture6");
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o700);
    const outside = join(root, "outside-entry-limit-target");
    await writeFile(outside, "must-survive", { mode: 0o600 });
    await symlink(outside, join(directory, "outside-link"));
    for (let index = 0; index < 4_096; index += 1) {
      await writeFile(join(directory, `entry-${String(index).padStart(4, "0")}`), "", { mode: 0o600 });
    }

    const started = performance.now();
    await expect(preparePrimeRecovery({
      temporaryRoot: root,
      directory,
      socketPath: join(directory, "prime.sock"),
      detectedVersion: "0.7.0",
    })).rejects.toMatchObject({ code: "PROCESS_CLEANUP_FAILED" });
    expect(performance.now() - started).toBeLessThan(2_000);
    expect(await Bun.file(outside).text()).toBe("must-survive");
    expect((await lstat(directory)).isDirectory()).toBeTrue();
  });

  test("retry resumes if interrupted after ticket creation", async () => {
    const root = await mkdtemp("/tmp/op-pr-recovery-");
    roots.push(root);
    const directory = join(root, "openprose-prime-Fixture5");
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o700);
    const socketPath = join(directory, "prime.sock");
    await listen(socketPath);
    const recovery = await preparePrimeRecovery({ temporaryRoot: root, directory, socketPath, detectedVersion: "0.7.0" });
    const token = recovery!.handle.split(".")[2]!;
    const ticket = join(root, `.openprose-prime-cleanup-${token}.json`);
    await link(join(directory, ".openprose-prime-cleanup.json"), ticket);

    await recoverPrimeOwnedService({ temporaryRoot: root, handle: recovery!.handle, policy });
    expect(await Bun.file(directory).exists()).toBeFalse();
    expect(await Bun.file(ticket).exists()).toBeFalse();
  });

  test("changed temporary root and symlink substitution fail without contacting the service", async () => {
    const root = await mkdtemp("/tmp/op-pr-recovery-");
    const otherRoot = await mkdtemp("/tmp/op-pr-other-root-");
    roots.push(root, otherRoot);
    const directory = join(root, "openprose-prime-Fixture4");
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o700);
    const socketPath = join(directory, "prime.sock");
    const fake = await listen(socketPath);
    const recovery = await preparePrimeRecovery({ temporaryRoot: root, directory, socketPath, detectedVersion: "0.7.0" });
    await symlink(directory, join(otherRoot, "openprose-prime-Fixture4"));
    await expect(recoverPrimeOwnedService({ temporaryRoot: otherRoot, handle: recovery!.handle, policy })).rejects.toMatchObject({
      code: expect.stringMatching(/^(CONFIG_INVALID|PROCESS_CLEANUP_FAILED)$/),
    });
    expect(fake.commands).toHaveLength(0);
    expect((await lstat(directory)).isDirectory()).toBeTrue();
  });

  test("rejects a mode-tampered marker without contacting or deleting its sibling", async () => {
    const root = await mkdtemp("/tmp/op-pr-recovery-");
    roots.push(root);
    const directory = join(root, "openprose-prime-Fixture2");
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o700);
    const socketPath = join(directory, "prime.sock");
    const fake = await listen(socketPath);
    const recovery = await preparePrimeRecovery({
      temporaryRoot: root,
      directory,
      socketPath,
      detectedVersion: "0.7.0",
    });
    expect(recovery).not.toBeNull();
    await chmod(join(directory, ".openprose-prime-cleanup.json"), 0o644);
    const sibling = join(root, "sibling");
    await writeFile(sibling, "keep", { mode: 0o600 });
    await expect(recoverPrimeOwnedService({ temporaryRoot: root, handle: recovery!.handle, policy })).rejects.toMatchObject({
      code: "CONFIG_INVALID",
    });
    expect(await Bun.file(sibling).text()).toBe("keep");
    expect(fake.commands).toHaveLength(0);
    expect((await lstat(directory)).isDirectory()).toBeTrue();
  });

  test.each(["success", "protocol", "postprocess", "timeout", "cancellation", "child-failure"])(
    "owned-service cleanup failure overrides Prime %s settlement and preserves private files",
    async (mode) => {
      const root = await mkdtemp(join(tmpdir(), "openprose-prime-precedence-"));
      roots.push(root);
      const transportRoot = await mkdtemp("/tmp/op-prime-");
      roots.push(transportRoot);
      const readySignal = join(root, `daemon-${mode}.ready`);
      const listenerReadySignal = join(root, `listener-${mode}.ready`);
      const harness = join(root, `prime-${mode}`);
      const modeBody = mode === "success" || mode === "postprocess"
        ? `for (const frame of ${JSON.stringify(primeScenario.fakeStdout)}) console.log(JSON.stringify(frame));`
        : mode === "protocol"
          ? "console.log('{malformed');"
          : mode === "child-failure"
            ? "process.exit(9);"
            : "await Bun.sleep(10_000);";
      await writeFile(harness, [
        `#!${process.execPath}`,
        `for (let index = 0; index < 400 && !(await Bun.file(${JSON.stringify(listenerReadySignal)}).exists()); index += 1) await Bun.sleep(5);`,
        `if (!(await Bun.file(${JSON.stringify(listenerReadySignal)}).exists())) throw new Error('fixture listener was not ready');`,
        `await Bun.write(${JSON.stringify(readySignal)}, 'ready');`,
        modeBody,
      ].join("\n"), { mode: 0o700 });
      await chmod(harness, 0o700);
      const image = await verifyRuntimeImage(sentinelFixtureImage);
      const task = taskFixture as TaskEnvelope;
      const invocation: RunnerInvocation = {
        schema: "openprose.runner-invocation/1",
        invocationId: `prime-cleanup-${mode}`,
        cwd: root,
        languageImage: {
          formatVersion: image.manifest.imageFormatVersion,
          version: image.manifest.imageVersion,
          sha256: image.aggregateSha256,
        },
        runner: { name: "bun", version: "0.1.0", commit: "test" },
        harness: "prime",
        transport: "rpc",
        recursionToken: `openprose:prime-cleanup-${mode}`,
        task,
        taskDigestSha256: await sha256(canonicalJson(task)),
      };
      const controller = new AbortController();
      const ownedServices: Array<{
        server: Server;
        connections: Set<Socket>;
        directory: string;
        socketPath: string;
      }> = [];
      const running = runInstalledAdapter({
        // This unit fixture exercises Prime's admitted host on either POSIX CI host.
        platform: "darwin", arch: "arm64",
        adapterId: "prime/rpc",
        executable: harness,
        harnessVersion: "0.7.0",
        credentialGroup: "prime-harness-login",
        invocation,
        image,
        ambient: { HOME: join(root, "home"), OPENROUTER_API_KEY: "must-not-leak" },
        timeoutMs: mode === "timeout" ? 500 : 2_000,
        temporaryRoot: transportRoot,
        testHooks: {
          afterTransportDirectoryCreated: async (directory) => {
            const socketPath = join(directory, "prime.sock");
            const fixture = await listen(socketPath, {
              hello: {
                type: "daemon_hello",
                socketPath,
                protocol: { name: "wrong-daemon", version: 7 },
                appVersion: "0.7.0",
              },
              keepListening: true,
            });
            ownedServices.push({
              server: fixture.server,
              connections: fixture.connections,
              directory,
              socketPath,
            });
            await writeFile(listenerReadySignal, "ready", { mode: 0o600 });
          },
        },
        ...(mode === "cancellation" ? { cancelSignal: controller.signal } : {}),
      }).then(() => null, (error: unknown) => error as { code?: string; details?: Record<string, unknown> });
      let serviceClosed = false;
      let ownedService: (typeof ownedServices)[number] | undefined;
      try {
        if (mode === "cancellation") {
          for (let index = 0; index < 600 && !(await Bun.file(readySignal).exists()); index += 1) await Bun.sleep(5);
          expect(await Bun.file(readySignal).exists()).toBeTrue();
          controller.abort();
        }
        const caught = await running;
        ownedService = ownedServices[0];
        if (ownedService === undefined) throw new Error("The owned-service fixture was not installed.");
        expect(caught).toMatchObject({
          code: "PROCESS_CLEANUP_FAILED",
          details: { processResource: "owned-prime-harness-service" },
        });
        expect(JSON.stringify(caught)).not.toContain("must-not-leak");
        const retained = (await readdir(transportRoot)).filter((entry) => entry.startsWith("openprose-prime-"));
        expect(retained).toHaveLength(1);
        const directory = join(transportRoot, retained[0]!);
        expect(directory).toBe(ownedService.directory);
        expect((await readdir(directory)).sort()).toEqual([".openprose-prime-cleanup.json", "prime.sock"]);
        const handle = caught?.details?.cleanupHandle;
        expect(typeof handle).toBe("string");
        expect(caught?.details?.cleanupArgv).toEqual(["cli", "cleanup", "prime", handle]);
        expect(caught?.details?.sensitiveFilesRemoved).toBeTrue();
        expect(JSON.stringify(caught)).not.toContain(transportRoot);
        await closeOwnedFixtureService(ownedService.server, ownedService.connections, ownedService.socketPath);
        serviceClosed = true;
        await expect(lstat(join(directory, "prime.sock"))).rejects.toBeDefined();
        await recoverPrimeOwnedService({ temporaryRoot: transportRoot, handle: handle as string, policy });
        expect(await Bun.file(directory).exists()).toBeFalse();
      } finally {
        if (!serviceClosed && ownedService !== undefined) {
          await closeOwnedFixtureService(ownedService.server, ownedService.connections, ownedService.socketPath);
        }
        await running;
      }
    },
  );
});
