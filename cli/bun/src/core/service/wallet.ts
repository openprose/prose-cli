// Service wallet operations: `wallet balance|events|usage|redeem|topup`.
//
// Every projection is closed and carries only server-provided price fields
// (`*_cents`, `*_dollars`, `*_usd`); the CLI never computes a price. The redeem
// code is read from a file or standard input and never echoed; the top-up
// always sends an Idempotency-Key (a minted UUIDv4 unless --idempotency-key is
// given) and prints the Checkout URL without opening a browser. Mirrors
// cli/rust/crates/prose-runner-core/src/service/wallet.rs byte for byte.
import { failure, invocationFailure } from "../errors";
import { humanSafeScalar, quote } from "../output";
import { RunnerFailure } from "../types";
import { readText } from "./fs";
import { canonicalJson, dollarsOrDash, sanitizeServiceMessage, validText } from "./render";
import { jsonObject, requestFor } from "./http";
import type { Context } from "./index";
import type { Json, JsonObject } from "./manifest";

const EVENTS_DEFAULT_LIMIT = 20;
const EVENTS_MAX_LIMIT = 100;
const CODE_FILE_MAX_BYTES = 1024;
const CODE_MAX_CHARS = 64;
const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;

export async function execute(context: Context): Promise<Json> {
  switch (context.operation.id) {
    case "wallet.balance": return await balance(context);
    case "wallet.events": return await events(context);
    case "wallet.usage": return await usage(context);
    case "wallet.redeem": return await redeem(context);
    case "wallet.topup": return await topup(context);
    default: return await context.notImplemented();
  }
}

function protocol(reason: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason: `unexpected wallet response: ${reason}` });
}

// ---------------------------------------------------------------- validators

function plainText(value: Json | undefined, max: number, field: string): string {
  if (typeof value === "string" && (value.length === 0 || validText(value, max))) return value;
  throw protocol(field);
}

function freeText(value: Json | undefined, field: string): string {
  if (typeof value !== "string") throw protocol(field);
  return sanitizeServiceMessage(value) ?? "";
}

function integer(value: Json | undefined, field: string, minimum?: number): number {
  if (typeof value === "number" && Number.isInteger(value) && Math.abs(value) <= MAX_SAFE_INTEGER && (minimum === undefined || value >= minimum)) return value;
  throw protocol(field);
}

function numberValue(value: Json | undefined, field: string): number {
  if (typeof value === "number") return value;
  throw protocol(field);
}

function dollars(value: Json | undefined, field: string): string {
  if (typeof value === "string" && /^-?[0-9]+\.[0-9]{2}$/u.test(value)) return value;
  throw protocol(field);
}

/** YYYY-MM-DD naming a real calendar day (leap years included); the service answers any other date with a 500. */
function isDate(text: string): boolean {
  if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/u.test(text)) return false;
  const ms = Date.parse(`${text}T00:00:00.000Z`);
  return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === text;
}

function timestamp(value: Json | undefined, field: string): string {
  if (typeof value === "string" && value.length <= 40 && /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$/u.test(value) && isDate(value.slice(0, 10))) return value;
  throw protocol(field);
}

function date(value: Json | undefined, field: string): string {
  if (typeof value === "string" && isDate(value)) return value;
  throw protocol(field);
}

function object(value: Json | undefined, field: string): JsonObject {
  if (value !== null && value !== undefined && typeof value === "object" && !Array.isArray(value)) return value;
  throw protocol(field);
}

// ------------------------------------------------------------------- balance

/** Projects the service balance (drops customer_id and *_nanos). */
export function projectBalance(body: JsonObject): JsonObject {
  const balance = object(body.balance, "balance");
  const projected: JsonObject = {};
  for (const field of ["available", "posted", "reserved"]) {
    projected[`${field}_cents`] = integer(balance[`${field}_cents`], `balance.${field}_cents`);
    projected[`${field}_dollars`] = dollars(balance[`${field}_dollars`], `balance.${field}_dollars`);
  }
  return projected;
}

async function balance(context: Context): Promise<Json> {
  const body = jsonObject(await context.send(requestFor(context.operation, 0, "/wallet/balance")));
  const projected = projectBalance(body);
  context.human = humanBalance(projected);
  return { balance: projected };
}

