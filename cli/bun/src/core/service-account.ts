import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { TEST_SEAMS_ENABLED } from "./build";
import { failure } from "./errors";
import { humanSafeScalar, jsonLine } from "./output";
import { RunnerFailure, type OutputMode, type RunnerOperation } from "./types";

// This target is deliberately independent of hosted language execution.
type ServiceEnvironment = "production" | "staging";
const MAX_BYTES = 65_536;
type RecordValue = Record<string, unknown>;
interface Fixture { environment?: ServiceEnvironment; credentials?: Partial<Record<ServiceEnvironment, string | null>>; credential: string | null; storeAvailable: boolean; exchanges: Array<{method: string; path: string; status: number; body: unknown; origin?: string; expectedBody?: unknown; expectedSha256?: string}>; cancelBeforePoll?: boolean }
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
  const path = TEST_SEAMS_ENABLED ? deps.env.PROSE_TEST_SERVICE_FIXTURE : undefined;
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
  constructor(readonly deps: Dependencies, readonly environment: ServiceEnvironment, readonly fixture?: Fixture) {
    if (fixture?.environment !== undefined && fixture.environment !== environment) throw failure("SERVICE_PROTOCOL_INVALID");
  }
  get origin(): string { return `https://run-prose-${this.environment}.openprose.workers.dev`; }
  get storeIdentity(): { service: string; name: string } { return { service: `org.openprose.cli.${this.environment}`, name: "api-key" }; }
  get environmentToken(): string | undefined { return this.deps.env[this.environment === "production" ? "OPENPROSE_API_KEY" : "OPENPROSE_STAGING_API_KEY"]; }
  get fixtureCredential(): string | null { return this.fixture?.credentials === undefined ? this.fixture?.credential ?? null : this.fixture.credentials[this.environment] ?? null; }
  checkCancel(): void { if (this.deps.cancellationSignal?.aborted) throw failure("CANCELLED"); }
  async store(action: "get" | "set" | "delete", value?: string): Promise<string | null> {
    this.checkCancel();
    try {
      if (this.fixture !== undefined) {
        if (!this.fixture.storeAvailable) throw new Error();
        if (action !== "get") {
          const next = action === "set" ? value! : null;
          if (this.fixture.credentials !== undefined) this.fixture.credentials[this.environment] = next;
          else this.fixture.credential = next;
        }
        return this.fixtureCredential;
      }
      if (typeof Bun.secrets?.get !== "function") throw new Error();
      // Native APIs cannot cancel an in-flight keychain mutation. Bound waiting,
      // but report store failure (not cancellation) when its outcome is unknown.
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const operation = action === "get" ? Bun.secrets.get(this.storeIdentity)
          : action === "set" ? Bun.secrets.set({ ...this.storeIdentity, value: value! }).then(() => null)
          : Bun.secrets.delete(this.storeIdentity).then(() => null);
        return await Promise.race([operation, new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error("Credential store timeout")), 10_000);
        })]);
      } finally { if (timer !== undefined) clearTimeout(timer); }
    } catch { throw failure("CREDENTIAL_STORE_UNAVAILABLE"); }
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
      const response = await fetch(`${this.origin}${path}`, { method, redirect: "error", signal, headers: { Accept: "application/json", ...(credential === undefined ? {} : { Authorization: `Bearer ${credential}` }), ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, ...(body === undefined ? {} : { body: Buffer.from(body) }) });
      if (response.status === 401 || response.status === 403) { await response.body?.cancel(); throw failure("SERVICE_AUTH_REQUIRED"); }
      if (response.status >= 500 || response.status === 429) { await response.body?.cancel(); throw failure("SERVICE_UNAVAILABLE"); }
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
        return { status: integer(exchange.status, 100, 599), body: object(exchange.body) };
      }
      const signal = this.deps.cancellationSignal === undefined ? AbortSignal.timeout(timeoutMs) : AbortSignal.any([this.deps.cancellationSignal, AbortSignal.timeout(timeoutMs)]);
      const response = await fetch(`${this.origin}${path}`, {
        method, redirect: "error", signal,
        headers: { Accept: "application/json", ...(credential === undefined ? {} : { Authorization: `Bearer ${credential}` }), ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      // HTTP authentication/service failures do not depend on an error body's format.
      if (response.status === 401 || response.status === 403) { await response.body?.cancel(); throw failure("SERVICE_AUTH_REQUIRED"); }
      if (response.status >= 500 || response.status === 429) { await response.body?.cancel(); throw failure("SERVICE_UNAVAILABLE"); }
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
      let parsed: unknown;
      try { parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
      catch { throw failure("SERVICE_PROTOCOL_INVALID"); }
      return { status: response.status, body: object(parsed) };
    } catch (error) {
      this.checkCancel();
      if (error instanceof RunnerFailure) throw error;
      throw failure("SERVICE_UNAVAILABLE");
    }
  }
}
function checkStatus(status: number): void {
  if (status === 401 || status === 403) throw failure("SERVICE_AUTH_REQUIRED");
  if (status < 200 || status >= 300) throw failure("SERVICE_UNAVAILABLE");
}
function organizations(body: RecordValue, credential: string): RecordValue[] {
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
async function login(service: Service): Promise<void> {
  // Fail closed before obtaining a credential if its durable store is unavailable.
  await service.store("get");
  const start = await service.request("POST", "/auth/device");
  checkStatus(start.status);
  const deviceCode = text(start.body.device_code);
  const userCode = text(start.body.user_code, 32);
  if (!/^[A-Z0-9-]+$/u.test(userCode) || start.body.verification_uri !== "https://github.com/login/device" || userCode.includes(deviceCode)) throw failure("SERVICE_PROTOCOL_INVALID");
  const expiry = integer(start.body.expires_in, 1, 900);
  let interval = integer(start.body.interval, 1, 30);
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
export async function runServiceAccount(operation: RunnerOperation, mode: OutputMode, deps: Dependencies, environment: ServiceEnvironment = "production"): Promise<number> {
  const isOrg = operation === "org-list";
  const report: RecordValue = isOrg
    ? { schema: "openprose.organization-list/1", environment, organizations: [], problem: null }
    : { schema: "openprose.service-account/1", environment, operation: operation.slice(5), authenticated: false, credentialSource: "none", problem: null };
  if (mode === "human" && environment === "staging") deps.writeStderr("OpenProse staging environment\n");
  let exitCode = 0;
  try {
    const service = new Service(deps, environment, await fixtureFor(deps));
    const environmentToken = service.environmentToken;
    if ((operation === "auth-login" || operation === "auth-logout") && environmentToken !== undefined && environmentToken !== "") throw failure("INVOCATION_INVALID");
    if (operation === "auth-login") {
      await login(service);
      report.authenticated = true;
      report.credentialSource = "os-credential-store";
    } else if (operation === "auth-logout") {
      await service.store("delete");
    } else {
      const fromEnvironment = environmentToken !== undefined && environmentToken !== "";
      const credential = fromEnvironment ? token(environmentToken) : await service.store("get");
      if (credential === null) {
        if (isOrg) throw failure("SERVICE_AUTH_REQUIRED");
      } else {
        const response = await service.request("GET", "/organizations", token(credential));
        checkStatus(response.status);
        const rows = organizations(response.body, credential);
        if (isOrg) report.organizations = rows;
        else { report.authenticated = true; report.credentialSource = fromEnvironment ? "environment" : "os-credential-store"; }
      }
    }
  } catch (caught) {
    const error = caught instanceof RunnerFailure ? caught : failure("SERVICE_UNAVAILABLE");
    report.problem = error.toJSON();
    exitCode = error.exitCode;
  }
  if (mode !== "human") deps.writeStdout(jsonLine(report));
  else {
    if (isOrg) for (const row of report.organizations as RecordValue[]) deps.writeStdout(`${humanSafeScalar(String(row.slug))}\t${humanSafeScalar(String(row.name))}\n`);
    else deps.writeStdout(`OpenProse ${environment} account: ${report.authenticated ? "authenticated" : "signed out"}\n`);
    if (report.problem !== null) { const problem = report.problem as RecordValue; deps.writeStderr(`${problem.code}: ${problem.message}\n${problem.action}\n`); }
  }
  return exitCode;
}
