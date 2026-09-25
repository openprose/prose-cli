import { quote } from "../output";
// `OWNER/SLUG[@REV]` parsing and latest-to-pinned resolution. A pinned reference is `owner/slug@<16 hex rev_id>`.
import { failure, invocationFailure } from "../errors";
import { RunnerFailure } from "../types";
import { encodeSegment, jsonObject, requestFor } from "./http";
import type { Context } from "./index";
import type { JsonObject } from "./manifest";
import { explainNotFound } from "./not-found";
import { argvText } from "./render";

/** `revNumber` is a numeric `@N` revision (only `program show` resolves it). */
export interface ProgramRef { owner: string; slug: string; rev?: string; revNumber?: number }

export const validOwner = (value: string): boolean => /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/u.test(value);
export const validSlug = (value: string): boolean => /^[a-z0-9][a-z0-9-]{0,63}$/u.test(value);
export const validRev = (value: string): boolean => /^[0-9a-f]{16}$/u.test(value);
/** A revision number `N` of `@N` (the `rev` column), 1 to 999999999. */
export const validRevNumber = (value: string): boolean => /^[1-9][0-9]{0,8}$/u.test(value);

/** The revision number a `@REV` names by number: `3`, or `rev3` / `r3` as people write it (mirrors Rust `rev_number_text`). */
function revNumberText(rev: string | undefined): string | undefined {
  if (rev === undefined) return undefined;
  if (validRevNumber(rev)) return rev;
  const number = rev.startsWith("rev") ? rev.slice(3) : rev.startsWith("r") ? rev.slice(1) : undefined;
  return number !== undefined && validRevNumber(number) ? number : undefined;
}

/** `owner/slug@rev_id`, the pinned reference `program list`, `revisions` and `show` print as `ref`. */
export const refOf = (owner: string, slug: string, rev: string): string => `${owner}/${slug}@${rev}`;

/** A numeric `@N` where a rev_id is required: name the command that resolves it. */
function revisionNumberFailure(value: string, name: string, rev: string): RunnerFailure {
  return invocationFailure(`program reference ${quote(value)} names revision ${rev} by number; this command needs the 16-hex-digit rev_id, which \`cli program show ${name}@${rev} --json\` prints as \`ref\``);
}

export function pinned(reference: ProgramRef): string | undefined {
  return reference.rev === undefined ? undefined : `${reference.owner}/${reference.slug}@${reference.rev}`;
}

export function readPath(reference: ProgramRef): string {
  return `/p/${encodeSegment(reference.owner)}/${encodeSegment(reference.slug)}`;
}

/**
 * Parses `OWNER/SLUG[@REV]`; `requireRev` rejects an unpinned reference. A
 * numeric `@N` is kept as `revNumber` when `allowNumber` (only `program show`
 * resolves it), otherwise refused with the command that prints the rev_id.
 */
export function parseProgramRef(value: string, requireRev: boolean, allowNumber = false): ProgramRef {
  const expected = requireRev ? "OWNER/SLUG@REV" : "OWNER/SLUG[@REV]";
  const invalid = () => invocationFailure(`program reference ${quote(value)} must be ${expected}: an owner handle, a lowercase slug and, after @, the 16-hex-digit rev_id`);
  const at = value.indexOf("@");
  const name = at >= 0 ? value.slice(0, at) : value;
  const rev = at >= 0 ? value.slice(at + 1) : undefined;
  const slash = name.indexOf("/");
  if (slash < 0) throw invalid();
  const owner = name.slice(0, slash);
  const slug = name.slice(slash + 1);
  const number = revNumberText(rev);
  if (validOwner(owner) && validSlug(slug) && number !== undefined) {
    if (allowNumber) return { owner, slug, revNumber: Number(number) };
    throw revisionNumberFailure(value, name, number);
  }
  if (!validOwner(owner) || !validSlug(slug) || (rev !== undefined && !validRev(rev)) || (requireRev && rev === undefined)) throw invalid();
  return rev === undefined ? { owner, slug } : { owner, slug, rev };
}

