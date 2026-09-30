// Credential teaching shared by service operations and the
// account commands (`cli auth status|login|logout`, `cli org list`).
// Mirrors the credential helpers in
// cli/rust/crates/prose-runner-core/src/service/mod.rs.
import { TEST_SEAMS_ENABLED } from "../build";
import { failure } from "../errors";
import { RunnerFailure, type OutputMode } from "../types";
import { environmentLabel, type Environment } from "./manifest";
import { followUpCommand } from "./render";

export type CredentialSource = "environment" | "store" | "none";

/**
 * The Action of a failed credential. It follows where the key came from: an
 * environment key is replaced or unset (`cli auth login` would not change it),
 * a stored key is renewed by login, and a missing key names both ways to
 * supply one.
 */
export function credentialAction(environment: Environment, mode: OutputMode, source: CredentialSource): string {
  const variable = environment.credentialEnv;
  const login = () => followUpCommand(environment, mode, "auth login");
  if (source === "environment") return `Replace or unset ${variable}, then retry.`;
  if (source === "store") return `Run \`${login()}\` again, or set ${variable}, then retry.`;
  return `Set ${variable} or run \`${login()}\`, then retry.`;
}

/** SERVICE_AUTH_REQUIRED for a credential problem with its source, variable, reason and source-specific Action. */
export function credentialFailure(environment: Environment, mode: OutputMode, source: CredentialSource, problem: "missing" | "malformed" | "rejected", reason: string): RunnerFailure {
  const base = failure("SERVICE_AUTH_REQUIRED");
  return new RunnerFailure({
    code: base.code, boundary: base.boundary, message: base.message, exitCode: base.exitCode, retryable: base.retryable,
    action: credentialAction(environment, mode, source),
    details: { reason, credentialSource: source, credentialVariable: environment.credentialEnv, credentialProblem: problem },
  });
}

/** A CREDENTIAL_STORE_UNAVAILABLE that names the variable to set instead. A failure the store already explained (the macOS keychain-access and outcome-unknown cases) keeps its own reason and Action. */
export function storeUnavailable(environment: Environment, cause?: unknown, _mode: OutputMode = "human"): RunnerFailure {
  if (cause instanceof RunnerFailure && cause.code === "CREDENTIAL_STORE_UNAVAILABLE" && cause.details?.reason !== undefined) return cause;
  const variable = environment.credentialEnv;
  const base = failure("CREDENTIAL_STORE_UNAVAILABLE");
  return new RunnerFailure({
    code: base.code, boundary: base.boundary, message: base.message, exitCode: base.exitCode, retryable: base.retryable,
    action: `Set ${variable} for this command, or unlock or configure the operating system credential store, then retry.`,
    details: { reason: `the OS credential store is unavailable; set ${variable} for this command` },
  });
}

// ---------------------------------------------------------------------------
// macOS login keychain through `/usr/bin/security -i` (the same commands,
// classification and reasons as the Rust port; both test against
// shared/fixtures/credentials/macos-security.v1.json). The item is created by
// the `security` tool itself, so its access list trusts /usr/bin/security and
// every build of either port reads it without a keychain prompt. An item left
// by an earlier build (without the comment marker) is re-created through
// `security` after one successful read.

export const SECURITY_PROGRAM = "/usr/bin/security";
export const SECURITY_ACCOUNT = "api-key";
export const SECURITY_COMMENT = "openprose-cli-v1";
export const SECURITY_BOUND_MS = 10_000;
export const SECURITY_OUTPUT_LIMIT = 8192;
export const SECURITY_MARKERS = {
  absent: ["could not be found", "(-25300)"],
  denied: ["User canceled", "(-128)", "user name or passphrase you entered is not correct", "(-25293)"],
  "interaction-not-allowed": ["User interaction is not allowed", "(-25308)"],
  unavailable: ["SecKeychain", "SecItem", "error:"],
} as const;

export type SecurityClass = "absent" | "denied" | "interaction-not-allowed" | "unavailable" | "ok";
/** A finished `security -i` session, or why it did not finish (spawn failure, signal, time bound or oversized output is "failed"). */
export type SecuritySession = { exitCode: number; stdout: string; stderr: string } | "failed" | "cancelled";

