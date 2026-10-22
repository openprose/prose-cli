// Program workspaces: `program list|show|save|visibility|delete|revisions
// --org ORG` act in an organization's namespace, and `program copy --to ORG`
// copies a program into one. Every workspace route and query name the client
// sends is written here and in the manifest only (mirrors Rust
// `service/workspace.rs`); both are pinned by
// cli/conformance/cases/service/programs/.
import { failure, invocationFailure } from "../errors";
import { quote } from "../output";
import { RunnerFailure } from "../types";
import { encodeSegment, type Request } from "./http";
import { withAction, type Context } from "./index";
import { asciiLower, type Json, type JsonObject } from "./manifest";
import { jsonObject, requestFor } from "./http";
import { explainNotFound } from "./not-found";
import { refOf, validOwner, validRev, validSlug } from "./program-ref";

/** The query parameter that names the organization namespace of a `/programs` route. */
export const WORKSPACE_QUERY = "workspace";

/** `POST /p/{owner}/{slug}[@rev]/copy`: the copy route, addressed like a program read. */
export function copyPath(owner: string, slug: string, rev: string | undefined): string {
  return `/p/${encodeSegment(owner)}/${encodeSegment(slug)}${rev === undefined ? "" : `@${rev}`}/copy`;
}

/** The query naming the organization namespace of a `/programs` request; personal requests carry none. */
export function workspaceQuery(organization: string | undefined): Array<[string, string]> {
  return organization === undefined ? [] : [[WORKSPACE_QUERY, organization]];
}

/** Adds the organization namespace to a `/programs` request. */
export function scoped(request: Request, organization: string | undefined): Request {
  return { ...request, query: [...request.query, ...workspaceQuery(organization)] };
}

/** An organization slug that can own programs: lowercase letters, digits and interior hyphens, at most 39 characters (a program owner's limit). */
export const validNamespace = (value: string): boolean => /^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$/u.test(value);
/** The service's organization slug rule: 1 to 63 lowercase letters, digits or interior hyphens. */
const validOrganizationSlug = (value: string): boolean => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/u.test(value);

/**
 * The organization an option names (`--org` or `--to`), checked before any
 * request: an upper-case slug names the lower-case one to pass, and a slug
 * longer than a program owner can be is not an eligible namespace.
 */
export function organizationOption(context: Context, option: "--org" | "--to"): string | undefined {
  const given = context.option(option);
  if (given === undefined) return undefined;
  if (validNamespace(given)) return given;
  const lower = asciiLower(given);
  if (lower !== given && validNamespace(lower)) {
    const error = invocationFailure(`${option} ${quote(given)} must be an organization slug in lowercase; pass ${quote(lower)}`);
    throw context.corrected(error, `Pass ${lower}: \`{command}\``, context.argvWithOption(option, lower));
  }
  if (validOrganizationSlug(lower)) {
    throw invocationFailure(`organization ${quote(given)} cannot hold programs: a program owner is at most 39 characters, and this slug has ${lower.length}`);
  }
  const error = invocationFailure(`${option} ${quote(given)} must be an organization slug as \`${context.command("org list")}\` prints it: lowercase letters, digits and interior hyphens`);
  throw withAction(error, `Run \`${context.command("org list")}\` to find the organization's slug, then retry with it.`, { suggestedArgv: context.followUpArgv(["org", "list"]) });
}

/** SERVICE_FEATURE_DISABLED for a service that did not confirm the organization namespace; nothing was changed. */
function unconfirmedOrganization(organization: string): RunnerFailure {
  return failure("SERVICE_FEATURE_DISABLED", { reason: `the service did not confirm organization ${organization}, so it does not offer organization programs; nothing was changed` });
}

/** Whether a `GET /programs?workspace=ORG` reply echoes ORG, as a service with organization programs does. */
export function echoesOrganization(body: JsonObject, organization: string): boolean {
  const workspace = body.workspace;
  return workspace !== null && typeof workspace === "object" && !Array.isArray(workspace)
    && typeof workspace.slug === "string" && asciiLower(workspace.slug) === organization;
}

