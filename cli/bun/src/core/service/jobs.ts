// Service job operations: `job list|show|create|update|
// configure|delete|deliveries|rotate-secret` and `job contract
// list|attach|detach`.
//
// - Job specs and configurations come from `--spec-file` / `--config-file` (a
//   path or `-`): a UTF-8 JSON object of at most 64 KiB, checked locally
//   (object, size, `type` on create, `interval_seconds` 60..2,678,400, no
//   result-only camelCase names or `inputEntries` in a configuration) before
//   any request and then sent byte for byte; the service validates the rest.
// - Configured inputs named like /cost/i are listed in
//   `run_configuration.input_entries` as {name, value}.
// - Every job result is built from its public field list, in snake_case:
//   `job` (not the service's trigger), `max_jobs`, `job_limit`, the opaque
//   `revision_token`, and run counts folded into the user states `queued`,
//   `running`, `completed`, `failed`, `cancelled` and `awaiting_billing`. Any
//   other service field (internal references, adoption and driver detail,
//   repository detail objects, receiver/reply details) is dropped. Every
//   epoch-ms time `x_at` gains an additive `x_at_iso` (RFC 3339 UTC).
// - `job configure --config-file` takes the same snake_case names
//   (`revision_token`, `run_configuration.context_repositories`); the product
//   translates them to the service's names before sending.
// - The webhook `signing_secret` and `endpoint` appear only in the result of
//   `job create` and `job rotate-secret`, never in stderr; human mode prints a
//   stderr warning without the secret. Both results, and `job show` when the
//   service's endpoint is the secret-free job-id webhook path, carry the
//   absolute `endpoint_url` built from the environment origin.
// - `job contract attach|detach` take a pinned `OWNER/SLUG@REV`. Attach
//   options set a webhook binding's run settings (other job types are
//   refused after reading the job). The job's listed contracts are always
//   read first, so a re-attach keeps a bound program's saved settings: a
//   service that lists `environment` merges a same-ref re-bind itself and is
//   sent only the changes; otherwise the saved settings are merged here. A
//   plan (--preview, or no --yes) also reads the job and carries an advisory
//   quote for the binding as it will run.
// - `job contract list` adds each binding's saved settings as
//   `run_configuration`, never the program text or file content.
//
// Mirrors cli/rust/crates/prose-runner-core/src/service/jobs.rs.
import { failure, invocationFailure } from "../errors";
import { humanSafeScalar, quote as quoteText } from "../output";
import { RunnerFailure } from "../types";
import { readSource } from "./fs";
import { encodeSegment, holdQuery, jsonObject, parseJson, requestFor, type Request } from "./http";
import type { Context } from "./index";
import { didYouMean, type Environment, type Json, type JsonObject } from "./manifest";
import { parseOwnAllowed, parseProgramRef, pinned, resolveToRun, validSlug } from "./program-ref";
import { absoluteUrl, addIso, argvText, canonicalJson, isoMs, nextLine, usdCents, validText } from "./render";
import { commitNotRead, modelOption, parseInputs, parseRepository, repositoryUrl, sameRepository, tokenOption, validInputKey, type Repository } from "./runs";

/** Largest job spec or configuration file. */
export const SPEC_MAX_BYTES = 65_536;
/** Schedule cadence bounds enforced by the service (`validateScheduleCadence`). */
export const INTERVAL_MIN = 60;
export const INTERVAL_MAX = 2_678_400;
const SECRET_WARNING = "Warning: the signing secret printed on stdout is shown only once. Store it now; `cli job rotate-secret` replaces it.\n";

export async function execute(context: Context): Promise<Json> {
  switch (context.invocation.operation) {
    case "job.list": return await list(context);
    case "job.show": return await show(context);
    case "job.create": return await create(context);
    case "job.update": return await update(context, false);
    case "job.configure": return await update(context, true);
    case "job.delete": return await remove(context);
    case "job.deliveries": return await deliveries(context);
    case "job.rotate-secret": return await rotateSecret(context);
    case "job.contract.list": return await contractList(context);
    case "job.contract.attach": return await contractAttach(context);
    case "job.contract.detach": return await contractDetach(context);
    default: return await context.notImplemented();
  }
}

function protocol(field: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason: `unexpected job response: ${field}` });
}

/** Rust's `{:?}` of a plain string (the file names in local errors). */
function quoted(value: string): string { return quoteText(value); }

// ------------------------------------------------------------------ inputs

/** The JOB_ID argument: 1..256 code points, no control characters. */
function jobId(context: Context): string {
  const id = context.argument("JOB_ID") ?? "";
  if (validText(id, 256)) return id;
  throw invocationFailure("job id must be 1 to 256 characters without control characters; list ids with `cli job list`");
}

/** An exact integer; a zero-fraction float counts, as in `Number.isSafeInteger`. */
export function asInteger(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : undefined;
}

function intervalReason(): string {
  return `interval_seconds must be an integer from ${INTERVAL_MIN} to ${INTERVAL_MAX}`;
}

function finiteNumbers(value: unknown): boolean {
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(finiteNumbers);
  if (value !== null && typeof value === "object") return Object.values(value).every(finiteNumbers);
  return true;
}

function resultOnly(prefix: string, snake: string, camel: string): string {
  return `the request field is ${prefix}${snake} (snake_case), not ${camel}`;
}

/** camelCase -> snake_case: the public name of a service field, and the request name of a camelCase key. */
function snakeCase(key: string): string {
  return Array.from(key).map((character) => (/^[A-Z]$/u.test(character) ? `_${character.toLowerCase()}` : character)).join("");
}

/**
 * The request name of a key: its snake_case form, with the service's name of
 * the opaque configuration token mapped to `revision_token` (mirrors Rust
 * `request_name`).
 */
function requestName(key: string): string {
  const snake = snakeCase(key);
  return snake === "configuration_token" ? "revision_token" : snake;
}

