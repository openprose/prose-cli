// Service run lifecycle: `cli run quote|submit|watch|input|cancel`.
// Mirrors cli/rust/crates/prose-runner-core/src/service/runs.rs; the shared
// corpus in cli/conformance/cases/service/runs/ pins both products.
//
// Runs are always live sessions (decision 6): `run submit` journals a session
// UUID before `POST /run?live=1&session=S`, resubmits once with the same
// session when the connection drops before the first event, and treats the
// service's 409 as proof of admission (recovering the run id from `X-Run-Id`
// or a `run_id` body field when present, otherwise RUN_SUBMISSION_AMBIGUOUS).
// Only the duplicate-session 409 is treated that way; any other 409 is
// classified normally. Interrupts, lost streams and the `--wait`
// deadline detach and never cancel (decision 8): they report
// HOSTED_RUN_DETACHED or SERVICE_WATCH_DEADLINE with details.resumeArgv, never
// a retry of the submit. A service-side cancel is the terminal
// HOSTED_RUN_CANCELLED.
import { createHash } from "node:crypto";
import { failure, hostedRunFailed, invocationFailure } from "../errors";
import { humanSafeMultiline, humanSafeScalar, quote as quoteText } from "../output";
import { RunnerFailure } from "../types";
import { readText, validRelativePath } from "./fs";
import { encodeSegment, jsonObject, parseJson, requestFor, type Request, type Response } from "./http";
import type { Context } from "./index";
import { validSession, type JournalEntry } from "./journal";
import { didYouMean, manifest, type Environment, type Json, type JsonObject } from "./manifest";
import { parseOwnAllowed, resolveToRun, validOwner, type ProgramRef } from "./program-ref";
import { argvText, canonicalJson, dollars, eventLine, publicBilling, runErrorText, statusMessage, usdCents, validText } from "./render";
import { explainNotFound } from "./not-found";
import { LATEST_RUN, endedStatus, projectRun, runArgument, runIdArgument, stoppedByOwner } from "./run-records";
import type { SseReader, StreamEnd } from "./sse";

/** A program source (`FILE` or `-`) is at most 1 MiB of UTF-8. */
const MAX_PROGRAM_BYTES = 1 << 20;
/** `--input K=@FILE` and `--inputs-file` are at most 1 MiB each. */
const MAX_INPUT_FILE_BYTES = 1 << 20;
/** The whole JSON submission is at most 8 MiB. */
const MAX_BODY_BYTES = 8 << 20;
/** `run input` text: 1 to 4000 UTF-16 code units (the service's own measure). */
const MAX_INSTRUCTION_UNITS = 4000;
const PRUNE_DAYS = 30;

export async function execute(context: Context): Promise<Json> {
  switch (context.operation.id) {
    case "run.quote": return await quote(context);
    case "run.submit": return await submit(context);
    case "run.watch": return await watch(context);
    case "run.input": return await input(context);
    case "run.cancel": return await cancel(context);
    default: return await context.notImplemented();
  }
}

// ---------------------------------------------------------------------------
// Validation and sanitation helpers (identical rules in the Rust product).

function protocol(reason: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason });
}

/** Adds (or replaces) detail keys on a failure. */
function withDetails(error: RunnerFailure, extra: Record<string, unknown>): RunnerFailure {
  return new RunnerFailure({
    code: error.code, boundary: error.boundary, message: error.message, action: error.action,
    exitCode: error.exitCode, retryable: error.retryable, details: { ...(error.details ?? {}), ...extra },
  });
}

/**
 * A failure whose Action names the one next command: the
 * Action quotes it and `details.suggestedArgv` carries it; never retryable.
 */
function withNextCommand(context: Context, error: RunnerFailure, action: string, words: string[], extra: Record<string, unknown>): RunnerFailure {
  return new RunnerFailure({
    code: error.code, boundary: error.boundary, message: error.message,
    action: action.split("{command}").join(command(context, words.join(" "))),
    exitCode: error.exitCode, retryable: false,
    details: { ...(error.details ?? {}), ...extra, suggestedArgv: context.followUpArgv(words) },
  });
}

/** Whether a failure is the service's 409 with exactly this message (ended-run refusals). */
function conflictSaying(caught: unknown, message: string): caught is RunnerFailure {
  return caught instanceof RunnerFailure && caught.code === "SERVICE_WRITE_CONFLICT"
    && caught.details?.serviceStatus === 409 && caught.details?.serviceMessage === message;
}

/**
 * Run record statuses that mean the run is over. A record is written only
 * when a run ends (completed, error or timeout today); the others are
 * accepted so a service that starts writing them is still read correctly.
 */
const ENDED_STATUSES = ["completed", "error", "failed", "timeout", "cancelled", "canceled"];

function isControl(point: number): boolean {
  return point <= 0x1f || point === 0x7f;
}

/** One-line text: every C0 control and DEL becomes a space; at most `max` code points. */
function cleanLine(value: string, max: number): string {
  return Array.from(value).slice(0, max).map((character) => (isControl(character.codePointAt(0)!) ? " " : character)).join("");
}

/** Multi-line text: like cleanLine but TAB, LF and CR are kept. */
function cleanText(value: string, max: number): string {
  return Array.from(value).slice(0, max).map((character) => {
    const point = character.codePointAt(0)!;
    return isControl(point) && point !== 0x09 && point !== 0x0a && point !== 0x0d ? " " : character;
  }).join("");
}

/** Terminal-safe program text for human mode: also blanks C1 controls. */
function terminalText(value: string): string {
  return Array.from(cleanText(value, Number.MAX_SAFE_INTEGER)).map((character) => {
    const point = character.codePointAt(0)!;
    return point >= 0x80 && point <= 0x9f ? " " : character;
  }).join("");
}

const validToken = (value: string): boolean => /^[a-z0-9][a-z0-9_-]{0,63}$/u.test(value);
const validModel = (value: string): boolean => /^[a-z0-9][a-z0-9.-]{0,63}$/u.test(value);
const validEffort = (value: string): boolean => /^[a-z]{1,16}$/u.test(value);
const validWord = (value: string): boolean => /^[A-Za-z0-9_-]{1,128}$/u.test(value);
export const validRunId = (value: string): boolean => /^run_[A-Za-z0-9_-]{1,128}$/u.test(value);
const validKind = (value: string): boolean => /^[a-z_]{1,32}$/u.test(value);
const validAgent = (value: string): boolean => /^Agent(?: [0-9]{1,3})?$/u.test(value);
const validDollars = (value: string): boolean => /^-?[0-9]+\.[0-9]{2}$/u.test(value);
const validRepoName = (value: string): boolean => /^[A-Za-z0-9._-]{1,100}$/u.test(value) && value !== "." && value !== ".." && !value.endsWith(".git");
function validBranch(value: string): boolean {
  const length = Array.from(value).length;
  return length >= 1 && length <= 255 && !/[\u0000-\u001f\u007f\s]/u.test(value) && !value.includes("..");
}

function isObject(value: Json | undefined): value is JsonObject {
  return value !== null && value !== undefined && typeof value === "object" && !Array.isArray(value);
}
function field(value: Json | undefined, key: string): Json | undefined {
  return isObject(value) ? value[key] : undefined;
}
function str(value: Json | undefined): string | undefined { return typeof value === "string" ? value : undefined; }
function uint(value: Json | undefined): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}
function int(value: Json | undefined): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : undefined;
}

/** --wait: `<digits>(s|m|h)`, at least 1 s and at most the manifest maximum. */
function parseWait(value: string | undefined): number {
  const stream = manifest.transportClasses.stream ?? {};
  const maximum = Number(stream.maxWaitMs ?? 21_600_000);
  if (value === undefined) return Number(stream.defaultWaitMs ?? 1_800_000);
  const error = () => invocationFailure(`--wait ${quoteText(value)} must be a duration like 90s, 10m or 2h, from 1s to ${Math.floor(maximum / 3_600_000)}h`);
  const match = /^([0-9]{1,9})([smh])$/u.exec(value);
  if (match === null) throw error();
  const unit = match[2] === "s" ? 1_000 : match[2] === "m" ? 60_000 : 3_600_000;
  const milliseconds = Number(match[1]) * unit;
  if (milliseconds === 0 || milliseconds > maximum) throw error();
  return milliseconds;
}

/** `parseWait` for this invocation: a bare number (`--wait 5`) is corrected to seconds (`--wait 5s`); mirrors Rust `wait_option`. */
function waitOption(context: Context): number {
  const value = context.option("--wait");
  try { return parseWait(value); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    const fixed = value !== undefined && /^[0-9]+$/u.test(value) ? `${value}s` : undefined;
    let valid = false;
    if (fixed !== undefined) { try { parseWait(fixed); valid = true; } catch { valid = false; } }
    throw fixed !== undefined && valid ? context.corrected(caught, `Give --wait a unit, ${fixed} for seconds: \`{command}\``, context.argvWithOption("--wait", fixed)) : caught;
  }
}

