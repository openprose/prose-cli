// Service run records: `cli run list|show|download|share`.
//
// Projections keep the service's field names verbatim, drop unknown fields and
// never output `file_urls` (signed `tok=` URLs) or `customer_id`. A known field
// with the wrong shape is SERVICE_PROTOCOL_INVALID, so service drift is loud.
// `files` is always an array of relative paths. Mirrors
// cli/rust/crates/prose-runner-core/src/service/run_records.rs; behavior is
// pinned by cli/conformance/cases/service/run-records/.
import { closeSync, fsyncSync, linkSync, lstatSync, renameSync, statSync, unlinkSync, writeSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { TEST_SEAMS_ENABLED } from "../build";
import { failure, invocationFailure } from "../errors";
import { RunnerFailure } from "../types";
import { FreshDirectory, createNewFile, validRelativePath } from "./fs";
import { TRANSPORT_REASON, encodeSegment, jsonObject, maxResponseBytes, requestFor, type Downloaded, type Request, type Sink } from "./http";
import type { Context } from "./index";
import { manifest, type Environment, type Json, type JsonObject } from "./manifest";
import { explainNotFound } from "./not-found";
import { validOwner, validRev, validSlug } from "./program-ref";
import { billingLabel, canonicalJson, dollars, nextLine, publicBilling, redact, runErrorText } from "./render";
import { humanSafeScalar, quote } from "../output";

// Compile-time only (scripts/image-bundle.ts); the inline guard below lets a
// release build fold this test seam away.
declare const OPENPROSE_TEST_SEAMS: boolean | undefined;

/** The completion marker `run download` writes last. */
export const DOWNLOAD_MARKER = ".prose-run-manifest.json";
const DOWNLOAD_MARKER_PARTIAL = ".prose-run-manifest.json.partial";

export async function execute(context: Context): Promise<Json> {
  switch (context.operation.id) {
    case "run.list": return await list(context);
    case "run.show": return await show(context);
    case "run.download": return await download(context);
    case "run.share": return await share(context);
    default: return await context.notImplemented();
  }
}

const protocol = (reason: string): RunnerFailure => failure("SERVICE_PROTOCOL_INVALID", { reason });
const tooLarge = (reason: string): RunnerFailure => failure("SERVICE_RESPONSE_TOO_LARGE", { reason });
/** A specific reason is kept; the generic transport reason gives way to the download's own. */
const hasReason = (error: RunnerFailure): boolean => error.details?.reason !== undefined && error.details.reason !== TRANSPORT_REASON;
const withReason = (error: RunnerFailure, reason: string): RunnerFailure => failure(error.code, { ...(error.details ?? {}), reason });

/** `^run_[A-Za-z0-9_-]{1,128}$`. */
export const validRunId = (value: string): boolean => /^run_[A-Za-z0-9_-]{1,128}$/u.test(value);

/** A run id a person typed: `run_` and 1 to 128 lowercase hex digits, the shape the service issues (mirrors Rust `valid_run_id_argument`). */
export const validRunIdArgument = (value: string): boolean => /^run_[0-9a-f]{1,128}$/u.test(value);

/** Hex digits in a full run id; a shorter one was cut off when copied. */
const FULL_RUN_ID_DIGITS = 64;

/**
 * A run id with fewer than 64 hex digits: INVOCATION_INVALID before any
 * request. The one run in this machine's journal it is a prefix of is the
 * correction; otherwise the recent runs listing (mirrors Rust `truncated_run_id`).
 */
function truncatedRunId(context: Context, value: string): RunnerFailure {
  const error = invocationFailure(`run id ${quote(value)} looks truncated; a full run id has ${FULL_RUN_ID_DIGITS} hex digits`);
  const matches = [...new Set(context.journal().entries().map((entry) => entry.runId).filter((run): run is string => typeof run === "string" && run !== value && run.startsWith(value)))];
  if (matches.length === 1) return context.corrected(error, `Use the full run id ${matches[0]!}: \`{command}\``, context.argvWithArgument(value, matches[0]!));
  const argv = context.followUpArgv(["run", "list", "--limit", "5"]);
  return context.corrected(error, "List recent runs with `{command}` and copy the full run id", argv);
}

/** The RUN_ID argument, or INVOCATION_INVALID before any request (identical in both ports). */
export function runIdArgument(context: Context): string {
  const value = context.argument("RUN_ID") ?? "";
  if (validRunIdArgument(value)) {
    if (value.length - "run_".length < FULL_RUN_ID_DIGITS) throw truncatedRunId(context, value);
    return value;
  }
  const error = invocationFailure(`run id ${quote(value)} is invalid; a run id is run_ followed by lowercase hex digits; list runs with \`${context.command("run list")}\``);
  // `4f1c...` without its prefix, or `RUN_4F1C...` in capitals.
  const lower = value.replace(/[A-Z]/gu, (letter) => letter.toLowerCase());
  const fixed = [lower, `run_${lower}`].find((candidate) => validRunIdArgument(candidate));
  throw fixed === undefined ? error : context.corrected(error, `Use the run id ${fixed}: \`{command}\``, context.argvWithArgument(value, fixed));
}

/** The word that selects the caller's newest run in place of RUN_ID. */
export const LATEST_RUN = "latest";

/**
 * The RUN_ID argument with the `latest` selector: the newest run of the
 * caller's account (`GET /runs?limit=1`, the operation's GET /runs request);
 * any other value is `runIdArgument`. No runs is SERVICE_RESOURCE_NOT_FOUND
 * (mirrors Rust `run_argument`).
 */
export async function runArgument(context: Context): Promise<string> {
  if (context.argument("RUN_ID") !== LATEST_RUN) return runIdArgument(context);
  const index = context.operation.requests.findIndex((request) => request.method === "GET" && request.path === "/runs");
  const request = { ...requestFor(context.operation, index, "/runs"), class: "control" as const };
  request.query = [["limit", "1"]];
  const rows = jsonObject(await context.send(request)).runs;
  if (!Array.isArray(rows) || rows.length > 200) throw protocol("the run list is missing or has more than 200 runs");
  if (rows.length === 0) {
    throw explainNotFound(failure("SERVICE_RESOURCE_NOT_FOUND", { reason: "run latest was not found: this account has no runs yet" }),
      context.environment, context.mode, "run", LATEST_RUN, ["run", "list", "--limit", "5"]);
  }
  return projectRun(rows[0]!, false).run_id as string;
}

const runPath = (runId: string): string => `/runs/${encodeSegment(runId)}`;
export const filePath = (runId: string, relative: string): string => `/runs/${encodeSegment(runId)}/files/${relative.split("/").map(encodeSegment).join("/")}`;

const codePoints = (value: string): string[] => [...value];
/** At most `max` code points without C0 controls or DEL (may be empty). */
const plain = (value: string, max: number): boolean => codePoints(value).length <= max && !/[\u0000-\u001f\u007f]/u.test(value);
const validTimestamp = (value: string): boolean => value.length <= 40 && /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$/u.test(value);
const validModel = (value: string): boolean => /^[a-z0-9][a-z0-9.-]{0,63}$/u.test(value);
function validProgramRef(value: string): boolean {
  const at = value.indexOf("@");
  if (at < 0) return false;
  const name = value.slice(0, at);
  const slash = name.indexOf("/");
  return slash >= 0 && validOwner(name.slice(0, slash)) && validSlug(name.slice(slash + 1)) && validRev(value.slice(at + 1));
}
const isInteger = (value: Json | undefined): value is number => typeof value === "number" && Number.isSafeInteger(value);
const isObject = (value: Json | undefined): value is JsonObject => value !== null && typeof value === "object" && !Array.isArray(value);
const str = (value: Json | undefined): string | undefined => typeof value === "string" ? value : undefined;

type Accept = (value: Json) => Json | undefined;

function optional(source: JsonObject, target: JsonObject, key: string, accept: Accept): void {
  const value = source[key];
  if (value === undefined || value === null) return;
  const projected = accept(value);
  if (projected === undefined) throw protocol(`the run field ${key} is malformed`);
  target[key] = projected;
}

function requiredString(source: JsonObject, target: JsonObject, key: string, accept: (value: string) => boolean): void {
  const value = str(source[key]);
  if (value === undefined || !accept(value)) throw protocol(`the run field ${key} is missing or malformed`);
  target[key] = value;
}

/** The run's environment as its public id (`builtin`, `linux`); the service's version and runtime contract are not part of the record. */
function environmentProvenance(value: Json): Json | undefined {
  if (!isObject(value)) return undefined;
  const id = str(value.id);
  return id === undefined || !plain(id, 64) ? undefined : id;
}

function repository(value: Json): Json | undefined {
  if (!isObject(value) || value.provider !== "github") return undefined;
  const owner = str(value.owner);
  const name = str(value.name);
  if (owner === undefined || !plain(owner, 100) || name === undefined || !plain(name, 100)) return undefined;
  const projected: JsonObject = { provider: "github", owner, name };
  if (value.ref !== undefined && value.ref !== null) {
    const reference = str(value.ref);
    if (reference === undefined || !plain(reference, 256)) return undefined;
    projected.ref = reference;
  }
  if (value.writable !== undefined && value.writable !== null) {
    if (typeof value.writable !== "boolean") return undefined;
    projected.writable = value.writable;
  }
  return projected;
}

function repositories(value: Json): Json | undefined {
  if (!Array.isArray(value) || value.length > 16) return undefined;
  const items = value.map(repository);
  return items.every((item) => item !== undefined) ? items as Json[] : undefined;
}

function usage(value: Json): Json | undefined {
  if (!isObject(value)) return undefined;
  const input = value.input_tokens;
  const output = value.output_tokens;
  if (!isInteger(input) || input < 0 || !isInteger(output) || output < 0) return undefined;
  return { input_tokens: input, output_tokens: output };
}

function downloadClass(key: string): number { return Number(manifest.transportClasses.download?.[key] ?? 0); }
const maxFiles = (): number => downloadClass("maxFiles") || 10_000;

function paths(value: Json, key: string): string[] {
  if (!Array.isArray(value)) throw protocol(`the run field ${key} is malformed`);
  if (value.length > maxFiles()) throw tooLarge(`the run lists more than ${maxFiles()} ${key}`);
  return value.map((item) => {
    if (typeof item === "string" && validRelativePath(item)) return item;
    throw protocol(`the run manifest lists an unsafe path in ${key}`);
  });
}

/** The run's `error` text: controls become spaces, keys redacted, at most 4096 code points; blank dropped. */
function errorText(value: string): string | undefined {
  const spaced = value.replace(/[\u0000-\u001f\u007f]/gu, " ");
  const cut = codePoints(redact(spaced, undefined)).slice(0, 4096).join("");
  return /^ *$/u.test(cut) ? undefined : cut;
}

/** Projects a run summary (`isManifest` false) or a run manifest. */
export function projectRun(source: Json, isManifest: boolean): JsonObject {
  if (!isObject(source)) throw protocol("a run record is not an object");
  const target: JsonObject = {};
  requiredString(source, target, "run_id", validRunId);
  requiredString(source, target, "created_at", validTimestamp);
  requiredString(source, target, "status", (value) => plain(value, 32));
  requiredString(source, target, "model", validModel);
  optional(source, target, "environment", environmentProvenance);
  for (const key of ["price_cents", "environment_price_cents"]) optional(source, target, key, (value) => isInteger(value) ? value : undefined);
  optional(source, target, "billing_status", (value) => typeof value === "string" && plain(value, 32) ? publicBilling(value) : undefined);
  optional(source, target, "program_ref", (value) => typeof value === "string" && validProgramRef(value) ? value : undefined);
  optional(source, target, "repositories", repositories);
  if (isManifest) {
    if (source.files === undefined) throw protocol("the run field files is missing or malformed");
    target.files = paths(source.files, "files");
    if (typeof source.has_patch !== "boolean") throw protocol("the run field has_patch is missing or malformed");
    target.has_patch = source.has_patch;
    optional(source, target, "usage", usage);
    if (source.session_files !== undefined && source.session_files !== null) target.session_files = paths(source.session_files, "session_files");
    const error = source.error;
    if (typeof error === "string") {
      const text = errorText(error);
      if (text !== undefined) target.error = runErrorText(text);
    } else if (error !== undefined && error !== null) throw protocol("the run field error is malformed");
  }
  return target;
}

/**
 * Download limits [per file, per run]. Test-seam builds may lower them with
 * PROSE_TEST_SERVICE_DOWNLOAD_LIMITS=<file bytes>,<run bytes>; ordinary builds ignore it.
 */
function downloadLimits(context: Context): [number, number] {
  const limits: [number, number] = [downloadClass("maxFileBytes"), downloadClass("maxRunBytes")];
  const value = (typeof OPENPROSE_TEST_SEAMS === "boolean" && !OPENPROSE_TEST_SEAMS) ? undefined : TEST_SEAMS_ENABLED ? context.deps.env.PROSE_TEST_SERVICE_DOWNLOAD_LIMITS : undefined;
  const match = value === undefined ? null : /^([0-9]+),([0-9]+)$/u.exec(value);
  if (match !== null) return [Math.min(limits[0], Number(match[1])), Math.min(limits[1], Number(match[2]))];
  return limits;
}

async function fetchManifest(context: Context, runId: string): Promise<JsonObject> {
  const request: Request = { ...requestFor(context.operation, 0, runPath(runId)), class: "control" };
  return projectRun(jsonObject(await context.send(request)), true);
}

const manifestFiles = (value: JsonObject): string[] => (value.files as string[] | undefined) ?? [];

function exists(path: string): boolean {
  try { lstatSync(path); return true; } catch { return false; }
}

function isDirectory(path: string): boolean {
  try { return statSync(path).isDirectory(); } catch { return false; }
}

/**
 * Refuses an existing destination or a missing parent before any request, with
 * the fresh-writer messages. `defaulted` marks `run download`'s default
 * ./RUN_ID directory, whose refusal names --output-dir.
 */
function preflightDestination(cwd: string, value: string, directory: boolean, defaulted = false): void {
  const path = isAbsolute(value) ? value : resolve(cwd, value);
  const quoted = JSON.stringify(value);
  if (exists(path) && defaulted) throw invocationFailure(`output directory ${quoted} (the default, ./RUN_ID) already exists; pass --output-dir DIR to choose a new directory`);
  if (exists(path)) throw invocationFailure(directory ? `output directory ${quoted} already exists; choose a new directory` : `output file ${quoted} already exists; choose a new path`);
  if (!isDirectory(dirname(path))) throw invocationFailure(directory ? `the parent of output directory ${quoted} does not exist` : `the directory for output file ${quoted} does not exist`);
}

// ---------------------------------------------------------------- run list

export function parseLimit(value: string): number | undefined {
  if (!/^[0-9]{1,3}$/u.test(value)) return undefined;
  const limit = Number(value);
  return limit >= 1 && limit <= 200 ? limit : undefined;
}

async function list(context: Context): Promise<Json> {
  const rawLimit = context.option("--limit") ?? "20";
  const limit = parseLimit(rawLimit);
  if (limit === undefined) throw context.limitError(invocationFailure(`--limit ${quote(rawLimit)} is invalid; use a whole number from 1 to 200`), rawLimit, 200);
  const before = context.option("--before");
  if (before !== undefined && (before === "" || !plain(before, 512))) throw invocationFailure("--before must be the nextBefore value of a previous `cli run list` result");
  const request = requestFor(context.operation, 0, "/runs");
  request.query.push(["limit", String(limit)]);
  if (before !== undefined) request.query.push(["before", before]);
  const body = jsonObject(await context.send(request));
  const rows = body.runs;
  if (!Array.isArray(rows) || rows.length > 200) throw protocol("the run list is missing or has more than 200 runs");
  const runs = rows.map((run) => markCancelled(projectRun(run, false), run));
  const cursor = body.next_before;
  if (cursor === undefined || cursor === null) context.nextBefore = undefined;
  else if (typeof cursor === "string" && cursor !== "" && plain(cursor, 512)) context.nextBefore = cursor;
  else throw protocol("the run list cursor is malformed");
  context.human = runs.length === 0 ? "No runs.\n" : runs.map((run) => `${run.run_id as string}  ${run.cancelled === true ? "cancelled" : run.status as string}  ${run.model as string}  ${run.created_at as string}\n`).join("");
  return { runs };
}

// ---------------------------------------------------------------- run show

/** UTF-8 text the `text` schema admits (tab, LF and CR are the only controls). */
function textContent(bytes: Uint8Array): string | undefined {
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); } catch { return undefined; }
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(text) || codePoints(text).length > 1_048_576) return undefined;
  return text;
}

