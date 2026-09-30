// Service operations: execution context, confirmation gate and
// dispatch. Mirrors cli/rust/crates/prose-runner-core/src/service/mod.rs.
import { failure, invocationFailure } from "../errors";
import { humanSafeScalar, jsonLine, quote } from "../output";
import { RunnerFailure, type GlobalFlags, type OutputMode } from "../types";
import { credentialAction, credentialFailure, malformedReason, missingReason, rejectedReason, storeUnavailable } from "./credentials";
import * as discovery from "./discovery";
import { classify, jsonObject, requestFor, Transport, type Downloaded, type Request, type Response, type Sink, type StreamOpen } from "./http";
import * as jobs from "./jobs";
import { Journal } from "./journal";
import {
  GUIDE_TEXT, RUNNER_COMMANDS, environmentLabel, guideResult, helpTopicTexts, manifest, nearest, operation as findOperation, staticResult, staticView, validCredential,
  type Correction, type Environment, type Json, type JsonObject, type ManifestOperation, type ManifestRequest, type ServiceCommand, type ServiceInvalid, type ServiceInvocation,
} from "./manifest";
import { PRODUCTION, serviceEnvironment } from "./endpoint";
import { explainOperationNotFound, explainRejected } from "./not-found";
import * as organizations from "./organizations";
import * as programs from "./programs";
import { encodeSegment } from "./http";
import { argvText, configureRunErrors, envelope, eventLine, rejectedEnvelope, structuredOutput, followUpArgv, followUpCommand, humanError, humanErrorFor, humanResult, localizeHelp, plannedRequest, redactDetails, usdCents, validText, type Extras } from "./render";
import * as results from "./results";
import * as runRecords from "./run-records";
import * as runs from "./runs";
import * as wallet from "./wallet";

export interface ServiceDependencies {
  env: Readonly<Record<string, string | undefined>>;
  processCwd: string;
  homeDir?: string;
  platform?: NodeJS.Platform;
  cancellationSignal?: AbortSignal;
  writeStdout(text: string): void;
  writeStderr(text: string): void;
}

export type Gate = { kind: "proceed" } | { kind: "preview"; result: JsonObject };

export class Context {
  nextBefore: string | undefined;
  runId: string | undefined;
  human: string | undefined;
  /** The OWNER of an own-scope OWNER/SLUG confirmed as the caller; the plan carries it as plannedRequest.owner. */
  verifiedOwner: string | undefined;
  private credential: string | undefined;
  /** Where the credential came from: `environment` or `store`. */
  private credentialSource: "environment" | "store" | undefined;
  /** Set when the service rejected the key itself (401, or 403 `Invalid API key.`). */
  private keyRejected = false;

  constructor(
    readonly invocation: ServiceInvocation,
    readonly operation: ManifestOperation,
    readonly environment: Environment,
    readonly mode: OutputMode,
    readonly transport: Transport,
    readonly deps: ServiceDependencies,
  ) {}

  argument(name: string): string | undefined { return this.invocation.arguments.get(name)?.[0]; }
  /** A copyable follow-up command line that keeps the environment and machine output mode. */
  command(words: string): string { return followUpCommand(this.environment, this.mode, words); }
  /** A follow-up argv (after the product name); `words` follow `cli`. */
  followUpArgv(words: readonly string[]): string[] { return followUpArgv(this.environment, this.mode, words); }
  /** Rewrites every `` `prose cli ...` `` command in a fixed hint to keep the environment and output mode. */
  localize(text: string): string { return text.split("`prose cli ").join(`\`${this.command("")} `); }
  option(name: string): string | undefined { return this.invocation.options.get(name)?.[0]; }
  optionValues(name: string): string[] { return this.invocation.options.get(name) ?? []; }
  flag(name: string): boolean { return this.invocation.flags.has(name); }
  request(index: number): ManifestRequest { return this.operation.requests[index]!; }
  get cwd(): string { return this.deps.processCwd; }

  /** Streamed output (JSONL events, human text chunks). */
  out(text: string): void { this.deps.writeStdout(text); }
  /** Streamed diagnostics (human status and warnings). */
  err(text: string): void { this.deps.writeStderr(text); }
  emit(line: JsonObject): void { this.deps.writeStdout(jsonLine(line)); }