export function humanBalance(balance: JsonObject): string {
  return [["Available", "available"], ["Reserved", "reserved"], ["Balance", "posted"]]
    .map(([label, field]) => `${label}: $${humanSafeScalar(String(balance[`${field}_dollars`] ?? ""))}\n`).join("");
}

// -------------------------------------------------------------------- events

/** --limit: an integer from 1 to 100 (default 20). */
export function parseEventsLimit(value: string | undefined): number {
  if (value === undefined) return EVENTS_DEFAULT_LIMIT;
  const limit = /^[0-9]+$/u.test(value) ? Number(value) : Number.NaN;
  if (Number.isInteger(limit) && limit >= 1 && limit <= EVENTS_MAX_LIMIT) return limit;
  throw invocationFailure(`--limit must be an integer from 1 to ${EVENTS_MAX_LIMIT}, got ${quote(value)}`);
}

/** Projects one events page; returns the result and the next cursor. */
export function projectEvents(body: JsonObject): { result: JsonObject; next: string | undefined } {
  const items = body.events;
  if (!Array.isArray(items)) throw protocol("events");
  if (items.length > EVENTS_MAX_LIMIT) throw protocol("events (more than 100)");
  const events: Json[] = items.map((raw) => {
    const item = object(raw, "events[]");
    const event: JsonObject = {
      id: plainText(item.id, 64, "events[].id"),
      type: plainText(item.type, 64, "events[].type"),
      occurred_at: timestamp(item.occurred_at, "events[].occurred_at"),
      description: freeText(item.description, "events[].description"),
      amount_cents: integer(item.amount_cents, "events[].amount_cents"),
      balance_after_cents: integer(item.balance_after_cents, "events[].balance_after_cents"),
    };
    if (Object.hasOwn(item, "ref")) event.ref = item.ref === null ? null : plainText(item.ref, 256, "events[].ref");
    return event;
  });
  const result: JsonObject = { events };
  if (body.note !== undefined && body.note !== null) {
    const note = freeText(body.note, "note");
    if (note.trim().length > 0) result.note = note;
  }
  let next: string | undefined;
  const cursor = body.next_before;
  if (typeof cursor === "string" && validText(cursor, 512)) next = cursor;
  else if (cursor !== undefined && cursor !== null) throw protocol("next_before");
  return { result, next };
}

async function events(context: Context): Promise<Json> {
  let limit: number;
  try { limit = parseEventsLimit(context.option("--limit")); }
  catch (caught) {
    if (caught instanceof RunnerFailure) throw context.limitError(caught, context.option("--limit") ?? "", EVENTS_MAX_LIMIT);
    throw caught;
  }
  const before = context.option("--before");
  if (before !== undefined && !validText(before, 512)) {
    throw invocationFailure("--before must be a cursor from a previous result's nextBefore (1 to 512 characters, no control characters)");
  }
  const request = requestFor(context.operation, 0, "/wallet/events");
  request.query.push(["limit", String(limit)]);
  if (before !== undefined) request.query.push(["before", before]);
  const { result, next } = projectEvents(jsonObject(await context.send(request)));
  context.human = humanEvents(result);
  context.nextBefore = next;
  return result;
}

/**
 * The human label of a wallet event (mirrors Rust `event_label`): the type in
 * words, then the service's description without its internal `Contract run`
 * prefix (`run charge: model-luna`). A description that already starts with
 * the type is printed once, alone (`Credit code`, not `credit: Credit code`),
 * and an empty one falls back to the type. JSON keeps both fields as sent.
 */
function eventLabel(kind: string, description: string): string {
  const words = kind.replaceAll("_", " ");
  const text = description.startsWith("Contract run") ? description.slice("Contract run".length).trimStart() : description;
  if (text.length === 0) return words;
  return text.toLowerCase().startsWith(words.toLowerCase()) ? text : `${words}: ${text}`;
}

