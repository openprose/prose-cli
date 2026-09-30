// `cli service triage`: one read that answers "what state is
// this session in and what should I run next?". Mirrors
// cli/rust/crates/prose-runner-core/src/service/triage.rs byte for byte.
//
// It composes GET /health, /wallet/balance, /organizations/default,
// /runs?limit=5 and /triggers. Every section carries
// its own `problem`, so one failing read never hides the others and the
// operation still exits 0; only cancellation (and invocation errors) end it.
// Without a usable credential the account sections are null and `credential`
// names the exact variable to set. Price-side fields only.
// Result: shared/schemas/service/discovery.schema.json#/$defs/serviceTriage.
// Decisions: docs/service/discovery.md.
import { failure } from "../errors";
import { humanSafeScalar } from "../output";
import { RunnerFailure } from "../types";
import { projectHealth, Shape } from "./discovery";
import { jsonObject, requestFor, type Response } from "./http";
import type { Context } from "./index";
import { projectJobs } from "./jobs";
import type { Json, JsonObject } from "./manifest";
import { organization } from "./organizations";
import { argvText, redactDetails } from "./render";
import { projectRun } from "./run-records";
import { projectBalance } from "./wallet";

/** Run statuses that mean the run is still going (`cli run watch` applies). */
export const LIVE_RUN_STATUSES: readonly string[] = ["queued", "pending", "starting", "running", "input_needed", "awaiting_input"];
/** The low-balance threshold of the wallet section and its top-up suggestion. */
export const DEFAULT_LOW_BALANCE_CENTS = 500;
export const RECENT_RUNS = 5;
export const JOBS_SHOWN = 5;
export const LIVE_WATCH_MAX = 3;
export const FAILING_JOBS_MAX = 2;
const JOB_KEYS = ["id", "type", "name", "next_fire_at", "next_fire_at_iso", "last_event_at", "last_event_at_iso", "last_run_id", "last_error"] as const;
const HEALTH_KEYS = ["status", "default_model", "models"] as const;

// Indices into the manifest's service.triage requests.
const HEALTH = 0;
const WALLET = 1;
const ORGANIZATION = 2;
const RUNS = 3;
const JOBS = 4;

function protocol(reason: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason });
}

async function fetch(context: Context, index: number, path: string): Promise<Response> {
  const request = requestFor(context.operation, index, path);
  if (index === RUNS) request.query.push(["limit", String(RECENT_RUNS)]);
  return await context.send(request);
}

/** Runs one read; a failure is returned (cancellation and non-runner errors are thrown). */
async function attempt(read: () => Promise<JsonObject>): Promise<JsonObject | RunnerFailure> {
  try { return await read(); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code !== "CANCELLED") return caught;
    throw caught;
  }
}

function problemJson(error: RunnerFailure): Json { return error.toJSON() as unknown as Json; }

/** A section: the projected fields plus `problem: null`, or `{problem}`. */
function section(context: Context, projected: JsonObject | RunnerFailure): JsonObject {
  if (projected instanceof RunnerFailure) {
    redactDetails(projected.details as Record<string, unknown> | undefined, context.knownCredential());
    return { problem: problemJson(projected) };
  }
  return { ...projected, problem: null };
}

/** A failed /health with no serviceStatus: nothing answered, skip the other reads. */
function unreachable(error: RunnerFailure): boolean {
  return error.code === "SERVICE_UNAVAILABLE" && !Object.hasOwn(error.details ?? {}, "serviceStatus");
}

function detail(error: RunnerFailure, key: string): string | undefined {
  const value = (error.details ?? {})[key];
  return typeof value === "string" ? value : undefined;
}

export async function execute(context: Context): Promise<Json> {
  const healthRead = await attempt(async () => {
    const health = projectHealth(new Shape("service status").object(await fetch(context, HEALTH, "/health")));
    const kept: JsonObject = {};
    for (const key of HEALTH_KEYS) if (Object.hasOwn(health, key)) kept[key] = health[key]!;
    return kept;
  });
  const reachable = !(healthRead instanceof RunnerFailure && unreachable(healthRead));
  const health = section(context, healthRead);

  const variable = context.environment.credentialEnv;
  let state: string;
  let source: string;
  let credentialProblem: RunnerFailure | undefined;
  try {
    await context.credentialValue();
    state = "unverified";
    source = context.credentialOrigin() ?? "environment";
  } catch (caught) {
    if (!(caught instanceof RunnerFailure) || caught.code === "CANCELLED") throw caught;
    state = caught.code === "CREDENTIAL_STORE_UNAVAILABLE" ? "unavailable" : detail(caught, "credentialProblem") === "malformed" ? "malformed" : "missing";
    const origin = detail(caught, "credentialSource");
    source = origin === "environment" || origin === "store" ? origin : "none";
    redactDetails(caught.details as Record<string, unknown> | undefined, context.knownCredential());
    credentialProblem = caught;
  }

  const account: Json[] = [null, null, null, null];
  if (reachable && credentialProblem === undefined) {
    const reads: Array<() => Promise<JsonObject>> = [
      () => walletSection(context),
      () => organizationSection(context),
      () => runsSection(context),
      () => jobsSection(context),
    ];
    for (const [slot, read] of reads.entries()) {
      const projected = await attempt(read);
      if (!(projected instanceof RunnerFailure)) {
        state = "valid";
        account[slot] = section(context, projected);
        continue;
      }
      const annotated = context.annotateAuth(projected);
      if (detail(annotated, "credentialProblem") === "rejected") {
        redactDetails(annotated.details as Record<string, unknown> | undefined, context.knownCredential());
        state = "rejected";
        credentialProblem = annotated;
        break;
      }
      account[slot] = section(context, annotated);
    }
  }
  const [wallet, org, runs, jobs] = account;
  const result: JsonObject = {
    health,
    credential: { variable, source, state, problem: credentialProblem === undefined ? null : problemJson(credentialProblem) },
    wallet: wallet!,
    organization: org!,
    runs: runs!,
    jobs: jobs!,
  };
  result.nextCommands = nextCommands(context, result, reachable);
  context.human = human(result);
  return result;
}

