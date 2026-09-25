import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { chmod, copyFile, mkdir, mkdtemp, readFile, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

const workspace = resolve(import.meta.dir, "..");
const binary = join(workspace, "dist", process.platform === "win32" ? "prose.exe" : "prose");
const liveChildren = new Set<ReturnType<typeof Bun.spawn>>();
// This ceiling bounds test orchestration only. Each child remains registered
// for failure-safe cleanup if the whole test reaches the outer ceiling.
const SERIAL_BUILD_IDENTITY_TEST_TIMEOUT_MS = 30_000;
let hostileCwd = "";
let testBinary = "";

function shellSingleQuote(value: string): string {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

async function terminateChild(child: ReturnType<typeof Bun.spawn>): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    await child.exited;
    return;
  }
  try { child.kill("SIGTERM"); } catch { /* The child may have exited between inspection and signaling. */ }
  const forceTimer = setTimeout(() => {
    if (child.exitCode === null && child.signalCode === null) {
      try { child.kill("SIGKILL"); } catch { /* The child may have exited during the grace period. */ }
    }
  }, 1_000);
  try {
    await child.exited;
  } finally {
    clearTimeout(forceTimer);
  }
}

beforeAll(async () => {
  hostileCwd = await mkdtemp(join(tmpdir(), "openprose-bun-standalone-"));
  await writeFile(join(hostileCwd, ".env"), "PROSE_HARNESS=mock\nPROSE_OUTPUT=json\n");
  await writeFile(join(hostileCwd, "preload.ts"), 'process.env.PROSE_HARNESS = "mock";\n');
  await writeFile(join(hostileCwd, "bunfig.toml"), 'preload = ["./preload.ts"]\n');
  testBinary = join(hostileCwd, process.platform === "win32" ? "prose-test.exe" : "prose-test");
  const build = Bun.spawn([
    process.execPath,
    "run",
    "build",
    "--image-dir", resolve(workspace,"../shared/image/echo-v0"),
  ], {
    cwd: workspace,
    stdout: "pipe",
    stderr: "pipe",
    env: { PATH: process.env.PATH, OPENPROSE_BUILD_COMMIT: "standalone-test-commit" },
  });
  const [exit, stderr] = await Promise.all([build.exited, new Response(build.stderr).text()]);
  if (exit !== 0) throw new Error(`Standalone build failed (${exit}): ${stderr}`);

  const testBuild = Bun.spawn([
    process.execPath,
    "run",
    "build:test",
    "--outfile",
    testBinary,
  ], {
    cwd: workspace,
    stdout: "pipe",
    stderr: "pipe",
    env: { PATH: process.env.PATH, OPENPROSE_BUILD_COMMIT: "standalone-test-commit" },
  });
  const [testExit, testStderr] = await Promise.all([
    testBuild.exited,
    new Response(testBuild.stderr).text(),
  ]);
  if (testExit !== 0) throw new Error(`Test-only standalone build failed (${testExit}): ${testStderr}`);
});

afterAll(async () => {
  if (hostileCwd !== "") await rm(hostileCwd, { recursive: true, force: true });
});

afterEach(async () => {
  const children = [...liveChildren];
  const settlements = await Promise.allSettled(children.map(terminateChild));
  for (const child of children) liveChildren.delete(child);
  const failure = settlements.find((settlement) => settlement.status === "rejected");
  if (failure?.status === "rejected") throw failure.reason;
});