function contentType(headers: Map<string, string>): string | undefined {
  const value = headers.get("content-type");
  return value !== undefined && value !== "" && plain(value, 128) ? value : undefined;
}

function fileSink(descriptor: number): Sink {
  return {
    write(bytes: Uint8Array): void {
      let offset = 0;
      try {
        while (offset < bytes.length) offset += writeSync(descriptor, bytes, offset, bytes.length - offset);
      } catch { throw invocationFailure("cannot write the downloaded file"); }
    },
  };
}

/** A 404 on one of the run's files names the file, not the run. */
function fileNotFound(context: Context, runId: string, path: string, error: unknown): unknown {
  return error instanceof RunnerFailure ? explainNotFound(error, context.environment, context.mode, "file", `${runId}/${path}`, ["run", "show", runId]) : error;
}

function annotateTooLarge(error: unknown, reason: () => string): unknown {
  if (error instanceof RunnerFailure && error.code === "SERVICE_RESPONSE_TOO_LARGE" && !hasReason(error)) return withReason(error, reason());
  return error;
}

/** Run statuses after which nothing more happens (any other status can still be watched). */
const ENDED_STATUSES = ["completed", "failed", "error", "cancelled", "canceled"];

/** The service's error text for a run its owner stopped. */
const USER_STOPPED = "Stopped by the user.";

