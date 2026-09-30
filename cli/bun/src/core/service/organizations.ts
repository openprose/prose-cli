// Service organization management: `org show|create|rename|default`,
// `org member list|role|remove`, `org invite` and `org invitation revoke|accept`.
// There is no `org delete` (the service always refuses it). `org list` stays
// the frozen service operation. Every mutation is confirm-class: the exact
// request is planned, gated, then sent. Responses are projected onto the closed
// service/organizations.schema.json shapes; anything else is
// SERVICE_PROTOCOL_INVALID. Mirrors
// cli/rust/crates/prose-runner-core/src/service/organizations.rs byte for byte.
import { failure, invocationFailure } from "../errors";
import { humanSafeScalar, quote } from "../output";
import { RunnerFailure } from "../types";
import { readText } from "./fs";
import { encodeSegment, jsonObject, requestFor, withJsonBody, type Request } from "./http";
import type { Context } from "./index";
import type { Json, JsonObject } from "./manifest";
import { addIso, argvText, durationText, humanResult, isoMs, validText } from "./render";

const MAX_REF = 256;
/** Account and invitation ids: the closed result schema caps both at 128, so a longer value is refused before anything is sent. */
const MAX_ID = 128;
const MAX_NAME = 200;
const MAX_TOKEN_FILE = 4096;
const MAX_TOKEN = 512;
const MAX_EXPIRES_IN = 2_592_000;
const ROLES = ["admin", "developer", "reader"];

function textArgument(context: Context, name: string, max: number): string {
  const value = context.argument(name) ?? "";
  if (validText(value, max)) return value;
  throw invocationFailure(`<${name}> must be 1-${max} characters with no control characters`);
}

/** A positional argument that becomes one URL path segment; `.` and `..` would be dropped by URL normalization. */
function segmentArgument(context: Context, name: string, max: number): string {
  const value = textArgument(context, name, max);
  if (value === "." || value === "..") throw invocationFailure(`<${name}> cannot be \`.\` or \`..\`; a URL path segment would drop it`);
  return value;
}

function roleValue(value: string, label: string): string {
  if (ROLES.includes(value)) return value;
  throw invocationFailure(`${label} must be admin, developer or reader`);
}

/** The service resolves `default` only for GET /organizations/default. */
const DEFAULT_HINT = "ORG `default` is accepted only by `cli org show`; run `prose cli org show default` and pass the slug it prints";

/** Not-found hints for member and invitation routes (member_not_found and invitation_not_found are not allowlisted service codes). */
const MEMBER_HINT = "ORG exists but has no such member; run `prose cli org member list ORG` and pass a member number it prints";
const INVITATION_HINT = "ORG exists but has no invitation with this INVITATION_ID; `prose cli org invite` prints the invitation id";
/** A rejected `invitation accept` (the invalid_invitation code is dropped). */
const ACCEPT_HINT = "the invitation token is invalid, expired, revoked, already used, or was issued to a different account";

function serviceCode(caught: RunnerFailure): unknown {
  return (caught.details as Record<string, unknown> | undefined)?.serviceCode;
}

/**
 * Sends a request that addresses `org`. A not-found for the literal `default` teaches the fix; on member and
 * invitation routes, a not-found that is not organization_not_found names the identifier that missed.
 */
async function sendFor(context: Context, request: Request, org: string, missing?: string): Promise<JsonObject> {
  let response;
  try { response = await context.send(request); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") {
      if (org === "default") throw failure("SERVICE_RESOURCE_NOT_FOUND", { ...(caught.details ?? {}), reason: context.localize(DEFAULT_HINT) });
      if (missing !== undefined && serviceCode(caught) !== "organization_not_found") {
        throw failure("SERVICE_RESOURCE_NOT_FOUND", { ...(caught.details ?? {}), reason: context.localize(missing) });
      }
    }
    throw caught;
  }
  return jsonObject(response);
}

