// The macOS credential store (/usr/bin/security -i) against the shared
// fixture both ports test: commands, classification, migration, reasons and
// cancellation. A fake `security` stands in for the real tool; nothing here
// touches a keychain.
import { expect, test } from "bun:test";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  SECURITY_ACCOUNT, SECURITY_BOUND_MS, SECURITY_COMMENT, SECURITY_MARKERS, SECURITY_OUTPUT_LIMIT, SECURITY_PROGRAM,
  keychainAccessFailure, nativeStore, operateSecurity, outcomeUnknownFailure, securityOwnedMarker, securityScripts,
  spawnSecurity, storeUnavailable, type SecuritySession,
} from "../src/core/service/credentials";
import { PRODUCTION } from "../src/core/service/endpoint";
import { RunnerFailure } from "../src/core/types";

const fixture = JSON.parse(readFileSync(join(import.meta.dir, "../../shared/fixtures/credentials/macos-security.v1.json"), "utf8"));
const VARIABLE = "OPENPROSE_API_KEY";
const TOKEN = "rr_test_0123456789abcdef0123456789abcdef";

test("the macOS store constants match the shared fixture", () => {
  expect(fixture.program).toBe(SECURITY_PROGRAM);
  expect(fixture.argv).toEqual(["-i"]);
  expect(fixture.account).toBe(SECURITY_ACCOUNT);
  expect(fixture.comment).toBe(SECURITY_COMMENT);
  expect(fixture.ownedMarker).toBe(securityOwnedMarker);
  expect(fixture.boundMs).toBe(SECURITY_BOUND_MS);
  expect(fixture.outputLimitBytes).toBe(SECURITY_OUTPUT_LIMIT);
  const service = "{service}";
  expect(fixture.scripts.probe).toBe(securityScripts.probe(service));
  expect(fixture.scripts.read).toBe(securityScripts.read(service));
  expect(fixture.scripts.remove).toBe(securityScripts.remove(service));
  expect(fixture.scripts.add).toBe(securityScripts.add(service, "{token}"));
  expect(fixture.scripts.migrate).toBe(securityScripts.remove(service) + securityScripts.add(service, "{token}"));
  for (const name of ["absent", "denied", "interaction-not-allowed", "unavailable"] as const) {
    expect(fixture.classification[name]).toEqual([...SECURITY_MARKERS[name]]);
  }
  for (const [name, error] of [["keychain-access", keychainAccessFailure(VARIABLE)], ["outcome-unknown", outcomeUnknownFailure(VARIABLE)]] as const) {
    const fill = (text: string) => text.split("{variable}").join(VARIABLE);
    expect(error.details).toEqual({ reason: fill(fixture.reasons[name].reason) });
    expect(error.action).toBe(fill(fixture.reasons[name].action));
  }
});

function check(id: string, expected: Record<string, unknown>, outcome: { value?: string | null; error?: unknown }): void {
  if ("value" in expected) { expect({ id, value: outcome.value }).toEqual({ id, value: expected.value as string }); return; }
  if ("absent" in expected || "ok" in expected) { expect({ id, value: outcome.value }).toEqual({ id, value: null }); return; }
  const error = outcome.error as RunnerFailure;
  expect({ id, code: error?.code }).toEqual({ id, code: expected.error as never });
  const want = expected.reason === "keychain-access" ? keychainAccessFailure(VARIABLE).details
    : expected.reason === "outcome-unknown" ? outcomeUnknownFailure(VARIABLE).details : undefined;
  expect({ id, details: error.details }).toEqual({ id, details: want });
}

test("the macOS store follows every shared scenario", async () => {
  for (const scenario of fixture.scenarios) {
    const responses = [...scenario.responses];
    const scripts: string[] = [];
    const run = async (script: string): Promise<SecuritySession> => {
      scripts.push(script);
      const response = responses.shift();
      if (response === undefined) throw new Error(`${scenario.id}: extra session`);
      return response;
    };
    const outcome: { value?: string | null; error?: unknown } = {};
    try { outcome.value = await operateSecurity(run, scenario.operation, scenario.token, fixture.service, VARIABLE); }
    catch (error) { outcome.error = error; }
    expect({ id: scenario.id, scripts }).toEqual({ id: scenario.id, scripts: scenario.expectedScripts });
    check(scenario.id, scenario.expected, outcome);
  }
});