export const stoppedByOwner = (error: Json | undefined): boolean => typeof error === "string" && error.trim() === USER_STOPPED;

/**
 * Adds `cancelled: true` to a projected run whose owner stopped it (the
 * service records it as `error` with its stop reason) or whose status is
 * cancelled, so `run list` and `run show` agree with `run watch`'s exit 24.
 * `source` is the service's record (a run list row carries the reason
 * without the CLI projecting it). Mirrors Rust `mark_cancelled`.
 */
export function markCancelled(run: JsonObject, source: Json | undefined): JsonObject {
  const status = typeof run.status === "string" ? run.status : "";
  const error = isObject(source) ? source.error : run.error;
  if (status === "cancelled" || status === "canceled" || stoppedByOwner(error)) run.cancelled = true;
  return run;
}

/**
 * A run record's status word for `run cancel` of an ended run: `cancelled`
 * for a run its owner stopped (the service records it as `error` with its
 * stop reason), otherwise the record's status (mirrors Rust `ended_status`).
 */
export function endedStatus(record: JsonObject): string | undefined {
  const status = typeof record.status === "string" ? record.status : undefined;
  if (status === undefined) return undefined;
  return stoppedByOwner(record.error) ? "cancelled" : status;
}

/**
 * The human `run show` status. The status word is the service's in every
 * command (`run list` and `service triage` list records without the stop
 * reason); `run show` has the reason and adds it: `error (stopped by you)`.
 * Mirrors Rust `shown_status`; JSON keeps the record.
 */
