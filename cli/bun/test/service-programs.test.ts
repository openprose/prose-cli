// Service programs: checks the shared corpus cannot express compactly.
// Behavior is pinned by cli/conformance/cases/service/programs/; the Rust twin
// is cli/rust/crates/prose-cli/tests/service_programs.rs.
import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { isText, MAX_PROGRAM_BYTES } from "../src/core/service/programs";
import { parseOwnAllowed, parseOwnSlug, parseProgramRef, refOf } from "../src/core/service/program-ref";
import { canonicalJson } from "../src/core/service/render";
import { copyPath, projectWorkspace, validNamespace } from "../src/core/service/workspace";

const KEY = "rr_test_0123456789abcdef0123456789abcdef";
const SAVE = ["--output", "json", "cli", "program", "save", "demo", "big.prose.md"];

async function cli(args: string[], setup: (root: string) => Record<string, string> = () => ({})) {
  let stdout = "";
  let stderr = "";
  const root = mkdtempSync(join(tmpdir(), "prose-programs-"));
  const env = setup(root);
  const code = await runCli(args, {
    env: { HOME: root, XDG_STATE_HOME: join(root, "state"), ...env }, processCwd: root, homeDir: root,
    userConfigPath: join(root, "config", "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { code, stdout, stderr };
}

function fixture(root: string, exchanges: unknown[]): Record<string, string> {
  const path = join(root, "fixture.json");
  writeFileSync(path, JSON.stringify({ environment: "production", credentials: { production: KEY }, storeAvailable: true, exchanges }));
  return { PROSE_TEST_SERVICE_FIXTURE: path };
}

const RECORD = {
  owner: "alice", slug: "demo", visibility: "private", rev: 1,
  rev_id: "0123456789abcdef", commit_id: "1111111111111111", parent_commit_id: null, updated_at: 1,
};

describe("Service programs", () => {
  test("program text admits TAB, LF and CR but no other control character", () => {
    expect(isText("a\tb\r\nc")).toBe(true);
    expect(isText("")).toBe(true);
    expect(isText("a\u001b[31m")).toBe(false);
    expect(isText("a\u007f")).toBe(false);
    expect(MAX_PROGRAM_BYTES).toBe(262144);
  });

  test("save refuses programs over 256 KiB before any request", async () => {
    // Generated at runtime: a 256 KiB corpus file would bloat the public repository.
    const result = await cli(SAVE, (root) => {
      writeFileSync(join(root, "big.prose.md"), "x".repeat(256 * 1024 + 1));
      return fixture(root, []);
    });
    expect(result.code).toBe(2);
    const report = JSON.parse(result.stdout);
    expect(report.problem.code).toBe("INVOCATION_INVALID");
    expect(report.problem.details.reason).toBe('FILE "big.prose.md" is larger than 262144 bytes');
  });

  test("save sends a program of exactly 256 KiB unchanged", async () => {
    const content = "y".repeat(256 * 1024);
    const digest = createHash("sha256").update(canonicalJson({ content })).digest("hex");
    const result = await cli(SAVE, (root) => {
      writeFileSync(join(root, "big.prose.md"), content);
      return fixture(root, [{ method: "PUT", path: "/programs/demo", expectedSha256: digest, status: 200,
        body: { program: { ...RECORD, content }, url: "/p/alice/demo" } }]);
    });
    expect(result.code).toBe(0);
    const report = JSON.parse(result.stdout);
    expect(report.result.program.rev).toBe(1);
    expect(report.result.program.content).toBeUndefined();
  });

  test("a listing over the result schema bound is SERVICE_RESPONSE_TOO_LARGE", async () => {
    const result = await cli(["--output", "json", "cli", "program", "list"],
      (root) => fixture(root, [{ method: "GET", path: "/programs", status: 200, body: { programs: Array.from({ length: 1001 }, () => RECORD) } }]));
    expect(result.code).toBe(10);
    expect(JSON.parse(result.stdout).problem.code).toBe("SERVICE_RESPONSE_TOO_LARGE");
  });

  test("draft without --yes needs no credential or network", async () => {
    const result = await cli(["--output", "json", "cli", "program", "draft", "write a haiku"]);
    expect(result.code).toBe(2);
    const report = JSON.parse(result.stdout);
    expect(report.problem.code).toBe("CONFIRMATION_REQUIRED");
    expect(report.problem.details.plannedRequest).not.toHaveProperty("flags");
  });
});

// The Rust twin is program_ref.rs `revision_numbers_parse_only_where_allowed`
// and `own_slugs_take_owner_slug_and_explain_bad_values_in_words`.
describe("program references", () => {
  const reason = (run: () => unknown): string => {
    try { run(); } catch (caught) { return String((caught as { details?: { reason?: string } }).details?.reason); }
    throw new Error("expected a failure");
  };
  test("revision numbers parse only where allowed", () => {
    expect(parseOwnAllowed("hello@2", true)).toEqual({ owner: "", slug: "hello", revNumber: 2 });
    expect(parseProgramRef("Alice/hello@1", false, true)).toEqual({ owner: "Alice", slug: "hello", revNumber: 1 });
    for (const [value, allowed] of [["hello@1", false], ["a/hello@1", false], ["hello@01", true]] as const) {
      expect(() => parseOwnAllowed(value, allowed)).toThrow();
    }
    expect(reason(() => parseProgramRef("a/hello@3", true))).toContain("names revision 3 by number");
    expect(refOf("a", "hello", "0123456789abcdef")).toBe("a/hello@0123456789abcdef");
  });
  test("own slugs take OWNER/SLUG and explain bad values in words", () => {
    expect(parseOwnSlug("Alice/hello")).toEqual({ owner: "Alice", slug: "hello" });
    expect(parseOwnSlug("hello").owner).toBe("");
    expect(reason(() => parseOwnSlug("Hello")).endsWith('; pass "hello"')).toBe(true);
    expect(reason(() => parseOwnSlug("Hello"))).not.toContain("^");
    expect(reason(() => parseOwnSlug("a/hello@0123456789abcdef"))).toContain('pass "hello" without @');
    expect(() => parseOwnSlug("a/b/c")).toThrow();
  });
});

describe("program workspaces", () => {
  test("owner_kind keeps known values only and copied_from accepts an object or a reference", () => {
    const out: Record<string, unknown> = {};
    projectWorkspace({ owner_kind: "org", copied_from: "alice/hello@fedcba9876543210" }, out as never);
    expect(out).toEqual({ owner_kind: "organization", copied_from: { owner: "alice", slug: "hello", rev_id: "fedcba9876543210", ref: "alice/hello@fedcba9876543210" } });
    const camel: Record<string, unknown> = {};
    projectWorkspace({ ownerKind: "user", copiedFrom: { owner: "alice", slug: "hello", rev_id: null } }, camel as never);
    expect(camel).toEqual({ owner_kind: "personal", copied_from: { owner: "alice", slug: "hello" } });
    const unknown: Record<string, unknown> = {};
    projectWorkspace({ owner_kind: "team" }, unknown as never);
    expect(unknown).toEqual({});
    // Informational: a malformed source is dropped, never fatal to the record.
    for (const bad of ["hello", "alice/Hello", "alice/hello@1", 7, { owner: "alice" }]) {
      const out: Record<string, unknown> = {};
      projectWorkspace({ copied_from: bad } as never, out as never);
      expect(out).toEqual({});
    }
  });

  test("namespaces are organization slugs a program owner can be", () => {
    expect(validNamespace("acme")).toBe(true);
    expect(validNamespace("a-1")).toBe(true);
    for (const bad of ["Acme", "-acme", "acme-", "", "a".repeat(40)]) expect(validNamespace(bad)).toBe(false);
    expect(copyPath("alice", "hello", undefined)).toBe("/p/alice/hello/copy");
    expect(copyPath("alice", "hello", "0123456789abcdef")).toBe("/p/alice/hello@0123456789abcdef/copy");
  });

  test("a personal command line sends no workspace query", async () => {
    const result = await cli(["--output", "json", "cli", "program", "list"], (root) => fixture(root, [
      { method: "GET", path: "/programs", status: 200, body: { programs: [RECORD] } },
    ]));
    expect(result.code).toBe(0);
    expect(JSON.parse(result.stdout).result.programs[0].owner_kind).toBeUndefined();
  });
});