async function walletSection(context: Context): Promise<JsonObject> {
  const body = jsonObject(await fetch(context, WALLET, "/wallet/balance"));
  const balance = projectBalance(body);
  const threshold = DEFAULT_LOW_BALANCE_CENTS;
  const available = typeof balance.available_cents === "number" ? balance.available_cents : 0;
  return { balance, low: available < threshold, lowBelowCents: threshold };
}

async function organizationSection(context: Context): Promise<JsonObject> {
  const body = jsonObject(await fetch(context, ORGANIZATION, "/organizations/default"));
  return { default: organization(Object.hasOwn(body, "organization") ? body.organization : undefined) };
}

/** The run fields `service triage` reports (user fields only). */
const TRIAGE_RUN_FIELDS = ["run_id", "status", "model", "created_at", "program_ref", "price_cents"];

/** The user fields of a recent run: triage never carries runtime details. */
function triageRun(run: JsonObject): JsonObject {
  return Object.fromEntries(Object.entries(run).filter(([key]) => TRIAGE_RUN_FIELDS.includes(key)));
}

async function runsSection(context: Context): Promise<JsonObject> {
  const body = jsonObject(await fetch(context, RUNS, "/runs"));
  const rows = body.runs;
  if (!Array.isArray(rows) || rows.length > 200) throw protocol("the run list is missing or has more than 200 runs");
  const recent = rows.slice(0, RECENT_RUNS).map((run) => triageRun(projectRun(run, false)));
  const live = recent.filter((run) => typeof run.status === "string" && LIVE_RUN_STATUSES.includes(run.status)).map((run) => run.run_id!);
  return { recent, live };
}

async function jobsSection(context: Context): Promise<JsonObject> {
  const body = jsonObject(await fetch(context, JOBS, "/triggers"));
  const { jobs: all, max } = projectJobs(body);
  const shown = all.slice(0, JOBS_SHOWN).map((entry) => {
    const job: JsonObject = {};
    for (const key of JOB_KEYS) {
      const value = entry[key];
      if (value !== undefined && value !== null) job[key] = value;
    }
    return job;
  });
  return { total: all.length, max, jobs: shown };
}

function objectOf(value: Json | undefined): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function reasonOf(problem: Json | undefined): string {
  const shape = objectOf(problem);
  const reason = objectOf(shape.details).reason;
  if (typeof reason === "string") return reason;
  return typeof shape.message === "string" ? shape.message : "";
}

function centsText(cents: number): string {
  return `${Math.trunc(cents / 100)}.${String(Math.abs(cents % 100)).padStart(2, "0")}`;
}

/** nextCommands in a fixed order: credential, unreachable, live runs, low balance, failing jobs. */
function nextCommands(context: Context, result: JsonObject, reachable: boolean): Json[] {
  const next: Json[] = [];
  const credential = objectOf(result.credential);
  if (credential.problem !== null && credential.problem !== undefined) {
    next.push({ why: reasonOf(credential.problem), argv: null, env: credential.variable! });
  }
  if (!reachable) {
    next.push({ why: "the service did not respond; check your network connection, then retry", argv: context.followUpArgv(["service", "status"]), env: null });
  }
  const runs = objectOf(result.runs);
  if (Array.isArray(runs.live)) {
    const recent = Array.isArray(runs.recent) ? runs.recent.map(objectOf) : [];
    for (const runId of runs.live.filter((value): value is string => typeof value === "string").slice(0, LIVE_WATCH_MAX)) {
      const status = recent.find((run) => run.run_id === runId)?.status;
      next.push({ why: `run ${runId} is ${typeof status === "string" ? status : ""}`, argv: context.followUpArgv(["run", "watch", runId]), env: null });
    }
  }
  const wallet = objectOf(result.wallet);
  if (wallet.low === true) {
    const threshold = typeof wallet.lowBelowCents === "number" ? wallet.lowBelowCents : 0;
    const available = objectOf(wallet.balance).available_dollars;
    next.push({
      why: `the available balance $${typeof available === "string" ? available : ""} is below $${centsText(threshold)}`,
      argv: context.followUpArgv(["wallet", "topup", "--amount-cents", String(threshold), "--preview"]),
      env: null,
    });
  }
  const jobs = objectOf(result.jobs);
  if (Array.isArray(jobs.jobs)) {
    for (const job of jobs.jobs.map(objectOf).filter((entry) => Object.hasOwn(entry, "last_error")).slice(0, FAILING_JOBS_MAX)) {
      const id = typeof job.id === "string" ? job.id : "";
      next.push({ why: `job ${id} reported an error`, argv: context.followUpArgv(["job", "show", id]), env: null });
    }
  }
  return next;
}