/** "a, b and c". */
function andList(items: readonly string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]!}`;
}

const byCodeUnit = (left: string, right: string): number => (left < right ? -1 : left > right ? 1 : 0);

type SpecField = string | { enum: string[] } | { object: SpecObject } | { array: SpecObject };
interface SpecObject { required: string[]; keys: Record<string, SpecField> }
interface JobSpecSchema {
  option: string; discriminator?: string; common?: Record<string, SpecField>; variants?: Record<string, SpecObject>;
  unpaidWhen?: { type: string; absent: string }; minKeys?: number; required?: string[]; keys?: Record<string, SpecField>;
}

function specSchema(context: Context): JobSpecSchema {
  return (context.operation as unknown as { spec: JobSpecSchema }).spec;
}

/**
 * The accepted key nearest to `name`, compared without case, `_` and `-` (so
 * `intervalSecond` finds `interval_seconds`), within the manifest's
 * did-you-mean distance.
 */
function nearestKey(name: string, keys: Record<string, SpecField>): string | undefined {
  const fold = (text: string): string => Array.from(text).filter((character) => character !== "_" && character !== "-").map((character) => character.toLowerCase()).join("");
  const target = fold(name);
  const folded = Object.keys(keys).map((key) => [fold(key), key] as const);
  const exact = folded.find(([text]) => text === target);
  if (exact !== undefined) return exact[1];
  const found = didYouMean(target, folded.map(([text]) => text));
  return found === undefined ? undefined : folded.find(([text]) => text === found)?.[1];
}

function jsonType(value: Json): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "object") return "object";
  return typeof value;
}

const isObject = (value: Json | undefined): value is JsonObject => value !== null && typeof value === "object" && !Array.isArray(value);

/** The closed-schema walk over a job spec; every violation is collected, in key order. */
class SpecCheck {
  readonly violations: string[] = [];
  constructor(private readonly context: Context) {}

  object(object: JsonObject, keys: Record<string, SpecField>, required: readonly string[], path: string, label: string, hint: string): void {
    const covered: string[] = [];
    for (const name of Object.keys(object).sort(byCodeUnit)) {
      const value = object[name]!;
      if (Object.hasOwn(keys, name)) {
        this.value(value, keys[name]!, `${path}${name}`);
        continue;
      }
      const snake = requestName(name);
      const near = nearestKey(name, keys);
      if (name === "cron" && Object.hasOwn(keys, "interval_seconds")) {
        this.violations.push(`unknown key ${path}cron; a schedule job runs every ${path}interval_seconds seconds (3600 = hourly, 86400 = daily), not on a cron expression`);
        covered.push("interval_seconds");
      } else if ((name === "input_entries" || name === "inputEntries") && Object.hasOwn(keys, "inputs")) {
        this.violations.push(`${path}${name} appears only in results: move each {name, value} entry into ${path}inputs as "name": "value" and remove ${name} (an input left out is deleted)`);
        covered.push("inputs");
      } else if (snake !== name && Object.hasOwn(keys, snake)) {
        this.violations.push(resultOnly(path, snake, name));
        covered.push(snake);
      } else if (near !== undefined) {
        this.violations.push(`unknown key ${path}${name}; did you mean ${path}${near}?`);
        covered.push(near);
      } else if (label === "a job spec") {
        this.violations.push(`unknown key ${path}${name}; no job type accepts it`);
      } else {
        this.violations.push(`unknown key ${path}${name} in ${label} (accepted: ${Object.keys(keys).sort(byCodeUnit).join(", ")})`);
      }
    }
    const missing = required.filter((key) => !Object.hasOwn(object, key) && !covered.includes(key));
    if (missing.length > 0) this.violations.push(`${label} needs ${andList(missing.map((key) => `${path}${key}`))}${hint}`);
  }

  value(value: Json, field: SpecField, path: string): void {
    let problem: string | undefined;
    if (typeof field === "string") {
      switch (field) {
        case "text": if (typeof value !== "string") problem = `${path} must be a string`; break;
        case "integer": if (asInteger(value) === undefined) problem = `${path} must be an integer`; break;
        case "interval": {
          const interval = asInteger(value);
          if (interval === undefined || interval < INTERVAL_MIN || interval > INTERVAL_MAX) problem = `${path} must be an integer from ${INTERVAL_MIN} to ${INTERVAL_MAX}`;
          break;
        }
        // `job create` resolves and pins a program reference the way
        // `run submit --from` does (mirrors Rust).
        case "programRef": {
          let valid = false;
          if (typeof value === "string" && value.length <= 200) { try { parseOwnAllowed(value, true); valid = true; } catch { valid = false; } }
          if (typeof value !== "string") problem = `${path} must be a program reference [OWNER/]SLUG[@REV]`;
          else if (!valid) problem = `${path}: program reference ${quoteText(value)} must be [OWNER/]SLUG[@REV]: an optional owner handle, a lowercase slug and, after @, the rev_id or a revision number of your own program; a bare SLUG is your own program`;
          break;
        }
        case "pinnedRef":
          if (typeof value !== "string") problem = `${path} must be a pinned program reference OWNER/SLUG@REV`;
          else if (!isProgramRef(value)) problem = `${path}: ${unpinnedReason(this.context, value)}`;
          break;
        case "stringMap":
          if (!isObject(value)) problem = `${path} must be an object of name -> string`;
          else {
            for (const name of Object.keys(value).sort(byCodeUnit)) {
              if (typeof value[name] !== "string") this.violations.push(`${path}.${name} must be a string (got ${jsonType(value[name]!)})`);
            }
          }
          break;
        case "object": if (!isObject(value)) problem = `${path} must be an object`; break;
        case "array": if (!Array.isArray(value)) problem = `${path} must be an array`; break;
        default: break;
      }
    } else if ("enum" in field) {
      if (typeof value !== "string" || !field.enum.includes(value)) problem = `${path} must be one of ${field.enum.join(", ")}`;
    } else if ("object" in field) {
      if (!isObject(value)) problem = `${path} must be an object`;
      else this.object(value, field.object.keys, field.object.required, `${path}.`, path, "");
    } else if ("array" in field) {
      if (!Array.isArray(value)) problem = `${path} must be an array`;
      else {
        value.forEach((item, index) => {
          const at = `${path}[${index}]`;
          if (isObject(item)) this.object(item, field.array.keys, field.array.required, `${at}.`, at, "");
          else this.violations.push(`${at} must be an object`);
        });
      }
    }
    if (problem !== undefined) this.violations.push(problem);
  }
}

/** Every violation of the operation's manifest `spec`: the type, then each key (sorted), then missing required keys. */
function specViolations(context: Context, spec: JsonObject): string[] {
  const schema = specSchema(context);
  const check = new SpecCheck(context);
  const discriminator = schema.discriminator;
  if (discriminator !== undefined) {
    const variants = schema.variants ?? {};
    const types = Object.keys(variants).sort(byCodeUnit);
    const kind = spec[discriminator];
    let chosen: string | undefined;
    if (kind === undefined) {
      check.violations.push(`a job spec needs ${discriminator} (schedule or webhook, for example); \`${context.command("job create --help")}\` shows minimal specs and \`${context.command("job list")}\` prints every type with its config_fields`);
    } else if (typeof kind === "string" && Object.hasOwn(variants, kind)) {
      chosen = kind;
    } else if (typeof kind === "string") {
      const near = didYouMean(kind, types);
      check.violations.push(`${discriminator} ${quoted(kind)} is not a job type${near === undefined ? "" : `; did you mean ${near}?`} (types: ${types.join(", ")})`);
    } else {
      check.violations.push(`${discriminator} must be a string naming a job type (types: ${types.join(", ")})`);
    }
    const keys: Record<string, SpecField> = { ...(schema.common ?? {}) };
    if (chosen !== undefined) {
      Object.assign(keys, variants[chosen]!.keys);
      check.object(spec, keys, variants[chosen]!.required, "", `a ${chosen} job spec`, "");
    } else {
      // An unknown type: check keys against every type's.
      for (const variant of Object.values(variants)) Object.assign(keys, variant.keys);
      check.object(spec, keys, [], "", "a job spec", "");
    }
  } else {
    const keys = schema.keys ?? {};
    const label = schema.option === "--config-file" ? "the configuration" : "the spec";
    check.object(spec, keys, schema.required ?? [], "", label, `; \`${context.command("job show JOB_ID")}\` prints the current values`);
    if (Object.keys(spec).length < (schema.minKeys ?? 0)) {
      check.violations.push(`the spec is empty; give at least one of ${Object.keys(keys).sort(byCodeUnit).join(", ")}`);
    }
  }
  return check.violations;
}

/** Whether a create spec starts no runs (manifest `spec.unpaidWhen`: a webhook with no program). */
function unpaid(context: Context, spec: JsonObject): boolean {
  const rule = specSchema(context).unpaidWhen;
  return rule !== undefined && spec.type === rule.type && !Object.hasOwn(spec, rule.absent);
}

/** Reads a spec or configuration file and checks it against the manifest `spec`. */
async function readSpec(context: Context, option: string): Promise<{ bytes: Uint8Array; spec: JsonObject }> {
  const value = context.option(option) ?? "";
  const bytes = await readSource(context.cwd, value, SPEC_MAX_BYTES, option);
  try { new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); }
  catch { throw invocationFailure(`${option} ${quoted(value)} is not UTF-8 text`); }
  // Strict JSON as the Rust product parses it: no byte-order mark, no lone
  // surrogates, every number finite.
  const bom = bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
  const parsed = bom ? undefined : parseJson(bytes);
  if (parsed === undefined || !finiteNumbers(parsed)) throw invocationFailure(`${option} ${quoted(value)} is not valid JSON`);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw invocationFailure(`${option} ${quoted(value)} must contain a JSON object`);
  }
  const spec = parsed as JsonObject;
  const violations = specViolations(context, spec);
  if (violations.length === 0) return { bytes, spec };
  const reason = violations.length === 1
    ? violations[0]!
    : `${violations.length} problems in ${option} ${quoted(value)}: ${violations.map((violation, index) => `(${index + 1}) ${violation}`).join(" ")}`;
  throw failure("INVOCATION_INVALID", { reason, violations });
}

// -------------------------------------------------------------- validators

const isUuid = (text: string): boolean => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(text);
const isRunId = (text: string): boolean => /^run_[A-Za-z0-9_-]{1,128}$/u.test(text);
const isModelId = (text: string): boolean => /^[a-z0-9][a-z0-9.-]{0,63}$/u.test(text);
const isHex64 = (text: string): boolean => /^[0-9a-f]{64}$/u.test(text);
const isRevId = (text: string): boolean => /^[0-9a-f]{16}$/u.test(text);
function isProgramRef(text: string): boolean {
  if (text.length > 200) return false;
  try { parseProgramRef(text, true); return true; } catch { return false; }
}

type Kind =
  | { text: number } | { prose: number }
  | "integer" | "epochMs" | "bool" | "uuid" | "runId" | "modelId" | "programRef" | "slug" | "hex64" | "revId";

function scalar(value: Json | undefined, kind: Kind, field: string): Json {
  const text = (): string => {
    if (typeof value !== "string") throw protocol(field);
    return value;
  };
  const check = (ok: boolean): Json => {
    if (!ok) throw protocol(field);
    return value as Json;
  };
  if (typeof kind === "object") {
    if ("text" in kind) {
      const current = text();
      return check(current === "" || validText(current, kind.text));
    }
    return Array.from(text()).map((c) => (c <= "\u001f" || c === "\u007f" ? " " : c)).slice(0, kind.prose).join("");
  }
  switch (kind) {
    case "integer": {
      const number = asInteger(value);
      if (number === undefined) throw protocol(field);
      return number;
    }
    case "epochMs": {
      const number = asInteger(value);
      if (number === undefined || number < 0) throw protocol(field);
      return number;
    }
    case "bool": return check(typeof value === "boolean");
    case "uuid": return check(isUuid(text()));
    case "runId": return check(isRunId(text()));
    case "modelId": return check(isModelId(text()));
    case "programRef": return check(isProgramRef(text()));
    case "slug": return check(validSlug(text()));
    case "hex64": return check(isHex64(text()));
    case "revId": return check(isRevId(text()));
  }
}

type Field = [string, Kind, boolean];

/** Copies `fields` from `source` into `target` under their public snake_case names; a present wrong shape is SERVICE_PROTOCOL_INVALID. */
function copy(target: JsonObject, source: JsonObject, prefix: string, fields: Field[]): void {
  for (const [name, kind, nullable] of fields) {
    if (!Object.hasOwn(source, name)) continue;
    const value = source[name];
    const field = `${prefix}.${name}`;
    if (value === null) {
      if (!nullable) throw protocol(field);
      target[snakeCase(name)] = null;
    } else target[snakeCase(name)] = scalar(value, kind, field);
  }
}

function object(value: Json | undefined, field: string): JsonObject {
  if (value === null || value === undefined || typeof value !== "object" || Array.isArray(value)) throw protocol(field);
  return value;
}

function array(value: Json | undefined, max: number, field: string): Json[] {
  if (!Array.isArray(value) || value.length > max) throw protocol(field);
  return value;
}

// ------------------------------------------------------------- projections

const byUtf8 = (left: string, right: string): number => Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
const costNamed = (name: string): boolean => /[Cc][Oo][Ss][Tt]/u.test(name);

/**
 * Money rule: no output property name may match /cost/i. An input so named is
 * a user key, not a money field, and `job configure` replaces every input, so
 * it is kept losslessly as a {name, value} entry of `input_entries` instead of
 * being dropped. Entries are ordered by UTF-8 bytes, as the Rust product's
 * sorted map. `input_entries` is omitted when there are none.
 */
