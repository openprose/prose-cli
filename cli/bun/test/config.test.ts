import { afterEach, describe, expect, test } from "bun:test";
import { chmod, lstat, mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { resolveConfiguration, writeUserHarnessSelection } from "../src/core/config";
import configGrammarCorpus from "../../shared/fixtures/config/flat-toml-v1.json" with { type: "json" };

const roots: string[] = [];
afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function root(): Promise<string> {
  // Canonical, as reported sources are (a symlinked temporary directory
  // resolves, as in the Rust port).
  const value = await realpath(await mkdtemp(join(tmpdir(), "openprose-bun-config-")));
  roots.push(value);
  return value;
}

describe("configuration", () => {
  test("implements the shared portable flat TOML corpus without exposing rejected values", async () => {
    for (const accepted of configGrammarCorpus.accepted) {
      const workspace = await root();
      const configPath = join(workspace, "accepted.toml");
      await writeFile(configPath, accepted.source);
      const config = await resolveConfiguration({}, {
        processCwd: workspace,
        env: {},
        userConfigPath: configPath,
      });
      for (const [key, value] of Object.entries(accepted.values)) {
        expect(config.values[key as keyof typeof config.values], accepted.id).toBe(value);
      }
    }

    for (const rejected of configGrammarCorpus.rejected) {
      const workspace = await root();
      const configPath = join(workspace, "rejected.toml");
      await writeFile(configPath, rejected.source);
      try {
        await resolveConfiguration({}, {
          processCwd: workspace,
          env: {},
          userConfigPath: configPath,
        });
        throw new Error(`accepted rejected shared configuration case: ${rejected.id}`);
      } catch (caught) {
        expect(caught, rejected.id).toMatchObject({
          code: "CONFIG_INVALID",
          details: {
            source: `${configPath}:${rejected.line}`,
            reason: rejected.reason,
          },
        });
        if ("forbidden" in rejected) {
          expect(JSON.stringify(caught), rejected.id).not.toContain(rejected.forbidden);
          await expect(writeUserHarnessSelection(
            configPath,
            "claude",
            { model: null, authProfile: null },
          )).rejects.toMatchObject({
            code: "CONFIG_INVALID",
            details: {
              source: `${configPath}:${rejected.line}`,
              reason: rejected.reason,
            },
          });
          expect(await readFile(configPath, "utf8"), rejected.id).toBe(rejected.source);
        }
      }
    }

    for (const rejected of configGrammarCorpus.binaryRejected) {
      const workspace = await root();
      const configPath = join(workspace, "rejected-binary.toml");
      const source = Uint8Array.from(rejected.sourceHex.match(/../gu)!, (byte) => Number.parseInt(byte, 16));
      await writeFile(configPath, source);
      await expect(resolveConfiguration({}, {
        processCwd: workspace,
        env: {},
        userConfigPath: configPath,
      })).rejects.toMatchObject({
        code: "CONFIG_INVALID",
        details: {
          source: `${configPath}:${rejected.line}`,
          reason: rejected.reason,
        },
      });
      await expect(writeUserHarnessSelection(
        configPath,
        "claude",
        { model: null, authProfile: null },
      )).rejects.toMatchObject({
        code: "CONFIG_INVALID",
        details: {
          source: `${configPath}:${rejected.line}`,
          reason: rejected.reason,
        },
      });
      expect(new Uint8Array(await readFile(configPath))).toEqual(source);
    }
  });

  test("uses flag > environment > project > user > default with provenance", async () => {
    const workspace = await root();
    const nested = join(workspace, "packages", "demo");
    const userConfig = join(workspace, "user-cli.toml");
    await mkdir(join(workspace, ".git"));
    await mkdir(join(workspace, ".prose"));
    await mkdir(nested, { recursive: true });
    await writeFile(join(workspace, ".prose", "cli.toml"), 'harness = "codex"\nmodel = "project-model"\n');
    await writeFile(userConfig, 'harness = "claude"\ntransport = "user-transport"\ntimeout = "9m"\n');

    const config = await resolveConfiguration(
      { cwd: nested, harness: "mock", authProfile: "openrouter" },
      {
        processCwd: workspace,
        env: { PROSE_MODEL: "environment-model", PROSE_AUTH_PROFILE: "openai" },
        userConfigPath: userConfig,
      },
    );

    expect(config.values.harness).toBe("mock");
    expect(config.sources.harness.kind).toBe("flag");
    expect(config.values.model).toBe("environment-model");
    expect(config.sources.model.kind).toBe("environment");
    expect(config.values.transport).toBe("user-transport");
    expect(config.sources.transport.kind).toBe("user-config");
    expect(config.values.output).toBe("human");
    expect(config.sources.output.kind).toBe("default");
    expect(config.values.authProfile).toBe("openrouter");
    expect(config.sources.authProfile).toEqual({ kind: "flag", location: "--auth-profile" });
    expect(config.projectConfigPath).toBe(join(await realpath(workspace), ".prose", "cli.toml"));
  });

  test("canonicalizes cwd before project discovery", async () => {
    const workspace = await root();
    const actual = join(workspace, "actual");
    const alias = join(workspace, "alias");
    await mkdir(join(actual, ".git"), { recursive: true });
    await mkdir(join(actual, ".prose"));
    await writeFile(join(actual, ".prose", "cli.toml"), 'harness = "mock"\n');
    await symlink(actual, alias);

    const config = await resolveConfiguration({ cwd: alias }, {
      processCwd: workspace,
      env: {},
      userConfigPath: join(workspace, "absent.toml"),
    });
    expect(config.cwd).toBe(await realpath(actual));
    expect(config.values.harness).toBe("mock");
  });

  test("rejects unknown file keys with source location but ignores unknown PROSE names", async () => {
    const workspace = await root();
    const configPath = join(workspace, "user.toml");
    await writeFile(configPath, 'mystery = "nope"\n');

    await expect(resolveConfiguration({}, {
      processCwd: workspace,
      env: { PROSE_MYSTERY: "ignored" },
      userConfigPath: configPath,
    })).rejects.toMatchObject({
      code: "CONFIG_INVALID",
      details: { source: `${configPath}:1`, reason: "Configuration contains an unknown key." },
    });
  });

  test("rejects unsupported harness values at the configuration source", async () => {
    const workspace = await root();
    const configPath = join(workspace, "user.toml");
    await writeFile(configPath, 'harness = "nope"\n');

    await expect(resolveConfiguration({}, {
      processCwd: workspace,
      env: {},
      userConfigPath: configPath,
    })).rejects.toMatchObject({
      code: "CONFIG_INVALID",
      details: {
        source: `${configPath}:1`,
        reason: "Configuration key harness contains an unsupported value.",
      },
    });

    await expect(resolveConfiguration({}, {
      processCwd: workspace,
      env: { PROSE_HARNESS: "nope" },
      userConfigPath: join(workspace, "absent.toml"),
    })).rejects.toMatchObject({
      code: "CONFIG_INVALID",
      details: { source: "PROSE_HARNESS" },
    });

    await expect(resolveConfiguration({ harness: "nope" }, {
      processCwd: workspace,
      env: {},
      userConfigPath: join(workspace, "absent.toml"),
    })).rejects.toMatchObject({
      code: "CONFIG_INVALID",
      details: { source: "--harness" },
    });
  });

  test("refuses absent empty relative or workspace-local user configuration roots", async () => {
    const workspace = await root();
    const absoluteHome = join(workspace, "home");
    const invalidDependencies = [
      { processCwd: workspace, env: {}, platform: "linux" as const },
      { processCwd: workspace, env: { HOME: "" }, platform: "linux" as const },
      { processCwd: workspace, env: { HOME: "relative" }, platform: "linux" as const },
      { processCwd: workspace, env: { HOME: absoluteHome, XDG_CONFIG_HOME: "" }, platform: "linux" as const },
      { processCwd: workspace, env: { HOME: absoluteHome, XDG_CONFIG_HOME: "relative" }, platform: "linux" as const },
      { processCwd: workspace, env: {}, platform: "darwin" as const },
      { processCwd: workspace, env: {}, platform: "win32" as const },
      { processCwd: workspace, env: { APPDATA: "relative" }, platform: "win32" as const },
      { processCwd: workspace, env: {}, userConfigPath: "relative/cli.toml", platform: "linux" as const },
    ];
    for (const dependencies of invalidDependencies) {
      await expect(resolveConfiguration({}, dependencies)).rejects.toMatchObject({
        code: "CONFIG_INVALID",
      });
    }
  });

  test("atomically writes and updates the user harness while preserving other keys", async () => {
    const workspace = await root();
    const path = join(workspace, "config", "openprose", "cli.toml");
    expect(await writeUserHarnessSelection(path, "claude", { model: null, authProfile: null })).toBeTrue();
    expect(await readFile(path, "utf8")).toBe('harness = "claude"\n');
    expect((await lstat(path)).mode & 0o777).toBe(0o600);
    expect((await lstat(join(workspace, "config", "openprose"))).mode & 0o777).toBe(0o700);
    await writeFile(path, 'harness = "claude"\ntimeout = "9m"\n# keep me\n', { mode: 0o600 });
    expect(await writeUserHarnessSelection(path, "codex", { model: null, authProfile: null })).toBeTrue();
    expect(await readFile(path, "utf8")).toBe('harness = "codex"\ntimeout = "9m"\n# keep me\n');
    await chmod(join(workspace, "config", "openprose"), 0o777);
    expect(await writeUserHarnessSelection(path, "codex", { model: null, authProfile: null })).toBeFalse();
    expect((await lstat(join(workspace, "config", "openprose"))).mode & 0o777).toBe(0o700);
  });

  test("atomically writes and clears a complete harness selection bundle", async () => {
    const workspace = await root();
    const path = join(workspace, "config", "openprose", "cli.toml");
    expect(await writeUserHarnessSelection(path, "prime", {
      model: "openai/gpt-5.4",
      authProfile: "prime-harness-login",
    })).toBeTrue();
    expect(await readFile(path, "utf8")).toBe([
      'auth_profile = "prime-harness-login"',
      'harness = "prime"',
      'model = "openai/gpt-5.4"',
      "",
    ].join("\n"));
    expect(await writeUserHarnessSelection(path, "prime", {
      model: "openai/gpt-5.4",
      authProfile: "prime-harness-login",
    })).toBeFalse();

    await writeFile(path, [
      'model = "stale/model"',
      'timeout = "9m"',
      'harness = "prime"',
      'auth_profile = "openrouter"',
      "# keep me",
      "",
    ].join("\n"), { mode: 0o600 });
    expect(await writeUserHarnessSelection(path, "claude", {
      model: null,
      authProfile: null,
    })).toBeTrue();
    expect(await readFile(path, "utf8")).toBe([
      'harness = "claude"',
      'timeout = "9m"',
      "# keep me",
      "",
    ].join("\n"));
  });

  test("refuses config-file and direct-parent symlinks without changing their targets", async () => {
    const workspace = await root();
    const target = join(workspace, "target.toml");
    const link = join(workspace, "cli.toml");
    await writeFile(target, 'harness = "prime"\n');
    await symlink(target, link);
    await expect(writeUserHarnessSelection(link, "claude", { model: null, authProfile: null }))
      .rejects.toMatchObject({ code: "CONFIG_INVALID" });
    expect(await readFile(target, "utf8")).toBe('harness = "prime"\n');

    const actualParent = join(workspace, "actual-parent");
    const parentLink = join(workspace, "linked-parent");
    await mkdir(actualParent);
    await symlink(actualParent, parentLink);
    await expect(writeUserHarnessSelection(
      join(parentLink, "cli.toml"),
      "claude",
      { model: null, authProfile: null },
    ))
      .rejects.toMatchObject({ code: "CONFIG_INVALID" });
    expect(await readFile(target, "utf8")).toBe('harness = "prime"\n');
    await expect(readFile(join(actualParent, "cli.toml"), "utf8")).rejects.toMatchObject({ code: "ENOENT" });

    const nonDirectoryParent = join(workspace, "not-a-directory");
    await writeFile(nonDirectoryParent, "preserve me\n");
    await expect(writeUserHarnessSelection(
      join(nonDirectoryParent, "cli.toml"),
      "claude",
      { model: null, authProfile: null },
    ))
      .rejects.toMatchObject({ code: "CONFIG_INVALID" });
    expect(await readFile(nonDirectoryParent, "utf8")).toBe("preserve me\n");
  });
});

test("the shared config values corpus resolves identically", async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, realpathSync, rmSync, readFileSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const corpus = JSON.parse(readFileSync(join(import.meta.dir, "../../shared/fixtures/config/values-v1.json"), "utf8"));
  for (const item of corpus.cases) {
    const root = realpathSync(mkdtempSync(join(tmpdir(), "prose-config-values-")));
    try {
      mkdirSync(join(root, ".git"));
      mkdirSync(join(root, ".prose"));
      if (typeof item.files?.project === "string") writeFileSync(join(root, ".prose/cli.toml"), item.files.project);
      if (item.files?.projectDirectory === true) mkdirSync(join(root, ".prose/cli.toml"));
      const fill = (text: string) => text.split("{ROOT}").join(root);
      let outcome: { values?: Record<string, unknown>; error?: unknown };
      try {
        const config = await resolveConfiguration({}, { processCwd: root, env: item.environment ?? {}, userConfigPath: join(root, "xdg/openprose/cli.toml") });
        outcome = { values: config.values as unknown as Record<string, unknown> };
      } catch (error) { outcome = { error }; }
      if (item.expected.values !== undefined) {
        expect({ id: item.id, error: outcome.error }).toEqual({ id: item.id, error: undefined });
        for (const [key, value] of Object.entries(item.expected.values)) expect({ id: item.id, key, value: outcome.values![key] }).toEqual({ id: item.id, key, value });
      } else {
        const details = (outcome.error as { details?: Record<string, unknown> } | undefined)?.details;
        expect({ id: item.id, reason: details?.reason, source: details?.source }).toEqual({ id: item.id, reason: fill(item.expected.error.reason), source: fill(item.expected.error.source) });
      }
    } finally { rmSync(root, { recursive: true, force: true }); }
  }
});

test("the maximum timeout matches the transport limits", async () => {
  const { MAX_TIMEOUT_MS, timeoutMs } = await import("../src/core/config");
  const limits = await import("../../shared/capabilities/transport-limits.v1.json", { with: { type: "json" } });
  expect(limits.default.maxTimeoutMs).toBe(MAX_TIMEOUT_MS);
  expect(timeoutMs("24h")).toBe(MAX_TIMEOUT_MS);
  expect(typeof timeoutMs("1441m")).toBe("string");
});