export async function execute(context: Context): Promise<Json> {
  switch (context.operation.id) {
    case "org.show": return await show(context);
    case "org.create": return await create(context);
    case "org.rename": return await rename(context);
    case "org.default": return await setDefault(context);
    case "org.member.list": return await memberList(context);
    case "org.member.role": return await memberRole(context);
    case "org.member.remove": return await memberRemove(context);
    case "org.invite": return await invite(context);
    case "org.invitation.revoke": return await invitationRevoke(context);
    case "org.invitation.accept": return await invitationAccept(context);
    default: return await context.notImplemented();
  }
}

type Sent = { kind: "preview"; result: JsonObject } | { kind: "sent"; body: JsonObject };

/**
 * Plans a mutation, passes the confirmation gate, then sends it. `target` names, for the human preview, what the
 * request acts on that its Summary does not show (the JSON plan carries the path and the body digest); a secret body
 * field is never among them. `missing` is the not-found hint (mirrors Rust `confirm_and_send`).
 */
async function confirmAndSend(context: Context, org: string, path: string, body: JsonObject | undefined, target: ReadonlyArray<readonly [string, string]>, missing?: string): Promise<Sent> {
  let request = requestFor(context.operation, 0, path);
  if (body !== undefined) request = withJsonBody(request, body);
  const gate = context.gate(context.planned(0, path, [], request.body));
  if (gate.kind === "preview") {
    context.human = withTarget(humanResult(gate.result), target);
    return { kind: "preview", result: gate.result };
  }
  return { kind: "sent", body: await sendFor(context, request, org, missing) };
}

/** The human preview with one `Label: value` line per target, right after its `Preview:` line (mirrors Rust `with_target`). */
function withTarget(text: string, target: ReadonlyArray<readonly [string, string]>): string {
  const end = text.indexOf("\n");
  if (end < 0) return text;
  return text.slice(0, end + 1) + target.map(([label, value]) => `${label}: ${humanSafeScalar(value)}\n`).join("") + text.slice(end + 1);
}

/** The organization `cli org show` and `cli org member list` read without ORG: the caller's default. */
const DEFAULT_ORG = "default";

async function show(context: Context): Promise<Json> {
  const org = context.argument("ORG") === undefined ? DEFAULT_ORG : segmentArgument(context, "ORG", MAX_REF);
  const response = await context.send(requestFor(context.operation, 0, `/organizations/${encodeSegment(org)}`));
  const result = organizationResult(jsonObject(response));
  context.human = humanOrganization("Organization", result.organization as JsonObject);
  return result;
}

/** Organization slugs the service reserves (`validateOrganizationSlug`). */
const RESERVED_SLUGS = ["openprose", "system", "default", "invitations"];

/**
 * The service's organization slug rule: 1-63 lowercase letters, digits or
 * interior hyphens, not reserved and not UUID-shaped.
 */
function slugProblem(slug: string): string | undefined {
  if (!/^[a-z0-9-]{1,63}$/u.test(slug) || slug.startsWith("-") || slug.endsWith("-")) return "must be 1-63 lowercase letters, digits or interior hyphens";
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(slug);
  return RESERVED_SLUGS.includes(slug) || uuid ? "is reserved" : undefined;
}

/** A slug that satisfies the shape rule, derived from `slug`: lowercased, every other run of characters one hyphen, trimmed to 63. */
function suggestedSlug(slug: string): string | undefined {
  let out = "";
  for (const character of Array.from(slug.toLowerCase())) {
    if (/^[a-z0-9]$/u.test(character)) out += character;
    else if (out.length > 0 && !out.endsWith("-")) out += "-";
  }
  out = out.slice(0, 63).replace(/-+$/u, "");
  return slugProblem(out) === undefined && out !== slug ? out : undefined;
}

