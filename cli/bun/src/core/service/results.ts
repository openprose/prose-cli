// Published results: `result list|show` (anonymous reads of a
// public program's publications) and `result publish|unpublish` (owner-only,
// confirm-class). Mirrors cli/rust/crates/prose-runner-core/src/service/results.rs
// byte for byte; see that module for the design notes.
import { closeSync, fsyncSync, unlinkSync, writeSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { failure, invocationFailure } from "../errors";
import { humanSafeScalar, quote } from "../output";
import { RunnerFailure, type OutputMode } from "../types";
import { createNewFile, validRelativePath } from "./fs";
import { encodeSegment, jsonObject, maxResponseBytes, requestFor, withJsonBody, type Request, type Response } from "./http";
import type { Context } from "./index";
import { manifest as operationsManifest, type Environment, type Json, type JsonObject } from "./manifest";
import { explainNotFound as notFoundError } from "./not-found";
import { confirmOwnSlug, fillOwner, ownSlugReference, parseOwnSlug, validOwner, validRev, validSlug, type ProgramRef } from "./program-ref";
import { dollarsOrDash, followUpCommand, validText } from "./render";

const SHOW_BY_ID = 0;
const SHOW_LATEST = 1;
const SHOW_RAW = 2;
/** GET /programs/{slug}/revisions: the owner of a bare SLUG (list 1, show 4) or the OWNER/SLUG check (publish, unpublish 1). */
const LIST_REVISIONS = 1;
const SHOW_REVISIONS = 4;
const OWNER_CHECK = 1;
const MAX_LIST_ITEMS = 200;
const MAX_MANIFEST_FILES = 16;
const MAX_ID = 64;
const MAX_CONTENT_TYPE = 128;

function malformed(field: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason: `the service returned an invalid ${field}` });
}

/** Rust `{:?}` of a plain string (quotes and escapes), as the shared fs helpers use JSON.stringify. */
function quoted(value: string): string { return quote(value); }

/** Adds a corrective `reason` to SERVICE_RESOURCE_NOT_FOUND. */
async function explainNotFound<T>(promise: Promise<T>, reason: () => string): Promise<T> {
  try { return await promise; }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") {
      throw failure("SERVICE_RESOURCE_NOT_FOUND", { ...(caught.details ?? {}), reason: reason() });
    }
    throw caught;
  }
}

export async function execute(context: Context): Promise<Json> {
  switch (context.operation.id) {
    case "result.list": return await list(context);
    case "result.show": return await show(context);
    case "result.publish": return await publish(context);
    case "result.unpublish": return await unpublish(context);
    default: return await context.notImplemented();
  }
}

// ---------------------------------------------------------------- arguments

function splitProgram(name: string): [string, string] | undefined {
  const slash = name.indexOf("/");
  if (slash < 0) return undefined;
  const owner = name.slice(0, slash);
  const slug = name.slice(slash + 1);
  return validOwner(owner) && validSlug(slug) ? [owner, slug] : undefined;
}

/**
 * `OWNER/SLUG` of a public program, or a bare SLUG for the caller's own
 * program (owner "" until `ownerOf` reads it). A pinned `@REV`
 * is refused.
 */
export function publicProgram(value: string): [string, string] {
  const at = value.indexOf("@");
  if (at >= 0) {
    const name = value.slice(0, at);
    if (splitProgram(name) !== undefined || validSlug(name)) throw invocationFailure(`results belong to a program, not a revision; pass ${quoted(name)} without @REV`);
  }
  if (validSlug(value)) return ["", value];
  const split = splitProgram(value);
  // `cli result` reads published results, not run records.
  if (split === undefined && value.startsWith("run_")) {
    throw invocationFailure(`${quoted(value)} is a run id, and \`cli result\` reads the published results of a public program; read a run's output with \`cli run show ${value}\` or \`cli run download ${value} --output-dir DIR\``);
  }
  if (split === undefined) throw invocationFailure(`program ${quoted(value)} must be OWNER/SLUG (an owner handle and a lowercase program slug) or a bare SLUG for your own program; \`cli result\` reads published results, and a run's own output is \`cli run show RUN_ID\``);
  return split;
}

