import { afterEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const bunRoot = resolve(import.meta.dir, "..");
const buildScript = join(bunRoot, "scripts", "image-bundle.ts");
const fakeHarness = resolve(bunRoot, "../conformance/fake-harness/fake_harness.py");
const adapterProbe = resolve(bunRoot, "../shared/fixtures/adapters/bin/adapter_probe.py");
const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("standalone build identity and seam exclusion", () => {
  test("macOS ad-hoc sealing is explicit and non-timestamped", async () => {
    const source = await readFile(buildScript, "utf8");
    expect(source).toContain([
      '    "--force",',
      '    "--sign",',
      '    "-",',
      '    "--identifier",',
      '    "org.openprose.prose",',
      '    "--timestamp=none",',
    ].join("\n"));
  });

  test("explicit placeholder release build retains its structural release gate", async () => {
    const result = await run([process.execPath, "run", "build:release", "--image-dir", resolve(bunRoot,"../shared/image/echo-v0")], bunRoot);
    expect(result.exitCode, result.stderr).toBe(0);
    const executable = join(bunRoot, "dist", process.platform === "win32" ? "prose.exe" : "prose");
    const doctor = await run([executable, "--output=json", "cli", "doctor"], bunRoot);
    expect(doctor.exitCode).toBe(10);
    expect(JSON.parse(doctor.stdout)).toMatchObject({
      image: { version: "echo-v0", releaseEligible: true },
      build: { profile: "release", testSeamsEnabled: false },
    });
  });

  test("release startup retains published kernel resolution and excludes mock execution", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-published-release-"));
    roots.push(root);
    const executable = join(root, "prose");
    const result = await run([process.execPath, buildScript, "build", "--require-release-eligible", "--outfile", executable], bunRoot);
    expect(result.exitCode, result.stderr).toBe(0);
    const contract = JSON.parse(await readFile(resolve(bunRoot, "../shared/fixtures/build/published-release.json"), "utf8"));
    const doctor = await run([executable, "--output=json", "cli", "doctor"], root);
    expect(JSON.parse(doctor.stdout)).toMatchObject(contract.doctor);
    const mock = await run([executable, "--harness=mock", "--output=json", "run", "hello"], root);
    expect(mock.exitCode).toBe(contract.mockRunExitCode);
    const error = JSON.parse(mock.stdout);
    expect((error.error ?? error).code).toBe(contract.mockRunErrorCode);
  }, 30_000);

  test("ordinary build matches Rust's development profile without test seams", async () => {
    const result = await run([process.execPath, "run", "build"], bunRoot);
    expect(result.exitCode, result.stderr).toBe(0);
    const executable = join(bunRoot, "dist", process.platform === "win32" ? "prose.exe" : "prose");
    const doctor = await run([executable, "--output=json", "cli", "doctor"], bunRoot);
    expect(doctor.exitCode).toBe(10);
    expect(JSON.parse(doctor.stdout)).toMatchObject({
      image: { version: "echo-v0", releaseEligible: true },
      build: { profile: "development", testSeamsEnabled: false },
    });
  });

  test("release image admission and test seams cannot be combined", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-profile-conflict-"));
    roots.push(root);
    const executable = join(root, "prose-conflict");
    const result = await run([
      process.execPath,
      "--no-env-file",
      buildScript,
      "build",
      "--outfile",
      executable,
      "--require-release-eligible",
      "--test-seams",
    ], bunRoot);
    expect(result.exitCode).toBe(2);
    expect(result.stdout).toBe("");
    expect(result.stderr).toContain("release-eligible builds cannot enable test seams");
    expect(await Bun.file(executable).exists()).toBeFalse();
  });

  test("same source, image, flags, and commit produce identical release artifacts", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-reproducible-"));
    roots.push(root);
    const first = join(root, "prose-first");
    const second = join(root, "prose-second");
    const commit = "0123456789abcdef0123456789abcdef01234567";
    await build(first, commit);
    await build(second, commit);
    const [firstBytes, secondBytes] = await Promise.all([readFile(first), readFile(second)]);
    expect(digest(firstBytes)).toBe(digest(secondBytes));
    expect(firstBytes).toEqual(secondBytes);

    if (process.platform === "darwin") {
      for (const executable of [first, second]) {
        const verified = await run([
          "/usr/bin/codesign", "--verify", "--deep", "--strict", executable,
        ], root);
        expect(verified).toEqual({ exitCode: 0, stdout: "", stderr: "" });

        const described = await run([
          "/usr/bin/codesign", "--display", "--verbose=2", executable,
        ], root);
        expect(described.exitCode, described.stderr).toBe(0);
        expect(described.stderr).toContain("Identifier=org.openprose.prose");
        expect(described.stderr).toContain("Signature=adhoc");
      }
    }

    const inventory = await run([first, "--output=json", "cli", "harness", "list"], root);
    expect(inventory.exitCode).toBe(0);
    const mock = JSON.parse(inventory.stdout).harnesses.find((item: { id: string }) => item.id === "mock");
    expect(mock).toMatchObject({
      availability: "unavailable",
      detectedVersion: null,
      strictWrapperConformant: false,
      testOnly: true,
      admissionBlock: "test-seams-disabled",
    });
    const humanInventory = await run([first, "cli", "harness", "list"], root);
    expect(humanInventory).toMatchObject({ exitCode: 0, stderr: "" });
    expect(humanInventory.stdout).toContain(
      "* openprose availability=not-implemented transport=hosted (selected)\n",
    );
    const exactFirst = await realpath(first);
    expect(humanInventory.stdout).toContain(`Choose Codex: '${exactFirst}' cli harness use codex\n`);
    expect(humanInventory.stdout).toContain(`Choose Claude: '${exactFirst}' cli harness use claude\n`);
    expect(humanInventory.stdout).toContain("Prime and OMP: set PROSE_MODEL to a fully qualified provider/model installed in that harness; unset or invalid values are refused.\n");
    expect(humanInventory.stdout).toContain(`Choose Prime: '${exactFirst}' cli harness use prime --model "$PROSE_MODEL" --auth-profile prime-harness-login\n`);
    expect(humanInventory.stdout).toContain(`Choose OMP: '${exactFirst}' cli harness use omp --model "$PROSE_MODEL" --auth-profile omp-harness-login\n`);
    expect(humanInventory.stdout).toContain(`Then verify: '${exactFirst}' cli doctor\n`);
    expect(humanInventory.stdout).not.toContain("replace-with-provider/model");
    expect(humanInventory.stdout).not.toContain('"$PROSE"');
    expect(humanInventory.stdout).not.toContain("mock");
    expect(humanInventory.stdout).not.toContain("Test-only harnesses:");
    expect(humanInventory.stdout).not.toContain("prose cli");

    if (process.platform !== "win32") {
      const hostileBin = join(root, "hostile-bin");
      const hostileMarker = join(root, "path-resolved-prose-ran");
      await mkdir(hostileBin);
      await writeFile(
        join(hostileBin, "prose"),
        `#!/bin/sh\nprintf attacked > ${JSON.stringify(hostileMarker)}\n`,
        { mode: 0o700 },
      );
      await chmod(join(hostileBin, "prose"), 0o700);
      const hostileInventory = await run(
        [first, "cli", "harness", "list"],
        root,
        { PATH: hostileBin },
      );
      expect(hostileInventory).toMatchObject({ exitCode: 0, stderr: "" });
      expect(hostileInventory.stdout).toContain(`'${exactFirst}' cli harness use`);
      expect(hostileInventory.stdout).not.toContain('"$PROSE"');
      expect(hostileInventory.stdout).not.toContain("prose cli");
      expect(await Bun.file(hostileMarker).exists()).toBeFalse();

      for (const harness of ["prime", "omp"] as const) {
        const command = humanInventory.stdout.split("\n")
          .find((line) => line.startsWith(`Choose ${harness === "prime" ? "Prime" : "OMP"}: `))!
          .slice(`Choose ${harness === "prime" ? "Prime" : "OMP"}: `.length);
        const authProfile = `${harness}-harness-login`;
        for (const [label, model] of [
          ["unset", undefined],
          ["unqualified", "unqualified"],
          ["hostile", `openai/model$(touch ${join(root, `${harness}-injection`)})`],
        ] as const) {
          const configRoot = join(root, `${harness}-${label}-config`);
          const attempted = await run(["/bin/sh", "-c", command], root, {
            HOME: join(root, `${harness}-${label}-home`),
            XDG_CONFIG_HOME: configRoot,
            ...(model === undefined ? {} : { PROSE_MODEL: model }),
          });
          expect(attempted.exitCode).toBe(2);
          expect(await Bun.file(join(configRoot, "openprose", "cli.toml")).exists()).toBeFalse();
          expect(await Bun.file(join(root, `${harness}-injection`)).exists()).toBeFalse();
        }

        const validConfigRoot = join(root, `${harness}-valid-config`);
        const selected = await run(["/bin/sh", "-c", command], root, {
          HOME: join(root, `${harness}-valid-home`),
          XDG_CONFIG_HOME: validConfigRoot,
          PROSE_MODEL: "openai/gpt-5.4",
        });
        expect(selected.exitCode, selected.stderr).toBe(0);
        expect(await readFile(join(validConfigRoot, "openprose", "cli.toml"), "utf8")).toBe([
          `auth_profile = ${JSON.stringify(authProfile)}`,
          `harness = ${JSON.stringify(harness)}`,
          'model = "openai/gpt-5.4"',
          "",
        ].join("\n"));
      }
    }

    const mockDoctor = await run([first, "--harness=mock", "--output=json", "cli", "doctor"], root);
    expect(mockDoctor.exitCode).toBe(10);
    expect(JSON.parse(mockDoctor.stdout)).toMatchObject({
      build: { profile: "release", testSeamsEnabled: false },
      ready: false,
      selectedHarness: "mock",
      promptPlacement: null,
      isolation: "unsupported",
      problems: [{ code: "HARNESS_UNAVAILABLE", exitCode: 10 }],
    });

    for (const extra of [[], ["--dry-run"]]) {
      const refused = await run([
        first, "--harness=mock", "--output=json", ...extra, "run", "fixture.prose.md",
      ], root);
      expect(refused.exitCode).toBe(10);
      expect(JSON.parse(refused.stdout)).toMatchObject({
        runner: { name: "bun", version: "0.1.0", commit },
        adapter: {
          id: "mock/unavailable",
          harnessVersion: null,
          descriptorDigestSha256: digest("mock/unavailable"),
        },
        negotiatedCapabilities: {
          promptPlacement: "unsupported",
          isolation: "unsupported",
          streaming: "unsupported",
          cancellation: "unsupported",
          terminal: "unsupported",
        },
        semantic: { status: "unknown" },
        terminal: { transportCompleted: false, terminalEventObserved: false },
        error: { code: "HARNESS_UNAVAILABLE", exitCode: 10 },
        runnerExitCode: 10,
      });
      expect(refused.stdout).not.toContain("IMAGE_INVALID");
    }
    const doctor = await run([first, "--output=json", "cli", "doctor"], root);
    expect(doctor.exitCode).toBe(10);
    expect(JSON.parse(doctor.stdout)).toMatchObject({
      schema: "openprose.doctor-report/1",
      runner: { name: "bun", version: "0.1.0", commit },
      build: { profile: "release", testSeamsEnabled: false },
    });
    const failure = await run([first, "--output=json", "run", "example.prose.md"], root);
    expect(failure.exitCode).toBe(10);
    expect(JSON.parse(failure.stdout)).toMatchObject({
      schema: "openprose.runner-result/1",
      runner: { name: "bun", version: "0.1.0", commit },
    });
  }, 30_000);

  test("ambient controls cannot enable compiled-out release seams", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-release-seams-"));
    roots.push(root);
    const executable = join(root, "prose-release");
    const fakeObservation = join(root, "fake-observation.json");
    const adapterObservation = join(root, "adapter-observation.json");
    await build(executable, "release-seam-test");

    const fake = await run([
      executable,
      "--harness=mock",
      "--transport=fake-process",
      "--output=json",
      "run",
    ], root, {
      OPENPROSE_CONFORMANCE_FAKE_HARNESS: fakeHarness,
      OPENPROSE_CONFORMANCE_FAKE_SCENARIO: "success",
      OPENPROSE_CONFORMANCE_FAKE_OBSERVATION: fakeObservation,
    });
    expect(fake.exitCode).toBe(10);
    expect(JSON.parse(fake.stdout)).toMatchObject({
      error: { code: "HARNESS_UNAVAILABLE" },
    });
    expect(await Bun.file(fakeObservation).exists()).toBeFalse();

    const installed = await run([
      executable,
      "--harness=codex",
      "--transport=exec-json",
      "--output=json",
      "run",
    ], root, {
      PATH: root,
      OPENPROSE_CONFORMANCE_ADAPTER_MODE: "provider-free-v1",
      OPENPROSE_CONFORMANCE_ADAPTER_PROBE: adapterProbe,
      OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION: adapterObservation,
      OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP: "cached-chatgpt-login",
      OPENPROSE_CONFORMANCE_ADAPTER_CLEANUP_FAILURE: "1",
      OPENPROSE_CONFORMANCE_ADAPTER_CREATION_FAILURE: "cleanup-failure",
    });
    expect(installed.exitCode).toBe(10);
    expect(JSON.parse(installed.stdout)).toMatchObject({
      error: { code: "HARNESS_UNAVAILABLE" },
    });
    expect(await Bun.file(adapterObservation).exists()).toBeFalse();

  }, 30_000);

  test("exact build-version override is embedded in every standalone version surface", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-version-"));
    roots.push(root);
    const executable = join(root, "prose-versioned");
    const version = "1.2.3-rc.0+build.7";
    await build(executable, "version-test-commit", version);

    const versionOutput = await run([executable, "--version"], root);
    expect(versionOutput).toEqual({
      exitCode: 0,
      stdout: `prose ${version} (bun)\n`,
      stderr: "",
    });

    const doctor = await run([executable, "--harness=mock", "--output=json", "cli", "doctor"], root);
    expect(doctor.exitCode).toBe(10);
    expect(JSON.parse(doctor.stdout)).toMatchObject({
      runner: { name: "bun", version, commit: "version-test-commit" },
    });

    const failure = await run([executable, "--output=json", "run", "example.prose.md"], root);
    expect(failure.exitCode).toBe(10);
    expect(JSON.parse(failure.stdout)).toMatchObject({
      runner: { name: "bun", version, commit: "version-test-commit" },
    });
  }, 30_000);

  test("build-version override accepts exact SemVer and rejects malformed values before output", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-bun-version-validation-"));
    roots.push(root);
    for (const version of ["0.1.0-alpha.1", "1.2.3-rc.0+build.7"]) {
      const executable = join(root, `accepted-${version.replaceAll("+", "_")}`);
      const result = await run([
        process.execPath,
        "--no-env-file",
        buildScript,
        "build",
        "--outfile",
        executable,
      ], bunRoot, {
        OPENPROSE_BUILD_COMMIT: "version-validation",
        OPENPROSE_BUILD_VERSION: version,
      });
      expect(result.exitCode, result.stderr).toBe(0);
      expect((await stat(executable)).isFile()).toBeTrue();
    }

    for (const version of ["", "v0.1.0", "0.1", "01.0.0", "0.1.0-alpha..1", "0.1.0-01", "0.1.0-雪"]) {
      const executable = join(root, `rejected-${Buffer.from(version).toString("hex") || "empty"}`);
      const result = await run([
        process.execPath,
        "--no-env-file",
        buildScript,
        "build",
        "--outfile",
        executable,
      ], bunRoot, {
        OPENPROSE_BUILD_COMMIT: "version-validation",
        OPENPROSE_BUILD_VERSION: version,
      });
      expect(result.exitCode).toBe(2);
      expect(result.stdout).toBe("");
      expect(result.stderr).toContain("OPENPROSE_BUILD_VERSION must be an exact SemVer 2.0.0 version");
      expect(await Bun.file(executable).exists()).toBeFalse();
    }
  }, 60_000);
});

async function build(outfile: string, commit: string, version?: string): Promise<void> {
  const result = await run([
    process.execPath,
    "--no-env-file",
    buildScript,
    "build",
    "--outfile",
    outfile,
    "--require-release-eligible",
    "--image-dir", resolve(bunRoot,"../shared/image/echo-v0"),
  ], bunRoot, {
    OPENPROSE_BUILD_COMMIT: commit,
    ...(version === undefined ? {} : { OPENPROSE_BUILD_VERSION: version }),
  });
  expect(result.exitCode, result.stderr).toBe(0);
}

async function run(
  argv: string[],
  cwd: string,
  extraEnvironment: Record<string, string> = {},
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const child = Bun.spawn(argv, {
    cwd,
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ...extraEnvironment },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const [exitCode, stdout, stderr] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ]);
  return { exitCode, stdout, stderr };
}

function digest(value: Uint8Array | string): string {
  return createHash("sha256").update(value).digest("hex");
}