function parseAfter(value: string | undefined): number {
  const text = value ?? "0";
  if (!/^[0-9]{1,10}$/u.test(text)) throw invocationFailure(`--after ${quoteText(text)} must be a sequence number (0 to 9999999999)`);
  return Number(text);
}

function requireRunId(context: Context): string {
  return runIdArgument(context);
}

function sessionOption(context: Context): string | undefined {
  const value = context.option("--session");
  if (value === undefined) return undefined;
  if (validSession(value)) return value;
  throw invocationFailure(`--session ${quoteText(value)} must be a lowercase UUID`);
}

/** A copyable follow-up command line (the shared renderer: environment and machine output mode are kept). */
function command(context: Context, words: string): string {
  return context.command(words);
}

/** The verb that needs a run's live session (its refusal names it). */
type Verb = "watch" | "cancel" | "input";

/**
 * The session for `run input|cancel`: --session, else the journal, else
 * the run record GET /runs/{id} (manifest request `index`)
 * decides: an ended run is `{ ended: status }` (nothing to steer or cancel);
 * a live or unknown run is refused with where its live session is.
 */
async function resolveSession(context: Context, runId: string, verb: Verb, index: number): Promise<{ session: string } | { ended: string }> {
  const given = sessionOption(context);
  if (given !== undefined) return { session: given };
  const entry = context.journal().findRun(runId);
  if (entry !== undefined) return { session: entry.session };
  const request: Request = { ...requestFor(context.operation, index, `/runs/${encodeSegment(runId)}`), class: "control" };
  let record: JsonObject;
  try { record = projectRun(jsonObject(await context.send(request)), true); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") throw noLiveSession(context, runId, undefined, verb);
    if (caught instanceof RunnerFailure) throw withDetails(caught, { runId });
    throw caught;
  }
  const status = str(record.status) ?? "";
  if (ENDED_STATUSES.includes(status)) return { ended: status };
  throw noLiveSession(context, runId, status, verb);
}

// ---------------------------------------------------------------------------
// run quote

/** The public hold of a /run/quote body; the service's price policy reference stays internal (mirrors Rust `quote_fields`). */
function quoteFields(body: JsonObject): { hold: JsonObject } {
  const hold = body.hold;
  const holdUsd = str(field(hold, "hold_usd"));
  if (holdUsd === undefined || !validDollars(holdUsd)) throw protocol("quote hold.hold_usd is missing or malformed");
  const holdCents = usdCents(holdUsd);
  if (holdCents === undefined) throw protocol("quote hold.hold_usd is missing or malformed");
  const ttl = uint(field(hold, "ttl_seconds"));
  if (ttl === undefined) throw protocol("quote hold.ttl_seconds is missing or malformed");
  return { hold: { hold_usd: holdUsd, hold_cents: holdCents, ttl_seconds: ttl } };
}

async function quote(context: Context): Promise<Json> {
  const requested = context.option("--environment");
  if (requested !== undefined && !validToken(requested)) {
    throw invocationFailure(`--environment ${quoteText(requested)} is not an environment id (lowercase letters, digits, _ and -)`);
  }
  const health = jsonObject(await context.send(requestFor(context.operation, 0, "/health")));
  const environments = health.environments;
  const list = field(environments, "available");
  if (!Array.isArray(list) || list.some((item) => typeof item !== "string")) throw protocol("service status environments.available is missing or malformed");
  const available = list as string[];
  let environment: string;
  if (requested !== undefined) {
    if (!available.includes(requested)) {
      throw invocationFailure(`environment ${quoteText(requested)} is not offered by this service (available: ${available.join(", ")}); retry with --environment ${available[0] ?? "builtin"}`);
    }
    environment = requested;
  } else {
    const fallback = str(field(environments, "default"));
    if (fallback === undefined || !validText(fallback, 64)) throw protocol("service status environments.default is missing or malformed");
    environment = fallback;
  }
  const request = requestFor(context.operation, 1, "/run/quote");
  if (requested !== undefined) request.query.push(["environment", requested]);
  const body = jsonObject(await context.send(request));
  const { hold } = quoteFields(body);
  let note = "";
  if (body.note !== undefined && body.note !== null) {
    if (typeof body.note !== "string") throw protocol("quote note is malformed");
    note = cleanLine(body.note, 512);
  }
  let text = `Environment: ${humanSafeScalar(environment)}\n`;
  text += `Hold: $${humanSafeScalar(String(hold.hold_usd))}, set aside from the wallet while a run is live; not its price. The same for every program and model; released within ${String(hold.ttl_seconds)} s when unused\n`;
  text += `Price: known only after a run settles; read it with \`${context.command("run show RUN_ID")}\`\n`;
  if (note.length > 0) text += `Note: ${humanSafeScalar(note)}\n`;
  context.human = text;
  return { environment, hold, holdBasis: HOLD_BASIS, note };
}

/** `run quote` holdBasis: the hold is not a price estimate. */
const HOLD_BASIS = "flat hold, independent of program and model; a run's price is known only after it settles";

// ---------------------------------------------------------------------------
// Event projection (decision 5) and terminal mapping (decision 9).

/** Sanitized `unrecognized` name: ASCII lowercase, other characters `_`. */
function unrecognizedName(value: string): string {
  const name = Array.from(value).slice(0, 64).map((character) => {
    if (/^[a-z0-9_]$/u.test(character)) return character;
    if (/^[A-Z]$/u.test(character)) return character.toLowerCase();
    return "_";
  }).join("");
  return name.length === 0 ? "unknown" : name;
}

function copyString(target: JsonObject, source: Json, key: string, max: number, text: boolean): void {
  const value = str(field(source, key));
  if (value !== undefined) target[key] = text ? cleanText(value, max) : cleanLine(value, max);
}

function projectStatus(data: Json): [string, JsonObject] | undefined {
  const status = str(field(data, "status"));
  if (status === undefined) return undefined;
  const out: JsonObject = {};
  if (status === "history_truncated") {
    copyString(out, data, "message", 1000, false);
    return ["history_truncated", out];
  }
  out.status = cleanLine(status, 32);
  const message = str(field(data, "message"));
  if (message !== undefined) out.message = statusMessage(cleanLine(message, 1000));
  const controls = field(data, "controls");
  const steer = field(controls, "steer");
  const stop = field(controls, "stop");
  if (typeof steer === "boolean" && typeof stop === "boolean") out.controls = { steer, stop };
  return ["status", out];
}

function projectActivity(data: Json): JsonObject | undefined {
  const kind = str(field(data, "kind"));
  if (kind === undefined || !validKind(kind)) return undefined;
  const out: JsonObject = { kind };
  copyString(out, data, "message", 1000, true);
  copyString(out, data, "target", 500, false);
  copyString(out, data, "tool", 128, false);
  // The service's tool call reference stays internal; `input_id` names the
  // caller's own instruction (mirrors Rust).
  const inputId = str(field(data, "input_id"));
  if (inputId !== undefined && validWord(inputId)) out.input_id = inputId;
  const agent = str(field(data, "agent"));
  if (agent !== undefined && validAgent(agent)) out.agent = agent;
  const turn = uint(field(data, "turn"));
  if (turn !== undefined && turn >= 1 && turn <= 512) out.turn = turn;
  const outcome = str(field(data, "outcome"));
  if (outcome === "success" || outcome === "error" || outcome === "cancelled") out.outcome = outcome;
  const details = field(data, "details");
  if (Array.isArray(details)) {
    const projected: JsonObject[] = [];
    for (const detail of details) {
      const label = str(field(detail, "label"));
      const text = str(field(detail, "text"));
      const format = str(field(detail, "format"));
      if (label === undefined || text === undefined || (format !== "text" && format !== "diff")) continue;
      const item: JsonObject = { label: cleanLine(label, 80), text: cleanText(text, 4000), format };
      if (field(detail, "truncated") === true) item.truncated = true;
      projected.push(item);
      if (projected.length === 3) break;
    }
    if (projected.length > 0) out.details = projected;
  }
  return out;
}

function projectBrowser(data: Json): JsonObject | undefined {
  const status = str(field(data, "status"));
  if (status !== "live" && status !== "ended") return undefined;
  const started = str(field(data, "started_at"));
  if (started === undefined) return undefined;
  const out: JsonObject = { status, started_at: cleanLine(started, 40) };
  copyString(out, data, "ended_at", 40, false);
  return out;
}

/** Projects one non-terminal event: [type, data]. */
function projectEvent(kind: string, data: Json): [string, JsonObject] {
  const unrecognized = (): [string, JsonObject] => ["unrecognized", { name: unrecognizedName(kind) }];
  switch (kind) {
    case "status": return projectStatus(data) ?? unrecognized();
    case "agent_activity": {
      const value = projectActivity(data);
      return value === undefined ? unrecognized() : ["agent_activity", value];
    }
    case "text_chunk": {
      const text = str(field(data, "text"));
      return text === undefined ? unrecognized() : ["text_chunk", { text: cleanText(text, 1 << 20) }];
    }
    case "browser_live_view_changed": {
      const value = projectBrowser(data);
      return value === undefined ? unrecognized() : ["browser_live_view_changed", value];
    }
    case "error": {
      const message = str(field(data, "message"));
      return ["error", { message: message === undefined ? "The run reported an error." : runErrorText(cleanLine(message, 4096)) }];
    }
    default: return unrecognized();
  }
}