/** Refuses an organization listing the service did not confirm (a service that ignores `?workspace=` lists your own programs). */
export function requireEcho(body: JsonObject, organization: string): void {
  if (!echoesOrganization(body, organization)) throw unconfirmedOrganization(organization);
}

/** Refuses organization revisions owned by anyone but the organization (a service that ignores `?workspace=` lists yours). */
export function requireOrganizationOwner(owners: readonly unknown[], organization: string): void {
  if (owners.some((owner) => typeof owner !== "string" || asciiLower(owner) !== organization)) throw unconfirmedOrganization(organization);
}

/**
 * Confirms the organization before an organization-scoped write, through
 * `GET /programs?workspace=ORG` (manifest request `index`): the caller must
 * be a member (a 404 names the organization) and the service must echo the
 * organization, so a service that ignores `?workspace=` never receives a
 * write meant for the organization.
 */
export async function confirmOrganization(context: Context, index: number, organization: string): Promise<void> {
  let body: JsonObject;
  try { body = jsonObject(await context.send({ ...scoped(requestFor(context.operation, index, "/programs"), organization), class: "listing" as const })); }
  catch (caught) { throw organizationFailure(context, caught, organization, "reader", "listing its programs", "--org", true); }
  requireEcho(body, organization);
}

/** OWNER of `OWNER/SLUG` must be the `--org` organization (letter case ignored). */
export function requireSameOrganization(context: Context, owner: string, slug: string, organization: string, given: string): void {
  if (owner === "" || asciiLower(owner) === organization) return;
  const error = invocationFailure(`OWNER ${quote(owner)} is not --org ${quote(organization)}: with --org the program is ${organization}/${slug}; pass ${quote(slug)}`);
  throw context.corrected(error, `Pass ${slug}: \`{command}\``, context.argvWithArgument(given, given.slice(given.indexOf("/") + 1)));
}

/** The organization role each verb needs (reader reads and runs; developer also creates, edits and copies in; admin also changes visibility and deletes). */
export type Role = "reader" | "developer" | "admin";
const ROLES_TEXT = "reader can read and run; developer can also create, edit and copy in; admin can also change visibility and delete";

/**
 * Explains an organization-scoped failure: an organization the caller is
 * not a member of reads as not found (the service answers a non-member and
 * an unknown organization with the same 404; `anyMissing` reads every 404
 * that way, for verbs whose only 404 is the organization), a role that does
 * not allow the verb names the role it needs, and a namespace the service
 * will not let own programs says so. Other failures pass through.
 */
export function organizationFailure(context: Context, caught: unknown, organization: string, role: Role, what: string, option: "--org" | "--to" = "--org", anyMissing = false): unknown {
  if (!(caught instanceof RunnerFailure)) return caught;
  const code = caught.details?.serviceCode;
  if (caught.code === "SERVICE_RESOURCE_NOT_FOUND" && (anyMissing || code === "organization_not_found")) {
    const details = { ...(caught.details ?? {}), reason: `organization ${organization} was not found, or you are not a member of it` };
    const error = new RunnerFailure({ code: caught.code, boundary: caught.boundary, message: caught.message, action: caught.action, exitCode: caught.exitCode, retryable: caught.retryable, details });
    return explainNotFound(error, context.environment, context.mode, "organization", organization, ["org", "list"]);
  }
  // Without --org the verb acts on the caller's own programs; `copy --to` names another organization instead.
  const otherwise = option === "--org" ? "act on your own programs without --org" : "copy into another organization";
  if (caught.code === "SERVICE_REQUEST_REJECTED" && code === "organization_forbidden") {
    const ask = role === "admin" ? `Ask an admin of ${organization} to make you an admin` : `Ask an admin of ${organization} for the ${role} role`;
    return withAction(caught, `${ask}, or ${otherwise}.`, {
      reason: `your role in organization ${organization} does not allow this: ${what} needs the ${role} role (${ROLES_TEXT})`,
      requiredRole: role,
    });
  }
  if (caught.code === "SERVICE_REQUEST_REJECTED" && code === "workspace_namespace_unavailable") {
    const choose = `\`${context.command("org list")}\` lists your organizations`;
    return withAction(caught, option === "--org" ? `Choose another organization (${choose}), or ${otherwise}.` : `Copy into another organization (${choose}).`, {
      reason: `organization ${organization} cannot hold programs: its slug is not an eligible program namespace`,
    });
  }
  return caught;
}