function shownStatus(status: string, error: Json | undefined): string {
  return stoppedByOwner(error) ? "cancelled (stopped by you)" : status;
}

/** The human billing state (`billingLabel`; mirrors Rust `shown_billing`). */
function shownBilling(billing: string, priceCents: Json | undefined): string {
  return billingLabel(billing, priceCents);
}

/**
 * Readable `run show` output: one `label: value` line per
 * field, prices in dollars, nested records flattened, files one per line, then
 * copyable `Next:` commands in this environment. Mirrors Rust `human_run`.
 */

function humanRun(run: JsonObject, environment: Environment): string {
  const textOf = (value: Json | undefined): string => (typeof value === "string" ? humanSafeScalar(value) : "");
  const id = typeof run.run_id === "string" ? run.run_id : "";
  let text = `Run ${humanSafeScalar(id)}\n`;
  const line = (label: string, value: string): void => { if (value !== "") text += `  ${label}: ${value}\n`; };
  const integer = (value: Json | undefined): number | undefined => (typeof value === "number" && Number.isSafeInteger(value) ? value : undefined);
  const shown = shownStatus(typeof run.status === "string" ? run.status : "", run.error);
  line("status", humanSafeScalar(shown));
  line("model", textOf(run.model));
  line("created", textOf(run.created_at));
  line("program", textOf(run.program_ref));
  const price = integer(run.price_cents);
  if (price !== undefined) {
    const environmentPrice = integer(run.environment_price_cents);
    line("price", `${dollars(price)}${environmentPrice === undefined ? "" : ` (environment ${dollars(environmentPrice)})`}`);
  }
  line("billing", humanSafeScalar(shownBilling(typeof run.billing_status === "string" ? run.billing_status : "", run.price_cents)));
  const usage = (run.usage ?? {}) as JsonObject;
  const input = integer(usage.input_tokens);
  const output = integer(usage.output_tokens);
  if (input !== undefined && output !== undefined) line("usage", `${input} input tokens, ${output} output tokens`);
  line("environment", textOf(run.environment));
  for (const repository of (Array.isArray(run.repositories) ? run.repositories : []) as JsonObject[]) {
    const reference = typeof repository.ref === "string" ? `@${humanSafeScalar(repository.ref)}` : "";
    line("repository", `${textOf(repository.owner)}/${textOf(repository.name)}${reference}${repository.writable === true ? " (writable)" : ""}`);
  }
  if (run.output !== null && typeof run.output === "object" && !Array.isArray(run.output)) {
    const result = run.output;
    const changed = Array.isArray(result.changed_files) ? result.changed_files.length : 0;
    line("output", `commit ${textOf(result.commit_sha)} on ${textOf(result.repository)} ${textOf(result.branch)} (${changed} changed files) ${textOf(result.url)}`);
  }
  if (!stoppedByOwner(run.error)) line("error", textOf(run.error));
  if (run.has_patch === true) line("repository changes", "yes");
  const files = manifestFiles(run);
  const sessionFiles = (Array.isArray(run.session_files) ? run.session_files : []).filter((path): path is string => typeof path === "string");
  for (const [label, list] of [["files", files], ["session files", sessionFiles]] as const) {
    if (list.length === 0) {
      if (label === "files") text += "  files: none\n";
      continue;
    }
    text += `  ${label} (${list.length}):\n`;
    for (const path of list) text += `    ${humanSafeScalar(path)}\n`;
  }
  const status = typeof run.status === "string" ? run.status : "";
  if (!ENDED_STATUSES.includes(status)) text += nextLine(environment, ["run", "watch", id]);
  const first = files[0];
  if (first !== undefined) text += nextLine(environment, ["run", "show", id, "--file", first]) + nextLine(environment, ["run", "download", id]);
  return text;
}

