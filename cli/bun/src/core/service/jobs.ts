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
// - `job contract attach|detach` take a pinned `OWNER/SLUG@REV`.
//
// Mirrors cli/rust/crates/prose-runner-core/src/service/jobs.rs.
import { failure, invocationFailure } from "../errors";
import { humanSafeScalar, quote as quoteText } from "../output";
import { RunnerFailure } from "../types";
import { readSource } from "./fs";
import { encodeSegment, jsonObject, parseJson, requestFor, type Request } from "./http";
import type { Context } from "./index";
import { didYouMean, type Environment, type Json, type JsonObject } from "./manifest";
import { parseOwnAllowed, parseProgramRef, pinned, resolveToRun, validSlug } from "./program-ref";
import { absoluteUrl, addIso, canonicalJson, isoMs, nextLine, usdCents, validText } from "./render";

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
    case "job.contract.attach": return await contractChange(context, true);
    case "job.contract.detach": return await contractChange(context, false);
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
function isProgramRef(text: string): boolean {
  if (text.length > 200) return false;
  try { parseProgramRef(text, true); return true; } catch { return false; }
}

type Kind =
  | { text: number } | { prose: number }
  | "integer" | "epochMs" | "bool" | "uuid" | "runId" | "modelId" | "programRef" | "slug" | "hex64";

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

function projectRunConfiguration(value: Json, field: string): JsonObject {
  const source = object(value, field);
  const target: JsonObject = {};
  copy(target, source, field, [["model", "modelId", false], ["environment", { text: 64 }, false], ["reasoning_effort", { text: 32 }, false]]);
  if (Object.hasOwn(source, "inputs")) {
    const inputs = object(source.inputs, `${field}.inputs`);
    if (Object.keys(inputs).length > 100 || Object.values(inputs).some((item) => typeof item !== "string")) throw protocol(`${field}.inputs`);
    // Money rule: no output property name may match /cost/i. An input
    // so named is a user key, not a money field, and `job configure` replaces
    // every input, so it is kept losslessly as a {name, value} entry of
    // `input_entries` instead of being dropped. Entries are ordered
    // by UTF-8 bytes, as the Rust product's sorted map.
    const costNamed = (name: string): boolean => /[Cc][Oo][Ss][Tt]/u.test(name);
    target.inputs = Object.fromEntries(Object.entries(inputs).filter(([name]) => !costNamed(name)));
    const entries = Object.entries(inputs).filter(([name]) => costNamed(name))
      .sort(([a], [b]) => Buffer.compare(Buffer.from(a, "utf8"), Buffer.from(b, "utf8")));
    if (entries.length > 0) target.input_entries = entries.map(([name, value]) => ({ name, value }));
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

/** The anonymous GET /run/quote hold for the confirmation plan (default environment); the price policy reference stays internal. */
async function quote(context: Context): Promise<JsonObject> {
  const body = await getJson(context, 0, "/run/quote");
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
  else if (context.invocation.preview || !context.invocation.yes) planned.quote = await quote(context);
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
  // The contract route names differ from the job record's; both project to
  // the same public contract fields.
  const contracts = array(body.contracts ?? null, 16, "contracts").map((contract) => {
    const source = object(contract, "contracts");
    const renamed: JsonObject = {};
    for (const [from, to] of [["program_ref", "programRef"], ["slug", "programSlug"], ["enabled", "enabled"], ["model", "model"]] as const) {
      if (Object.hasOwn(source, from)) renamed[to] = source[from]!;
    }
    return projectContract(renamed, "contracts");
  });
  const result: JsonObject = { contracts };
  if (Object.hasOwn(body, "max_contracts")) result.max_contracts = scalar(body.max_contracts, "epochMs", "max_contracts");
  context.human = humanContracts(result);
  return result;
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

async function contractChange(context: Context, attach: boolean): Promise<Json> {
  const id = jobId(context);
  const value = context.argument("OWNER/SLUG@REV") ?? "";
  let reference: string;
  try { reference = pinned(parseProgramRef(value, true)) ?? ""; }
  catch { throw invocationFailure(unpinnedReason(context, value)); }
  if (attach && context.option("--model") !== undefined) {
    throw invocationFailure("the service does not accept --model when attaching a contract; attached contracts run on the service's default job model (see `cli job contract list`)");
  }
  const body = attach ? new TextEncoder().encode(canonicalJson({ program_ref: reference })) : undefined;
  const path = attach
    ? `/triggers/${encodeSegment(id)}/contracts`
    : `/triggers/${encodeSegment(id)}/contracts/${encodeSegment(reference)}`;
  const gate = context.gate(context.planned(0, path, [], body));
  if (gate.kind === "preview") return gate.result;
  const request: Request = { ...requestFor(context.operation, 0, path), ...(body === undefined ? {} : { body }) };
  const response = jsonObject(await context.send(request));
  const key = attach ? "bound" : "unbound";
  if (response[key] !== reference) throw protocol(key);
  context.human = `${attach ? "Attached" : "Detached"} ${humanSafeScalar(reference)} ${attach ? "to" : "from"} job ${humanSafeScalar(id)}.\n`
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
  return contracts.map((contract) =>
    `${textOrDash(contract.program_ref)} enabled=${typeof contract.enabled === "boolean" ? String(contract.enabled) : "-"} model=${textOrDash(contract.model)}\n`,
  ).join("");
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