  /** The bearer credential: the environment variable, then the OS store. */
  async credentialValue(): Promise<string> {
    if (this.credential !== undefined) return this.credential;
    const variable = this.environment.credentialEnv;
    const fromEnvironment = this.deps.env[variable];
    const inEnvironment = fromEnvironment !== undefined && fromEnvironment !== "";
    const source = inEnvironment ? "environment" : "store";
    let token: string;
    if (inEnvironment) token = fromEnvironment;
    else {
      let stored: string | null;
      try { stored = await this.transport.storedCredential(); }
      catch (caught) {
        if (caught instanceof RunnerFailure && caught.code === "CREDENTIAL_STORE_UNAVAILABLE") throw storeUnavailable(this.environment, caught, this.mode);
        throw caught;
      }
      if (stored === null) throw credentialFailure(this.environment, this.mode, "none", "missing", missingReason(this.environment, this.mode));
      token = stored;
    }
    if (!validCredential(token)) {
      throw credentialFailure(this.environment, this.mode, source, "malformed", this.localize(malformedReason(token, variable, inEnvironment, this.environment)));
    }
    this.credential = token;
    this.credentialSource = source;
    return token;
  }

  /** Names the credential source of a service authentication failure. */
  annotateAuth(error: RunnerFailure): RunnerFailure {
    if (error.code !== "SERVICE_AUTH_REQUIRED") return error;
    const details: Record<string, unknown> = { ...(error.details ?? {}) };
    if (Object.hasOwn(details, "credentialSource") || this.credentialSource === undefined) return error;
    const variable = this.environment.credentialEnv;
    details.credentialSource = this.credentialSource;
    details.credentialVariable = variable;
    if (this.keyRejected) {
      details.credentialProblem = "rejected";
      details.reason = rejectedReason(this.environment, this.mode, this.credentialSource);
      return withAction(failure("SERVICE_AUTH_REQUIRED"), credentialAction(this.environment, this.mode, this.credentialSource), details);
    }
    return failure("SERVICE_AUTH_REQUIRED", details);
  }

  knownCredential(): string | undefined { return this.credential; }
  /** Where the resolved credential came from, once resolved. */
  credentialOrigin(): "environment" | "store" | undefined { return this.credentialSource; }

  /** Sends one request and returns a 2xx response; other statuses are classified. */
  async send(request: Request): Promise<Response> {
    const response = await this.sendRaw(request);
    if (response.status >= 200 && response.status < 300) return response;
    throw this.classify(request, response);
  }

  async sendRaw(request: Request): Promise<Response> {
    const token = request.bearer ? await this.credentialValue() : undefined;
    return await this.transport.send(request, token);
  }

  classify(request: Request, response: Response): RunnerFailure {
    if (request.bearer && (response.status === 401 || (response.status === 403 && keyRejectedBody(response.body)))) this.keyRejected = true;
    return discovery.paidModelRefusal(classify(this.operation.contract, request, response, this.credential), response.body, this.environment, this.mode, this.credential);
  }

  async openStream(request: Request): Promise<StreamOpen> {
    const token = request.bearer ? await this.credentialValue() : undefined;
    return await this.transport.openStream(request, token);
  }

  async download(request: Request, sink: Sink, maxBytes: number): Promise<Downloaded> {
    const token = request.bearer ? await this.credentialValue() : undefined;
    return await this.transport.download(request, token, sink, maxBytes, this.operation.contract);
  }

  nowRfc3339(): string { return this.transport.nowRfc3339(); }
  monotonicMs(): number { return this.transport.monotonicMs(); }
  uuidV4(): string { return this.transport.uuidV4(); }

  journal(): Journal {
    return Journal.forEnvironment(this.deps.env, this.deps.homeDir ?? this.deps.env.HOME, this.deps.platform ?? process.platform, this.environment);
  }

  planned(index: number, path: string, query: Array<[string, string]> = [], body?: Uint8Array): JsonObject {
    const planned = plannedRequest(this.operation, this.request(index), path, query, body);
    if (this.verifiedOwner !== undefined) planned.owner = { handle: this.verifiedOwner, verified: true };
    return planned;
  }