async function create(context: Context): Promise<Json> {
  const slug = textArgument(context, "SLUG", MAX_REF);
  const problem = slugProblem(slug);
  if (problem !== undefined) {
    const error = invocationFailure(`organization slug ${quote(slug)} ${problem}; slugs are global and permanent`);
    const better = suggestedSlug(slug);
    if (better === undefined) throw error;
    // The positional SLUG: the first token after `cli` equal to it that is not the value of --name.
    const argv = [...context.invocation.argv];
    const start = Math.max(0, argv.indexOf("cli"));
    for (let at = start; at < argv.length; at += 1) {
      if (argv[at] === slug && (at === 0 || argv[at - 1] !== "--name")) { argv[at] = better; break; }
    }
    throw new RunnerFailure({
      code: error.code, boundary: error.boundary, message: error.message, exitCode: error.exitCode, retryable: error.retryable,
      action: `Choose a slug that fits the rule, for example \`${argvText(argv)}\`.`,
      details: { ...(error.details ?? {}), suggestedArgv: argv },
    });
  }
  const body: JsonObject = {};
  const name = context.option("--name");
  if (name !== undefined) {
    if (!validText(name, MAX_NAME)) throw invocationFailure(`--name must be 1-${MAX_NAME} characters with no control characters`);
    body.name = name;
  }
  body.slug = slug;
  const sent = await confirmAndSend(context, "", "/organizations", body, []);
  if (sent.kind === "preview") return sent.result;
  const result = organizationResult(sent.body);
  context.human = humanOrganization("Created organization", result.organization as JsonObject);
  return result;
}

/** The new display name: NAME, or `--name NAME` as `org create` spells it. */
function renameName(context: Context): string {
  const positional = context.argument("NAME");
  const option = context.option("--name");
  if (positional !== undefined && option !== undefined && positional !== option) {
    throw invocationFailure("give the new display name once, as NAME or as --name NAME");
  }
  const value = positional ?? option;
  if (value === undefined) throw invocationFailure("missing the new display name: give NAME or --name NAME");
  if (validText(value, MAX_NAME)) return value;
  throw invocationFailure(positional !== undefined ? `<NAME> must be 1-${MAX_NAME} characters with no control characters` : `--name must be 1-${MAX_NAME} characters with no control characters`);
}

async function rename(context: Context): Promise<Json> {
  const org = segmentArgument(context, "ORG", MAX_REF);
  const name = renameName(context);
  const sent = await confirmAndSend(context, org, `/organizations/${encodeSegment(org)}`, { name }, [["Organization", org]]);
  if (sent.kind === "preview") return sent.result;
  const result = organizationResult(sent.body);
  context.human = humanOrganization("Renamed organization", result.organization as JsonObject);
  return result;
}

async function setDefault(context: Context): Promise<Json> {
  const org = textArgument(context, "ORG", MAX_REF);
  const sent = await confirmAndSend(context, org, "/organizations/default", { organization: org }, [["Organization", org]]);
  if (sent.kind === "preview") return sent.result;
  const result = organizationResult(sent.body);
  context.human = humanOrganization("Default organization", result.organization as JsonObject);
  return result;
}

/**
 * ORG, or without it the caller's default organization's slug: the service
 * resolves `default` only for GET /organizations/default (the operation's
 * request `index`), so that is read first (mirrors Rust `org_or_default`).
 */
async function orgOrDefault(context: Context, index: number): Promise<string> {
  if (context.argument("ORG") !== undefined) return segmentArgument(context, "ORG", MAX_REF);
  const response = await context.send(requestFor(context.operation, index, `/organizations/${DEFAULT_ORG}`));
  const organization = organizationResult(jsonObject(response)).organization as JsonObject;
  return organization.slug as string;
}

const byBytes = (left: string, right: string): number => Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));

/**
 * An organization's members, numbered: sorted by when they joined (then by
 * account), each `{member: "member N", handle?, role, created_at}` with its
 * account id kept alongside for resolving a member number (mirrors Rust
 * `numbered_members`). The account id itself is never printed.
 */
async function numberedMembers(context: Context, org: string, index: number): Promise<Array<[string, JsonObject]>> {
  const body = await sendFor(context, requestFor(context.operation, index, `/organizations/${encodeSegment(org)}/members`), org);
  const entries = own(body, "members");
  if (!Array.isArray(entries) || entries.length > 10_000) throw invalidField("members");
  const members = entries.map((entry): [string, JsonObject] => [textField(objectAt(entry, "members[]"), "members[].", "account_id", 128), member(entry, "members[]")]);
  members.sort(([leftAccount, left], [rightAccount, right]) => ((left.created_at as number) - (right.created_at as number)) || byBytes(leftAccount, rightAccount));
  return members.map(([account, entry], number) => [account, { member: `member ${number + 1}`, ...entry }]);
}