export function humanEvents(result: JsonObject): string {
  const events = (result.events ?? []) as JsonObject[];
  let text = events.length === 0 ? "No wallet events.\n" : "";
  for (const event of events) {
    text += `${humanSafeScalar(String(event.occurred_at))}  ${humanSafeScalar(String(event.id))}  ${dollarsOrDash(event.amount_cents)}  balance ${dollarsOrDash(event.balance_after_cents)}  ${humanSafeScalar(eventLabel(String(event.type), String(event.description)))}\n`;
  }
  if (typeof result.note === "string") text += `Note: ${humanSafeScalar(result.note)}\n`;
  return text;
}

// --------------------------------------------------------------------- usage

/** The server's default usage window for `now` (RFC 3339): [now - 30 days, today] as UTC dates, the window the service uses when a bound is missing. */
export function defaultUsageWindow(now: string): [string, string] | undefined {
  const ms = Date.parse(now);
  if (!Number.isFinite(ms)) return undefined;
  return [new Date(ms - 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10), new Date(ms).toISOString().slice(0, 10)];
}

/** A `YYYY-M-D` date with its month and day zero-padded (`2026-9-1` -> `2026-09-01`), when that is a real calendar date (mirrors Rust `padded_date`). */
function paddedDate(value: string): string | undefined {
  const match = /^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})$/u.exec(value);
  if (match === null) return undefined;
  const padded = `${match[1]}-${match[2]!.padStart(2, "0")}-${match[3]!.padStart(2, "0")}`;
  return isDate(padded) ? padded : undefined;
}

/** `argv` with the value of `option` replaced (separate or `=` form). */
function withOptionValue(argv: readonly string[], option: string, value: string): string[] {
  const out = [...argv];
  for (let index = 0; index < out.length; index += 1) {
    if (out[index] === option && index + 1 < out.length) { out[index + 1] = value; index += 1; }
    else if (out[index]!.startsWith(`${option}=`)) out[index] = `${option}=${value}`;
  }
  return out;
}

/** --start / --end: real calendar dates as YYYY-MM-DD, start not after end; a lone bound is checked against the server-defaulted other bound. */
export function usageWindow(start: string | undefined, end: string | undefined, now: string): Array<[string, string]> {
  const query: Array<[string, string]> = [];
  for (const [name, value] of [["start", start], ["end", end]] as const) {
    if (value === undefined) continue;
    if (!isDate(value)) throw invocationFailure(`--${name} must be a calendar date as YYYY-MM-DD, got ${quote(value)}`);
    query.push([name, value]);
  }
  if (start !== undefined && end !== undefined && start > end) throw invocationFailure(`--start ${start} is after --end ${end}; swap them`);
  if ((start === undefined) !== (end === undefined)) {
    const defaults = defaultUsageWindow(now);
    if (defaults !== undefined) {
      const [defaultStart, defaultEnd] = defaults;
      if (start !== undefined && start > defaultEnd) {
        throw invocationFailure(`--start ${start} is after today (${defaultEnd} UTC), the default --end; pass an earlier --start or an explicit --end`);
      }
      if (end !== undefined && end < defaultStart) {
        throw invocationFailure(`--end ${end} is before the default --start (${defaultStart} UTC, 30 days ago); pass an explicit --start`);
      }
    }
  }
  return query;
}

export function projectUsage(body: JsonObject): JsonObject {
  const period = object(body.period, "period");
  const days = body.daily;
  if (!Array.isArray(days)) throw protocol("daily");
  if (days.length > 400) throw protocol("daily (more than 400 days)");
  const daily: Json[] = days.map((raw) => {
    const day = object(raw, "daily[]");
    return {
      date: date(day.date, "daily[].date"),
      runs: integer(day.runs, "daily[].runs", 0),
      input_tokens: integer(day.input_tokens, "daily[].input_tokens", 0),
      output_tokens: integer(day.output_tokens, "daily[].output_tokens", 0),
      price_cents: integer(day.price_cents, "daily[].price_cents"),
    };
  });
  return {
    period: { start: date(period.start, "period.start"), end: date(period.end, "period.end") },
    total_runs: integer(body.total_runs, "total_runs", 0),
    total_input_tokens: integer(body.total_input_tokens, "total_input_tokens", 0),
    total_output_tokens: integer(body.total_output_tokens, "total_output_tokens", 0),
    total_price_cents: integer(body.total_price_cents, "total_price_cents"),
    daily,
  };
}