  /**
   * Refuses a --model this account may not run before any
   * confirmation, naming the nearest offered models. `index` is the
   * operation's GET /models request. Advisory: when the lookup fails the
   * service's own check applies; only an interrupt stops here.
   */
  async checkModel(index: number, model: string): Promise<void> {
    let body: JsonObject;
    try { body = jsonObject(await this.send({ ...requestFor(this.operation, index, "/models"), class: "control" })); }
    catch (caught) {
      if (caught instanceof RunnerFailure && caught.code !== "CANCELLED") return;
      throw caught;
    }
    const offered = Array.isArray(body.models) ? body.models.filter((item): item is string => typeof item === "string" && validText(item, 64)) : [];
    // A premium model is refused here as the service would refuse it; an
    // older id the catalog still accepts is known.
    const premium = discovery.premiumInCatalog(body, model, this.environment, this.mode);
    if (premium !== undefined) throw premium;
    const known = offered.includes(model) || body.default_model === model || discovery.acceptedInCatalog(body, model);
    if (offered.length === 0 || known) return;
    const near = nearest(model, offered, 3);
    const error = invocationFailure(`--model ${quote(model)} is not a model this account may run; nearest: ${near.join(", ")}; \`${this.command("model list")}\` lists them all`);
    const argv = this.argvWithOption("--model", near[0]!);
    throw withAction(error, `Rerun with a model the service offers, for example \`${argvText(argv)}\`.`, { suggestedArgv: argv });
  }

  /** This invocation's argv with the value of `option` replaced. */
  argvWithOption(option: string, value: string): string[] {
    const argv = [...this.invocation.argv];
    for (let index = 0; index < argv.length; index += 1) {
      if (argv[index] === option && index + 1 < argv.length) { argv[index + 1] = value; index += 1; }
      else if (argv[index]!.startsWith(`${option}=`)) argv[index] = `${option}=${value}`;
    }
    return argv;
  }

  /** This invocation's argv with `option` set to `value`: replaced when given, otherwise added before any `--`. */
  argvSettingOption(option: string, value: string): string[] {
    if (this.option(option) !== undefined) return this.argvWithOption(option, value);
    const argv = [...this.invocation.argv];
    const end = argv.includes("--") ? argv.indexOf("--") : argv.length;
    argv.splice(end, 0, option, value);
    return argv;
  }

  /** This invocation's argv with its last `given` token (a positional argument as typed) replaced by `value`. */
  argvWithArgument(given: string, value: string): string[] {
    const argv = [...this.invocation.argv];
    const index = argv.lastIndexOf(given);
    if (index >= 0) argv[index] = value;
    return argv;
  }

  /** A handler's value error with an exact fix: the Action quotes the corrected argv, also details.suggestedArgv. */
  corrected(error: RunnerFailure, action: string, argv: string[]): RunnerFailure {
    let text = action.split("{command}").join(argvText(argv));
    if (!text.endsWith(".")) text += ".";
    return withAction(error, text, { suggestedArgv: argv });
  }

  /**
   * A --limit error (mirrors Rust `Context::limit_error`):
   * when `raw` is a whole number, the suggestion is the nearest value from
   * 1 to `max`; any other value keeps the operation's help.
   */
  limitError(error: RunnerFailure, raw: string, max: number): RunnerFailure {
    const negative = raw.startsWith("-");
    const digits = negative ? raw.slice(1) : raw;
    if (!/^[0-9]+$/u.test(digits)) return error;
    const significant = digits.replace(/^0+/u, "");
    const nearest = negative || significant === "" ? 1 : significant.length > 18 ? max : Math.min(Math.max(Number(significant), 1), max);
    const which = nearest === 1 ? "smallest" : nearest === max ? "largest" : "same";
    const command = `cli ${this.operation.command.join(" ")}`;
    const action = which === "same" ? "Write --limit as a plain whole number: `{command}`" : `Use --limit ${nearest}, the ${which} value \`${command}\` accepts: \`{command}\``;
    return this.corrected(error, action, this.argvWithOption("--limit", String(nearest)));
  }

  /**
   * The advisory GET /run/quote hold for a plan (`index` is the operation's
   * quote request): {hold}, or undefined when the quote fails, so a failed
   * quote never hides the plan. The service's price policy reference stays
   * internal.
   */
  async advisoryQuote(index: number, environment?: string): Promise<JsonObject | undefined> {
    const request: Request = { ...requestFor(this.operation, index, "/run/quote"), class: "control" };
    if (environment !== undefined) request.query.push(["environment", environment]);
    let body: JsonObject;
    try { body = jsonObject(await this.send(request)); }
    catch (caught) {
      if (caught instanceof RunnerFailure) return undefined;
      throw caught;
    }
    const hold = body.hold;
    if (hold === null || typeof hold !== "object" || Array.isArray(hold)) return undefined;
    const holdUsd = hold.hold_usd;
    const ttl = hold.ttl_seconds;
    const holdCents = typeof holdUsd === "string" ? usdCents(holdUsd) : undefined;
    if (typeof holdUsd !== "string" || holdCents === undefined) return undefined;
    if (typeof ttl !== "number" || !Number.isSafeInteger(ttl) || ttl < 0) return undefined;
    return { hold: { hold_usd: holdUsd, hold_cents: holdCents, ttl_seconds: ttl } };
  }