function splitInputs(inputs: Record<string, string>): JsonObject {
  const target: JsonObject = { inputs: Object.fromEntries(Object.entries(inputs).filter(([name]) => !costNamed(name))) };
  const entries = Object.entries(inputs).filter(([name]) => costNamed(name)).sort(([a], [b]) => byUtf8(a, b));
  if (entries.length > 0) target.input_entries = entries.map(([name, value]) => ({ name, value }));
  return target;
}

function projectRunConfiguration(value: Json, field: string): JsonObject {
  const source = object(value, field);
  const target: JsonObject = {};
  copy(target, source, field, [["model", "modelId", false], ["environment", { text: 64 }, false], ["reasoning_effort", { text: 32 }, false]]);
  if (Object.hasOwn(source, "inputs")) {
    const inputs = object(source.inputs, `${field}.inputs`);
    if (Object.keys(inputs).length > 100 || Object.values(inputs).some((item) => typeof item !== "string")) throw protocol(`${field}.inputs`);
    Object.assign(target, splitInputs(inputs as Record<string, string>));
  }
  if (Object.hasOwn(source, "files")) {
    const name = `${field}.files`;
    target.files = array(source.files, 64, name).map((file) => {
      const entry = object(file, name);
      const item: JsonObject = {};
      copy(item, entry, name, [["name", { text: 256 }, false], ["size", "epochMs", false]]);
      if (typeof entry.content !== "string") throw protocol(name);
      item.content = entry.content;
      if (Object.keys(item).length !== 3) throw protocol(name);
      return item;
    });
  }
  if (Object.hasOwn(source, "contextRepositories")) {
    const name = `${field}.contextRepositories`;
    target.context_repositories = array(source.contextRepositories, 16, name).map((repository) => {
      const item: JsonObject = {};
      copy(item, object(repository, name), name, [["url", { text: 2048 }, false], ["branch", { text: 256 }, true]]);
      if (!Object.hasOwn(item, "url")) throw protocol(name);
      return item;
    });
  }
  if (Object.hasOwn(source, "output") && source.output !== null) {
    const name = `${field}.output`;
    const item: JsonObject = {};
    copy(item, object(source.output, name), name, [["type", { text: 32 }, false], ["repository", { text: 2048 }, false], ["branch", { text: 256 }, true]]);
    if (!Object.hasOwn(item, "type") || !Object.hasOwn(item, "repository")) throw protocol(name);
    target.output = item;
  }
  return target;
}

function projectContract(value: Json, field: string): JsonObject {
  const source = object(value, field);
  const target: JsonObject = {};
  copy(target, source, field, [
    ["programRef", "programRef", false], ["programSlug", "slug", false], ["enabled", "bool", false], ["model", "modelId", true],
    ["contextRepositoryFullName", { text: 200 }, true], ["contextRepositoryBranch", { text: 256 }, true],
  ]);
  if (!Object.hasOwn(target, "program_ref")) throw protocol(`${field}.program_ref`);
  if (Object.hasOwn(source, "runConfiguration") && source.runConfiguration !== null) {
    target.run_configuration = projectRunConfiguration(source.runConfiguration!, `${field}.run_configuration`);
  }
  return target;
}

function projectContracts(value: Json | undefined, field: string): Json[] {
  return array(value, 16, field).map((contract) => projectContract(contract, field));
}

/** A job record: its public fields only (mirrors Rust `project_job`). */
export function projectJob(value: Json | undefined): JsonObject {
  const source = object(value, "job");
  for (const required of ["id", "type", "createdAt"]) {
    if (!Object.hasOwn(source, required) || source[required] === null) throw protocol(`job.${snakeCase(required)}`);
  }
  const target: JsonObject = {};
  copy(target, source, "job", [
    ["id", "uuid", false], ["type", { text: 64 }, false], ["createdAt", "epochMs", false],
    ["name", { prose: 256 }, true], ["url", { text: 2048 }, true], ["intervalSeconds", "integer", true],
    ["mode", { text: 64 }, true], ["repositoryId", "integer", true], ["repositoryFullName", { text: 200 }, true],
    ["repositoryBranch", { text: 256 }, true],
    ["contextRepositoryFullName", { text: 200 }, true], ["contextRepositoryBranch", { text: 256 }, true],
    ["programRef", "programRef", true], ["programSlug", "slug", true],
    ["model", "modelId", true], ["lastRunId", "runId", true], ["lastError", { prose: 1024 }, true],
    ["nextFireAt", "epochMs", true], ["lastEventAt", "epochMs", true],
  ]);
  if (Object.hasOwn(source, "contracts")) target.contracts = projectContracts(source.contracts, "job.contracts");
  addIso(target, ["created_at", "next_fire_at", "last_event_at"]);
  return target;
}

function projectStatus(value: Json): JsonObject {
  const source = object(value, "status");
  const target: JsonObject = {};
  copy(target, source, "status", [
    ["configured", "bool", false], ["active", "bool", false], ["deliveryMode", { text: 32 }, false],
    ["receiverSecretConfigured", "bool", false], ["replySecretConfigured", "bool", false],
    ["secretRotatedAt", "epochMs", true], ["lastEventAt", "epochMs", true], ["lastRunId", "runId", true],
    ["lastError", { prose: 1024 }, true], ["intervalSeconds", "integer", true],
    ["configurationRevision", "integer", true], ["configurationToken", "hex64", true],
    ["nextFireAt", "epochMs", true], ["lastFiredAt", "epochMs", true],
  ]);
  // The optimistic-concurrency token is opaque to the caller: `job
  // configure` sends it back as `revision_token`.
  if (Object.hasOwn(target, "configuration_token")) {
    target.revision_token = target.configuration_token!;
    delete target.configuration_token;
  }
  if (Object.hasOwn(source, "counts")) {
    const counts = object(source.counts, "status.counts");
    const projected: JsonObject = {};
    for (const [state, folded] of RUN_STATES) {
      let total = 0;
      for (const name of folded) {
        if (!Object.hasOwn(counts, name)) continue;
        const number = asInteger(counts[name]);
        if (number === undefined || number < 0) throw protocol("status.counts");
        total += number;
      }
      projected[state] = total;
    }
    target.counts = projected;
  }
  if (Object.hasOwn(source, "contracts")) target.contracts = projectContracts(source.contracts, "status.contracts");
  addIso(target, ["secret_rotated_at", "last_event_at", "next_fire_at", "last_fired_at"]);
  return target;
}

/**
 * `{job, status?}` plus, for create, the once-only `endpoint` and
 * `signing_secret`, and the absolute `endpoint_url` (on create always;
 * otherwise only when the service's endpoint is the secret-free job-id webhook
 * path).
 */
function projectDetail(body: JsonObject, secrets: boolean, environment: Environment): JsonObject {
  const target: JsonObject = { job: projectJob(body.trigger ?? null) };
  if (Object.hasOwn(body, "status") && body.status !== null) target.status = projectStatus(body.status!);
  if (secrets) {
    copy(target, body, "response", [["endpoint", { text: 512 }, false], ["signing_secret", { text: 512 }, false]]);
    const url = typeof target.endpoint === "string" ? absoluteUrl(environment, target.endpoint) : undefined;
    if (url !== undefined) target.endpoint_url = url;
  } else if (typeof body.endpoint === "string" && body.endpoint === `/webhooks/triggers/${String((target.job as JsonObject).id)}`) {
    const url = absoluteUrl(environment, body.endpoint);
    if (url !== undefined) target.endpoint_url = url;
  }
  return target;
}

function projectType(value: Json): JsonObject {
  const source = object(value, "types");
  const target: JsonObject = {};
  copy(target, source, "types", [["id", { text: 64 }, false], ["label", { prose: 256 }, false], ["description", { prose: 2048 }, false]]);
  target.config_fields = array(source.config_fields ?? null, 64, "types.config_fields").map((item) => scalar(item, { text: 64 }, "types.config_fields"));
  for (const required of ["id", "label", "description"]) {
    if (!Object.hasOwn(target, required)) throw protocol(`types.${required}`);
  }
  return target;
}

// -------------------------------------------------------------- operations

async function getJson(context: Context, index: number, path: string): Promise<JsonObject> {
  return jsonObject(await context.send(requestFor(context.operation, index, path)));
}

/** The projected jobs and job limit (`max_jobs`) of a job list body (shared with `cli service triage`). */
export function projectJobs(body: JsonObject): { jobs: JsonObject[]; max: Json } {
  const jobs = array(body.triggers ?? null, 1000, "jobs").map(projectJob);
  const max = body.max_triggers === undefined || body.max_triggers === null ? null : scalar(body.max_triggers, "epochMs", "max_jobs");
  return { jobs, max };
}

async function list(context: Context): Promise<Json> {
  const body = await getJson(context, 0, "/triggers");
  const { jobs, max } = projectJobs(body);
  const jobLimit: JsonObject = {};
  copy(jobLimit, object(body.trigger_limit ?? null, "job_limit"), "job_limit", [["kind", { text: 64 }, false], ["limit", "epochMs", false]]);
  if (!Object.hasOwn(jobLimit, "kind")) throw protocol("job_limit.kind");
  const types = array(body.types ?? null, 64, "types").map(projectType);
  const result: JsonObject = { jobs, max_jobs: max, job_limit: jobLimit, types };
  context.human = humanList(result);
  return result;
}

async function show(context: Context): Promise<Json> {
  const id = jobId(context);
  const result = projectDetail(await getJson(context, 0, `/triggers/${encodeSegment(id)}`), false, context.environment);
  context.human = humanDetail(result, context.environment);
  return result;
}