/** Classifies a finished session by its standard error, then its exit status. */
export function classifySecurity(exitCode: number, stderr: string): SecurityClass {
  const has = (markers: readonly string[]) => markers.some((marker) => stderr.includes(marker));
  if (has(SECURITY_MARKERS.absent)) return "absent";
  if (has(SECURITY_MARKERS.denied)) return "denied";
  if (has(SECURITY_MARKERS["interaction-not-allowed"])) return "interaction-not-allowed";
  if (has(SECURITY_MARKERS.unavailable) || exitCode !== 0) return "unavailable";
  return "ok";
}

export const securityScripts = {
  probe: (service: string) => `find-generic-password -s ${service} -a ${SECURITY_ACCOUNT}\n`,
  read: (service: string) => `find-generic-password -s ${service} -a ${SECURITY_ACCOUNT} -w\n`,
  remove: (service: string) => `delete-generic-password -s ${service} -a ${SECURITY_ACCOUNT}\n`,
  add: (service: string, token: string) => `add-generic-password -U -s ${service} -a ${SECURITY_ACCOUNT} -j ${SECURITY_COMMENT} -w ${token}\n`,
};
export const securityOwnedMarker = `"icmt"<blob>="${SECURITY_COMMENT}"`;

function storeFailure(reason: string, action: string): RunnerFailure {
  const base = failure("CREDENTIAL_STORE_UNAVAILABLE");
  return new RunnerFailure({ code: base.code, boundary: base.boundary, message: base.message, exitCode: base.exitCode, retryable: base.retryable, action, details: { reason } });
}

/** The keychain refused to read an earlier build's item without a prompt. */
export function keychainAccessFailure(variable: string): RunnerFailure {
  return storeFailure(
    `the macOS keychain needs approval in a prompt before this build can read the stored key (the key was saved by an earlier build of the CLI); set ${variable} for this command`,
    `Run the command in a desktop session and allow access in the macOS keychain prompt once, or set ${variable} for this command.`,
  );
}

/** A cancelled write whose outcome the store never confirmed. */
export function outcomeUnknownFailure(variable: string): RunnerFailure {
  return storeFailure(
    `the command was cancelled before the operating system credential store confirmed the change, so it is unknown whether the key was stored; set ${variable} for this command`,
    `Set ${variable} for this command, or unlock or configure the operating system credential store, then retry.`,
  );
}

const TOKEN = /^rr_test_[0-9a-f]{32}$/u;

/** Runs `get`, `set` or `delete` over `run` (one `security -i` session per call). `get` and `delete` report a missing item as null. */
export async function operateSecurity(run: (script: string) => Promise<SecuritySession>, operation: "get" | "set" | "delete", token: string | undefined, service: string, variable: string): Promise<string | null> {
  if (!/^[a-z0-9.-]+$/u.test(service)) throw failure("CREDENTIAL_STORE_UNAVAILABLE");
  const writing = operation === "set";
  const step = async (script: string): Promise<{ klass: SecurityClass; stdout: string; stderr: string }> => {
    const session = await run(script);
    if (session === "cancelled") throw writing ? outcomeUnknownFailure(variable) : failure("CANCELLED");
    if (session === "failed") throw failure("CREDENTIAL_STORE_UNAVAILABLE");
    return { klass: classifySecurity(session.exitCode, session.stderr), stdout: session.stdout, stderr: session.stderr };
  };
  const refused = (klass: SecurityClass): RunnerFailure => klass === "denied" ? failure("CANCELLED")
    : klass === "interaction-not-allowed" ? keychainAccessFailure(variable) : failure("CREDENTIAL_STORE_UNAVAILABLE");
  if (operation === "get") {
    const probe = await step(securityScripts.probe(service));
    if (probe.klass === "absent") return null;
    if (probe.klass !== "ok") throw refused(probe.klass);
    const owned = probe.stdout.includes(securityOwnedMarker) || probe.stderr.includes(securityOwnedMarker);
    const read = await step(securityScripts.read(service));
    if (read.klass === "absent") return null;
    if (read.klass !== "ok") throw refused(read.klass);
    const value = read.stdout.replace(/^[ \t\r\n]+|[ \t\r\n]+$/gu, "");
    if (!owned && TOKEN.test(value)) {
      // Best effort: re-create the item through `security`, so later reads by any build never prompt.
      await run(securityScripts.remove(service) + securityScripts.add(service, value)).catch(() => undefined);
    }
    return value;
  }
  if (operation === "set") {
    if (token === undefined || !TOKEN.test(token)) throw failure("SERVICE_PROTOCOL_INVALID");
    const removed = await step(securityScripts.remove(service));
    if (removed.klass !== "absent" && removed.klass !== "ok") throw refused(removed.klass);
    const added = await step(securityScripts.add(service, token));
    if (added.klass === "ok") return null;
    throw added.klass === "absent" ? failure("CREDENTIAL_STORE_UNAVAILABLE") : refused(added.klass);
  }
  const removed = await step(securityScripts.remove(service));
  if (removed.klass === "absent" || removed.klass === "ok") return null;
  throw refused(removed.klass);
}