  /** --preview returns the plan; confirm-class without --yes fails; else proceed. */
  gate(planned: JsonObject): Gate {
    if (this.invocation.preview) return { kind: "preview", result: { preview: true, plannedRequest: planned } };
    if (this.operation.confirm && !this.invocation.yes) {
      // The consequence that needs --yes (manifest `confirmReason`).
      const reason = (this.operation as unknown as { confirmReason?: string }).confirmReason;
      throw failure("CONFIRMATION_REQUIRED", {
        ...(reason === undefined ? {} : { reason: `--yes is required because ${reason}` }),
        plannedRequest: planned,
        confirmArgv: [...this.invocation.argv, "--yes"],
        previewArgv: [...this.invocation.argv, "--preview"],
      });
    }
    return { kind: "proceed" };
  }

  /**
   * Default body of a not-yet-implemented feature (see the Rust twin): plans
   * body-less confirm/preview mutations generically (never sending them) and
   * sends the first always-sent GET so framework classification is observable.
   */
  async notImplemented(): Promise<Json> {
    if (this.invocation.preview || this.operation.confirm) {
      const plan = this.genericPath(false);
      if (plan !== undefined) {
        const gate = this.gate(this.planned(plan.index, plan.path));
        if (gate.kind === "preview") return gate.result;
      }
    } else {
      const read = this.genericPath(true);
      if (read !== undefined) await this.send(requestFor(this.operation, read.index, read.path));
    }
    throw failure("INTERNAL_ERROR", { reason: "not implemented" });
  }

  private genericPath(read: boolean): { index: number; path: string } | undefined {
    const index = this.operation.requests.findIndex((request) => (request.method === "GET") === read && request.when === "always");
    if (index < 0) return undefined;
    const request = this.operation.requests[index]!;
    if (request.body !== null || (request.query !== undefined && request.query !== null)) return undefined;
    const values = this.operation.arguments.map((item) => this.argument(item.name)).filter((value): value is string => value !== undefined);
    let path = "";
    for (const segment of request.path.split("/").slice(1)) {
      if (segment.startsWith("{")) {
        const value = values.shift();
        if (value === undefined) return undefined;
        path += `/${encodeSegment(value)}`;
      } else path += `/${segment}`;
    }
    return { index, path };
  }
}

/** Whether a 403 body is the service's invalid-key refusal. */
function keyRejectedBody(body: Uint8Array): boolean {
  try {
    const parsed = JSON.parse(new TextDecoder().decode(body)) as unknown;
    return parsed !== null && typeof parsed === "object" && (parsed as Record<string, unknown>).error === "Invalid API key.";
  } catch { return false; }
}

/** Why a credential failed the strict predicate, without echoing it. */
/** An invocation error whose Action is fixed text. */
function taught(reason: string, action: string): RunnerFailure {
  return withAction(invocationFailure(reason), action);
}

export function withAction(error: RunnerFailure, action: string, details?: Record<string, unknown>): RunnerFailure {
  return new RunnerFailure({
    code: error.code, boundary: error.boundary, message: error.message, action,
    exitCode: error.exitCode, retryable: error.retryable,
    ...(error.details === undefined && details === undefined ? {} : { details: { ...(error.details ?? {}), ...(details ?? {}) } }),
  });
}

/**
 * Applies a correction (mirrors Rust `service::teach`): the per-cause Action
 * and, when a command applies, `details.suggestedArgv` rendered by the shared
 * follow-up renderer.
 */
export function teach(error: RunnerFailure, correction: Correction, environment: Environment | undefined, mode: OutputMode): RunnerFailure {
  let argv: string[] | undefined;
  if (correction.kind === "argv") argv = correction.argv;
  else if (correction.kind === "command") {
    argv = followUpArgv(environment ?? PRODUCTION, mode, correction.words);
  }
  let action = argv === undefined ? correction.action : correction.action.split("{command}").join(argvText(argv));
  if (!action.endsWith(".")) action += ".";
  return withAction(error, action, argv === undefined ? undefined : { suggestedArgv: argv });
}

const GENERIC_INVOCATION_ACTION = invocationFailure("").action;

