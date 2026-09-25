// Service operations: the embedded operation manifest drives parsing,
// help and confirmation. Feature modules file follow-ups instead of
// editing this module.
import manifestSource from "../../../../shared/service/operations.v1.json" with { type: "text" };
import helpSource from "../../../../shared/service/help.v1.json" with { type: "text" };
import guideSource from "../../../../shared/service/guide.v1.md" with { type: "text" };
import projectionSource from "../../../../shared/service/operations-public.v1.json" with { type: "text" };
import { statSync } from "node:fs";
import { failure, invocationFailure } from "../errors";
import { canonicalJson, jsonLine, quote } from "../output";
import type { OutputMode } from "../types";
import { RunnerFailure } from "../types";
import { argvText, shellQuote } from "./render";

/** Every help topic's text, by topic (`cli`, `cli run`, `cli run list`, ...). */
export function helpTopicTexts(): Record<string, string> {
  return helpTopics;
}

/** The operation manifest, byte for byte (`cli service operations`). */
export const MANIFEST_TEXT = manifestSource as unknown as string;
const HELP_TEXT = helpSource as unknown as string;
/** The agent guide `cli service guide` prints byte for byte (`shared/service/guide.v1.md`). */
export const GUIDE_TEXT = guideSource as unknown as string;

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export interface ManifestOption { name: string; value: string | null; description: string; repeatable: boolean; required: boolean; default?: string; choices?: string[] }
export interface ManifestArgument { name: string; description: string; required: boolean; variadic: boolean }
export interface ManifestRequest {
  interaction: string; method: string; path: string; auth: string; body: string | null; when: string;
  query?: Record<string, string>; headers?: Record<string, string>; response: string | null;
  catalogRoute: { method: string; path: string; auth: string };
}
export interface ManifestOperation {
  id: string; command: string[]; contract: string; feature: string; summary: string;
  interaction: string | null; interactions: string[]; arguments: ManifestArgument[]; options: ManifestOption[];
  requests: ManifestRequest[]; effect: string; mutation: boolean; confirm: boolean; preview: boolean;
  transport: string;
  output: { schema: string | null; stream: boolean; paged: boolean; records?: string };
  /** The --spec-file object of `job create|update|configure` (keys and their kinds). */
  spec?: { option: string; keys?: Record<string, unknown> };
}
export interface IntentInference {
  distance: { metric: string; shortWordLength: number; shortWordMax: number; max: number; optionShortWordLength?: number; optionShortWordMax?: number };
  nounSynonyms: Record<string, string[]>;
  verbSynonyms: Record<string, string[]>;
  optionAliases: Record<string, string[]>;
  /** Unit-changing option spellings, command phrases, secret file options. */
  optionConversions: Record<string, { option: string; multiplier: number; from: string; to: string }>;
  commandRewrites: Record<string, Rewrite>;
  /** Exact command spellings that run another command (hidden from help). */
  commandAliases: Record<string, string[]>;
  /** Options dropped with a reason. */
  optionRemovals: Record<string, { value: boolean; operations: string[]; why: string }>;
  secretFileOptions: Record<string, string>;
  languageCommands: string[];
}
/** A command phrase that is not a command path (`commandRewrites`). */
export interface Rewrite { command: string[]; append: string[]; argumentOption?: string; argumentCommand?: string[]; note?: string; why: string }

export interface Manifest {
  schema: string;
  grammar: { intentInference: IntentInference; globalOptions: string[]; commonOptions: ManifestOption[] };
  transportClasses: Record<string, Record<string, number | string>>;
  errorClassification: {
    bodyCodes: Record<string, string>;
    routeOverrides: Array<{ method: string; path: string; status: number; code: string }>;
    statuses: Record<string, string>;
    otherStatus: string;
    serviceMessage: { statuses: number[] };
    frozenContracts: string[];
  };
  operations: ManifestOperation[];
}

export const manifest: Manifest = JSON.parse(MANIFEST_TEXT) as Manifest;
// The parser works on command paths: the manifest `command` argv without
// its leading `cli` (`cli service operations` prints the argv as published).
for (const candidate of manifest.operations) {
  if (candidate.command[0] === "cli") candidate.command = candidate.command.slice(1);
}
const helpDocument = JSON.parse(HELP_TEXT) as { topics: Record<string, string>; views: Record<string, string>; capabilities: Json };
const helpTopics: Record<string, string> = helpDocument.topics;

/**
 * The human pages of `cli service operations` (the summary table) and
 * `cli service capabilities` (mirrors Rust `static_view`). Neither makes a
 * request.
 */
export function staticView(id: string): string | undefined {
  if (id === "service.operations") return helpDocument.views["cli service operations"];
  if (id === "service.capabilities") return helpDocument.views["cli service capabilities"];
  return undefined;
}

/**
 * The public projection of the manifest
 * (`shared/service/operations-public.v1.json`): the allowlist of members
 * `cli service operations` prints. Service routes, service error strings, the
 * service origin, the key format and this client's own settings stay internal
 * (mirrors Rust `public_manifest`).
 */
const PUBLIC_PROJECTION = JSON.parse(projectionSource as unknown as string) as { fields: Json };

/**
 * `value` reduced to the members `fields` lists: `true` copies a member whole,
 * an object applies the same allowlist to that member (to each element of an
 * array) (mirrors Rust `project_fields`).
 */
export function projectFields(value: Json, fields: Json): Json {
  if (fields === true) return value;
  if (Array.isArray(value)) return value.map((item) => projectFields(item, fields));
  if (value !== null && typeof value === "object" && fields !== null && typeof fields === "object" && !Array.isArray(fields)) {
    const projected: JsonObject = {};
    for (const [key, sub] of Object.entries(fields)) {
      if (Object.hasOwn(value, key)) projected[key] = projectFields(value[key]!, sub);
    }
    return projected;
  }
  return value;
}

/** The published manifest: the embedded one reduced to its public projection. */
export function publicManifest(): JsonObject {
  return projectFields(JSON.parse(MANIFEST_TEXT) as Json, PUBLIC_PROJECTION.fields) as JsonObject;
}

/**
 * The result of `cli service operations` (the manifest, as published) or
 * `cli service capabilities` in JSON modes: the envelope's `result`, like
 * every other command's (mirrors Rust `static_result`).
 */
export function staticResult(id: string): Json | undefined {
  if (id === "service.operations") return publicManifest();
  if (id === "service.capabilities") return helpDocument.capabilities;
  return undefined;
}

/**
 * A guide section id: the title lowercased, apostrophes dropped, every other
 * run of characters outside [a-z0-9] one hyphen, no leading or trailing hyphen
 * (`ci/render_service_help.py` `guide_slug`).
 */
export function guideSlug(title: string): string {
  return title.toLowerCase().replaceAll("'", "").replace(/[^a-z0-9]+/gu, "-").replace(/^-+|-+$/gu, "");
}

/**
 * `cli service guide --json`: `{sections: [{id, title, body}]}`.
 * A section starts at each line beginning `## `; its body is every following
 * line up to the next section, without leading or trailing LF. The text before
 * the first section (the `# ` title) is not a section. This is the split of
 * `ci/render_service_help.py` `guide_sections`, which generates the corpus
 * cases pinning both ports.
 */
export function guideResult(): JsonObject {
  const sections: Array<{ title: string; lines: string[] }> = [];
  for (const line of GUIDE_TEXT.split("\n")) {
    if (line.startsWith("## ")) sections.push({ title: line.slice(3), lines: [] });
    else sections.at(-1)?.lines.push(line);
  }
  return {
    sections: sections.map(({ title, lines }) => ({
      id: guideSlug(title), title, body: lines.join("\n").replace(/^\n+|\n+$/gu, ""),
    })),
  };
}

export function operation(id: string): ManifestOperation | undefined {
  return manifest.operations.find((candidate) => candidate.id === id);
}

function isServiceOperation(candidate: ManifestOperation): boolean { return candidate.contract === "service/1"; }

function serviceNouns(): Set<string> {
  return new Set(manifest.operations.filter(isServiceOperation).map((candidate) => candidate.command[0]!));
}

/** The parser intent-inference tables (`grammar.intentInference`). */
const inference: IntentInference = manifest.grammar.intentInference;

/** Runner commands outside the manifest, for the unknown-command listing and verb suggestions. */
export const RUNNER_COMMANDS: readonly (readonly string[])[] = [["doctor"], ["harness", "list"], ["harness", "use"], ["cleanup", "prime"], ["config", "explain"]];

/** Every command path after `cli`: manifest operations (account and service) and the runner commands. */
function commandPaths(): (readonly string[])[] {
  return [...manifest.operations.map((candidate) => candidate.command), ...RUNNER_COMMANDS];
}

/** Every first word after `cli`, sorted: the unknown-command listing. */
function allNouns(): string[] {
  return [...new Set(commandPaths().map((path) => path[0]!))].sort();
}

/** Whether `words` is a command group or a complete command path. */
function isCommandPrefix(words: readonly string[]): boolean {
  return words.length > 0 && commandPaths().some((path) => path.length >= words.length && words.every((word, index) => path[index] === word));
}

/**
 * How to fix a rejected service invocation (mirrors Rust
 * `service::Correction`). `{command}` in `action` is replaced by the rendered
 * command line, which is also returned as `details.suggestedArgv`.
 */
export type Correction =
  | { kind: "argv"; action: string; argv: string[] }
  | { kind: "command"; action: string; words: string[] }
  | { kind: "text"; action: string };

/** A service-noun invocation rejected before an operation was known. */
export interface ServiceInvalid {
  error: RunnerFailure;
  correction: Correction;
  output?: OutputMode;
  json: boolean;
  /** The complete original argv, set by the entry point: the JSON envelope's `operation` is the one it names. */
  argv?: readonly string[];
}

export type ServiceCommand =
  | { kind: "help"; text: string }
  | { kind: "invoke"; invocation: ServiceInvocation }
  | { kind: "invalid"; invalid: ServiceInvalid };

export interface ServiceInvocation {
  operation: string;
  arguments: Map<string, string[]>;
  options: Map<string, string[]>;
  flags: Set<string>;
  json: boolean;
  yes: boolean;
  preview: boolean;
  output?: OutputMode;
  argv: string[];
  error?: RunnerFailure;
  /** How to fix `error`. */
  correction?: Correction;
}

export function argument(invocation: ServiceInvocation, name: string): string | undefined { return invocation.arguments.get(name)?.[0]; }
export function option(invocation: ServiceInvocation, name: string): string | undefined { return invocation.options.get(name)?.[0]; }
export function optionValues(invocation: ServiceInvocation, name: string): string[] { return invocation.options.get(name) ?? []; }

/** Whether the tokens after `cli` belong to the service parser. */
export function claims(args: readonly string[]): boolean {
  const noun = args[0];
  if (noun === undefined) return false;
  if (noun === "org" && args[1] === "list") return false;
  return serviceNouns().has(noun);
}

/** Whether `noun verb` is a frozen service operation (`auth status`, `auth login`, `org list`, `package fetch`, ...). */
function isAccountVerb(noun: string, verb: string | undefined): boolean {
  return manifest.operations.some((candidate) => candidate.contract === "account/1" && candidate.command.length === 2 && candidate.command[0] === noun && candidate.command[1] === verb);
}

/**
 * Help for `cli --help`, `cli <noun> --help` of every manifest noun (including
 * the frozen service groups `auth` and `package`) and
 * `cli <noun> <verb> --help` of a frozen service operation. Every topic comes
 * from `help.v1.json`, so none prints the runner help. Runner
 * operations (`doctor`, `harness`, ...) keep it.
 */
export function groupHelp(args: readonly string[]): string | undefined {
  let topic: string | undefined;
  if (args.length === 1 && args[0] === "--help") topic = "cli";
  else if (args.length === 2 && args[1] === "--help" && Object.hasOwn(helpTopics, `cli ${args[0]}`)) topic = `cli ${args[0]}`;
  // Account operations keep their own parser, which never takes a value
  // starting with `-`, so `--help` or `-h` anywhere after a known verb asks for
  // that verb's topic.
  else if (args.length > 2 && (args.slice(2).includes("--help") || args.slice(2).includes("-h")) && isAccountVerb(args[0]!, args[1])) topic = `cli ${args[0]} ${args[1]}`;
  // A nested command group such as `org member` or `job contract`.
  else if (args.length > 2 && args.at(-1) === "--help" && serviceNouns().has(args[0]!) && verbsAfter(args.slice(0, -1)).length > 0) topic = `cli ${args.slice(0, -1).join(" ")}`;
  return topic === undefined ? undefined : helpTopics[topic];
}