function validCommitOutput(value: Json | undefined): boolean {
  if (!isObject(value)) return false;
  const keys = ["type", "provider", "repository", "branch", "source_commit_sha", "commit_sha", "changed_files", "url"];
  const sha = (key: string) => { const text = str(value[key]); return text !== undefined && /^[0-9a-f]{40}$/u.test(text); };
  const repository = str(value.repository);
  const branch = str(value.branch);
  const files = value.changed_files;
  const url = str(value.url);
  return Object.keys(value).length === keys.length && keys.every((key) => key in value)
    && value.type === "commit" && value.provider === "github"
    && repository !== undefined && validText(repository, 200)
    && branch !== undefined && validText(branch, 256)
    && sha("source_commit_sha") && sha("commit_sha")
    && Array.isArray(files) && files.length <= 10_000 && files.every((file) => typeof file === "string" && validRelativePath(file))
    && url !== undefined && url.length <= 512 && url.startsWith("https://github.com/") && url.length > "https://github.com/".length && !/\s/u.test(url);
}

/** The closed runTerminal projection of a run_complete payload. */
function projectTerminal(data: JsonObject): JsonObject {
  const runId = str(data.run_id);
  if (runId === undefined || !validRunId(runId)) throw protocol("run_complete has no valid run_id");
  const status = str(data.status);
  if (status === undefined) throw protocol("run_complete has no status");
  let files: string[] = [];
  if (Array.isArray(data.files)) files = data.files.filter((path): path is string => typeof path === "string" && validRelativePath(path));
  else if (isObject(data.files)) files = Object.keys(data.files).filter((path) => validRelativePath(path)).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const out: JsonObject = { run_id: runId, status: cleanLine(status, 32), cancelled: data.cancelled === true, files: files.slice(0, 10_000) };
  // The public environment id (`builtin`, `linux`) only: the service's
  // runtime name, runtime contract and version are not part of the record.
  const environmentId = str(field(data.environment, "id"));
  if (environmentId !== undefined) out.environment = cleanLine(environmentId, 64);
  if (typeof data.response === "string") out.response = cleanText(data.response, 1 << 20);
  else if ("response" in data && data.response === null) out.response = null;
  for (const key of ["price_cents", "environment_price_cents"]) {
    const value = int(data[key]);
    if (value !== undefined) out[key] = value;
  }
  const billing = str(data.billing_status);
  if (billing !== undefined) out.billing_status = publicBilling(cleanLine(billing, 32));
  const inputTokens = uint(field(data.usage, "input_tokens"));
  const outputTokens = uint(field(data.usage, "output_tokens"));
  if (inputTokens !== undefined && outputTokens !== undefined) out.usage = { input_tokens: inputTokens, output_tokens: outputTokens };
  if (validCommitOutput(data.output)) out.output = data.output!;
  const error = str(data.error);
  if (error !== undefined) out.error = runErrorText(cleanLine(error, 4096));
  return out;
}

/**
 * Deadline for --wait. With the real transport a timer aborts the stream
 * promptly (heartbeats keep a quiet stream open); with a fixture the virtual
 * clock is read after each delivered event.
 */
class Deadline {
  private hit = false;
  private known = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private grace: ReturnType<typeof setTimeout> | undefined;
  private abort: (() => void) | undefined;
  private constructor(private readonly startMs: number, private readonly waitMs: number) {}

  /**
   * `known` is whether the run id is already known. When the deadline passes
   * before it is, the stream keeps being read until the run id arrives, for
   * at most the stream connect timeout, so the deadline names the run (exit
   * 21) instead of leaving the submission ambiguous (mirrors Rust).
   */
  static start(context: Context, waitMs: number, known: boolean): Deadline {
    const deadline = new Deadline(context.monotonicMs(), waitMs);
    deadline.known = known;
    if (!context.transport.isFixture) {
      deadline.abort = () => context.transport.cancellation.abort();
      deadline.timer = setTimeout(() => {
        deadline.hit = true;
        if (deadline.known) {
          deadline.abort?.();
          return;
        }
        deadline.grace = setTimeout(() => deadline.abort?.(), graceMs());
        (deadline.grace as { unref?: () => void }).unref?.();
      }, waitMs);
      (deadline.timer as { unref?: () => void }).unref?.();
    }
    return deadline;
  }

  /** The run id is now known: a deadline that already passed ends the wait now. */
  markKnown(): void {
    this.known = true;
    if (this.hit) this.abort?.();
  }

  /** Reads the clock once; true when the deadline has passed. */
  passed(context: Context): boolean {
    if (this.hit) return true;
    if (context.monotonicMs() - this.startMs >= this.waitMs) {
      this.hit = true;
      return true;
    }
    return false;
  }

  get wasHit(): boolean { return this.hit; }

  stop(): void {
    if (this.timer !== undefined) clearTimeout(this.timer);
    if (this.grace !== undefined) clearTimeout(this.grace);
  }
}

/** How long a passed --wait still waits for the run id: the stream connect timeout. */
function graceMs(): number {
  return Number(manifest.transportClasses.stream?.connectTimeoutMs ?? 30_000);
}

/**
 * Closes any response body still open (for example after --detach, the
 * deadline or a 409 recovery). Bun keeps the process alive while a fetch body
 * is readable, so without this `submit --detach` would only exit when the run
 * ends. The controller belongs to this invocation; nothing is sent.
 */
function closeStreams(context: Context): void {
  context.transport.cancellation.abort();
}

/** Stream state shared by `run submit` and `run watch`. */
interface Follow {
  runId: string | undefined;
  session: string;
  /** The last delivered sequence (or --after); events at or below it are never delivered again. */
  after: number;
  delivered: number;
  error: string | undefined;
  textOpen: boolean;
  /** The last status event's status word, if any. */
  status?: string;
}

type Outcome = { kind: "terminal"; data: JsonObject } | { kind: "error-end" } | { kind: "ended"; end: StreamEnd } | { kind: "detached" };

function followDetails(follow: Follow, error: RunnerFailure): RunnerFailure {
  return withDetails(error, { afterSequence: follow.after, session: follow.session, ...(follow.runId === undefined ? {} : { runId: follow.runId }) });
}

/** The copyable command that resumes following the run: exactly `details.resumeArgv`, session included (mirrors Rust `watch_command`). */
function watchCommand(context: Context, follow: Follow): string {
  return follow.runId !== undefined ? argvText(resumeArgv(context, follow.runId, follow)) : command(context, "run list --limit 5");
}

/** The copyable command that cancels the run: exactly `details.cancelArgv` (mirrors Rust `cancel_command`). */
function cancelCommand(context: Context, run: string, follow: Follow): string {
  return argvText(cancelArgv(context, run, follow));
}

/** The argv (after the product name) that resumes following the run. */
function resumeArgv(context: Context, run: string, follow: Follow): string[] {
  return context.followUpArgv(["run", "watch", run, "--after", String(follow.after), "--session", follow.session]);
}

/** The argv (after the product name) that cancels the run. */
function cancelArgv(context: Context, run: string, follow: Follow): string[] {
  return context.followUpArgv(["run", "cancel", run, "--session", follow.session, "--yes"]);
}

/** Adds the structured resume and cancel commands when the run id is known. */
function withFollowUp(context: Context, follow: Follow, error: RunnerFailure): RunnerFailure {
  const detailed = followDetails(follow, error);
  if (follow.runId === undefined) return detailed;
  return withDetails(detailed, { resumable: true, resumeArgv: resumeArgv(context, follow.runId, follow), cancelArgv: cancelArgv(context, follow.runId, follow) });
}

/**
 * The client stopped following a run whose id is known: never
 * retryable, because retrying `run submit` would start a second paid run.
 */
function detachedError(context: Context, follow: Follow, reason: string): RunnerFailure {
  return withFollowUp(context, follow, failure("HOSTED_RUN_DETACHED", { reason }));
}

function interrupted(context: Context, follow: Follow): RunnerFailure {
  if (follow.runId === undefined) return ambiguous(context, follow, "interrupted before the run id was known");
  return detachedError(context, follow, `interrupted; the run continues and was not cancelled. Follow it with \`${watchCommand(context, follow)}\` or cancel it with \`${cancelCommand(context, follow.runId, follow)}\``);
}

function deadlineError(context: Context, follow: Follow): RunnerFailure {
  if (follow.runId === undefined) return ambiguous(context, follow, "the --wait deadline passed before the run id was known");
  return withFollowUp(context, follow, failure("SERVICE_WATCH_DEADLINE", { reason: `the --wait deadline passed; the run continues. Resume with \`${watchCommand(context, follow)}\`` }));
}

