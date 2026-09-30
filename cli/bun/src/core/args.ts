import { parsePackageCommand } from "./package-args";
import { invocationFailure } from "./errors";
import { quote } from "./output";
import { claims, cliRedirect, groupHelp, helpRequest, helpWithoutJson, invalidGlobalOutput, misplacedGlobals, misplacedLocalOptions, parseService, unknown, unknownOptionBeforeCli } from "./service/manifest";
import { RunnerFailure, type GlobalFlags, type OutputMode, type ParsedEntrypoint } from "./types";

const valueOptions: Record<string, keyof GlobalFlags> = {
  "--harness": "harness",
  "--transport": "transport",
  "--cwd": "cwd",
  "--model": "model",
  "--auth-profile": "authProfile",
  "--native-profile": "nativeProfile",
  "--native-max-turns": "nativeMaxTurns",
  "--native-timeout": "nativeTimeout",
  "--native-tool-timeout": "nativeToolTimeout",
  "--native-output-bytes": "nativeOutputBytes",
  "--native-add-dir": "nativeAddDirs",
  "--native-allow-tool": "nativeAllowTools",
  "--native-log": "nativeLog",
  "--output-contract": "outputContract",
  "--permission-mode": "permissionMode",
  "--timeout": "timeout",
  "--output": "output",
};

/** Whether a token is a runner-global value option (it consumes the next token). */
export function isValueOption(token: string): boolean {
  return valueOptions[token] !== undefined;
}

function invalid(message: string): never {
  throw invocationFailure(message);
}

function setValue(global: GlobalFlags, key: keyof GlobalFlags, value: string, option: string): void {
  if (value.length === 0) invalid(`${option} requires a value.`);
  if (key === "nativeAddDirs" || key === "nativeAllowTools") { (global[key] ??= []).push(value); return; }
  if (key === "nativeMaxTurns" || key === "nativeTimeout" || key === "nativeToolTimeout" || key === "nativeOutputBytes") {global[key]=value;return;}
  if (key === "nativeProfile") { global.nativeProfile=value; return; }
  if (key === "output") {
    if (value !== "human" && value !== "json" && value !== "jsonl") {
      invalid(`invalid output mode ${quote(value)}; expected human, json, or jsonl`);
    }
    global.output = value as OutputMode;
    return;
  }
  if (key === "harness") global.harness = value;
  else if (key === "transport") global.transport = value;
  else if (key === "cwd") global.cwd = value;
  else if (key === "model") global.model = value;
  else if (key === "authProfile") global.authProfile = value;
  else if (key === "nativeLog") global.nativeLog = value;
  else if (key === "outputContract") {
    if (value !== "native" && value !== "image-envelope") invalid("output contract must be native or image-envelope");
    global.outputContract = value;
  }
  else if (key === "permissionMode") global.permissionMode = value;
  else if (key === "timeout") global.timeout = value;
}

export function parseEntrypoint(args: readonly string[]): ParsedEntrypoint {
  const global: GlobalFlags = {};
  let index = 0;
  let forcedLanguage = false;

  while (index < args.length) {
    const token = args[index]!;
    if (token === "--") {
      forcedLanguage = true;
      index += 1;
      break;
    }
    if (token === "--help") return { kind: "help", global };
    if (token === "--version") return { kind: "version", global };
    if (token === "--dry-run") {
      global.dryRun = true;
      index += 1;
      continue;
    }
    if (token === "--no-color") {
      global.color = false;
      index += 1;
      continue;
    }
    if (token === "--verbose") {
      global.verbose = true;
      index += 1;
      continue;
    }

    // An invalid `--output` value before `cli` is a service invocation error
    // with a suggestion, never an alias.
    const badGlobal = invalidGlobalOutput(args, index);
    if (badGlobal !== undefined) return { kind: "service", global, command: badGlobal };

    const equals = token.indexOf("=");
    const option = equals >= 0 ? token.slice(0, equals) : token;
    const key = valueOptions[option];
    if (key !== undefined) {
      const value = equals >= 0 ? token.slice(equals + 1) : args[index + 1];
      if (value === undefined) invalid(`${option} requires a value.`);
      if ((key === "model" || key === "authProfile" || key === "nativeProfile" || key === "nativeMaxTurns" || key === "nativeTimeout" || key === "nativeToolTimeout" || key === "nativeOutputBytes") && global[key] !== undefined) {
        invalid(`runner option ${option} was specified more than once`);
      }
      setValue(global, key, value, option);
      index += equals >= 0 ? 1 : 2;
      continue;
    }

    // Unknown option-looking tokens are language input. This is the permanent
    // freeze point; nothing after it is considered runner configuration.
    break;
  }

  if (index >= args.length && !forcedLanguage) return { kind: "help", global };
  const tail = args.slice(index);
  if (forcedLanguage && tail.length === 0) invalid("`--` must be followed by a language command");
  // Service command words without `cli`, or a runner-global alias before
  // them, never reach the language unless a file of that name makes them a
  // language command; the caller checks `redirect.operands`.
  // `prose help cli [COMMAND]` asks for the runner's cli help: `cli` is
  // reserved, so it can never be a language topic.
  // Command-local options before `cli` (`prose --json cli run list`) are
  // never forwarded to the language.
  const misplacedLocal = forcedLanguage ? undefined : misplacedLocalOptions(args, index, globalKind);
  if (misplacedLocal !== undefined) return { kind: "service", global, command: misplacedLocal };
  // Help is text in every mode: `prose help cli --json`.
  const helped = ["help", ...tail.slice(2)];
  const helpedText = helpWithoutJson(helped) ?? helped;
  if (!forcedLanguage && tail[0] === "help" && tail[1] === "cli" && helpRequest(helpedText) !== undefined) {
    return parseOperation(global, helpedText, args);
  }
  const redirect = forcedLanguage ? undefined : cliRedirect(args, index, globalKind);
  if (redirect !== undefined) return { kind: "language", global, argv: ["prose", ...tail], redirect };
  // An unknown option before `cli` and a service command is a service
  // invocation error, never language input.
  const unknownLeading = forcedLanguage ? undefined : unknownOptionBeforeCli(args, index, globalKind);
  if (unknownLeading !== undefined) return { kind: "service", global, command: unknownLeading };
  if (!forcedLanguage && tail[0] === "cli") return parseOperation(global, tail.slice(1), args);
  return { kind: "language", global, argv: ["prose", ...tail] };
}