/** The owner of a bare SLUG from the caller's own revisions (manifest request `index`). */
async function ownerOf(context: Context, owner: string, slug: string, index: number): Promise<string> {
  if (owner !== "") return owner;
  const reference: ProgramRef = { owner: "", slug };
  await fillOwner(context, reference, index);
  return reference.owner;
}

/** `OWNER/SLUG` for a hint: the confirmed owner, or the bare own slug that `result list` and `run submit --from` resolve. */
const programName = (owner: string, slug: string): string => owner === "" ? slug : `${owner}/${slug}`;

type Hint = { environment: Environment; mode: OutputMode };

/**
 * A copyable follow-up command line for the selected environment that keeps
 * the machine output mode (the shared renderer: render.ts followUpCommand).
 */
export function command(hint: Hint, words: string): string {
  return followUpCommand(hint.environment, hint.mode, words);
}

export const validPublicationId = (value: string): boolean => /^[A-Za-z0-9_-]{1,64}$/u.test(value);

function publicationId(hint: Hint, value: string, program: string): string {
  if (validPublicationId(value)) return value;
  throw invocationFailure(`<PUBLICATION_ID> ${quoted(value)} must be a publication id as printed by \`${command(hint, `result list ${program}`)}\` (letters, digits, - and _)`);
}

export const validRunId = (value: string): boolean => /^run_[A-Za-z0-9_-]{1,128}$/u.test(value);

function limit(context: Context): number {
  const fallback = context.operation.options.find((option) => option.name === "--limit")?.default;
  const value = context.option("--limit") ?? fallback ?? "20";
  const number = /^[0-9]{1,3}$/u.test(value) ? Number(value) : Number.NaN;
  if (!Number.isInteger(number) || number < 1 || number > 100) throw context.limitError(invocationFailure(`--limit must be an integer from 1 to 100, got ${quoted(value)}`), value, 100);
  return number;
}

function resultsPath(owner: string, slug: string): string {
  return `/p/${encodeSegment(owner)}/${encodeSegment(slug)}/results`;
}

// ---------------------------------------------------------------- projection

export const validTimestamp = (value: string): boolean => value.length <= 40 && /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$/u.test(value);
const validModel = (value: string): boolean => /^[a-z0-9][a-z0-9.-]{0,63}$/u.test(value);