function streamLost(context: Context, follow: Follow, what: string): RunnerFailure {
  if (follow.runId === undefined) return ambiguous(context, follow, what);
  return detachedError(context, follow, `${what}; the run was not cancelled. Resume with \`${watchCommand(context, follow)}\` (it reports the outcome even if the run already finished), or read the record with \`${command(context, `run show ${follow.runId}`)}\``);
}

/**
 * The words of an agent's structured final answer (`{status, reason,
 * semantic_diff: {summary, notes}}`), which human output shows instead of the
 * JSON: the reason, the summary when it differs, then each note, one per line.
 * Undefined for any other text, which prints as the run wrote it (mirrors Rust
 * `assistant_words`).
 */
function assistantWords(text: string): string | undefined {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{")) return undefined;
  let value: unknown;
  try { value = JSON.parse(trimmed); } catch { return undefined; }
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (typeof record.status !== "string" || typeof record.reason !== "string" || !Object.hasOwn(record, "semantic_diff")) return undefined;
  const diff = (record.semantic_diff !== null && typeof record.semantic_diff === "object" && !Array.isArray(record.semantic_diff) ? record.semantic_diff : {}) as Record<string, unknown>;
  const summary = typeof diff.summary === "string" ? diff.summary : "";
  const lines: string[] = [];
  for (const line of [record.reason, summary]) if (line.trim().length > 0 && !lines.includes(line)) lines.push(line);
  for (const note of Array.isArray(diff.notes) ? diff.notes : []) if (typeof note === "string" && note.trim().length > 0) lines.push(note);
  if (lines.length === 0) lines.push(record.status);
  return `${lines.join("\n")}\n`;
}

/** Delivers one projected event in the selected output mode. */
function deliver(context: Context, follow: Follow, kind: string, data: JsonObject, sequence: number, at: string | undefined): void {
  if (context.mode === "jsonl") {
    context.emit(eventLine(follow.runId, sequence, at, kind, data));
    return;
  }
  if (context.mode !== "human") return;
  const line = (value: Json | undefined) => humanSafeScalar(typeof value === "string" ? value : "");
  let text: string;
  switch (kind) {
    case "text_chunk": {
      const raw = str(data.text) ?? "";
      const chunk = terminalText(assistantWords(raw) ?? raw);
      if (chunk.length > 0) {
        follow.textOpen = !chunk.endsWith("\n");
        context.out(chunk);
      }
      return;
    }
    // The status word only: the service's status message names its runtime,
    // which is not the person's business (JSONL keeps the event data).
    case "status":
      text = `[status] ${line(data.status)}\n`;
      break;
    case "agent_activity":
      text = `[agent] ${line(data.kind)}${data.message !== undefined ? `: ${agentMessage(data.message)}` : ""}${data.target !== undefined ? ` (${line(data.target)})` : ""}\n`;
      break;
    case "history_truncated":
      text = `[history truncated] ${data.message !== undefined ? line(data.message) : ""}\n`;
      break;
    case "browser_live_view_changed":
      text = `[browser] ${line(data.status)}\n`;
      break;
    case "error":
      text = `[error] ${line(data.message)}\n`;
      break;
    default:
      return;
  }
  context.err(text);
}

/** Records the run id and the last sequence in this session's journal entry, if there is one. */
function record(context: Context, follow: Follow, create?: JournalEntry): void {
  const journal = context.journal();
  let entry: JournalEntry | undefined;
  try { entry = journal.get(follow.session); } catch { entry = undefined; }
  entry ??= create === undefined ? undefined : { ...create };
  if (entry === undefined) return;
  if (entry.runId !== null && entry.runId !== (follow.runId ?? null)) return;
  entry.runId = follow.runId ?? null;
  entry.lastSequence = Math.max(entry.lastSequence, follow.after);
  try { journal.write(entry); } catch { /* the journal is a recovery aid */ }
}

/** Consumes one event stream until a terminal event, the end, a detach, the deadline or an interrupt. */
async function consume(context: Context, reader: SseReader, follow: Follow, detach: boolean, deadline: Deadline): Promise<Outcome> {
  for (;;) {
    let item: Awaited<ReturnType<SseReader["next"]>>;
    try { item = await reader.next(); }
    catch (caught) {
      if (caught instanceof RunnerFailure && caught.code === "CANCELLED") throw deadline.wasHit ? deadlineError(context, follow) : interrupted(context, follow);
      if (caught instanceof RunnerFailure) throw followDetails(follow, caught);
      throw caught;
    }
    if (item.kind === "end") return follow.error !== undefined && item.end === "closed" ? { kind: "error-end" } : { kind: "ended", end: item.end };
    const event = item.event;
    const parsed = parseJson(new TextEncoder().encode(event.data));
    if (parsed === undefined) throw followDetails(follow, protocol("an event's data is not JSON"));
    if (!isObject(parsed)) throw followDetails(follow, protocol("an event's data is not a JSON object"));
    const data = parsed;
    const kind = event.event === "message" ? str(data.type) ?? "message" : event.event;
    const sequence = uint(data.sequence) ?? (event.id !== undefined && /^[0-9]{1,15}$/u.test(event.id) ? Number(event.id) : undefined);
    if (sequence === undefined) throw followDetails(follow, protocol(`a ${unrecognizedName(kind)} event has no sequence`));
    // A replay from 0 (see `watch`) honors the terminal event even at or
    // below --after; every other event there was already delivered.
    if (sequence <= follow.after && kind !== "run_complete") continue;
    if (follow.runId === undefined) {
      const run = str(data.run_id);
      if (run === undefined || !validRunId(run)) throw followDetails(follow, protocol("the first event has no valid run_id"));
      follow.runId = run;
      context.runId = run;
      deadline.markKnown();
      record(context, follow);
    }
    const atValue = str(data.at);
    const at = atValue === undefined ? undefined : cleanLine(atValue, 40);
    if (kind === "run_complete") {
      follow.after = Math.max(follow.after, sequence);
      return { kind: "terminal", data };
    }
    const [projectedKind, projected] = projectEvent(kind, data);
    follow.after = sequence;
    follow.delivered += 1;
    if (projectedKind === "error") follow.error = str(projected.message);
    if (projectedKind === "status" && typeof projected.status === "string") follow.status = projected.status;
    deliver(context, follow, projectedKind, projected, sequence, at);
    if (detach) return { kind: "detached" };
    if (deadline.passed(context)) throw deadlineError(context, follow);
  }
}

/** Maps a terminal run_complete to the stream result or its error. */
function finishTerminal(context: Context, follow: Follow, data: JsonObject): Json {
  let run: JsonObject;
  try { run = projectTerminal(data); }
  catch (caught) { throw caught instanceof RunnerFailure ? followDetails(follow, caught) : caught; }
  if (context.mode === "human" && follow.textOpen) context.out("\n");
  if (run.cancelled === true) {
    throw followDetails(follow, failure("HOSTED_RUN_CANCELLED", { reason: `the run was cancelled on the service; read it with \`${command(context, `run show ${follow.runId ?? "RUN_ID"}`)}\`` }));
  }
  if (run.status !== "completed") {
    const error = typeof run.error === "string" ? `: ${cleanLine(run.error, 480)}` : "";
    throw followDetails(follow, hostedRunFailed({ reason: `the run finished with status ${String(run.status)}${error}` }));
  }
  if (context.mode === "human") {
    const price = typeof run.price_cents === "number" ? ` Price: ${dollars(run.price_cents)}.` : "";
    const files = Array.isArray(run.files) ? run.files.length : 0;
    context.err(`Run ${humanSafeScalar(String(run.run_id))} completed.${price} Files: ${files}.\n`);
    context.human = "";
  }
  return { runId: follow.runId ?? null, run_id: follow.runId ?? null, session: follow.session, detached: false, afterSequence: follow.after, run };
}

/** `run submit --session S` when this machine's journal already maps S to a run: that run (`reused: true`), never a second submission (mirrors Rust `reused_result`). */
function reusedResult(context: Context, follow: Follow, fromJournal: boolean): Json {
  const result = detachedResult(context, follow, "unknown") as JsonObject;
  result.reused = true;
  if (context.mode === "human") {
    const run = follow.runId ?? "";
    const where = fromJournal ? ", submitted earlier from this machine" : "";
    context.human = `Session ${humanSafeScalar(follow.session)} already has run ${humanSafeScalar(run)}${where}; nothing was submitted again.\nFollow it with: ${watchCommand(context, follow)}\nRead it with: ${command(context, `run show ${run}`)}\n`;
  }
  return result;
}

/**
 * A detached (or reused) run: `run` is `{run_id, status}`, the status the
 * stream last reported, else `fallback` (`running` after a detach, `unknown`
 * for a reused session).
 */
function detachedResult(context: Context, follow: Follow, fallback = "running"): Json {
  if (context.mode === "human") {
    if (follow.textOpen) context.out("\n");
    const run = follow.runId ?? "";
    context.human = `Run ${humanSafeScalar(run)} is running (detached after sequence ${follow.after}).\nFollow it with: ${watchCommand(context, follow)}\nCancel it with: ${cancelCommand(context, run, follow)}\n`;
  }
  const run: Json = follow.runId === undefined ? null : { run_id: follow.runId, status: follow.status ?? fallback };
  const result: JsonObject = { runId: follow.runId ?? null, run_id: follow.runId ?? null, session: follow.session, detached: true, afterSequence: follow.after, run };
  // The commands that follow or cancel the detached run, as in the exit-21
  // errors' details.
  if (follow.runId !== undefined) {
    result.resumeArgv = resumeArgv(context, follow.runId, follow);
    result.cancelArgv = cancelArgv(context, follow.runId, follow);
  }
  return result;
}

function errorEnd(follow: Follow): RunnerFailure {
  return followDetails(follow, hostedRunFailed({ reason: `the event stream ended after an error: ${cleanLine(follow.error ?? "", 480)}` }));
}

// ---------------------------------------------------------------------------
// run submit

interface Repository { owner: string; name: string; branch?: string }

const repositoryUrl = (repository: Repository): string => `https://github.com/${repository.owner}/${repository.name}`;
const sameRepository = (left: Repository, right: Repository): boolean =>
  left.owner.toLowerCase() === right.owner.toLowerCase() && left.name.toLowerCase() === right.name.toLowerCase();

function parseRepository(option: string, value: string): Repository {
  const error = () => invocationFailure(`${option} ${quoteText(value)} must be OWNER/NAME or OWNER/NAME@BRANCH (a GitHub repository)`);
  const at = value.indexOf("@");
  const name = at >= 0 ? value.slice(0, at) : value;
  const branch = at >= 0 ? value.slice(at + 1) : undefined;
  const slash = name.indexOf("/");
  if (slash < 0) throw error();
  const owner = name.slice(0, slash);
  const repo = name.slice(slash + 1);
  if (!validOwner(owner) || !validRepoName(repo) || (branch !== undefined && !validBranch(branch))) throw error();
  return branch === undefined ? { owner, name: repo } : { owner, name: repo, branch };
}

function validInputKey(key: string): boolean {
  const length = Array.from(key).length;
  return length >= 1 && length <= 128 && !/[\u0000-\u001f\u007f]/u.test(key);
}

async function parseInputs(context: Context): Promise<Map<string, string>> {
  const inputs = new Map<string, string>();
  const file = context.option("--inputs-file");
  if (file !== undefined) {
    const text = await readText(context.cwd, file, MAX_INPUT_FILE_BYTES, "--inputs-file");
    const value = parseJson(new TextEncoder().encode(text));
    if (value === undefined) throw invocationFailure(`--inputs-file ${quoteText(file)} is not valid JSON`);
    if (!isObject(value)) throw invocationFailure(`--inputs-file ${quoteText(file)} must hold a JSON object of string values`);
    for (const key of Object.keys(value).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))) {
      const item = value[key];
      if (typeof item !== "string") throw invocationFailure(`--inputs-file ${quoteText(file)}: input ${quoteText(key)} must be a string`);
      if (!validInputKey(key)) throw invocationFailure(`--inputs-file ${quoteText(file)}: input name ${quoteText(key)} must be 1 to 128 characters without control characters`);
      inputs.set(key, item);
    }
  }
  const seen = new Set<string>();
  for (const raw of context.optionValues("--input")) {
    const equals = raw.indexOf("=");
    if (equals < 0) throw invocationFailure(`--input ${quoteText(raw)} must be KEY=VALUE or KEY=@FILE`);
    const key = raw.slice(0, equals);
    const value = raw.slice(equals + 1);
    if (!validInputKey(key)) throw invocationFailure(`--input name ${quoteText(key)} must be 1 to 128 characters without control characters`);
    if (seen.has(key)) throw invocationFailure(`--input ${key} was given more than once`);
    seen.add(key);
    if (value.startsWith("@")) {
      const path = value.slice(1);
      if (path.length === 0) throw invocationFailure(`--input ${key}=@ needs a file path after @`);
      inputs.set(key, await readText(context.cwd, path, MAX_INPUT_FILE_BYTES, `--input ${key} file`));
    } else inputs.set(key, value);
  }
  return inputs;
}

