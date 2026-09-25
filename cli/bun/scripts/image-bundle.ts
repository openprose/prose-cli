#!/usr/bin/env bun

import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import packageManifest from "../package.json" with { type: "json" };
import { nativeCompileTarget } from "./compile-target";

type Options = {
  command: "build" | "build-test" | "check";
  imageDir: string;
  bundle: string;
  checksum: string;
  outfile: string;
  requireReleaseEligible: boolean;
  testSeams: boolean;
  /** OpenProse developer build: honours the runtime endpoint override (src/core/service/dev-endpoint.ts). */
  devBuild: boolean;
  buildProfile: "development" | "release";
  buildCommit: string;
  buildVersion: string;
  windowsHostSha256: string;
  windowsHostAdmission: boolean;
  codexInstructionPlacement: "framed" | "developer" | "base";
  publishedKernelStartup: boolean;
};

const bunRoot = resolve(import.meta.dir, "..");
const sharedImageRoot = resolve(bunRoot, "../shared/image");
const generator = resolve(sharedImageRoot, "bundle/image_bundle.py");
const defaultBundle = resolve(sharedImageRoot, "embedded/current.bundle.bin");
const semver = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;

function usage(): never {
  console.error(
    "usage: bun scripts/image-bundle.ts <check|build> [--image-dir PATH] [--bundle PATH] "
      + "[--checksum PATH] [--outfile PATH] [--require-release-eligible] [--test-seams] [--dev-endpoint] "
      + "[--codex-instructions framed|developer|base] "
      + "| build-test [--outfile PATH]",
  );
  process.exit(2);
}

function takeValue(args: string[], index: number, option: string): string {
  const value = args[index + 1];
  if (value === undefined || value.startsWith("--")) {
    console.error(`image-bundle: ${option} requires a path`);
    process.exit(2);
  }
  return resolve(value);
}