test("cancellation is CANCELLED except while a key is being written", async () => {
  for (const [operation, token, code] of [["get", undefined, "CANCELLED"], ["delete", undefined, "CANCELLED"], ["set", TOKEN, "CREDENTIAL_STORE_UNAVAILABLE"]] as const) {
    const error = await operateSecurity(async () => "cancelled", operation, token, fixture.service, VARIABLE).catch((caught) => caught);
    expect(error.code).toBe(code);
    if (operation === "set") expect(error.details).toEqual(outcomeUnknownFailure(VARIABLE).details);
  }
  // An invalid service never reaches the tool.
  await expect(operateSecurity(async () => { throw new Error("spawned"); }, "get", undefined, "evil -w", VARIABLE)).rejects.toBeInstanceOf(RunnerFailure);
});

test("a failure the store explained keeps its reason through the variable teaching", () => {
  const explained = keychainAccessFailure(VARIABLE);
  expect(storeUnavailable(PRODUCTION, explained)).toBe(explained);
  const plain = storeUnavailable(PRODUCTION, new RunnerFailure({ code: "CREDENTIAL_STORE_UNAVAILABLE", boundary: "authentication", message: "x", action: "y", exitCode: 10, retryable: false }));
  expect(plain.details).toEqual({ reason: `the OS credential store is unavailable; set ${VARIABLE} for this command` });
});

function fake(body: string): { dir: string; program: string } {
  const dir = mkdtempSync(join(tmpdir(), "prose-security-"));
  const program = join(dir, "security");
  writeFileSync(program, `#!/bin/sh\nR='${dir}'\n[ "$1" = -i ] || exit 9\n/bin/cat > "$R/stdin"\n${body}\n`);
  chmodSync(program, 0o755);
  return { dir, program };
}

test.skipIf(process.platform === "win32")("a session sends the script on stdin with an empty environment", async () => {
  const { dir, program } = fake(`/usr/bin/env > "$R/env"; printf 'out'; printf 'err' >&2; exit 3`);
  try {
    expect(await spawnSecurity(program, "find-generic-password -s x -a api-key\n", 10_000)).toEqual({ exitCode: 3, stdout: "out", stderr: "err" });
    expect(readFileSync(join(dir, "stdin"), "utf8")).toBe("find-generic-password -s x -a api-key\n");
    const env = readFileSync(join(dir, "env"), "utf8").split("\n").filter(Boolean);
    expect(env.every((line) => /^(PWD|SHLVL|_|OLDPWD)=/u.test(line))).toBe(true);
    expect(await spawnSecurity(join(dir, "missing"), "", 1000)).toBe("failed");
    expect(await spawnSecurity(fake("kill -9 $$").program, "", 5000)).toBe("failed");
    expect(await spawnSecurity(fake("/usr/bin/head -c 9000 /dev/zero; exit 0").program, "", 5000)).toBe("failed");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test.skipIf(process.platform === "win32")("a session is bounded and cancellable", async () => {
  const { dir, program } = fake("exec /bin/sleep 30");
  try {
    let began = performance.now();
    expect(await spawnSecurity(program, "", 300)).toBe("failed");
    expect(performance.now() - began).toBeLessThan(5000);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 200);
    began = performance.now();
    expect(await spawnSecurity(program, "", 10_000, controller.signal)).toBe("cancelled");
    expect(performance.now() - began).toBeLessThan(3000);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test.skipIf(process.platform === "win32")("macOS uses the security program end to end and migrates an earlier item", async () => {
  // Replays the fixture's legacy-item scenario through a real process.
  const { dir, program } = fake([
    `n=$(/bin/cat "$R/n" 2>/dev/null || echo 0); /bin/cp "$R/stdin" "$R/stdin-$n"; echo $((n + 1)) > "$R/n"`,
    `case $n in 0) printf '%s\\n' '"icmt"<blob>=<NULL>' ;; 1) printf '%s\\n' '${TOKEN}' ;; esac`,
    "exit 0",
  ].join("\n"));
  try {
    const value = await nativeStore(PRODUCTION, { PROSE_TEST_MACOS_SECURITY: program }, "get", undefined, undefined, "darwin");
    expect(value).toBe(TOKEN);
    const scripts = [0, 1, 2].map((n) => readFileSync(join(dir, `stdin-${n}`), "utf8"));
    const legacy = fixture.scenarios.find((scenario: { id: string }) => scenario.id === "get-legacy-item-migrates");
    expect(scripts).toEqual(legacy.expectedScripts);
    // Windows and other platforms have no store.
    await expect(nativeStore(PRODUCTION, {}, "get", undefined, undefined, "win32")).rejects.toMatchObject({ code: "CREDENTIAL_STORE_UNAVAILABLE" });
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("the runner-error schema admits the keychain-access Action", () => {
  const schema = readFileSync(join(import.meta.dir, "../../shared/schemas/runner-error.schema.json"), "utf8");
  expect(schema).toContain(JSON.stringify(keychainAccessFailure(VARIABLE).action));
});
