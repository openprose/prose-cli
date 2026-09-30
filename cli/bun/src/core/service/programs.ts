// Service programs: `program list|show|save|visibility|delete|revisions|draft`.
// Mirrors cli/rust/crates/prose-runner-core/src/service/programs.rs; both are
// pinned by cli/conformance/cases/service/programs/. Projections follow
// shared/schemas/service/programs.schema.json with server field names kept
// verbatim. Program bytes are never normalized.
import { createHash } from "node:crypto";
import { lstatSync, statSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { failure, hostedRunFailed, invocationFailure } from "../errors";
import { humanSafeScalar, quote } from "../output";
import { RunnerFailure } from "../types";
import { readText, writeNewFile } from "./fs";
import { encodeSegment, jsonObject, requestFor, withJsonBody, type Request } from "./http";
import type { Context } from "./index";
import type { Json, JsonObject } from "./manifest";
import { absoluteUrl } from "./render";
import { checkOwner, confirmOwnSlug, fillOwner, ownSlugReference, parseOwnAllowed, parseOwnSlug, refOf, resolveRevNumber, unconfirmedOwner, validOwner, validRev, validSlug } from "./program-ref";
import { didYouMean } from "./manifest";
import { addIso, publicBilling, runErrorText, sanitizeServiceMessage, validText } from "./render";
import { eventJson } from "./sse";

/** The service's program size limit (MAX_CONTENT_BYTES, 256 KiB), enforced before any request. */
export const MAX_PROGRAM_BYTES = 256 * 1024;
const MAX_MESSAGE_CHARS = 160;
const MAX_SENTENCE_CHARS = 4096;
const MAX_ITEMS = 1000;
const MAX_TEXT_CHARS = 1_048_576;

export async function execute(context: Context): Promise<Json> {
  switch (context.invocation.operation) {
    case "program.list": return await list(context);
    case "program.show": return await show(context);
    case "program.save": return await save(context);
    case "program.visibility": return await visibility(context);
    case "program.delete": return await remove(context);
    case "program.revisions": return await revisions(context);
    case "program.draft": return await draft(context);
    default: return await context.notImplemented();
  }
}

function protocol(reason: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason });
}

/** common.schema.json#/$defs/text: at most 1,048,576 code points, no C0 but TAB/LF/CR, no DEL. */
export function isText(value: string): boolean {
  return Array.from(value).length <= MAX_TEXT_CHARS && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value);
}

/** Request index of GET /programs/{slug}/revisions in save, visibility and delete (the OWNER/SLUG check). */
const OWNER_CHECK = 1;

/** `<SLUG>`, or `OWNER/SLUG` when OWNER is the caller; `creates` is a first save. */
async function ownSlugArgument(context: Context, creates: boolean): Promise<string> {
  const reference = ownSlugReference(context);
  await confirmOwnSlug(context, reference, OWNER_CHECK, creates);
  return reference.slug;
}

/** Checks an --output-file target before any request; the file is created only after success. */
function preflightOutput(cwd: string, value: string): void {
  const path = isAbsolute(value) ? value : resolve(cwd, value);
  let exists = true;
  try { lstatSync(path); } catch { exists = false; }
  if (exists) throw invocationFailure(`output file ${quote(value)} already exists; choose a new path`);
  let parent = false;
  try { parent = statSync(dirname(path)).isDirectory(); } catch { parent = false; }
  if (!parent) throw invocationFailure(`the directory for output file ${quote(value)} does not exist`);
}

function writtenFile(context: Context, bytes: Uint8Array): JsonObject {
  const file: JsonObject = { bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") };
  const target = context.option("--output-file");
  if (target !== undefined) {
    writeNewFile(context.cwd, target, bytes);
    file.written = true;
  } else {
    file.written = false;
    let text: string | undefined;
    // ignoreBOM: a leading U+FEFF is program bytes, kept exactly as Rust keeps it.
    try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); } catch { text = undefined; }
    if (text !== undefined && isText(text)) file.content = text;
  }
  return file;
}

function integer(value: Json | undefined, minimum: number): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum ? value : undefined;
}

/** Controls become spaces, cut to 1024 code points; a blank message is dropped. */
function message(value: Json | undefined): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") throw protocol("program field `message` is invalid");
  const spaced = Array.from(value.replace(/[\u0000-\u001f\u007f]/gu, " ")).slice(0, 1024).join("");
  return /^ *$/u.test(spaced) ? undefined : spaced;
}

