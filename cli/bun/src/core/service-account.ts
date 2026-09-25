import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { TEST_SEAMS_ENABLED } from "./build";
import { failure, invocationFailure } from "./errors";
import { humanSafeScalar, jsonLine } from "./output";
import { credentialFailure, localizeHint, malformedReason, missingReason, nativeStore, rejectedReason, storeUnavailable, type CredentialSource } from "./service/credentials";
import { clientHeaders, parseJson } from "./service/http";
import { PRODUCTION } from "./service/endpoint";
import { environmentLabel, validCredential, type Environment, type Json } from "./service/manifest";
import { followUpCommand, humanError, structuredOutputFor } from "./service/render";
import { RunnerFailure, type OutputMode, type RunnerOperation } from "./types";

// Compile-time only (scripts/image-bundle.ts); the inline guard below lets a
// release build fold this test seam away.
declare const OPENPROSE_TEST_SEAMS: boolean | undefined;

// This target is deliberately independent of hosted language execution.
// The service is production (a developer build may point it elsewhere; see
// service/endpoint.ts).
const MAX_BYTES = 65_536;
type RecordValue = Record<string, unknown>;
interface Fixture { environment?: string; credentials?: Partial<Record<string, string | null>>; credential: string | null; storeAvailable: boolean; exchanges: Array<{method: string; path: string; status: number; body: unknown; origin?: string; expectedBody?: unknown; expectedSha256?: string}>; cancelBeforePoll?: boolean }
export interface Dependencies {
  env: Readonly<Record<string, string | undefined>>;
  cancellationSignal?: AbortSignal;
  writeStdout(text: string): void;
  writeStderr(text: string): void;
}
function object(value: unknown): RecordValue {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw failure("SERVICE_PROTOCOL_INVALID");
  return value as RecordValue;
}
function text(value: unknown, max = 4096): string {
  if (typeof value !== "string" || value.length === 0 || Array.from(value).length > max || /[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)) throw failure("SERVICE_PROTOCOL_INVALID");
  return value;
}
export function token(value: unknown): string {
  const result = text(value);
  if (!/^rr_test_[0-9a-f]{32}$/u.test(result)) throw failure("SERVICE_PROTOCOL_INVALID");
  return result;
}
function integer(value: unknown, low: number, high: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < low || value > high) throw failure("SERVICE_PROTOCOL_INVALID");
  return value;
}
export async function fixtureFor(deps: Dependencies): Promise<Fixture | undefined> {
  const path = (typeof OPENPROSE_TEST_SEAMS === "boolean" && !OPENPROSE_TEST_SEAMS) ? undefined : TEST_SEAMS_ENABLED ? deps.env.PROSE_TEST_SERVICE_FIXTURE : undefined;
  if (path === undefined) return undefined;
  try {
    const bytes = await readFile(path);
    if (bytes.length > 16_777_216) throw new Error();
    const value = object(JSON.parse(bytes.toString("utf8")));
    if (!(value.credentials !== undefined || value.credential === null || typeof value.credential === "string") || typeof value.storeAvailable !== "boolean" || !Array.isArray(value.exchanges) || value.exchanges.length > 182) throw new Error();
    return value as unknown as Fixture;
  } catch { throw failure("SERVICE_PROTOCOL_INVALID"); }
}
export class Service {
  elapsed = 0;
  constructor(readonly deps: Dependencies, readonly environment: Environment, readonly fixture?: Fixture) {
    if (fixture?.environment !== undefined && fixture.environment !== environment.name) throw failure("SERVICE_PROTOCOL_INVALID");
  }
  get origin(): string { return this.environment.origin; }
  get storeIdentity(): { service: string; name: string } { return { service: this.environment.storeService, name: "api-key" }; }
  get environmentToken(): string | undefined { return this.deps.env[this.environment.credentialEnv]; }
  get fixtureCredential(): string | null {
    return this.fixture?.credentials === undefined ? this.fixture?.credential ?? null : this.fixture.credentials[this.environment.name] ?? null;
  }
  checkCancel(): void { if (this.deps.cancellationSignal?.aborted) throw failure("CANCELLED"); }
  async store(action: "get" | "set" | "delete", value?: string): Promise<string | null> {
    this.checkCancel();
    try {
      if (this.fixture !== undefined) {
        if (!this.fixture.storeAvailable) throw new Error();
        if (action !== "get") {
          const next = action === "set" ? value! : null;
          if (this.fixture.credentials !== undefined) this.fixture.credentials[this.environment.name] = next;
          else this.fixture.credential = next;
        }
        return this.fixtureCredential;
      }
    } catch { throw failure("CREDENTIAL_STORE_UNAVAILABLE"); }
    return nativeStore(this.environment, this.deps.env, action, value, this.deps.cancellationSignal);
  }
  async registryCredential(required: boolean): Promise<string | undefined> {
    const fromEnvironment = this.environmentToken;
    if (fromEnvironment !== undefined && fromEnvironment !== "") return token(fromEnvironment);
    let stored: string | null;
    try { stored = await this.store("get"); }
    catch (error) {
      if (!required && error instanceof RunnerFailure && error.code === "CREDENTIAL_STORE_UNAVAILABLE") return undefined;
      throw error;
    }
    if (stored !== null) return token(stored);
    if (required) throw failure("SERVICE_AUTH_REQUIRED");
    return undefined;
  }
  async registryRequest(method: string, path: string, credential: string | undefined, body?: Uint8Array): Promise<{ status: number; bytes: Uint8Array }> {
    this.checkCancel();
    const maximum = 2 * 1024 * 1024;
    if (!path.startsWith("/registry/v1/organizations/") || (body !== undefined && body.length > maximum)) throw failure("SERVICE_PROTOCOL_INVALID");
    try {
      if (this.fixture !== undefined) {
        const expected = this.environmentToken || (this.fixture.storeAvailable ? this.fixtureCredential : null) || undefined;
        if (credential !== expected) throw failure("SERVICE_PROTOCOL_INVALID");
        const exchange = this.fixture.exchanges.shift();
        if (exchange === undefined || exchange.method !== method || exchange.path !== path || (exchange.origin !== undefined && exchange.origin !== this.origin)) throw failure("SERVICE_PROTOCOL_INVALID");
        if (exchange.expectedBody !== undefined && (body === undefined || new TextDecoder().decode(body) !== (typeof exchange.expectedBody === "string" ? exchange.expectedBody : JSON.stringify(exchange.expectedBody)))) throw failure("SERVICE_PROTOCOL_INVALID");
        if (exchange.expectedSha256 !== undefined && (body === undefined || createHash("sha256").update(body).digest("hex") !== exchange.expectedSha256)) throw failure("SERVICE_PROTOCOL_INVALID");
        const bytes = new TextEncoder().encode(typeof exchange.body === "string" ? exchange.body : JSON.stringify(exchange.body));
        if (bytes.length > maximum) throw failure("SERVICE_PROTOCOL_INVALID");
        return { status: integer(exchange.status, 100, 599), bytes };
      }
      const signal = this.deps.cancellationSignal === undefined ? AbortSignal.timeout(10_000) : AbortSignal.any([this.deps.cancellationSignal, AbortSignal.timeout(10_000)]);
      const response = await fetch(`${this.origin}${path}`, { method, redirect: "manual", signal, headers: { ...Object.fromEntries(clientHeaders()), Accept: "application/json", ...(credential === undefined ? {} : { Authorization: `Bearer ${credential}` }), ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, ...(body === undefined ? {} : { body: Buffer.from(body) }) });
      // Classified on the status before any body is read (the caller maps
      // every other non-2xx status); a service error body is never kept.
      if (response.status < 200 || response.status >= 300) { await response.body?.cancel(); return { status: response.status, bytes: new Uint8Array() }; }
      if (response.body === null) throw failure("SERVICE_PROTOCOL_INVALID");
      const chunks: Uint8Array[] = [];
      let length = 0;
      const reader = response.body.getReader();
      try {
        for (;;) {
          const chunk = await reader.read();
          if (chunk.done) break;
          length += chunk.value.length;
          if (length > maximum) throw failure("SERVICE_PROTOCOL_INVALID");
          chunks.push(chunk.value);
        }
      } finally { await reader.cancel().catch(() => {}); }
      return { status: response.status, bytes: Buffer.concat(chunks) };
    } catch (error) {
      this.checkCancel();
      if (error instanceof RunnerFailure) throw error;
      throw failure("SERVICE_UNAVAILABLE");
    }
  }
  async sleep(seconds: number): Promise<void> {
    this.checkCancel();
    if (this.fixture?.cancelBeforePoll) throw failure("CANCELLED");
    this.elapsed += seconds;
    if (this.fixture !== undefined) return;
    await new Promise<void>((resolve, reject) => {
      const signal = this.deps.cancellationSignal;
      const abort = () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); reject(failure("CANCELLED")); };
      const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, seconds * 1000);
      signal?.addEventListener("abort", abort, { once: true });
      if (signal?.aborted) abort();
    });
  }
  async request(method: string, path: string, credential?: string, body?: unknown, timeoutMs = 10_000): Promise<{status:number; body:RecordValue}> {
    this.checkCancel();
    try {
      if (this.fixture !== undefined) {
        const expectedCredential = this.environmentToken || this.fixtureCredential;
        if (path === "/organizations" ? credential === undefined || credential !== expectedCredential : credential !== undefined) throw failure("SERVICE_PROTOCOL_INVALID");
        const exchange = this.fixture.exchanges.shift();
        if (exchange === undefined || exchange.method !== method || exchange.path !== path || (exchange.origin !== undefined && exchange.origin !== this.origin) || JSON.stringify(exchange.body).length > MAX_BYTES) throw failure("SERVICE_PROTOCOL_INVALID");
        const status = integer(exchange.status, 100, 599);
        classifyAccountStatus(status, path);
        return { status, body: object(exchange.body) };
      }
      const signal = this.deps.cancellationSignal === undefined ? AbortSignal.timeout(timeoutMs) : AbortSignal.any([this.deps.cancellationSignal, AbortSignal.timeout(timeoutMs)]);
      const response = await fetch(`${this.origin}${path}`, {
        method, redirect: "manual", signal,
        headers: { ...Object.fromEntries(clientHeaders()), Accept: "application/json", ...(credential === undefined ? {} : { Authorization: `Bearer ${credential}` }), ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      // Classified on the status before the body is read (the same code as
      // the fixture path): an error body's format never matters.
      try { classifyAccountStatus(response.status, path); }
      catch (caught) { await response.body?.cancel(); throw caught; }
      if (response.body === null) throw failure("SERVICE_PROTOCOL_INVALID");
      const reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let length = 0;
      try {
        while (true) {
          const item = await reader.read();
          if (item.done) break;
          length += item.value.length;
          if (length > MAX_BYTES) throw failure("SERVICE_PROTOCOL_INVALID");
          chunks.push(item.value);
        }
      } finally { await reader.cancel().catch(() => {}); }
      const bytes = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      const parsed = parseJson(bytes);
      if (parsed === undefined) throw failure("SERVICE_PROTOCOL_INVALID");
      return { status: response.status, body: object(parsed) };
    } catch (error) {
      this.checkCancel();
      if (error instanceof RunnerFailure) throw error;
      throw failure("SERVICE_UNAVAILABLE");
    }
  }
}
/**
 * The status classification of an account request, before its body is read:
 * 401/403 is SERVICE_AUTH_REQUIRED, and any other non-2xx status (a redirect
 * included) is SERVICE_UNAVAILABLE with details.serviceStatus, except the 400
 * a device-code poll answers with (its body says why).
 */
function classifyAccountStatus(status: number, path: string): void {
  if (status === 401 || status === 403) throw failure("SERVICE_AUTH_REQUIRED");
  if ((status < 200 || status >= 300) && !(status === 400 && path.endsWith("/poll"))) throw failure("SERVICE_UNAVAILABLE", { serviceStatus: status });
}
function checkStatus(status: number): void {
  if (status === 401 || status === 403) throw failure("SERVICE_AUTH_REQUIRED");
  if (status < 200 || status >= 300) throw failure("SERVICE_UNAVAILABLE", { serviceStatus: status });
}
/** SERVICE_PROTOCOL_INVALID naming the response field that failed (the reason shape of the service operations). */
function unexpected(what: string, field: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason: `unexpected ${what} response: ${field}` });
}
/** Runs `check`, turning a protocol failure without a reason into `unexpected(what, field)`. */
function field<T>(what: string, name: string, check: () => T): T {
  try { return check(); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_PROTOCOL_INVALID" && caught.details === undefined) throw unexpected(what, name);
    throw caught;
  }
}
function organizations(body: RecordValue, credential: string): RecordValue[] {
  return field("GET /organizations", "organizations", () => organizationRows(body, credential));
}
function organizationRows(body: RecordValue, credential: string): RecordValue[] {
  if (!Array.isArray(body.organizations) || body.organizations.length > 1000) throw failure("SERVICE_PROTOCOL_INVALID");
  return body.organizations.map((raw) => {
    const row = object(raw);
    const result: RecordValue = { id: text(row.id), slug: text(row.slug), name: text(row.name) };
    if (row.role !== undefined) {
      if (!["admin", "developer", "reader"].includes(String(row.role))) throw failure("SERVICE_PROTOCOL_INVALID");
      result.role = text(row.role);
    }
    if (Object.values(result).some((value) => String(value).includes(credential))) throw failure("SERVICE_PROTOCOL_INVALID");
    return result;
  });
}
/**
 * Who the key belongs to, when GET /organizations says: a top-level `login`
 * (at most 100 characters, no controls) and the slug of the organization
 * marked `default: true` (null when none is). Undefined when the service
 * names no login; never the key (mirrors Rust `identity`).
 */