const OWNER_KINDS: Record<string, "personal" | "organization"> = {
  personal: "personal", user: "personal", account: "personal",
  organization: "organization", org: "organization",
};

/**
 * Adds `owner_kind` and `copied_from` to a projected program record. The
 * service's `owner_kind` (or `ownerKind`) is kept when it is a known value;
 * `copied_from` is `{owner, slug, rev_id?}` or the reference text
 * `OWNER/SLUG[@rev_id]`, projected with its `ref`.
 */
export function projectWorkspace(record: JsonObject, out: JsonObject): void {
  const kind = record.owner_kind ?? record.ownerKind;
  if (typeof kind === "string" && Object.hasOwn(OWNER_KINDS, kind)) out.owner_kind = OWNER_KINDS[kind]!;
  const source = record.copied_from ?? record.copiedFrom;
  if (source === undefined || source === null) return;
  // Informational: a malformed source is dropped, never fatal to the record.
  const projected = copiedFrom(source);
  if (projected !== undefined) out.copied_from = projected;
}

function copiedFrom(source: Json): JsonObject | undefined {
  let owner: Json | undefined;
  let slug: Json | undefined;
  let rev: Json | undefined;
  if (typeof source === "string") {
    const at = source.indexOf("@");
    const name = at >= 0 ? source.slice(0, at) : source;
    const slash = name.indexOf("/");
    if (slash < 0) return undefined;
    owner = name.slice(0, slash);
    slug = name.slice(slash + 1);
    rev = at >= 0 ? source.slice(at + 1) : undefined;
  } else if (source !== null && typeof source === "object" && !Array.isArray(source)) {
    owner = source.owner;
    slug = source.slug;
    rev = source.rev_id ?? undefined;
  } else return undefined;
  if (typeof owner !== "string" || !validOwner(owner) || typeof slug !== "string" || !validSlug(slug)) return undefined;
  const out: JsonObject = { owner, slug };
  if (rev !== undefined) {
    if (typeof rev !== "string" || !validRev(rev)) return undefined;
    out.rev_id = rev;
    out.ref = refOf(owner, slug, rev);
  }
  return out;
}

/** `owner_kind=KIND  copied_from=REF` for a human listing line; empty when the record has neither. */
export function workspaceColumns(program: JsonObject): string {
  let text = "";
  if (typeof program.owner_kind === "string") text += `  owner_kind=${program.owner_kind}`;
  const source = program.copied_from as JsonObject | undefined;
  if (source !== undefined) text += `  copied_from=${copiedText(source)}`;
  return text;
}

/** OWNER/SLUG@rev_id of a `copied_from`, or OWNER/SLUG when the service named no revision. */
export function copiedText(source: JsonObject): string {
  return typeof source.ref === "string" ? source.ref : `${source.owner as string}/${source.slug as string}`;
}

/** Human `program show` notes (stderr): who owns the namespace and where the program was copied from. */
export function workspaceNotes(program: JsonObject): string {
  let text = "";
  if (typeof program.owner_kind === "string") text += `Owner: ${program.owner as string} (${program.owner_kind})\n`;
  const source = program.copied_from as JsonObject | undefined;
  if (source !== undefined) text += `Copied from: ${copiedText(source)}\n`;
  return text;
}
