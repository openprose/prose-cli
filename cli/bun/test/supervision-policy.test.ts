import { afterEach, describe, expect, test } from "bun:test";
import { lstat, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { sentinelFixtureImage as sentinelImage } from "./sentinel-fixture";
import { canonicalJson, verifyRuntimeImage } from "../src/core/image";
import type { RunnerInvocation } from "../src/core/types";
import { buildChildEnvironment, collectSecretValues, redactDiagnostic } from "../src/supervision/environment";
import { createPrivateTransportFiles } from "../src/supervision/files";
import { resolveExecutable } from "../src/supervision/process";

const roots: string[] = [];
afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("supervision security policy", () => {
  test("allows only operating-system necessities and out-of-band runner metadata", () => {
    const env = buildChildEnvironment({
      PATH: "/bin",
      HOME: "/home/fixture",
      LANG: "en_US.UTF-8",
      HTTPS_PROXY: "http://127.0.0.1:9",
      OPENAI_API_KEY: "must-not-pass",
      ANTHROPIC_API_KEY: "must-not-pass-either",
      PROSE_TOKEN: "openprose-billing-secret",
      RANDOM_AMBIENT: "nope",
    }, { invocationId: "id", recursionToken: "recursion", runNonce: "nonce" });
    expect(env).toEqual({
      PATH: "/bin",
      HOME: "/home/fixture",
      LANG: "en_US.UTF-8",
      OPENPROSE_INVOCATION_ID: "id",
      OPENPROSE_RECURSION_TOKEN: "recursion",
      OPENPROSE_RUN_NONCE: "nonce",
    });
  });

  test("redacts secret values and assignment-shaped diagnostics", () => {
    const ambient = {
      OPENAI_API_KEY: "sk-fixture-secret",
      SHORT_TOKEN: "xy",
      NORMAL: "visible",
      PRIME_AGENT_CODING_AGENT_DIR: "/private/prime-config",
      PI_CODING_AGENT_DIR: "/private/omp-config",
      UNRELATED_DIR: "/visible/unrelated-dir",
    };
    const output = redactDiagnostic(
      "failed sk-fixture-secret and xy /private/prime-config /private/omp-config "
        + "PRIME_AGENT_CODING_AGENT_DIR=/other/prime PI_CODING_AGENT_DIR=/other/omp "
        + "AUTH_TOKEN=second-secret NORMAL=visible UNRELATED_DIR=/visible/unrelated-dir",
      collectSecretValues(ambient),
    );
    expect(output).toBe(
      "failed [REDACTED] and [REDACTED] [REDACTED] [REDACTED] "
        + "PRIME_AGENT_CODING_AGENT_DIR=[REDACTED] PI_CODING_AGENT_DIR=[REDACTED] "
        + "AUTH_TOKEN=[REDACTED] NORMAL=visible UNRELATED_DIR=/visible/unrelated-dir",
    );
  });

  test("creates 0700 transport roots and 0600 exact image/task files", async () => {
    const base = await mkdtemp(join(tmpdir(), "openprose-bun-policy-"));
    roots.push(base);
    const image = await verifyRuntimeImage(sentinelImage);
    const invocation: RunnerInvocation = {
      schema: "openprose.runner-invocation/1",
      invocationId: "fixture-id",
      cwd: base,
      languageImage: { formatVersion: image.manifest.imageFormatVersion, version: image.manifest.imageVersion, sha256: image.aggregateSha256 },
      runner: { name: "bun", version: "test", commit: "test" },
      harness: "mock",
      transport: "fake-process",
      recursionToken: "openprose:fixture-id",
      task: { schema: image.manifest.taskEnvelope.schemaId, argv: ["prose", "run", "a;b", "$(never)"], interactionMode: "non-interactive" },
      taskDigestSha256: "0".repeat(64),
    };
    const files = await createPrivateTransportFiles(image, invocation, base);
    try {
      expect((await lstat(files.directory)).mode & 0o777).toBe(0o700);
      expect((await lstat(files.imagePath)).mode & 0o777).toBe(0o600);
      expect((await lstat(files.taskPath)).mode & 0o777).toBe(0o600);
      const expectedImage = Buffer.concat(image.manifest.payload.map((entry) => Buffer.from(image.files.get(entry.path)!)));
      expect(await readFile(files.imagePath)).toEqual(expectedImage);
      expect(await readFile(files.taskPath, "utf8")).toBe(canonicalJson(invocation.task));
    } finally {
      await files.cleanup();
    }
  });

  test("rejects a symlinked harness path that resolves back to the wrapper", async () => {
    const base = await mkdtemp(join(tmpdir(), "openprose-bun-recursion-"));
    roots.push(base);
    const wrapper = join(base, "prose");
    const alias = join(base, "harness");
    await writeFile(wrapper, "fixture", { mode: 0o700 });
    await symlink(wrapper, alias);
    await expect(resolveExecutable(alias, wrapper)).rejects.toMatchObject({ code: "RECURSIVE_INVOCATION" });
  });

  test("truthfully rejects Windows process mode when compiled admission is off", async () => {
    const executable = resolve(import.meta.dir, "../../conformance/fake-harness/fake_harness.py");
    const { superviseStructuredProcess } = await import("../src/supervision/process");
    await expect(superviseStructuredProcess({
      executable,
      argv: ["--version"],
      cwd: process.cwd(),
      environment: {
        OPENPROSE_WINDOWS_HOST_ADMISSION: "1",
        OPENPROSE_WINDOWS_HOST_SHA256: "a".repeat(64),
      },
      invocationId: "id",
      recursionToken: "token",
      runNonce: "nonce",
      startupTimeoutMs: 10,
      runTimeoutMs: 10,
      graceMs: 10,
      hardKillAfterMs: 10,
      platform: "win32",
    })).rejects.toMatchObject({ code: "TRANSPORT_UNSUPPORTED", details: { compiledAdmission: false } });
  });
});