async function usage(context: Context): Promise<Json> {
  const request = requestFor(context.operation, 0, "/wallet/usage");
  const start = context.option("--start");
  const end = context.option("--end");
  const now = context.nowRfc3339();
  let window: Array<[string, string]>;
  try { window = usageWindow(start, end, now); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    // `2026-9-1` -> `2026-09-01`; an inverted window is swapped.
    let fixedStart = start === undefined ? undefined : paddedDate(start) ?? start;
    let fixedEnd = end === undefined ? undefined : paddedDate(end) ?? end;
    if (fixedStart !== undefined && fixedEnd !== undefined && fixedStart > fixedEnd && isDate(fixedStart) && isDate(fixedEnd)) [fixedStart, fixedEnd] = [fixedEnd, fixedStart];
    let valid = fixedStart !== start || fixedEnd !== end;
    if (valid) { try { usageWindow(fixedStart, fixedEnd, now); } catch { valid = false; } }
    if (!valid) throw caught;
    let argv = [...context.invocation.argv];
    for (const [option, value] of [["--start", fixedStart], ["--end", fixedEnd]] as const) {
      if (value !== undefined && context.option(option) !== undefined) argv = withOptionValue(argv, option, value);
    }
    throw context.corrected(caught, "Use the corrected dates: `{command}`", argv);
  }
  request.query.push(...window);
  const result = projectUsage(jsonObject(await context.send(request)));
  context.human = humanUsage(result);
  return result;
}

export function humanUsage(result: JsonObject): string {
  const period = result.period as JsonObject;
  let text = `Period: ${humanSafeScalar(String(period.start))} to ${humanSafeScalar(String(period.end))}\nRuns: ${String(result.total_runs)}\nInput tokens: ${String(result.total_input_tokens)}\nOutput tokens: ${String(result.total_output_tokens)}\nTotal price: ${dollarsOrDash(result.total_price_cents)}\n`;
  const daily = (result.daily ?? []) as JsonObject[];
  if (daily.length === 0) text += "No runs in this period.\n";
  for (const day of daily) {
    text += `${humanSafeScalar(String(day.date))}  runs ${String(day.runs)}  input ${String(day.input_tokens)}  output ${String(day.output_tokens)}  price ${dollarsOrDash(day.price_cents)}\n`;
  }
  return text;
}

// -------------------------------------------------------------------- redeem

/** The code from --code-file (surrounding whitespace trimmed). Never echoed. */
export function redeemCode(text: string): string {
  const code = text.trim();
  if (code.length === 0) throw invocationFailure("--code-file is empty; put the credit code in the file (or pipe it to --code-file -)");
  if (!validText(code, CODE_MAX_CHARS)) throw invocationFailure(`--code-file must contain one credit code on one line (at most ${CODE_MAX_CHARS} characters)`);
  return code;
}

export function projectRedeem(body: JsonObject): JsonObject {
  if (body.ok !== true) throw protocol("ok");
  const result: JsonObject = { ok: true, amount_usd: numberValue(body.amount_usd, "amount_usd") };
  for (const flag of ["already_redeemed", "credit_pending"]) {
    const value = body[flag];
    if (value === undefined || value === null) continue;
    if (typeof value !== "boolean") throw protocol(flag);
    result[flag] = value;
  }
  if (body.available_usd !== undefined && body.available_usd !== null) result.available_usd = numberValue(body.available_usd, "available_usd");
  if (body.message !== undefined && body.message !== null) {
    const message = freeText(body.message, "message");
    if (message.trim().length > 0) result.message = message;
  }
  return result;
}

async function redeem(context: Context): Promise<Json> {
  const source = context.option("--code-file");
  if (source === undefined) throw invocationFailure("--code-file is required");
  const code = redeemCode(await readText(context.cwd, source, CODE_FILE_MAX_BYTES, "--code-file"));
  const request = { ...requestFor(context.operation, 0, "/wallet/redeem"), body: new TextEncoder().encode(canonicalJson({ code })) };
  // The code is a short bearer secret, so the plan never carries a digest of
  // the body (it would be brute-forceable offline): body fields are null.
  const gate = context.gate(context.planned(0, "/wallet/redeem"));
  if (gate.kind === "preview") return gate.result;
  const result = projectRedeem(jsonObject(await context.send(request)));
  context.human = humanRedeem(result);
  return result;
}