function options(argv: string[]): Options {
  const [rawCommand, ...args] = argv;
  if (rawCommand !== "check" && rawCommand !== "build" && rawCommand !== "build-test") usage();
  const testOnly = rawCommand === "build-test";
  const result: Options = {
    command: rawCommand,
    imageDir: resolve(sharedImageRoot, testOnly ? "sentinel-v1" : "echo-v0"),
    bundle: defaultBundle,
    checksum: resolve(sharedImageRoot, "embedded/current.bundle.sha256"),
    outfile: resolve(bunRoot, testOnly ? "dist/prose-test" : "dist/prose"),
    requireReleaseEligible: false,
    testSeams: testOnly,
    devBuild: false,
    buildProfile: "development",
    buildCommit: process.env.OPENPROSE_BUILD_COMMIT ?? "development",
    buildVersion: process.env.OPENPROSE_BUILD_VERSION ?? packageManifest.version,
    windowsHostSha256: process.env.OPENPROSE_WINDOWS_HOST_SHA256 ?? "",
    windowsHostAdmission: process.env.OPENPROSE_WINDOWS_HOST_ADMISSION === "1",
    codexInstructionPlacement: testOnly || args.includes("--test-seams") ? "framed" : "developer",
    publishedKernelStartup: !testOnly && !args.some(a => ["--image-dir", "--bundle", "--checksum", "--test-seams"].includes(a)),
  };
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index]!;
    if (testOnly) {
      if (argument === "--outfile") {
        result.outfile = takeValue(args, index++, argument);
        continue;
      }
      usage();
    }
    if (argument === "--require-release-eligible") {
      result.requireReleaseEligible = true;
      result.buildProfile = "release";
      continue;
    }
    if (argument === "--codex-instructions") {
      const value = args[++index];
      if (value !== "framed" && value !== "developer" && value !== "base") usage();
      result.codexInstructionPlacement = value;
      continue;
    }
    if (argument === "--test-seams") {
      result.testSeams = true;
      continue;
    }
    if (argument === "--dev-endpoint") {
      result.devBuild = true;
      continue;
    }
    if (argument === "--image-dir") result.imageDir = takeValue(args, index++, argument);
    else if (argument === "--bundle") result.bundle = takeValue(args, index++, argument);
    else if (argument === "--checksum") result.checksum = takeValue(args, index++, argument);
    else if (argument === "--outfile") result.outfile = takeValue(args, index++, argument);
    else usage();
  }
  if (testOnly && result.outfile === resolve(bunRoot, "dist/prose")) {
    console.error("image-bundle: build-test must not overwrite the ordinary dist/prose build");
    process.exit(2);
  }
  if (result.publishedKernelStartup && result.codexInstructionPlacement !== "developer") {
    console.error("image-bundle: published startup requires developer append; select an explicit image for other placements");
    process.exit(2);
  }
  if (result.devBuild && result.requireReleaseEligible) {
    console.error("image-bundle: release-eligible builds cannot enable the developer endpoint");
    process.exit(2);
  }
  if (result.devBuild && result.outfile === resolve(bunRoot, "dist/prose")) {
    console.error("image-bundle: a developer-endpoint build must not overwrite the ordinary dist/prose build; pass --outfile");
    process.exit(2);
  }
  if (result.requireReleaseEligible && result.testSeams) {
    console.error("image-bundle: release-eligible builds cannot enable test seams");
    process.exit(2);
  }
  if (!/^[A-Za-z0-9._+-]{1,128}$/u.test(result.buildCommit)) {
    console.error("image-bundle: OPENPROSE_BUILD_COMMIT must be 1-128 portable identity characters");
    process.exit(2);
  }
  if (!semver.test(result.buildVersion)) {
    console.error("image-bundle: OPENPROSE_BUILD_VERSION must be an exact SemVer 2.0.0 version");
    process.exit(2);
  }
  const rawAdmission = process.env.OPENPROSE_WINDOWS_HOST_ADMISSION;
  if (rawAdmission !== undefined && rawAdmission !== "0" && rawAdmission !== "1") {
    console.error("image-bundle: Windows host admission must be exact 0 or 1");
    process.exit(2);
  }
  const rawDigest = process.env.OPENPROSE_WINDOWS_HOST_SHA256;
  if (rawDigest !== undefined && !/^[0-9a-f]{64}$/.test(rawDigest)) {
    console.error("image-bundle: Windows host SHA-256 must be exact lowercase 64 hex");
    process.exit(2);
  }
  if (result.windowsHostAdmission && result.windowsHostSha256.length === 0) {
    console.error("image-bundle: Windows host admission requires an exact compiled SHA-256");
    process.exit(2);
  }
  return result;
}

async function verify(input: Options): Promise<void> {
  const args = [generator, "check", input.imageDir, input.bundle, "--checksum", input.checksum];
  if (input.requireReleaseEligible) args.push("--require-release-eligible");
  const child = Bun.spawn(["python3", ...args], {
    cwd: bunRoot,
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      PYTHONUTF8: "1",
    },
    stdin: "ignore",
    stdout: "inherit",
    stderr: "inherit",
  });
  const exitCode = await child.exited;
  if (exitCode !== 0) throw new Error(`embedded image validation failed with exit code ${exitCode}`);
}

async function generateTestBundle(input: Options): Promise<void> {
  const child = Bun.spawn([
    "python3",
    generator,
    "build",
    input.imageDir,
    input.bundle,
    "--checksum",
    input.checksum,
  ], {
    cwd: bunRoot,
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      PYTHONUTF8: "1",
    },
    stdin: "ignore",
    stdout: "inherit",
    stderr: "inherit",
  });
  const exitCode = await child.exited;
  if (exitCode !== 0) throw new Error(`test-only image generation failed with exit code ${exitCode}`);
}