async function show(context: Context): Promise<Json> {
  if (context.argument("RUN_ID") !== LATEST_RUN) runIdArgument(context);
  const file = context.option("--file");
  const outputFile = context.option("--output-file");
  if (outputFile !== undefined && file === undefined) throw invocationFailure("--output-file needs --file PATH; `cli run download RUN_ID --output-dir DIR` saves every file");
  if (file !== undefined && !validRelativePath(file)) throw invocationFailure(`--file ${quote(file)} must be a relative path exactly as listed in the run manifest`);
  const cwd = context.cwd;
  if (outputFile !== undefined) preflightDestination(cwd, outputFile, false);
  const runId = await runArgument(context);
  const runManifest = markCancelled(await fetchManifest(context, runId), undefined);
  if (file === undefined) {
    context.human = humanRun(runManifest, context.environment);
    // The run record is `result.run`, as in `run submit` and `run watch`.
    return { runId, run: runManifest };
  }
  const files = manifestFiles(runManifest);
  if (!files.includes(file)) {
    // A file the run does not list is not found (like a run id the service
    // does not know). `--file part6.md` for `outputs/part6.md`: the one
    // listed file whose path ends with the typed one is the correction.
    const error = failure("SERVICE_RESOURCE_NOT_FOUND", {
      reason: `run ${runId} has no file ${quote(file)}; list its files with \`${context.command(`run show ${runId}`)}\``,
      resource: { kind: "file", id: `${runId}/${file}` },
    });
    const matches = files.filter((path) => path.endsWith(`/${file}`));
    if (matches.length === 1) throw context.corrected(error, `Use the listed path ${matches[0]!}: \`{command}\``, context.argvWithOption("--file", matches[0]!));
    throw explainNotFound(error, context.environment, context.mode, "file", `${runId}/${file}`, ["run", "show", runId]);
  }
  const request = requestFor(context.operation, 1, filePath(runId, file));
  let result: JsonObject;
  if (outputFile !== undefined) {
    const [maxFile] = downloadLimits(context);
    const descriptor = createNewFile(cwd, outputFile);
    let downloaded: Downloaded;
    try {
      downloaded = await context.download(request, fileSink(descriptor), maxFile);
      try { fsyncSync(descriptor); } catch { throw invocationFailure(`cannot write output file ${quote(outputFile)}`); }
    } catch (caught) {
      try { closeSync(descriptor); } catch { /* already closed */ }
      try { unlinkSync(isAbsolute(outputFile) ? outputFile : resolve(cwd, outputFile)); } catch { /* best effort */ }
      throw fileNotFound(context, runId, file, annotateTooLarge(caught, () => `${quote(file)} is larger than ${maxFile} bytes`));
    }
    closeSync(descriptor);
    result = { bytes: downloaded.bytes, sha256: downloaded.sha256, written: true };
    const kind = contentType(downloaded.headers);
    if (kind !== undefined) result.contentType = kind;
    context.human = `Saved ${downloaded.bytes} bytes from ${file} (sha256 ${downloaded.sha256})\n`;
  } else {
    const limit = maxResponseBytes("control");
    const chunks: Uint8Array[] = [];
    let downloaded: Downloaded;
    try { downloaded = await context.download(request, { write(bytes) { chunks.push(bytes.slice()); } }, limit); }
    catch (caught) { throw fileNotFound(context, runId, file, annotateTooLarge(caught, () => `${quote(file)} is larger than ${limit} bytes; pass --output-file FILE to save it`)); }
    result = { bytes: downloaded.bytes, sha256: downloaded.sha256, written: false };
    const kind = contentType(downloaded.headers);
    if (kind !== undefined) result.contentType = kind;
    const text = textContent(Buffer.concat(chunks));
    if (text !== undefined) {
      // Human output ends with a line feed; JSON keeps the file's bytes.
      context.human = text.length === 0 || text.endsWith("\n") ? text : `${text}\n`;
      result.content = text;
    } else {
      context.human = `${file} is ${downloaded.bytes} bytes of binary data (sha256 ${downloaded.sha256}); pass --output-file FILE to save it\n`;
    }
  }
  return { runId, path: file, file: result };
}