/** A spec key's value when it is a nonempty string (a hold option the spec gives). */
function specString(spec: JsonObject, key: string): string | undefined {
  const value = spec[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * The GET /run/quote hold for the confirmation plan, quoted for the pinned
 * program the job stores (so the service applies its run settings and
 * declared tools) and the spec's model, reasoning effort, environment and
 * bound repositories (only those the spec gives); the price policy reference
 * stays internal.
 */
async function quote(context: Context, spec: JsonObject): Promise<JsonObject> {
  const repository = (key: string): boolean => specString(spec, key) !== undefined;
  const request = requestFor(context.operation, 0, "/run/quote");
  const programRef = specString(spec, "program_ref");
  if (programRef !== undefined) request.query.push(["program_ref", programRef]);
  request.query.push(...holdQuery({
    model: specString(spec, "model"),
    reasoningEffort: specString(spec, "reasoning_effort"),
    environment: specString(spec, "environment"),
    repositoriesBound: repository("repository_url") || repository("context_repository_url"),
  }));
  // The job's type lets the service price the model a job of that type runs
  // on when the spec names none.
  const jobType = specString(spec, "type");
  if (jobType !== undefined) request.query.push(["job_type", jobType]);
  const body = jsonObject(await context.send(request));
  const hold = object(body.hold ?? null, "quote.hold");
  const holdUsd = hold.hold_usd;
  if (typeof holdUsd !== "string" || !/^-?[0-9]+\.[0-9]{2}$/u.test(holdUsd)) throw protocol("quote.hold.hold_usd");
  const holdCents = usdCents(holdUsd);
  if (holdCents === undefined) throw protocol("quote.hold.hold_usd");
  const ttl = scalar(hold.ttl_seconds ?? null, "epochMs", "quote.hold.ttl_seconds");
  return { hold: { hold_usd: holdUsd, hold_cents: holdCents, ttl_seconds: ttl } };
}

async function create(context: Context): Promise<Json> {
  const read = await readSpec(context, "--spec-file");
  let body = read.bytes;
  const spec = read.spec;
  // A program reference is pinned before the plan: a bare SLUG, `@N` or a
  // latest OWNER/SLUG resolves to OWNER/SLUG@REV (requests 2 and 3), and the
  // job stores the pinned reference (mirrors Rust).
  if (typeof spec.program_ref === "string") {
    const given = spec.program_ref;
    const reference = parseOwnAllowed(given, true);
    if (pinned(reference) === undefined || reference.owner === "") {
      const fixed = await resolveToRun(context, reference, 2, 3, undefined, false);
      if (fixed !== given) {
        spec.program_ref = fixed;
        body = new TextEncoder().encode(canonicalJson(spec));
      }
    }
  }
  const planned = context.planned(1, "/triggers", [], body);
  // A webhook with no program starts no runs: nothing is held.
  if (unpaid(context, spec)) planned.effect = "write";
  else if (context.invocation.preview || !context.invocation.yes) planned.quote = await quote(context, spec);
  const gate = context.gate(planned);
  if (gate.kind === "preview") return gate.result;
  const request: Request = { ...requestFor(context.operation, 1, "/triggers"), body };
  const result = projectDetail(jsonObject(await context.send(request)), true, context.environment);
  if (Object.hasOwn(result, "signing_secret") && context.mode === "human") context.err(SECRET_WARNING);
  context.human = humanDetail(result, context.environment);
  return result;
}

async function update(context: Context, configure: boolean): Promise<Json> {
  const id = jobId(context);
  const body = configure ? await configurationBody(context, id) : (await readSpec(context, "--spec-file")).bytes;
  const path = configure ? `/triggers/${encodeSegment(id)}/configuration` : `/triggers/${encodeSegment(id)}`;
  const gate = context.gate(context.planned(0, path, [], body));
  if (gate.kind === "preview") return gate.result;
  const request: Request = { ...requestFor(context.operation, 0, path), body };
  let response: JsonObject;
  try { response = jsonObject(await context.send(request)); }
  catch (caught) {
    // PUT /triggers/{id} changes webhook jobs only.
    if (!configure && caught instanceof RunnerFailure && caught.details?.serviceStatus === 405) {
      throw failure(caught.code, { ...caught.details, reason: `\`cli job update\` changes webhook jobs only; change a schedule job with \`cli job configure ${id} --interval-seconds N --yes\` or \`cli job configure ${id} --config-file FILE --yes\`, and see details.serviceMessage for other job types` });
    }
    throw caught;
  }
  const result = projectDetail(response, false, context.environment);
  context.human = humanDetail(result, context.environment);
  return result;
}

/**
 * The `job configure` body: the `--config-file` configuration, or, with
 * `--interval-seconds N`, the job's current revision, token and bindings read
 * from the job (manifest request 1) with the new interval; `input_entries` is
 * merged back into `inputs`. Either way the public names are translated to
 * the service's (`serviceConfiguration`).
 */
async function configurationBody(context: Context, id: string): Promise<Uint8Array> {
  const file = context.option("--config-file");
  const value = context.option("--interval-seconds");
  if (file !== undefined && value !== undefined) throw invocationFailure("give exactly one of --config-file and --interval-seconds");
  if (file === undefined && value === undefined) {
    throw invocationFailure(`give --interval-seconds N to change only the cadence, or --config-file FILE with the full configuration; see \`cli job configure --help\` (${intervalReason()})`);
  }
  if (value === undefined) return new TextEncoder().encode(canonicalJson(serviceConfiguration((await readSpec(context, "--config-file")).spec)));
  const interval = /^[0-9]{1,7}$/u.test(value) ? Number(value) : Number.NaN;
  if (!(interval >= INTERVAL_MIN && interval <= INTERVAL_MAX)) throw invocationFailure(`--interval-seconds: ${intervalReason()}`);
  const detail = projectDetail(await getJson(context, 1, `/triggers/${encodeSegment(id)}`), false, context.environment);
  const job = detail.job as JsonObject;
  const kind = typeof job.type === "string" ? job.type : "";
  if (kind !== "schedule") {
    throw invocationFailure(`--interval-seconds applies to schedule jobs only; job ${id} is a ${humanSafeScalar(kind)} job (change webhook settings with \`cli job update\`)`);
  }
  const status = (detail.status ?? {}) as JsonObject;
  const revision = status.configuration_revision;
  if (typeof revision !== "number") throw protocol("status.configuration_revision");
  const token = status.revision_token;
  if (typeof token !== "string") throw protocol("status.revision_token");
  const contracts = status.contracts;
  if (!Array.isArray(contracts) || contracts.length === 0) throw protocol("status.contracts");
  const bindings = (contracts as JsonObject[]).map((contract) => {
    const source = contract.run_configuration;
    const configuration: JsonObject = source !== null && typeof source === "object" && !Array.isArray(source)
      ? { ...source }
      : (typeof contract.model === "string" ? { model: contract.model } : {});
    const entries = configuration.input_entries;
    delete configuration.input_entries;
    if (Array.isArray(entries)) {
      const current = configuration.inputs;
      const inputs: JsonObject = current !== null && typeof current === "object" && !Array.isArray(current) ? { ...current } : {};
      for (const entry of entries as JsonObject[]) {
        if (typeof entry.name === "string" && Object.hasOwn(entry, "value")) inputs[entry.name] = entry.value!;
      }
      configuration.inputs = inputs;
    }
    return { program_ref: contract.program_ref ?? null, run_configuration: configuration };
  });
  return new TextEncoder().encode(canonicalJson(serviceConfiguration({ interval_seconds: interval, configuration_revision: revision, revision_token: token, bindings })));
}

/**
 * A public `job configure` configuration in the service's names: the opaque
 * `revision_token` is sent as the service's configuration token and each
 * binding's `run_configuration.context_repositories` under the service's
 * camelCase name (mirrors Rust `service_configuration`).
 */
function serviceConfiguration(configuration: JsonObject): JsonObject {
  const out: JsonObject = { ...configuration };
  if (Object.hasOwn(out, "revision_token")) {
    out.configuration_token = out.revision_token!;
    delete out.revision_token;
  }
  if (Array.isArray(out.bindings)) {
    out.bindings = out.bindings.map((binding) => {
      if (!isObject(binding) || !isObject(binding.run_configuration)) return binding;
      const run: JsonObject = { ...binding.run_configuration };
      if (Object.hasOwn(run, "context_repositories")) {
        run.contextRepositories = run.context_repositories!;
        delete run.context_repositories;
      }
      return { ...binding, run_configuration: run };
    });
  }
  return out;
}

/**
 * The <JOB_ID> of `job delete`: a lowercase UUID as `cli job
 * list` prints it, checked before any request. A malformed id could otherwise
 * reach a 404 (or a normalized path such as `..`) that the idempotent delete
 * would report as alreadyAbsent.
 */
function deleteId(context: Context): string {
  const id = jobId(context);
  if (isUuid(id)) return id;
  const lower = id.toLowerCase();
  const hint = isUuid(lower) ? `; pass ${quoteText(lower)}` : "";
  const list = context.command("job list");
  const base = invocationFailure(`JOB_ID ${quoteText(id)} is not a job id: job ids are lowercase UUIDs (8-4-4-4-12 hex digits) as \`${list}\` prints them; nothing was sent${hint}`);
  throw new RunnerFailure({
    code: base.code, boundary: base.boundary, message: base.message,
    action: `List your jobs with \`${list}\` and pass one of their ids.`,
    exitCode: base.exitCode, retryable: false,
    details: { ...(base.details ?? {}), suggestedArgv: context.followUpArgv(["job", "list"]) },
  });
}

async function remove(context: Context): Promise<Json> {
  const id = deleteId(context);
  const path = `/triggers/${encodeSegment(id)}`;
  const gate = context.gate(context.planned(0, path));
  if (gate.kind === "preview") return gate.result;
  let response: JsonObject;
  try { response = jsonObject(await context.send(requestFor(context.operation, 0, path))); }
  catch (caught) {
    // A confirmed delete is idempotent: nothing to delete is the goal state.
    if (!(caught instanceof RunnerFailure) || caught.code !== "SERVICE_RESOURCE_NOT_FOUND") throw caught;
    context.human = `Job ${humanSafeScalar(id)} was already absent; nothing was deleted.\n`;
    return { id, deleted: true, already_absent: true };
  }
  if (response.deleted !== true) throw protocol("deleted");
  context.human = `Deleted job ${humanSafeScalar(id)}.\n`;
  return { id, deleted: true };
}

/**
 * The service answers 404 for the deliveries of any job that is not a webhook.
 * Reading the job (manifest request 1) tells a wrong job type from an unknown
 * id; any other outcome keeps the original 404.
 */
async function explainMissingDeliveries(context: Context, id: string, original: RunnerFailure): Promise<RunnerFailure> {
  let body: JsonObject;
  try { body = await getJson(context, 1, `/triggers/${encodeSegment(id)}`); }
  catch { return original; }
  const trigger = body.trigger;
  const kind = trigger !== null && typeof trigger === "object" && !Array.isArray(trigger) ? (trigger as JsonObject).type : undefined;
  if (typeof kind === "string" && /^[a-z0-9_-]{1,64}$/u.test(kind) && kind !== "webhook") {
    return failure("SERVICE_REQUEST_REJECTED", { serviceStatus: 404, reason: `job ${id} is a ${kind} job; deliveries exist only for webhook jobs. See its runs with \`cli job show ${id}\` and \`cli run list\`` });
  }
  return original;
}

async function deliveries(context: Context): Promise<Json> {
  const id = jobId(context);
  let body: JsonObject;
  try { body = await getJson(context, 0, `/triggers/${encodeSegment(id)}/deliveries`); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") throw await explainMissingDeliveries(context, id, caught);
    throw caught;
  }
  const projected = array(body.deliveries ?? null, 100, "deliveries").map((delivery) => {
    const source = object(delivery, "deliveries");
    const target: JsonObject = {};
    copy(target, source, "deliveries", [
      ["id", "integer", false], ["receivedAt", "epochMs", false], ["outcome", { text: 32 }, false],
      ["testOnly", "bool", false], ["deliveryId", { text: 256 }, true], ["reason", { prose: 1024 }, true],
    ]);
    for (const required of ["id", "received_at", "outcome", "test_only"]) {
      if (!Object.hasOwn(target, required)) throw protocol(`deliveries.${required}`);
    }
    addIso(target, ["received_at"]);
    // The runs the delivery started.
    if (Object.hasOwn(source, "jobs")) {
      target.runs = array(source.jobs, 32, "deliveries.runs").map((run) => {
        const item: JsonObject = {};
        copy(item, object(run, "deliveries.runs"), "deliveries.runs", [["programRef", "programRef", false], ["status", { text: 32 }, false], ["runId", "runId", true]]);
        if (Object.keys(item).length !== 3) throw protocol("deliveries.runs");
        return item;
      });
    }
    return target;
  });
  const result: JsonObject = { deliveries: projected };
  context.human = humanDeliveries(result);
  return result;
}

async function rotateSecret(context: Context): Promise<Json> {
  const id = jobId(context);
  const path = `/triggers/${encodeSegment(id)}/rotate-secret`;
  const gate = context.gate(context.planned(0, path));
  if (gate.kind === "preview") return gate.result;
  const response = jsonObject(await context.send(requestFor(context.operation, 0, path)));
  const result: JsonObject = { id };
  copy(result, response, "response", [["endpoint", { text: 512 }, false], ["signing_secret", { text: 512 }, false]]);
  for (const required of ["endpoint", "signing_secret"]) {
    if (!Object.hasOwn(result, required)) throw protocol(required);
  }
  const url = typeof result.endpoint === "string" ? absoluteUrl(context.environment, result.endpoint) : undefined;
  if (url !== undefined) result.endpoint_url = url;
  if (context.mode === "human") context.err(SECRET_WARNING);
  context.human = humanRotated(result, context.environment);
  return result;
}

async function contractList(context: Context): Promise<Json> {
  const id = jobId(context);
  const body = await getJson(context, 0, `/triggers/${encodeSegment(id)}/contracts`);
  const contracts = array(body.contracts ?? null, 16, "contracts").map(projectListedContract);
  const result: JsonObject = { contracts };
  if (Object.hasOwn(body, "max_contracts")) result.max_contracts = scalar(body.max_contracts, "epochMs", "max_contracts");
  context.human = humanContracts(result);
  return result;
}

/**
 * One contract of the contract route, whose names differ from the job
 * record's: the public contract fields plus its saved run settings as
 * `run_configuration`. The program text and file content are never projected.
 */
function projectListedContract(value: Json, index: number): JsonObject {
  const field = "contracts";
  const source = object(value, field);
  const target: JsonObject = {};
  copy(target, source, field, [
    ["program_ref", "programRef", false], ["enabled", "bool", false], ["model", "modelId", true],
    ["effective_model", "modelId", true], ["rev_id", "revId", false], ["bound_at", "epochMs", false],
    ["is_platform_default", "bool", false],
  ]);
  if (!Object.hasOwn(target, "program_ref")) throw protocol(`${field}.program_ref`);
  if (Object.hasOwn(source, "slug")) target.program_slug = scalar(source.slug, "slug", `${field}.slug`);
  addIso(target, ["bound_at"]);
  const run = listedRunConfiguration(source, field, index);
  if (Object.keys(run).length > 0) target.run_configuration = run;
  return target;
}

/** A listed binding's saved settings in the public `run_configuration` names; absent and null settings are left out. */
function listedRunConfiguration(source: JsonObject, field: string, index: number): JsonObject {
  const present = (name: string): boolean => Object.hasOwn(source, name) && source[name] !== null;
  const target: JsonObject = {};
  if (present("reasoning_effort")) target.reasoning_effort = scalar(source.reasoning_effort, { text: 32 }, `${field}.reasoning_effort`);
  if (present("inputs")) {
    const name = `${field}.inputs`;
    const inputs = object(source.inputs, name);
    if (Object.keys(inputs).length > 100) throw protocol(name);
    // A string is kept as it is; another JSON scalar is carried as its JSON text.
    const texts: Record<string, string> = {};
    for (const [key, item] of Object.entries(inputs)) {
      if (item !== null && typeof item === "object") throw protocol(`${field}[${index}].inputs.${key}`);
      texts[key] = typeof item === "string" ? item : canonicalJson(item);
    }
    const split = splitInputs(texts);
    if (Object.keys(split.inputs as JsonObject).length === 0) delete split.inputs;
    Object.assign(target, split);
  }
  if (present("repositories")) {
    const name = `${field}.repositories`;
    const repositories = array(source.repositories, 16, name).map((repository) => {
      const item: JsonObject = {};
      copy(item, object(repository, name), name, [["url", { text: 2048 }, false], ["branch", { text: 256 }, true]]);
      if (!Object.hasOwn(item, "url")) throw protocol(name);
      return item;
    });
    if (repositories.length > 0) target.context_repositories = repositories;
  }
  if (present("environment")) target.environment = scalar(source.environment, { text: 64 }, `${field}.environment`);
  if (present("files")) {
    const name = `${field}.files`;
    const files = array(source.files, 64, name).map((file) => {
      const item: JsonObject = {};
      copy(item, object(file, name), name, [["name", { text: 256 }, false], ["size", "epochMs", false], ["sha256", "hex64", false]]);
      if (!Object.hasOwn(item, "name") || !Object.hasOwn(item, "size")) throw protocol(name);
      return item;
    });
    if (files.length > 0) target.stored_files = files;
  }
  if (present("output")) {
    const name = `${field}.output`;
    const item: JsonObject = {};
    copy(item, object(source.output, name), name, [["type", { text: 32 }, false], ["repository", { text: 2048 }, false], ["branch", { text: 256 }, true]]);
    if (!Object.hasOwn(item, "type") || !Object.hasOwn(item, "repository")) throw protocol(name);
    target.output = item;
  }
  return target;
}

/**
 * Why a contract reference is not pinned, with the command that prints the
 * rev_id for the program the caller named: `program show`
 * resolves `@N` and prints the pinned reference as `ref`.
 */
function unpinnedReason(context: Context, value: string): string {
  const at = value.indexOf("@");
  const name = at >= 0 ? value.slice(0, at) : value;
  const rev = at >= 0 ? value.slice(at + 1) : "";
  const named = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[a-z0-9][a-z0-9-]{0,63}$/u.test(name);
  if (named && /^[1-9][0-9]{0,8}$/u.test(rev)) {
    return `program reference ${quoted(value)} names revision ${rev} by number; a contract pins the 16-hex-digit rev_id, which \`${context.command(`program show ${value} --json`)}\` prints as \`ref\``;
  }
  const show = context.command(`program show ${named ? name : "OWNER/SLUG"} --json`);
  return `program reference ${quoted(value)} must be pinned as OWNER/SLUG@REV (the 16-hex-digit rev_id); \`${show}\` prints the latest as \`ref\``;
}

/** The pinned OWNER/SLUG@REV of `option` (the positional argument when undefined). */
function pinnedValue(context: Context, value: string, option?: string): string {
  try { return pinned(parseProgramRef(value, true)) ?? ""; }
  catch { throw invocationFailure(`${option === undefined ? "" : `${option}: `}${unpinnedReason(context, value)}`); }
}

async function contractDetach(context: Context): Promise<Json> {
  const id = jobId(context);
  const reference = pinnedValue(context, context.argument("OWNER/SLUG@REV") ?? "");
  const path = `/triggers/${encodeSegment(id)}/contracts/${encodeSegment(reference)}`;
  const gate = context.gate(context.planned(0, path));
  if (gate.kind === "preview") return gate.result;
  const response = jsonObject(await context.send(requestFor(context.operation, 0, path)));
  if (response.unbound !== reference) throw protocol("unbound");
  context.human = `Detached ${humanSafeScalar(reference)} from job ${humanSafeScalar(id)}.\n`
    + `List the job's contracts with \`cli job contract list ${humanSafeScalar(id)}\`.\n`;
  return { contracts: [{ program_ref: reference }] };
}

/** The `job contract attach` options that change a webhook binding's settings (with --replace, they need a webhook job). */
const BINDING_OPTIONS = [
  "--model", "--reasoning-effort", "--repo", "--commit-output", "--clear-repo", "--clear-commit-output",
  "--input", "--inputs-file", "--clear-input", "--environment", "--file", "--clear-files", "--replace",
] as const;
const BINDING_FLAGS = new Set(["--clear-repo", "--clear-commit-output", "--clear-files"]);
/** Stored binding files: at most 20, 5 MiB each and 10 MiB in total. */
const MAX_BINDING_FILES = 20;
const MAX_BINDING_FILE_BYTES = 5 << 20;
const MAX_BINDING_FILES_BYTES = 10 << 20;

/** The settings options of one `job contract attach`, checked locally. */
interface Binding {
  model?: string;
  effort?: string;
  repo?: Repository;
  commit?: Repository;
  clearRepo: boolean;
  clearCommit: boolean;
  /** --file NAME -> base64 content (replaces the stored set), or undefined. */
  files?: JsonObject;
  clearFiles: boolean;
  inputs: Map<string, string>;
  clearInputs: string[];
  environment?: string;
  replace?: string;
  /** Any option above was given. */
  any: boolean;
}

/**
 * Reads and checks the settings options before any request: conflicting
 * options, and a --commit-output that is not the --repo given, are
 * INVOCATION_INVALID.
 */
async function bindingOptions(context: Context): Promise<Binding> {
  const given = (name: string): boolean => (BINDING_FLAGS.has(name) ? context.flag(name) : context.optionValues(name).length > 0);
  const clearRepo = context.flag("--clear-repo");
  const clearCommit = context.flag("--clear-commit-output");
  if (given("--repo") && clearRepo) throw invocationFailure("--repo and --clear-repo cannot be combined: --repo replaces the saved repository, --clear-repo removes it");
  if (given("--commit-output") && clearRepo) throw invocationFailure("--commit-output and --clear-repo cannot be combined: --commit-output sets the commit output, --clear-repo removes it");
  if (given("--commit-output") && clearCommit) throw invocationFailure("--commit-output and --clear-commit-output cannot be combined: --commit-output sets the commit output, --clear-commit-output removes it");
  const clearFiles = context.flag("--clear-files");
  if (given("--file") && clearFiles) throw invocationFailure("--file and --clear-files cannot be combined: --file replaces the stored files, --clear-files removes them");
  const clearInputs: string[] = [];
  for (const key of context.optionValues("--clear-input")) {
    if (!validInputKey(key)) throw invocationFailure(`--clear-input ${quoteText(key)} must be an input name of 1 to 128 characters without control characters`);
    if (!clearInputs.includes(key)) clearInputs.push(key);
  }
  for (const raw of context.optionValues("--input")) {
    const key = raw.includes("=") ? raw.slice(0, raw.indexOf("=")) : raw;
    if (clearInputs.includes(key)) throw invocationFailure(`--input ${quoteText(key)} and --clear-input ${quoteText(key)} cannot be combined: --input sets the input, --clear-input removes it`);
  }
  const replaceValue = context.option("--replace");
  const repoValue = context.option("--repo");
  const commitValue = context.option("--commit-output");
  const binding: Binding = {
    clearRepo, clearCommit, clearInputs, clearFiles,
    inputs: await parseInputs(context),
    any: BINDING_OPTIONS.some(given),
  };
  const model = modelOption(context);
  if (model !== undefined) binding.model = model;
  const effort = context.option("--reasoning-effort");
  if (effort !== undefined) binding.effort = effort;
  if (repoValue !== undefined) binding.repo = parseRepository("--repo", repoValue);
  if (commitValue !== undefined) binding.commit = parseRepository("--commit-output", commitValue);
  const environment = tokenOption(context, "--environment", "an environment");
  if (environment !== undefined) binding.environment = environment;
  if (replaceValue !== undefined) binding.replace = pinnedValue(context, replaceValue, "--replace");
  const files = await bindingFiles(context);
  if (files !== undefined) binding.files = files;
  if (binding.commit !== undefined && binding.repo !== undefined && !sameRepository(binding.commit, binding.repo)) {
    throw commitNotRead(binding.commit);
  }
  return binding;
}

/**
 * The last component of a `/`-separated path, as a file name: trailing `/`
 * and `.` components are skipped, and a path ending in `..` (or only `.`)
 * has none (empty).
 */
function baseName(path: string): string {
  const parts = path.split("/").filter((part, index) => part !== "" && (part !== "." || index === 0));
  const last = parts[parts.length - 1] ?? "";
  return last === "." || last === ".." ? "" : last;
}

/**
 * The `--file [NAME=]PATH` files as NAME -> base64 of their UTF-8 content
 * (NAME defaults to PATH's last segment), or undefined when none is given.
 */
async function bindingFiles(context: Context): Promise<JsonObject | undefined> {
  const values = context.optionValues("--file");
  if (values.length === 0) return undefined;
  if (values.length > MAX_BINDING_FILES) throw invocationFailure(`--file was given ${values.length} times; a binding stores at most ${MAX_BINDING_FILES} files`);
  const files: JsonObject = {};
  let total = 0;
  for (const raw of values) {
    const equals = raw.indexOf("=");
    const path = equals >= 0 ? raw.slice(equals + 1) : raw;
    const name = equals >= 0 ? raw.slice(0, equals) : baseName(path);
    if (!validText(name, 256) || name.includes("/") || name.includes("\\")) {
      throw invocationFailure(`--file ${quoteText(raw)}: the file name must be 1 to 256 characters without /, \\ or control characters; give it as NAME=PATH`);
    }
    const bytes = await readSource(context.cwd, path, MAX_BINDING_FILE_BYTES, "--file");
    try { new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); }
    catch { throw invocationFailure(`--file ${quoteText(path)} is not UTF-8 text`); }
    total += bytes.length;
    if (total > MAX_BINDING_FILES_BYTES) throw invocationFailure(`the --file files hold more than ${MAX_BINDING_FILES_BYTES} bytes together; a binding stores at most that much`);
    if (Object.hasOwn(files, name)) throw invocationFailure(`--file name ${quoteText(name)} was given more than once; name each file uniquely with NAME=PATH`);
    files[name] = Buffer.from(bytes).toString("base64");
  }
  return files;
}

/** Whether two repository URLs name the same repository (GitHub names ignore case). */
const sameUrl = (left: string, right: string): boolean => left.toLowerCase() === right.toLowerCase();

/** The commit output of `--commit-output`, committing to `url` (the repository the runs read). */
function commitOutput(commit: Repository, url: string): JsonObject {
  return commit.branch === undefined ? { type: "commit", repository: url } : { type: "commit", repository: url, branch: commit.branch };
}

/** A bound contract's saved settings as the service listed them; a wrong shape is SERVICE_PROTOCOL_INVALID. */
interface Saved {
  /** The binding has stored files (their content is never listed). */
  files?: boolean;
  model?: string;
  effort?: string;
  environment?: string;
  repository?: { url: string; branch?: string };
  output?: JsonObject;
  inputs: JsonObject;
}

function savedSettings(contract: JsonObject): Saved {
  const field = "contracts";
  const text = (name: string, max: number): string | undefined =>
    contract[name] === null || contract[name] === undefined ? undefined : scalar(contract[name], { text: max }, `${field}.${name}`) as string;
  const saved: Saved = { inputs: {} };
  const model = contract.model === null || contract.model === undefined ? undefined : scalar(contract.model, "modelId", `${field}.model`) as string;
  if (model !== undefined) saved.model = model;
  const effort = text("reasoning_effort", 32);
  if (effort !== undefined) saved.effort = effort;
  const environment = text("environment", 64);
  if (environment !== undefined) saved.environment = environment;
  saved.files = Array.isArray(contract.files) && contract.files.length > 0;
  if (contract.repositories !== null && contract.repositories !== undefined) {
    const first = array(contract.repositories, 16, `${field}.repositories`)[0];
    if (first !== undefined) {
      const item: JsonObject = {};
      copy(item, object(first, `${field}.repositories`), `${field}.repositories`, [["url", { text: 2048 }, false], ["branch", { text: 256 }, true]]);
      if (typeof item.url !== "string") throw protocol(`${field}.repositories`);
      saved.repository = typeof item.branch === "string" ? { url: item.url, branch: item.branch } : { url: item.url };
    }
  }
  if (contract.output !== null && contract.output !== undefined) {
    const output = object(contract.output, `${field}.output`);
    if (typeof output.repository !== "string") throw protocol(`${field}.output`);
    saved.output = output;
  }
  // Inputs are kept as returned: a non-string value is sent back unchanged.
  if (contract.inputs !== null && contract.inputs !== undefined) saved.inputs = { ...object(contract.inputs, `${field}.inputs`) };
  return saved;
}

/** `argv` without the settings options and their values (the suggestion for a job that is not a webhook). */
function withoutBindingOptions(argv: readonly string[]): string[] {
  return withoutOptions(argv, BINDING_OPTIONS, BINDING_FLAGS);
}

/** `argv` without `options` (with their values) and `flags`, up to any `--`. */
function withoutOptions(argv: readonly string[], options: readonly string[], flags: ReadonlySet<string>): string[] {
  const kept: string[] = [];
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index]!;
    if (token === "--") { kept.push(...argv.slice(index)); break; }
    const name = token.includes("=") ? token.slice(0, token.indexOf("=")) : token;
    if (!options.includes(name)) { kept.push(token); continue; }
    if (name === token && !flags.has(name)) index += 1;
  }
  return kept;
}

