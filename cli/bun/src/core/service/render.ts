// Envelopes, stream lines, planned requests and human rendering for service
// service operations, plus the shared text validator and redaction. Output
// must match the Rust product exactly.
import { createHash } from "node:crypto";
import { canonicalJson as canonical, humanSafeDetail, humanSafeScalar, jsonLine } from "../output";
import { failure } from "../errors";
import { RunnerFailure, type OutputMode } from "../types";
import runErrorsSource from "../../../../shared/fixtures/service/run-errors.v1.json" with { type: "text" };
import { environmentLabel, manifest, operation as manifestOperation, type Environment, type Json, type JsonObject, type ManifestOperation, type ManifestRequest } from "./manifest";

export interface Extras {
  nextBefore?: string;
  runId?: string;
  human?: string;
  /** Words after `cli` that fetch the next page, without `--before`. */
  pageWords?: string[];
}

/** Compact JSON with sorted keys (identical in both products; the one `output.canonicalJson`). */
export function canonicalJson(value: Json): string {
  return canonical(value);
}

/** The shared text validator: non-empty, at most `max` code points, no C0 or DEL. */
export function validText(value: string, max: number): boolean {
  return value.length > 0 && Array.from(value).length <= max && !/[\u0000-\u001f\u007f]/u.test(value);
}

/** Replaces every `rr_test_…` token and the known credential with [REDACTED]. */
export function redact(value: string, credential?: string): string {
  const text = credential !== undefined && credential.length > 0 ? value.split(credential).join("[REDACTED]") : value;
  return text.replace(/rr_test_[A-Za-z0-9]*/gu, "[REDACTED]");
}

/** details.serviceMessage: controls become spaces, keys redacted, 512 code points; blank dropped. */
export function sanitizeServiceMessage(value: string, credential?: string): string | undefined {
  const spaced = value.replace(/[\u0000-\u001f\u007f]/gu, " ");
  const cut = Array.from(redact(spaced, credential)).slice(0, 512).join("");
  return cut.trim().length === 0 ? undefined : cut;
}

/** The run error words (`shared/fixtures/service/run-errors.v1.json`). */
const RUN_ERRORS = JSON.parse(runErrorsSource as unknown as string) as { debugEnv: string; messages: Array<{ contains: string; message: string }>; fallback: string };

/** Whether run error text is printed as the service sent it (the `debugEnv` variable set to `1`); set once per invocation. */
let rawRunErrors = false;

/** Reads the run error debug variable from the invocation's environment (mirrors Rust `configure_run_errors`). */
export function configureRunErrors(env: Record<string, string | undefined>): void {
  rawRunErrors = env[RUN_ERRORS.debugEnv] === "1";
}

/**
 * The words printed for a hosted run's error text: the first mapped message
 * whose `contains` the text includes, else the generic fallback, so no
 * internal stage name is echoed. The debug variable keeps the text (mirrors
 * Rust `run_error_text`).
 */
export function runErrorText(raw: string): string {
  if (rawRunErrors) return raw;
  return RUN_ERRORS.messages.find((entry) => raw.includes(entry.contains))?.message ?? RUN_ERRORS.fallback;
}

/**
 * The public billing state of a run (`settled`, `settling` or `unknown`): a
 * charge the service has not settled yet (its `parked`, `pending`) is
 * `settling` (mirrors Rust `public_billing`).
 */
export function publicBilling(raw: string): string {
  if (raw === "settled") return "settled";
  return raw === "settling" || raw === "parked" || raw === "pending" ? "settling" : "unknown";
}

/** The human label of a public billing state: nothing is charged for a run priced at $0 (mirrors Rust `billing_label`). */
export function billingLabel(state: string, priceCents: Json | undefined): string {
  if (state === "") return "";
  if (priceCents === 0) return "nothing charged";
  return state === "settling" ? "settling (the final price is being confirmed)" : state;
}

/** A run status message in public words: the service's `reactor MODEL` is `Running on MODEL` (mirrors Rust `status_message`). */
export function statusMessage(raw: string): string {
  if (raw === "reactor") return "Running";
  if (raw.startsWith("reactor ")) return `Running on ${raw.slice("reactor".length).trimStart()}`;
  return raw;
}