/** One `security -i` session: the script on standard input, an empty environment, a time bound and bounded output. */
export async function spawnSecurity(program: string, script: string, boundMs: number, signal?: AbortSignal): Promise<SecuritySession> {
  if (signal?.aborted) return "cancelled";
  let child: ReturnType<typeof Bun.spawn>;
  try {
    child = Bun.spawn([program, "-i"], { cwd: "/", env: {}, stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  } catch { return "failed"; }
  let ending: "cancelled" | "failed" | undefined;
  const stop = (why: "cancelled" | "failed") => { ending ??= why; try { child.kill("SIGKILL"); } catch { /* already gone */ } };
  const onAbort = () => stop("cancelled");
  signal?.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => stop("failed"), boundMs);
  const collect = async (stream: ReadableStream<Uint8Array>): Promise<Uint8Array | undefined> => {
    const chunks: Uint8Array[] = [];
    let length = 0;
    const reader = stream.getReader();
    try {
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        length += chunk.value.length;
        if (length > SECURITY_OUTPUT_LIMIT) { stop("failed"); return undefined; }
        chunks.push(chunk.value);
      }
    } catch { return undefined; } finally { reader.releaseLock(); }
    return Buffer.concat(chunks);
  };
  try {
    const stdin = child.stdin as { write(data: string): unknown; end(): unknown };
    try { stdin.write(script); await stdin.end(); } catch { stop("failed"); }
    const [stdout, stderr, exitCode] = await Promise.all([
      collect(child.stdout as ReadableStream<Uint8Array>), collect(child.stderr as ReadableStream<Uint8Array>), child.exited,
    ]);
    if (ending !== undefined) return ending;
    if (signal?.aborted) return "cancelled";
    if (stdout === undefined || stderr === undefined || child.signalCode !== null || typeof exitCode !== "number") return "failed";
    const decode = (bytes: Uint8Array) => new TextDecoder().decode(bytes);
    return { exitCode, stdout: decode(stdout), stderr: decode(stderr) };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

/** The macOS store of this environment: `/usr/bin/security`, or (test builds only) the PROSE_TEST_MACOS_SECURITY program. */
export async function macosStore(environment: Environment, operation: "get" | "set" | "delete", value: string | undefined, signal: AbortSignal | undefined, program: string = SECURITY_PROGRAM): Promise<string | null> {
  if (signal?.aborted) throw failure("CANCELLED");
  return operateSecurity((script) => spawnSecurity(program, script, SECURITY_BOUND_MS, signal), operation, value, environment.storeService, environment.credentialEnv);
}

/** Why no key was found. */
export function missingReason(environment: Environment, mode: OutputMode): string {
  return `no API key for ${environmentLabel(environment)}: set ${environment.credentialEnv} or run \`${followUpCommand(environment, mode, "auth login")}\``;
}

/** Why the service refused the key (401, or 403 `Invalid API key.`). */
export function rejectedReason(environment: Environment, mode: OutputMode, source: CredentialSource): string {
  const variable = environment.credentialEnv;
  const login = followUpCommand(environment, mode, "auth login");
  return source === "environment"
    ? `the service rejected the API key in ${variable} (unknown or revoked); replace or unset ${variable}: an environment key takes precedence over \`${login}\``
    : `the service rejected the API key stored for ${environmentLabel(environment)} (unknown or revoked); run \`${login}\` again`;
}

/** Why a key fails the strict `rr_test_` predicate; `prose cli` hints are not yet localized. */
export function malformedReason(token: string, variable: string, fromEnvironment: boolean, environment: Environment): string {
  const shape = "an OpenProse API key is rr_test, an underscore and 32 lowercase hex digits";
  if (!fromEnvironment) return `the key stored for ${environmentLabel(environment)} is not a valid OpenProse API key (${shape}); run \`prose cli auth login\` again`;
  if (token.startsWith("rr_live_")) return `the ${variable} value starts with rr_live_, which is not an OpenProse API key (${shape}); replace or unset ${variable}`;
  return `the ${variable} value is not a valid OpenProse API key (${shape}); replace or unset ${variable}: an environment key takes precedence over \`prose cli auth login\``;
}

/** Rewrites every `` `prose cli ...` `` command in a fixed hint to keep the output mode. */
export function localizeHint(environment: Environment, mode: OutputMode, text: string): string {
  return text.split("`prose cli ").join(`\`${followUpCommand(environment, mode, "")} `);
}

// Compile-time only (scripts/image-bundle.ts); the inline guard below lets a
// release build fold the test seam away.
declare const OPENPROSE_TEST_SEAMS: boolean | undefined;

/**
 * The operating-system credential store: the login keychain through
 * /usr/bin/security on macOS, the Secret Service through Bun.secrets
 * (libsecret, the item the Rust port's secret-tool shares) on Linux, and none
 * elsewhere (set the variable instead). Cancellation ends a read or delete
 * with CANCELLED; a cancelled write is CREDENTIAL_STORE_UNAVAILABLE with the
 * outcome-unknown reason.
 */
export async function nativeStore(environment: Environment, env: Readonly<Record<string, string | undefined>>, operation: "get" | "set" | "delete", value: string | undefined, signal: AbortSignal | undefined, platform: string = process.platform): Promise<string | null> {
  if (signal?.aborted) throw failure("CANCELLED");
  if (platform === "darwin") {
    const seam = (typeof OPENPROSE_TEST_SEAMS !== "boolean" || OPENPROSE_TEST_SEAMS) && TEST_SEAMS_ENABLED ? env.PROSE_TEST_MACOS_SECURITY : undefined;
    return macosStore(environment, operation, value, signal, seam === undefined || seam === "" ? SECURITY_PROGRAM : seam);
  }
  if (platform !== "linux" || typeof Bun.secrets?.get !== "function") throw failure("CREDENTIAL_STORE_UNAVAILABLE");
  const identity = { service: environment.storeService, name: SECURITY_ACCOUNT };
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  try {
    const call = operation === "get" ? Bun.secrets.get(identity)
      : operation === "set" ? Bun.secrets.set({ ...identity, value: value! }).then(() => null)
      : Bun.secrets.delete(identity).then(() => null);
    return await Promise.race([
      call.catch(() => { throw failure("CREDENTIAL_STORE_UNAVAILABLE"); }),
      new Promise<never>((_, reject) => { timer = setTimeout(() => reject(failure("CREDENTIAL_STORE_UNAVAILABLE")), SECURITY_BOUND_MS); }),
      new Promise<never>((_, reject) => {
        onAbort = () => reject(operation === "set" ? outcomeUnknownFailure(environment.credentialEnv) : failure("CANCELLED"));
        signal?.addEventListener("abort", onAbort, { once: true });
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    if (onAbort !== undefined) signal?.removeEventListener("abort", onAbort);
  }
}