/**
 * The POST body of `job contract attach` from the job's listed contracts.
 * A re-bind of the same ref on a merging service sends only what the options
 * change (clears as JSON nulls); otherwise the saved settings of the bound
 * ref (or of the --replace target) are merged here and sent in full. An
 * unbound ref sends only the options given.
 */
/** The settings the runs of a binding use after an attach, as the plan quote prices them. */
interface Effective { model?: string; effort?: string; environment?: string; repository: boolean }

function attachBody(reference: string, binding: Binding, contracts: JsonObject[]): { body: JsonObject; note: string | undefined; effective: Effective } {
  const base = contracts.find((contract) => contract.program_ref === (binding.replace ?? reference));
  const saved = base === undefined ? undefined : savedSettings(base);
  // A service that lists environment (and file metadata) merges a re-bind itself.
  const merging = contracts.some((contract) => Object.hasOwn(contract, "environment"));
  const repoUrl = binding.repo === undefined ? undefined : repositoryUrl(binding.repo);
  // The repository the runs read after this change.
  const effectiveUrl = repoUrl ?? (binding.clearRepo ? undefined : saved?.repository?.url);
  if (binding.commit !== undefined && (effectiveUrl === undefined || !sameUrl(effectiveUrl, repositoryUrl(binding.commit)))) {
    throw commitNotRead(binding.commit);
  }
  // A new repository drops a saved output that commits elsewhere.
  const outputElsewhere = repoUrl !== undefined && typeof saved?.output?.repository === "string" && !sameUrl(saved.output.repository, repoUrl);
  const mergedInputs = (): JsonObject => {
    const inputs: JsonObject = { ...(saved?.inputs ?? {}) };
    for (const [key, value] of binding.inputs) inputs[key] = value;
    for (const key of binding.clearInputs) delete inputs[key];
    return inputs;
  };
  const body: JsonObject = { program_ref: reference };
  // A --replace of the attached ref itself is an ordinary re-attach.
  const moved = binding.replace !== undefined && binding.replace !== reference;
  if (saved !== undefined && merging && !moved) {
    body.replace_program_ref = reference;
    if (binding.model !== undefined) body.model = binding.model;
    if (binding.effort !== undefined) body.reasoning_effort = binding.effort;
    if (binding.repo !== undefined) {
      body.repository_url = repoUrl!;
      body.repository_branch = binding.repo.branch ?? null;
      if (outputElsewhere) body.output = null;
    }
    if (binding.clearRepo) Object.assign(body, { repository_url: null, repository_branch: null, output: null });
    if (binding.clearCommit) body.output = null;
    if (binding.commit !== undefined) body.output = commitOutput(binding.commit, effectiveUrl!);
    if (binding.inputs.size > 0 || binding.clearInputs.length > 0) body.inputs = mergedInputs();
    if (binding.environment !== undefined) body.environment = binding.environment;
    if (binding.files !== undefined) body.files = binding.files;
    if (binding.clearFiles) body.files = null;
    // The service keeps what is not sent: the saved settings, with the options over them.
    const effective: Effective = { repository: effectiveUrl !== undefined };
    const model = binding.model ?? saved.model;
    if (model !== undefined) effective.model = model;
    const effort = binding.effort ?? saved.effort;
    if (effort !== undefined) effective.effort = effort;
    const environment = binding.environment ?? saved.environment;
    if (environment !== undefined) effective.environment = environment;
    return { body, note: undefined, effective };
  }
  const model = binding.model ?? saved?.model;
  if (model !== undefined) body.model = model;
  const effort = binding.effort ?? saved?.effort;
  if (effort !== undefined) body.reasoning_effort = effort;
  const repository = binding.repo !== undefined
    ? (binding.repo.branch === undefined ? { url: repoUrl! } : { url: repoUrl!, branch: binding.repo.branch })
    : (binding.clearRepo ? undefined : saved?.repository);
  if (repository !== undefined) {
    body.repository_url = repository.url;
    if (repository.branch !== undefined) body.repository_branch = repository.branch;
  }
  const output = binding.commit !== undefined ? commitOutput(binding.commit, effectiveUrl!)
    : (binding.clearRepo || binding.clearCommit || outputElsewhere ? undefined : saved?.output);
  if (output !== undefined) body.output = output;
  const inputs = mergedInputs();
  if (Object.keys(inputs).length > 0) body.inputs = inputs;
  // A move to another revision is a full replace: the saved environment is
  // carried too (an older service lists none).
  const environment = binding.environment ?? (moved ? saved?.environment : undefined);
  if (environment !== undefined) body.environment = environment;
  if (binding.files !== undefined) body.files = binding.files;
  // Clearing files of an unbound program changes nothing.
  else if (binding.clearFiles && saved !== undefined) body.files = null;
  if (binding.replace !== undefined) body.replace_program_ref = binding.replace;
  const effective: Effective = { repository: repository !== undefined };
  if (model !== undefined) effective.model = model;
  if (effort !== undefined) effective.effort = effort;
  if (environment !== undefined) effective.environment = environment;
  // An older service lists neither stored files nor environment, so a
  // client-merged re-bind cannot carry them; stored file content is never
  // listed, so a move cannot carry the files either.
  let note: string | undefined;
  if (saved !== undefined && !merging) note = DROPPED_NOTE;
  else if (moved && saved?.files === true && binding.files === undefined && !binding.clearFiles) note = FILES_NOTE;
  return { body, note, effective };
}

