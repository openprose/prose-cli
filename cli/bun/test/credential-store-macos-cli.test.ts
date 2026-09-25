// The Bun CLI uses the macOS login keychain through /usr/bin/security -i,
// exactly like the Rust port (twin: cli/rust/crates/prose-cli/tests/credential_store_macos.rs).
// A fake `security` (the test-seam PROSE_TEST_MACOS_SECURITY) stands in for
// the real tool; nothing touches a keychain or the network.
import { expect, test } from "bun:test";
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";

const TOKEN = "rr_test_0123456789abcdef0123456789abcdef";

function fake(root: string): string {
  const program = join(root, "security");
  writeFileSync(program, `#!/bin/sh
R='${root}'
n=$(/bin/cat "$R/n" 2>/dev/null || echo 0)
echo $((n + 1)) > "$R/n"
/bin/cat > "$R/stdin-$n"
[ -f "$R/reply-$n" ] && /bin/cat "$R/reply-$n"
[ -f "$R/error-$n" ] && /bin/cat "$R/error-$n" >&2
exit $(/bin/cat "$R/exit-$n" 2>/dev/null || echo 0)
`);
  chmodSync(program, 0o755);
  return program;
}

async function prose(root: string, args: string[]) {
  let stdout = "", stderr = "";
  const code = await runCli(args, {
    env: { HOME: join(root, "home"), PATH: "/usr/bin:/bin", HTTPS_PROXY: "http://127.0.0.1:9", PROSE_TEST_MACOS_SECURITY: fake(root) },
    processCwd: root, userConfigPath: join(root, "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test-invocation" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { code, report: JSON.parse(stdout), stdout, stderr };
}

function sessions(root: string): string[] {
  const out: string[] = [];
  for (let n = 0; existsSync(join(root, `stdin-${n}`)); n += 1) out.push(readFileSync(join(root, `stdin-${n}`), "utf8"));
  return out;
}

const macos = process.platform === "darwin";

test.skipIf(!macos)("logout and a signed-out status use security", async () => {
  const root = mkdtempSync(join(tmpdir(), "prose-macos-store-"));
  try {
    let result = await prose(root, ["cli", "auth", "logout", "--json"]);
    expect(result.code).toBe(0);
    expect(result.report.result.authenticated).toBe(false);
    expect(sessions(root)).toEqual(["delete-generic-password -s org.openprose.cli.production -a api-key\n"]);
    writeFileSync(join(root, "error-1"), "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n");
    result = await prose(root, ["cli", "auth", "status", "--json"]);
    expect(result.code).toBe(0);
    expect(result.report.result).toEqual({ authenticated: false, credentialSource: "none" });
    expect(sessions(root)[1]).toBe("find-generic-password -s org.openprose.cli.production -a api-key\n");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test.skipIf(!macos)("an earlier item that needs a prompt names the keychain and the variable", async () => {
  const root = mkdtempSync(join(tmpdir(), "prose-macos-store-"));
  try {
    writeFileSync(join(root, "reply-0"), "\"icmt\"<blob>=<NULL>\n");
    writeFileSync(join(root, "error-1"), "security: SecKeychainItemCopyContent: User interaction is not allowed.\n");
    const result = await prose(root, ["cli", "auth", "status", "--json"]);
    expect(result.code).toBe(10);
    expect(result.report.problem.code).toBe("CREDENTIAL_STORE_UNAVAILABLE");
    expect(result.report.problem.details.reason.startsWith("the macOS keychain needs approval in a prompt")).toBe(true);
    expect(result.report.problem.action).toContain("OPENPROSE_API_KEY");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test.skipIf(!macos)("a malformed stored value is rejected by the caller and not migrated", async () => {
  const root = mkdtempSync(join(tmpdir(), "prose-macos-store-"));
  try {
    writeFileSync(join(root, "reply-0"), "\"icmt\"<blob>=<NULL>\n");
    writeFileSync(join(root, "reply-1"), "not-a-key\n");
    const result = await prose(root, ["cli", "auth", "status", "--json"]);
    expect(result.code).toBe(10);
    expect(result.report.problem.code).toBe("SERVICE_AUTH_REQUIRED");
    expect(result.report.problem.details.credentialProblem).toBe("malformed");
    expect(sessions(root).length).toBe(2);
    expect(result.stdout).not.toContain(TOKEN);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
