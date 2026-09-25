// The OpenProse developer endpoint build (PROSE_DEV_BUILD). Public builds talk
// only to production and compile the override out; a developer build reads
// OPENPROSE_API_URL and keeps its login under an origin-scoped store entry.
import { afterEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { DEV_ENDPOINT_INVALID_ACTION, customEnvironment, customOrigin } from "../src/core/service/dev-endpoint";
import { PRODUCTION, serviceEnvironment } from "../src/core/service/endpoint";
import { RunnerFailure } from "../src/core/types";

const bunRoot = resolve(import.meta.dir, "..");
const buildScript = join(bunRoot, "scripts", "image-bundle.ts");
const VARIABLE = "OPENPROSE_API_URL";
const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

function digest16(origin: string): string {
  return createHash("sha256").update(origin).digest("hex").slice(0, 16);
}

describe("developer endpoint resolution", () => {
  test("source-level (public) resolution ignores the variable", () => {
    expect(serviceEnvironment({ [VARIABLE]: "https://example.invalid" })).toBe(PRODUCTION);
  });

  test("an https origin becomes a custom service with an origin-scoped store entry", () => {
    const custom = customEnvironment({ [VARIABLE]: "https://Example.Invalid:8443/" })!;
    const digest = digest16("https://example.invalid:8443");
    expect(custom).toEqual({
      name: "custom",
      origin: "https://example.invalid:8443",
      credentialEnv: "OPENPROSE_API_KEY",
      storeService: `org.openprose.cli.custom-${digest}`,
      label: "OpenProse (custom endpoint https://example.invalid:8443)",
      journal: `custom-${digest}`,
    });
    expect(custom.storeService).not.toBe(PRODUCTION.storeService);
    // The store service name is a closed charset (it reaches OS keychain tools).
    expect(custom.storeService).toMatch(/^org\.openprose\.cli\.custom-[0-9a-f]{16}$/u);
  });

  test("unset or empty means production", () => {
    expect(customEnvironment({})).toBeUndefined();
    expect(customEnvironment({ [VARIABLE]: "" })).toBeUndefined();
  });

  test("anything but a bare https origin is CONFIG_INVALID", () => {
    for (const bad of [
      "http://example.invalid",
      "example.invalid",
      "https://user:pass@example.invalid",
      "https://example.invalid/path",
      "https://example.invalid/?q=1",
      "https://example.invalid/#x",
      "https://example.invalid?",
      "ftp://example.invalid",
    ]) {
      let caught: unknown;
      try { customOrigin(bad); } catch (error) { caught = error; }
      expect(caught, bad).toBeInstanceOf(RunnerFailure);
      expect((caught as RunnerFailure).code).toBe("CONFIG_INVALID");
      expect(String((caught as RunnerFailure).details?.reason)).toStartWith(VARIABLE);
      expect((caught as RunnerFailure).action).toBe(DEV_ENDPOINT_INVALID_ACTION);
    }
  });
});

describe("developer endpoint build switch", () => {
  test("the build refuses a dev endpoint for release or over the ordinary binary", async () => {
    const release = await run([process.execPath, "--no-env-file", buildScript, "build", "--dev-endpoint", "--require-release-eligible", "--outfile", join(tmpdir(), "never-built-prose-dev")], bunRoot);
    expect(release.exitCode).toBe(2);
    expect(release.stderr).toContain("release-eligible builds cannot enable the developer endpoint");
    const ordinary = await run([process.execPath, "--no-env-file", buildScript, "build", "--dev-endpoint"], bunRoot);
    expect(ordinary.exitCode).toBe(2);
    expect(ordinary.stderr).toContain("must not overwrite the ordinary dist/prose build");
  });

  test("a public build compiles the override out; a dev build honours it", async () => {
    const root = await mkdtemp(join(tmpdir(), "openprose-dev-endpoint-"));
    roots.push(root);
    const publicBinary = join(root, "prose");
    const devBinary = join(root, "prose-dev");
    for (const [outfile, extra] of [[publicBinary, []], [devBinary, ["--dev-endpoint"]]] as const) {
      const built = await run([process.execPath, "--no-env-file", buildScript, "build", ...extra, "--outfile", outfile], bunRoot);
      expect(built.exitCode, built.stderr).toBe(0);
    }
    const publicBytes = await readFile(publicBinary);
    const devBytes = await readFile(devBinary);
    expect(publicBytes.includes(Buffer.from(VARIABLE))).toBe(false);
    expect(publicBytes.includes(Buffer.from("org.openprose.cli.custom-"))).toBe(false);
    expect(publicBytes.includes(Buffer.from("custom endpoint"))).toBe(false);
    expect(devBytes.includes(Buffer.from(VARIABLE))).toBe(true);

    const env = { [VARIABLE]: "https://example.invalid", XDG_STATE_HOME: join(root, "state"), XDG_CONFIG_HOME: join(root, "config"), HTTPS_PROXY: "http://127.0.0.1:9" };
    // `cli service guide` makes no request, so neither binary touches the network.
    const publicGuide = await run([publicBinary, "--output", "json", "cli", "service", "guide"], root, env);
    expect(publicGuide.exitCode, publicGuide.stderr).toBe(0);
    // JSON names no service environment; a dev build's human output names its endpoint.
    expect(JSON.parse(publicGuide.stdout)).not.toHaveProperty("environment");
    const devGuide = await run([devBinary, "--output", "json", "cli", "service", "guide"], root, env);
    expect(devGuide.exitCode, devGuide.stderr).toBe(0);
    expect(JSON.parse(devGuide.stdout)).not.toHaveProperty("environment");
    // Human errors carry the custom label; a malformed key fails before any store read or request.
    const devHuman = await run([devBinary, "cli", "run", "list"], root, { ...env, OPENPROSE_API_KEY: "not-a-key" });
    expect(devHuman.stderr).toStartWith("OpenProse (custom endpoint https://example.invalid): SERVICE_AUTH_REQUIRED");
    const publicHuman = await run([publicBinary, "cli", "run", "list"], root, { ...env, OPENPROSE_API_KEY: "not-a-key" });
    expect(publicHuman.stderr).toStartWith("OpenProse: SERVICE_AUTH_REQUIRED");
    // Copyable commands name the executable as invoked: a dev build's own
    // path or PATH name, never the public `prose`; a public build keeps `prose`.
    expect(devHuman.stderr).toContain(`\`${devBinary} cli auth login\``);
    expect(devHuman.stderr).not.toContain("`prose cli");
    expect(publicHuman.stderr).toContain("`prose cli auth login`");
    const bin = join(root, "bin");
    await mkdir(bin);
    await symlink(devBinary, join(bin, "prose-dev"));
    const onPath = await run(["/bin/sh", "-c", "prose-dev cli run list"], root, { ...env, PATH: `${bin}:/usr/bin:/bin`, OPENPROSE_API_KEY: "not-a-key" });
    expect(onPath.stderr).toContain("`prose-dev cli auth login`");
    // Help names it in its usage, examples and pointers.
    const help = await run(["/bin/sh", "-c", "prose-dev cli run --help"], root, { ...env, PATH: `${bin}:/usr/bin:/bin` });
    expect(help.stdout.startsWith("Usage: prose-dev [GLOBAL OPTIONS] cli run")).toBe(true);
    expect(help.stdout).toContain("\n  prose-dev cli run submit hello.prose.md --preview\n");
    expect(help.stdout).not.toContain("prose cli");
    // A dev build given a bad endpoint refuses before any request.
    const bad = await run([devBinary, "--output", "json", "cli", "run", "list"], root, { ...env, [VARIABLE]: "http://example.invalid" });
    expect(bad.exitCode).toBe(2);
    expect(JSON.parse(bad.stdout).problem.code).toBe("CONFIG_INVALID");
  }, 120_000);
});

async function run(argv: string[], cwd: string, extra: Record<string, string> = {}): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const child = Bun.spawn(argv, {
    cwd,
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ...extra },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const [exitCode, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
  return { exitCode, stdout, stderr };
}

test("custom origins match the shared vectors", async () => {
  const { customOrigin } = await import("../src/core/service/dev-endpoint");
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const fixture = JSON.parse(readFileSync(join(import.meta.dir, "../../shared/fixtures/dev-endpoint-origins.json"), "utf8"));
  for (const item of fixture.cases) {
    let outcome: Record<string, unknown>;
    try { outcome = { origin: customOrigin(item.value) }; }
    catch (error) { outcome = { reason: (error as { details?: { reason?: string } }).details?.reason }; }
    expect({ id: item.id, ...outcome }).toEqual({ id: item.id, ...(item.origin === undefined ? { reason: item.reason } : { origin: item.origin }) });
  }
});