/** A `MEMBER` argument: a member number as `cli org member list` prints it (`N` or `member N`), or the member's account id (mirrors Rust `member_argument`). */
function memberArgument(context: Context): { number: number } | { account: string } {
  const value = segmentArgument(context, "MEMBER", MAX_ID);
  const lower = value.replace(/[A-Z]/gu, (character) => character.toLowerCase());
  let digits = lower.startsWith("member") ? lower.slice("member".length) : lower;
  if (lower.startsWith("member") && (digits.startsWith(" ") || digits.startsWith("-"))) digits = digits.slice(1);
  return /^[1-9][0-9]{0,4}$/u.test(digits) ? { number: Number(digits) } : { account: value };
}

/** The account id and label (`member N`, or the account id the caller named) of a `MEMBER` argument; a member number reads the member list (manifest request 1). */
async function resolveMember(context: Context, org: string): Promise<[string, string]> {
  const argument = memberArgument(context);
  if ("account" in argument) return [argument.account, argument.account];
  const members = await numberedMembers(context, org, 1);
  const found = members[argument.number - 1];
  if (found === undefined) {
    const count = members.length;
    const list = context.command(`org member list ${org}`);
    const base = invocationFailure(`member ${argument.number} is not in ${humanSafeScalar(org)}'s member list (${count} member${count === 1 ? "" : "s"}); \`${list}\` numbers them`);
    throw new RunnerFailure({
      code: base.code, boundary: base.boundary, message: base.message, action: `List the members with \`${list}\` and pass one of their numbers.`, exitCode: base.exitCode, retryable: base.retryable,
      details: { ...(base.details ?? {}), suggestedArgv: context.followUpArgv(["org", "member", "list", org]) },
    });
  }
  return [found[0], `member ${argument.number}`];
}

/** The human name of a member label: `member N`, or `member ACCOUNT` (mirrors Rust `member_name`). */
function memberName(label: string): string {
  return label.startsWith("member ") ? label : `member ${humanSafeScalar(label)}`;
}

async function memberList(context: Context): Promise<Json> {
  const org = await orgOrDefault(context, 1);
  const members = (await numberedMembers(context, org, 0)).map(([, entry]) => entry);
  let text = members.length === 0 ? `No members in ${humanSafeScalar(org)}.\n` : `Members of ${humanSafeScalar(org)} (${members.length}):\n`;
  for (const entry of members) {
    const handle = typeof entry.handle === "string" ? ` (${humanSafeScalar(entry.handle)})` : "";
    text += `${entry.member as string}${handle}  ${entry.role as string}\n`;
  }
  context.human = text;
  return { members };
}

async function memberRole(context: Context): Promise<Json> {
  const org = segmentArgument(context, "ORG", MAX_REF);
  memberArgument(context);
  const role = roleValue(context.argument("ROLE") ?? "", "<ROLE>");
  const [account, label] = await resolveMember(context, org);
  const sent = await confirmAndSend(context, org, `/organizations/${encodeSegment(org)}/members/${encodeSegment(account)}`, { role }, [["Organization", org], ["Member", label]], MEMBER_HINT);
  if (sent.kind === "preview") return sent.result;
  const projected: JsonObject = { ...member(own(sent.body, "member"), "member"), member: label };
  const name = memberName(label);
  context.human = `${name.charAt(0).toUpperCase()}${name.slice(1)} is now ${projected.role as string}.\n`;
  return { member: projected };
}

async function memberRemove(context: Context): Promise<Json> {
  const org = segmentArgument(context, "ORG", MAX_REF);
  const [account, label] = await resolveMember(context, org);
  const sent = await confirmAndSend(context, org, `/organizations/${encodeSegment(org)}/members/${encodeSegment(account)}`, undefined, [["Organization", org], ["Member", label]], MEMBER_HINT);
  if (sent.kind === "preview") return sent.result;
  const result = ok(sent.body);
  context.human = `Removed ${memberName(label)} from ${humanSafeScalar(org)}.\n`;
  return result;
}