/** Whether an error still carries the generic taxonomy Action of INVOCATION_INVALID (a handler's value check). */
function genericInvocation(error: RunnerFailure): boolean {
  return error.code === "INVOCATION_INVALID" && error.action === GENERIC_INVOCATION_ACTION;
}

/** The Action for a handler's value check: the operation's own help. */
function valueCorrection(operation: ManifestOperation): Correction {
  return { kind: "command", action: "Correct the value named in Detail; `{command}` shows the accepted syntax", words: [...operation.command, "--help"] };
}

/** The token span of a runner-global option before `cli` in `argv`. */
function globalSpan(argv: readonly string[], name: string, takesValue: boolean): [number, number] | undefined {
  const cli = argv.indexOf("cli");
  if (cli < 0) return undefined;
  const before = argv.slice(0, cli);
  const exact = before.indexOf(name);
  if (exact >= 0) return [exact, takesValue ? 2 : 1];
  const inline = before.findIndex((token) => token.startsWith(`${name}=`));
  return inline >= 0 ? [inline, 1] : undefined;
}

/**
 * Runner-global options other than these are not accepted by service
 * operations; --model, --harness and --dry-run get a targeted reason and a
 * corrected argv.
 */
function checkGlobals(global: GlobalFlags, operation: ManifestOperation, argv: readonly string[]): void {
  const corrected = (error: RunnerFailure, action: string, span: [number, number] | undefined, append: string[]): RunnerFailure => {
    const correction: Correction = span === undefined
      ? { kind: "text", action: action.replace(": `{command}`", "") }
      : { kind: "argv", action, argv: [...argv.slice(0, span[0]), ...argv.slice(span[0] + span[1]), ...append] };
    return teach(error, correction, undefined, "human");
  };
  if (global.model !== undefined) {
    const error = invocationFailure("the runner option --model does not apply to service operations; pass --model after the command, for example `cli run submit --model MODEL`");
    const span = globalSpan(argv, "--model", true);
    throw operation.options.some((item) => item.name === "--model")
      ? corrected(error, "Pass --model after the command: `{command}`", span, ["--model", global.model])
      : corrected(error, "Drop --model; this command takes no model: `{command}`", span, []);
  }
  if (global.harness !== undefined) {
    throw corrected(invocationFailure("the runner option --harness does not apply to service operations; hosted runs use `cli run submit`"),
      "Drop --harness; the hosted service chooses where a run executes: `{command}`", globalSpan(argv, "--harness", true), []);
  }
  if (global.dryRun === true) {
    const error = invocationFailure("the runner option --dry-run does not apply to service operations; use --preview after the command");
    const span = globalSpan(argv, "--dry-run", false);
    throw operation.preview
      ? corrected(error, "Use --preview after the command instead: `{command}`", span, ["--preview"])
      : corrected(error, "Drop --dry-run; this command only reads: `{command}`", span, []);
  }
  const allowed = new Set(["output", "color", "verbose"]);
  if (Object.entries(global).some(([key, value]) => value !== undefined && !allowed.has(key))) {
    throw taught("service operations accept only --output, --no-color and --verbose before cli",
      "Remove the other runner options from before cli; service operations take their own options after the command.");
  }
}

/** The service this process talks to: production (a dev build may override it). */
function resolveEnvironment(deps: Pick<ServiceDependencies, "env">): Environment {
  return serviceEnvironment(deps.env);
}

function resolveMode(output: OutputMode | undefined, json: boolean, global: GlobalFlags, env: Readonly<Record<string, string | undefined>>): OutputMode {
  if (global.output !== undefined && output !== undefined && global.output !== output) {
    throw taught("--output was given twice with different values", "Pass --output once, with the mode you mean.");
  }
  if (json) {
    if (global.output !== undefined && global.output !== "json") {
      throw taught("--json conflicts with --output", "Keep one output choice: drop --json or the --output before cli.");
    }
    return "json";
  }
  const explicit = output ?? global.output;
  if (explicit !== undefined) return explicit;
  const value = env.PROSE_OUTPUT;
  if (value === undefined) return "human";
  if (value === "human" || value === "json" || value === "jsonl") return value;
  throw failure("CONFIG_INVALID", { reason: `invalid output mode ${quote(value)}; expected human, json, or jsonl`, source: "PROSE_OUTPUT" });
}

