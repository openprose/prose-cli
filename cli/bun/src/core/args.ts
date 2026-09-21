import { parsePackageCommand } from "./package-args";
import { invocationFailure } from "./errors";
import type { GlobalFlags, OutputMode, ParsedEntrypoint } from "./types";

const valueOptions: Record<string, keyof GlobalFlags> = {
  "--service-environment": "serviceEnvironment",
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

function invalid(message: string): never {
  throw invocationFailure(message);
}

function setValue(global: GlobalFlags, key: keyof GlobalFlags, value: string, option: string): void {
  if (key === "serviceEnvironment") {
    if (value !== "staging" && value !== "production") invalid("Service environment must be production or staging.");
    if (global.serviceEnvironment !== undefined) invalid("Service environment was specified more than once.");
    global.serviceEnvironment = value;
    return;
  }
  if (value.length === 0) invalid(`${option} requires a non-empty value.`);
  if (key === "nativeAddDirs" || key === "nativeAllowTools") { (global[key] ??= []).push(value); return; }
  if (key === "nativeMaxTurns" || key === "nativeTimeout" || key === "nativeToolTimeout" || key === "nativeOutputBytes") {global[key]=value;return;}
  if (key === "nativeProfile") { global.nativeProfile=value; return; }
  if (key === "output") {
    if (value !== "human" && value !== "json" && value !== "jsonl") {
      invalid(`invalid output mode ${JSON.stringify(value)}; expected human, json, or jsonl`);
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
  if (!forcedLanguage && tail[0] === "cli") return parseOperation(global, tail.slice(1));
  return { kind: "language", global, argv: ["prose", ...tail] };
}

function parseOperation(global: GlobalFlags, args: readonly string[]): ParsedEntrypoint {
  if (args[0] === "weave") return { kind: "weave", global, argv: [...args.slice(1)] };
  if (knownRunnerHelpPath(args)) return { kind: "help", global };
  if (args[0] === "harness" && args[1] === "use") {
    return parseHarnessUse(global, args.slice(2));
  }
  const withoutJson = [...args];
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
  if (key === "environment show") return { kind: "operation", global, operation: "environment-show", json };
  if (key === "environment reset") return { kind: "operation", global, operation: "environment-reset", json };
  if (withoutJson.length === 3 && withoutJson[0] === "environment" && withoutJson[1] === "use" && ["production", "staging"].includes(withoutJson[2]!)) return { kind: "operation", global, operation: "environment-use", json, value: withoutJson[2]! };
  if (key === "doctor") return { kind: "operation", global, operation: "doctor", json };
  if (key === "harness list") return { kind: "operation", global, operation: "harness-list", json };
  if (key === "config explain") return { kind: "operation", global, operation: "config-explain", json };
  if (key === "org list") return { kind: "operation", global, operation: "org-list", json };
  if (key === "auth status") return { kind: "operation", global, operation: "auth-status", json };
  if (key === "auth login") return { kind: "operation", global, operation: "auth-login", json };
  if (key === "auth logout") return { kind: "operation", global, operation: "auth-logout", json };
  invalid(`Unknown runner operation: cli${key.length === 0 ? "" : ` ${key}`}.`);
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
    "environment --help",
    "environment show --help",
    "environment use --help",
    "environment reset --help",
    "environment use staging --help",
    "environment use production --help",
    "package --help",
    "package publish --help",
    "package fetch --help",
    "package list --help",
    "package withdraw --help",
    "org --help",
    "org list --help",
    "auth --help",
    "auth status --help",
    "auth login --help",
    "auth logout --help",
  ]).has(key)) return true;
  return args.length === 4
    && (
      (args[0] === "harness" && args[1] === "use")
      || (args[0] === "package" && ["publish", "fetch", "list", "withdraw"].includes(args[1]!))
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