/** Job types that always bind a repository, so their runs are quoted with one. */
const REPOSITORY_JOB_TYPES = ["github-issue-opened", "github-pull-request-opened", "github-release-published"];

const DROPPED_NOTE = "note: this service does not report stored files or environment; re-attaching may drop them\n";
const FILES_NOTE = "note: stored files are not carried to the new revision; pass --file to attach them\n";

async function contractAttach(context: Context): Promise<Json> {
  const id = jobId(context);
  const reference = pinnedValue(context, context.argument("OWNER/SLUG@REV") ?? "");
  const binding = await bindingOptions(context);
  const jobPath = `/triggers/${encodeSegment(id)}`;
  const path = `${jobPath}/contracts`;
  // Settings apply to webhook jobs only, and a plan's quote prices the
  // job's type: read the job first.
  const plan = context.invocation.preview || !context.invocation.yes;
  let live = false;
  let jobType: string | undefined;
  if (binding.any || plan) {
    const detail = projectDetail(await getJson(context, 0, jobPath), false, context.environment);
    const kind = (detail.job as JsonObject).type;
    if (typeof kind === "string") jobType = kind;
    if (binding.any && kind !== "webhook") {
      const error = invocationFailure(`job ${id} is a ${humanSafeScalar(typeof kind === "string" ? kind : "")} job; run settings (--model, --reasoning-effort, --repo, --commit-output, --input, --inputs-file, --environment, --file, --replace and the --clear options) apply to webhook jobs only`);
      throw context.corrected(error, "Attach without those options: `{command}`", withoutBindingOptions(context.invocation.argv));
    }
    live = (detail.status as JsonObject | undefined)?.delivery_mode === "live";
  }
  // The listed contracts keep a bound program's saved settings.
  const listed = array((await getJson(context, 1, path)).contracts ?? null, 16, "contracts").map((contract) => object(contract, "contracts"));
  // The listing must be valid as `job contract list` reads it.
  listed.forEach(projectListedContract);
  // --replace onto another program that is already bound would reset that
  // binding's settings.
  const replaced = binding.replace;
  if (replaced !== undefined && replaced !== reference && listed.some((contract) => contract.program_ref === reference)) {
    const error = invocationFailure(`${reference} is already bound to job ${humanSafeScalar(id)}; --replace would reset its settings. Change it in place without --replace, or detach ${replaced} first`);
    throw context.corrected(error, "Change it in place without --replace: `{command}`", withoutOptions(context.invocation.argv, ["--replace"], new Set()));
  }
  const { body, note, effective } = attachBody(reference, binding, listed);
  const bytes = new TextEncoder().encode(canonicalJson(body));
  const planned = context.planned(2, path, [], bytes);
  if (plan) {
    // Advisory: a failed quote leaves the plan without one.
    const query: Array<[string, string]> = [["program_ref", reference], ...holdQuery({
      model: effective.model, reasoningEffort: effective.effort, environment: effective.environment,
      repositoriesBound: effective.repository || (jobType !== undefined && REPOSITORY_JOB_TYPES.includes(jobType)),
    })];
    if (jobType !== undefined) query.push(["job_type", jobType]);
    const quoted = await context.advisoryQuote(3, undefined, query);
    if (quoted !== undefined) planned.quote = quoted;
  }
  const gate = context.gate(planned);
  if (gate.kind === "preview") return gate.result;
  let response: JsonObject;
  try { response = jsonObject(await context.send({ ...requestFor(context.operation, 2, path), body: bytes })); }
  catch (caught) {
    // A live webhook may refuse binding changes: switching it to test delivery first allows them.
    const message = caught instanceof RunnerFailure ? caught.details?.serviceMessage : undefined;
    if (live && caught instanceof RunnerFailure && caught.code === "SERVICE_REQUEST_REJECTED" && typeof message === "string" && /immutable/iu.test(message)) {
      const argv = context.followUpArgv(["job", "update", id, "--spec-file", "-", "--yes"]);
      const stdin = canonicalJson({ delivery_mode: "test" });
      throw new RunnerFailure({
        code: caught.code, boundary: caught.boundary, message: caught.message, exitCode: caught.exitCode, retryable: caught.retryable,
        action: `${caught.action} The job delivers live; to change its binding, switch it to test delivery first: \`${argvText(argv)}\` with details.suggestedStdin on standard input.`,
        details: { ...(caught.details ?? {}), suggestedArgv: argv, suggestedStdin: `${stdin}\n` },
      });
    }
    throw caught;
  }
  if (response.bound !== reference) throw protocol("bound");
  if (note !== undefined && context.mode === "human") context.err(note);
  const settings = settingsLine(body);
  context.human = `Attached ${humanSafeScalar(reference)} to job ${humanSafeScalar(id)}.\n`
    + (settings === undefined ? "" : `  settings: ${settings}\n`)
    + `List the job's contracts with \`cli job contract list ${humanSafeScalar(id)}\`.\n`;
  return { contracts: [{ program_ref: reference }] };
}