function identity(body: RecordValue, credential: string): RecordValue | undefined {
  const login = body.login;
  if (typeof login !== "string" || login === "" || [...login].length > 100 || /[\u0000-\u001f\u007f]/u.test(login) || login.includes(credential)) return undefined;
  const rows = Array.isArray(body.organizations) ? body.organizations : [];
  const marked = rows.find((row) => row !== null && typeof row === "object" && !Array.isArray(row) && (row as RecordValue).default === true) as RecordValue | undefined;
  const slug = marked === undefined ? undefined : marked.slug;
  return { login, organization: typeof slug === "string" && !slug.includes(credential) ? slug : null };
}
/** A login that ended before a key was stored says so (the reason of each ending). */
function loginEnded(error: RunnerFailure): RunnerFailure {
  if (error.details !== undefined) return error;
  const reason = error.code === "DEVICE_AUTH_FAILED" ? "the device authorization was denied; nothing was stored"
    : error.code === "DEVICE_AUTH_EXPIRED" ? "the device code expired before it was approved; nothing was stored"
    : error.code === "CANCELLED" ? "login was interrupted before the code was approved; nothing was stored"
    : undefined;
  return reason === undefined ? error : failure(error.code, { reason });
}
async function login(service: Service): Promise<void> {
  try { await deviceLogin(service); }
  catch (caught) { throw caught instanceof RunnerFailure ? loginEnded(caught) : caught; }
}
async function deviceLogin(service: Service): Promise<void> {
  // Fail closed before obtaining a credential if its durable store is unavailable.
  await service.store("get");
  const start = await service.request("POST", "/auth/device");
  checkStatus(start.status);
  const what = "POST /auth/device";
  const deviceCode = field(what, "device_code", () => text(start.body.device_code));
  const userCode = field(what, "user_code", () => text(start.body.user_code, 32));
  if (!/^[A-Z0-9-]+$/u.test(userCode) || userCode.includes(deviceCode)) throw unexpected(what, "user_code");
  if (start.body.verification_uri !== "https://github.com/login/device") throw unexpected(what, "verification_uri");
  const expiry = field(what, "expires_in", () => integer(start.body.expires_in, 1, 900));
  let interval = field(what, "interval", () => integer(start.body.interval, 1, 30));
  const deadline = performance.now() + expiry * 1000;
  service.deps.writeStderr(`Go to https://github.com/login/device and enter code: ${userCode}\n`);
  for (let count = 0; count < 180; count += 1) {
    if (service.elapsed + interval >= expiry || (service.fixture === undefined && performance.now() + interval * 1000 >= deadline)) throw failure("DEVICE_AUTH_EXPIRED");
    await service.sleep(interval);
    const remaining = service.fixture === undefined ? deadline - performance.now() : (expiry - service.elapsed) * 1000;
    if (remaining <= 0) throw failure("DEVICE_AUTH_EXPIRED");
    const poll = await service.request("POST", "/auth/device/poll", undefined, { device_code: deviceCode }, Math.max(1, Math.min(10_000, Math.floor(remaining))));
    if (poll.body.status === "error") throw failure(poll.body.error === "expired_token" ? "DEVICE_AUTH_EXPIRED" : "DEVICE_AUTH_FAILED");
    checkStatus(poll.status);
    if (poll.body.status === "pending") continue;
    if (poll.body.status === "slow_down") { interval = Math.min(30, interval + 5); continue; }
    if (poll.body.status !== "complete") throw failure("SERVICE_PROTOCOL_INVALID");
    if (service.fixture === undefined && performance.now() >= deadline) throw failure("DEVICE_AUTH_EXPIRED");
    await service.store("set", token(poll.body.api_key));
    return;
  }
  throw failure("DEVICE_AUTH_EXPIRED");
}
/**
 * INVOCATION_INVALID for login or logout while the selected variable is set:
 * the command cannot change the key that commands actually use.
 */