/**
 * Parses `[OWNER/]SLUG[@REV]`. A bare slug is the caller's own program: the
 * owner stays empty until `fillOwner` reads it, so a syntax error
 * is still reported before any request.
 */
export function parseOwnAllowed(value: string, allowNumber = false): ProgramRef {
  const at = value.indexOf("@");
  const name = at >= 0 ? value.slice(0, at) : value;
  const rev = at >= 0 ? value.slice(at + 1) : undefined;
  if (name.includes("/")) return parseProgramRef(value, false, allowNumber);
  const number = revNumberText(rev);
  if (validSlug(name) && number !== undefined) {
    if (allowNumber) return { owner: "", slug: name, revNumber: Number(number) };
    throw revisionNumberFailure(value, name, number);
  }
  if (!validSlug(name) || (rev !== undefined && !validRev(rev))) {
    throw invocationFailure(`program reference ${quote(value)} must be [OWNER/]SLUG[@REV]: an optional owner handle, a lowercase slug and, after @, the 16-hex-digit rev_id; a bare SLUG is your own program`);
  }
  return rev === undefined ? { owner: "", slug: name } : { owner: "", slug: name, rev };
}

/**
 * Fills the owner of a bare-slug reference from GET /programs/{slug}/revisions
 * (manifest request `index`), which lists only the caller's own programs.
 */
export async function fillOwner(context: Context, reference: ProgramRef, index: number): Promise<void> {
  if (reference.owner !== "") return;
  const slug = reference.slug;
  const request = { ...requestFor(context.operation, index, `/programs/${encodeSegment(slug)}/revisions`), class: "control" as const };
  let body: JsonObject;
  try { body = jsonObject(await context.send(request)); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") throw missingOwn(context, slug, caught.details ?? {});
    throw caught;
  }
  const own = ownRevisions(body, slug);
  if (own === undefined) throw missingOwn(context, slug, {});
  reference.owner = own.owner;
}

/** SERVICE_RESOURCE_NOT_FOUND for a bare SLUG the caller has not saved. */
function missingOwn(context: Context, slug: string, details: Record<string, unknown>): RunnerFailure {
  const reason = `you have no saved program ${quote(slug)}; list yours with \`${context.command("program list")}\`, or pass OWNER/SLUG for another owner's public program`;
  return explainNotFound(failure("SERVICE_RESOURCE_NOT_FOUND", { ...details, reason }), context.environment, context.mode, "program", slug, ["program", "list"]);
}

/**
 * Resolves a reference to `owner/slug@rev_id`. A pinned reference makes no
 * request; otherwise GET /p/{owner}/{slug} (manifest request `index`) supplies
 * the latest rev_id. Returns the pinned reference and the program read.
 */
export async function resolveProgramRef(context: Context, reference: ProgramRef, index: number): Promise<{ ref: string; program?: JsonObject }> {
  const fixed = pinned(reference);
  if (fixed !== undefined) return { ref: fixed };
  const request = { ...requestFor(context.operation, index, readPath(reference)), class: "control" as const };
  const body = jsonObject(await context.send(request));
  const program = (body.program ?? null) as JsonObject | null;
  const rev = program?.rev_id;
  const owner = program?.owner;
  if (program === null || typeof rev !== "string" || !validRev(rev) || typeof owner !== "string" || owner.toLowerCase() !== reference.owner.toLowerCase() || program.slug !== reference.slug) {
    throw failure("SERVICE_PROTOCOL_INVALID");
  }
  return { ref: `${owner}/${reference.slug}@${rev}`, program };
}

/** The caller's handle and revisions from a GET /programs/{slug}/revisions body (own programs only). */
function ownRevisions(body: JsonObject, slug: string): { owner: string; revisions: JsonObject[] } | undefined {
  const revisions = body.revisions;
  if (!Array.isArray(revisions)) throw failure("SERVICE_PROTOCOL_INVALID");
  const first = revisions[0];
  if (first === undefined) return undefined;
  const owner = first !== null && typeof first === "object" && !Array.isArray(first) ? (first as JsonObject).owner : undefined;
  if (typeof owner !== "string" || !validOwner(owner) || (first as JsonObject).slug !== slug) throw failure("SERVICE_PROTOCOL_INVALID");
  return { owner, revisions: revisions.filter((item): item is JsonObject => item !== null && typeof item === "object" && !Array.isArray(item)) };
}