/** --expires-in: decimal digits only, 1 to 2592000 seconds. */
export function expiresIn(value: string): number {
  if (/^[0-9]+$/u.test(value)) {
    const seconds = Number(value);
    if (Number.isSafeInteger(seconds) && seconds >= 1 && seconds <= MAX_EXPIRES_IN) return seconds;
  }
  throw invocationFailure(`--expires-in must be a whole number of seconds from 1 to ${MAX_EXPIRES_IN}`);
}

async function invite(context: Context): Promise<Json> {
  const org = segmentArgument(context, "ORG", MAX_REF);
  const account = textArgument(context, "ACCOUNT_ID", MAX_ID);
  const role = roleValue(context.option("--role") ?? "", "--role");
  const body: JsonObject = { accountId: account };
  const expires = context.option("--expires-in");
  const target: Array<readonly [string, string]> = [["Organization", org], ["Account", account]];
  if (expires !== undefined) {
    const seconds = expiresIn(expires);
    body.expiresInSeconds = seconds;
    target.push(["Expires in", durationText(seconds)]);
  }
  body.role = role;
  const sent = await confirmAndSend(context, org, `/organizations/${encodeSegment(org)}/invitations`, body, target);
  if (sent.kind === "preview") return sent.result;
  const result = invitation(sent.body);
  const entry = result.invitation as JsonObject;
  context.human = context.localize(`Invitation ${humanSafeScalar(entry.id as string)} for ${humanSafeScalar(entry.account_id as string)} as ${entry.role as string} (expires ${isoMs(entry.expires_at) ?? String(entry.expires_at)}).\n`
    + `Token: ${humanSafeScalar(result.token as string)}\n`
    + "The token appears only here. The invitee accepts with `prose cli org invitation accept --token-file FILE`.\n");
  return result;
}

async function invitationRevoke(context: Context): Promise<Json> {
  const org = segmentArgument(context, "ORG", MAX_REF);
  const id = segmentArgument(context, "INVITATION_ID", MAX_ID);
  const sent = await confirmAndSend(context, org, `/organizations/${encodeSegment(org)}/invitations/${encodeSegment(id)}`, undefined, [["Organization", org], ["Invitation", id]], INVITATION_HINT);
  if (sent.kind === "preview") return sent.result;
  const result = ok(sent.body);
  context.human = `Revoked invitation ${humanSafeScalar(id)} in ${humanSafeScalar(org)}.\n`;
  return result;
}

async function invitationAccept(context: Context): Promise<Json> {
  const source = context.option("--token-file") ?? "";
  const text = await readText(context.cwd, source, MAX_TOKEN_FILE, "--token-file");
  const token = text.endsWith("\r\n") ? text.slice(0, -2) : text.endsWith("\n") ? text.slice(0, -1) : text;
  if (!validText(token, MAX_TOKEN)) {
    throw invocationFailure(`--token-file must hold one invitation token (1-${MAX_TOKEN} characters, no control characters) on a single line`);
  }
  let sent: Sent;
  try { sent = await confirmAndSend(context, "", "/organizations/invitations/accept", { token }, []); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_REQUEST_REJECTED" && [undefined, "invalid_invitation"].includes(serviceCode(caught) as string | undefined)) {
      throw failure("SERVICE_REQUEST_REJECTED", { ...(caught.details ?? {}), reason: ACCEPT_HINT });
    }
    throw caught;
  }
  if (sent.kind === "preview") return sent.result;
  const result = organizationResult(sent.body);
  context.human = humanOrganization("Joined organization", result.organization as JsonObject);
  return result;
}

// ---- response projections (closed service/organizations.schema.json) ----

/** SERVICE_PROTOCOL_INVALID naming the response field that failed. */
function invalidField(field: string): RunnerFailure {
  return failure("SERVICE_PROTOCOL_INVALID", { reason: `the service response has a missing or invalid ${field}` });
}

function objectAt(value: Json | undefined, name: string): JsonObject {
  if (value === undefined || value === null || typeof value !== "object" || Array.isArray(value)) throw invalidField(name);
  return value;
}