function environmentKeyRefusal(operation: "login" | "logout", environment: Environment, mode: OutputMode): RunnerFailure {
  const variable = environment.credentialEnv;
  const command = (words: string) => followUpCommand(environment, mode, words);
  const [reason, action] = operation === "login"
    ? [`${variable} is set, and a non-empty ${variable} takes precedence over a stored key, so \`cli auth login\` would not change the key commands use`,
      `Keep using ${variable} and check it with \`${command("auth status")}\`, or unset ${variable} and run \`${command("auth login")}\`.`]
    : [`${variable} is set; \`cli auth logout\` removes only the stored key, so commands would keep using ${variable}`,
      `Unset ${variable} to stop using that key; then \`${command("auth logout")}\` removes the stored key.`];
  const base = invocationFailure(reason);
  return new RunnerFailure({ code: base.code, boundary: base.boundary, message: base.message, action, exitCode: base.exitCode, retryable: base.retryable, details: { reason, credentialSource: "environment", credentialVariable: variable } });
}

export async function runServiceAccount(operation: RunnerOperation, mode: OutputMode, deps: Dependencies, environment: Environment = PRODUCTION): Promise<number> {
  const isOrg = operation === "org-list";
  const report: RecordValue = isOrg ? { organizations: [] } : { operation: operation.slice(5), authenticated: false, credentialSource: "none" };
  const teaching = environment;
  // Where the key came from; kept on a failure that is about that key.
  let source: CredentialSource = "none";
  let exitCode = 0;
  let problem: RunnerFailure | undefined;
  try {
    const service = new Service(deps, environment, await fixtureFor(deps));
    const environmentToken = service.environmentToken;
    const fromEnvironment = environmentToken !== undefined && environmentToken !== "";
    if ((operation === "auth-login" || operation === "auth-logout") && fromEnvironment) {
      source = "environment";
      throw environmentKeyRefusal(operation === "auth-login" ? "login" : "logout", teaching, mode);
    }
    if (operation === "auth-login") {
      await login(service);
      report.authenticated = true;
      source = "store";
    } else if (operation === "auth-logout") {
      await service.store("delete");
    } else {
      // Status and org list share the service classifier: a
      // missing, malformed or rejected key is SERVICE_AUTH_REQUIRED that names
      // the variable, and its Action follows the key's source.
      let credential: string | null;
      if (fromEnvironment) {
        source = "environment";
        credential = environmentToken;
      } else {
        credential = await service.store("get");
        if (credential !== null) source = "store";
      }
      if (credential === null) {
        if (isOrg) throw credentialFailure(teaching, mode, "none", "missing", missingReason(teaching, mode));
      } else {
        if (!validCredential(credential)) {
          throw credentialFailure(teaching, mode, source, "malformed", localizeHint(teaching, mode, malformedReason(credential, teaching.credentialEnv, source === "environment", teaching)));
        }
        let response: { status: number; body: RecordValue };
        try {
          response = await service.request("GET", "/organizations", credential);
          checkStatus(response.status);
        } catch (caught) {
          if (caught instanceof RunnerFailure && caught.code === "SERVICE_AUTH_REQUIRED") throw credentialFailure(teaching, mode, source, "rejected", rejectedReason(teaching, mode, source));
          throw caught;
        }
        const rows = organizations(response.body, credential);
        if (isOrg) report.organizations = rows;
        else {
          report.authenticated = true;
          const who = identity(response.body, credential);
          if (who !== undefined) report.identity = who;
        }
      }
    }
  } catch (caught) {
    let error = caught instanceof RunnerFailure ? caught : failure("SERVICE_UNAVAILABLE");
    // An unavailable store names the variable that works without it (not for
    // logout, which only removes a stored key).
    if (error.code === "CREDENTIAL_STORE_UNAVAILABLE" && operation !== "auth-logout") error = storeUnavailable(teaching, error, mode);
    problem = error;
    exitCode = error.exitCode;
    // A failure that is about the key keeps the key's source; others name none.
    if (error.code !== "SERVICE_AUTH_REQUIRED" && error.code !== "INVOCATION_INVALID") source = "none";
    if (isOrg) report.organizations = [];
  }
  if (!isOrg) report.credentialSource = source;
  if (mode !== "human") {
    // The service-operation/1 envelope every `cli` command prints: the account
    // state (or the organizations) is the result.
    const result = problem ?? (isOrg ? { organizations: report.organizations as Json }
      : { authenticated: report.authenticated as boolean, credentialSource: source, ...(report.identity === undefined ? {} : { identity: report.identity as Json }) });
    deps.writeStdout(structuredOutputFor(isOrg ? "org.list" : `auth.${operation.slice(5)}`, mode, result));
    return exitCode;
  }
  // Human: a failure prints only its error (label, Detail, Action) on stderr,
  // never a status line; success prints a dev build's custom-endpoint banner
  // on stderr, then the result on stdout (identical in both products).
  if (problem !== undefined) {
    deps.writeStderr(humanError(environmentLabel(environment), problem));
    return exitCode;
  }
  if (environment.name !== "production") deps.writeStderr(`${environmentLabel(environment)}\n`);
  if (isOrg) {
    for (const row of report.organizations as RecordValue[]) {
      deps.writeStdout(`${humanSafeScalar(String(row.slug))}  ${row.role === undefined ? "-" : String(row.role)}  ${humanSafeScalar(String(row.name))}\n`);
    }
  } else {
    const who = report.identity as RecordValue | undefined;
    const as = who === undefined ? "" : ` as ${humanSafeScalar(String(who.login))}${typeof who.organization === "string" ? ` (organization ${humanSafeScalar(who.organization)})` : ""}`;
    deps.writeStdout(`OpenProse account ${String(report.operation)}: ${report.authenticated ? `authenticated${as}` : "signed out"}\n`);
  }
  return exitCode;
}