// ------------------------------------------------------------ run download

/** Paths must be unique, must not nest under another listed file, and must not take the marker's names. */
export function checkDownloadPaths(files: string[]): void {
  const seen = new Set<string>();
  for (const path of files) {
    if (path === DOWNLOAD_MARKER || path === DOWNLOAD_MARKER_PARTIAL) throw protocol(`the run manifest lists the reserved path ${quote(path)}`);
    if (seen.has(path)) throw protocol(`the run manifest lists ${quote(path)} more than once`);
    seen.add(path);
  }
  for (const path of files) {
    if (files.some((other) => other.startsWith(`${path}/`))) throw protocol(`the run manifest lists ${quote(path)} as both a file and a directory`);
  }
}

/** When `directory` exists, the first of `DIRECTORY-2` .. `DIRECTORY-99` that does not. */
function freeDirectory(cwd: string, directory: string): string | undefined {
  const base = directory.replace(/\/+$/u, "");
  const at = (name: string): string => (isAbsolute(name) ? name : resolve(cwd, name));
  if (base === "" || !exists(at(base))) return undefined;
  for (let index = 2; index < 100; index += 1) {
    const candidate = `${base}-${index}`;
    if (!exists(at(candidate))) return candidate;
  }
  return undefined;
}

async function download(context: Context): Promise<Json> {
  if (context.argument("RUN_ID") !== LATEST_RUN) runIdArgument(context);
  const runId = await runArgument(context);
  const given = context.option("--output-dir");
  // Without --output-dir the files go to ./RUN_ID (a run id is a safe directory name).
  const directory = given ?? runId;
  const cwd = context.cwd;
  try { preflightDestination(cwd, directory, true, given === undefined); }
  catch (caught) {
    // An existing directory names a free one to use.
    const free = caught instanceof RunnerFailure ? freeDirectory(cwd, directory) : undefined;
    if (free === undefined) throw caught;
    throw context.corrected(caught as RunnerFailure, `Download into the new directory ${free}: \`{command}\``, context.argvSettingOption("--output-dir", free));
  }
  const runManifest = await fetchManifest(context, runId);
  const files = manifestFiles(runManifest);
  checkDownloadPaths(files);
  const [maxFile, maxRun] = downloadLimits(context);
  const fresh = FreshDirectory.create(cwd, directory);
  const incomplete = `the output directory is incomplete and has no ${DOWNLOAD_MARKER}`;
  let total = 0;
  const entries: JsonObject[] = [];
  for (const path of files) {
    const cap = Math.min(maxFile, Math.max(0, maxRun - total));
    const request = requestFor(context.operation, 1, filePath(runId, path));
    let downloaded: Downloaded;
    try {
      const descriptor = fresh.createFile(path);
      try {
        downloaded = await context.download(request, fileSink(descriptor), cap);
        try { fsyncSync(descriptor); } catch { throw invocationFailure("cannot write the downloaded file"); }
      } finally { closeSync(descriptor); }
    } catch (caught) {
      if (!(caught instanceof RunnerFailure) || hasReason(caught)) throw caught;
      const cause = caught.code === "SERVICE_RESPONSE_TOO_LARGE"
        ? cap < maxFile ? `the run's files exceed ${maxRun} bytes in total` : `${quote(path)} is larger than ${maxFile} bytes`
        : `downloading ${quote(path)} failed`;
      throw fileNotFound(context, runId, path, withReason(caught, `${cause}; ${incomplete}`));
    }
    total += downloaded.bytes;
    entries.push({ path, bytes: downloaded.bytes, sha256: downloaded.sha256 });
  }
  const result: JsonObject = { runId, outputDir: directory, fileCount: entries.length, totalBytes: total, files: entries, marker: DOWNLOAD_MARKER };
  writeMarker(fresh, { download: result, run: runManifest });
  context.human = `Downloaded ${files.length} file${files.length === 1 ? "" : "s"} (${total} bytes) from ${runId} into ${quote(directory)}\n`
    + entries.map((entry) => `  ${entry.path as string} (${entry.bytes as number} bytes)\n`).join("")
    + `  ${DOWNLOAD_MARKER} (the download record)\n`;
  return result;
}