/** Classifies a runner-global option: `true` takes a value, `false` is a flag, `undefined` is not a runner global. */
function globalKind(name: string): boolean | undefined {
  if (valueOptions[name] !== undefined) return true;
  if (name === "--dry-run" || name === "--no-color" || name === "--verbose") return false;
  return undefined;
}

function parseOperation(global: GlobalFlags, tokens: readonly string[], full: readonly string[]): ParsedEntrypoint {
  if (tokens[0] === "weave") return { kind: "weave", global, argv: [...tokens.slice(1)] };
  // Help is text in every mode, so `--json` beside a help request is
  // dropped.
  const unjson = helpWithoutJson(tokens) ?? tokens;
  // Bare `cli`, `cli help [COMMAND]`, `cli <group> help` and a trailing `-h`
  // read as the matching `--help`.
  const args = helpRequest(unjson) ?? unjson;
  const serviceHelp = groupHelp(args);
  if (serviceHelp !== undefined) return { kind: "service", global, command: { kind: "help", text: serviceHelp } };
  if (knownRunnerHelpPath(args.at(-1) === "-h" ? [...args.slice(0, -1), "--help"] : args)) return { kind: "help", global };
  const misplaced = misplacedGlobals(args, full);
  if (misplaced !== undefined) return { kind: "service", global, command: misplaced };
  if (claims(args)) return { kind: "service", global, command: parseService(args, full) };
  if (args[0] === "harness" && args[1] === "use") {
    return parseHarnessUse(global, args.slice(2));
  }
  // Account and package verbs take `--output` after the command path, like service verbs.
  const account = args[0] === "package" || (args[0] === "auth" && ["status", "login", "logout"].includes(args[1] ?? "")) || (args[0] === "org" && args[1] === "list");
  const withoutJson = account ? takeTrailingGlobals(global, args) : [...args];
  let json = false;
  if (withoutJson.at(-1) === "--json") {
    withoutJson.pop();
    json = true;
  }
  if (withoutJson[0] === "package") return { kind: "operation", global, operation: "package", json, packageCommand: parsePackageCommand(withoutJson.slice(1)) };
  const key = withoutJson.join(" ");
  if (withoutJson.length === 3 && withoutJson[0] === "cleanup" && withoutJson[1] === "prime") {
    if (json) invalid("Prime cleanup uses the global `--output json` option before `cli`.");
    return { kind: "operation", global, operation: "prime-cleanup", json, value: withoutJson[2]! };
  }
  if (key === "doctor") return { kind: "operation", global, operation: "doctor", json };
  if (key === "harness list") return { kind: "operation", global, operation: "harness-list", json };
  if (key === "config explain") return { kind: "operation", global, operation: "config-explain", json };
  if (key === "org list") return { kind: "operation", global, operation: "org-list", json };
  if (key === "auth status") return { kind: "operation", global, operation: "auth-status", json };
  if (key === "auth login") return { kind: "operation", global, operation: "auth-login", json };
  if (key === "auth logout") return { kind: "operation", global, operation: "auth-logout", json };
  return { kind: "service", global, command: unknown(args, full) };
}