/** One input a program declares: its name and whether its description says it is optional. */
interface Parameter { name: string; optional: boolean }

/**
 * The input names a program file declares: the `` `name` `` bullets under its
 * `Parameters` heading, up to the next heading. Undefined when the program
 * has no such heading, so nothing is checked. Only the heading and bullet
 * names are read (mirrors Rust `declared_parameters`).
 */
export function declaredParameters(text: string): Parameter[] | undefined {
  const lines = text.split(/\r?\n/u);
  const start = lines.findIndex((line) => /^#{1,6}[ \t]+Parameters[ \t]*$/u.test(line));
  if (start < 0) return undefined;
  const parameters: Parameter[] = [];
  for (const line of lines.slice(start + 1)) {
    if (/^#{1,6}[ \t]/u.test(line)) break;
    const match = /^[ \t]*[-*][ \t]+`([^`]+)`(.*)$/u.exec(line);
    if (match === null) continue;
    const name = match[1]!;
    if (!parameters.some((known) => known.name === name)) parameters.push({ name, optional: /\boptional\b/iu.test(match[2]!) });
  }
  return parameters;
}

/**
 * Compares the inputs with the program's declared Parameters before any
 * confirmation or request: an input the program does not declare, or a
 * declared input that is neither given nor optional, is INVOCATION_INVALID
 * (mirrors Rust `check_parameters`).
 */
function checkParameters(text: string, inputs: Map<string, string>): void {
  const declared = declaredParameters(text);
  if (declared === undefined) return;
  const names = declared.map((parameter) => parameter.name);
  const listed = names.length === 0 ? "none" : names.join(", ");
  // Sorted, as the Rust product's input map iterates.
  for (const key of [...inputs.keys()].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))) {
    if (names.includes(key)) continue;
    const near = didYouMean(key, names);
    throw invocationFailure(`input ${quoteText(key)} is not a parameter of this program (its parameters: ${listed})${near === undefined ? "" : `; did you mean ${quoteText(near)}?`}`);
  }
  const missing = declared.filter((parameter) => !parameter.optional && !inputs.has(parameter.name)).map((parameter) => parameter.name);
  if (missing.length > 0) {
    throw invocationFailure(`the program needs input${missing.length === 1 ? "" : "s"} ${missing.map((name) => JSON.stringify(name)).join(", ")}; pass ${missing.map((name) => `--input ${name}=VALUE`).join(" ")}`);
  }
}

const looksLikeUrl = (value: string): boolean => /^[A-Za-z][A-Za-z0-9+.-]*:\/\//u.test(value);

interface Submission {
  body: Uint8Array;
  extraQuery: Array<[string, string]>;
  sourceSha256: string | null;
  session: string | undefined;
  waitMs: number;
  environment: string | undefined;
}

async function prepareSubmission(context: Context): Promise<Submission> {
  const file = context.argument("FILE");
  const from = context.option("--from");
  if (file !== undefined && from !== undefined) throw invocationFailure("give either FILE or --from OWNER/SLUG[@REV], not both");
  if (file === undefined && from === undefined) throw invocationFailure("missing program: give FILE, - for standard input, or --from OWNER/SLUG[@REV]");
  if (file !== undefined && looksLikeUrl(file)) {
    throw invocationFailure(`URL sources are not supported (${quoteText(file)}); save the program to a file or run a saved program with --from OWNER/SLUG[@REV]`);
  }
  const reference: ProgramRef | undefined = from === undefined ? undefined : parseOwnAllowed(from, true);
  const session = sessionOption(context);
  const waitMs = waitOption(context);
  const model = context.option("--model");
  if (model !== undefined && !validModel(model)) throw invocationFailure(`--model ${quoteText(model)} is not a model id; list them with \`${command(context, "model list")}\``);
  const effort = context.option("--reasoning-effort");
  if (effort !== undefined && !validEffort(effort)) throw invocationFailure(`--reasoning-effort ${quoteText(effort)} must be lowercase letters (for example low, medium or high)`);
  const environment = context.option("--environment");
  const runtime = context.option("--runtime");
  for (const [option, kind, value] of [["--environment", "an environment", environment], ["--runtime", "a runtime", runtime]] as const) {
    if (value !== undefined && !validToken(value)) {
      throw invocationFailure(`${option} ${quoteText(value)} is not ${kind} id (lowercase letters, digits, _ and -)`);
    }
  }
  const repositories: Repository[] = [];
  for (const value of context.optionValues("--repo")) {
    const repository = parseRepository("--repo", value);
    if (repositories.some((seen) => sameRepository(seen, repository))) throw invocationFailure(`--repo ${quoteText(value)} was given more than once`);
    repositories.push(repository);
  }
  const commitValue = context.option("--commit-output");
  const commit = commitValue === undefined ? undefined : parseRepository("--commit-output", commitValue);
  if (commit !== undefined && !repositories.some((repository) => sameRepository(repository, commit))) {
    throw invocationFailure(`--commit-output ${commit.owner}/${commit.name} must also be given as --repo ${commit.owner}/${commit.name}[@BRANCH]; the service commits only to a context repository`);
  }
  const inputs = await parseInputs(context);
  const body: JsonObject = {};
  let sourceSha256: string | null = null;
  if (file !== undefined) {
    const programText = await readText(context.cwd, file, MAX_PROGRAM_BYTES, "program file");
    if (programText.trim().length === 0) throw invocationFailure(`program file ${quoteText(file)} is empty`);
    sourceSha256 = createHash("sha256").update(programText).digest("hex");
    body.content = programText;
    checkParameters(programText, inputs);
  }
  if (inputs.size > 0) body.inputs = Object.fromEntries(inputs);
  if (model !== undefined) body.model = model;
  if (effort !== undefined) body.reasoning_effort = effort;
  const repositoryValues: JsonObject[] = repositories.map((repository) => (repository.branch === undefined
    ? { url: repositoryUrl(repository) } : { url: repositoryUrl(repository), branch: repository.branch }));
  const extraQuery: Array<[string, string]> = [];
  if (environment !== undefined) extraQuery.push(["environment", environment]);
  if (runtime !== undefined) extraQuery.push(["runtime", runtime]);
  if (repositoryValues.length > 0) {
    extraQuery.push(["repositories", canonicalJson(repositoryValues)]);
    body.repositories = repositoryValues;
  }
  if (commit !== undefined) {
    body.output = commit.branch === undefined
      ? { type: "commit", repository: repositoryUrl(commit) }
      : { type: "commit", repository: repositoryUrl(commit), branch: commit.branch };
    extraQuery.push(["output_repository", repositoryUrl(commit)]);
  }
  // An unknown model fails before confirmation (request 4).
  if (typeof body.model === "string") await context.checkModel(4, body.model);
  // So do an environment or runtime the service does not offer (GET /health).
  await checkOffered(context, environment, runtime);
  if (reference !== undefined) {
    // A bare `--from SLUG` is the caller's own program (manifest request 3).
    // A bare SLUG, `@N` and the caller's own pinned revisions resolve through
    // the caller's revisions (request 3); a latest reference reads the newest
    // rev_id (request 0).
    body.program_ref = await resolveToRun(context, reference, 3, 0, "--from", true);
  }
  const bytes = new TextEncoder().encode(canonicalJson(body));
  if (bytes.length > MAX_BODY_BYTES) {
    throw invocationFailure(`the submission is ${bytes.length} bytes, above the ${MAX_BODY_BYTES}-byte limit; shrink the program or inputs`);
  }
  return { body: bytes, extraQuery, sourceSha256, session, waitMs, environment };
}