/**
 * Resolves a numeric `@N` of `program show` to its rev_id through
 * GET /programs/{slug}/revisions (manifest request `index`), which lists only
 * the caller's own programs; the owner is filled too. Another owner's program
 * has no revision listing, so its `@N` is refused with the rev_id to use.
 */
export async function resolveRevNumber(context: Context, reference: ProgramRef, index: number): Promise<void> {
  const number = reference.revNumber;
  if (number === undefined) return;
  const slug = reference.slug;
  const other = (): RunnerFailure => {
    const name = `${reference.owner}/${slug}`;
    return invocationFailure(`revision numbers such as @${number} resolve only for your own programs; for ${name} pass the 16-hex-digit rev_id, which \`${context.command(`program show ${name} --json`)}\` prints (latest) as \`ref\``);
  };
  const request = { ...requestFor(context.operation, index, `/programs/${encodeSegment(slug)}/revisions`), class: "control" as const };
  let body: JsonObject;
  try { body = jsonObject(await context.send(request)); }
  catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "SERVICE_RESOURCE_NOT_FOUND") throw reference.owner !== "" ? other() : missingOwn(context, slug, caught.details ?? {});
    throw caught;
  }
  const own = ownRevisions(body, slug);
  if (own === undefined) throw reference.owner !== "" ? other() : missingOwn(context, slug, {});
  if (reference.owner !== "" && reference.owner.toLowerCase() !== own.owner.toLowerCase()) throw other();
  const match = own.revisions.find((item) => item.rev === number);
  const rev = match?.rev_id;
  if (match === undefined || typeof rev !== "string" || !validRev(rev)) {
    const newest = typeof own.revisions[0]?.rev === "number" ? own.revisions[0].rev : undefined;
    const tail = newest === undefined ? "" : `; the newest is rev ${newest}`;
    throw invocationFailure(`${own.owner}/${slug} has no revision ${number}${tail}; list them with \`${context.command(`program revisions ${slug}`)}\``);
  }
  reference.owner = own.owner;
  reference.rev = rev;
  delete reference.revNumber;
}

/**
 * Resolves a program reference to run (`run submit --from`) to the pinned
 * `owner/slug@rev_id`, the way `program show` reads references (mirrors Rust
 * `resolve_to_run`): a bare SLUG is the caller's own program, `@N` (or
 * `@revN`) is revision N of the caller's own program, a pinned `@REV` of the
 * caller's own program must be one of its revisions (a commit id is named as
 * such, with the rev_id to pass), and a latest reference reads the newest
 * rev_id. Another owner's pinned reference is checked by the service.
 */