/** Redacts credential material from every string in an error's details. */
export function redactDetails(details: Record<string, unknown> | undefined, credential?: string): void {
  if (details === undefined) return;
  const walk = (value: unknown): unknown => {
    if (typeof value === "string") return redact(value, credential);
    if (Array.isArray(value)) return value.map(walk);
    if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, walk(item)]));
    return value;
  };
  for (const key of Object.keys(details)) details[key] = walk(details[key]);
}

/**
 * details.plannedRequest / the --preview result for one request: the method,
 * what it does in words (`description`), the body digest and the non-secret
 * `summary` of the caller's inputs. The service route and query stay internal
 * (mirrors Rust `planned_request`).
 */
export function plannedRequest(operation: ManifestOperation, template: ManifestRequest, _path: string, _query: Array<[string, string]>, body?: Uint8Array): JsonObject {
  const planned: JsonObject = {
    operation: operation.id,
    method: template.method,
    description: operationSentence(operation),
    bodySha256: body === undefined ? null : createHash("sha256").update(body).digest("hex"),
    bodyBytes: body === undefined ? null : body.length,
    effect: operation.effect,
  };
  const summary = bodySummary(body);
  if (summary !== undefined) planned.summary = summary;
  return planned;
}

/** The first sentence of an operation's summary, ending in a period (mirrors Rust `operation_sentence`). */
function operationSentence(operation: ManifestOperation): string {
  const first = operation.summary.split("\n")[0] ?? "";
  return `${(first.split(". ")[0] ?? "").replace(/\.+$/u, "")}.`;
}

interface SummaryField { body: string; field: string; kind: "text" | "integer" | "cents" | "keys" }

function summaryFields(): SummaryField[] {
  return (manifest as unknown as { confirmation: { summaryFields: SummaryField[] } }).confirmation.summaryFields;
}

/**
 * plannedRequest.summary: the non-secret top-level body fields
 * the manifest lists in confirmation.summaryFields, so a preview or
 * CONFIRMATION_REQUIRED shows what the digest stands for. Undefined when the
 * body is not a JSON object or carries none of them.
 */
export function bodySummary(body?: Uint8Array): JsonObject | undefined {
  if (body === undefined) return undefined;
  let object: Json;
  try { object = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(body)) as Json; }
  catch { return undefined; }
  if (object === null || typeof object !== "object" || Array.isArray(object)) return undefined;
  const summary: JsonObject = {};
  for (const field of summaryFields()) {
    if (!Object.hasOwn(object, field.body)) continue;
    const value = object[field.body]!;
    if (field.kind === "text") {
      if (typeof value === "string" && validText(value, 200)) summary[field.field] = value;
    } else if (field.kind === "integer" || field.kind === "cents") {
      if (typeof value === "number" && Number.isSafeInteger(value)) summary[field.field] = value;
    } else if (field.kind === "keys") {
      if (value !== null && typeof value === "object" && !Array.isArray(value)) {
        summary[field.field] = Object.keys(value).filter((key) => validText(key, 200)).sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
      }
    }
  }
  return Object.keys(summary).length > 0 ? summary : undefined;
}

/** One human line for plannedRequest.summary: `field=value` pairs in manifest order, keys joined with commas, cents also in dollars. */
/** The human label of a summary field (mirrors Rust `summary_label`); JSON keeps the field names. */
const SUMMARY_LABELS: Record<string, string> = {
  programRef: "program", reasoning_effort: "reasoning effort", inputKeys: "inputs", interval_seconds: "every", delivery_mode: "delivery mode", amount_cents: "amount",
};