/**
 * Refuses an --environment (or --runtime) that /health does not list, before
 * any confirmation, as `run quote` does. Advisory: when /health cannot be
 * read or lists none, the service's own check applies; only an interrupt
 * stops here (mirrors Rust `check_offered`).
 */
async function checkOffered(context: Context, environment: string | undefined, runtime: string | undefined): Promise<void> {
  if (environment === undefined && runtime === undefined) return;
  const index = context.operation.requests.findIndex((request) => request.method === "GET" && request.path === "/health");
  let health: JsonObject;
  try { health = jsonObject(await context.send({ ...requestFor(context.operation, index, "/health"), class: "control" })); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code !== "CANCELLED") return;
    throw caught;
  }
  for (const [option, key, value] of [["--environment", "environments", environment], ["--runtime", "runtimes", runtime]] as const) {
    if (value === undefined) continue;
    const list = field(field(health, key), "available");
    if (!Array.isArray(list)) continue;
    const available = list.filter((item): item is string => typeof item === "string" && validToken(item));
    if (available.length === 0 || available.includes(value)) continue;
    const noun = option === "--environment" ? "environment" : "runtime";
    const error = invocationFailure(`${noun} ${quoteText(value)} is not offered by this service (available: ${available.join(", ")}); retry with ${option} ${available[0]!}`);
    throw context.corrected(error, `Rerun with ${noun === "environment" ? "an" : "a"} ${noun} the service offers: \`{command}\``, context.argvWithOption(option, available[0]!));
  }
}

function submitQuery(session: string, extra: Array<[string, string]>): Array<[string, string]> {
  return [["live", "1"], ["session", session], ...extra];
}

/**
 * The service's duplicate-session refusal text (`POST /run`). The
 * deployed service sends it without a `code` and without naming the run.
 */
const DUPLICATE_SESSION = "This session already has a run.";

/**
 * Whether a 409 is the duplicate-session refusal rather than another conflict
 * such as `model_policy_mismatch` or an admission refusal: it names
 * a run, or it carries the duplicate text and no `code`.
 */
function duplicateSession(response: Response): boolean {
  if (recoveredRunId(response) !== undefined) return true;
  const body = parseJson(response.body);
  if (!isObject(body)) return false;
  const error = str(body.error);
  return body.code === undefined && error !== undefined && error.startsWith(DUPLICATE_SESSION);
}

/** The run id a 409 live-session response names, if any. */
function recoveredRunId(response: Response): string | undefined {
  const header = response.headers.get("x-run-id");
  if (header !== undefined && validRunId(header)) return header;
  const run = str(field(parseJson(response.body), "run_id"));
  return run !== undefined && validRunId(run) ? run : undefined;
}

function ambiguous(context: Context, follow: Follow, reason: string, status?: number): RunnerFailure {
  const resume = sessionResubmitArgv(context, follow.session);
  return failure("RUN_SUBMISSION_AMBIGUOUS", {
    session: follow.session,
    reason: `${reason}; the submission was not repeated. Check \`${command(context, "run list --limit 5")}\` before submitting again, or recover the run with \`${argvText(resume)}\`: the same session never starts a second run; the local run journal keeps the session`,
    ...(status === undefined ? {} : { serviceStatus: status }),
    suggestedArgv: context.followUpArgv(["run", "list", "--limit", "5"]),
    resumeArgv: resume,
  });
}

/**
 * This `run submit` again with `--session S --detach`: the service answers a
 * session that already has a run with that run (`result.reused`), so it
 * recovers an ambiguous submission without starting a second run (mirrors
 * Rust `session_resubmit_argv`).
 */
function sessionResubmitArgv(context: Context, session: string): string[] {
  const argv = context.argvSettingOption("--session", session);
  if (!context.flag("--detach")) {
    const end = argv.includes("--") ? argv.indexOf("--") : argv.length;
    argv.splice(end, 0, "--detach");
  }
  return argv;
}

async function submit(context: Context): Promise<Json> {
  const submission = await prepareSubmission(context);
  if (context.invocation.preview || !context.invocation.yes) {
    const query = submitQuery(submission.session ?? "{session}", submission.extraQuery);
    const planned = context.planned(2, "/run", query, submission.body);
    const quoteRequest: Request = { ...requestFor(context.operation, 1, "/run/quote"), class: "control" };
    if (submission.environment !== undefined) quoteRequest.query.push(["environment", submission.environment]);
    // The quote is advisory: a failed quote never hides the plan.
    try {
      const { hold } = quoteFields(jsonObject(await context.send(quoteRequest)));
      planned.quote = { hold };
    } catch (caught) {
      if (!(caught instanceof RunnerFailure)) throw caught;
    }
    const gate = context.gate(planned);
    if (gate.kind === "preview") return gate.result;
  }
  const sessionGiven = submission.session !== undefined;
  const session = submission.session ?? context.uuidV4();
  const createdAt = context.nowRfc3339();
  const journal = context.journal();
  journal.prune(createdAt, PRUNE_DAYS);
  const entry: JournalEntry = { session, runId: null, createdAt, lastSequence: 0, sourceSha256: submission.sourceSha256 };
  let existing: JournalEntry | undefined;
  try { existing = sessionGiven ? journal.get(session) : undefined; } catch { existing = undefined; }
  // `--session` of a run this machine already submitted: nothing is sent
  // again (a second submission would be refused, or start a second paid run);
  // the result is that run, with the commands that follow it (mirrors Rust).
  if (existing !== undefined && typeof existing.runId === "string") {
    return reusedResult(context, { runId: existing.runId, session, after: existing.lastSequence, delivered: 0, error: undefined, textOpen: false }, true);
  }
  if (existing === undefined) journal.write(entry);
  const request: Request = {
    ...requestFor(context.operation, 2, "/run"),
    query: submitQuery(session, submission.extraQuery),
    headers: [["X-Session-Id", session], ["Accept", "text/event-stream"]],
    body: submission.body,
  };
  const follow: Follow = { runId: undefined, session, after: 0, delivered: 0, error: undefined, textOpen: false };
  const deadline = Deadline.start(context, submission.waitMs, false);
  const detach = context.flag("--detach");
  let resubmitted = false;
  try {
    for (;;) {
      let open: Awaited<ReturnType<Context["openStream"]>>;
      try { open = await context.openStream(request); }
      catch (caught) {
        if (caught instanceof RunnerFailure && caught.code === "CANCELLED") throw deadline.wasHit ? deadlineError(context, follow) : interrupted(context, follow);
        throw caught;
      }
      let outcome: Outcome;
      if (open.kind === "dropped") {
        if (resubmitted) throw ambiguous(context, follow, "the connection failed twice before the service answered");
        resubmitted = true;
        continue;
      } else if (open.kind === "response") {
        const response = open.response;
        const duplicate = response.status === 409 && duplicateSession(response);
        // A resubmission refused for another reason leaves the first attempt's fate unknown.
        if (response.status === 409 && resubmitted && !duplicate) throw ambiguous(context, follow, "the resubmission after a lost connection was refused with a conflict", 409);
        if (duplicate && (resubmitted || sessionGiven)) {
          const run = recoveredRunId(response);
          if (run === undefined) throw ambiguous(context, follow, "the service reports that this session already has a run but did not name it", 409);
          follow.runId = run;
          context.runId = run;
          deadline.markKnown();
          record(context, follow, entry);
          // `--session S` of a session that already has a run: that run
          // (`reused: true`), as when this machine's journal names it.
          if (sessionGiven && !resubmitted) return reusedResult(context, follow, false);
          if (detach) return detachedResult(context, follow);
          throw streamLost(context, follow, `the submission was admitted as ${run} but its event stream was lost`);
        }
        throw context.classify(request, response);
      } else {
        if (follow.runId === undefined) {
          const run = open.headers.get("x-run-id");
          if (run !== undefined && validRunId(run)) {
            follow.runId = run;
            context.runId = run;
            deadline.markKnown();
            record(context, follow, entry);
          }
        }
        try { outcome = await consume(context, open.reader, follow, detach, deadline); }
        finally { record(context, follow); }
      }
      if (outcome.kind === "terminal") return finishTerminal(context, follow, outcome.data);
      if (outcome.kind === "detached") return detachedResult(context, follow);
      if (outcome.kind === "error-end") throw errorEnd(follow);
      if (follow.runId === undefined && follow.delivered === 0) {
        if (resubmitted) throw ambiguous(context, follow, "the event stream ended twice before the first event");
        resubmitted = true;
        continue;
      }
      throw streamLost(context, follow, outcome.end === "closed" ? "the event stream closed before run_complete" : "the event stream was lost before run_complete");
    }
  } finally {
    deadline.stop();
    closeStreams(context);
  }
}

