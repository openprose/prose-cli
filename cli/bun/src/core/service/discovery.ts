// Service discovery: `cli service status`, `cli model list`,
// `cli example list|show` and `cli repo list`. Mirrors
// cli/rust/crates/prose-runner-core/src/service/discovery.rs: fields are
// validated in the same fixed order so SERVICE_PROTOCOL_INVALID reasons match.
// Decisions: docs/service/discovery.md.
import { createHash } from "node:crypto";
import { failure, invocationFailure } from "../errors";
import { humanSafeScalar, quote } from "../output";
import { RunnerFailure, type OutputMode } from "../types";
import { checkNewFile, writeNewFile } from "./fs";
import { jsonObject, requestFor, type Response } from "./http";
import type { Context } from "./index";
import { didYouMean, type Environment, type Json, type JsonObject } from "./manifest";
import { argvText, followUpArgv, nextLine, sanitizeServiceMessage, validText } from "./render";
import * as triage from "./triage";

export async function execute(context: Context): Promise<Json> {
  switch (context.operation.id) {
    case "service.status": return await serviceStatus(context);
    case "service.triage": return await triage.execute(context);
    case "model.list": return await modelList(context);
    case "example.list": return await exampleList(context);
    case "example.show": return await exampleShow(context);
    case "repo.list": return await repoList(context);
    default: return await context.notImplemented();
  }
}

// ---------------------------------------------------------------------------
// Response validation

export class Shape {
  constructor(readonly what: string) {}

  fail(field: string): RunnerFailure {
    return failure("SERVICE_PROTOCOL_INVALID", { reason: `unexpected ${this.what} response: ${field}` });
  }

  object(response: Response): JsonObject {
    try { return jsonObject(response); } catch { throw this.fail("body"); }
  }

  field(object: JsonObject, key: string, path: string): Json {
    if (!Object.hasOwn(object, key)) throw this.fail(path);
    return object[key]!;
  }

  text(value: Json, max: number, path: string): string {
    if (typeof value !== "string" || !plain(value, max)) throw this.fail(path);
    return value;
  }

  model(value: Json, path: string): string {
    if (typeof value !== "string" || !modelId(value)) throw this.fail(path);
    return value;
  }

  list(value: Json, max: number, path: string): Json[] {
    if (!Array.isArray(value) || value.length > max) throw this.fail(path);
    return value;
  }

  map(value: Json, path: string): JsonObject {
    if (value === null || typeof value !== "object" || Array.isArray(value)) throw this.fail(path);
    return value;
  }
}

function codePoints(value: string): number { let count = 0; for (const _ of value) count += 1; return count; }

function plain(value: string, max: number): boolean {
  return codePoints(value) <= max && !/[\u0000-\u001f\u007f]/u.test(value);
}

const modelId = (value: string): boolean => /^[a-z0-9][a-z0-9.-]{0,63}$/u.test(value);
const slug = (value: string): boolean => /^[a-z0-9][a-z0-9-]{0,63}$/u.test(value);
const integer = (value: Json): number | undefined => (typeof value === "number" && Number.isSafeInteger(value) ? value : undefined);
/** Byte-wise ordering (Rust `String` order) for ASCII-dominated keys. */
function byteCompare(left: string, right: string): number {
  const a = Buffer.from(left, "utf8");
  const b = Buffer.from(right, "utf8");
  return Buffer.compare(a, b);
}

// ---------------------------------------------------------------------------
// service status

/**
 * `cli service status`: whether the service answers and which models you can
 * run. Only GET /health; every other field of the body is ignored, never
 * validated or echoed.
 */
async function serviceStatus(context: Context): Promise<Json> {
  const health = await context.send(requestFor(context.operation, 0, "/health"));
  const body = new Shape("service status").object(health);
  const result = projectHealth(body);
  result.environments = healthEnvironments(body);
  context.human = statusHuman(result, context.environment);
  return result;
}

/** The user-facing /health fields: status, models and default_model, validated in that order. */
export function projectHealth(body: JsonObject): JsonObject {
  const shape = new Shape("service status");
  const status = shape.text(shape.field(body, "status", "status"), 32, "status");
  const models = shape.list(shape.field(body, "models", "models"), 64, "models").map((item) => shape.model(item, "models"));
  const fallback = shape.model(shape.field(body, "default_model", "default_model"), "default_model");
  return { status, models, default_model: fallback };
}

/**
 * The environment ids /health offers (`environments.available`), the ones
 * `run submit --environment` and `run quote --environment` accept; empty when
 * /health lists none (mirrors Rust `health_environments`).
 */