/**
 * The terminal jsonl line's type (decision 5): `service.detached` for an
 * exit-21 end (the run continues: detached or the --wait deadline), so a
 * filter on `*.failed` never mistakes a still-running run for a failure.
 */
function terminalType(result: Json | RunnerFailure): string {
  if (!(result instanceof RunnerFailure)) return "service.completed";
  return result.exitCode === 21 ? "service.detached" : "service.failed";
}

/** The terminal jsonl event of a stream operation carries the process exit code (mirrors Rust `terminal_line`). */
function terminalLine(line: JsonObject, exit: number): JsonObject {
  line.exitCode = exit;
  return line;
}

function render(operation: ManifestOperation, environment: Environment, mode: OutputMode, result: Json | RunnerFailure, extras: Extras, deps: ServiceDependencies): number {
  const failed = result instanceof RunnerFailure;
  const exit = failed ? result.exitCode : 0;
  if (mode === "jsonl" && operation.output.stream) deps.writeStdout(jsonLine(terminalLine(eventLine(extras.runId, undefined, undefined, terminalType(result), envelope(operation, result, extras.nextBefore)), exit)));
  else if (mode !== "human") deps.writeStdout(structuredOutput(operation, mode, result, extras.nextBefore));
  else if (failed) deps.writeStderr(humanErrorFor(environment, result));
  else {
    let text = extras.human ?? humanResult(result);
    if (extras.nextBefore !== undefined) {
      text += `Next page: ${humanSafeScalar(argvText(followUpArgv(environment, "human", [...(extras.pageWords ?? operation.command), "--before", extras.nextBefore])))}\n`;
    }
    deps.writeStdout(text);
  }
  return exit;
}

/**
 * The words after `cli` that repeat this invocation for the next page
 *: the command and every option given, in manifest order,
 * except `--before`. Mirrors Rust `page_words`.
 */
function pageWords(operation: ManifestOperation, invocation: ServiceInvocation): string[] {
  const words = [...operation.command];
  for (const option of operation.options) {
    if (option.name === "--before") continue;
    for (const value of invocation.options.get(option.name) ?? []) words.push(option.name, value);
  }
  return words;
}

/**
 * An invocation the service surface rejected before an operation ran
 *: in JSON modes always the service-operation/1 envelope with
 * the error as `problem`, never a bare runner error. Mirrors Rust
 * `render::rejected`.
 */
function renderRejected(operation: ManifestOperation | undefined, environment: Environment | undefined, mode: OutputMode, error: RunnerFailure, deps: ServiceDependencies): number {
  if (operation !== undefined && environment !== undefined) return render(operation, environment, mode, error, {}, deps);
  if (mode === "human") deps.writeStderr(environment === undefined ? humanError("OpenProse", error) : humanErrorFor(environment, error));
  else deps.writeStdout(jsonLine(rejectedEnvelope(operation, error)));
  return error.exitCode;
}

/**
 * The manifest operation an argv names: the longest run of
 * command words right after the first `cli` (before any `--`) that equals an
 * operation's command; undefined when there is none. Mirrors Rust.
 */
export function commandOperation(argv: readonly string[]): ManifestOperation | undefined {
  const end = argv.indexOf("--");
  const head = end === -1 ? argv : argv.slice(0, end);
  const at = head.indexOf("cli");
  if (at === -1) return undefined;
  const words: string[] = [];
  for (const token of argv.slice(at + 1)) {
    if (token.startsWith("-")) break;
    words.push(token);
  }
  let best: ManifestOperation | undefined;
  for (const candidate of manifest.operations) {
    const command = candidate.command;
    if (command.length > words.length || !command.every((word, index) => word === words[index])) continue;
    if (best === undefined || command.length > best.command.length) best = candidate;
  }
  return best;
}

/** Whether the command word after the first `cli` is a runner command (`doctor`, `harness`, `cleanup`, `config`). Mirrors Rust. */
export function runnerArgv(argv: readonly string[]): boolean {
  const end = argv.indexOf("--");
  const at = (end === -1 ? argv : argv.slice(0, end)).indexOf("cli");
  const word = at === -1 ? undefined : argv[at + 1];
  return word !== undefined && RUNNER_COMMANDS.some((path) => path[0] === word);
}

