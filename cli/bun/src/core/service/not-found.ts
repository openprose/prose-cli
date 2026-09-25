// Not-found errors that name the identifier, the environment and
// a listing command, and Actions built only from the details that are present.
// Mirrors cli/rust/crates/prose-runner-core/src/service/not_found.rs.
import { failure } from "../errors";
import { RunnerFailure, type OutputMode } from "../types";
import type { Environment, ManifestOperation } from "./manifest";
import { argvText, followUpArgv } from "./render";

/** A manifest `notFound` entry: what a 404 on this operation names. */
export interface NotFoundSpec {
  resource: string;
  id: string;
  list?: string[];
  hint?: string;
  serviceCodes?: Record<string, NotFoundSpec>;
}

const NOT_FOUND_ACTION = failure("SERVICE_RESOURCE_NOT_FOUND").action;
const REJECTED_ACTION = failure("SERVICE_REQUEST_REJECTED").action;

function rebuilt(error: RunnerFailure, action: string, details: Record<string, unknown>): RunnerFailure {
  return new RunnerFailure({ code: error.code, boundary: error.boundary, message: error.message, action, exitCode: error.exitCode, retryable: error.retryable, details });
}

/** Replaces `{ARGUMENT}` placeholders with invocation arguments; undefined when one is missing. */
function fill(template: string, argument: (name: string) => string | undefined): string | undefined {
  let missing = false;
  const value = template.replace(/\{([^{}]+)\}/gu, (_, name: string) => {
    const found = argument(name);
    if (found === undefined) missing = true;
    return found ?? "";
  });
  return missing ? undefined : value;
}

/**
 * Names the missing resource on a SERVICE_RESOURCE_NOT_FOUND: details.resource
 * {kind, id}, a reason naming the id, details.suggestedArgv
 * for the listing command, and an Action that quotes it. Details a handler
 * already set are kept.
 */
export function explainNotFound(error: RunnerFailure, environment: Environment, mode: OutputMode, kind: string, id: string, list: readonly string[] | undefined, hint?: string): RunnerFailure {
  if (error.code !== "SERVICE_RESOURCE_NOT_FOUND") return error;
  const details: Record<string, unknown> = { ...(error.details ?? {}) };
  const resource = (details.resource ?? { kind, id }) as { kind: string; id: string };
  details.resource = resource;
  if (typeof details.reason !== "string") details.reason = `${resource.kind} ${resource.id} was not found${hint === undefined ? "" : `, ${hint}`}`;
  if (!Array.isArray(details.suggestedArgv) && list !== undefined) details.suggestedArgv = followUpArgv(environment, mode, list);
  let action = error.action;
  if (action === NOT_FOUND_ACTION) {
    const argv = details.suggestedArgv as string[] | undefined;
    action = argv === undefined
      ? `Check the ${resource.kind} identifier named in Detail, then retry with the right one.`
      : `Run \`${argvText(argv)}\` to find the right ${resource.kind}, then retry with it.`;
  }
  return rebuilt(error, action, details);
}

/** Applies the operation's manifest `notFound` entry to a SERVICE_RESOURCE_NOT_FOUND. */
export function explainOperationNotFound(error: RunnerFailure, operation: ManifestOperation, argument: (name: string) => string | undefined, environment: Environment, mode: OutputMode): RunnerFailure {
  const base = (operation as ManifestOperation & { notFound?: NotFoundSpec }).notFound;
  if (error.code !== "SERVICE_RESOURCE_NOT_FOUND" || base === undefined) return error;
  const code = (error.details as Record<string, unknown> | undefined)?.serviceCode;
  const spec = (typeof code === "string" ? base.serviceCodes?.[code] : undefined) ?? base;
  const id = fill(spec.id, argument);
  if (id === undefined) return error;
  const list = spec.list === undefined ? undefined : spec.list.map((word) => fill(word, argument));
  return explainNotFound(error, environment, mode, spec.resource, id, list === undefined || list.includes(undefined) ? undefined : list as string[], spec.hint);
}

/**
 * The SERVICE_REQUEST_REJECTED Action names only the details that are present
 * (no "whichever of" hedge): the reason, the service message and the service
 * code, in that order; without any, the operation's help.
 */
export function explainRejected(error: RunnerFailure, environment: Environment, mode: OutputMode, help: readonly string[]): RunnerFailure {
  if (error.code !== "SERVICE_REQUEST_REJECTED" || error.action !== REJECTED_ACTION) return error;
  const details: Record<string, unknown> = { ...(error.details ?? {}) };
  const present = ["reason", "serviceMessage", "serviceCode"].filter((key) => typeof details[key] === "string").map((key) => `details.${key}`);
  if (present.length > 0) {
    const names = present.length === 1 ? present[0]! : `${present.slice(0, -1).join(", ")} and ${present.at(-1)!}`;
    return rebuilt(error, `Correct the request as ${names} ${present.length === 1 ? "describes" : "describe"}, then retry.`, details);
  }
  if (!Array.isArray(details.suggestedArgv)) details.suggestedArgv = followUpArgv(environment, mode, help);
  return rebuilt(error, `The service gave no reason; check the command against \`${argvText(details.suggestedArgv as string[])}\`, then retry.`, details);
}