export function healthEnvironments(body: JsonObject): string[] {
  const environments = body.environments;
  const list = environments !== null && typeof environments === "object" && !Array.isArray(environments) ? environments.available : undefined;
  if (list === undefined) return [];
  const shape = new Shape("service status");
  return shape.list(list, 64, "environments.available").map((item) => {
    if (typeof item !== "string" || !/^[a-z0-9][a-z0-9_-]{0,63}$/u.test(item)) throw shape.fail("environments.available");
    return item;
  });
}

function strings(value: Json | undefined): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").map(humanSafeScalar) : [];
}

function joined(items: string[]): string { return items.length === 0 ? "(none)" : items.join(", "); }

function textOf(value: Json | undefined): string { return humanSafeScalar(typeof value === "string" ? value : ""); }

function statusHuman(result: JsonObject, environment: Environment): string {
  let text = `Service: ${textOf(result.status)}\n`;
  text += `Models: ${joined(strings(result.models))} (default ${textOf(result.default_model)})\n`;
  const environments = strings(result.environments);
  if (environments.length > 0) text += `Environments: ${environments.join(", ")}\n`;
  return text + nextLine(environment, ["service", "triage"]);
}

// ---------------------------------------------------------------------------
// model list

async function modelList(context: Context): Promise<Json> {
  const response = await context.send(requestFor(context.operation, 0, "/models"));
  const shape = new Shape("model list");
  const result = projectModels(shape, shape.object(response));
  context.human = modelsHuman(result, context.environment);
  return result;
}

/**
 * The models this account may run, the default and, when the service sends
 * one, the `catalog` of every model with its status (see {@link catalog});
 * every other field is ignored.
 */
export function projectModels(shape: Shape, body: JsonObject): JsonObject {
  const models = shape.list(shape.field(body, "models", "models"), 64, "models").map((item) => shape.model(item, "models"));
  const result: JsonObject = { models, default_model: shape.model(shape.field(body, "default_model", "default_model"), "default_model") };
  const entries = catalog(body);
  if (entries !== undefined) result.catalog = entries;
  return result;
}

/** The catalog status of a model that unlocks with any wallet top-up. */
const PREMIUM = "paid_top_up";
/** The catalog status of a model the service still accepts but no longer offers. */
const HIDDEN = "hidden";
/** The catalog status of a retired model; never shown. */
const DEPRECATED = "deprecated";

/** A catalog status or tier: `^[a-z][a-z0-9_]{0,31}$`. */
const token = (value: unknown): value is string => typeof value === "string" && /^[a-z][a-z0-9_]{0,31}$/u.test(value);
const isModelId = (value: unknown): value is string => typeof value === "string" && modelId(value);

/**
 * `/models` `catalog` through an explicit allowlist (mirrors Rust `catalog`):
 * each entry's `id` and `status`, plus `tier`, `summary` and `successor` when
 * valid. A missing or non-list catalog is undefined; the first 64 entries are
 * read; an entry without a model id or status token, a repeated id or a
 * `deprecated` model is skipped; an invalid optional field is left out.
 */
function catalog(body: JsonObject): JsonObject[] | undefined {
  const entries = body.catalog;
  if (!Array.isArray(entries)) return undefined;
  const projected: JsonObject[] = [];
  for (const raw of entries.slice(0, 64)) {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) continue;
    const { id, status } = raw;
    if (!isModelId(id) || !token(status)) continue;
    if (status === DEPRECATED || projected.some((known) => known.id === id)) continue;
    const item: JsonObject = { id, status };
    if (token(raw.tier)) item.tier = raw.tier;
    const summary = raw.summary;
    if (typeof summary === "string" && summary.trim().length > 0 && plain(summary, 512)) item.summary = summary;
    if (isModelId(raw.successor)) item.successor = raw.successor;
    projected.push(item);
  }
  return projected;
}

/** The `cli` words of the top-up preview that unlocks premium models. */
const TOPUP = ["wallet", "topup", "--amount-cents", "500", "--preview"] as const;

/**
 * Readable `model list` output, ending with a copyable `Next:` command
 * (mirrors Rust `models_human`): one model per line without a catalog;
 * with one, sections for available, premium and still-accepted models.
 */