export function validProgramRef(value: string): boolean {
  if (value.length > 200) return false;
  const at = value.indexOf("@");
  if (at < 0) return false;
  return validRev(value.slice(at + 1)) && splitProgram(value.slice(0, at)) !== undefined;
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stringField(object: JsonObject, key: string, path: string, valid: (value: string) => boolean): string {
  const value = object[key];
  if (typeof value !== "string" || !valid(value)) throw malformed(`${path}.${key}`);
  return value;
}

/** A JSON number both products read as a non-negative 64-bit integer. */
function nonNegativeInteger(value: Json | undefined): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

export function publication(value: Json | undefined, path: string): JsonObject {
  if (!isObject(value)) throw malformed(path);
  const id = stringField(value, "id", path, (item) => validText(item, MAX_ID));
  const publishedAt = stringField(value, "published_at", path, validTimestamp);
  const runId = stringField(value, "run_id", path, validRunId);
  const programRef = stringField(value, "program_ref", path, validProgramRef);
  const createdAt = stringField(value, "created_at", path, validTimestamp);
  const model = stringField(value, "model", path, validModel);
  stringField(value, "status", path, (item) => item === "completed");
  return { id, published_at: publishedAt, run_id: runId, program_ref: programRef, created_at: createdAt, model, status: "completed" };
}

export function publicManifest(value: Json | undefined): JsonObject {
  const path = "manifest";
  if (!isObject(value)) throw malformed(path);
  const runId = stringField(value, "run_id", path, validRunId);
  stringField(value, "status", path, (item) => item === "completed");
  const model = stringField(value, "model", path, validModel);
  const programRef = stringField(value, "program_ref", path, validProgramRef);
  const createdAt = stringField(value, "created_at", path, validTimestamp);
  const usage = value.usage;
  const input = isObject(usage) ? nonNegativeInteger(usage.input_tokens) : undefined;
  const output = isObject(usage) ? nonNegativeInteger(usage.output_tokens) : undefined;
  if (input === undefined || output === undefined) throw malformed("manifest.usage");
  const price = value.price_cents;
  if (typeof price !== "number" || !Number.isSafeInteger(price)) throw malformed("manifest.price_cents");
  const files = value.files;
  if (!Array.isArray(files) || files.length > MAX_MANIFEST_FILES || !files.every((file) => typeof file === "string" && validRelativePath(file))) {
    throw malformed("manifest.files");
  }
  const hasPatch = value.has_patch;
  if (typeof hasPatch !== "boolean") throw malformed("manifest.has_patch");
  const projected: JsonObject = {
    run_id: runId, status: "completed", model, program_ref: programRef, created_at: createdAt,
    usage: { input_tokens: input, output_tokens: output }, price_cents: price, files: [...files], has_patch: hasPatch,
  };
  const commit = value.spec_commit;
  if (commit !== undefined && commit !== null) {
    if (typeof commit !== "string" || !validText(commit, MAX_ID)) throw malformed("manifest.spec_commit");
    projected.spec_commit = commit;
  }
  return projected;
}

// ---------------------------------------------------------------- operations

async function list(context: Context): Promise<Json> {
  const [given, slug] = publicProgram(context.argument("OWNER/SLUG") ?? "");
  const count = limit(context);
  const owner = await ownerOf(context, given, slug, LIST_REVISIONS);
  const request: Request = { ...requestFor(context.operation, 0, resultsPath(owner, slug)), query: [["limit", String(count)]] };
  const body = jsonObject(await explainNotFound(context.send(request), () => `${owner}/${slug} is not a public program (it does not exist or is private)`));
  const rawContract = body.contract;
  if (!isObject(rawContract)) throw malformed("contract");
  const contract: JsonObject = {
    owner: stringField(rawContract, "owner", "contract", validOwner),
    slug: stringField(rawContract, "slug", "contract", validSlug),
    rev_id: stringField(rawContract, "rev_id", "contract", validRev),
  };
  const items = body.results;
  if (!Array.isArray(items) || items.length > MAX_LIST_ITEMS) throw malformed("results");
  const results = items.map((item, index) => publication(item, `results[${index}]`));
  context.human = humanList(contract, results);
  return { contract, results };
}

function downloadLimit(): number {
  const value = (operationsManifest.transportClasses.download as Record<string, unknown> | undefined)?.maxFileBytes;
  return typeof value === "number" ? value : 64 * 1024 * 1024;
}

/** Text as `common.schema.json#/$defs/text` admits it (TAB, LF and CR are the only controls). */
export function rawText(text: string): boolean {
  return [...text].length <= 1_048_576 && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(text);
}

function contentType(headers: Map<string, string>): string | undefined {
  const value = headers.get("content-type");
  return value !== undefined && validText(value, MAX_CONTENT_TYPE) ? value : undefined;
}

async function show(context: Context): Promise<Json> {
  const [givenOwner, slug] = publicProgram(context.argument("OWNER/SLUG") ?? "");
  const latest = context.flag("--latest");
  const raw = context.flag("--raw");
  const given = context.argument("PUBLICATION_ID");
  let requested: string | undefined;
  // No id, `latest` or --latest all read the newest publication.
  if (given !== undefined && given !== "latest" && latest) throw invocationFailure("give either <PUBLICATION_ID> or --latest, not both");
  if (given !== undefined && given !== "latest") requested = publicationId(context, given, programName(givenOwner, slug));
  const outputFile = context.option("--output-file");
  if (outputFile !== undefined && !raw) throw invocationFailure("--output-file writes the raw result bytes; add --raw");
  const owner = await ownerOf(context, givenOwner, slug, SHOW_REVISIONS);

  let descriptor: number | undefined;
  let outputPath: string | undefined;
  if (outputFile !== undefined) {
    descriptor = createNewFile(context.cwd, outputFile);
    outputPath = isAbsolute(outputFile) ? outputFile : resolve(context.cwd, outputFile);
  }
  let keep = false;
  try {
    const base = resultsPath(owner, slug);
    const detail = requested !== undefined
      ? requestFor(context.operation, SHOW_BY_ID, `${base}/${encodeSegment(requested)}`)
      : requestFor(context.operation, SHOW_LATEST, `${base}/latest`);
    const listCommand = command(context, `result list ${owner}/${slug}`);
    const body = jsonObject(await explainNotFound(context.send(detail), () => requested !== undefined
      ? `no publication ${requested} of ${owner}/${slug}; list them with \`${listCommand}\``
      : `${owner}/${slug} has no published results or is not a public program; check with \`${listCommand}\``));
    const summary = publication(body.publication, "publication");
    const id = summary.id as string;
    if (requested !== undefined && requested !== id) throw malformed("publication.id");
    if (!raw) {
      const manifest = publicManifest(body.manifest);
      context.human = humanShow(summary, manifest);
      return { publication: summary, manifest };
    }
    if (!validPublicationId(id)) throw malformed("publication.id");
    const request: Request = { ...requestFor(context.operation, SHOW_RAW, `${base}/${encodeSegment(id)}/raw`), class: "download" };
    const written: JsonObject = { written: descriptor !== undefined };
    if (descriptor !== undefined) {
      const fd = descriptor;
      const downloaded = await context.download(request, { write(bytes) { writeSync(fd, bytes); } }, downloadLimit());
      try { fsyncSync(fd); } catch { throw invocationFailure("cannot write the output file"); }
      written.bytes = downloaded.bytes;
      written.sha256 = downloaded.sha256;
      const type = contentType(downloaded.headers);
      if (type !== undefined) written.contentType = type;
      keep = true;
      context.human = humanWritten(summary, written);
    } else {
      const cap = maxResponseBytes("control");
      const chunks: Uint8Array[] = [];
      let downloaded;
      try { downloaded = await context.download(request, { write(bytes) { chunks.push(bytes.slice()); } }, cap); }
      catch (caught) {
        if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESPONSE_TOO_LARGE") {
          throw failure("SERVICE_RESPONSE_TOO_LARGE", { ...(caught.details ?? {}), reason: `the raw result is larger than ${cap} bytes; rerun with --output-file FILE` });
        }
        throw caught;
      }
      written.bytes = downloaded.bytes;
      written.sha256 = downloaded.sha256;
      const type = contentType(downloaded.headers);
      if (type !== undefined) written.contentType = type;
      let text: string | undefined;
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)); } catch { text = undefined; }
      if (text !== undefined && !rawText(text)) text = undefined;
      if (text !== undefined) written.content = text;
      context.human = text ?? humanWritten(summary, written);
    }
    return { publication: summary, raw: written };
  } finally {
    if (descriptor !== undefined) {
      try { closeSync(descriptor); } catch { /* already closed */ }
      if (!keep && outputPath !== undefined) {
        try { unlinkSync(outputPath); } catch { /* best effort */ }
      }
    }
  }
}