/** Whether an argv is a service command line: after the runner-global prefix, `cli` then a manifest noun. Mirrors Rust. */
export function serviceArgv(argv: readonly string[], isValueOption: (token: string) => boolean): boolean {
  let index = 0;
  while (index < argv.length) {
    const token = argv[index]!;
    if (token === "--") return false;
    if (token === "cli") {
      const noun = argv[index + 1];
      return noun !== undefined && manifest.operations.some((candidate) => candidate.command[0] === noun);
    }
    if (!token.startsWith("-")) return false;
    index += !token.includes("=") && isValueOption(token) ? 2 : 1;
  }
  return false;
}

/**
 * Renders an invocation error the entry point raised outside the service
 * parser for a service argv in JSON modes. Returns undefined,
 * writing nothing, when the runner renderer keeps it: human mode, other
 * codes, and argvs that are not service command lines. Mirrors Rust
 * `argv_error_outcome`.
 */
export async function argvErrorOutcome(argv: readonly string[], error: RunnerFailure, mode: OutputMode, isValueOption: (token: string) => boolean, deps: Pick<ServiceDependencies, "writeStdout" | "env">): Promise<number | undefined> {
  if (mode === "human" || !["INVOCATION_INVALID", "CONFIRMATION_REQUIRED"].includes(error.code) || !serviceArgv(argv, isValueOption)) return undefined;
  let environment: Environment | undefined;
  try { environment = resolveEnvironment(deps); }
  catch { environment = undefined; }
  const operation = commandOperation(argv);
  const document = operation !== undefined && environment !== undefined
    ? (operation.output.stream && mode === "jsonl" ? terminalLine(eventLine(undefined, undefined, undefined, "service.failed", envelope(operation, error)), error.exitCode) : envelope(operation, error))
    : rejectedEnvelope(operation, error);
  deps.writeStdout(jsonLine(document));
  return error.exitCode;
}

async function dispatch(context: Context): Promise<Json> {
  switch (context.operation.feature) {
    case "discovery": return await discovery.execute(context);
    case "runs": return await runs.execute(context);
    case "run-records": return await runRecords.execute(context);
    case "programs": return await programs.execute(context);
    case "results": return await results.execute(context);
    case "jobs": return await jobs.execute(context);
    case "wallet": return await wallet.execute(context);
    case "organizations": return await organizations.execute(context);
    default: return await context.notImplemented();
  }
}

/** Renders a service-noun invocation that named no operation. */
async function runInvalid(rejected: ServiceInvalid, global: GlobalFlags, deps: ServiceDependencies): Promise<number> {
  let mode: OutputMode;
  try { mode = resolveMode(rejected.output, rejected.json, global, deps.env); }
  catch { mode = rejected.json ? "json" : rejected.output ?? global.output ?? "human"; }
  let environment: Environment | undefined;
  try { environment = resolveEnvironment(deps); }
  catch { environment = undefined; }
  const error = teach(rejected.error, rejected.correction, environment, mode);
  // A runner command (`cli doctor`, `cli harness ...`) keeps the runner error document, like the language.
  if (runnerArgv(rejected.argv ?? []) && mode !== "human") {
    deps.writeStdout(jsonLine(error.toJSON()));
    return error.exitCode;
  }
  return renderRejected(commandOperation(rejected.argv ?? []), environment, mode, error, deps);
}

/**
 * A service help request's output: the text, or in a JSON mode the envelope
 * whose result is `{help, operations}` (the text and the manifest records of
 * the commands it describes). `operation` is the one command a topic names,
 * else `service.operations` (mirrors Rust `help_outcome`).
 */
export function helpOutput(text: string, mode: OutputMode): string {
  const shown = localizeHelp(text);
  if (mode === "human") return shown;
  const topic = Object.entries(helpTopicTexts()).find(([, value]) => value === text)?.[0] ?? "cli";
  const words = topic.split(" ").slice(1);
  const published = ((staticResult("service.operations") as JsonObject).operations ?? []) as JsonObject[];
  const records = published.filter((_, index) => words.every((word, position) => manifest.operations[index]!.command[position] === word));
  const named = manifest.operations.find((candidate) => candidate.command.length === words.length && words.every((word, index) => candidate.command[index] === word))
    ?? findOperation("service.operations")!;
  return jsonLine({ schema: "openprose.service-operation/1", operation: named.id, interaction: named.interaction, result: { help: shown, operations: records as unknown as Json }, problem: null });
}

