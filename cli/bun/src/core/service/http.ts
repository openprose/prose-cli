// Service transport: bounded requests with client identity headers, no
// redirects and no retries, the body-code-first classifier, and the test-seam
// fixture transport (PROSE_TEST_SERVICE_FIXTURE).
import { createHash, randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { TEST_SEAMS_ENABLED, RUNNER_VERSION } from "../build";
import { failure } from "../errors";
import { RunnerFailure, type RunnerErrorCode } from "../types";
import { manifest, type Environment, type Json, type JsonObject, type ManifestOperation } from "./manifest";
import { canonicalJson, patternMatches, sanitizeServiceMessage } from "./render";
import { FixtureSource, SseReader, StreamSource, fixtureBytes, type StreamEnd } from "./sse";
import { nativeStore } from "./credentials";

// Compile-time only (scripts/image-bundle.ts); the inline guard below lets a
// release build fold this test seam away.
declare const OPENPROSE_TEST_SEAMS: boolean | undefined;

export type TransportClass = "account" | "control" | "listing" | "stream" | "download";

function limit(klass: TransportClass, key: string): number {
  return Number(manifest.transportClasses[klass]?.[key] ?? 0);
}

export function maxResponseBytes(klass: TransportClass): number {
  return klass === "stream" || klass === "download" ? limit("control", "maxResponseBytes") : limit(klass, "maxResponseBytes");
}
export function timeoutMs(klass: TransportClass): number {
  return klass === "stream" || klass === "download" ? limit("control", "timeoutMs") : limit(klass, "timeoutMs");
}
export function maxEventBytes(): number { return limit("stream", "maxEventBytes") || 1 << 20; }

export interface Request {
  method: string;
  /** Manifest path template, used for classification. */
  template: string;
  /** Concrete, percent-encoded path. */
  path: string;
  query: Array<[string, string]>;
  headers: Array<[string, string]>;
  body?: Uint8Array;
  bearer: boolean;
  class: TransportClass;
}

/** A request for manifest template `index` of `operation`. */
export function requestFor(operation: ManifestOperation, index: number, path: string): Request {
  const template = operation.requests[index]!;
  return {
    method: template.method, template: template.path, path, query: [], headers: [],
    bearer: template.auth === "bearer", class: operation.transport as TransportClass,
  };
}

/** Sets a compact JSON body with sorted keys (both products serialize identically). */
export function withJsonBody(request: Request, value: Json): Request {
  return { ...request, body: new TextEncoder().encode(canonicalJson(value)) };
}

export function clientHeaders(): Array<[string, string]> {
  return [["X-OpenProse-Client", `cli/${RUNNER_VERSION}+bun`], ["User-Agent", `prose-cli/${RUNNER_VERSION}`]];
}

function hasHeader(request: Request, name: string): boolean {
  return request.headers.some(([key]) => key.toLowerCase() === name.toLowerCase());
}

function wireHeaders(request: Request, token: string | undefined): Array<[string, string]> {
  const headers = clientHeaders();
  if (!hasHeader(request, "Accept")) headers.push(["Accept", "application/json"]);
  if (token !== undefined) headers.push(["Authorization", `Bearer ${token}`]);
  if (request.body !== undefined && !hasHeader(request, "Content-Type")) headers.push(["Content-Type", "application/json"]);
  headers.push(...request.headers);
  return headers;
}

/** Percent-encodes one path segment or query component (RFC 3986 unreserved stay). */
export function encodeSegment(value: string): string {
  let encoded = "";
  for (const byte of new TextEncoder().encode(value)) {
    const character = String.fromCharCode(byte);
    encoded += /[A-Za-z0-9\-._~]/u.test(character) ? character : `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return encoded;
}

function url(origin: string, request: Request): string {
  return `${origin}${request.path}${request.query.map(([name, value], index) => `${index === 0 ? "?" : "&"}${encodeSegment(name)}=${encodeSegment(value)}`).join("")}`;
}

export interface Response {
  status: number;
  /** Lower-case header names. */
  headers: Map<string, string>;
  body: Uint8Array;
}

/** The body as a JSON object, or SERVICE_PROTOCOL_INVALID. */
export function jsonObject(response: Response): JsonObject {
  const value = parseJson(response.body);
  if (value === undefined || value === null || typeof value !== "object" || Array.isArray(value)) throw failure("SERVICE_PROTOCOL_INVALID", { reason: RESPONSE_NOT_OBJECT });
  return value as JsonObject;
}

/** The reason of a response body that is not a JSON object under the shared rules. */
export const RESPONSE_NOT_OBJECT = "the service response is not a valid JSON object";

/** The largest integer both ports represent exactly. */
export const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;

/** Whether a parsed value keeps the shared rules: no lone surrogate, fewer than 128 nested containers, no integral number beyond the safe range. */
function acceptable(value: unknown, depth: number): boolean {
  if (typeof value === "string") return !/\p{Surrogate}/u.test(value);
  if (typeof value === "number") return !Number.isInteger(value) || Number.isSafeInteger(value);
  if (value === null || typeof value !== "object") return true;
  if (depth + 1 >= 128) return false;
  if (Array.isArray(value)) return value.every((item) => acceptable(item, depth + 1));
  return Object.entries(value).every(([key, item]) => acceptable(key, depth + 1) && acceptable(item, depth + 1));
}

/**
 * A service body or event as JSON, with the rules both ports share
 * (shared/fixtures/transport/json-parse.json): strict UTF-8 without a byte
 * order mark, no lone surrogate, fewer than 128 nested containers, and no
 * integral number beyond the safe range. undefined is rejected.
 */
export function parseJson(bytes: Uint8Array): Json | undefined {
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); }
  catch { return undefined; }
  return parseJsonText(text);
}

/** {@link parseJson} of already decoded text (a stream event's data). */
export function parseJsonText(text: string): Json | undefined {
  let value: Json;
  try { value = JSON.parse(text) as Json; } catch { return undefined; }
  return acceptable(value, 0) ? value : undefined;
}

export type StreamOpen =
  | { kind: "events"; status: number; headers: Map<string, string>; reader: SseReader }
  | { kind: "response"; response: Response }
  | { kind: "dropped" };

export interface Downloaded { status: number; headers: Map<string, string>; bytes: number; sha256: string }

/**
 * The `details.reason` of a transport failure: every service
 * failure carries `.problem.details`, including a connection that failed
 * before a complete response, where the service said nothing. Mirrors Rust.
 */
export const TRANSPORT_REASON = "the connection to the service failed before a complete response arrived";

/** SERVICE_UNAVAILABLE for a transport failure, with its reason. */
export function unavailable(): RunnerFailure {
  return failure("SERVICE_UNAVAILABLE", { reason: TRANSPORT_REASON });
}

export function fixtureError(reason: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason: `test fixture: ${reason}` });
}

interface FixtureState {
  document: JsonObject;
  next: number;
  completed: number;
  interruptAfter?: number;
  clockMs?: number;
  stepMs: number;
  idsUsed: number;
}

export interface Sink { write(bytes: Uint8Array): void | Promise<void> }

/** The PROSE_TEST_SERVICE_FIXTURE transcript of a test-seam build, if one is set. */
async function testSeamFixture(environment: Environment, env: Readonly<Record<string, string | undefined>>): Promise<FixtureState | undefined> {
  const path = TEST_SEAMS_ENABLED ? env.PROSE_TEST_SERVICE_FIXTURE : undefined;
  if (path === undefined) return undefined;
  let bytes: Uint8Array;
  try { bytes = await readFile(path); }
  catch { throw fixtureError("cannot read PROSE_TEST_SERVICE_FIXTURE"); }
  if (bytes.length > 32 * 1024 * 1024) throw fixtureError("larger than 32 MiB");
  // A transcript is test input, not a service response: plain JSON (its
  // bodies meet the service rules when the product reads them).
  let document: Json;
  try { document = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as Json; }
  catch { throw fixtureError("not JSON"); }
  return parseFixture(document, environment);
}

export class Transport {
  private readonly started = performance.now();
  /**
   * Download-class timeouts (manifest `transportClasses.download`), applied to
   * every Transport.download call exactly as the Rust port does: the wait for
   * response headers, then the gap between body chunks. Mutable only so unit
   * tests can shorten them; no environment variable reaches it.
   */
  downloadTimeouts = { connectMs: Math.max(1, limit("download", "connectTimeoutMs")), idleMs: Math.max(1, limit("download", "idleTimeoutMs")) };
  /**
   * Stream limits: connectMs bounds only the wait for response headers and
   * idleMs the gap between chunks (Rust: ureq timeout_connect/timeout_read).
   * Never a whole-exchange bound: a live run streams for minutes.
   */
  streamTimeouts = { connectMs: Math.max(1, limit("stream", "connectTimeoutMs")), idleMs: Math.max(1, limit("stream", "idleTimeoutMs")) };
  /** The process environment (the test-seam store program of a test build). */
  env: Readonly<Record<string, string | undefined>> = {};
  constructor(
    readonly environment: Environment,
    readonly cancellation: AbortController,
    private readonly fixture?: FixtureState,
  ) {
    this.observeCompletion();
  }

  static async create(environment: Environment, env: Readonly<Record<string, string | undefined>>, cancellation: AbortController): Promise<Transport> {
    // Inline, so a release build folds the fixture seam away.
    if (typeof OPENPROSE_TEST_SEAMS !== "boolean" || OPENPROSE_TEST_SEAMS) {
      const fixture = await testSeamFixture(environment, env);
      if (fixture !== undefined) return new Transport(environment, cancellation, fixture);
    }
    const transport = new Transport(environment, cancellation);
    transport.env = env;
    return transport;
  }

  static withFixture(environment: Environment, document: Json): Transport {
    return new Transport(environment, new AbortController(), parseFixture(document, environment));
  }

  get isFixture(): boolean { return this.fixture !== undefined; }
  get fixtureDocument(): JsonObject | undefined { return this.fixture?.document; }

  check(): void { if (this.cancellation.signal.aborted) throw failure("CANCELLED"); }

  private observeCompletion(): void {
    if (this.fixture !== undefined && this.fixture.interruptAfter === this.fixture.completed) this.cancellation.abort();
  }

  private completeExchange(): void {
    if (this.fixture !== undefined) this.fixture.completed += 1;
    this.observeCompletion();
  }

  /** Fails when a fixture still holds unused exchanges. */
  finish(): void {
    if (this.fixture === undefined) return;
    const total = Array.isArray(this.fixture.document.exchanges) ? this.fixture.document.exchanges.length : 0;
    if (this.fixture.next < total) throw fixtureError(`${total - this.fixture.next} exchange(s) were not requested`);
  }

  nowRfc3339(): string {
    const ms = this.tick();
    return new Date(ms ?? Date.now()).toISOString();
  }

  monotonicMs(): number {
    const ms = this.tick();
    return ms ?? Math.floor(performance.now() - this.started);
  }

  private tick(): number | undefined {
    if (this.fixture?.clockMs === undefined) return undefined;
    const now = this.fixture.clockMs;
    this.fixture.clockMs = now + this.fixture.stepMs;
    return now;
  }

  uuidV4(): string {
    if (this.fixture !== undefined) {
      const ids = this.fixture.document.ids;
      const value = Array.isArray(ids) ? ids[this.fixture.idsUsed] : undefined;
      if (typeof value !== "string") throw fixtureError("no deterministic id left in `ids`");
      this.fixture.idsUsed += 1;
      return value;
    }
    return randomUUID();
  }

  /** The stored credential for this service (fixture or the OS credential store). */
  async storedCredential(): Promise<string | null> {
    this.check();
    if (this.fixture !== undefined) {
      if (this.fixture.document.storeAvailable === false) throw failure("CREDENTIAL_STORE_UNAVAILABLE");
      const credentials = this.fixture.document.credentials;
      const slot = credentials !== undefined && credentials !== null && typeof credentials === "object" && !Array.isArray(credentials)
        ? (credentials as JsonObject)[this.environment.name] : this.fixture.document.credential;
      return typeof slot === "string" ? slot : null;
    }
    return nativeStore(this.environment, this.env, "get", undefined, this.cancellation.signal);
  }

  private nextExchange(request: Request, token: string | undefined): JsonObject {
    const fixture = this.fixture!;
    const index = fixture.next;
    const exchanges = Array.isArray(fixture.document.exchanges) ? fixture.document.exchanges : [];
    const exchange = exchanges[index] as JsonObject | undefined;
    if (exchange === undefined) throw fixtureError(`unexpected request ${request.method} ${request.path} (no exchange left)`);
    fixture.next += 1;
    const place = `exchange ${index}`;
    if (exchange.method !== request.method || exchange.path !== request.path) {
      throw fixtureError(`${place} expects ${String(exchange.method ?? "?")} ${String(exchange.path ?? "?")} but the product sent ${request.method} ${request.path}`);
    }
    if (exchange.origin !== undefined && exchange.origin !== this.environment.origin) throw fixtureError(`${place} origin differs`);
    const expectedQuery = (exchange.query ?? {}) as Record<string, Json>;
    const sent = new Map(request.query);
    if (sent.size !== request.query.length || Object.keys(expectedQuery).length !== sent.size
      || Object.entries(expectedQuery).some(([name, value]) => sent.get(name) !== value)) throw fixtureError(`${place} query differs`);
    const headers = wireHeaders(request, token);
    const assertions = exchange.requestHeaders as Record<string, Json> | undefined;
    for (const [name, expected] of Object.entries(assertions ?? {})) {
      const actual = headers.find(([key]) => key.toLowerCase() === name.toLowerCase())?.[1];
      let ok: boolean;
      if (typeof expected === "string") ok = actual === expected;
      else if (expected !== null && typeof expected === "object" && !Array.isArray(expected) && "present" in expected) ok = (actual !== undefined) === (expected.present === true);
      else if (expected !== null && typeof expected === "object" && !Array.isArray(expected) && typeof expected.pattern === "string") {
        if (actual === undefined) ok = false;
        else {
          const matched = patternMatches(expected.pattern, actual);
          if (matched === undefined) throw fixtureError(`${place} header pattern for ${name} is not supported`);
          ok = matched;
        }
      } else ok = false;
      if (!ok) throw fixtureError(`${place} request header ${name} differs`);
    }
    const body = request.body ?? new Uint8Array();
    if ("expectedBody" in exchange) {
      const parsed = parseJson(body);
      if (parsed === undefined || !jsonEqual(parsed, exchange.expectedBody!)) throw fixtureError(`${place} request body differs`);
    }
    if (typeof exchange.expectedBodyText === "string" && new TextDecoder().decode(body) !== exchange.expectedBodyText) throw fixtureError(`${place} request body text differs`);
    if (typeof exchange.expectedSha256 === "string" && createHash("sha256").update(body).digest("hex") !== exchange.expectedSha256) throw fixtureError(`${place} request body digest differs`);
    return exchange;
  }

  /** Sends a buffered request; any status is returned. */
  async send(request: Request, token: string | undefined): Promise<Response> {
    this.check();
    const cap = maxResponseBytes(request.class);
    if (this.fixture !== undefined) {
      const exchange = this.nextExchange(request, token);
      let outcome: { status: number; headers: Map<string, string>; body: Uint8Array } | RunnerFailure;
      try {
        if (typeof exchange.disconnect === "string") throw unavailable();
        const status = fixtureStatus(exchange);
        const body = fixtureBody(exchange);
        const sse = exchange.sse as JsonObject | undefined;
        if (sse !== undefined && sse.end !== "close") throw unavailable();
        outcome = { status, headers: fixtureHeaders(exchange), body };
      } catch (caught) {
        if (!(caught instanceof RunnerFailure)) throw caught;
        outcome = caught;
      }
      this.completeExchange();
      this.check();
      if (outcome instanceof RunnerFailure) throw outcome;
      return bounded(outcome.status, outcome.headers, outcome.body, cap);
    }
    const response = await this.realCall(request, token, timeoutMs(request.class));
    const body = await readCapped(response, cap, this.cancellation.signal).catch((caught) => {
      this.check();
      throw caught instanceof RunnerFailure ? caught : unavailable();
    });
    this.check();
    return bounded(response.status, headerMap(response.headers), body.bytes, cap, body.over);
  }

  /**
   * `timeout` bounds the whole exchange (headers and body). `connectMs` bounds
   * only the wait for response headers; the body is then read under the
   * caller's own idle timer (download), matching the Rust ureq
   * timeout_connect/timeout_read pair.
   */
  private async realCall(request: Request, token: string | undefined, timeout?: number, connectMs?: number): Promise<globalThis.Response> {
    const signals = [this.cancellation.signal];
    if (timeout !== undefined) signals.push(AbortSignal.timeout(timeout));
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (connectMs !== undefined) {
      const connect = new AbortController();
      timer = setTimeout(() => connect.abort(), connectMs);
      signals.push(connect.signal);
    }
    try {
      return await fetch(url(this.environment.origin, request), {
        method: request.method,
        redirect: "manual",
        signal: AbortSignal.any(signals),
        headers: wireHeaders(request, token),
        ...(request.body === undefined ? {} : { body: Buffer.from(request.body) }),
      });
    } catch {
      this.check();
      throw unavailable();
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  /** Opens a server-sent event stream; failure before headers is `dropped`. */
  async openStream(request: Request, token: string | undefined): Promise<StreamOpen> {
    this.check();
    const cap = maxResponseBytes("stream");
    if (this.fixture !== undefined) {
      const exchange = this.nextExchange(request, token);
      if (exchange.disconnect === "before-response") {
        this.completeExchange();
        this.check();
        return { kind: "dropped" };
      }
      const status = fixtureStatus(exchange);
      const headers = fixtureHeaders(exchange);
      const sse = exchange.sse as JsonObject | undefined;
      if (status < 200 || status >= 300 || sse === undefined) {
        const body = fixtureBody(exchange);
        this.completeExchange();
        this.check();
        if (typeof exchange.disconnect === "string") throw unavailable();
        return { kind: "response", response: bounded(status, headers, body, cap) };
      }
      const frames = exchange.disconnect === "after-headers" ? [] : fixtureBytes(sse.frames ?? []);
      const end: StreamEnd = typeof exchange.disconnect === "string" || sse.end === "disconnect" ? "disconnected" : sse.end === "idle" ? "idle" : "closed";
      const fixture = this.fixture;
      const fire = fixture.interruptAfter === fixture.completed + 1;
      fixture.completed += 1;
      const cancellation = this.cancellation;
      const reader = new SseReader(new FixtureSource(frames, end, () => { if (fire) cancellation.abort(); }), cancellation.signal, maxEventBytes());
      return { kind: "events", status, headers, reader };
    }
    let response: globalThis.Response;
    const { connectMs, idleMs } = this.streamTimeouts;
    try { response = await this.realCall(request, token, undefined, connectMs); }
    catch (caught) {
      if (caught instanceof RunnerFailure && caught.code === "SERVICE_UNAVAILABLE") return { kind: "dropped" };
      throw caught;
    }
    if (response.status < 200 || response.status >= 300 || response.body === null) {
      const body = await readCapped(response, cap, this.cancellation.signal).catch(() => { this.check(); throw unavailable(); });
      this.check();
      return { kind: "response", response: bounded(response.status, headerMap(response.headers), body.bytes, cap, body.over) };
    }
    const reader = new SseReader(new StreamSource(response.body.getReader(), idleMs), this.cancellation.signal, maxEventBytes());
    return { kind: "events", status: response.status, headers: headerMap(response.headers), reader };
  }

  /** Streams a 2xx body into `sink` (at most `maxBytes`); other statuses are classified. */
  async download(request: Request, token: string | undefined, sink: Sink, maxBytes: number, contract: string): Promise<Downloaded> {
    this.check();
    const control = maxResponseBytes("control");
    let status: number;
    let headers: Map<string, string>;
    let chunks: AsyncIterable<Uint8Array>;
    if (this.fixture !== undefined) {
      const exchange = this.nextExchange(request, token);
      let parsed: { status: number; body: Uint8Array } | RunnerFailure;
      try { parsed = { status: fixtureStatus(exchange), body: fixtureBody(exchange) }; }
      catch (caught) { if (!(caught instanceof RunnerFailure)) throw caught; parsed = caught; }
      this.completeExchange();
      this.check();
      if (exchange.disconnect === "before-response" || exchange.disconnect === "after-headers") throw unavailable();
      if (parsed instanceof RunnerFailure) throw parsed;
      status = parsed.status;
      headers = fixtureHeaders(exchange);
      const body = parsed.body;
      const failing = exchange.disconnect === "mid-body";
      chunks = (async function* () {
        yield failing ? body.slice(0, Math.floor(body.length / 2)) : body;
        if (failing) throw new Error("fixture disconnect");
      })();
    } else {
      const { connectMs, idleMs } = this.downloadTimeouts;
      const response = await this.realCall(request, token, undefined, connectMs);
      status = response.status;
      headers = headerMap(response.headers);
      const reader = response.body?.getReader();
      chunks = reader === undefined ? (async function* () {})() : idleBoundedChunks(reader, idleMs);
    }
    if (status < 200 || status >= 300) {
      const collected: Uint8Array[] = [];
      let length = 0;
      try {
        for await (const chunk of chunks) {
          length += chunk.length;
          if (length > control) break;
          collected.push(chunk);
        }
      } catch { this.check(); throw unavailable(); }
      this.check();
      const body = length > control ? new Uint8Array() : Buffer.concat(collected);
      throw classify(contract, request, { status, headers, body }, token);
    }
    const hash = createHash("sha256");
    let total = 0;
    try {
      for await (const chunk of chunks) {
        this.check();
        total += chunk.length;
        if (total > maxBytes) throw failure("SERVICE_RESPONSE_TOO_LARGE");
        hash.update(chunk);
        await sink.write(chunk);
      }
    } catch (caught) {
      this.check();
      if (caught instanceof RunnerFailure) throw caught;
      throw unavailable();
    }
    this.check();
    return { status, headers, bytes: total, sha256: hash.digest("hex") };
  }
}

function parseFixture(document: Json, environment: Environment): FixtureState {
  if (document === null || typeof document !== "object" || Array.isArray(document)) throw fixtureError("must be an object with at most 512 exchanges");
  const exchanges = document.exchanges;
  if (exchanges !== undefined && !(Array.isArray(exchanges) && exchanges.length <= 512)) throw fixtureError("must be an object with at most 512 exchanges");
  if (document.environment !== undefined && document.environment !== environment.name) {
    throw fixtureError("environment differs from the resolved service");
  }
  const clock = document.clock as JsonObject | undefined;
  let clockMs: number | undefined;
  if (clock !== undefined && typeof clock.start === "string") {
    clockMs = Date.parse(clock.start);
    if (Number.isNaN(clockMs)) throw fixtureError("clock.start is not RFC 3339");
  }
  const state: FixtureState = {
    document, next: 0, completed: 0, stepMs: typeof clock?.stepMs === "number" ? clock.stepMs : 0, idsUsed: 0,
  };
  if (typeof document.interruptAfterExchange === "number") state.interruptAfter = document.interruptAfterExchange;
  if (clockMs !== undefined) state.clockMs = clockMs;
  return state;
}

function fixtureStatus(exchange: JsonObject): number {
  const status = exchange.status;
  if (typeof status !== "number" || !Number.isInteger(status) || status < 100 || status > 599) throw fixtureError("status must be 100-599");
  return status;
}

function fixtureHeaders(exchange: JsonObject): Map<string, string> {
  const headers = new Map<string, string>();
  for (const [name, value] of Object.entries((exchange.responseHeaders ?? {}) as Record<string, Json>)) {
    if (typeof value === "string") headers.set(name.toLowerCase(), value);
  }
  if (typeof exchange.redirect === "string") headers.set("location", exchange.redirect);
  return headers;
}

function fixtureBody(exchange: JsonObject): Uint8Array {
  if (typeof exchange.bodyText === "string") return new TextEncoder().encode(exchange.bodyText);
  if (typeof exchange.bodyBase64 === "string") {
    if (!/^[A-Za-z0-9+/]*={0,2}$/u.test(exchange.bodyBase64)) throw fixtureError("bodyBase64 is not base64");
    return new Uint8Array(Buffer.from(exchange.bodyBase64, "base64"));
  }
  if (exchange.sse !== undefined) return Buffer.concat(fixtureBytes((exchange.sse as JsonObject).frames ?? []));
  if ("body" in exchange) return new TextEncoder().encode(canonicalJson(exchange.body!));
  return new Uint8Array();
}

/**
 * Yields a response body's chunks; a gap longer than `idleMs` cancels the
 * reader and fails with SERVICE_UNAVAILABLE (callers map any other rejection
 * the same way, and CANCELLED wins through their `check()`).
 */
export async function* idleBoundedChunks(reader: ReadableStreamDefaultReader<Uint8Array>, idleMs: number): AsyncGenerator<Uint8Array> {
  try {
    for (;;) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const idle = new Promise<"idle">((resolve) => { timer = setTimeout(() => resolve("idle"), idleMs); });
      let item: Awaited<ReturnType<typeof reader.read>> | "idle";
      try { item = await Promise.race([reader.read(), idle]); }
      finally { clearTimeout(timer); }
      if (item === "idle") throw unavailable();
      if (item.done) return;
      yield item.value;
    }
  } finally { await reader.cancel().catch(() => {}); }
}

function headerMap(headers: Headers): Map<string, string> {
  const map = new Map<string, string>();
  headers.forEach((value, name) => map.set(name.toLowerCase(), value));
  return map;
}

async function readCapped(response: globalThis.Response, cap: number, signal: AbortSignal): Promise<{ bytes: Uint8Array; over: boolean }> {
  if (response.body === null) return { bytes: new Uint8Array(), over: false };
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      if (signal.aborted) throw failure("CANCELLED");
      const item = await reader.read();
      if (item.done) break;
      length += item.value.length;
      if (length > cap) return { bytes: new Uint8Array(), over: true };
      chunks.push(item.value);
    }
  } finally { await reader.cancel().catch(() => {}); }
  return { bytes: Buffer.concat(chunks), over: false };
}

function bounded(status: number, headers: Map<string, string>, body: Uint8Array, cap: number, over = false): Response {
  if (over || body.length > cap) {
    if (status >= 200 && status < 300) throw failure("SERVICE_RESPONSE_TOO_LARGE");
    return { status, headers, body: new Uint8Array() };
  }
  return { status, headers, body };
}

function jsonEqual(left: Json, right: Json): boolean {
  if (left === right) return true;
  if (typeof left !== typeof right || left === null || right === null || typeof left !== "object") return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  if (Array.isArray(left)) return left.length === (right as Json[]).length && left.every((item, index) => jsonEqual(item, (right as Json[])[index]!));
  const a = left as JsonObject;
  const b = right as JsonObject;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((key) => key in b && jsonEqual(a[key]!, b[key]!));
}

/** Classifies a non-2xx response: body code, then route override, then status. */
export function classify(contract: string, request: Request, response: Response, credential: string | undefined): RunnerFailure {
  const table = manifest.errorClassification;
  const parsed = parseJson(response.body);
  const body = parsed !== undefined && parsed !== null && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as JsonObject : undefined;
  const bodyCode = typeof body?.code === "string" ? body.code : undefined;
  const status = response.status;
  const byBody = bodyCode !== undefined && Object.hasOwn(table.bodyCodes, bodyCode) ? table.bodyCodes[bodyCode] : undefined;
  const byRoute = table.routeOverrides.find((entry) => entry.code !== "RUN_SUBMISSION_AMBIGUOUS" && entry.method === request.method && entry.path === request.template && entry.status === status)?.code;
  const byStatus = table.statuses[String(status)] ?? (status >= 500 && status <= 599 ? table.statuses["5xx"] : undefined) ?? table.otherStatus;
  const code = (byBody ?? byRoute ?? byStatus ?? "SERVICE_UNAVAILABLE") as RunnerErrorCode;
  const details: Record<string, unknown> = { serviceStatus: status };
  if (bodyCode !== undefined && Object.hasOwn(table.bodyCodes, bodyCode)) {
    details.serviceCode = bodyCode;
  }
  // A feature_disabled body never reaches the output beyond its code: the
  // service's feature name and error text are internal (manifest
  // errorClassification.featureDisabled).
  if (bodyCode !== "feature_disabled" && !table.frozenContracts.includes(contract) && table.serviceMessage.statuses.includes(status) && typeof body?.error === "string") {
    const message = sanitizeServiceMessage(body.error, credential);
    if (message !== undefined) details.serviceMessage = message;
  }
  return failure(code, details);
}

export type { StreamEnd };