/** Projects one service program record (see the Rust twin). */
function project(record: Json | undefined, withVisibility: boolean, withContent: boolean): JsonObject {
  const field = (name: string) => protocol(`program field \`${name}\` is invalid`);
  if (record === undefined || record === null || typeof record !== "object" || Array.isArray(record)) throw protocol("the service returned an invalid program record");
  const out: JsonObject = {};
  const owner = record.owner;
  if (typeof owner !== "string" || !validOwner(owner)) throw field("owner");
  out.owner = owner;
  const slug = record.slug;
  if (typeof slug !== "string" || !validSlug(slug)) throw field("slug");
  out.slug = slug;
  if (withVisibility) {
    const visibility = record.visibility;
    if (visibility !== "public" && visibility !== "private") throw field("visibility");
    out.visibility = visibility;
  }
  const rev = integer(record.rev, 1);
  if (rev === undefined) throw field("rev");
  out.rev = rev;
  for (const name of ["rev_id", "commit_id"]) {
    const value = record[name];
    if (typeof value !== "string" || !validRev(value)) throw field(name);
    out[name] = value;
  }
  // The pinned reference, ready for `run submit --from` and `job contract attach`.
  out.ref = refOf(owner, slug, out.rev_id as string);
  const parent = record.parent_commit_id;
  if (parent === undefined || parent === null) out.parent_commit_id = null;
  else if (typeof parent === "string" && validRev(parent)) out.parent_commit_id = parent;
  else throw field("parent_commit_id");
  const updated = integer(record.updated_at, 0);
  if (updated === undefined) throw field("updated_at");
  out.updated_at = updated;
  addIso(out, ["updated_at"]);
  const text = message(record.message);
  if (text !== undefined) out.message = text;
  if (withContent) {
    const content = record.content;
    if (typeof content === "string") { if (isText(content)) out.content = content; }
    else if (content !== undefined && content !== null) throw field("content");
  }
  return out;
}

function items(body: JsonObject, key: string): Json[] {
  const values = body[key];
  if (!Array.isArray(values)) throw protocol(`the service response has no \`${key}\` array`);
  if (values.length > MAX_ITEMS) throw failure("SERVICE_RESPONSE_TOO_LARGE", { reason: `the service returned more than ${MAX_ITEMS} ${key}` });
  return values;
}

const nameOf = (program: JsonObject): string => `${program.owner as string}/${program.slug as string}`;

async function list(context: Context): Promise<Json> {
  const withContent = context.flag("--with-content");
  const body = jsonObject(await context.send(requestFor(context.operation, 0, "/programs")));
  const programs = items(body, "programs").map((record) => project(record, true, withContent));
  let human = programs.length === 0 ? "No saved programs.\n" : "";
  for (const program of programs) human += `${nameOf(program)}  rev ${program.rev as number}  ${program.visibility as string}  rev_id=${program.rev_id as string}  ref=${program.ref as string}\n`;
  context.human = human;
  return { programs };
}

/** `program show` reads anonymously when no credential is configured (public programs). */
async function hasCredential(context: Context): Promise<boolean> {
  const value = context.deps.env[context.environment.credentialEnv];
  if (value !== undefined && value !== "") return true;
  try { return (await context.transport.storedCredential()) !== null; }
  catch { return false; }
}

function fileLine(label: string, file: JsonObject, target: string | undefined): string {
  const bytes = file.bytes as number;
  const sha = file.sha256 as string;
  return target !== undefined
    ? `Wrote ${bytes} bytes to ${humanSafeScalar(target)} (sha256 ${sha})\n`
    : `${label} is ${bytes} bytes (sha256 ${sha}) and not plain text; write it with --output-file FILE\n`;
}

/**
 * A bare SLUG that is not one of the caller's programs: when the caller's
 * program list (manifest request 2) has one slug within two edits, the
 * suggestion is the same command with that slug. The listing is advisory: a
 * failed listing keeps `error` (mirrors Rust `nearest_own_program`).
 */