/**
 * Optimal-string-alignment distance: Levenshtein plus adjacent transpositions
 * counted as one edit (`lsit` -> `list` is 1).
 */
function distance(left: string, right: string): number {
  const a = Array.from(left);
  const b = Array.from(right);
  const table = Array.from({ length: a.length + 1 }, (_, i) => Array.from({ length: b.length + 1 }, (_, j) => (i === 0 ? j : j === 0 ? i : 0)));
  for (let i = 1; i <= a.length; i += 1) {
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let best = Math.min(table[i - 1]![j]! + 1, table[i]![j - 1]! + 1, table[i - 1]![j - 1]! + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) best = Math.min(best, table[i - 2]![j - 2]! + 1);
      table[i]![j] = best;
    }
  }
  return table[a.length]![b.length]!;
}

/** The largest distance a suggestion may have for `word`. */
function maxDistance(word: string): number {
  const limits = inference.distance;
  return Array.from(word).length <= limits.shortWordLength ? limits.shortWordMax : limits.max;
}

/** The unique nearest candidate within the manifest distance limit, if any. */
export function didYouMean(word: string, candidates: Iterable<string>, limit: number = maxDistance(word)): string | undefined {
  const found = [...new Set(candidates)]
    .filter((candidate) => candidate !== word)
    .map((candidate) => [distance(word, candidate), candidate] as const)
    .filter(([measured]) => measured <= limit)
    .sort(([left], [right]) => left - right);
  if (found.length === 0) return undefined;
  return found.length === 1 || found[1]![0] > found[0]![0] ? found[0]![1] : undefined;
}

/**
 * Up to `count` candidates nearest to `word` (edit distance, then name), for
 * listing alternatives when no unique suggestion exists.
 */
export function nearest(word: string, candidates: readonly string[], count: number): string[] {
  return [...new Set(candidates)]
    .map((candidate) => [distance(word, candidate), candidate] as const)
    .sort(([leftDistance, left], [rightDistance, right]) => leftDistance - rightDistance || (left < right ? -1 : left > right ? 1 : 0))
    .slice(0, count)
    .map(([, candidate]) => candidate);
}

/** The first candidate of a synonym-table entry that `valid` accepts. */
function synonym(table: Record<string, string[]>, word: string, valid: (candidate: string) => boolean): string | undefined {
  return Object.hasOwn(table, word) ? table[word]!.find(valid) : undefined;
}

/** A verb for an unknown word in a group: a synonym first, then the nearest. */
function suggestVerb(word: string, verbs: readonly string[]): string | undefined {
  return synonym(inference.verbSynonyms, word, (candidate) => verbs.includes(candidate)) ?? didYouMean(word, verbs);
}

/**
 * The command words for an unknown word right after `cli`: a noun synonym
 * (`organization` -> `org`, `whoami` -> `auth status`) first, then the nearest noun.
 */
function suggestNounWords(noun: string): string[] | undefined {
  const listed = Object.hasOwn(inference.nounSynonyms, noun) ? inference.nounSynonyms[noun]! : undefined;
  if (listed !== undefined && isCommandPrefix(listed)) return [...listed];
  const found = didYouMean(noun, allNouns());
  return found === undefined ? undefined : [found];
}

/** The `optionAliases` target of an unknown option that is one of `names` and `fits` the given value. */
function optionAlias(name: string, names: readonly string[], fits: (candidate: string) => boolean = () => true): string | undefined {
  return synonym(inference.optionAliases, name, (candidate) => names.includes(candidate) && fits(candidate));
}

/**
 * The largest distance an option suggestion may have (mirrors Rust
 * `option_distance_limit`): a name of at most `optionShortWordLength`
 * characters without its leading dashes allows `optionShortWordMax`
 * (`--cron` is never `--json`); a longer one the ordinary limit.
 */
function optionDistanceLimit(name: string): number {
  const limits = inference.distance;
  const bare = Array.from(name.replace(/^-+/u, ""));
  const ordinary = maxDistance(name);
  return limits.optionShortWordLength !== undefined && limits.optionShortWordMax !== undefined && bare.length <= limits.optionShortWordLength
    ? Math.min(ordinary, limits.optionShortWordMax)
    : ordinary;
}

/**
 * A canonical option for an unknown one (mirrors Rust `suggest_option`): an
 * alias first, then the nearest within the option distance limit, which
 * counts only when its value type fits: a flag never takes the value the
 * unknown option was given (`hasValue`).
 */
function suggestOption(name: string, names: readonly string[], fits: (candidate: string) => boolean, hasValue: boolean, takesValue: (candidate: string) => boolean): string | undefined {
  const aliased = optionAlias(name, names, fits);
  if (aliased !== undefined) return aliased;
  const near = didYouMean(name, names, optionDistanceLimit(name));
  return near === undefined || (hasValue && !takesValue(near)) ? undefined : near;
}

/**
 * An exact command spelling that runs another command (`commandAliases`,
 * `cli run status` -> `cli run show`): the target operation and how many
 * typed words the alias spans (mirrors Rust `command_alias`). Aliases are
 * hidden from help and listings.
 */
function commandAlias(tokens: readonly string[]): [ManifestOperation, number] | undefined {
  for (const [phrase, target] of Object.entries(inference.commandAliases)) {
    const words = phrase.split(" ");
    if (tokens.length < words.length || !words.every((word, index) => tokens[index] === word)) continue;
    const operation = manifest.operations.find((candidate) => isServiceOperation(candidate) && candidate.command.length === target.length && candidate.command.every((word, index) => word === target[index]));
    if (operation !== undefined) return [operation, words.length];
  }
  return undefined;
}

/** An option dropped with a reason (`optionRemovals`) for operation `id`: `[why, takes a value]` (mirrors Rust `option_removal`). */
function optionRemoval(name: string, id: string): [string, boolean] | undefined {
  const entry = Object.hasOwn(inference.optionRemovals, name) ? inference.optionRemovals[name]! : undefined;
  if (entry === undefined || (entry.operations.length > 0 && !entry.operations.includes(id))) return undefined;
  return [entry.why, entry.value];
}

