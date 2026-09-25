// Service discovery unit tests (projection helpers). Behavior is pinned by the
// shared corpus cli/conformance/cases/service/discovery/; these mirror the Rust
// unit tests in prose-runner-core/src/service/discovery.rs.
import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { exampleSourcePath, exampleText, projectHealth, projectModels, projectRepositories, Shape } from "../src/core/service/discovery";
import { RunnerFailure } from "../src/core/types";

const shape = new Shape("GET /test");
const origin = "https://run-prose-production.openprose.workers.dev";

function reason(run: () => unknown): string {
  try { run(); } catch (caught) {
    if (caught instanceof RunnerFailure) return String(caught.details?.reason);
    throw caught;
  }
  throw new Error("expected a failure");
}

describe("Service discovery", () => {
  test("example sources follow only same-origin private routes", () => {
    expect(exampleSourcePath("/examples/private/private-example", origin)).toBe("/examples/private/private-example");
    expect(exampleSourcePath(`${origin}/examples/private/a.prose.md`, origin)).toBe("/examples/private/a.prose.md");
    for (const refused of [
      "https://examples.example.org/examples/agent-native.prose.md",
      `${origin}.evil.example/examples/private/x`,
      "//evil.example/examples/private/x",
      "/examples/private/../wallet",
      "/examples/private/",
      "/examples/private/a/b",
      "/examples/private/x?y=1",
      "/examples/public/x",
      "examples/private/x",
    ]) expect(exampleSourcePath(refused, origin)).toBeNull();
  });

  test("service status keeps only whether the service answers and the models", () => {
    const body = { status: "ok", models: ["model-sol"], default_model: "model-sol", version: "9.9.9-extra", extra: { nested: true } };
    expect(projectHealth(body)).toEqual({ status: "ok", models: ["model-sol"], default_model: "model-sol" });
    expect(reason(() => projectHealth({ status: "ok", models: [] }))).toBe("unexpected service status response: default_model");
    expect(reason(() => projectHealth({ status: "o\u001bk", models: [], default_model: "m" }))).toBe("unexpected service status response: status");
  });

  test("model list keeps only the offered models and the default", () => {
    const body = { models: ["model-astra", "model-sol"], default_model: "model-sol", other: { "b-model": { state: "x" } } };
    expect(projectModels(shape, body)).toEqual({ models: ["model-astra", "model-sol"], default_model: "model-sol" });
  });

  test("repositories are flattened, sorted and deduplicated", () => {
    const repos = projectRepositories(shape, { installations: [
      { owner: "b", owner_type: "User", repos: [{ id: 2, name: "z", full_name: "b/z", private: true }] },
      { owner: "a", owner_type: "Organization", repos: [
        { id: 1, name: "y", full_name: "a/y", private: false, default_branch: "main" },
        { id: 2, name: "z", full_name: "b/z", private: true }] },
    ] });
    expect(repos.map((repo) => repo.full_name)).toEqual(["a/y", "b/z"]);
    expect(repos[0]!.default_branch).toBe("main");
    expect("default_branch" in repos[1]!).toBe(false);
  });

  test("example text allows TAB, LF and CR only", () => {
    expect(exampleText(new TextEncoder().encode("a\tb\r\nc"))).toBe("a\tb\r\nc");
    expect(exampleText(new TextEncoder().encode("a\u001bb"))).toBeUndefined();
    expect(exampleText(new Uint8Array([0xff]))).toBeUndefined();
  });

  test("example show writes no file when the list request fails", async () => {
    const home = mkdtempSync(join(tmpdir(), "prose-discovery-"));
    const fixture = join(home, "fixture.json");
    writeFileSync(fixture, JSON.stringify({
      environment: "production", credentials: { production: "rr_test_0123456789abcdef0123456789abcdef" }, storeAvailable: true,
      exchanges: [{ method: "GET", path: "/examples/private", status: 503, bodyText: "" }],
    }));
    let stdout = "";
    const code = await runCli(
      ["--output", "json", "cli", "example", "show", "private-example", "--output-file", "e.md"],
      {
        env: { HOME: home, XDG_STATE_HOME: join(home, "state"), PROSE_TEST_SERVICE_FIXTURE: fixture },
        processCwd: home, homeDir: home, userConfigPath: join(home, "config", "cli.toml"),
        clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
        writeStdout: (value) => { stdout += value; }, writeStderr: () => {},
      },
    );
    expect(code).toBe(10);
    const report = JSON.parse(stdout);
    expect(report.operation).toBe("example.show");
    expect(report.problem.code).toBe("SERVICE_UNAVAILABLE");
    expect(existsSync(join(home, "e.md"))).toBe(false);
    expect(stdout.includes("rr_test_")).toBe(false);
  });
});