async function compile(input: Options): Promise<void> {
  await mkdir(dirname(input.outfile), { recursive: true });
  const result = await Bun.build({
    entrypoints: [resolve(bunRoot, "src/main.ts")],
    target: "bun",
    env: "disable",
    define: {
      OPENPROSE_BUILD_COMMIT: JSON.stringify(input.buildCommit),
      OPENPROSE_BUILD_VERSION: JSON.stringify(input.buildVersion),
      OPENPROSE_BUILD_PROFILE: JSON.stringify(input.buildProfile),
      OPENPROSE_TEST_SEAMS: String(input.testSeams),
      // Always defined, so a public build folds the dev-endpoint branch away.
      PROSE_DEV_BUILD: String(input.devBuild),
      OPENPROSE_WINDOWS_HOST_SHA256: JSON.stringify(input.windowsHostSha256),
      OPENPROSE_WINDOWS_HOST_ADMISSION: String(input.windowsHostAdmission),
      OPENPROSE_CODEX_INSTRUCTION_PLACEMENT: JSON.stringify(input.codexInstructionPlacement),
      OPENPROSE_KERNEL_STARTUP: String(input.publishedKernelStartup),
    },
    compile: {
      // bun-types 1.3.5 omits the documented glibc x64 baseline spelling
      // even though the compiler accepts it. Keep the narrow local union in
      // compile-target.ts and cross this upstream typing defect only here.
      target: nativeCompileTarget() as Bun.Build.Target,
      outfile: input.outfile,
      autoloadDotenv: false,
      autoloadBunfig: false,
    },
    plugins: [{
      name: "verified-openprose-image-bundle",
      setup(builder) {
        builder.onResolve({ filter: /current\.bundle\.bin$/u }, () => ({ path: input.bundle }));
      },
    }],
  });
  if (!result.success) {
    for (const log of result.logs) console.error(log);
    throw new Error("Bun standalone build failed");
  }
  await sealMacOsExecutable(input.outfile);
}

async function runCodeSign(args: string[], failure: string): Promise<void> {
  const child = Bun.spawn(["/usr/bin/codesign", ...args], {
    env: {
      PATH: "/usr/bin:/bin",
    },
    stdin: "ignore",
    stdout: "ignore",
    stderr: "pipe",
  });
  const [exitCode, stderr] = await Promise.all([
    child.exited,
    new Response(child.stderr).text(),
  ]);
  if (exitCode !== 0) {
    const suffix = stderr.trim().length === 0 ? "" : `: ${stderr.trim().slice(0, 512)}`;
    throw new Error(`${failure}${suffix}`);
  }
}

async function sealMacOsExecutable(outfile: string): Promise<void> {
  if (process.platform !== "darwin") return;

  // Bun's compiler emits an ad-hoc Mach-O signature, but versions that are
  // otherwise usable have shipped stale page hashes. Replace it with one
  // deterministic explicit identifier and verify the final bytes before they
  // can become a package input. A real release signer may replace this seal
  // later; this step prevents distributing a locally runnable but invalid
  // Mach-O executable.
  await runCodeSign([
    "--force",
    "--sign",
    "-",
    "--identifier",
    "org.openprose.prose",
    "--timestamp=none",
    outfile,
  ], "macOS ad-hoc code signing failed");
  await runCodeSign([
    "--verify",
    "--deep",
    "--strict",
    outfile,
  ], "macOS code-signature verification failed");
}

const input = options(Bun.argv.slice(2));
let testBundleRoot: string | undefined;
try {
  if (input.command === "build-test") {
    testBundleRoot = await mkdtemp(join(tmpdir(), "openprose-bun-test-image-"));
    input.bundle = join(testBundleRoot, "sentinel.bundle.bin");
    input.checksum = join(testBundleRoot, "sentinel.bundle.sha256");
    await generateTestBundle(input);
  }
  await verify(input);
  if (input.command !== "check") await compile(input);
} catch (error) {
  console.error(`image-bundle: ${error instanceof Error ? error.message : String(error)}`);
  // Bun throws AggregateError before returning BuildOutput for compilation
  // failures. Retain bounded compiler messages so CI can diagnose native builds.
  if (error instanceof AggregateError) {
    for (const item of error.errors.slice(0, 8)) {
      console.error(`image-bundle detail: ${String(item).slice(0, 2048)}`);
    }
  }
  process.exitCode = 2;
} finally {
  if (testBundleRoot !== undefined) {
    try {
      await rm(testBundleRoot, { recursive: true, force: true });
    } catch {
      console.error("image-bundle: test-only image cleanup failed");
      process.exitCode = 2;
    }
  }
}