async function nearestOwnProgram(context: Context, error: RunnerFailure, given: string, slug: string): Promise<RunnerFailure> {
  let body: JsonObject;
  try { body = jsonObject(await context.send({ ...requestFor(context.operation, 2, "/programs"), class: "control" as const })); }
  catch (caught) {
    if (caught instanceof RunnerFailure) return error;
    throw caught;
  }
  const slugs = (Array.isArray(body.programs) ? body.programs : [])
    .map((program) => (program !== null && typeof program === "object" && !Array.isArray(program) ? (program as JsonObject).slug : undefined))
    .filter((candidate): candidate is string => typeof candidate === "string" && validSlug(candidate));
  const near = didYouMean(slug, slugs);
  if (near === undefined) return error;
  const details = { ...(error.details ?? {}) };
  if (typeof details.reason === "string") details.reason = `${details.reason}; did you mean ${near}?`;
  const withReason = new RunnerFailure({ code: error.code, boundary: error.boundary, message: error.message, action: error.action, exitCode: error.exitCode, retryable: error.retryable, details });
  return context.corrected(withReason, `Run \`{command}\`: ${near} is the closest program you have`, context.argvWithArgument(given, given.replace(slug, near)));
}

async function show(context: Context): Promise<Json> {
  const given = context.argument("OWNER/SLUG[@REV]") ?? "";
  const reference = parseOwnAllowed(given, true);
  const target = context.option("--output-file");
  if (target !== undefined) preflightOutput(context.cwd, target);
  // A bare SLUG is the caller's own program and a numeric @N is resolved to
  // its rev_id (manifest request 1).
  const byNumber = reference.revNumber !== undefined;
  const bare = reference.owner === "";
  try {
    if (byNumber) await resolveRevNumber(context, reference, 1);
    else await fillOwner(context, reference, 1);
  } catch (caught) {
    if (bare && caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") throw await nearestOwnProgram(context, caught, given, reference.slug);
    throw caught;
  }
  let path = `/p/${encodeSegment(reference.owner)}/${encodeSegment(reference.slug)}`;
  if (reference.rev !== undefined) path += `@${reference.rev}`;
  const request: Request = { ...requestFor(context.operation, 0, path), bearer: await hasCredential(context) };
  const body = jsonObject(await context.send(request));
  const record = body.program;
  const program = project(record, true, false);
  if ((program.owner as string).toLowerCase() !== reference.owner.toLowerCase() || program.slug !== reference.slug
    || (reference.rev !== undefined && program.rev_id !== reference.rev)) {
    throw protocol("the service returned a different program");
  }
  const source = (record as JsonObject).content;
  if (typeof source !== "string") throw protocol("program field `content` is invalid");
  const isOwner = body.is_owner;
  if (typeof isOwner !== "boolean") throw protocol("the service response has no `is_owner` flag");
  const file = writtenFile(context, new TextEncoder().encode(source));
  context.human = typeof file.content === "string" ? file.content : fileLine(nameOf(program), file, target);
  if (byNumber && context.mode === "human") context.err(`Resolved ${humanSafeScalar(given)} to ${program.ref as string} (rev ${program.rev as number}).\n`);
  return { program, is_owner: isOwner, file };
}

function trimAscii(value: string): string {
  return value.replace(/^[ \t\n\r]+|[ \t\n\r]+$/gu, "");
}

async function save(context: Context): Promise<Json> {
  const reference = ownSlugReference(context);
  const source = context.argument("FILE") ?? "";
  const text = await readText(context.cwd, source, MAX_PROGRAM_BYTES, "FILE");
  if (text.length === 0) throw invocationFailure(`FILE ${quote(source)} is empty; a program needs content`);
  const body: JsonObject = { content: text };
  const note = context.option("--message");
  if (note !== undefined) {
    const trimmed = trimAscii(note);
    if (!validText(trimmed, MAX_MESSAGE_CHARS)) throw invocationFailure(`--message must be 1 to ${MAX_MESSAGE_CHARS} characters without control characters`);
    body.message = trimmed;
  }
  const base = context.option("--base");
  if (base !== undefined) {
    if (!validRev(base)) throw invocationFailure(`--base ${quote(base)} must be a commit_id: 16 lowercase hex digits`);
    body.base_commit_id = base;
  }
  await confirmOwnSlug(context, reference, OWNER_CHECK, true);
  const slug = reference.slug;
  const path = `/programs/${encodeSegment(slug)}`;
  const request = withJsonBody(requestFor(context.operation, 0, path), body);
  const gate = context.gate(context.planned(0, path, [], request.body));
  if (gate.kind === "preview") return gate.result;
  const response = await context.sendRaw(request);
  if (response.status < 200 || response.status >= 300) {
    const error = context.classify(request, response);
    if (error.code === "SERVICE_WRITE_CONFLICT") withCurrent(error, response.body);
    if (error.code === "GITHUB_LINK_REQUIRED") throw refineSaveForbidden(error, response.body, context.knownCredential());
    throw error;
  }
  const reply = jsonObject(response);
  const program = project(reply.program, true, false);
  const url = reply.url;
  if (typeof url !== "string" || !url.startsWith("/p/") || url.length <= 3 || url.length > 256 || /\s/u.test(url)) {
    throw protocol("the service response has no valid `url`");
  }
  // The service answers with a path; the result is the absolute URL.
  const absolute = absoluteUrl(context.environment, url);
  if (absolute === undefined) throw protocol("the service response has no valid `url`");
  context.human = `Saved ${nameOf(program)} rev ${program.rev as number}: ref ${nameOf(program)}@${program.rev_id as string} (commit ${program.commit_id as string})\n`;
  return { program, url: absolute };
}

/** The service's 403 texts on PUT /programs/{slug}; only the first is a missing GitHub link. */
const LINK_REQUIRED_TEXT = "Sharing requires a linked GitHub login";
const REJECTED_TEXTS = ["This handle collides with a reserved namespace", "Slug is owned by a different account"];

/**
 * Refines the manifest's PUT /programs/{slug} 403 override by the service's
 * `error` text (see the Rust twin): linked-login refusal stays
 * GITHUB_LINK_REQUIRED, a reserved handle or reassigned slug is
 * SERVICE_REQUEST_REJECTED, anything else (including `Invalid API key.`) is
 * SERVICE_AUTH_REQUIRED.
 */
export function refineSaveForbidden(error: RunnerFailure, body: Uint8Array, credential: string | undefined): RunnerFailure {
  let text = "";
  try {
    const parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(body)) as Json;
    if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed) && typeof parsed.error === "string") text = parsed.error;
  } catch { text = ""; }
  if (text.startsWith(LINK_REQUIRED_TEXT)) return error;
  if (REJECTED_TEXTS.some((prefix) => text.startsWith(prefix))) {
    const message = sanitizeServiceMessage(text, credential);
    return failure("SERVICE_REQUEST_REJECTED", message === undefined ? { serviceStatus: 403 } : { serviceStatus: 403, serviceMessage: message });
  }
  return failure("SERVICE_AUTH_REQUIRED", { serviceStatus: 403 });
}