// ------------------------------------------------------------------- human

function textOrDash(value: Json | undefined): string {
  if (typeof value === "string" && value !== "") return humanSafeScalar(value);
  if (typeof value === "number") return String(value);
  return "-";
}

/** 9999-12-31T23:59:59.999Z in epoch milliseconds: later times print raw. */
const MAX_ISO_MS = 253_402_300_799_999;

/** Epoch milliseconds as RFC 3339 UTC with milliseconds; anything else is `-`. */
function isoOrDash(value: Json | undefined): string {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) return "-";
  return value >= 0 && value <= MAX_ISO_MS ? isoMs(value) ?? String(value) : String(value);
}

function boolOrDash(value: Json | undefined): string {
  return typeof value === "boolean" ? String(value) : "-";
}

/**
 * Readable `job show|create|update|configure` output: one `label: value` line
 * per known field in a fixed order, times as RFC 3339, the
 * absolute endpoint URL and, on create, the once-only signing secret; then
 * copyable `Next:` commands in this environment.
 */
function humanDetail(result: JsonObject, environment: Environment): string {
  const job = (result.job ?? {}) as JsonObject;
  const status = (result.status ?? {}) as JsonObject;
  const id = textOrDash(job.id);
  const kind = textOrDash(job.type);
  let text = `Job ${id} (${kind})\n`;
  const line = (label: string, value: string): void => { if (value !== "-") text += `  ${label}: ${value}\n`; };
  const either = (name: string): Json | undefined => (status[name] === null || status[name] === undefined ? job[name] : status[name]);
  line("name", textOrDash(job.name));
  line("created", isoOrDash(job.created_at));
  line("active", boolOrDash(status.active));
  const interval = either("interval_seconds");
  line("interval", typeof interval === "number" ? `${interval}s` : "-");
  line("next fire", isoOrDash(status.next_fire_at));
  line("last fired", isoOrDash(status.last_fired_at));
  line("delivery mode", textOrDash(status.delivery_mode));
  line("last event", isoOrDash(status.last_event_at));
  line("secret rotated", isoOrDash(status.secret_rotated_at));
  line("last run", textOrDash(either("last_run_id")));
  line("last error", textOrDash(either("last_error")));
  const counts = status.counts;
  if (counts !== null && typeof counts === "object" && !Array.isArray(counts)) line("runs", runCounts(counts as JsonObject));
  const contracts = Array.isArray(status.contracts) ? status.contracts : Array.isArray(job.contracts) ? job.contracts : undefined;
  if (contracts !== undefined) {
    for (const contract of contracts as JsonObject[]) {
      line("program", `${textOrDash(contract.program_ref)} enabled=${boolOrDash(contract.enabled)} model=${textOrDash(contract.model)}`);
    }
  } else line("program", textOrDash(job.program_ref));
  line("endpoint URL", textOrDash(result.endpoint_url));
  line("signing secret", textOrDash(result.signing_secret));
  const rawId = typeof job.id === "string" ? job.id : "";
  if (kind === "schedule") text += nextLine(environment, ["job", "configure", rawId, "--interval-seconds", "N", "--yes"]);
  else if (kind === "webhook") text += nextLine(environment, ["job", "deliveries", rawId]) + nextLine(environment, ["job", "contract", "list", rawId]);
  const lastRun = either("last_run_id");
  if (typeof lastRun === "string") text += nextLine(environment, ["run", "show", lastRun]);
  return text;
}