/** Executes a service service command and returns the exit code. */
export async function runService(command: ServiceCommand, global: GlobalFlags, deps: ServiceDependencies): Promise<number> {
  configureRunErrors(deps.env);
  if (command.kind === "help") {
    // In a JSON mode (`prose --output json cli run --help`) help is the
    // envelope with the text and the command records (mirrors Rust
    // `help_outcome`).
    let mode: OutputMode = "human";
    try { mode = resolveMode(undefined, false, global, deps.env); } catch { mode = global.output ?? "human"; }
    deps.writeStdout(helpOutput(command.text, mode));
    return 0;
  }
  if (command.kind === "invalid") return await runInvalid(command.invalid, global, deps);
  const invocation = command.invocation;
  const operation = findOperation(invocation.operation)!;
  const fallbackMode: OutputMode = invocation.json ? "json" : invocation.output ?? global.output ?? "human";
  let environment: Environment;
  try { environment = resolveEnvironment(deps); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    return renderRejected(operation, undefined, fallbackMode, caught, deps);
  }
  let mode: OutputMode;
  try { mode = resolveMode(invocation.output, invocation.json, global, deps.env); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    return render(operation, environment, fallbackMode, caught, {}, deps);
  }
  if (invocation.error !== undefined) {
    const error = invocation.correction === undefined ? invocation.error : teach(invocation.error, invocation.correction, environment, mode);
    return render(operation, environment, mode, error, {}, deps);
  }
  try { checkGlobals(global, operation, invocation.argv); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    return render(operation, environment, mode, caught, {}, deps);
  }
  const view = mode === "human" ? staticView(operation.id) : undefined;
  if (view !== undefined) {
    deps.writeStdout(view);
    return 0;
  }
  const staticValue = mode === "human" ? undefined : staticResult(operation.id);
  if (staticValue !== undefined) return render(operation, environment, mode, staticValue, {}, deps);
  if (operation.id === "service.guide") return render(operation, environment, mode, guideResult(), { human: GUIDE_TEXT }, deps);
  const cancellation = new AbortController();
  const signal = deps.cancellationSignal;
  if (signal !== undefined) {
    if (signal.aborted) cancellation.abort();
    else signal.addEventListener("abort", () => cancellation.abort(), { once: true });
  }
  let transport: Transport;
  try { transport = await Transport.create(environment, deps.env, cancellation); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    return render(operation, environment, mode, caught, {}, deps);
  }
  // A dev build's custom-endpoint banner is printed once, before the first
  // streamed byte. Public builds never print one. A failure
  // that streamed nothing prints no separate banner: its error line already
  // starts with the label.
  let banner: string | undefined = mode === "human" && environment.name !== "production" ? `${environmentLabel(environment)}\n` : undefined;
  const emitBanner = (): void => {
    if (banner === undefined) return;
    const text = banner;
    banner = undefined;
    deps.writeStderr(text);
  };
  const streamed: ServiceDependencies = {
    ...deps,
    writeStdout: (text: string) => { if (text.length > 0) emitBanner(); deps.writeStdout(text); },
    writeStderr: (text: string) => { if (text.length > 0) emitBanner(); deps.writeStderr(text); },
  };
  const context = new Context(invocation, operation, environment, mode, transport, streamed);
  let result: Json | RunnerFailure;
  try {
    const preset = transport.fixtureDocument?.journal;
    if (Array.isArray(preset)) {
      const journal = context.journal();
      for (const entry of preset) journal.writeJson(entry);
    }
    result = await dispatch(context);
  } catch (caught) {
    result = caught instanceof RunnerFailure ? caught : failure("INTERNAL_ERROR", { reason: "unexpected failure" });
  }
  try { transport.finish(); }
  catch (caught) { if (caught instanceof RunnerFailure) result = caught; }
  if (result instanceof RunnerFailure) result = context.annotateAuth(result);
  if (result instanceof RunnerFailure) redactDetails(result.details as Record<string, unknown> | undefined, context.knownCredential());
  if (result instanceof RunnerFailure && genericInvocation(result)) result = teach(result, valueCorrection(operation), environment, mode);
  if (result instanceof RunnerFailure) result = explainOperationNotFound(result, operation, (name) => context.argument(name), environment, mode);
  if (result instanceof RunnerFailure) result = explainRejected(result, environment, mode, [...operation.command, "--help"]);
  const extras: Extras = { pageWords: pageWords(operation, invocation) };
  if (context.nextBefore !== undefined) extras.nextBefore = context.nextBefore;
  if (context.runId !== undefined) extras.runId = context.runId;
  if (context.human !== undefined) extras.human = context.human;
  if (!(result instanceof RunnerFailure)) emitBanner();
  return render(operation, environment, mode, result, extras, deps);
}

export { requestFor };