function own(source: JsonObject, key: string): Json | undefined {
  return Object.hasOwn(source, key) ? source[key] : undefined;
}

function textField(source: JsonObject, prefix: string, key: string, max: number): string {
  const value = own(source, key);
  if (typeof value === "string" && validText(value, max)) return value;
  throw invalidField(`${prefix}${key}`);
}

function uuidField(source: JsonObject, prefix: string, key: string): string {
  const value = own(source, key);
  if (typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value)) return value;
  throw invalidField(`${prefix}${key}`);
}

function slugField(source: JsonObject, prefix: string, key: string): string {
  const value = own(source, key);
  if (typeof value === "string" && /^[a-z0-9][a-z0-9-]{0,63}$/u.test(value)) return value;
  throw invalidField(`${prefix}${key}`);
}

function epochField(source: JsonObject, prefix: string, key: string): number {
  const value = own(source, key);
  // `-0` passes as 0, as the Rust product reads it.
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value === 0 ? 0 : value;
  throw invalidField(`${prefix}${key}`);
}

function nullableEpoch(source: JsonObject, prefix: string, key: string): number | null {
  const value = own(source, key);
  return value === undefined || value === null ? null : epochField(source, prefix, key);
}

function roleField(source: JsonObject, prefix: string, key: string): string {
  const value = own(source, key);
  if (typeof value === "string" && ROLES.includes(value)) return value;
  throw invalidField(`${prefix}${key}`);
}

export function organization(value: Json | undefined): JsonObject {
  const source = objectAt(value, "organization");
  const prefix = "organization.";
  const output: JsonObject = {};
  if (Object.hasOwn(source, "created_at")) output.created_at = epochField(source, prefix, "created_at");
  output.id = uuidField(source, prefix, "id");
  output.name = textField(source, prefix, "name", MAX_REF);
  if (Object.hasOwn(source, "role")) output.role = roleField(source, prefix, "role");
  output.slug = slugField(source, prefix, "slug");
  addIso(output, ["created_at"]);
  return output;
}

function organizationResult(body: JsonObject): JsonObject {
  return { organization: organization(own(body, "organization")) };
}

/**
 * One member's public fields; `name` is `member` or `members[]`. The account
 * id is checked but never printed, and the organization id is the caller's
 * own `ORG`; a `handle` is kept when the service sends one (mirrors Rust).
 */
function member(value: Json | undefined, name: string): JsonObject {
  const source = objectAt(value, name);
  const prefix = `${name}.`;
  textField(source, prefix, "account_id", 128);
  const projected: JsonObject = {
    created_at: epochField(source, prefix, "created_at"),
    role: roleField(source, prefix, "role"),
  };
  const handle = source.handle;
  if (typeof handle === "string" && validText(handle, 64)) projected.handle = handle;
  addIso(projected, ["created_at"]);
  return projected;
}

export function invitation(body: JsonObject): JsonObject {
  const source = objectAt(own(body, "invitation"), "invitation");
  const prefix = "invitation.";
  const projected: JsonObject = {
    account_id: textField(source, prefix, "account_id", 128),
    consumed_at: nullableEpoch(source, prefix, "consumed_at"),
    created_at: epochField(source, prefix, "created_at"),
    expires_at: epochField(source, prefix, "expires_at"),
    id: textField(source, prefix, "id", 128),
    revoked_at: nullableEpoch(source, prefix, "revoked_at"),
    role: roleField(source, prefix, "role"),
  };
  addIso(projected, ["consumed_at", "created_at", "expires_at", "revoked_at"]);
  return { invitation: projected, token: textField(body, "", "token", MAX_TOKEN) };
}

function ok(body: JsonObject): JsonObject {
  if (own(body, "ok") === true) return { ok: true };
  throw invalidField("ok");
}

function humanOrganization(heading: string, entry: JsonObject): string {
  let text = `${heading}: ${humanSafeScalar(entry.slug as string)}\nName: ${humanSafeScalar(entry.name as string)}\nID: ${entry.id as string}\n`;
  if (typeof entry.role === "string") text += `Your role: ${entry.role}\n`;
  return text;
}