/**
 * Writes the completion marker under a temporary name, then links it into
 * place without replacing anything (a rename where links are unsupported; the
 * directory is this invocation's own).
 */
function writeMarker(fresh: FreshDirectory, document: Json): void {
  const failed = () => invocationFailure(`cannot write ${DOWNLOAD_MARKER}`);
  const bytes = new TextEncoder().encode(`${canonicalJson(document)}\n`);
  const descriptor = fresh.createFile(DOWNLOAD_MARKER_PARTIAL);
  try {
    let offset = 0;
    while (offset < bytes.length) offset += writeSync(descriptor, bytes, offset, bytes.length - offset);
    fsyncSync(descriptor);
  } catch { throw failed(); }
  finally { closeSync(descriptor); }
  const temporary = join(fresh.root, DOWNLOAD_MARKER_PARTIAL);
  const target = join(fresh.root, DOWNLOAD_MARKER);
  try { linkSync(temporary, target); }
  catch (caught) {
    if ((caught as NodeJS.ErrnoException).code === "EEXIST") throw failed();
    try { renameSync(temporary, target); } catch { throw failed(); }
    return;
  }
  try { unlinkSync(temporary); } catch { throw failed(); }
}

// --------------------------------------------------------------- run share

/** 9999-12-31T23:59:59Z: later expiries would need an expanded year form. */
const MAX_EXPIRY_SECONDS = 253_402_300_799;