export async function resolveToRun(context: Context, reference: ProgramRef, list: number, read: number, option: string | undefined, verifyPinned: boolean): Promise<string> {
  const given = option === undefined ? "" : context.option(option) ?? "";
  if (reference.owner === "" || reference.revNumber !== undefined || (verifyPinned && reference.rev !== undefined)) {
    const slug = reference.slug;
    const request = { ...requestFor(context.operation, list, `/programs/${encodeSegment(slug)}/revisions`), class: "control" as const };
    let body: JsonObject | undefined;
    let missing: RunnerFailure | undefined;
    try { body = jsonObject(await context.send(request)); }
    catch (caught) {
      if (!(caught instanceof RunnerFailure) || caught.code !== "SERVICE_RESOURCE_NOT_FOUND") throw caught;
      missing = caught;
    }
    const own = body === undefined ? undefined : ownRevisions(body, slug);
    if (own !== undefined && (reference.owner === "" || reference.owner.toLowerCase() === own.owner.toLowerCase())) {
      reference.owner = own.owner;
      const name = `${own.owner}/${slug}`;
      if (reference.revNumber !== undefined) {
        const number = reference.revNumber;
        delete reference.revNumber;
        const rev = own.revisions.find((item) => item.rev === number)?.rev_id;
        if (typeof rev !== "string" || !validRev(rev)) throw noRevision(context, name, slug, String(number), own.revisions);
        reference.rev = rev;
      } else if (verifyPinned && reference.rev !== undefined) {
        const rev = reference.rev;
        if (!own.revisions.some((item) => item.rev_id === rev)) {
          const commit = own.revisions.find((item) => item.commit_id === rev);
          if (commit !== undefined) {
            const revId = typeof commit.rev_id === "string" ? commit.rev_id : "";
            const number = typeof commit.rev === "number" ? commit.rev : 0;
            const error = invocationFailure(`${rev} is the commit_id of ${name} rev ${number}, not a rev_id; a program reference pins the rev_id, ${revId}`);
            throw option === undefined ? error : context.corrected(error, "Pass the rev_id: `{command}`", context.argvWithOption(option, given.replace(`@${rev}`, `@${revId}`)));
          }
          throw noRevision(context, name, slug, rev, own.revisions);
        }
      }
    } else {
      if (reference.owner === "") throw missingOwn(context, slug, missing?.details ?? {});
      if (reference.revNumber !== undefined) {
        const name = `${reference.owner}/${slug}`;
        const argv = context.followUpArgv(["program", "show", name]);
        const error = invocationFailure(`revision numbers such as @${reference.revNumber} resolve only for your own programs; for ${name} pass the 16-hex-digit rev_id, which \`${context.command(`program show ${name}`)}\` prints (latest) as \`ref\``);
        throw new RunnerFailure({ code: error.code, boundary: error.boundary, message: error.message, action: `Read the program's rev_id with \`${argvText(argv)}\`, then pass OWNER/SLUG@REV.`, exitCode: error.exitCode, retryable: error.retryable, details: { ...(error.details ?? {}), suggestedArgv: argv } });
      }
    }
  }
  return (await resolveProgramRef(context, reference, read)).ref;
}

/** SERVICE_RESOURCE_NOT_FOUND for a revision the caller's program does not have, naming the newest and the listing command (mirrors Rust `no_revision`). */
function noRevision(context: Context, name: string, slug: string, rev: string, revisions: JsonObject[]): RunnerFailure {
  const newest = typeof revisions[0]?.rev === "number" ? `; the newest is rev ${revisions[0].rev}` : "";
  const reason = `${name} has no revision ${rev}${newest}; list them with \`${context.command(`program revisions ${slug}`)}\``;
  return explainNotFound(failure("SERVICE_RESOURCE_NOT_FOUND", { reason }), context.environment, context.mode, "revision", rev, ["program", "revisions", slug]);
}

/**
 * The `<SLUG>` of an owner-scoped verb (`program save|visibility|delete|revisions`,
 * `result publish|unpublish`): a bare slug, or `OWNER/SLUG` as `program list`
 * prints it when OWNER is the caller. Syntax is checked here,
 * before any request; `confirmOwnSlug` checks OWNER.
 */
export function parseOwnSlug(value: string): ProgramRef {
  const at = value.indexOf("@");
  const name = at >= 0 ? value.slice(0, at) : value;
  const slash = name.indexOf("/");
  const owner = slash >= 0 ? name.slice(0, slash) : "";
  const slug = slash >= 0 ? name.slice(slash + 1) : name;
  const wellFormed = (slash < 0 || validOwner(owner)) && validSlug(slug);
  if (wellFormed && at >= 0) throw invocationFailure(`<SLUG> names a program, not a revision; pass ${quote(slug)} without @${value.slice(at + 1)}`);
  if (wellFormed) return { owner, slug };
  const lower = value.toLowerCase();
  const hint = !value.includes("/") && lower !== value && validSlug(lower) ? `; pass ${quote(lower)}` : "";
  throw invocationFailure(`<SLUG> ${quote(value)} must be a program slug in your own account: lowercase letters, digits and hyphens, at most 64 characters (OWNER/SLUG is accepted when OWNER is you)${hint}`);
}