function modelsHuman(result: JsonObject, environment: Environment): string {
  const fallback = result.default_model as string;
  const models = result.models as string[];
  const entries = result.catalog as JsonObject[] | undefined;
  if (entries === undefined) {
    let text = models.length === 0 ? "No models.\n" : "";
    text += models.map((model) => `${humanSafeScalar(model)}${model === fallback ? " (default)" : ""}\n`).join("");
    if (!models.includes(fallback)) text += `Default: ${humanSafeScalar(fallback)}\n`;
    return text + nextLine(environment, ["run", "quote"]);
  }
  let text = "Available:\n";
  if (models.length === 0) text += "  (none)\n";
  text += models.map((model) => `  ${humanSafeScalar(model)}${model === fallback ? " (default)" : ""}\n`).join("");
  if (!models.includes(fallback)) text += `  Default: ${humanSafeScalar(fallback)}\n`;
  const premium = entries.filter((entry) => entry.status === PREMIUM);
  if (premium.length > 0) {
    text += "Premium — unlocks with any wallet top-up:\n";
    for (const entry of premium) {
      const id = humanSafeScalar(entry.id as string);
      text += typeof entry.summary === "string" ? `  ${id}: ${humanSafeScalar(entry.summary)}\n` : `  ${id}\n`;
    }
    text += nextLine(environment, TOPUP);
  }
  const hidden = entries.filter((entry) => entry.status === HIDDEN);
  if (hidden.length > 0) {
    text += "Also accepted (not recommended):\n";
    for (const entry of hidden) {
      const id = humanSafeScalar(entry.id as string);
      text += typeof entry.successor === "string" ? `  ${id} → use ${humanSafeScalar(entry.successor)}\n` : `  ${id}\n`;
    }
  }
  return text + nextLine(environment, ["run", "quote"]);
}

/**
 * Service codes that refuse a premium model until the wallet has been topped
 * up: `paid_top_up_required` (HTTP 402), and `paid_model_required` (HTTP 403)
 * from older servers.
 */
const PAID_CODES: readonly string[] = ["paid_top_up_required", "paid_model_required"];

/**
 * Explains a classified service refusal of a premium model (mirrors Rust
 * `paid_model_refusal`): the body's `model`, `tier` and `summary` become
 * details.model, details.tier and details.reason, and the Action (also
 * details.suggestedArgv) previews a wallet top-up.
 */
export function paidModelRefusal(error: RunnerFailure, body: Uint8Array, environment: Environment, mode: OutputMode, credential: string | undefined): RunnerFailure {
  let parsed: unknown;
  try { parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)); } catch { return error; }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return error;
  const object = parsed as JsonObject;
  if (typeof object.code !== "string" || !PAID_CODES.includes(object.code)) return error;
  const summary = typeof object.summary === "string" ? sanitizeServiceMessage(object.summary, credential) : undefined;
  return premiumRefusal(error, isModelId(object.model) ? object.model : undefined, token(object.tier) ? object.tier : undefined, summary, environment, mode);
}

/** The premium-model refusal predicted from the `/models` catalog before any request (mirrors Rust `premium_in_catalog`). */
export function premiumInCatalog(body: JsonObject, model: string, environment: Environment, mode: OutputMode): RunnerFailure | undefined {
  const entry = catalog(body)?.find((item) => item.id === model);
  if (entry?.status !== PREMIUM) return undefined;
  return premiumRefusal(failure("SERVICE_PREMIUM_MODEL_LOCKED"), model, entry.tier as string | undefined, entry.summary as string | undefined, environment, mode);
}

/** Whether the `/models` catalog lists `model` as one the service accepts (any status but premium). */
export function acceptedInCatalog(body: JsonObject, model: string): boolean {
  return catalog(body)?.some((entry) => entry.id === model && entry.status !== PREMIUM) ?? false;
}

function premiumRefusal(error: RunnerFailure, model: string | undefined, tier: string | undefined, summary: string | undefined, environment: Environment, mode: OutputMode): RunnerFailure {
  const reason = model !== undefined
    ? summary !== undefined ? `${model} is a premium model: ${summary}` : `${model} is a premium model; it unlocks with any wallet top-up.`
    : summary !== undefined ? `This model is premium: ${summary}` : "This model is premium; it unlocks with any wallet top-up.";
  const argv = followUpArgv(environment, mode, TOPUP);
  return new RunnerFailure({
    code: error.code, boundary: error.boundary, message: error.message,
    action: `Top up the wallet with any amount to unlock premium models; preview a top-up with \`${argvText(argv)}\`.`,
    exitCode: error.exitCode, retryable: error.retryable,
    details: {
      ...(error.details ?? {}),
      reason,
      ...(model === undefined ? {} : { model }),
      ...(tier === undefined ? {} : { tier }),
      suggestedArgv: argv,
    },
  });
}

// ---------------------------------------------------------------------------
// example list / show

const EXAMPLE_PREFIX = "/examples/private/";