function text(value: Json | undefined): string { return humanSafeScalar(typeof value === "string" ? value : ""); }

function problemLine(label: string, problem: Json | undefined): string {
  const code = objectOf(problem).code;
  return `${label}: ${typeof code === "string" ? code : ""}: ${humanSafeScalar(reasonOf(problem))}\n`;
}

/** The human report: at most 25 lines. Exported for the unit test. */
export function human(result: JsonObject): string {
  let out = "";
  const health = objectOf(result.health);
  if (health.problem === null) {
    out += `Service: ${text(health.status)}, default model ${text(health.default_model)}\n`;
  } else out += problemLine("Service", health.problem);
  const credential = objectOf(result.credential);
  const variable = text(credential.variable);
  const origin =
    credential.source === "none"
      ? `${variable} not set`
      : credential.source === "environment"
        ? `from ${variable}`
        : credential.source === "store"
          ? "from the credential store"
          : typeof credential.source === "string"
            ? `${variable} from ${credential.source}`
            : variable;
  out += `Credential: ${text(credential.state)}, ${origin}`;
  out += credential.problem === null ? "\n" : `; ${humanSafeScalar(reasonOf(credential.problem))}\n`;
  if (result.wallet === null) out += "Wallet: skipped\n";
  else {
    const wallet = objectOf(result.wallet);
    if (wallet.problem === null) {
      const balance = objectOf(wallet.balance);
      out += `Wallet: $${text(balance.available_dollars)} available, $${text(balance.reserved_dollars)} reserved`;
      if (wallet.low === true) out += ` (low: below $${centsText(typeof wallet.lowBelowCents === "number" ? wallet.lowBelowCents : 0)})`;
      out += "\n";
    } else out += problemLine("Wallet", wallet.problem);
  }
  if (result.organization === null) out += "Organization: skipped\n";
  else {
    const org = objectOf(result.organization);
    if (org.problem === null) {
      const entry = objectOf(org.default);
      out += `Organization: ${text(entry.slug)}`;
      if (typeof entry.role === "string") out += ` (${humanSafeScalar(entry.role)})`;
      out += "\n";
    } else out += problemLine("Organization", org.problem);
  }
  if (result.runs === null) out += "Runs: skipped\n";
  else {
    const runs = objectOf(result.runs);
    if (runs.problem === null) {
      const recent = Array.isArray(runs.recent) ? runs.recent.map(objectOf) : [];
      const live = Array.isArray(runs.live) ? runs.live.length : 0;
      out += `Runs: ${recent.length} recent, ${live} live\n`;
      for (const run of recent) out += `  ${text(run.run_id)}  ${text(run.status)}  ${text(run.model)}  ${text(run.created_at)}\n`;
    } else out += problemLine("Runs", runs.problem);
  }
  if (result.jobs === null) out += "Jobs: skipped\n";
  else {
    const jobs = objectOf(result.jobs);
    if (jobs.problem === null) {
      const total = typeof jobs.total === "number" ? jobs.total : 0;
      out += typeof jobs.max === "number" ? `Jobs: ${total} of ${jobs.max} allowed\n` : `Jobs: ${total}\n`;
      for (const job of Array.isArray(jobs.jobs) ? jobs.jobs.map(objectOf) : []) {
        const name = Object.hasOwn(job, "name") ? text(job.name) : "-";
        out += `  ${text(job.id)}  ${text(job.type)}  ${name}${Object.hasOwn(job, "last_error") ? "  (error)" : ""}\n`;
      }
    } else out += problemLine("Jobs", jobs.problem);
  }
  const next = Array.isArray(result.nextCommands) ? result.nextCommands.map(objectOf) : [];
  if (next.length === 0) out += "Next: nothing needs attention\n";
  else {
    out += "Next:\n";
    for (const command of next) {
      const action = Array.isArray(command.argv)
        ? argvText(command.argv.filter((word): word is string => typeof word === "string"))
        : `set ${typeof command.env === "string" ? command.env : ""}`;
      out += `  ${humanSafeScalar(action)}  # ${humanSafeScalar(typeof command.why === "string" ? command.why : "")}\n`;
    }
  }
  return out;
}