describe("standalone executable", () => {
  test("ordinary and explicit test-only builds have closed image and seam identities", async () => {
    const environment = { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" };
    const ordinary = Bun.spawn([binary, "--output=json", "cli", "doctor"], {
      cwd: hostileCwd,
      env: environment,
      stdout: "pipe",
      stderr: "pipe",
    });
    liveChildren.add(ordinary);
    const [ordinaryExit, ordinaryStdout, ordinaryStderr] = await Promise.all([
      ordinary.exited,
      new Response(ordinary.stdout).text(),
      new Response(ordinary.stderr).text(),
    ]).finally(() => liveChildren.delete(ordinary));
    expect(ordinaryExit).toBe(10);
    expect(ordinaryStderr).toBe("");
    expect(JSON.parse(ordinaryStdout)).toMatchObject({
      image: { version: "echo-v0", releaseEligible: true },
      build: { profile: "development", testSeamsEnabled: false },
    });

    const testOnly = Bun.spawn([
      testBinary,
      "--harness=mock",
      "--output=json",
      "cli",
      "doctor",
    ], {
      cwd: hostileCwd,
      env: environment,
      stdout: "pipe",
      stderr: "pipe",
    });
    liveChildren.add(testOnly);
    const [testExit, testStdout, testStderr] = await Promise.all([
      testOnly.exited,
      new Response(testOnly.stdout).text(),
      new Response(testOnly.stderr).text(),
    ]).finally(() => liveChildren.delete(testOnly));
    expect(testExit).toBe(0);
    expect(testStderr).toBe("");
    expect(JSON.parse(testStdout)).toMatchObject({
      ready: true,
      selectedHarness: "mock",
      image: { version: "sentinel-v1", releaseEligible: false },
      build: { profile: "development", testSeamsEnabled: true },
    });
  }, SERIAL_BUILD_IDENTITY_TEST_TIMEOUT_MS);

  test("does not auto-load cwd .env or bunfig.toml", async () => {
    const child = Bun.spawn([binary, "--output=json", "run", "example.prose.md"], {
      cwd: hostileCwd,
      env: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" },
      stdout: "pipe",
      stderr: "pipe",
    });
    const [exit, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ]);
    expect(exit).toBe(10);
    expect(JSON.parse(stdout)).toMatchObject({
      schema: "openprose.runner-result/1",
      error: { code: "HOSTED_UNAVAILABLE" },
      runnerExitCode: 10,
    });
    expect(stderr).toBe("");
  });

  test("does not expose the deterministic mock with the public alpha image", async () => {
    const child = Bun.spawn([binary, "--harness=mock", "--output=json", "write", "snow 雪", "a;b"], {
      cwd: hostileCwd,
      env: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" },
      stdout: "pipe",
      stderr: "pipe",
    });
    const [exit, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ]);
    expect(exit).toBe(10);
    expect(JSON.parse(stdout)).toMatchObject({
      adapter: { id: "mock/unavailable" },
      error: { code: "HARNESS_UNAVAILABLE" },
      runnerExitCode: 10,
    });
    expect(stderr).toBe("");
  });

  test.skipIf(process.platform === "win32")("runs a discovered live adapter through both standalone and npm launcher surfaces", async () => {
    const harnessRoot = join(hostileCwd, "live-harness-bin");
    await mkdir(harnessRoot);
    const fakeCodex = join(harnessRoot, "codex");
    const terminal = '{"schema":"openprose.echo-terminal/1","semanticStatus":"not-applicable","placeholder":true,"marker":"OPENPROSE_ECHO_TERMINAL_V0","task":{"argv":["prose","run","hello.prose.md"]}}';
    const protocolFrames = [
      JSON.stringify({ type: "thread.started", thread_id: "standalone-thread" }),
      JSON.stringify({ type: "turn.started" }),
      JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: `Echoed task\n${terminal}` } }),
      JSON.stringify({ type: "turn.completed" }),
    ];
    await writeFile(fakeCodex, [
      "#!/bin/sh",
      'if [ "$1" = "--version" ]; then',
      "  printf '%s\\n' 'codex-cli 0.149.0-alpha.4.1'",
      "  exit 0",
      "fi",
      'if [ "$1" = "login" ] && [ "$2" = "status" ]; then',
      "  printf '%s\\n' 'Logged in using ChatGPT'",
      "  exit 0",
      "fi",
      ...protocolFrames.map((frame) => `printf '%s\\n' ${shellSingleQuote(frame)}`),
      "exit 0",
    ].join("\n"), { mode: 0o700 });
    await chmod(fakeCodex, 0o700);
    const env = {
      PATH: `${harnessRoot}:${process.env.PATH ?? ""}`,
      HOME: hostileCwd,
      LANG: "C.UTF-8",
      OPENPROSE_RUNNER_REENTRY: "/ambient-spoof/prose",
    };
    const invoke = async (command: string[]) => {
      const child = Bun.spawn(command, { cwd: hostileCwd, env, stdout: "pipe", stderr: "pipe" });
      liveChildren.add(child);
      const completion = Promise.all([
        child.exited, new Response(child.stdout).text(), new Response(child.stderr).text(),
      ]).then(([exit, stdout, stderr]) => ({ exit, stdout, stderr }));
      let deadline: ReturnType<typeof setTimeout> | undefined;
      try {
        return await Promise.race([
          completion,
          new Promise<never>((_resolve, reject) => {
            deadline = setTimeout(() => {
              reject(new Error(`test child exceeded its 5-second deadline: ${command[0]}`));
            }, 5_000);
          }),
        ]);
      } catch (error) {
        await terminateChild(child);
        await Promise.allSettled([completion]);
        throw error;
      } finally {
        if (deadline !== undefined) clearTimeout(deadline);
        liveChildren.delete(child);
      }
    };
    const args = ["--harness", "codex", "--output", "json", "run", "hello.prose.md"];
    const [direct, directGuidance] = await Promise.all([
      invoke([binary, ...args]),
      invoke([binary, "cli", "harness", "list"]),
    ]);
    expect(direct.exit).toBe(0);
    expect(JSON.parse(direct.stdout)).toMatchObject({
      adapter: { id: "codex/exec-json", harnessVersion: "codex-cli 0.149.0-alpha.4.1" },
      semantic: { status: "not-applicable" },
      runnerExitCode: 0,
    });
    expect(direct.stderr).toBe("");
    expect(directGuidance.exit).toBe(0);
    expect(directGuidance.stdout).toContain(`Then verify: '${binary}' cli doctor`);
    expect(directGuidance.stderr).toBe("");

    const processReport = process.report?.getReport() as { header?: { glibcVersionRuntime?: string } } | undefined;
    const id = process.platform === "linux"
      ? `linux-${process.arch}-${processReport?.header?.glibcVersionRuntime ? "gnu" : "musl"}`
      : `${process.platform}-${process.arch}`;
    const installRoot = join(hostileCwd, "npm-install", "node_modules", "@openprose");
    const metaRoot = join(installRoot, "prose-cli");
    const platformRoot = join(installRoot, `prose-cli-${id}`);
    await mkdir(join(metaRoot, "bin"), { recursive: true });
    await mkdir(join(platformRoot, "bin"), { recursive: true });
    const imageManifestBytes = await readFile(join(workspace, "..", "shared", "image", "echo-v0", "manifest.json"));
    const imageManifest = JSON.parse(imageManifestBytes.toString("utf8")) as {
      imageFormatVersion: string;
      imageVersion: string;
      aggregateSha256: { sha256: string };
      purpose: string;
      releaseEligible: boolean;
    };
    const image = {
      formatVersion: imageManifest.imageFormatVersion,
      version: imageManifest.imageVersion,
      sha256: imageManifest.aggregateSha256.sha256,
      manifestSha256: createHash("sha256").update(imageManifestBytes).digest("hex"),
      purpose: imageManifest.purpose,
      releaseEligible: imageManifest.releaseEligible,
    };
    const admittedPlatforms = [
      "darwin-arm64", "darwin-x64", "linux-arm64-gnu", "linux-x64-gnu", "win32-x64",
    ];
    const cohort = {
      schema: "openprose.npm-cohort/1",
      version: "0.1.0",
      sourceRevision: "standalone-test-commit",
      releaseChannel: "development",
      purpose: image.purpose,
      image,
      admittedPlatforms,
      semanticStatus: "unverified",
      releaseEligible: false,
      publicationAuthorized: false,
    };
    const launcherTemplate = await readFile(join(workspace, "npm", "bin", "prose.js"), "utf8");
    const launcher = join(metaRoot, "bin", "prose.js");
    const launcherBytes = Buffer.from(launcherTemplate.replace("__OPENPROSE_COHORT__", JSON.stringify(cohort)));
    await writeFile(launcher, launcherBytes);
    await chmod(launcher, 0o755);
    const metaManifestPath = join(metaRoot, "package.json");
    const metaManifest = {
      name: "@openprose/prose-cli",
      version: "0.1.0",
      engines: { node: ">=22.22.3" },
      optionalDependencies: Object.fromEntries(
        admittedPlatforms.map((platform) => [`@openprose/prose-cli-${platform}`, "0.1.0"]),
      ),
      openproseCohort: cohort,
      openproseLauncher: {
        path: "bin/prose.js",
        byteLength: launcherBytes.byteLength,
        sha256: createHash("sha256").update(launcherBytes).digest("hex"),
      },
    };
    const metaManifestBytes = Buffer.from(`${JSON.stringify(metaManifest)}\n`);
    await writeFile(metaManifestPath, metaManifestBytes);
    const packagedBinary = join(platformRoot, "bin", "prose");
    await copyFile(binary, packagedBinary);
    const binaryStat = await stat(packagedBinary);
    const packagedBytes = await readFile(packagedBinary);
    const packagedDigest = createHash("sha256").update(packagedBytes).digest("hex");
    const bunRuntime = {
      "darwin-arm64": { compileTarget: "bun-darwin-arm64", runtimeVariant: "native" },
      "darwin-x64": { compileTarget: "bun-darwin-x64-baseline", runtimeVariant: "baseline" },
      "linux-arm64-gnu": { compileTarget: "bun-linux-arm64", runtimeVariant: "native" },
      "linux-x64-gnu": { compileTarget: "bun-linux-x64-baseline", runtimeVariant: "baseline" },
    }[id];
    if (bunRuntime === undefined) throw new Error(`unsupported standalone npm fixture platform: ${id}`);
    await writeFile(join(platformRoot, "package.json"), `${JSON.stringify({
      name: `@openprose/prose-cli-${id}`,
      version: "0.1.0",
      openproseBinary: "bin/prose",
      openproseBinaryByteLength: binaryStat.size,
      openproseBinarySha256: packagedDigest,
      openproseSourceRevision: cohort.sourceRevision,
      openproseImage: image,
      openproseCohort: cohort,
      openprosePlatform: id,
      openproseBunCompileTarget: bunRuntime.compileTarget,
      openproseBunRuntimeVariant: bunRuntime.runtimeVariant,
      ...(id.startsWith("linux-") ? {
        openproseMinimumGlibc: "2.34",
        openproseRequiredGlibcMaximum: "2.34",
        openproseLinuxExecutionEvidence: "ubuntu-22.04-only",
      } : {}),
    })}\n`);
    const exactLauncher = await realpath(launcher);
    const exactPackagedBinary = await realpath(packagedBinary);
    const [launched, npmGuidance] = await Promise.all([
      invoke(["node", launcher, ...args]),
      invoke([launcher, "cli", "harness", "list"]),
    ]);
    expect(launched.exit).toBe(0);
    expect(JSON.parse(launched.stdout)).toMatchObject({
      adapter: { id: "codex/exec-json" }, semantic: { status: "not-applicable" }, runnerExitCode: 0,
    });
    expect(launched.stderr).toBe("");
    expect(launched.stdout).not.toContain(launcher);
    expect(launched.stdout).not.toContain(packagedBinary);

    expect(npmGuidance.exit).toBe(0);
    expect(npmGuidance.stdout).toContain(`Then verify: '${exactLauncher}' cli doctor`);
    expect(npmGuidance.stdout).not.toContain(`Then verify: '${exactPackagedBinary}' cli doctor`);
    expect(npmGuidance.stdout).not.toContain("ambient-spoof");
    expect(npmGuidance.stderr).toBe("");

    const nestedPlatformRoot = join(metaRoot, "node_modules", "@openprose", `prose-cli-${id}`);
    const nestedPackagedBinary = join(nestedPlatformRoot, "bin", "prose");
    await mkdir(join(nestedPlatformRoot, "bin"), { recursive: true });
    await copyFile(join(platformRoot, "package.json"), join(nestedPlatformRoot, "package.json"));
    await copyFile(packagedBinary, nestedPackagedBinary);
    await chmod(nestedPackagedBinary, 0o755);
    await rm(platformRoot, { recursive: true, force: true });
    const exactNestedPackagedBinary = await realpath(nestedPackagedBinary);
    const nestedGuidance = await invoke([launcher, "cli", "harness", "list"]);
    expect(nestedGuidance.exit).toBe(0);
    expect(nestedGuidance.stdout).toContain(`Then verify: '${exactLauncher}' cli doctor`);
    expect(nestedGuidance.stdout).not.toContain(`Then verify: '${exactNestedPackagedBinary}' cli doctor`);
    expect(nestedGuidance.stderr).toBe("");

    await writeFile(launcher, Buffer.concat([launcherBytes, Buffer.from("\n// changed\n")]));
    await chmod(launcher, 0o755);
    const tamperedLauncherGuidance = await invoke([nestedPackagedBinary, "cli", "harness", "list"]);
    expect(tamperedLauncherGuidance.exit).toBe(0);
    expect(tamperedLauncherGuidance.stdout).toContain(`Then verify: '${exactNestedPackagedBinary}' cli doctor`);
    expect(tamperedLauncherGuidance.stdout).not.toContain(`Then verify: '${exactLauncher}' cli doctor`);
    await writeFile(launcher, launcherBytes);
    await chmod(launcher, 0o755);

    const incompleteMetaManifest = {
      ...metaManifest,
      optionalDependencies: { [`@openprose/prose-cli-${id}`]: "0.1.0" },
    };
    await writeFile(metaManifestPath, `${JSON.stringify(incompleteMetaManifest)}\n`);
    const incompleteCohortGuidance = await invoke([nestedPackagedBinary, "cli", "harness", "list"]);
    expect(incompleteCohortGuidance.exit).toBe(0);
    expect(incompleteCohortGuidance.stdout).toContain(`Then verify: '${exactNestedPackagedBinary}' cli doctor`);
    expect(incompleteCohortGuidance.stdout).not.toContain(`Then verify: '${exactLauncher}' cli doctor`);
    await writeFile(metaManifestPath, metaManifestBytes);

    const tampered = Buffer.from(packagedBytes);
    tampered[Math.floor(tampered.byteLength / 2)]! ^= 1;
    await writeFile(nestedPackagedBinary, tampered);
    const refused = await invoke(["node", launcher, ...args]);
    expect(refused.exit).toBe(1);
    expect(refused.stdout).toBe("");
    expect(refused.stderr).toContain("integrity mismatch (SHA-256)");
  }, 30_000);

  test.skipIf(process.platform === "win32")("rejects an installed-harness alias that resolves to the standalone wrapper", async () => {
    const aliasRoot = join(hostileCwd, "recursive-harness");
    await mkdir(aliasRoot, { recursive: true });
    const alias = join(aliasRoot, "codex");
    await symlink(binary, alias);
    const child = Bun.spawn([
      binary,
      "--harness=codex",
      "--transport=exec-json",
      "--output=json",
      "run",
    ], {
      cwd: hostileCwd,
      env: {
        PATH: aliasRoot,
        HOME: process.env.HOME ?? "",
      },
      stdout: "pipe",
      stderr: "pipe",
    });
    const [exit, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ]);
    expect(exit).toBe(20);
    expect(JSON.parse(stdout)).toMatchObject({
      schema: "openprose.runner-result/1",
      error: { code: "RECURSIVE_INVOCATION" },
    });
    expect(stderr).toBe("");
  });
});