/**
 * `parseOwnSlug` of this invocation's `<SLUG>`, with the exact fix when one
 * exists: the slug without its `@REV` (`hello@1` -> `hello`), or in lowercase
 * (mirrors Rust `own_slug_argument`).
 */
export function ownSlugReference(context: Context): ProgramRef {
  const value = context.argument("SLUG") ?? "";
  try { return parseOwnSlug(value); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure)) throw caught;
    const name = value.includes("@") ? value.slice(0, value.indexOf("@")) : value;
    const fixed = [name, name.toLowerCase()].find((candidate) => {
      if (candidate === value) return false;
      try { parseOwnSlug(candidate); return true; } catch { return false; }
    });
    throw fixed === undefined ? caught : context.corrected(caught, `Pass ${fixed}: \`{command}\``, context.argvWithArgument(value, fixed));
  }
}

/** Refuses OWNER when the caller's own revisions of the slug name another handle. An empty listing confirms nothing; see `unconfirmedOwner`. */
export function checkOwner(reference: ProgramRef, body: JsonObject): void {
  if (reference.owner === "") return;
  const own = ownRevisions(body, reference.slug);
  if (own === undefined || own.owner.toLowerCase() === reference.owner.toLowerCase()) return;
  throw invocationFailure(`OWNER ${quote(reference.owner)} is not you: your program ${quote(reference.slug)} is ${own.owner}/${reference.slug}, and <SLUG> names a program in your own account; pass ${quote(reference.slug)}`);
}

/**
 * Checks the OWNER of `OWNER/SLUG` against the caller through
 * GET /programs/{slug}/revisions (manifest request `index`). A bare slug makes
 * no request. When the caller has no program SLUG, OWNER cannot be confirmed,
 * so the verb is refused before anything is changed: an
 * unconfirmed OWNER never reaches a write, and its 404 is never reported as
 * alreadyAbsent. A confirmed OWNER is recorded on the context so the plan
 * (--preview, CONFIRMATION_REQUIRED) says it was verified. Returns the
 * confirmed owner, or "" for a bare slug.
 */
export async function confirmOwnSlug(context: Context, reference: ProgramRef, index: number, creates: boolean): Promise<string> {
  if (reference.owner === "") return "";
  const slug = reference.slug;
  const request = { ...requestFor(context.operation, index, `/programs/${encodeSegment(slug)}/revisions`), class: "control" as const };
  let body: JsonObject | undefined;
  try { body = jsonObject(await context.send(request)); }
  catch (caught) {
    if (!(caught instanceof RunnerFailure) || caught.code !== "SERVICE_RESOURCE_NOT_FOUND") throw caught;
    body = undefined;
  }
  const own = body === undefined ? undefined : ownRevisions(body, slug);
  if (own === undefined) {
    if (creates) throw invocationFailure(`you have no saved program ${quote(slug)} yet, so OWNER ${quote(reference.owner)} cannot be checked against your account; a first save takes the bare slug: pass ${quote(slug)}`);
    throw unconfirmedOwner(context, reference);
  }
  checkOwner(reference, body!);
  context.verifiedOwner = own.owner;
  return own.owner;
}

/** INVOCATION_INVALID for an OWNER that cannot be confirmed as the caller: the caller has no program SLUG to check it against. */
export function unconfirmedOwner(context: Context, reference: ProgramRef): RunnerFailure {
  const slug = JSON.stringify(reference.slug);
  const base = invocationFailure(`OWNER ${quote(reference.owner)} is not confirmed as you: you have no saved program ${slug} to check it against, and <SLUG> names a program in your own account; nothing was changed. Pass the bare slug ${slug} for your own program; another owner's program cannot be changed from this account`);
  return new RunnerFailure({
    code: base.code, boundary: base.boundary, message: base.message,
    action: `Pass the bare slug ${slug} to act on your own program; \`${context.command("program list")}\` lists yours as OWNER/SLUG.`,
    exitCode: base.exitCode, retryable: false,
    details: { ...(base.details ?? {}), suggestedArgv: context.followUpArgv(["program", "list"]) },
  });
}