async function publish(context: Context): Promise<Json> {
  const reference = ownSlugReference(context);
  const runId = context.option("--run") ?? "";
  if (!validRunId(runId)) throw invocationFailure(`--run ${quoted(runId)} must be a run id (run_…); find completed runs with \`${command(context, "run list")}\``);
  const owner = await confirmOwnSlug(context, reference, OWNER_CHECK, false);
  const slug = reference.slug;
  const path = `/programs/${encodeSegment(slug)}/results`;
  const request = withJsonBody(requestFor(context.operation, 0, path), { run_id: runId });
  const gate = context.gate(context.planned(0, path, [], request.body));
  if (gate.kind === "preview") return gate.result;
  const response = await context.sendRaw(request);
  if (response.status < 200 || response.status >= 300) throw explainPublishFailure(context.classify(request, response), response, context, owner, slug, runId);
  const body = jsonObject(response);
  const summary = publication(body.publication, "publication");
  context.human = humanPublished(context.environment, summary);
  return { publication: summary };
}

function explainPublishFailure(error: RunnerFailure, response: Response, hint: Hint, owner: string, slug: string, runId: string): RunnerFailure {
  let code: string | undefined;
  try {
    const body = jsonObject(response);
    code = typeof body.code === "string" ? body.code : undefined;
  } catch { code = undefined; }
  let reason: string;
  // Never a --yes: making a program public exposes its source, which is the owner's decision.
  if (response.status === 409 && code === "program_not_public") reason = `program ${quoted(slug)} is private and only public programs can publish results; making it public exposes its source to everyone and is the owner's decision; review that change with \`${command(hint, `program visibility ${slug} public --preview`)}\``;
  else if (response.status === 409 && code === "run_not_completed") reason = `run ${runId} did not complete; only completed runs can be published`;
  else if (response.status === 409 && code === "program_ref_mismatch") reason = `run ${runId} was not run from a saved revision of program ${quoted(slug)}; publish a run started with \`${command(hint, `run submit --from ${programName(owner, slug)}`)}\``;
  else if (response.status === 409 && code === "canonical_result_missing") reason = `run ${runId} has no outputs/result.json to publish`;
  else if (response.status === 404 && code === "run_not_found") {
    reason = `no run ${runId} in this account; find completed runs with \`${command(hint, "run list")}\``;
    return notFoundError(failure(error.code, { ...(error.details ?? {}), reason }), hint.environment, hint.mode, "run", runId, ["run", "list"]);
  }
  else if (response.status === 404) reason = `no program ${quoted(slug)} owned by this account; list yours with \`${command(hint, "program list")}\``;
  else return error;
  return failure(error.code, { ...(error.details ?? {}), reason });
}