/**
 * The same-origin request path for an example `source`, or null when the
 * source is outside the selected origin or is not a private-example route.
 */
export function exampleSourcePath(source: string, origin: string): string | null {
  let path: string;
  if (source.startsWith("/") && !source.startsWith("//")) path = source;
  else if (source.startsWith(origin) && source.slice(origin.length).startsWith("/")) path = source.slice(origin.length);
  else return null;
  if (!path.startsWith(EXAMPLE_PREFIX)) return null;
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(path.slice(EXAMPLE_PREFIX.length)) ? path : null;
}

function projectExamples(shape: Shape, body: JsonObject, origin: string): JsonObject[] {
  return shape.list(shape.field(body, "examples", "examples"), 256, "examples").map((raw) => {
    const item = shape.map(raw, "examples");
    const id = shape.field(item, "id", "examples.id");
    if (typeof id !== "string" || !slug(id)) throw shape.fail("examples.id");
    const label = shape.text(shape.field(item, "label", "examples.label"), 256, "examples.label");
    const source = shape.field(item, "source", "examples.source");
    if (typeof source !== "string") throw shape.fail("examples.source");
    const path = exampleSourcePath(source, origin);
    // The web page of an example published outside the service, when the
    // service names one: an https URL without spaces or controls.
    const web = item.web_url;
    const web_url = typeof web === "string" && webUrl(web) ? web : null;
    // Where a person can read an example `example show` cannot fetch
    // (EXAMPLE_NOT_VIEWABLE details.webUrl): the service's web page, else the
    // source itself when it is an https address.
    const read_at = path !== null ? null : web_url ?? (webUrl(source) ? source : null);
    return { id, label, source: path, web_url, read_at };
  });
}

/** An example's web page URL: `https://` plus a host, at most 2048 printable ASCII characters (mirrors Rust `web_url`). */
function webUrl(url: string): boolean {
  const rest = url.startsWith("https://") ? url.slice("https://".length) : undefined;
  return url.length <= 2048 && /^[\x21-\x7e]*$/u.test(url) && rest !== undefined && rest.length > 0 && !rest.startsWith("/");
}

async function fetchExamples(context: Context): Promise<JsonObject[]> {
  const response = await context.send(requestFor(context.operation, 0, "/examples/private"));
  const shape = new Shape("example list");
  return projectExamples(shape, shape.object(response), context.environment.origin);
}

async function exampleList(context: Context): Promise<Json> {
  const examples = await fetchExamples(context);
  let text = examples.length === 0 ? "No examples.\n" : "";
  for (const example of examples) {
    const suffix = example.source !== null ? "" : typeof example.web_url === "string" ? ` (view on the web: ${humanSafeScalar(example.web_url)})` : " (view on the web)";
    text += `${textOf(example.id)}: ${textOf(example.label)}${suffix}\n`;
  }
  context.human = text;
  // The public listing: each example's id and label; one that `cli example
  // show` cannot fetch is `viewOnWeb`, with its `webUrl` when the service
  // names one. The service route stays internal (mirrors Rust).
  return {
    examples: examples.map((example) => {
      const item: JsonObject = { id: example.id!, label: example.label! };
      if (example.source === null) {
        item.viewOnWeb = true;
        if (typeof example.web_url === "string") item.webUrl = example.web_url;
      }
      return item;
    }) as Json,
  };
}

/** UTF-8 without C0 controls other than TAB, LF and CR, and without DEL. */
export function exampleText(bytes: Uint8Array): string | undefined {
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); } catch { return undefined; }
  return /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(text) ? undefined : text;
}

/** Fails before any request when `--output-file` cannot be created (fs.checkNewFile rule). */
function checkOutputFile(cwd: string, value: string): void {
  checkNewFile(cwd, value);
}

/**
 * The listed example `given` names: its id, else the id or label compared
 * case-insensitively; a label must name exactly one example.
 */
export function findExample(examples: JsonObject[], given: string): JsonObject | undefined {
  const exact = examples.find((item) => item.id === given);
  if (exact !== undefined) return exact;
  const key = given.trim().toLowerCase();
  const byId = examples.find((item) => typeof item.id === "string" && item.id.toLowerCase() === key);
  if (byId !== undefined) return byId;
  const byLabel = examples.filter((item) => typeof item.label === "string" && item.label.trim().toLowerCase() === key);
  return byLabel.length === 1 ? byLabel[0] : undefined;
}