/** A stale --base (409 with the current record): name the current commit. */
function withCurrent(error: RunnerFailure, body: Uint8Array): void {
  let current: JsonObject | undefined;
  try {
    const parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)) as JsonObject;
    current = project(parsed?.current, true, false);
  } catch { current = undefined; }
  if (current === undefined || error.details === undefined) return;
  error.details.currentCommitId = current.commit_id;
  error.details.currentRev = current.rev;
}

/** VISIBILITY, case-insensitively; a typo names the nearest value. */
function visibilityArgument(context: Context): string {
  const given = context.argument("VISIBILITY") ?? "";
  const value = given.toLowerCase();
  if (value === "public" || value === "private") return value;
  const near = didYouMean(value, ["public", "private"]);
  const hint = near === undefined ? "" : `; did you mean ${near}?`;
  const error = invocationFailure(`VISIBILITY ${quote(given)} must be public or private${hint}`);
  // The did-you-mean is also the corrected command.
  throw near === undefined ? error : context.corrected(error, `Use ${near}: \`{command}\``, context.argvWithArgument(given, near));
}

async function visibility(context: Context): Promise<Json> {
  const reference = ownSlugReference(context);
  const value = visibilityArgument(context);
  await confirmOwnSlug(context, reference, OWNER_CHECK, false);
  const slug = reference.slug;
  const path = `/programs/${encodeSegment(slug)}/visibility`;
  const request = withJsonBody(requestFor(context.operation, 0, path), { visibility: value });
  const gate = context.gate(context.planned(0, path, [], request.body));
  if (gate.kind === "preview") return gate.result;
  let response;
  try { response = await context.send(request); }
  catch (caught) { throw unsavedProgram(caught); }
  const reply = jsonObject(response);
  const program = project(reply.program, true, false);
  context.human = `${nameOf(program)} is now ${program.visibility as string}\n`;
  return { program };
}

/** The service's 400 for a visibility change of a program the caller never saved (mirrors Rust `unsaved_program`). */
const NEW_PROGRAM_TEXT = "New program needs content.";