/** The `exp` (epoch seconds) of the share URL's fragment, as RFC 3339. */
export function shareExpiry(url: string): string | undefined {
  const hash = url.indexOf("#");
  if (hash < 0) return undefined;
  const value = url.slice(hash + 1).split("&").find((pair) => pair.startsWith("exp="))?.slice(4);
  if (value === undefined || !/^[0-9]{1,12}$/u.test(value)) return undefined;
  const seconds = Number(value);
  if (seconds > MAX_EXPIRY_SECONDS) return undefined;
  return new Date(seconds * 1000).toISOString().replace(/\.000Z$/u, "Z");
}

const validShareUrl = (url: string): boolean => url.length <= 2048 && url.length > "https://".length && url.startsWith("https://") && /^[!-~]+$/u.test(url);

async function share(context: Context): Promise<Json> {
  const runId = await runArgument(context);
  const path = `${runPath(runId)}/share`;
  const gate = context.gate(context.planned(0, path));
  if (gate.kind === "preview") return gate.result;
  const body = jsonObject(await context.send(requestFor(context.operation, 0, path)));
  const url = body.url;
  if (typeof url !== "string" || !validShareUrl(url)) throw protocol("the share response has no valid https url");
  const result: JsonObject = { runId, url };
  const expires = shareExpiry(url);
  if (expires !== undefined) {
    result.expires_at = expires;
    result.expiresAt = expires;
  }
  if (context.mode === "human") {
    context.err("Warning: anyone with this link can read this run's outputs for 24 hours, and it cannot be revoked. To give specific people access instead, invite them to your organization (`cli org invite`).\n");
    context.human = `${url}\n${expires === undefined ? "" : `Expires: ${expires}\n`}`;
  }
  return result;
}