/**
 * Moves `--output` (separate or `=` value) found
 * after an account or package command path into the globals, the way service
 * verbs read them. The same value before and after `cli` is
 * accepted; different values are an invocation error. Other tokens are
 * returned in order for the command's own parser.
 */
function takeTrailingGlobals(global: GlobalFlags, tokens: readonly string[]): string[] {
  const rest: string[] = [];
  for (let index = 0; index < tokens.length;) {
    const token = tokens[index]!;
    const equals = token.indexOf("=");
    const name = equals >= 0 ? token.slice(0, equals) : token;
    if (name !== "--output") {
      rest.push(token);
      index += 1;
      continue;
    }
    const value = equals >= 0 ? token.slice(equals + 1) : tokens[index + 1];
    if (value === undefined) invalid(`${name} requires a value.`);
    index += equals >= 0 ? 1 : 2;
    const twice = (): never => { throw withInvocationAction(invocationFailure(`${name} was given twice with different values`), `Pass ${name} once, with the value you mean.`); };
    const before = global.output;
    setValue(global, "output", value, name);
    if (before !== undefined && before !== global.output) twice();
  }
  return rest;
}

function withInvocationAction(error: RunnerFailure, action: string): RunnerFailure {
  return new RunnerFailure({ code: error.code, boundary: error.boundary, message: error.message, action, exitCode: error.exitCode, retryable: error.retryable, ...(error.details === undefined ? {} : { details: error.details }) });
}

function parseHarnessUse(global: GlobalFlags, args: readonly string[]): ParsedEntrypoint {
  const harness = args[0];
  if (harness === undefined || !["openprose", "prime", "omp", "codex", "claude"].includes(harness)) {
    invalid("Harness selection must be one of openprose, prime, omp, codex, or claude.");
  }

  let json = false;
  let index = 1;
  while (index < args.length) {
    const token = args[index]!;
    if (token === "--json") {
      if (json) invalid("--json may be supplied only once for harness selection.");
      json = true;
      index += 1;
      continue;
    }
    const equals = token.indexOf("=");
    const option = equals >= 0 ? token.slice(0, equals) : token;
    const key = option === "--model" ? "model" : option === "--auth-profile" ? "authProfile" : undefined;
    if (key === undefined) {
      invalid(`Unknown harness-selection option: ${option}.`);
    }
    if (global[key] !== undefined) {
      invalid(`runner option ${option} was specified more than once`);
    }
    const value = equals >= 0 ? token.slice(equals + 1) : args[index + 1];
    if (value === undefined) invalid(`${option} requires a value.`);
    setValue(global, key, value, option);
    index += equals >= 0 ? 1 : 2;
  }
  return { kind: "operation", global, operation: "harness-use", json, value: harness };
}

function knownRunnerHelpPath(args: readonly string[]): boolean {
  const key = args.join(" ");
  if (new Set([
    "--help",
    "doctor --help",
    "harness --help",
    "harness list --help",
    "harness use --help",
    "cleanup --help",
    "cleanup prime --help",
    "config --help",
    "config explain --help",
  ]).has(key)) return true;
  return args.length === 4
    && (
      (args[0] === "harness" && args[1] === "use")
      || (args[0] === "cleanup" && args[1] === "prime")
    )
    && args[2]!.length > 0
    && args[3] === "--help";
}

export function inferOutputMode(args: readonly string[]): OutputMode {
  let index = 0;
  let mode: OutputMode = "human";
  while (index < args.length) {
    const token = args[index]!;
    if (token === "--") break;
    if (token === "--output") {
      const candidate = args[index + 1];
      if (candidate === "human" || candidate === "json" || candidate === "jsonl") mode = candidate;
      index += 2;
      continue;
    }
    if (token.startsWith("--output=")) {
      const candidate = token.slice("--output=".length);
      if (candidate === "human" || candidate === "json" || candidate === "jsonl") mode = candidate;
      index += 1;
      continue;
    }
    if (valueOptions[token] !== undefined) { index += 2; continue; }
    if (token.includes("=") && valueOptions[token.slice(0,token.indexOf("="))] !== undefined) { index += 1; continue; }
    if (token === "--dry-run" || token === "--no-color" || token === "--verbose") {
      index += 1;
      continue;
    }
    // `cli` is the one unescaped runner-owned noun. Honor its conventional
    // trailing machine-output flag even when command parsing itself fails, so
    // malformed local commands still produce one structured runner error.
    if (token === "cli" && args.at(-1) === "--json") return "json";
    break;
  }
  return mode;
}