function unsavedProgram(caught: unknown): unknown {
  const message = caught instanceof RunnerFailure ? caught.details?.serviceMessage : undefined;
  return typeof message === "string" && message.trim() === NEW_PROGRAM_TEXT ? failure("SERVICE_RESOURCE_NOT_FOUND") : caught;
}

async function remove(context: Context): Promise<Json> {
  const slug = await ownSlugArgument(context, false);
  const path = `/programs/${encodeSegment(slug)}`;
  const gate = context.gate(context.planned(0, path));
  if (gate.kind === "preview") return gate.result;
  let reply: JsonObject;
  try { reply = jsonObject(await context.send(requestFor(context.operation, 0, path))); }
  catch (caught) {
    // A confirmed delete is idempotent: nothing to delete is the goal state.
    if (!(caught instanceof RunnerFailure) || caught.code !== "SERVICE_RESOURCE_NOT_FOUND") throw caught;
    context.human = `Program ${slug} was already absent; nothing was deleted.\n`;
    return { slug, deleted: true, alreadyAbsent: true };
  }
  if (reply.deleted !== true) throw protocol("the service did not confirm the deletion");
  context.human = `Deleted ${slug}\n`;
  return { slug, deleted: true };
}

async function revisions(context: Context): Promise<Json> {
  // OWNER/SLUG is checked against the owner these (own) revisions name.
  const reference = ownSlugReference(context);
  const slug = reference.slug;
  const owned = reference.owner !== "";
  let body: JsonObject;
  try { body = jsonObject(await context.send(requestFor(context.operation, 0, `/programs/${encodeSegment(slug)}/revisions`))); }
  catch (caught) {
    // OWNER/SLUG of a program the caller has not saved: OWNER cannot be
    // confirmed, which is not "your program is missing".
    if (owned && caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") throw unconfirmedOwner(context, reference);
    throw caught;
  }
  if (owned && Array.isArray(body.revisions) && body.revisions.length === 0) throw unconfirmedOwner(context, reference);
  checkOwner(reference, body);
  const list = items(body, "revisions").map((record) => project(record, false, false));
  let human = list.length === 0 ? "No revisions.\n" : "";
  for (const revision of list) {
    human += `rev ${revision.rev as number}  rev_id=${revision.rev_id as string}  commit_id=${revision.commit_id as string}  ref=${revision.ref as string}`;
    if (typeof revision.message === "string") human += `  ${humanSafeScalar(revision.message)}`;
    human += "\n";
  }
  context.human = human;
  return { revisions: list };
}

function validSentence(value: string): boolean {
  return !/^[ \t\n]*$/u.test(value) && Array.from(value).length <= MAX_SENTENCE_CHARS && !/[\u0000-\u0008\u000b-\u001f\u007f]/u.test(value);
}

const validModel = (value: string): boolean => /^[a-z0-9][a-z0-9.-]{0,63}$/u.test(value);
const validRunId = (value: string): boolean => /^run_[A-Za-z0-9_-]{1,128}$/u.test(value);

function runFailed(runId: string | undefined, reason: string): RunnerFailure {
  return hostedRunFailed(runId === undefined ? { reason } : { reason, runId });
}

/**
 * `program draft`: POST /write runs the platform authoring program. It is read
 * as a server-sent event stream (with service heartbeats), so a multi-minute
 * authoring run is bounded by the stream idle timeout.
 */
async function draft(context: Context): Promise<Json> {
  const sentence = context.argument("SENTENCE") ?? "";
  if (!validSentence(sentence)) throw invocationFailure(`SENTENCE must be non-blank text of at most ${MAX_SENTENCE_CHARS} characters without control characters`);
  const body: JsonObject = { request: sentence };
  const current = context.option("--current");
  if (current !== undefined) body.current_program = await readText(context.cwd, current, MAX_PROGRAM_BYTES, "--current");
  const model = context.option("--model");
  if (model !== undefined) {
    if (!validModel(model)) throw invocationFailure(`--model ${quote(model)} is not a model id; list models with \`cli model list\``);
    body.model = model;
  }
  const target = context.option("--output-file");
  if (target !== undefined) preflightOutput(context.cwd, target);
  // An unknown model fails before confirmation (request 1).
  if (model !== undefined) await context.checkModel(1, model);
  const base = withJsonBody(requestFor(context.operation, 0, "/write"), body);
  const request: Request = { ...base, headers: [...base.headers, ["Accept", "text/event-stream"]] };
  const planned = context.planned(0, "/write", [], request.body);
  if (context.invocation.preview || !context.invocation.yes) {
    // A draft reserves the same flat hold as a run (request 2, advisory).
    const quote = await context.advisoryQuote(2);
    if (quote !== undefined) planned.quote = quote;
  }
  const gate = context.gate(planned);
  if (gate.kind === "preview") return gate.result;
  const opened = await context.openStream(request);
  if (opened.kind === "response") throw context.classify(request, opened.response);
  if (opened.kind === "dropped") {
    throw failure("SERVICE_UNAVAILABLE", { reason: "the connection failed before the service answered; check `cli run list` before drafting again, because a draft may have started" });
  }
  const reader = opened.reader;
  let runId: string | undefined;
  let problem: string | undefined;
  let complete: JsonObject | undefined;
  while (complete === undefined) {
    let item;
    try { item = await reader.next(); }
    catch (caught) {
      if (caught instanceof RunnerFailure && caught.code === "CANCELLED" && runId !== undefined) {
        throw failure("CANCELLED", { runId, reason: `stopped waiting; the draft run continues on the service: \`cli run show ${runId}\`` });
      }
      throw caught;
    }
    if (item.kind === "end") {
      if (item.end === "closed" && problem !== undefined) throw runFailed(runId, problem);
      const reason = runId === undefined
        ? "the draft stream ended before the run started"
        : `the draft stream ended before the run finished; inspect it with \`cli run show ${runId}\``;
      throw failure("SERVICE_UNAVAILABLE", runId === undefined ? { reason } : { reason, runId });
    }
    const data = eventJson(item.event);
    const object = data !== null && typeof data === "object" && !Array.isArray(data) ? data : {};
    const kind = typeof object.type === "string" ? object.type : item.event.event;
    if (runId === undefined && typeof object.run_id === "string" && validRunId(object.run_id)) runId = object.run_id;
    if (kind === "run_complete") complete = object;
    else if (kind === "error") {
      const text = typeof object.message === "string" ? sanitizeServiceMessage(object.message, context.knownCredential()) : undefined;
      problem = text === undefined ? "the hosted writer reported an error" : `the hosted writer reported an error: ${runErrorText(text)}`;
    }
  }
  const status = typeof complete.status === "string" ? complete.status : "";
  if (status !== "completed") throw runFailed(runId, problem ?? `the hosted writer run ended with status ${quote(humanSafeScalar(status))}`);
  const response = decodeDraft(typeof complete.response === "string" ? complete.response : "");
  const trimmed = trimAscii(response);
  if (trimmed.length === 0) throw runFailed(runId, "the hosted writer returned no program");
  const file = writtenFile(context, new TextEncoder().encode(`${trimmed}\n`));
  context.human = typeof file.content === "string" ? file.content : fileLine("The draft", file, target);
  const result: JsonObject = { file };
  if (runId !== undefined) result.runId = runId;
  // Server-provided billing, copied verbatim (never computed here).
  for (const key of ["price_cents", "environment_price_cents"]) {
    const value = complete[key];
    if (typeof value === "number" && Number.isSafeInteger(value)) result[key] = value;
  }
  const billing = complete.billing_status;
  if (typeof billing === "string" && validText(billing, 32)) result.billing_status = publicBilling(billing);
  return result;
}

/**
 * run_complete.response is the raw bytes of outputs/result.json; prose-write
 * returns one string, so the program arrives as a JSON string literal. The
 * service unwraps it only on its blocking JSON path, so decode it the same
 * way (see the Rust twin): unwrap a JSON string, else apply the service's
 * escaped-one-liner backstop, else keep it.
 */
export function decodeDraft(response: string): string {
  try {
    const parsed = JSON.parse(response) as Json;
    if (typeof parsed === "string") return parsed;
  } catch { /* not JSON: fall through */ }
  if (!response.includes("\n") && response.includes("\\n")) {
    let text = trimAscii(response);
    if (text.startsWith('"')) text = text.slice(1);
    if (text.endsWith('"')) text = text.slice(0, -1);
    return text
      .replaceAll("\\r\\n", "\n")
      .replaceAll("\\n", "\n")
      .replaceAll("\\t", "\t")
      .replaceAll('\\"', '"')
      .replaceAll("\\\\", "\\");
  }
  return response;
}