async function exampleShow(context: Context): Promise<Json> {
  const given = context.argument("NAME") ?? "";
  if (!validText(given, 256)) throw invocationFailure(`example name ${quote(given)} is invalid; use an id from \`${context.command("example list")}\``);
  const outputFile = context.option("--output-file");
  if (outputFile !== undefined) checkOutputFile(context.cwd, outputFile);
  const examples = await fetchExamples(context);
  const example = findExample(examples, given);
  if (example === undefined) {
    const near = didYouMean(given.toLowerCase(), examples.map((item) => item.id as string));
    const details: JsonObject = near === undefined
      ? { reason: `no example named ${quote(given)}; list the available ids with \`${context.command("example list")}\`` }
      : {
        reason: `no example named ${quote(given)}; did you mean ${quote(near)}? Show it with \`${context.command(`example show ${near}`)}\`, or list the available ids with \`${context.command("example list")}\``,
        suggestedArgv: context.followUpArgv(["example", "show", near]),
      };
    throw failure("SERVICE_RESOURCE_NOT_FOUND", details);
  }
  const name = example.id as string;
  if (name !== given && context.mode === "human") context.err(`Using example ${name} (${humanSafeScalar(example.label as string)}) for ${humanSafeScalar(given)}.\n`);
  if (typeof example.source !== "string") {
    throw failure("EXAMPLE_NOT_VIEWABLE", {
      reason: `example ${quote(name)} is published outside the OpenProse service, so its source cannot be shown here; view it on the web`,
      ...(typeof example.read_at === "string" ? { webUrl: example.read_at } : {}),
    });
  }
  const response = await context.send(requestFor(context.operation, 1, example.source));
  const text = exampleText(response.body);
  if (text === undefined) throw new Shape("example").fail("body");
  const bytes = response.body.byteLength;
  const sha256 = createHash("sha256").update(response.body).digest("hex");
  const result: JsonObject = { id: name, label: example.label!, bytes, sha256, written: outputFile !== undefined };
  if (outputFile !== undefined) {
    writeNewFile(context.cwd, outputFile, response.body);
    context.human = `Wrote ${bytes} bytes to ${humanSafeScalar(outputFile)} (sha256 ${sha256})\n`;
  } else {
    result.content = text;
    context.human = text;
  }
  return result;
}

// ---------------------------------------------------------------------------
// repo list

export function projectRepositories(shape: Shape, body: JsonObject): JsonObject[] {
  const installations = shape.list(shape.field(body, "installations", "installations"), Number.MAX_SAFE_INTEGER, "installations");
  const repositories: Array<{ fullName: string; id: number; value: JsonObject }> = [];
  for (const rawInstallation of installations) {
    const installation = shape.map(rawInstallation, "installations");
    const repos = shape.list(shape.field(installation, "repos", "installations.repos"), Number.MAX_SAFE_INTEGER, "installations.repos");
    for (const rawRepo of repos) {
      const repo = shape.map(rawRepo, "installations.repos");
      const id = integer(shape.field(repo, "id", "installations.repos.id"));
      if (id === undefined) throw shape.fail("installations.repos.id");
      const name = shape.text(shape.field(repo, "name", "installations.repos.name"), 100, "installations.repos.name");
      const fullName = shape.text(shape.field(repo, "full_name", "installations.repos.full_name"), 200, "installations.repos.full_name");
      const isPrivate = shape.field(repo, "private", "installations.repos.private");
      if (typeof isPrivate !== "boolean") throw shape.fail("installations.repos.private");
      const value: JsonObject = { id, name, full_name: fullName, private: isPrivate };
      if (repo.default_branch !== undefined && repo.default_branch !== null) {
        value.default_branch = shape.text(repo.default_branch, 256, "installations.repos.default_branch");
      }
      if (!repositories.some((known) => known.id === id)) repositories.push({ fullName, id, value });
    }
  }
  if (repositories.length > 10_000) throw shape.fail("installations.repos");
  repositories.sort((left, right) => byteCompare(left.fullName, right.fullName) || left.id - right.id);
  return repositories.map((entry) => entry.value);
}

async function repoList(context: Context): Promise<Json> {
  const response = await context.send(requestFor(context.operation, 0, "/repos"));
  const shape = new Shape("repository list");
  const repositories = projectRepositories(shape, shape.object(response));
  let text = repositories.length === 0 ? "No repositories.\n" : "";
  for (const repo of repositories) {
    const visibility = repo.private === true ? "private" : "public";
    const branch = typeof repo.default_branch === "string" ? `, default branch ${humanSafeScalar(repo.default_branch)}` : "";
    text += `${textOf(repo.full_name)} (${visibility}${branch})\n`;
  }
  context.human = text;
  return { repositories };
}