// ---------------------------------------------------------------------------
// run watch

async function watch(context: Context): Promise<Json> {
  if (context.argument("RUN_ID") !== LATEST_RUN) requireRunId(context);
  const after = parseAfter(context.option("--after"));
  const waitMs = waitOption(context);
  sessionOption(context);
  const runId = await runArgument(context);
  const session = sessionOption(context) ?? context.journal().findRun(runId)?.session;
  context.runId = runId;
  if (session === undefined) return await watchRecord(context, runId);
  const request: Request = {
    ...requestFor(context.operation, 0, `/run/${encodeSegment(runId)}/events`),
    query: [["after", String(after)], ["session", session]],
    headers: [["Accept", "text/event-stream"]],
  };
  const follow: Follow = { runId, session, after, delivered: 0, error: undefined, textOpen: false };
  const deadline = Deadline.start(context, waitMs, true);
  // The service keeps a live run's stream open (heartbeats) and closes it
  // cleanly only once the run is terminal. A clean close without a
  // run_complete past --after therefore means the terminal event is at or
  // below --after: replay once from 0 to report the run's real outcome
  // instead of claiming it continues.
  let replayed = false;
  try {
    for (;;) {
      let open: Awaited<ReturnType<Context["openStream"]>>;
      try { open = await context.openStream(request); }
      catch (caught) {
        if (caught instanceof RunnerFailure && caught.code === "CANCELLED") throw deadline.wasHit ? deadlineError(context, follow) : interrupted(context, follow);
        if (caught instanceof RunnerFailure) throw followDetails(follow, caught);
        throw caught;
      }
      if (open.kind === "dropped") throw streamLost(context, follow, "the connection failed before the service answered");
      if (open.kind === "response") throw context.classify(request, open.response);
      let outcome: Outcome;
      try { outcome = await consume(context, open.reader, follow, false, deadline); }
      finally { record(context, follow); }
      if (outcome.kind === "terminal") return finishTerminal(context, follow, outcome.data);
      if (outcome.kind === "error-end") throw errorEnd(follow);
      if (outcome.kind === "ended" && outcome.end === "closed") {
        if (!replayed && after > 0) {
          replayed = true;
          request.query[0] = ["after", "0"];
          continue;
        }
        throw followDetails(follow, protocol(`the event stream closed without run_complete; read the run with \`${command(context, `run show ${runId}`)}\``));
      }
      throw streamLost(context, follow, "the event stream was lost before run_complete");
    }
  } finally {
    deadline.stop();
    closeStreams(context);
  }
}

/**
 * `run watch` without a live session on this machine: the
 * stream cannot be opened, so read the run record instead. An ended run
 * reports its outcome like a replay would (exit 0, 22 or 24) from the record;
 * a run without an ended record is still live elsewhere (or unknown), and the
 * error says where its session is.
 */
async function watchRecord(context: Context, runId: string): Promise<Json> {
  const request: Request = { ...requestFor(context.operation, 1, `/runs/${encodeSegment(runId)}`), class: "control" };
  let record: JsonObject;
  try { record = projectRun(jsonObject(await context.send(request)), true); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") throw runNotFound(context, runId, caught);
    throw caught;
  }
  const status = str(record.status) ?? "";
  if (!ENDED_STATUSES.includes(status)) throw noLiveSession(context, runId, status, "watch");
  const run = recordTerminal(record);
  const files = run.files as string[];
  const source = "from its record; this machine has no live session to replay";
  const details = { runId, files, source: "record" };
  if (run.cancelled === true) {
    throw withDetails(failure("HOSTED_RUN_CANCELLED", { reason: `the run was cancelled on the service (${source}); read it with \`${command(context, `run show ${runId}`)}\`` }), details);
  }
  if (status !== "completed") {
    const error = typeof run.error === "string" ? `: ${cleanLine(run.error, 480)}` : "";
    const read = files.length > 0 ? `; its ${files.length} file${files.length === 1 ? "" : "s"}: \`${command(context, `run download ${runId}`)}\`` : "";
    throw withDetails(hostedRunFailed({ reason: `the run finished with status ${status}${error} (${source})${read}` }), details);
  }
  if (context.mode === "human") {
    const price = typeof run.price_cents === "number" ? ` Price: ${dollars(run.price_cents)}.` : "";
    let text = `Run ${humanSafeScalar(runId)} completed (${source}).${price} Files: ${files.length}.\n`;
    for (const file of files) text += `  ${humanSafeScalar(file)}\n`;
    if (files.length > 0) text += `Read a file with: ${command(context, `run show ${runId} --file ${files[0]!}`)}\n`;
    context.human = text;
  }
  return { runId, run_id: runId, session: null, detached: false, afterSequence: 0, run, source: "record" };
}

/**
 * `run watch` of a run the service has no record of and this machine did not
 * submit: SERVICE_RESOURCE_NOT_FOUND naming the run, unless the typed id is a
 * mistyped copy of one this machine submitted (mirrors Rust `run_not_found`).
 */
function runNotFound(context: Context, runId: string, error: RunnerFailure): RunnerFailure {
  const near = journalNearRun(context, runId);
  if (near !== undefined) return noLiveSession(context, runId, undefined, "watch");
  return explainNotFound(withDetails(error, {
    runId,
    reason: `run ${runId} was not found: no run has this id, or it has not ended and was submitted from another machine (a run's record is written when it ends)`,
  }), context.environment, context.mode, "run", runId, ["run", "list", "--limit", "5"]);
}

/** The closed runTerminal projection of an ended run record (no response text; the record has none). A run its owner stopped is cancelled. */
function recordTerminal(record: JsonObject): JsonObject {
  const status = str(record.status) ?? "";
  const cancelled = status === "cancelled" || status === "canceled" || stoppedByOwner(record.error);
  const out: JsonObject = { run_id: record.run_id!, status, cancelled, files: record.files ?? [] };
  for (const key of ["environment", "price_cents", "environment_price_cents", "billing_status", "usage", "error"]) {
    if (record[key] !== undefined) out[key] = record[key]!;
  }
  return out;
}

/** `run watch|cancel|input` of a run with no live session here and no ended record: the Action names where the session is for that verb. */
const NO_SESSION: Record<Verb, [string, string]> = {
  watch: ["watch it there", "Watch the run from the machine that submitted it or pass --session UUID; once it ends, read its outcome with `{command}`."],
  cancel: ["cancel it there", "Cancel the run from the machine that submitted it (its run journal holds the live session) or pass that session with --session UUID; nothing was cancelled. Once it ends, read its outcome with `{command}`."],
  input: ["send the instruction there", "Send the instruction from the machine that submitted the run (its run journal holds the live session) or pass that session with --session UUID; nothing was sent. Once it ends, read its outcome with `{command}`."],
};