/** A whole number of seconds as a short duration (`24h`, `90m`, `45s`). */
export function durationText(seconds: number): string {
  if (seconds > 0 && seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds > 0 && seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

/** One human line for `plannedRequest.summary`: `label value` pairs in manifest order (mirrors Rust `summary_line`). */
export function summaryLine(summary: Json | undefined): string | undefined {
  if (summary === null || summary === undefined || typeof summary !== "object" || Array.isArray(summary)) return undefined;
  const parts: string[] = [];
  for (const field of summaryFields()) {
    if (!Object.hasOwn(summary, field.field)) continue;
    const value = summary[field.field]!;
    let text: string;
    if (Array.isArray(value)) text = value.filter((item) => typeof item === "string").join(", ");
    else if (typeof value === "number" && field.kind === "cents") {
      const magnitude = Math.abs(value);
      text = `$${value < 0 ? "-" : ""}${Math.floor(magnitude / 100)}.${String(magnitude % 100).padStart(2, "0")}`;
    } else if (typeof value === "number" && field.field === "interval_seconds") text = durationText(value);
    else text = scalar(value);
    parts.push(`${SUMMARY_LABELS[field.field] ?? field.field} ${text}`);
  }
  return parts.length > 0 ? humanSafeScalar(parts.join("; ")) : undefined;
}

/** What each write operation does, for the human `Effect:` line (mirrors Rust `effect_text`). */
const WRITE_EFFECTS: Record<string, string> = {
  "org.create": "creates an organization; its slug is permanent, so this cannot be undone",
  "run.cancel": "stops the run; this cannot be undone",
  "job.rotate-secret": "replaces the signing secret; the old one stops working and this cannot be undone",
  "run.input": "sends an instruction to the run",
  "program.save": "saves a new revision of the program",
  "result.unpublish": "removes the published result",
  "job.create": "creates a job that starts no runs",
  "job.update": "changes the job",
  "job.configure": "changes the job",
  "job.contract.attach": "attaches a program to the job",
  "job.contract.detach": "detaches a program from the job",
  "org.rename": "renames the organization",
  "org.default": "changes your default organization",
  "org.member.role": "changes the member's role",
  "org.invite": "creates an invitation to the organization",
  "org.invitation.accept": "joins you to the organization",
};

/** What a planned request's effect means for the person, in words (mirrors Rust `effect_text`); JSON keeps the effect class. */
function effectText(planned: JsonObject): string {
  const effect = typeof planned.effect === "string" ? planned.effect : "";
  const operation = typeof planned.operation === "string" ? planned.operation : "";
  let text: string;
  if (effect === "money") {
    text = operation === "run.submit" || operation === "program.draft" ? "starts a paid run"
      : operation === "wallet.topup" ? "starts a card payment"
      : operation === "wallet.redeem" ? "adds credit to your wallet"
      : operation.startsWith("job.") ? "starts paid runs" : "costs money";
  } else if (effect === "outward") {
    text = operation === "run.share" ? "creates a public 24-hour link that cannot be revoked"
      : operation === "program.visibility" ? "changes who can see the program"
      : operation === "result.publish" ? "publishes a public result" : "makes something public";
  } else if (effect === "destructive") text = "cannot be undone";
  else if (effect === "write") text = Object.hasOwn(WRITE_EFFECTS, operation) ? WRITE_EFFECTS[operation]! : "changes your account";
  else if (effect === "read") text = "reads only";
  else text = effect;
  return humanSafeScalar(text);
}

/** The human line of a schedule job's plan: when its first run fires (mirrors Rust `first_run_line`). */
function firstRunLine(planned: JsonObject): string | undefined {
  const interval = (planned.summary as JsonObject | undefined)?.interval_seconds;
  if (planned.operation !== "job.create" || typeof interval !== "number" || !Number.isSafeInteger(interval)) return undefined;
  return `First run: about 1 second after the job is created, then every ${durationText(interval)}`;
}

/** The human hold line of a plan with a quote (The hold is flat, not an estimate of this run's price). */
function holdLine(planned: JsonObject): string | undefined {
  const hold = ((planned.quote as JsonObject | undefined)?.hold as JsonObject | undefined)?.hold_usd;
  return typeof hold === "string" ? `Hold: $${humanSafeScalar(hold)} (set aside from the wallet while the run is live; not its price)` : undefined;
}

function problemJson(error: RunnerFailure): Json {
  return plainInvocation(error).toJSON() as unknown as Json;
}

/**
 * A service command's `INVOCATION_INVALID` says what was wrong in plain words
 * instead of the runner's generic message: an unknown command, an unknown
 * option, or another mistake in the command line (mirrors Rust
 * `plain_invocation`). Other errors are unchanged.
 */
export function plainInvocation(error: RunnerFailure): RunnerFailure {
  if (error.code !== "INVOCATION_INVALID" || error.message !== failure("INVOCATION_INVALID").message) return error;
  const reason = typeof error.details?.reason === "string" ? error.details.reason : "";
  const message = reason.startsWith("unknown command") || reason.includes("is not a command") ? "Unknown command."
    : reason.startsWith("unknown option") ? "Unknown option." : "That command isn't quite right.";
  return new RunnerFailure({ code: error.code, boundary: error.boundary, message, action: error.action, exitCode: error.exitCode, retryable: error.retryable, ...(error.details === undefined ? {} : { details: error.details }) });
}

/**
 * The openprose.service-operation/1 envelope. A paged operation's successful
 * result carries `nextBefore` (null on the last page); an error envelope has
 * none (mirrors Rust `render::envelope`).
 */
export function envelope(operation: ManifestOperation, result: Json | RunnerFailure, nextBefore?: string): JsonObject {
  const value: JsonObject = { schema: "openprose.service-operation/1" };
  value.operation = operation.id;
  value.interaction = operation.interaction;
  const failed = result !== null && typeof result === "object" && !Array.isArray(result) && "toJSON" in result && typeof (result as { toJSON?: unknown }).toJSON === "function";
  if (failed) value.result = null;
  else if (operation.output.paged && result !== null && typeof result === "object" && !Array.isArray(result)) value.result = { ...(result as JsonObject), nextBefore: nextBefore ?? null };
  else value.result = result as Json;
  value.problem = failed ? problemJson(result as RunnerFailure) : null;
  return value;
}

/**
 * The envelope of an invocation rejected before an operation ran: `operation`
 * is the one the argv names, else the literal `cli`. Mirrors Rust
 * `render::rejected`.
 */
export function rejectedEnvelope(operation: ManifestOperation | undefined, error: RunnerFailure): JsonObject {
  const value: JsonObject = { schema: "openprose.service-operation/1" };
  value.operation = operation?.id ?? "cli";
  value.interaction = operation?.interaction ?? null;
  value.result = null;
  value.problem = problemJson(error);
  return value;
}

/** One openprose.service-event/1 line. */
export function eventLine(runId: string | undefined, sequence: number | undefined, at: string | undefined, type: string, data: Json): JsonObject {
  const value: JsonObject = { schema: "openprose.service-event/1" };
  value.runId = runId ?? null;
  value.sequence = sequence ?? null;
  value.at = at ?? null;
  value.type = type;
  value.data = data;
  return value;
}

/**
 * A list operation's JSONL output (decision 5): one
 * `openprose.service-record/1` line per item of the manifest's
 * `output.records` collection, then one `openprose.service-page/1` trailer with
 * the item count, `nextBefore` (null when there is no next page) and `meta`,
 * the rest of the result. Mirrors Rust `render::record_lines`.
 */
export function recordLines(operation: ManifestOperation, collection: string, result: JsonObject, nextBefore?: string): JsonObject[] {
  const head = (schema: string): JsonObject => {
    const value: JsonObject = { schema };
    value.operation = operation.id;
    value.collection = collection;
    return value;
  };
  const items = result[collection] as Json[];
  const lines = items.map((item, index) => ({ ...head("openprose.service-record/1"), index, record: item }));
  const meta: JsonObject = {};
  for (const [key, value] of Object.entries(result)) if (key !== collection) meta[key] = value;
  // The trailer ends a successful listing: exitCode is the process exit, 0.
  lines.push({ ...head("openprose.service-page/1"), interaction: operation.interaction, count: items.length, nextBefore: nextBefore ?? null, meta, exitCode: 0 } as never);
  return lines as JsonObject[];
}

/**
 * The structured stdout of a non-stream operation (mirrors the JSON and JSONL
 * arms of Rust `render::outcome`): one envelope line, or, for a list
 * operation's successful `--output jsonl`, its record lines and page trailer.
 */
export function structuredOutput(operation: ManifestOperation, mode: "json" | "jsonl", result: Json | RunnerFailure, nextBefore?: string): string {
  const collection = operation.output.records;
  const failed = result !== null && typeof result === "object" && !Array.isArray(result) && "toJSON" in result;
  if (mode === "jsonl" && !failed && collection !== undefined && result !== null && typeof result === "object" && !Array.isArray(result) && Array.isArray((result as JsonObject)[collection])) {
    return recordLines(operation, collection, result as JsonObject, nextBefore).map((line) => jsonLine(line)).join("");
  }
  return jsonLine(envelope(operation, result, nextBefore));
}

/** {@link structuredOutput} of a manifest operation by id (the account and package commands, which keep their own parsers). */
export function structuredOutputFor(id: string, mode: "json" | "jsonl", result: Json | RunnerFailure): string {
  const operation = manifestOperation(id);
  if (operation === undefined) throw new Error(`no manifest operation ${id}`);
  return structuredOutput(operation, mode, result);
}

/** 9999-12-31T23:59:59.999Z in epoch milliseconds. */
export const MAX_ISO_MS = 253_402_300_799_999;

/** Epoch milliseconds as RFC 3339 UTC with milliseconds (`toISOString`); undefined otherwise. */
export function isoMs(value: Json | undefined): string | undefined {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || value > MAX_ISO_MS) return undefined;
  return new Date(value).toISOString();
}

/** The additive `<name>_iso` companions; null when the field is null. */
export function addIso(object: JsonObject, names: readonly string[]): void {
  for (const name of names) {
    if (!Object.hasOwn(object, name)) continue;
    object[`${name}_iso`] = isoMs(object[name]) ?? null;
  }
}

/** A service dollar string (`-?D+.DD`, at most 13 whole digits) as whole cents: the `hold_cents` companion of `hold_usd` (mirrors Rust `usd_cents`). */
export function usdCents(value: string): number | undefined {
  const match = /^(-?)([0-9]{1,13})\.([0-9]{2})$/u.exec(value);
  if (match === null) return undefined;
  const total = Number(match[2]) * 100 + Number(match[3]);
  return match[1] === "-" && total !== 0 ? -total : total;
}

/** Whole cents as dollars for human output: `$1.23`, `-$0.02` (formatting only). */
export function dollars(cents: number): string {
  const magnitude = Math.abs(cents);
  return `${cents < 0 ? "-" : ""}$${Math.floor(magnitude / 100)}.${String(magnitude % 100).padStart(2, "0")}`;
}

/** A cents value as dollars, or `-` when it is not an integer. */
export function dollarsOrDash(value: Json | undefined): string {
  return typeof value === "number" && Number.isSafeInteger(value) ? dollars(value) : "-";
}

/** One human `Next: prose ...` line; `words` follow `cli`. */
export function nextLine(environment: Environment, words: readonly string[]): string {
  return `Next: ${humanSafeScalar(argvText(followUpArgv(environment, "human", words)))}\n`;
}

/** The absolute URL of a service path (`/webhooks/...`) in this environment. */
export function absoluteUrl(environment: Environment, path: string): string | undefined {
  return path.startsWith("/") && !path.startsWith("//") ? `${environment.origin}${path}` : undefined;
}

function scalar(value: Json | undefined): string {
  if (typeof value === "string") return humanSafeScalar(value);
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean" || typeof value === "number") return JSON.stringify(value);
  return humanSafeScalar(canonicalJson(value));
}

/** What a planned request does, in words (its `description`; mirrors Rust `plan_line`). */
function planLine(planned: JsonObject): string {
  return humanSafeScalar(typeof planned.description === "string" ? planned.description : "");
}

/** `Owner: HANDLE (verified as you)` for a plan whose OWNER/SLUG was confirmed as the caller. */
function ownerLine(planned: JsonObject): string | undefined {
  const owner = planned.owner;
  if (owner === null || typeof owner !== "object" || Array.isArray(owner)) return undefined;
  const { handle, verified } = owner as JsonObject;
  return typeof handle === "string" && verified === true ? `Owner: ${humanSafeScalar(handle)} (verified as you)` : undefined;
}

/** Default human rendering: sorted `key: value` lines; nested values as canonical JSON. */
export function humanResult(result: Json): string {
  if (result !== null && typeof result === "object" && !Array.isArray(result) && result.preview === true) {
    const planned = (result.plannedRequest ?? {}) as JsonObject;
    let text = `Preview: ${planLine(planned)}\n`;
    const summary = summaryLine(planned.summary);
    if (summary !== undefined) text += `Summary: ${summary}\n`;
    const owner = ownerLine(planned);
    if (owner !== undefined) text += `${owner}\n`;
    text += `Effect: ${effectText(planned)}\n`;
    const hold = holdLine(planned);
    if (hold !== undefined) text += `${hold}\n`;
    const first = firstRunLine(planned);
    if (first !== undefined) text += `${first}\n`;
    return `${text}Nothing was changed.\n`;
  }
  if (result === null || typeof result !== "object" || Array.isArray(result)) return `${scalar(result)}\n`;
  return Object.keys(result).sort().map((key) => `${humanSafeScalar(key)}: ${scalar(result[key])}\n`).join("");
}

/** Shell-quotes one argument for a copyable command line. */
export function shellQuote(value: string): string {
  return value.length > 0 && /^[A-Za-z0-9_./:=@%+,-]+$/u.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`;
}

/**
 * The one follow-up argv renderer (mirrors Rust
 * `render::follow_up_argv`). Every copyable command (`resumeArgv`,
 * `cancelArgv`, `suggestedArgv`, commands inside reasons and actions) keeps
 * the invocation's output mode for machine output. It never names a service:
 * a developer build reads its endpoint from the process environment, which
 * the copied command inherits. `words` follow `cli`.
 */
export function followUpArgv(_environment: Environment, mode: OutputMode, words: readonly string[]): string[] {
  return [...(mode === "human" ? [] : ["--output", mode]), "cli", ...words];
}

// Compile-time only (see `endpoint.ts`): public builds fold the dev branch away.
declare const PROSE_DEV_BUILD: boolean | undefined;

let invokedProgramName: string | undefined;

/**
 * The shell-quoted program that re-runs this build: `argv[0]` exactly as it
 * was invoked (a name found on PATH such as `prose-dev`, or a path), or
 * undefined for `prose` itself or a value that cannot be copied safely
 * (mirrors Rust `dev_endpoint::invoked_program`).
 */
export function invokedProgram(argv0: string | undefined): string | undefined {
  if (argv0 === undefined || argv0.length === 0 || argv0 === "prose") return undefined;
  if (/[\u0000-\u001f\u007f-\u009f]/u.test(argv0)) return undefined;
  return shellQuote(argv0);
}

/**
 * Records `argv[0]` as the program copyable commands name. Only a developer
 * build (`PROSE_DEV_BUILD`) honors it, so a copied command re-runs the same
 * build against the same endpoint; a public build always prints `prose`.
 */
export function recordInvokedName(argv0: string | undefined): void {
  if (typeof PROSE_DEV_BUILD === "boolean" && PROSE_DEV_BUILD) {
    if (invokedProgramName === undefined) invokedProgramName = invokedProgram(argv0);
  }
}

/** The program that starts every copyable command (mirrors Rust `render::product`). */
export function product(): string {
  return invokedProgramName ?? "prose";
}

/** Rewrites every `` `prose ...` `` command in a fixed text to name {@link product}. */
export function localizeProduct(text: string): string {
  const name = product();
  return name === "prose" ? text : text.split("`prose ").join(`\`${name} `);
}

/**
 * A help topic as this build prints it (mirrors Rust `localize_help`):
 * `help.v1.json` stores `prose` as the program placeholder, and every command
 * in it (a line starting with `prose `, the `Usage: prose` line and every
 * `` `prose ...` `` span) names {@link product} instead, the same program
 * errors name. The identity in a public build.
 */
export function localizeHelp(text: string): string {
  return localizeHelpFor(product(), text);
}

/** {@link localizeHelp} with an explicit program. */
export function localizeHelpFor(program: string, text: string): string {
  if (program === "prose") return text;
  return text.split(/(?<=\n)/u).map((line) => {
    const indent = line.length - line.replace(/^ +/u, "").length;
    const head = line.slice(0, indent);
    let rest = line.slice(indent);
    if (rest.startsWith("prose ")) rest = `${program} ${rest.slice("prose ".length)}`;
    else if (rest.startsWith("Usage: prose ")) rest = `Usage: ${program} ${rest.slice("Usage: prose ".length)}`;
    return head + rest.split("`prose ").join(`\`${program} `);
  }).join("");
}

/** A copyable command line (`prose ...`, shell-quoted, not yet terminal-safe). */
export function argvText(argv: readonly string[]): string {
  return argvTextFor(product(), argv);
}

/** {@link argvText} with an explicit (already shell-quoted) program. */
export function argvTextFor(program: string, argv: readonly string[]): string {
  return `${program} ${argv.map(shellQuote).join(" ")}`;
}

/** A copyable follow-up command line; `words` (space-separated) follow `cli`. */
export function followUpCommand(environment: Environment, mode: OutputMode, words: string): string {
  return argvText(followUpArgv(environment, mode, words.split(" ").filter((word) => word.length > 0)));
}

function argvLine(argv: unknown): string | undefined {
  if (!Array.isArray(argv) || !argv.every((word) => typeof word === "string")) return undefined;
  return humanSafeScalar(argvText(argv as string[]));
}

/** Human error text (stderr). Identical in both products. */
export function humanError(label: string, given: RunnerFailure): string {
  const error = plainInvocation(given);
  const details = error.details ?? {};
  let text = `${label}: ${error.code}: ${humanSafeScalar(error.message)}\n`;
  if (typeof details.reason === "string") text += `Detail: ${humanSafeDetail(details.reason)}\n`;
  // The HTTP status stays in JSON (`details.serviceStatus`); a person reads
  // the cause in the headline and Detail, and the service's own code.
  if (typeof details.serviceCode === "string") text += `Service code: ${humanSafeScalar(details.serviceCode)}\n`;
  if (typeof details.serviceMessage === "string") text += `Service message: ${humanSafeScalar(details.serviceMessage)}\n`;
  if (typeof details.webUrl === "string") text += `Web: ${humanSafeScalar(details.webUrl)}\n`;
  if (typeof details.runId === "string") text += `Run: ${humanSafeScalar(details.runId)}${details.afterSequence !== undefined ? ` (after sequence ${String(details.afterSequence)})` : ""}\n`;
  if (details.plannedRequest !== undefined) {
    const planned = details.plannedRequest as JsonObject;
    text += `Planned: ${planLine(planned)}\n`;
    const summary = summaryLine(planned.summary);
    if (summary !== undefined) text += `Planned summary: ${summary}\n`;
    const owner = ownerLine(planned);
    if (owner !== undefined) text += `${owner}\n`;
    const hold = holdLine(planned);
    if (hold !== undefined) text += `${hold}\n`;
    const first = firstRunLine(planned);
    if (first !== undefined) text += `${first}\n`;
  }
  const confirm = argvLine(details.confirmArgv);
  if (confirm !== undefined) text += `Confirm with: ${confirm}\n`;
  const preview = argvLine(details.previewArgv);
  if (preview !== undefined) text += `Preview with: ${preview}\n`;
  return `${text}Action: ${humanSafeScalar(humanAction(error, details))}\n`;
}

/**
 * The Action as a person reads it (mirrors Rust `human_action`): JSON keeps the catalog Action, which names
 * `details.*` fields; human output names the lines printed above instead. An Action a handler replaced is kept.
 */
function humanAction(error: RunnerFailure, details: Record<string, unknown>): string {
  const has = (key: string) => typeof details[key] === "string";
  if (error.code === "SERVICE_REQUEST_REJECTED" && error.action.startsWith("Correct the request as details.")) {
    const present = ([["reason", "Detail"], ["serviceMessage", "Service message"], ["serviceCode", "Service code"]] as const)
      .filter(([key]) => has(key)).map(([, label]) => label);
    if (present.length === 1) return `Correct the request as the ${present[0]!} line above describes, then retry.`;
    if (present.length > 1) return `Correct the request as the ${present.slice(0, -1).join(", ")} and ${present.at(-1)!} lines above describe, then retry.`;
  }
  if (!has("reason") || error.action !== failure(error.code).action) return error.action;
  switch (error.code) {
    case "SERVICE_WATCH_DEADLINE": return "The run continues; keep following it with the command in Detail above.";
    case "HOSTED_RUN_DETACHED": return "Keep following the run with the command in Detail above; never submit the run again to resume it, because that starts a second paid run.";
    case "HOSTED_RUN_CANCELLED": return "Read the cancelled run with the command in Detail above; submit again only to start a new run.";
    case "RUN_SUBMISSION_AMBIGUOUS": return "Check recent runs with the command in Detail above before submitting again; submitting again could start a second paid run.";
    case "EXAMPLE_NOT_VIEWABLE": return "Read the example at the Web address above when one is printed, or pick an example that `cli example list` does not mark for viewing on the web.";
    default: return error.action;
  }
}

export function humanErrorFor(environment: Environment, error: RunnerFailure): string {
  return humanError(environmentLabel(environment), error);
}

/**
 * Fixture header patterns: ECMAScript regular expressions (the Rust product
 * supports the documented subset; `undefined` means unsupported syntax).
 */
export function patternMatches(pattern: string, value: string): boolean | undefined {
  if (/\(\?[=!<]|\\[bBpPkuxc0-9]/u.test(pattern)) return undefined;
  try { return new RegExp(pattern, "u").test(value); } catch { return undefined; }
}