export function humanRedeem(result: JsonObject): string {
  const amount = JSON.stringify(result.amount_usd);
  let text = result.credit_pending === true
    ? `Code claimed: $${amount} credit will appear shortly.\n`
    : result.already_redeemed === true
      ? `Code already redeemed to this wallet: $${amount} credit.\n`
      : `Code redeemed: $${amount} credit added.\n`;
  if (result.available_usd !== undefined) text += `Available: $${JSON.stringify(result.available_usd)}\n`;
  return text;
}

// --------------------------------------------------------------------- topup

/** --amount-cents: a positive integer (the service enforces its minimum). */
export function parseAmountCents(value: string): number {
  const amount = /^[0-9]+$/u.test(value) ? Number(value) : Number.NaN;
  if (Number.isSafeInteger(amount) && amount >= 1) return amount;
  throw invocationFailure(`--amount-cents must be a positive whole number of cents, for example 500 for $5.00; got ${quote(value)}`);
}

/** --idempotency-key: a lowercase canonical UUID. */
export function validIdempotencyKey(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
}

export function projectTopup(body: JsonObject, key: string): JsonObject {
  const url = body.checkout_url;
  if (typeof url !== "string" || url.length > 4096 || !/^https:\/\/[^\s]+$/u.test(url)) throw protocol("checkout_url");
  const session = body.session_id;
  if (typeof session !== "string" || !validText(session, 256)) throw protocol("session_id");
  const amount = object(body.amount, "amount");
  return {
    checkout_url: url,
    session_id: session,
    amount: {
      credits_cents: integer(amount.credits_cents, "amount.credits_cents", 0),
      fee_cents: integer(amount.fee_cents, "amount.fee_cents", 0),
      total_cents: integer(amount.total_cents, "amount.total_cents", 0),
    },
    idempotencyKey: key,
  };
}

function withIdempotencyKey(error: RunnerFailure, key: string): RunnerFailure {
  return new RunnerFailure({
    code: error.code, boundary: error.boundary, message: error.message, action: error.action,
    exitCode: error.exitCode, retryable: error.retryable, details: { ...(error.details ?? {}), idempotencyKey: key },
  });
}

async function topup(context: Context): Promise<Json> {
  const raw = context.option("--amount-cents");
  if (raw === undefined) throw invocationFailure("--amount-cents is required");
  const amount = parseAmountCents(raw);
  const given = context.option("--idempotency-key");
  if (given !== undefined && !validIdempotencyKey(given)) {
    throw invocationFailure(`--idempotency-key must be a lowercase UUID such as 123e4567-e89b-42d3-a456-426614174000 (reuse the idempotencyKey of an earlier top-up); got ${quote(given)}`);
  }
  const body = new TextEncoder().encode(canonicalJson({ amount_cents: amount }));
  const gate = context.gate(context.planned(0, "/wallet/topup", [], body));
  if (gate.kind === "preview") return gate.result;
  // The key is minted only once the request will really be sent, so --preview
  // and CONFIRMATION_REQUIRED stay free and deterministic.
  const key = given ?? context.uuidV4();
  const request = requestFor(context.operation, 0, "/wallet/topup");
  request.headers.push(["Idempotency-Key", key]);
  request.body = body;
  try {
    const result = projectTopup(jsonObject(await context.send(request)), key);
    context.human = context.localize(humanTopup(result));
    return result;
  } catch (caught) {
    // Retrying with the same key cannot create a second Checkout session.
    if (caught instanceof RunnerFailure) throw withIdempotencyKey(caught, key);
    throw caught;
  }
}

export function humanTopup(result: JsonObject): string {
  const amount = result.amount as JsonObject;
  return `Checkout URL: ${humanSafeScalar(String(result.checkout_url))}\nCredits: ${dollarsOrDash(amount.credits_cents)}\nFee: ${dollarsOrDash(amount.fee_cents)}\nTotal: ${dollarsOrDash(amount.total_cents)}\nIdempotency key: ${humanSafeScalar(String(result.idempotencyKey))}\nOpen the URL in a browser to pay. The credit lands in the wallet once payment settles; check with \`prose cli wallet balance\`.\n`;
}