/** Whether a character is whitespace as Rust `char::is_whitespace` reads it. */
const WHITESPACE = /[\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u;

/**
 * A whole command line typed as one word (quoted, so the shell did not split
 * it): its words, when the first names `cli` or a command group (mirrors Rust
 * `unsplit_words`).
 */
function unsplitWords(token: string): string[] | undefined {
  if (!WHITESPACE.test(token)) return undefined;
  const words = token.split(new RegExp(`${WHITESPACE.source}+`, "u")).filter((word) => word.length > 0);
  const first = words[0];
  return first !== undefined && (first === "cli" || isCommandPrefix([first])) ? words : undefined;
}

/** A unit-changing spelling of an option (`optionConversions`), when its target is one of `names`. */
function optionConversion(name: string, names: readonly string[]): { option: string; multiplier: number; from: string; to: string } | undefined {
  const entry = Object.hasOwn(inference.optionConversions, name) ? inference.optionConversions[name]! : undefined;
  return entry !== undefined && names.includes(entry.option) ? entry : undefined;
}

/** `value` times `multiplier` when `value` is a positive whole number (at most 9 digits); nothing else is converted. */
function convertedValue(value: string, multiplier: number): string | undefined {
  if (!/^[0-9]{1,9}$/u.test(value)) return undefined;
  const number = Number(value);
  return number > 0 ? String(number * multiplier) : undefined;
}

/** The placeholder of a secret-reading option (`secretFileOptions`), such as `CODE` for `--code-file`. */
function secretPlaceholder(name: string): string | undefined {
  return Object.hasOwn(inference.secretFileOptions, name) ? inference.secretFileOptions[name] : undefined;
}

/** Whether `words` is a complete command path (an operation or a runner command). */
function isCommandPath(words: readonly string[]): boolean {
  return commandPaths().some((path) => path.length === words.length && path.every((word, index) => words[index] === word));
}

/**
 * Completes a corrected command prefix against the tokens that follow it
 * (mirrors Rust `complete_path`): a group gains the next typed
 * verb, the verb a misspelled next word means, or its only verb. Returns
 * the full path and how many following tokens it replaces, or undefined
 * when the group needs a verb the argv does not give and has several.
 */
function completePath(start: readonly string[], rest: readonly string[]): [string[], number] | undefined {
  const path = [...start];
  let consumed = 0;
  while (!isCommandPath(path)) {
    const verbs = verbsAfter(path);
    const typed = rest[consumed];
    const next = typed !== undefined && !typed.startsWith("-") ? typed : undefined;
    const verb = next === undefined ? undefined : verbs.find((candidate) => candidate === next) ?? suggestVerb(next, verbs);
    if (verb !== undefined) { path.push(verb); consumed += 1; }
    else if (next === undefined && verbs.length === 1) path.push(verbs[0]!);
    else return undefined;
  }
  return [path, consumed];
}

/**
 * When the tokens after `cli` ask for help without `--help` (bare `cli`,
 * `cli help [COMMAND...]`, a trailing `help` or `-h` after a command group or
 * path), the equivalent tokens ending in `--help`.
 */
export function helpRequest(args: readonly string[]): string[] | undefined {
  if (args.length === 0) return ["--help"];
  if (args[0] === "help" && args.slice(1).every((word) => !word.startsWith("-"))) return [...args.slice(1), "--help"];
  const last = args.at(-1);
  if ((last === "help" || last === "-h") && isCommandPrefix(args.slice(0, -1))) return [...args.slice(0, -1), "--help"];
  return undefined;
}

function verbsAfter(prefix: readonly string[]): string[] {
  const verbs = new Set<string>();
  for (const words of commandPaths()) {
    if (words.length > prefix.length && prefix.every((word, index) => words[index] === word)) verbs.add(words[prefix.length]!);
  }
  return [...verbs].sort();
}

/** A rejected invocation and how to fix it. */
class Rejected {
  constructor(readonly error: RunnerFailure, readonly correction: Correction) {}
}

function reject(reason: string, correction: Correction): Rejected {
  return new Rejected(invocationFailure(reason), correction);
}

/** Where the parsed tokens sit inside the original argv (they are a suffix of it). */
class Positions {
  readonly offset: number | undefined;
  constructor(readonly original: readonly string[], tokens: readonly string[]) {
    const offset = original.length - tokens.length;
    this.offset = offset >= 0 && tokens.every((token, index) => original[offset + index] === token) ? offset : undefined;
  }

  /** The original argv with `length` tokens at `at` replaced by `replacement`. */
  splice(at: number, length: number, replacement: readonly string[]): string[] | undefined {
    if (this.offset === undefined) return undefined;
    const start = this.offset + at;
    const end = start + length;
    if (end > this.original.length) return undefined;
    return [...this.original.slice(0, start), ...replacement, ...this.original.slice(end)];
  }
}

/** `prose ... cli <command> --help`, the fallback when no exact fix exists. */
function helpWords(command: readonly string[]): string[] {
  return [...command, "--help"];
}

/** The slug a program file suggests (`hello.prose.md` -> `hello`), when it is a valid slug (mirrors Rust `slug_from_file`). */
function slugFromFile(file: string): string | undefined {
  const base = file.split("/").pop() ?? "";
  const stem = base.endsWith(".prose.md") ? base.slice(0, -".prose.md".length) : base.endsWith(".md") ? base.slice(0, -".md".length) : undefined;
  return stem !== undefined && /^[a-z0-9][a-z0-9-]{0,63}$/u.test(stem) ? stem : undefined;
}

function argvOrHelp(action: string, argv: string[] | undefined, command: readonly string[], fallback: string): Correction {
  return argv !== undefined ? { kind: "argv", action, argv } : { kind: "command", action: fallback, words: helpWords(command) };
}

/** The listing that shows valid values for a missing positional argument. */
function lister(command: readonly string[], argument: string, invocation: ServiceInvocation): [string, string[]] | undefined {
  switch (argument) {
    case "RUN_ID": return ["runs", ["run", "list"]];
    case "JOB_ID": return ["jobs", ["job", "list"]];
    case "ORG": return ["organizations", ["org", "list"]];
    case "NAME": return command[0] === "example" ? ["examples", ["example", "list"]] : undefined;
    case "SLUG": return command[0] === "program" || command[0] === "result" ? ["your programs", ["program", "list"]] : undefined;
    case "OWNER/SLUG": case "OWNER/SLUG[@REV]": case "OWNER/SLUG@REV": return ["programs", ["program", "list"]];
    case "PUBLICATION_ID": {
      const program = invocation.arguments.get("OWNER/SLUG")?.[0];
      return program === undefined ? undefined : ["publications", ["result", "list", program]];
    }
    case "ACCOUNT_ID": {
      const org = invocation.arguments.get("ORG")?.[0];
      return org === undefined ? undefined : ["members", ["org", "member", "list", org]];
    }
    default: return undefined;
  }
}

function unknownCommand(tokens: readonly string[], positions: Positions): Rejected {
  return settle(unknownCommandUnsettled(tokens, positions));
}

/**
 * The correction for a corrected command path (mirrors Rust
 * `path_correction`): the argv with `length` tokens at `at` replaced by the
 * completed `path`, the help of a group that still needs one of several
 * verbs, or the help of a bare command that declares only optional
 * arguments. Returns the words the reason shows and the correction.
 */
function pathCorrection(path: readonly string[], at: number, length: number, tokens: readonly string[], positions: Positions): [string, Correction] {
  const rest = tokens.slice(at + length);
  const completed = completePath(path, rest);
  if (completed === undefined) {
    const spelled = path.join(" ");
    return [spelled, { kind: "command", action: `Use \`cli ${spelled}\` with one of its commands; \`{command}\` describes them`, words: helpWords(path) }];
  }
  const [full, consumed] = completed;
  const typed = full.slice(full.length - consumed).filter((word, index) => word !== rest[index]);
  const shown = [...full.slice(0, full.length - consumed), ...typed].join(" ");
  const spelled = full.join(" ");
  const bare = rest.length === consumed;
  const target = manifest.operations.find((candidate) => candidate.command.length === full.length && candidate.command.every((word, index) => full[index] === word));
  const optionalOnly = target !== undefined && target.arguments.length > 0 && target.arguments.every((item) => !item.required);
  if (bare && optionalOnly) {
    return [shown, argvOrHelp(`Use \`cli ${spelled}\`; \`{command}\` shows what it needs`, positions.splice(at, length + consumed, [...full, "--help"]), full, "`{command}` shows the syntax")];
  }
  return [shown, argvOrHelp(`Use \`cli ${spelled}\`: \`{command}\``, positions.splice(at, length + consumed, full), full, "`{command}` shows the syntax")];
}

/** What an unknown leading phrase means: the rejection, how many words it spans and a rewrite's explanation. */
interface Meaning { rejected: Rejected; typed: number; why?: string }

/**
 * A `commandRewrites` phrase of `length` words at the start of `tokens`
 * (mirrors Rust `phrase_meaning`). A one-word phrase followed by a verb of
 * its target's group names that command instead (`cli schedule list` ->
 * `cli job list`).
 */
function phraseMeaning(tokens: readonly string[], positions: Positions, length: number): Meaning | undefined {
  if (tokens.length < length) return undefined;
  const phrase = tokens.slice(0, length).join(" ");
  if (!Object.hasOwn(inference.commandRewrites, phrase)) return undefined;
  const rewrite = inference.commandRewrites[phrase]!;
  const next = tokens[length];
  const group = rewrite.command[0]!;
  if (length === 1 && next !== undefined && verbsAfter([group]).includes(next)) {
    const [shown, correction] = pathCorrection([group, next], 0, 2, tokens, positions);
    return { rejected: reject(`unknown command \`cli ${phrase} ${next}\`; did you mean \`cli ${shown}\`?`, correction), typed: 2 };
  }
  const [spelled, why, correction] = rewriteCorrection(rewrite, length - 1, tokens, positions);
  return { rejected: reject(`unknown command \`cli ${phrase}\`; did you mean \`${spelled}\`? ${why}`, correction), typed: length, why };
}

/**
 * A verb typed before its group, or before a noun synonym of it (mirrors Rust
 * `verb_first`): `cli list jobs` -> `cli job list`, `cli delete program SLUG`
 * -> `cli program delete SLUG`. The verb is exact or a `verbSynonyms` entry;
 * never a guess by distance.
 */
function verbFirst(tokens: readonly string[], positions: Positions): Meaning | undefined {
  const [noun, typed] = tokens;
  if (noun === undefined || typed === undefined || typed.startsWith("-")) return undefined;
  const group = isCommandPrefix([typed]) ? typed : Object.hasOwn(inference.nounSynonyms, typed) ? inference.nounSynonyms[typed]![0] : undefined;
  if (group === undefined || isCommandPath([group]) || !isCommandPrefix([group])) return undefined;
  // A noun synonym of the same group names its own command (`credits topup`).
  if (Object.hasOwn(inference.nounSynonyms, noun) && inference.nounSynonyms[noun]![0] === group) return undefined;
  const verbs = verbsAfter([group]);
  const verb = verbs.find((candidate) => candidate === noun) ?? synonym(inference.verbSynonyms, noun, (candidate) => verbs.includes(candidate));
  if (verb === undefined) return undefined;
  const [shown, correction] = pathCorrection([group, verb], 0, 2, tokens, positions);
  return { rejected: reject(`unknown command \`cli ${noun} ${typed}\`; the command word comes first: did you mean \`cli ${shown}\`?`, correction), typed: 2 };
}

/**
 * The command a noun synonym and the word after it name (mirrors Rust
 * `synonym_path`), and how many typed words that spans: `runs list` repeats
 * the synonym's verb, and a sibling verb replaces it (`credits topup` ->
 * `wallet topup`).
 */
function synonymPath(words: readonly string[], next: string | undefined): [string[], number] {
  if (next === undefined || words.length < 2) return [[...words], 1];
  if (words.at(-1) === next) return [[...words], 2];
  const parent = words.slice(0, -1);
  return isCommandPath(words) && verbsAfter(parent).includes(next) ? [[...parent, next], 2] : [[...words], 1];
}

/**
 * An unknown first word after `cli` (mirrors Rust `unknown_noun`): a whole
 * command line typed as one word, a phrase that means another command, a
 * noun synonym (a plural names its listing), the nearest noun, a verb typed
 * before its noun (`submit run`) or a verb of exactly one group (`watch
 * RUN_ID`).
 */
function unknownNoun(noun: string, tokens: readonly string[], positions: Positions): Rejected {
  const listing = allNouns().join(", ");
  const unsplit = unsplitWords(noun);
  if (unsplit !== undefined) {
    const split = unsplit[0] === "cli" ? unsplit.slice(1) : unsplit;
    return reject(`the word ${quote(noun)} holds a whole command line; the argv looks unsplit (quoted, so the shell passed it as one word): did you mean \`cli ${split.join(" ")}\`?`,
      argvOrHelp("Pass each word separately: `{command}`", positions.splice(0, 1, split), [], "`{command}` lists the commands"));
  }
  // A phrase that means another command (`cli environment list`), a verb
  // typed before its noun (`cli list jobs`), then a one-word phrase
  // (`cli stop RUN_ID`).
  const meant = phraseMeaning(tokens, positions, 2) ?? verbFirst(tokens, positions) ?? phraseMeaning(tokens, positions, 1);
  if (meant !== undefined) return meant.rejected;
  const words = suggestNounWords(noun);
  if (words !== undefined) {
    // `cli runs list`: the synonym already names the verb the user typed;
    // `cli credits topup`: a sibling verb replaces the synonym's.
    const [path, typed] = synonymPath(words, tokens[1]);
    const [shown, correction] = pathCorrection(path, 0, typed, tokens, positions);
    return reject(`unknown command \`cli ${noun}\`; did you mean \`cli ${shown}\`? Commands: ${listing}`, correction);
  }
  // `cli submit run FILE`: a verb typed before its noun.
  const group = tokens[1];
  if (group !== undefined && !group.startsWith("-") && isCommandPrefix([group]) && !isCommandPath([group])) {
    const verbs = verbsAfter([group]);
    const verb = verbs.find((candidate) => candidate === noun) ?? suggestVerb(noun, verbs);
    if (verb !== undefined) {
      const [shown, correction] = pathCorrection([group, verb], 0, 2, tokens, positions);
      return reject(`unknown command \`cli ${noun} ${group}\`; the command word comes first: did you mean \`cli ${shown}\`?`, correction);
    }
  }
  // `cli watch RUN_ID`: a verb of exactly one command group.
  const groups = [...new Set(commandPaths().filter((path) => path.length === 2 && path[1] === noun).map((path) => path[0]!))];
  if (groups.length === 1) {
    const [shown, correction] = pathCorrection([groups[0]!, noun], 0, 1, tokens, positions);
    return reject(`unknown command \`cli ${noun}\`; did you mean \`cli ${shown}\`? Commands: ${listing}`, correction);
  }
  return reject(`unknown command \`cli ${noun}\`. Commands: ${listing}`, { kind: "command", action: "List the service commands with `{command}`", words: ["--help"] });
}

function unknownCommandUnsettled(tokens: readonly string[], positions: Positions): Rejected {
  const prefix: string[] = [];
  for (const word of tokens) {
    if (verbsAfter([...prefix, word]).length === 0) break;
    prefix.push(word);
  }
  // A complete one-word runner command whose own parser rejected the rest (`cli doctor extra`).
  if (prefix.length === 0 && commandPaths().some((path) => path.length === 1 && path[0] === tokens[0])) {
    return reject(`missing or invalid arguments for \`cli ${tokens[0]}\``, { kind: "command", action: "`{command}` shows the syntax", words: helpWords([tokens[0]!]) });
  }
  if (prefix.length === 0) return unknownNoun(tokens[0] ?? "", tokens, positions);
  const group = prefix.join(" ");
  const verbs = verbsAfter(prefix);
  const groupHelp: Correction = { kind: "command", action: `Choose one of the \`cli ${group}\` commands; \`{command}\` describes them`, words: helpWords(prefix) };
  const typed = tokens[prefix.length];
  const word = typed !== undefined && !typed.startsWith("-") ? typed : undefined;
  if (word === undefined) {
    const reason = `\`cli ${group}\` needs a command: ${verbs.join(", ")}`;
    // A group with one verb completes to it (`cli model` -> `cli model list`).
    return verbs.length === 1 ? reject(reason, pathCorrection(prefix, 0, prefix.length, tokens, positions)[1]) : reject(reason, groupHelp);
  }
  // A complete runner or service command whose own parser rejected the rest
  // (`cli cleanup prime` without a handle).
  if (verbs.includes(word)) {
    const path = [...prefix, word];
    return reject(`missing or invalid arguments for \`cli ${path.join(" ")}\``, { kind: "command", action: "`{command}` shows the syntax", words: helpWords(path) });
  }
  // A phrase that means another command (`cli run result RUN_ID`).
  const phrase = `${group} ${word}`;
  if (Object.hasOwn(inference.commandRewrites, phrase)) {
    return rewriteRejection(phrase, inference.commandRewrites[phrase]!, prefix.length, tokens, positions);
  }
  const verb = suggestVerb(word, verbs);
  const hint = verb === undefined ? "." : `; did you mean \`cli ${group} ${verb}\`?`;
  const correction = verb === undefined ? groupHelp : pathCorrection([...prefix, verb], 0, prefix.length + 1, tokens, positions)[1];
  return reject(`unknown command \`cli ${group} ${word}\`${hint} Commands: ${verbs.join(", ")}`, correction);
}

/**
 * `commandRewrites` (mirrors Rust `rewrite_rejection`):
 * `cli run result RUN_ID` is `cli run show RUN_ID --file outputs/result.json`,
 * and `cli program run SLUG` is `cli run submit --from SLUG`
 * (`argumentOption`). `typed` is the phrase as typed; its last word is token
 * `at`. Without the target's required arguments the suggestion is the
 * target's help.
 */
function rewriteRejection(typed: string, rewrite: Rewrite, at: number, tokens: readonly string[], positions: Positions): Rejected {
  const [spelled, why, correction] = rewriteCorrection(rewrite, at, tokens, positions);
  return reject(`unknown command \`cli ${typed}\`; did you mean \`${spelled}\`? ${why}`, correction);
}

/**
 * The correction of a `commandRewrites` phrase whose last word is token `at`
 * (mirrors Rust `rewrite_correction`): the spelled target, the reason's
 * explanation and the correction. A typed positional argument selects
 * `argumentCommand` when there is one (`cli why RUN_ID` -> `cli run show
 * RUN_ID`); a target that appends `--help` drops the rest of the line;
 * `note` starts the Action.
 */
function rewriteCorrection(chosen: Rewrite, at: number, tokens: readonly string[], positions: Positions): [string, string, Correction] {
  let rewrite = chosen;
  if (rewrite.argumentCommand !== undefined) {
    const alternative = manifest.operations.find((candidate) => candidate.command.join(" ") === rewrite.argumentCommand!.join(" "));
    if (firstPositional(alternative, tokens.slice(at + 1)) !== undefined) {
      rewrite = { command: [...rewrite.argumentCommand], append: [], why: rewrite.why, ...(rewrite.note === undefined ? {} : { note: rewrite.note }) };
    }
  }
  const target = rewrite.command.join(" ");
  const operation = manifest.operations.find((candidate) => candidate.command.join(" ") === target);
  const required = (operation?.arguments ?? []).filter((item) => item.required);
  const placeholders = rewrite.append.includes("--help") ? "" : required.map((item) => ` ${item.name}`).join("");
  const optionValue = rewrite.argumentOption === undefined ? undefined : operation?.options.find((item) => item.name === rewrite.argumentOption)?.value ?? "VALUE";
  const option = rewrite.argumentOption === undefined ? "" : ` ${rewrite.argumentOption} ${optionValue ?? "VALUE"}`;
  const appended = rewrite.append.map((word) => ` ${word}`).join("");
  const spelled = `cli ${target}${placeholders}${option}${appended}`;
  const rest = tokens.slice(at + 1);
  const needed = required.length + (rewrite.argumentOption === undefined ? 0 : 1);
  // A bare target whose arguments are all optional still needs one (`cli
  // run quote` needs a program): its help.
  const optionalOnly = operation !== undefined && operation.arguments.length > 0 && operation.arguments.every((item) => !item.required);
  const given = positionalCount(operation, rest);
  const help = rewrite.append.includes("--help");
  let corrected = (given >= needed && !(optionalOnly && given === 0 && rewrite.argumentOption === undefined)) || help
    ? positions.splice(0, help ? tokens.length : at + 1, help ? [...rewrite.command, ...rewrite.append] : rewrite.command)
    : undefined;
  if (corrected !== undefined && !help) {
    const start = corrected.length - rest.length;
    if (rewrite.argumentOption !== undefined) {
      // The first positional becomes the option's value.
      const first = firstPositional(operation, corrected.slice(start));
      if (first !== undefined) corrected.splice(start + first, 0, rewrite.argumentOption);
    }
    const end = corrected.includes("--") ? corrected.indexOf("--") : corrected.length;
    corrected = [...corrected.slice(0, end), ...rewrite.append, ...corrected.slice(end)];
  }
  const note = rewrite.note === undefined ? "" : `${rewrite.note}. `;
  const correction: Correction = corrected === undefined
    ? { kind: "command", action: `${note}Use \`${spelled}\`; \`{command}\` shows its arguments`, words: helpWords(rewrite.command) }
    : { kind: "argv", action: `${note}Use \`cli ${target}\`: \`{command}\``, argv: corrected };
  return [spelled, rewrite.why, correction];
}

/** The index of the first positional argument in `rest` for `operation`, skipping options and their values (mirrors Rust `first_positional`). */
function firstPositional(operation: ManifestOperation | undefined, rest: readonly string[]): number | undefined {
  const takesValue = (name: string): boolean => name === "--output" || (operation?.options ?? []).some((item) => item.name === name && item.value !== null);
  let index = 0;
  while (index < rest.length) {
    const token = rest[index]!;
    if (token === "--") return index + 1 < rest.length ? index + 1 : undefined;
    if (token.startsWith("-") && token !== "-") {
      index += !token.includes("=") && takesValue(token) ? 2 : 1;
      continue;
    }
    return index;
  }
  return undefined;
}

/** How many positional arguments `rest` gives an operation (mirrors Rust `positional_count`). */
function positionalCount(operation: ManifestOperation | undefined, rest: readonly string[]): number {
  const takesValue = (name: string): boolean => name === "--output"
    || (operation?.options ?? []).some((item) => item.name === name && item.value !== null);
  let count = 0;
  let index = 0;
  while (index < rest.length) {
    const token = rest[index]!;
    index += 1;
    if (token === "--") { count += rest.length - index; break; }
    if (token.startsWith("-") && token !== "-") {
      if (!token.includes("=") && takesValue(token)) index += 1;
      continue;
    }
    count += 1;
  }
  return count;
}

/**
 * (mirrors Rust `settle`): a parser correction is complete.
 * When a corrected argv names a service command the grammar would still
 * reject, the correction becomes the one that second rejection carries.
 */
function settle(rejected: Rejected): Rejected {
  // Each step may leave another fix (moving an option, then removing its
  // stray value): settle until the suggestion parses, at most four steps
  // (mirrors Rust `settle`).
  let current = rejected;
  for (let step = 0; step < 4; step += 1) {
    if (current.correction.kind !== "argv") break;
    const before = current.correction.argv;
    current = settleOnce(current);
    if (current.correction.kind !== "argv" || argvEqual(current.correction.argv, before)) break;
  }
  return current;
}

function argvEqual(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((word, index) => word === right[index]);
}

/** One step of `settle`. */
function settleOnce(rejected: Rejected): Rejected {
  const first = rejected.correction;
  if (first.kind !== "argv") return rejected;
  const cli = first.argv.indexOf("cli");
  if (cli < 0) return rejected;
  const args = first.argv.slice(cli + 1);
  if (!claims(args)) return rejected;
  const second = parseWith(args, first.argv, false);
  // A suggestion that asks for help is the command's help alone: `-h`
  // anywhere becomes `--help` after the command path, and the rest of the
  // line is dropped.
  if (second.kind === "help") {
    const path: string[] = [];
    for (const word of args) {
      if (word.startsWith("-") || !isCommandPrefix([...path, word])) break;
      path.push(word);
    }
    const argv = [...first.argv.slice(0, cli + 1), ...path, "--help"];
    return argvEqual(argv, first.argv) ? rejected : new Rejected(rejected.error, { kind: "argv", action: first.action, argv });
  }
  const next = second.kind === "invoke" && second.invocation.error !== undefined ? second.invocation.correction
    : second.kind === "invalid" ? second.invalid.correction : undefined;
  if (next === undefined) return rejected;
  const lead = first.action.endsWith(": `{command}`") ? first.action.slice(0, -": `{command}`".length) : first.action;
  const then = (text: string): string => `${lead}, then ${text.slice(0, 1).toLowerCase()}${text.slice(1)}`;
  const correction: Correction = next.kind === "argv" ? { kind: "argv", action: then(next.action), argv: next.argv }
    : next.kind === "command" ? { kind: "command", action: then(next.action), words: next.words }
    : { kind: "text", action: then(next.action) };
  return new Rejected(rejected.error, correction);
}

/** A service-noun invocation that names no operation. */
function invalidCommand(rejected: Rejected, args: readonly string[]): ServiceCommand {
  return { kind: "invalid", invalid: { ...scanGlobals(args), error: rejected.error, correction: rejected.correction } };
}

/**
 * Tolerant scan of the tokens after `cli` for the options that choose the
 * output mode (valid values only, first occurrence wins, nothing after
 * `--`). Used only to render an error that stopped parsing.
 */
function scanGlobals(tokens: readonly string[]): { output?: OutputMode; json: boolean } {
  const found: { output?: OutputMode; json: boolean } = { json: false };
  let index = 0;
  while (index < tokens.length) {
    const token = tokens[index]!;
    index += 1;
    if (token === "--") break;
    const equals = token.indexOf("=");
    const inline = token.startsWith("--") && equals >= 0 ? token.slice(equals + 1) : undefined;
    const name = inline === undefined ? token : token.slice(0, equals);
    if (name === "--json" && inline === undefined) { found.json = true; continue; }
    // A rejected spelling of --output (`--format json`, `-o json`) still
    // chooses how the error is printed.
    if (name !== "--output" && !(Object.hasOwn(inference.optionAliases, name) && inference.optionAliases[name]!.includes("--output"))) continue;
    let value = inline;
    if (value === undefined) {
      value = tokens[index];
      if (value === undefined) break;
      index += 1;
    }
    if (found.output === undefined && (value === "human" || value === "json" || value === "jsonl")) found.output = value;
  }
  return found;
}

/**
 * `cli --output json run list`: a runner-global option right
 * after `cli` belongs before it. `undefined` when the tokens are not a
 * service invocation.
 */
export function misplacedGlobals(args: readonly string[], original: readonly string[]): ServiceCommand | undefined {
  let index = 0;
  const moved: string[] = [];
  const names: string[] = [];
  while (index < args.length) {
    const token = args[index]!;
    const name = token.includes("=") ? token.slice(0, token.indexOf("=")) : token;
    if (name === "--output") {
      const width = token.includes("=") ? 1 : 2;
      if (index + width > args.length) return undefined;
      moved.push(...args.slice(index, index + width));
      names.push(name);
      index += width;
    } else if ((name === "--no-color" || name === "--verbose") && !token.includes("=")) {
      moved.push(token);
      names.push(name);
      index += 1;
    } else break;
  }
  if (names.length === 0 || !claims(args.slice(index))) return undefined;
  const positions = new Positions(original, args);
  let argv: string[] | undefined;
  if (positions.offset !== undefined && positions.offset >= 1 && original[positions.offset - 1] === "cli") {
    argv = [...original.slice(0, positions.offset - 1), ...moved, "cli", ...args.slice(index)];
  }
  const listed = [...new Set(names)].sort().join(", ");
  const rejected = reject(
    `the global option ${listed} must come before \`cli\`, not after it`,
    argv !== undefined
      ? { kind: "argv", action: "Place global options before cli: `{command}`", argv }
      : { kind: "text", action: "Place global options before cli and retry." },
  );
  return invalidCommand(rejected, args);
}

/**
 * Any tokens after `cli` that no parser claimed: an unknown or misspelled
 * command, or a runner or service group with an unknown verb. Rendered by
 * the service renderer so both ports print the same bytes.
 */
export function unknown(args: readonly string[], original: readonly string[]): ServiceCommand {
  const positions = new Positions(original, args);
  return invalidCommand(unknownCommand(args, positions), args);
}

/** ASCII-only lowercase, as Rust `to_ascii_lowercase`. */
function asciiLower(value: string): string {
  return value.replace(/[A-Z]/gu, (letter) => letter.toLowerCase());
}

/** Whether `tail` holds `cli` followed by a manifest command noun (a service or account command, not a runner operation such as `doctor`). */
function manifestCommandFollows(tail: readonly string[]): boolean {
  const at = tail.indexOf("cli");
  const noun = at < 0 ? undefined : tail[at + 1];
  return noun !== undefined && manifest.operations.some((candidate) => candidate.command[0] === noun);
}

/**
 * An `--output` value before `cli` that is not an output mode (`--output yaml
 * cli run list`; mirrors Rust `service::invalid_global_output`).
 * The error suggests the nearest mode, or `json` when none is near.
 * `undefined` unless `cli` and a manifest command follow: runner operations
 * (`cli doctor`) keep the runner renderer.
 */
export function invalidGlobalOutput(args: readonly string[], index: number): ServiceCommand | undefined {
  const token = args[index];
  if (token === undefined) return undefined;
  let value: string | undefined;
  let valueAt: number;
  let inline: boolean;
  if (token.startsWith("--output=")) {
    value = token.slice("--output=".length);
    valueAt = index;
    inline = true;
  } else if (token === "--output") {
    value = args[index + 1];
    valueAt = index + 1;
    inline = false;
  } else return undefined;
  if (value === undefined || value.length === 0 || value === "human" || value === "json" || value === "jsonl" || !manifestCommandFollows(args.slice(valueAt + 1))) return undefined;
  const modes = ["human", "json", "jsonl"];
  const lower = asciiLower(value);
  const near = modes.find((mode) => mode === lower) ?? didYouMean(lower, modes);
  const mode = near ?? "json";
  const argv = [...args];
  argv[valueAt] = inline ? `--output=${mode}` : mode;
  const expected = `invalid output mode ${quote(value)}; expected human, json, or jsonl`;
  const rejected = near !== undefined
    ? reject(`${expected}; did you mean \`${near}\`?`, { kind: "argv", action: `Use --output ${near}: \`{command}\``, argv })
    : reject(expected, { kind: "argv", action: "Use --output json for one JSON document (jsonl and human are the other modes): `{command}`", argv });
  return invalidCommand(rejected, args.slice(valueAt + 1));
}

/**
 * The canonical name of a command-local option spelled before `cli`, and
 * whether it takes a value: a common flag (`--json`, `--yes`, `--preview`),
 * any operation option, or an `optionAliases` spelling of one (`-j`, `-y`).
 * `undefined` for a runner-global option, an alias of one, `--help` and
 * unknown words.
 */
function localOption(name: string): [string, boolean] | undefined {
  const exact = manifest.operations.some((candidate) => candidate.options.some((item) => item.name === name));
  const canonical = (!exact && Object.hasOwn(inference.optionAliases, name) ? inference.optionAliases[name]![0] : undefined) ?? name;
  if (manifest.grammar.globalOptions.includes(canonical) || canonical === "--help") return undefined;
  const found = [...manifest.grammar.commonOptions, ...manifest.operations.flatMap((candidate) => candidate.options)]
    .find((option) => option.name === canonical);
  return found === undefined ? undefined : [found.name, found.value !== null];
}

/**
 * Command-local options before `cli` (`prose --json cli run list`; mirrors Rust `service::misplaced_local_options`). The argv is never
 * forwarded to the language: `suggestedArgv` moves the options (by their
 * canonical names) after the command path and keeps real runner globals
 * where they were.
 */
/** The removed service-selection option, spelled in two parts so the public build carries no trace of it (identical in both ports). */
const REMOVED_OPTION = ["--service-", "environment"].join("");

/**
 * An option neither the runner nor any service command knows, spelled before
 * `cli` and a manifest command (`--colour always cli service
 * status`): INVOCATION_INVALID naming the option, never language input. The
 * suggested argv drops it and the one value word between it and `cli`
 * (mirrors Rust `unknown_option_before_cli`).
 */
export function unknownOptionBeforeCli(args: readonly string[], index: number, globalKind: (name: string) => boolean | undefined): ServiceCommand | undefined {
  const optionName = (token: string): string => {
    const equals = token.indexOf("=");
    return token.startsWith("--") && equals >= 0 ? token.slice(0, equals) : token;
  };
  // The width of a known option token (with its value), or undefined.
  const known = (token: string): number | undefined => {
    const name = optionName(token);
    const inline = name !== token;
    const takesValue = globalKind(name)
      ?? (Object.hasOwn(inference.optionConversions, name) ? true : undefined)
      ?? localOption(name)?.[1];
    return takesValue === undefined ? undefined : takesValue && !inline ? 2 : 1;
  };
  const isOption = (token: string | undefined): token is string => token !== undefined && token !== "--" && token.startsWith("-");
  let at = index;
  let unknown: number | undefined;
  while (unknown === undefined) {
    const token = args[at];
    if (!isOption(token)) return undefined;
    const width = known(token);
    if (width === undefined) unknown = at;
    else at += width;
  }
  const token = args[unknown]!;
  const name = optionName(token);
  // The unknown option may take one value word.
  let end = unknown + 1;
  const value = args[end];
  if (!token.includes("=") && value !== undefined && value !== "cli" && !isOption(value)) end += 1;
  let cliAt = end;
  while (cliAt < args.length && args[cliAt] !== "cli") {
    const next = args[cliAt];
    if (!isOption(next)) return undefined;
    const width = known(next);
    if (width === undefined) return undefined;
    cliAt += width;
  }
  if (cliAt >= args.length || !manifestCommandFollows(args.slice(cliAt))) return undefined;
  const argv = [...args.slice(0, unknown), ...args.slice(end)];
  const rejected = name === REMOVED_OPTION
    ? reject(`unknown option ${name} before \`cli\`; the option was removed`, { kind: "argv", action: `The ${name} option was removed; public builds always use the OpenProse production service. Run the command without it: \`{command}\``, argv })
    : reject(`unknown option ${name} before \`cli\``, { kind: "argv", action: "Remove the unknown option: `{command}`", argv });
  return invalidCommand(rejected, args.slice(end));
}

export function misplacedLocalOptions(args: readonly string[], index: number, globalKind: (name: string) => boolean | undefined): ServiceCommand | undefined {
  const kept = args.slice(0, index);
  const moved: string[] = [];
  const named: string[] = [];
  const canonicalNames: string[] = [];
  let at = index;
  while (at < args.length) {
    const token = args[at]!;
    if (token === "cli" || token === "--" || !token.startsWith("-")) break;
    const equals = token.indexOf("=");
    const inline = token.startsWith("--") && equals >= 0 ? token.slice(equals + 1) : undefined;
    const name = inline === undefined ? token : token.slice(0, equals);
    const global = globalKind(name);
    if (global !== undefined) {
      const width = global && inline === undefined ? 2 : 1;
      if (at + width > args.length) return undefined;
      kept.push(...args.slice(at, at + width));
      at += width;
      continue;
    }
    // A unit-changing spelling (`--amount 5`) moves as its
    // target with the converted value; a value that is not converted moves
    // as typed, and the settled correction states the unit.
    if (Object.hasOwn(inference.optionConversions, name)) {
      const conversion = inference.optionConversions[name]!;
      const value = inline ?? args[at + 1];
      if (value === undefined) return undefined;
      at += inline === undefined ? 2 : 1;
      const converted = convertedValue(value, conversion.multiplier);
      if (converted !== undefined) {
        moved.push(conversion.option, converted);
        named.push(`\`${name} ${value}\` (${conversion.option} ${converted}, ${conversion.from} converted to ${conversion.to})`);
        canonicalNames.push(conversion.option);
      } else {
        moved.push(name, value);
        named.push(`\`${name}\``);
        canonicalNames.push(name);
      }
      continue;
    }
    const local = localOption(name);
    if (local === undefined) return undefined;
    const [canonical, takesValue] = local;
    if (takesValue && inline !== undefined) moved.push(`${canonical}=${inline}`);
    else if (takesValue) {
      const next = args[at + 1];
      if (next === undefined) return undefined;
      moved.push(canonical, next);
      at += 1;
    } else if (inline === undefined) moved.push(canonical);
    else return undefined;
    at += 1;
    named.push(name === canonical ? `\`${name}\`` : `\`${name}\` (${canonical})`);
    canonicalNames.push(canonical);
  }
  if (moved.length === 0 || args[at] !== "cli") return undefined;
  const rest = args.slice(at);
  const split = rest.includes("--") ? rest.indexOf("--") : rest.length;
  const corrected = [...kept, ...rest.slice(0, split), ...moved, ...rest.slice(split)];
  const verb = named.length === 1 ? "belongs" : "belong";
  const rejected = reject(
    `${named.join(", ")} ${verb} after the command path, not before \`cli\`; nothing was forwarded or sent`,
    { kind: "argv", action: `Move ${canonicalNames.join(", ")} after the command path: \`{command}\``, argv: [...corrected] },
  );
  return invalidCommand(settle(rejected), corrected.slice(index));
}

/**
 * Help is text in every output mode (mirrors Rust
 * `service::help_without_json`): a help request that also carries `--json`
 * reads as the same request without it. `undefined` when nothing changes.
 */
export function helpWithoutJson(args: readonly string[]): string[] | undefined {
  const end = args.includes("--") ? args.indexOf("--") : args.length;
  const head = args.slice(0, end);
  const asks = head[0] === "help" || head.at(-1) === "help" || head.some((token) => token === "--help" || token === "-h");
  if (!asks || !head.includes("--json")) return undefined;
  return [...head.filter((token) => token !== "--json"), ...args.slice(end)];
}

/**
 * A service command that reached the language path (mirrors
 * Rust `service::CliRedirect`): the words after the runner globals name a
 * service command without `cli` (`prose run list`), or a runner-global alias
 * precedes `cli` or the command.
 */
export interface CliRedirect {
  /** The corrected argv (after the product name). */
  argv: string[];
  /** Command words that, when one names an existing file or directory, make the original a language command after all. Empty when `cli` was given. */
  operands: string[];
  /** The rejection to render when no operand exists on disk. */
  command: ServiceCommand;
  /** The typed word is itself a language command (`prose status`, `prose run FILE`): the argv is forwarded, with `argv` as the HOSTED_UNAVAILABLE hint, unless the default hosted harness would refuse it; then `command` is rendered. */
  language: boolean;
  /**
   * The words are always forwarded, with `argv` as the HOSTED_UNAVAILABLE
   * hint (`prose run FILE`: the default hosted harness refuses it with
   * HOSTED_UNAVAILABLE, and the Action names `cli run submit FILE --preview`).
   */
  hintOnly?: boolean;
}

/** Whether `word` is a current OpenProse language command (SPEC 7.1, `grammar.intentInference.languageCommands`). */
function isLanguageCommand(word: string): boolean {
  return inference.languageCommands.includes(word);
}

/**
 * The one command a lone word before `cli` stands for through `nounSynonyms`
 * (`login` -> `auth login`, `models` -> `model list`): a complete command
 * path, or a group with exactly one verb. Never a guess by distance, so a
 * future language command is not captured.
 */
function loneSynonym(word: string): string[] | undefined {
  if (!Object.hasOwn(inference.nounSynonyms, word)) return undefined;
  const words = [...inference.nounSynonyms[word]!];
  const complete = (candidate: readonly string[]) => commandPaths().some((path) => path.length === candidate.length && path.every((part, index) => part === candidate[index]));
  if (complete(words)) return words;
  const verbs = verbsAfter(words);
  if (verbs.length !== 1) return undefined;
  words.push(verbs[0]!);
  return complete(words) ? words : undefined;
}

/** How a rejected language command word before `cli` is described (identical in both ports). */
const LANGUAGE_WORD = "is a language command, which runs only with a local harness";
const LANGUAGE_TAIL = "To run the language command, select a local harness with `prose cli harness use <id>`";
const FORWARD_TAIL = "To pass these words to the OpenProse language instead, put `--` before them";

/**
 * The command a lone word before `cli` and the word after it name through
 * `nounSynonyms` (mirrors Rust `lone_command`), and how many typed words
 * that spans: `loneSynonym`, whose verb a typed sibling verb replaces
 * (`credits topup` -> `wallet topup`), or a group synonym with one of its
 * verbs typed (`organization list` -> `org list`).
 */
function loneCommand(noun: string, verb: string | undefined): [string[], number] | undefined {
  const found = loneSynonym(noun);
  if (found !== undefined) return synonymPath(found, verb);
  const listed = Object.hasOwn(inference.nounSynonyms, noun) ? inference.nounSynonyms[noun]! : undefined;
  if (listed === undefined || verb === undefined || isCommandPath(listed) || !isCommandPath([...listed, verb])) return undefined;
  return [[...listed, verb], 2];
}

/**
 * A service word before `cli` that is no command path and no lone synonym
 * (mirrors Rust `service_word`): a verb before its group (`list jobs`), a
 * `commandRewrites` phrase (`stop`, `delete`, `share`, `cron`), `help
 * [COMMAND]`, or `run FILE`, which is `cli run submit FILE --preview`.
 * `prefix` is the runner globals before the words. `help` and `run` are
 * language commands: the caller forwards them unless the hosted harness
 * would refuse.
 */
function serviceWord(prefix: readonly string[], rest: readonly string[]): CliRedirect | undefined {
  const [noun, next] = rest;
  if (noun === undefined) return undefined;
  const positions = new Positions([...prefix, "cli", ...rest], rest);
  let meant: Meaning | undefined;
  let operands: string[];
  if (noun === "help") {
    const words: string[] = [];
    for (const word of rest.slice(1)) {
      if (word.startsWith("-") || !isCommandPrefix([...words, word])) break;
      words.push(word);
    }
    const argv = words.length === 0 ? [...prefix, "--help"] : [...prefix, "cli", ...words, "--help"];
    const action = words.length === 0 ? "Show the runner help: `{command}`" : `Show the help of \`cli ${words.join(" ")}\`: \`{command}\``;
    meant = { rejected: reject("", { kind: "argv", action, argv }), typed: 1 + words.length };
    operands = ["help"];
  } else if (noun === "run" && next !== undefined && !next.startsWith("-")) {
    const tail = rest.slice(2);
    const stop = tail.includes("--") ? tail.indexOf("--") : tail.length;
    const head = tail.slice(0, stop);
    const argv = [...prefix, "cli", "run", "submit", next, ...head, ...(head.includes("--preview") ? [] : ["--preview"]), ...tail.slice(stop)];
    meant = { rejected: settle(reject("", { kind: "argv", action: "Use `cli run submit`, previewing the run first: `{command}`", argv })), typed: 2 };
    operands = [next];
  } else {
    meant = phraseMeaning(rest, positions, 2) ?? verbFirst(rest, positions) ?? phraseMeaning(rest, positions, 1);
    if (meant === undefined) return undefined;
    meant = { ...meant, rejected: settle(meant.rejected) };
    operands = rest.slice(0, meant.typed);
  }
  const correction = meant.rejected.correction;
  const argv = correction.kind === "argv" ? correction.argv : correction.kind === "command" ? [...prefix, "cli", ...correction.words] : undefined;
  if (argv === undefined) return undefined;
  const language = isLanguageCommand(noun);
  const typed = rest.slice(0, meant.typed).join(" ");
  const why = meant.why === undefined ? "" : ` ${meant.why.slice(0, 1).toUpperCase()}${meant.why.slice(1)}.`;
  const reason = language
    ? `\`${typed}\` ${LANGUAGE_WORD}; did you mean \`${argvText(argv)}\`? Nothing was forwarded or sent. ${LANGUAGE_TAIL}`
    : `\`${typed}\` is not a command; did you mean \`${argvText(argv)}\`?${why} Nothing was forwarded or sent. ${FORWARD_TAIL}`;
  const command = invalidCommand(reject(reason, { kind: "argv", action: correction.action, argv }), rest);
  return { argv, operands, command, language, ...(noun === "run" ? { hintOnly: true } : {}) };
}

/**
 * Whether `globals` (the runner options before the command words) only choose
 * how output looks (`--output MODE`, `--no-color`, `--verbose`), so no local
 * harness run was asked for (mirrors Rust `only_display_globals`).
 */
function onlyDisplayGlobals(globals: readonly string[]): boolean {
  let index = 0;
  while (index < globals.length) {
    const token = globals[index]!;
    if (token === "--output") index += 2;
    else if (token === "--no-color" || token === "--verbose" || token.startsWith("--output=")) index += 1;
    else return false;
  }
  return true;
}

/** The runner-global option an alias before `cli` stands for: an `optionAliases` entry whose target is a `grammar.globalOptions` name. */
function globalAlias(name: string): string | undefined {
  if (!Object.hasOwn(inference.optionAliases, name)) return undefined;
  return inference.optionAliases[name]!.find((candidate) => manifest.grammar.globalOptions.includes(candidate));
}

/**
 * Detects a missing `cli` or a runner-global alias at `index`, the first token
 * the runner-global parser did not consume. `globalKind` classifies real runner
 * globals: `true` takes a value, `false` is a flag. Pure: the caller checks
 * `operands` against the filesystem and forwards (with `argv` as a hint) when
 * one exists. `undefined` leaves the argv to the ordinary parser.
 */
export function cliRedirect(args: readonly string[], index: number, globalKind: (name: string) => boolean | undefined): CliRedirect | undefined {
  if (index > args.length) return undefined;
  const argv = args.slice(0, index);
  const replaced: string[] = [];
  const reasons: string[] = [];
  let at = index;
  while (at < args.length) {
    const token = args[at]!;
    if (token === "cli" || token === "--") break;
    const equals = token.indexOf("=");
    const inline = token.startsWith("--") && equals >= 0 ? token.slice(equals + 1) : undefined;
    const name = inline === undefined ? token : token.slice(0, equals);
    const target = globalAlias(name);
    if (target !== undefined) {
      const value = inline ?? args[at + 1];
      if (value === undefined) return undefined;
      if (inline !== undefined) argv.push(`${target}=${value}`);
      else argv.push(target, value);
      replaced.push(`${name} with ${target}`);
      reasons.push(`\`${name}\` is not a runner option; did you mean ${target}?`);
      at += inline !== undefined ? 1 : 2;
      continue;
    }
    // A real runner global after an alias keeps its place.
    if (replaced.length > 0) {
      const kind = globalKind(name);
      if (kind === true) {
        const width = inline !== undefined ? 1 : 2;
        if (at + width > args.length) return undefined;
        argv.push(...args.slice(at, at + width));
        at += width;
        continue;
      }
      if (kind === false && inline === undefined) {
        argv.push(token);
        at += 1;
        continue;
      }
    }
    break;
  }
  const rest = args.slice(at);
  let tokens: readonly string[];
  let operands: string[];
  let language = false;
  let synonym: string | undefined;
  // A whole command line typed as one word (`prose "cli run list"`).
  const unsplit = replaced.length === 0 && rest[0] !== undefined ? unsplitWords(rest[0]) : undefined;
  if (unsplit !== undefined) {
    const corrected = [...argv, ...(unsplit[0] === "cli" ? [] : ["cli"]), ...unsplit, ...rest.slice(1)];
    const rejected = reject(`the word ${quote(rest[0]!)} holds a whole command line; the argv looks unsplit (quoted, so the shell passed it as one word): did you mean \`${argvText(corrected)}\`? Nothing was forwarded or sent`,
      { kind: "argv", action: "Pass each word separately: `{command}`", argv: [...corrected] });
    return { argv: corrected, operands: [rest[0]!], command: invalidCommand(rejected, rest), language: false };
  }
  if (rest[0] === "cli") {
    if (replaced.length === 0) return undefined;
    argv.push(...rest);
    tokens = rest.slice(1);
    operands = [];
  } else {
    const [noun, verb] = rest;
    let command: string[];
    let typed: number;
    if (noun === undefined) return undefined;
    // A bare `run` with no runner option selecting a harness runs nothing
    // here: hosted runs are `cli run submit`.
    const bareRun = noun === "run" && rest.length === 1 && onlyDisplayGlobals(args.slice(0, index));
    if (bareRun) { command = ["run", "submit", "--help"]; typed = 1; synonym = "run submit"; }
    else if (verb !== undefined && !verb.startsWith("-") && isCommandPrefix([noun, verb])) { command = [noun, verb]; typed = 2; }
    else if (commandPaths().some((path) => path.length === 1 && path[0] === noun)) { command = [noun]; typed = 1; }
    else {
      // `prose list jobs`: a verb before its group is never a lone synonym.
      const verbBefore = verbFirst(rest, new Positions([...argv, "cli", ...rest], rest)) !== undefined;
      const found = verbBefore ? undefined : loneCommand(noun, verb);
      if (found === undefined) return replaced.length === 0 ? serviceWord(argv, rest) : undefined;
      language = isLanguageCommand(noun);
      [command, typed] = found;
      synonym = command.join(" ");
    }
    argv.push("cli", ...command, ...rest.slice(typed));
    const kind = bareRun
      ? "alone runs nothing here: hosted runs are `prose cli run submit FILE`"
      : language ? LANGUAGE_WORD : synonym !== undefined ? "is not a command" : "is a service command";
    reasons.push(`\`${rest.slice(0, typed).join(" ")}\` ${kind}; did you mean \`${argvText(argv)}\`? Nothing was forwarded or sent. ${language ? LANGUAGE_TAIL : FORWARD_TAIL}`);
    tokens = rest;
    operands = rest.slice(0, typed);
  }
  const fix = synonym !== undefined ? `use \`cli ${synonym}\`` : "insert cli before the service command";
  const action = replaced.length === 0
    ? `${fix[0]!.toUpperCase()}${fix.slice(1)}: \`{command}\``
    : operands.length === 0
      ? `Replace ${replaced.join(", ")}: \`{command}\``
      : `Replace ${replaced.join(", ")} and ${fix}: \`{command}\``;
  // A rejected --output spelling before `cli` (`--format json`) chooses how the error is printed.
  const command = invalidCommand(settle(reject(reasons.join(" "), { kind: "argv", action, argv: [...argv] })), [...args.slice(index, at), ...tokens]);
  return { argv, operands, command, language };
}

/** Parses the tokens after `cli`; `argv` is the complete original argv. */
export function parseService(args: readonly string[], argv: readonly string[]): ServiceCommand {
  return parseWith(args, argv, true);
}

/** `parseService`, optionally without completing its corrections (the second parse of `settle`). */
function parseWith(args: readonly string[], argv: readonly string[], settled: boolean): ServiceCommand {
  const help = groupHelp(args);
  if (help !== undefined) return { kind: "help", text: help };
  let best: ManifestOperation | undefined;
  for (const candidate of manifest.operations.filter(isServiceOperation)) {
    const words = candidate.command;
    if (args.length >= words.length && words.every((word, index) => args[index] === word) && (best === undefined || best.command.length < words.length)) best = candidate;
  }
  // An exact alias spelling (`cli run status RUN_ID`) runs its target.
  const alias = commandAlias(args);
  let typed = best?.command.length ?? 0;
  if (alias !== undefined && (best === undefined || best.command.length < alias[1])) [best, typed] = alias;
  if (best === undefined) {
    const positions = new Positions(argv, args);
    return invalidCommand(settled ? unknownCommand(args, positions) : unknownCommandUnsettled(args, positions), args);
  }
  const rest = args.slice(typed);
  if (helpPosition(best, rest)) {
    const topic = `cli ${best.command.join(" ")}`;
    const text = helpTopics[topic];
    if (text === undefined) throw invocationFailure(`no help topic for \`${topic}\``);
    return { kind: "help", text };
  }
  const invocation: ServiceInvocation = {
    operation: best.id, arguments: new Map(), options: new Map(), flags: new Set(),
    json: false, yes: false, preview: false, argv: [...argv],
  };
  try { parseOperationArguments(best, rest, invocation, new Positions(argv, rest)); }
  catch (caught) {
    if (!(caught instanceof Rejected)) throw caught;
    const rejected = settled ? settle(caught) : caught;
    invocation.error = rejected.error;
    invocation.correction = rejected.correction;
    // Early global-option scan: a trailing --output after the rejected token
    // still selects the output mode of every suggested command.
    const scanned = scanGlobals(rest);
    if (invocation.output === undefined && scanned.output !== undefined) invocation.output = scanned.output;
    invocation.json ||= scanned.json;
  }
  return { kind: "invoke", invocation };
}

function helpPosition(candidate: ManifestOperation, rest: readonly string[]): boolean {
  const valueOptions = new Set(candidate.options.filter((item) => item.value !== null).map((item) => item.name));
  let index = 0;
  while (index < rest.length) {
    const token = rest[index]!;
    if (token === "--") return false;
    if (token === "--help" || token === "-h") return true;
    index += valueOptions.has(token) || token === "--output" ? 2 : 1;
  }
  return false;
}

function parseOperationArguments(candidate: ManifestOperation, rest: readonly string[], invocation: ServiceInvocation, positions: Positions): void {
  const words = candidate.command;
  const command = `cli ${words.join(" ")}`;
  const positionals: Array<[number, string]> = [];
  let jsonAt: number | undefined;
  let previewAt: number | undefined;
  let index = 0;
  let literal = false;
  while (index < rest.length) {
    const token = rest[index]!;
    const at = index;
    index += 1;
    if (literal || token === "-" || !token.startsWith("-")) { positionals.push([at, token]); continue; }
    if (token === "--") { literal = true; continue; }
    const equals = token.indexOf("=");
    const inline = token.startsWith("--") && equals >= 0 ? token.slice(equals + 1) : undefined;
    const name = inline === undefined ? token : token.slice(0, equals);
    const width = inline === undefined ? 2 : 1;
    const takeValue = (): string => {
      if (inline !== undefined) return inline;
      const value = rest[index];
      if (value === undefined) {
        throw reject(`option ${name} requires a value`, { kind: "command", action: `Give ${name} a value; \`{command}\` shows the syntax of \`${command}\``, words: helpWords(words) });
      }
      index += 1;
      return value;
    };
    const once = (length: number): Correction => argvOrHelp(`Pass ${name} once: \`{command}\``, positions.splice(at, length, []), words, "Pass the option once; `{command}` shows the syntax");
    const noValue = (): Correction => argvOrHelp(`Pass ${name} without a value: \`{command}\``, positions.splice(at, 1, [name]), words, "Pass the flag without a value; `{command}` shows the syntax");
    if (name === "--json" || name === "--yes" || name === "--preview") {
      if (inline !== undefined) throw reject(`option ${name} does not take a value`, noValue());
      const key = name === "--json" ? "json" : name === "--yes" ? "yes" : "preview";
      if (invocation[key]) throw reject(`option ${name} was specified more than once`, once(1));
      invocation[key] = true;
      if (name === "--json") jsonAt = at;
      if (name === "--preview") previewAt = at;
      continue;
    }
    // The global --no-color is accepted after the command path too.
    if (name === "--no-color") {
      if (inline !== undefined) throw reject(`option ${name} does not take a value`, noValue());
      continue;
    }
    if (name === "--output") {
      const value = takeValue();
      if (value !== "human" && value !== "json" && value !== "jsonl") {
        throw reject(`invalid output mode ${quote(value)}; expected human, json, or jsonl`, { kind: "text", action: "Use --output human, --output json or --output jsonl." });
      }
      if (invocation.output !== undefined) throw reject("option --output was specified more than once", once(width));
      invocation.output = value;
      continue;
    }
    const known = candidate.options.find((item) => item.name === name);
    if (known === undefined) {
      const names = [...candidate.options.map((item) => item.name), "--json", "--yes", "--preview", "--help", "--output"];
      const converted = conversionRejection(name, inline, rest[index], at, names, command, words, positions);
      if (converted !== undefined) throw converted;
      const specKey = specKeyRejection(candidate, name, rest, command, positions);
      if (specKey !== undefined) throw specKey;
      const asArgument = argumentOptionRejection(candidate, name, inline, at, rest, command, words, positions);
      if (asArgument !== undefined) throw asArgument;
      const schedule = scheduleRejection(candidate, name, rest, command, positions);
      if (schedule !== undefined) throw schedule;
      // An option dropped with a reason (`--quiet`, `--jq FILTER`).
      const removal = optionRemoval(name, candidate.id);
      if (removal !== undefined) {
        const [why, takesValue] = removal;
        const valued = takesValue && inline === undefined && rest[index] !== undefined && !rest[index]!.startsWith("-");
        const reason = `unknown option ${name} for \`${command}\`; ${why}`;
        if (candidate.id === "run.share") throw reject(reason, shareCorrection(at, valued ? 2 : 1, rest, words, positions));
        throw reject(reason,
          argvOrHelp(`Drop ${name}: \`{command}\``, positions.splice(at, valued ? 2 : 1, []), words, "Drop the option; `{command}` shows the options"));
      }
      // A KEY=VALUE option (`--input`) is the alias only for a value that has
      // `=` (`--env K=V`, but `--env linux`).
      const given = inline ?? (rest[index] !== undefined && !rest[index]!.startsWith("-") ? rest[index] : undefined);
      const fits = (target: string): boolean => {
        const spec = candidate.options.find((item) => item.name === target)?.value;
        return spec === undefined || spec === null || !spec.startsWith("KEY=") || (given !== undefined && given.includes("="));
      };
      const takesValue = (target: string): boolean => target === "--output" || candidate.options.some((item) => item.name === target && item.value !== null);
      // The word after the option is its value when it is inline, or when the
      // command has no positional slot left for it.
      const capacity = candidate.arguments.some((item) => item.variadic) ? Number.POSITIVE_INFINITY : candidate.arguments.length;
      const hasValue = inline !== undefined || (given !== undefined && positionalCount(candidate, rest) > capacity);
      const aliased = optionAlias(name, names, fits);
      const paging = aliased === "--before" ? pagingRejection(name, inline, given, at, command, words, positions) : undefined;
      if (paging !== undefined) throw paging;
      const withUnit = aliased === undefined ? undefined : durationRejection(candidate, name, aliased, inline, given, at, command, words, positions);
      if (withUnit !== undefined) throw withUnit;
      const found = suggestOption(name, names, fits, hasValue, takesValue);
      const correction: Correction = found === undefined
        // No mapping: the suggestion is the argv without it.
        ? argvOrHelp(`Remove ${name}, which \`${command}\` does not take: \`{command}\``, positions.splice(at, 1, []), words, "Remove the option; `{command}` shows the options")
        : argvOrHelp(`Replace ${name} with ${found}: \`{command}\``, positions.splice(at, 1, [inline === undefined ? found : `${found}=${inline}`]), words, "Use one of the listed options; `{command}` shows them");
      throw reject(`unknown option ${name} for \`${command}\`${found === undefined ? "" : `; did you mean ${found}?`}`, correction);
    }
    if (known.value === null) {
      if (inline !== undefined) throw reject(`option ${name} does not take a value`, noValue());
      if (invocation.flags.has(name)) throw reject(`option ${name} was specified more than once`, once(1));
      invocation.flags.add(name);
    } else {
      const value = takeValue();
      // An option with `choices` accepts only those values.
      if (known.choices !== undefined && !known.choices.includes(value)) {
        const lower = asciiLower(value);
        const near = known.choices.find((choice) => choice === lower) ?? nearest(lower, known.choices, 1)[0] ?? "";
        const fixed = inline !== undefined ? positions.splice(at, 1, [`${name}=${near}`]) : positions.splice(at, 2, [name, near]);
        throw reject(`option ${name} must be one of ${known.choices.join(", ")}; got ${quote(value)}`,
          argvOrHelp(`Use ${name} ${near}: \`{command}\``, fixed, words, "Use one of the listed values; `{command}` shows them"));
      }
      const values = invocation.options.get(name) ?? [];
      if (values.length > 0 && !known.repeatable) throw reject(`option ${name} was specified more than once`, once(width));
      values.push(value);
      invocation.options.set(name, values);
    }
  }
  if (invocation.preview && !candidate.preview) {
    throw reject(`\`${command}\` does not change anything, so --preview does not apply`,
      argvOrHelp(`Drop --preview; \`${command}\` only reads: \`{command}\``, previewAt === undefined ? undefined : positions.splice(previewAt, 1, []), words, "Drop --preview; `{command}` shows the syntax"));
  }
  if (invocation.json && invocation.output !== undefined && invocation.output !== "json") {
    throw reject("--json conflicts with --output",
      argvOrHelp("Keep one output choice; without --json: `{command}`", jsonAt === undefined ? undefined : positions.splice(jsonAt, 1, []), words, "Keep one of --json and --output; `{command}` shows the syntax"));
  }
  const missing = (name: string): Rejected => {
    const listing = lister(words, name, invocation);
    const correction: Correction = listing === undefined
      ? { kind: "command", action: `Pass <${name}>; \`{command}\` shows the arguments of \`${command}\``, words: helpWords(words) }
      : { kind: "command", action: `Pass <${name}>; list ${listing[0]} with \`{command}\``, words: listing[1] };
    return reject(`missing argument <${name}> for \`${command}\``, correction);
  };
  // `cli program save FILE`: the one argument is the program file, so the
  // missing argument is the slug that comes first.
  const only = positionals.length === 1 ? positionals[0] : undefined;
  if (candidate.id === "program.save" && only !== undefined && (only[1].endsWith(".md") || only[1].includes("/"))) {
    const [at, file] = only;
    const slug = slugFromFile(file);
    throw reject(`missing argument <SLUG> for \`${command}\`; ${quote(file)} is the program file, which comes after the slug: \`${command} <SLUG> <FILE>\``,
      argvOrHelp("Pass the slug before the file: `{command}`", slug === undefined ? undefined : positions.splice(at, 1, [slug, file]), words, "Pass <SLUG> before <FILE>; `{command}` shows the arguments"));
  }
  // `cli program save FILE SLUG`: the arguments are swapped.
  if (candidate.id === "program.save" && positionals.length === 2) {
    const looksLikeFile = (value: string): boolean => value.endsWith(".md") || value.includes("/");
    const [firstAt, first] = positionals[0]!;
    const [secondAt, second] = positionals[1]!;
    if (looksLikeFile(first) && !looksLikeFile(second) && /^[a-z0-9][a-z0-9-]{0,63}$/u.test(second)) {
      let swapped = positions.splice(firstAt, 1, [second]);
      if (swapped !== undefined && positions.offset !== undefined) swapped[positions.offset + secondAt] = first;
      if (positions.offset === undefined) swapped = undefined;
      throw reject(`the arguments of \`${command}\` are swapped: ${quote(first)} is the program file, which comes after the slug ${quote(second)}: \`${command} <SLUG> <FILE>\``,
        argvOrHelp("Pass the slug before the file: `{command}`", swapped, words, "Pass <SLUG> before <FILE>; `{command}` shows the arguments"));
    }
  }
  let position = 0;
  for (const item of candidate.arguments) {
    if (item.variadic) {
      const collected = positionals.slice(position).map(([, value]) => value);
      position = positionals.length;
      if (collected.length === 0 && item.required) throw missing(item.name);
      if (collected.length > 0) invocation.arguments.set(item.name, collected);
      continue;
    }
    const entry = positionals[position];
    if (entry === undefined) {
      if (item.required) throw missing(item.name);
      continue;
    }
    position += 1;
    invocation.arguments.set(item.name, [entry[1]]);
  }
  const extra = positionals[position];
  if (extra !== undefined) {
    const known = extraArgumentRejection(candidate, invocation, extra[0], extra[1], command, words, positions, rest);
    if (known !== undefined) throw known;
    throw reject(`unexpected argument ${quote(extra[1])} for \`${command}\``,
      argvOrHelp(`Remove the extra argument ${quote(extra[1])}: \`{command}\``, positions.splice(extra[0], 1, []), words, "Remove the extra argument; `{command}` shows the arguments"));
  }
  for (const item of candidate.options) {
    if (item.required && !invocation.options.has(item.name)) {
      throw reject(`missing required option ${item.name} for \`${command}\``,
        { kind: "command", action: `Add ${item.name} ${item.value ?? "VALUE"}; \`{command}\` shows the options of \`${command}\``, words: helpWords(words) });
    }
  }
}

/**
 * `--amount 5` on an operation with `--amount-cents` (`optionConversions`;
 * mirrors Rust `conversion_rejection`): a positive whole
 * number is converted and the reason says so; any other value is not, and
 * the reason states the unit.
 */
/** The argument an option spelling names (`--slug` names SLUG, `--run-id` RUN_ID, `--slug` also the SLUG of OWNER/SLUG[@REV]); mirrors Rust `named_argument`. */
function namedArgument(name: string, candidate: ManifestOperation): [number, string] | undefined {
  if (!name.startsWith("--")) return undefined;
  const wanted = name.slice(2).toUpperCase().replaceAll("-", "_");
  for (const [index, argument] of candidate.arguments.entries()) {
    const parts = argument.name.split(/[^A-Z_]/u).filter((part) => part.length > 0);
    if (argument.name === wanted || (wanted === "SLUG" && parts.includes("SLUG"))) return [index, argument.name];
  }
  return undefined;
}

/**
 * An option spelling of a positional argument (`cli org create --slug acme
 * --name Acme`): the suggestion passes the value as that argument, in its
 * place among the other arguments (mirrors Rust `argument_option_rejection`).
 */
function argumentOptionRejection(candidate: ManifestOperation, name: string, inline: string | undefined, at: number, rest: readonly string[], command: string, words: readonly string[], positions: Positions): Rejected | undefined {
  const named = namedArgument(name, candidate);
  if (named === undefined) return undefined;
  const [slot, argument] = named;
  let value: string;
  let width: number;
  if (inline !== undefined) { value = inline; width = 1; }
  else {
    const next = rest[at + 1];
    if (next === undefined || next.startsWith("-")) return undefined;
    value = next; width = 2;
  }
  const takesValue = (token: string): boolean => !token.includes("=")
    && (token === "--output" || candidate.options.some((option) => option.name === token && option.value !== null));
  const kept: string[] = [];
  const positional: number[] = [];
  let index = 0;
  let literal = false;
  while (index < rest.length) {
    const token = rest[index]!;
    if (index === at) { index += width; continue; }
    if (literal || token === "-" || !token.startsWith("-")) positional.push(kept.length);
    else if (token === "--") literal = true;
    else if (takesValue(token)) {
      kept.push(token);
      index += 1;
      const optionValue = rest[index];
      if (optionValue !== undefined) { kept.push(optionValue); index += 1; }
      continue;
    }
    kept.push(token);
    index += 1;
  }
  const last = positional.at(-1);
  const insertAt = positional[slot] ?? (last === undefined ? 0 : last + 1);
  kept.splice(insertAt, 0, value);
  return reject(`unknown option ${name} for \`${command}\`; <${argument}> is an argument, not an option`,
    argvOrHelp(`Pass <${argument}> as an argument: \`{command}\``, positions.splice(0, rest.length, kept), words, "Pass the argument without an option name; `{command}` shows the arguments"));
}

function conversionRejection(name: string, inline: string | undefined, next: string | undefined, at: number, names: readonly string[], command: string, words: readonly string[], positions: Positions): Rejected | undefined {
  const conversion = optionConversion(name, names);
  if (conversion === undefined) return undefined;
  const { option: target, multiplier, from, to } = conversion;
  const value = inline ?? (next !== undefined && !next.startsWith("-") ? next : undefined);
  const converted = value === undefined ? undefined : convertedValue(value, multiplier);
  if (value !== undefined && converted !== undefined) {
    const replacement = inline !== undefined ? [`${target}=${converted}`] : [target, converted];
    return reject(`unknown option ${name} for \`${command}\`; ${target} counts ${to}, so ${name} ${value} (${from}) is ${target} ${converted}`,
      argvOrHelp(`Use ${target} ${converted} (${from} converted to ${to}): \`{command}\``, positions.splice(at, inline !== undefined ? 1 : 2, replacement), words, "Use the listed option; `{command}` shows it"));
  }
  const given = value === undefined ? "" : `; ${quote(value)} is not a positive whole number of ${from}, so it was not converted`;
  return reject(`unknown option ${name} for \`${command}\`; the option is ${target}, a whole number of ${to} (${target} ${5 * multiplier} for 5 ${from})${given}`,
    { kind: "command", action: `Pass ${target} with a whole number of ${to}; \`{command}\` shows the options of \`${command}\``, words: helpWords(words) });
}

/** The Action of a `cli run share` correction: the link is public, never for one person. */
const SHARE_ACTION = "`cli run share` makes a public, unrevocable 24-hour link, and `cli org invite` gives one person access; preview the link first: `{command}`";

/**
 * A `cli run share` argv that tried to name a person (mirrors Rust
 * `share_correction`): `width` tokens at `at` are dropped, and so is
 * `--yes`, and `--preview` is added, so the suggestion never mints a public
 * link on its own.
 */
function shareCorrection(at: number, width: number, rest: readonly string[], words: readonly string[], positions: Positions): Correction {
  const base = positions.splice(at, width, []);
  const offset = positions.offset;
  if (base === undefined || offset === undefined) return { kind: "command", action: SHARE_ACTION.replace(": `{command}`", "; `{command}` shows the syntax"), words: helpWords(words) };
  const end = offset + rest.length - width;
  const region = base.slice(offset, end);
  const literal = region.indexOf("--");
  const stop = literal < 0 ? region.length : literal;
  const head = region.slice(0, stop).filter((token) => token !== "--yes");
  if (!head.includes("--preview")) head.push("--preview");
  return { kind: "argv", action: SHARE_ACTION, argv: [...base.slice(0, offset), ...head, ...region.slice(stop), ...base.slice(end)] };
}

/**
 * `--page 2`, `--cursor C` or `--offset 20` on a paged listing (mirrors Rust
 * `paging_rejection`): pages are cursors, so a number (or no value) is
 * dropped and the Action names result.nextBefore; any other value is the
 * cursor --before takes.
 */
function pagingRejection(name: string, inline: string | undefined, given: string | undefined, at: number, command: string, words: readonly string[], positions: Positions): Rejected {
  if (given === undefined || /^[0-9]+$/u.test(given)) {
    const width = inline === undefined && given !== undefined ? 2 : 1;
    return reject(`unknown option ${name} for \`${command}\`; pages are cursors, not numbers: --before takes the result.nextBefore of the previous page`,
      argvOrHelp("List the first page with `{command}`, then pass its result.nextBefore to --before for the next page", positions.splice(at, width, []), words, "Pass a previous result's result.nextBefore to --before; `{command}` shows the options"));
  }
  return reject(`unknown option ${name} for \`${command}\`; did you mean --before? It takes the cursor a previous page returned in result.nextBefore`,
    argvOrHelp(`Replace ${name} with --before, which takes the previous page's result.nextBefore: \`{command}\``, positions.splice(at, 1, [inline === undefined ? "--before" : `--before=${inline}`]), words, "Use one of the listed options; `{command}` shows them"));
}

/**
 * `--timeout 30` for a DURATION option (mirrors Rust `duration_rejection`):
 * the alias target with the unit a bare positive whole number lacks
 * (`--wait 30s`). `undefined` for any other value or target.
 */
function durationRejection(candidate: ManifestOperation, name: string, target: string, inline: string | undefined, given: string | undefined, at: number, command: string, words: readonly string[], positions: Positions): Rejected | undefined {
  const spec = candidate.options.find((item) => item.name === target)?.value;
  if (spec !== "DURATION" || given === undefined || !/^[0-9]{1,9}$/u.test(given) || Number(given) === 0) return undefined;
  const value = `${Number(given)}s`;
  const replacement = inline !== undefined ? [`${target}=${value}`] : [target, value];
  return reject(`unknown option ${name} for \`${command}\`; did you mean ${target}? ${target} takes a duration with a unit, so ${given} is ${value}`,
    argvOrHelp(`Use ${target} ${value}: \`{command}\``, positions.splice(at, inline !== undefined ? 1 : 2, replacement), words, "Use the listed option; `{command}` shows it"));
}

/** Seconds in an interval value: a positive whole number, optionally with s, m, h or d (mirrors Rust `interval_seconds`). */
function intervalSeconds(value: string): number | undefined {
  const match = /^([0-9]{1,9})([smhd]?)$/u.exec(value);
  if (match === null) return undefined;
  const unit: Record<string, number> = { "": 1, s: 1, m: 60, h: 3600, d: 86400 };
  const number = Number(match[1]) * unit[match[2]!]!;
  return number > 0 ? number : undefined;
}

/**
 * `cli job create --cron ...`, `--every 1h`, `--daily` or `--program SLUG`
 * (mirrors Rust `schedule_rejection`): a job is a JSON spec. The suggestion
 * reads a schedule spec from standard input with --preview;
 * `details.suggestedSpec` is the object and `details.suggestedStdin` the
 * line to pipe. A given program becomes program_ref, a given interval
 * interval_seconds (default 86400, daily).
 */
function scheduleRejection(candidate: ManifestOperation, name: string, rest: readonly string[], command: string, positions: Positions): Rejected | undefined {
  if (candidate.id !== "job.create") return undefined;
  const scheduleOption = (flag: string): [string, boolean] | undefined => {
    const key = flag === "--from" ? "--program" : flag;
    const entry = Object.hasOwn(inference.optionRemovals, key) ? inference.optionRemovals[key]! : undefined;
    return entry !== undefined && entry.operations.includes(candidate.id) ? [entry.why, entry.value] : undefined;
  };
  const own = scheduleOption(name);
  if (own === undefined) return undefined;
  let program: string | undefined;
  let interval: number | undefined;
  const kept: string[] = [];
  let index = 0;
  while (index < rest.length) {
    const token = rest[index]!;
    if (token === "--") break;
    const equals = token.indexOf("=");
    const inline = token.startsWith("--") && equals >= 0 ? token.slice(equals + 1) : undefined;
    const flag = inline === undefined ? token : token.slice(0, equals);
    const entry = token.startsWith("-") ? scheduleOption(flag) : undefined;
    if (entry !== undefined) {
      let value = inline;
      let width = 1;
      const next = rest[index + 1];
      if (value === undefined && entry[1] && next !== undefined && !next.startsWith("-")) { value = next; width = 2; }
      if (value !== undefined && (flag === "--program" || flag === "--from")) program = value;
      if (value !== undefined && (flag === "--every" || flag === "--interval")) interval = intervalSeconds(value) ?? interval;
      if (flag === "--daily") interval = 86400;
      index += width;
      continue;
    }
    if (flag === "--json" || flag === "--no-color") kept.push(token);
    if (flag === "--output") {
      kept.push(token);
      const value = rest[index + 1];
      if (inline === undefined && value !== undefined) { kept.push(value); index += 1; }
    }
    index += 1;
  }
  const spec = { interval_seconds: interval ?? 86400, program_ref: program ?? "your-program", type: "schedule" };
  const json = canonicalJson(spec);
  const argv = positions.splice(0, rest.length, [...kept, "--spec-file", "-", "--preview"]);
  const error = failure("INVOCATION_INVALID", { reason: `unknown option ${name} for \`${command}\`; ${own[0]}`, suggestedSpec: spec, suggestedStdin: `${json}\n` });
  const replace = program === undefined ? " (replace your-program with your program's slug; `cli program list` lists them)" : "";
  const action = `Create a schedule job from a JSON spec on standard input${replace}, previewing it first: \`printf '%s\\n' ${shellQuote(json)} | {command}\``;
  return new Rejected(error, argv === undefined
    ? { kind: "command", action: "Pass a schedule spec with --spec-file; `{command}` shows the spec", words: helpWords(candidate.command) }
    : { kind: "argv", action, argv });
}

/**
 * `cli job update ID --name x` (mirrors Rust
 * `spec_key_rejection`): an unknown option that names a key of the
 * operation's --spec-file object. The suggestion reads the spec from
 * standard input, and `details.suggestedStdin` holds the JSON object.
 */
function specKeyRejection(candidate: ManifestOperation, name: string, rest: readonly string[], command: string, positions: Positions): Rejected | undefined {
  const spec = candidate.spec;
  if (spec?.keys === undefined) return undefined;
  const keys = spec.keys;
  const keyOf = (token: string): [string, unknown] | undefined => {
    if (!token.startsWith("--")) return undefined;
    const key = token.slice(2).replaceAll("-", "_");
    if (!Object.hasOwn(keys, key)) return undefined;
    const kind = keys[key];
    const simple = kind === "text" || kind === "interval" || (typeof kind === "object" && kind !== null && "enum" in kind);
    return simple && !key.endsWith("_secret") ? [key, kind] : undefined;
  };
  if (keyOf(name) === undefined) return undefined;
  const object: Record<string, unknown> = {};
  const spans: Array<[number, number]> = [];
  let index = 0;
  while (index < rest.length) {
    const token = rest[index]!;
    if (token === "--") break;
    if (token === spec.option || token.startsWith(`${spec.option}=`)) return undefined;
    const equals = token.indexOf("=");
    const inline = token.startsWith("--") && equals >= 0 ? token.slice(equals + 1) : undefined;
    const flag = inline === undefined ? token : token.slice(0, equals);
    const found = keyOf(flag);
    if (found !== undefined) {
      const value = inline ?? rest[index + 1];
      if (value === undefined) return undefined;
      object[found[0]] = found[1] === "interval" && /^[0-9]+$/u.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : value;
      const width = inline === undefined ? 2 : 1;
      spans.push([index, width]);
      index += width;
      continue;
    }
    index += 1;
  }
  const first = spans[0];
  if (first === undefined) return undefined;
  const base = positions.splice(0, 0, []);
  if (base === undefined) return undefined;
  const offset = base.length - rest.length;
  const argv = [...base];
  for (const [start, width] of [...spans].reverse()) {
    argv.splice(offset + start, width, ...(start === first[0] ? [spec.option, "-"] : []));
  }
  const json = canonicalJson(object);
  const listed = spans.map(([start]) => rest[start]!.split("=")[0]!).join(", ");
  const what = spans.length === 1 ? "is a key of the" : "are keys of the";
  const error = failure("INVOCATION_INVALID", { reason: `unknown option ${name} for \`${command}\`; ${listed} ${what} ${spec.option} JSON object: ${json}`, suggestedStdin: `${json}\n` });
  return new Rejected(error, { kind: "argv", action: `Pass the settings as JSON on standard input: \`printf '%s\\n' ${shellQuote(json)} | {command}\``, argv });
}

/**
 * An unexpected positional argument with a known meaning (* mirrors Rust `extra_argument_rejection`): the never-echoed secret of a
 * `secretFileOptions` operation, or the value of the one missing required
 * value option.
 */
function extraArgumentRejection(candidate: ManifestOperation, invocation: ServiceInvocation, at: number, extra: string, command: string, words: readonly string[], positions: Positions, rest: readonly string[]): Rejected | undefined {
  // `cli run share RUN_ID someone@example.com`: a link is never shared with one person.
  if (candidate.id === "run.share" && extra.includes("@")) {
    return reject(`unexpected argument ${JSON.stringify(extra)} for \`${command}\`; \`${command}\` mints a public, unrevocable 24-hour link that anyone who has it can open; it is not shared with one person`,
      shareCorrection(at, 1, rest, words, positions));
  }
  const secret = candidate.options.map((item) => [item.name, secretPlaceholder(item.name)] as const).find(([, placeholder]) => placeholder !== undefined);
  if (secret !== undefined) {
    const [name, placeholder] = secret as readonly [string, string];
    const reason = `\`${command}\` never takes the ${placeholder.toLowerCase()} as an argument, so the argument was not echoed; it reads ${name}, a file or - for standard input`;
    return invocation.options.has(name)
      ? reject(reason, argvOrHelp("Remove the extra argument: `{command}`", positions.splice(at, 1, []), words, "Remove the extra argument; `{command}` shows the arguments"))
      : reject(reason, argvOrHelp(`Pipe the ${placeholder.toLowerCase()} in on standard input: \`printf '%s\\n' "$${placeholder}" | {command}\``, positions.splice(at, 1, [name, "-"]), words, "Pass the secret with its file option; `{command}` shows it"));
  }
  const missing = candidate.options.filter((item) => item.required && item.value !== null && !invocation.options.has(item.name));
  if (missing.length !== 1) return undefined;
  const option = missing[0]!;
  const fits = option.value === "N" ? /^[0-9]+$/u.test(extra) : option.value === "FILE" ? extra === "-" || isFile(extra) : true;
  if (!fits || (extra.startsWith("-") && extra !== "-")) return undefined;
  return reject(`unexpected argument ${quote(extra)} for \`${command}\`; did you mean ${option.name} ${extra}? ${option.name}: ${option.description}`,
    argvOrHelp(`Pass it as ${option.name}: \`{command}\``, positions.splice(at, 1, [option.name, extra]), words, "Pass the value with its option; `{command}` shows the options"));
}

/** Whether `path` names an existing file, relative to the working directory. */
function isFile(path: string): boolean {
  try { return statSync(path).isFile(); } catch { return false; }
}

export type { Environment } from "./endpoint";
export { environmentLabel, journalComponent } from "./endpoint";

export function validCredential(token: string): boolean {
  return /^rr_test_[0-9a-f]{32}$/u.test(token);
}