async function unpublish(context: Context): Promise<Json> {
  const reference = ownSlugReference(context);
  const id = publicationId(context, context.argument("PUBLICATION_ID") ?? "", programName(reference.owner, reference.slug));
  const [slug, owner] = [reference.slug, await confirmOwnSlug(context, reference, OWNER_CHECK, false)];
  const path = `/programs/${encodeSegment(slug)}/results/${encodeSegment(id)}`;
  const gate = context.gate(context.planned(0, path));
  if (gate.kind === "preview") return gate.result;
  const body = jsonObject(await explainNotFound(context.send(requestFor(context.operation, 0, path)),
    () => `no publication ${id} on your program ${quoted(slug)}; list them with \`${command(context, `result list ${programName(owner, slug)}`)}\``));
  if (body.unpublished !== true) throw malformed("unpublished");
  const returned = body.id;
  if (typeof returned !== "string" || !validText(returned, MAX_ID)) throw malformed("id");
  context.human = `Unpublished ${humanSafeScalar(returned)}\n`;
  return { unpublished: true, id: returned };
}

// ---------------------------------------------------------------- human text

const text = (value: Json | undefined): string => humanSafeScalar(typeof value === "string" ? value : "");

function humanList(contract: JsonObject, results: JsonObject[]): string {
  const reference = `${text(contract.owner)}/${text(contract.slug)}@${text(contract.rev_id)}`;
  if (results.length === 0) return `No published results for ${reference}.\n`;
  let out = `${reference}: `;
  out += results.length === 1 ? "1 published result\n" : `${results.length} published results\n`;
  for (const item of results) out += `${text(item.id)}  ${text(item.published_at)}  ${text(item.run_id)}  ${text(item.model)}\n`;
  return out;
}

function humanPublication(summary: JsonObject): string {
  return `Publication ${text(summary.id)} of ${text(summary.program_ref)}\nPublished: ${text(summary.published_at)}\nRun: ${text(summary.run_id)} (${text(summary.model)}, ${text(summary.created_at)})\n`;
}

function humanShow(summary: JsonObject, manifest: JsonObject): string {
  const usage = manifest.usage as JsonObject;
  const files = (manifest.files as Json[]).map(text).join(", ");
  return `${humanPublication(summary)}Price: ${dollarsOrDash(manifest.price_cents)}\nTokens: ${String(usage.input_tokens)} in, ${String(usage.output_tokens)} out\nFiles: ${files}\n`;
}

function humanWritten(summary: JsonObject, written: JsonObject): string {
  let out = `${humanPublication(summary)}Raw result: ${String(written.bytes)} bytes, sha256 ${text(written.sha256)}\n`;
  out += written.written === true ? "Wrote the raw result to the output file.\n" : "The raw result is not text; rerun with --output-file FILE to save it.\n";
  return out;
}

function humanPublished(environment: Environment, summary: JsonObject): string {
  const reference = typeof summary.program_ref === "string" ? summary.program_ref : "";
  const at = reference.indexOf("@");
  const program = at >= 0 ? humanSafeScalar(reference.slice(0, at)) : "";
  return `Published ${text(summary.id)} for ${text(summary.run_id)} on ${text(summary.program_ref)}\nPublic: ${command({ environment, mode: "human" }, `result show ${program} ${text(summary.id)}`)}\n`;
}
