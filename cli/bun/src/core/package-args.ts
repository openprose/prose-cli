import { invocationFailure } from "./errors";
import { localizeProduct } from "./service/render";
import { RunnerFailure } from "./types";
import { digest, identity, version } from "./package-format";
export type PackageOperation = "publish" | "fetch" | "list" | "withdraw";
export interface PackageCommand {
  operation: PackageOperation;
  input: string;
  organization?: string;
  name?: string;
  version?: string;
  public?: boolean;
  outputDir?: string;
  sha256?: string;
  cursor?: string;
  /**
   * Why the invocation is invalid: reported in the
   * openprose.service-operation/1 envelope, before any request.
   */
  invalid?: string;
}
const OPERATIONS: readonly PackageOperation[] = ["publish", "fetch", "list", "withdraw"];
/** Options per command, in help order (mirrors Rust `registry::OPTIONS`). */
const OPTIONS: Record<PackageOperation, readonly string[]> = {
  publish: ["--organization", "--name", "--version", "--public"],
  fetch: ["--output-dir", "--sha256"],
  list: ["--cursor"],
  withdraw: [],
};
const TARGET: Record<PackageOperation, string> = {
  publish: "FILE|DIR, the file or package directory to publish",
  fetch: "ORG/NAME@VERSION, the exact package version to download",
  list: "ORG, the organization whose public packages to list",
  withdraw: "ORG/NAME@VERSION, the exact package version to withdraw",
};
const TARGET_NAME: Record<PackageOperation, string> = { publish: "FILE|DIR", fetch: "ORG/NAME@VERSION", list: "ORG", withdraw: "ORG/NAME@VERSION" };
const SLUG_RULE = "lowercase letters, digits and inner hyphens, at most 63 characters";
function quoted(value: string): string { return JSON.stringify(value); }
function valid(check: (value: string) => unknown, value: string): boolean {
  try { check(value); return true; } catch { return false; }
}
function joined(names: readonly string[]): string {
  return names.length === 1 ? names[0]! : `${names.slice(0, -1).join(", ")} and ${names.at(-1)!}`;
}
/** The reason a `NAME` slug value is invalid, or undefined when it is valid. */
function slugProblem(label: string, value: string): string | undefined {
  return valid(identity, value) ? undefined : `${label} ${quoted(value)} is not a valid slug (${SLUG_RULE})`;
}
function versionProblem(value: string): string | undefined {
  return valid(version, value) ? undefined : `VERSION ${quoted(value)} is not an exact semantic version (for example 1.2.0)`;
}
/** The reason an `ORG/NAME@VERSION` reference is invalid (mirrors Rust `registry::reference_problem`). */
export function referenceProblem(value: string): string | undefined {
  const match = /^([^/]*)\/([^@]*)@(.*)$/su.exec(value);
  if (match === null) return `${quoted(value)} is not ORG/NAME@VERSION (for example acme/tool@1.2.0)`;
  return slugProblem("ORG", match[1]!) ?? slugProblem("NAME", match[2]!) ?? versionProblem(match[3]!);
}
function cursorProblem(value: string): string | undefined {
  const ok = value.length <= 210 && /^public:[a-z0-9-]+:[0-9A-Za-z.+-]+$/u.test(value);
  return ok ? undefined : `--cursor ${quoted(value)} is not a cursor from package list; pass the nextCursor value the previous page printed`;
}
/**
 * Parses `cli package <COMMAND> ...` (after a trailing --json is removed).
 * A missing or unknown command throws a bare INVOCATION_INVALID; any other
 * problem names its cause in `invalid` (mirrors Rust `registry::parse`).
 */
export function parsePackageCommand(args: readonly string[]): PackageCommand {
  const operation = args[0];
  if (operation === undefined || operation.length === 0) {
    throw withAction(invocationFailure("cli package needs a command: publish, fetch, list or withdraw"), localizeProduct("Run `prose cli package --help` to see the package commands."));
  }
  if (!(OPERATIONS as readonly string[]).includes(operation)) {
    throw withAction(invocationFailure(`unknown package command ${quoted(operation)}; the commands are publish, fetch, list and withdraw`), localizeProduct("Run `prose cli package --help` to see the package commands."));
  }
  const op = operation as PackageOperation;
  const input = args[1];
  const command: PackageCommand = { operation: op, input: input ?? "" };
  const invalid = problem(op, args.slice(1), command);
  if (invalid !== undefined) command.invalid = invalid;
  return command;
}
function problem(op: PackageOperation, rest: readonly string[], command: PackageCommand): string | undefined {
  const target = rest[0];
  if (target === undefined || target.length === 0 || target.startsWith("-")) return `package ${op} needs ${TARGET[op]}`;
  const allowed = OPTIONS[op];
  const values = new Map<string, string>();
  let index = 1;
  while (index < rest.length) {
    const raw = rest[index]!;
    if (raw === "--json") return "--json must be the last argument";
    if (!raw.startsWith("--")) return `unexpected argument ${quoted(raw)}; package ${op} takes one ${TARGET_NAME[op]}`;
    const equals = raw.indexOf("=");
    const name = equals < 0 ? raw : raw.slice(0, equals);
    if (!allowed.includes(name)) {
      return `package ${op} does not take ${name}; ${allowed.length === 0 ? "it takes no options" : `its options are ${joined(allowed)}`}`;
    }
    if (values.has(name)) return `${name} was given twice`;
    if (name === "--public") {
      if (equals >= 0) return "--public takes no value";
      values.set(name, "");
      index += 1;
      continue;
    }
    const value = equals < 0 ? rest[index + 1] : raw.slice(equals + 1);
    if (value === undefined || value.length === 0 || (equals < 0 && value.startsWith("-"))) return `${name} needs a value`;
    values.set(name, value);
    index += equals < 0 ? 2 : 1;
  }
  const required = op === "publish" ? [["--organization", "ORG"], ["--name", "NAME"], ["--version", "VERSION"]] : op === "fetch" ? [["--output-dir", "FRESH_DIR, a new directory to create"]] : [];
  for (const [name, what] of required) {
    if (!values.has(name!)) return `package ${op} needs ${name} ${what}`;
  }
  command.input = target;
  if (op === "publish") {
    command.organization = values.get("--organization")!;
    command.name = values.get("--name")!;
    command.version = values.get("--version")!;
    command.public = values.has("--public");
    return slugProblem("ORG", command.organization) ?? slugProblem("NAME", command.name) ?? versionProblem(command.version);
  }
  if (op === "fetch") {
    command.outputDir = values.get("--output-dir")!;
    const sha = values.get("--sha256");
    if (sha !== undefined) command.sha256 = sha;
    return referenceProblem(target) ?? (sha !== undefined && !valid(digest, sha) ? "--sha256 must be 64 lowercase hexadecimal digits" : undefined);
  }
  if (op === "list") {
    const cursor = values.get("--cursor");
    if (cursor !== undefined) command.cursor = cursor;
    return slugProblem("ORG", target) ?? (cursor === undefined ? undefined : cursorProblem(cursor));
  }
  return referenceProblem(target);
}
function withAction(error: RunnerFailure, action: string): RunnerFailure {
  return new RunnerFailure({ code: error.code, boundary: error.boundary, message: error.message, action, exitCode: error.exitCode, retryable: error.retryable, ...(error.details === undefined ? {} : { details: error.details }) });
}