/** The one run id in this machine's run journal that `typed` truncates (a prefix) or misspells (at most two edits), if exactly one does (mirrors Rust `journal_near_run`). */
function journalNearRun(context: Context, typed: string): string | undefined {
  const found = [...new Set(context.journal().entries().map((entry) => entry.runId).filter((run): run is string => typeof run === "string"))]
    .filter((run) => run !== typed && ((run.startsWith(typed) && typed.length > "run_".length) || didYouMean(typed, [run]) !== undefined));
  return found.length === 1 ? found[0] : undefined;
}

function noLiveSession(context: Context, runId: string, status: string | undefined, verb: Verb): RunnerFailure {
  // A run with no record that this machine did not submit, while it did submit
  // a run whose id the typed one truncates or misspells: that run.
  if (status === undefined) {
    const near = journalNearRun(context, runId);
    if (near !== undefined) {
      const error = withDetails(invocationFailure(`${runId} has no record and this machine did not submit it; this machine submitted ${near}, which the id looks like a truncated or mistyped copy of`), { runId });
      return context.corrected(error, `Use the run id ${near}: \`{command}\``, context.argvWithArgument(runId, near));
    }
  }
  const [there, action] = NO_SESSION[verb];
  const state = status === undefined
    ? `has no ended record yet: it is still running (a run's record is written when it ends) or it does not exist`
    : `is ${cleanLine(status, 32)}`;
  const reason = `${runId} ${state}, and this machine's run journal has no live session for it. A run's live session is kept in the run journal of the machine that submitted it: ${there}, or pass --session UUID; once the run ends, \`${command(context, `run show ${runId}`)}\` reads its outcome`;
  return withNextCommand(context, invocationFailure(reason), action, ["run", "show", runId], { runId });
}

// ---------------------------------------------------------------------------
// run input

/** The service's refusal to steer a run that has ended (or is stopping). */
const INPUT_ENDED = "This run is no longer accepting instructions.";
/** The service's refusal to cancel a run that has ended. */
const CANCEL_ENDED = "This run has already ended.";

async function input(context: Context): Promise<Json> {
  const runId = requireRunId(context);
  const text = context.argument("TEXT") ?? "";
  if (text.trim().length === 0 || text.length > MAX_INSTRUCTION_UNITS) {
    throw invocationFailure(`instruction text must be 1 to ${MAX_INSTRUCTION_UNITS} characters and not blank`);
  }
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(text)) {
    throw invocationFailure("instruction text must not contain control characters other than tab and line breaks");
  }
  const givenId = context.option("--id");
  if (givenId !== undefined && !validSession(givenId)) throw invocationFailure(`--id ${quoteText(givenId)} must be a lowercase UUID`);
  const resolved = await resolveSession(context, runId, "input", 1);
  if ("ended" in resolved) {
    // An ended run read from its record: the same
    // non-retryable conflict as the service's 409, before any write.
    throw withNextCommand(context, failure("SERVICE_WRITE_CONFLICT", {
      reason: `the run has ended (status ${cleanLine(resolved.ended, 32)}) and no longer accepts instructions; nothing was sent`,
    }), "Do not retry; the run takes no more instructions. Read its outcome with `{command}`.", ["run", "show", runId], { runId, source: "record" });
  }
  const session = resolved.session;
  const id = givenId ?? context.uuidV4();
  const path = `/run/${encodeSegment(runId)}/input`;
  const query: Array<[string, string]> = [["session", session]];
  const body = new TextEncoder().encode(canonicalJson({ id, text }));
  const gate = context.gate(context.planned(0, path, query, body));
  if (gate.kind === "preview") return gate.result;
  const request: Request = { ...requestFor(context.operation, 0, path), query, body };
  let response: JsonObject;
  try { response = jsonObject(await context.send(request)); }
  catch (caught) {
    // Steering an ended run can never succeed; retrying is pointless.
    if (conflictSaying(caught, INPUT_ENDED)) {
      throw withNextCommand(context, withDetails(caught, { reason: "the run has ended (or is being cancelled) and no longer accepts instructions" }),
        "Do not retry; the run takes no more instructions. Read its outcome with `{command}`.", ["run", "show", runId], { runId });
    }
    throw caught;
  }
  if (response.status !== "queued" || response.id !== id) throw protocol("the instruction response is not {id, status: queued}");
  if (context.mode === "human") context.human = `Instruction ${id} queued for ${humanSafeScalar(runId)}.\n`;
  return { runId, id, status: "queued" };
}

// ---------------------------------------------------------------------------
// run cancel

function projectBalance(body: JsonObject): JsonObject {
  const balance = body.balance;
  if (!isObject(balance)) throw protocol("wallet balance is not a JSON object");
  const out: JsonObject = {};
  for (const prefix of ["available", "posted", "reserved"]) {
    const cents = int(balance[`${prefix}_cents`]);
    if (cents === undefined) throw protocol(`wallet balance ${prefix}_cents is missing`);
    const dollars = str(balance[`${prefix}_dollars`]);
    if (dollars === undefined || !validDollars(dollars)) throw protocol(`wallet balance ${prefix}_dollars is missing`);
    out[`${prefix}_cents`] = cents;
    out[`${prefix}_dollars`] = dollars;
  }
  return out;
}

async function cancel(context: Context): Promise<Json> {
  const runId = requireRunId(context);
  const resolved = await resolveSession(context, runId, "cancel", 2);
  // An ended run read from its record: already done.
  if ("ended" in resolved) return endedResult(context, runId, resolved.ended);
  const session = resolved.session;
  const path = `/run/${encodeSegment(runId)}/cancel`;
  const query: Array<[string, string]> = [["session", session]];
  const gate = context.gate(context.planned(0, path, query));
  if (gate.kind === "preview") return gate.result;
  let response: JsonObject;
  try { response = jsonObject(await context.send({ ...requestFor(context.operation, 0, path), query })); }
  catch (caught) {
    // Cancelling an ended run is already done: report it as success.
    if (conflictSaying(caught, CANCEL_ENDED)) return await alreadyEnded(context, runId);
    throw caught;
  }
  const rawStatus = str(response.status);
  if (rawStatus === undefined) throw protocol("the cancel response has no status");
  const status = cleanLine(rawStatus, 32);
  let balance: JsonObject;
  try { balance = projectBalance(jsonObject(await context.send(requestFor(context.operation, 1, "/wallet/balance")))); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    throw withDetails(caught, {
      runId,
      reason: `the cancel was accepted (status ${status}) but the wallet balance could not be read; check it with \`${command(context, "wallet balance")}\``,
    });
  }
  if (context.mode === "human") {
    context.human = `Cancel requested for ${humanSafeScalar(runId)} (status: ${humanSafeScalar(status)}).\nWallet: available $${String(balance.available_dollars)}, reserved $${String(balance.reserved_dollars)}, balance $${String(balance.posted_dollars)}.\nA reserved hold stays until the service settles the run. Watch the run finish with: ${command(context, `run watch ${runId}`)}\n`;
  }
  return { runId, status, balance };
}

/**
 * `run cancel` of a run that has already ended: exit 0 with
 * `{status: "already_ended", runStatus}`. runStatus is the run record's status,
 * or null while the record is not written yet (404). The wallet is not read.
 */
async function alreadyEnded(context: Context, runId: string): Promise<Json> {
  let runStatus: string | null;
  try {
    const request: Request = { ...requestFor(context.operation, 2, `/runs/${encodeSegment(runId)}`), class: "control" };
    runStatus = endedStatus(projectRun(jsonObject(await context.send(request)), true)) ?? null;
  } catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    if (caught.code !== "SERVICE_RESOURCE_NOT_FOUND") {
      throw withDetails(caught, { runId, reason: `the run has already ended (nothing was cancelled) but its record could not be read; read it with \`${command(context, `run show ${runId}`)}\`` });
    }
    runStatus = null;
  }
  return endedResult(context, runId, runStatus);
}

/** The already_ended result of `run cancel` (exit 0) for a run record status, or null while the record is not written yet. */
function endedResult(context: Context, runId: string, runStatus: string | null): Json {
  if (context.mode === "human") {
    const state = runStatus === null ? "its record is not written yet" : `status: ${humanSafeScalar(runStatus)}`;
    context.human = `Run ${humanSafeScalar(runId)} has already ended (${state}); nothing was cancelled.\nRead it with: ${command(context, `run show ${runId}`)}\n`;
  }
  return { runId, status: "already_ended", runStatus };
}

export const __test = { cleanLine, cleanText, terminalText, unrecognizedName, parseWait, parseAfter, projectEvent, projectTerminal, validRunId };

/** A human `[agent]` message: line breaks stay line breaks, each continuation line indented under the tag (mirrors Rust `agent_message`). */
function agentMessage(message: Json | undefined): string {
  const text = typeof message === "string" ? message : "";
  return humanSafeMultiline(text.replaceAll("\r\n", "\n").replace(/\n+$/u, "")).replaceAll("\n", "\n    ");
}