/** Readable `job rotate-secret` output. */
function humanRotated(result: JsonObject, environment: Environment): string {
  const id = typeof result.id === "string" ? result.id : "";
  let text = `Rotated the signing secret of job ${humanSafeScalar(id)}\n`;
  for (const [label, field] of [["endpoint URL", "endpoint_url"], ["signing secret", "signing_secret"]] as const) {
    const value = textOrDash(result[field]);
    if (value !== "-") text += `  ${label}: ${value}\n`;
  }
  return text + nextLine(environment, ["job", "deliveries", id]);
}

function humanList(result: JsonObject): string {
  const jobs = result.jobs as JsonObject[];
  const limit = typeof result.max_jobs === "number" ? ` of ${result.max_jobs} allowed` : "";
  let text = `Jobs: ${jobs.length}${limit}\n`;
  if (jobs.length === 0) text += "No jobs.\n";
  for (const job of jobs) {
    text += `${textOrDash(job.id)} ${textOrDash(job.type)} name=${textOrDash(job.name)} program=${textOrDash(job.program_ref)} interval=${textOrDash(job.interval_seconds)} next=${isoOrDash(job.next_fire_at)}\n`;
  }
  const types = (result.types as JsonObject[]).map((kind) => textOrDash(kind.id)).join(", ");
  return `${text}Types: ${types === "" ? "-" : types}\n`;
}

function humanDeliveries(result: JsonObject): string {
  const items = result.deliveries as JsonObject[];
  if (items.length === 0) return "No deliveries.\n";
  return items.map((delivery) =>
    `${textOrDash(delivery.id)} ${isoOrDash(delivery.received_at)} ${textOrDash(delivery.outcome)} test=${String(delivery.test_only)} runs=${humanSafeScalar(delivery.runs === undefined ? "null" : canonicalJson(delivery.runs))}\n`,
  ).join("");
}

function humanContracts(result: JsonObject): string {
  const contracts = result.contracts as JsonObject[];
  if (contracts.length === 0) return "No contracts.\n";
  return contracts.map((contract) => {
    const line = `${textOrDash(contract.program_ref)} enabled=${typeof contract.enabled === "boolean" ? String(contract.enabled) : "-"} model=${textOrDash(contract.model)}\n`;
    const run = isObject(contract.run_configuration) ? contract.run_configuration : undefined;
    if (run === undefined) return line;
    const repository = Array.isArray(run.context_repositories) && isObject(run.context_repositories[0]) ? run.context_repositories[0] : undefined;
    const names = [
      ...(isObject(run.inputs) ? Object.keys(run.inputs) : []),
      ...(Array.isArray(run.input_entries) ? run.input_entries.map((entry) => (isObject(entry) && typeof entry.name === "string" ? entry.name : "")) : []),
    ];
    const settings = settingsText({
      effort: run.reasoning_effort, repository: repository?.url, branch: repository?.branch,
      output: isObject(run.output) ? run.output : undefined, inputs: names,
      files: Array.isArray(run.stored_files) ? run.stored_files.map((file) => (isObject(file) && typeof file.name === "string" ? file.name : "")) : [],
    });
    return settings === undefined ? line : `${line}  settings: ${settings}\n`;
  }).join("");
}

/** The human `settings:` text of an attach request body (clears, sent as null, are not shown). */
function settingsLine(body: JsonObject): string | undefined {
  return settingsText({
    effort: body.reasoning_effort, repository: body.repository_url, branch: body.repository_branch,
    output: isObject(body.output) ? body.output : undefined, inputs: isObject(body.inputs) ? Object.keys(body.inputs) : [],
    files: isObject(body.files) ? Object.keys(body.files) : [],
  });
}

/**
 * `reasoning effort E; repository URL@BRANCH; commit output URL; inputs k1,
 * k2; files n1, n2` with only the parts present; input values and file
 * content are never shown.
 */
function settingsText(parts: { effort: Json | undefined; repository: Json | undefined; branch: Json | undefined; output: JsonObject | undefined; inputs: string[]; files: string[] }): string | undefined {
  const at = (url: Json | undefined, branch: Json | undefined): string => `${humanSafeScalar(String(url))}${typeof branch === "string" ? `@${humanSafeScalar(branch)}` : ""}`;
  const shown: string[] = [];
  if (typeof parts.effort === "string") shown.push(`reasoning effort ${humanSafeScalar(parts.effort)}`);
  if (typeof parts.repository === "string") shown.push(`repository ${at(parts.repository, parts.branch)}`);
  if (typeof parts.output?.repository === "string") shown.push(`commit output ${humanSafeScalar(parts.output.repository)}`);
  const names = parts.inputs.filter((name) => name !== "").sort(byUtf8);
  if (names.length > 0) shown.push(`inputs ${names.map(humanSafeScalar).join(", ")}`);
  const files = parts.files.filter((name) => name !== "").sort(byUtf8);
  if (files.length > 0) shown.push(`files ${files.map(humanSafeScalar).join(", ")}`);
  return shown.length === 0 ? undefined : shown.join("; ");
}

/**
 * The public run states of a job's run counts, in display order, with the
 * service's queue states each one folds (mirrors Rust `RUN_STATES`): unknown
 * service states are dropped.
 */
const RUN_STATES: ReadonlyArray<readonly [string, readonly string[]]> = [
  ["queued", ["pending", "queued"]],
  ["running", ["claimed", "running"]],
  ["completed", ["completed"]],
  ["failed", ["failed", "error", "ambiguous"]],
  ["cancelled", ["cancelled", "canceled"]],
  ["awaiting_billing", ["billing_pending", "pending_funds"]],
];

/** The human label of a public run state. */
function runStateLabel(state: string): string {
  return state === "awaiting_billing" ? "awaiting billing settlement" : state;
}

/** The human summary of a job's run counts: each nonzero public state with its label, in order (mirrors Rust `run_counts`). */
function runCounts(counts: JsonObject): string {
  const shown = RUN_STATES.map(([state]) => [state, counts[state]] as const)
    .filter(([, count]) => typeof count === "number" && Number.isSafeInteger(count) && count > 0)
    .map(([state, count]) => `${runStateLabel(state)} ${String(count)}`);
  return shown.length === 0 ? "none yet" : shown.join(", ");
}
