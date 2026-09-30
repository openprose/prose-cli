import { PUBLISHED_KERNEL_STARTUP } from "./build";
import {nativeOutputLimits,validateNativeOutputBytes} from "../adapters/output-budget";
import {nativeLimits} from "../adapters/sdk-limits";
import { access, chmod, lstat, mkdir, readFile, realpath, rename, stat, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { dirname, join, parse, posix, resolve, win32 } from "node:path";
import type { GlobalFlags, EffectiveConfiguration, EffectiveValues, SourceKind, ValueSource } from "./types";
import { failure } from "./errors";
import { quote } from "./output";
import { RunnerFailure } from "./types";
import { nativeProfileArgv, validateNativeConfiguration } from "../adapters/native-profile";

export interface ConfigDependencies {
  processCwd: string;
  env: Readonly<Record<string, string | undefined>>;
  userConfigPath?: string;
  platform?: NodeJS.Platform;
  homeDir?: string;
}

type ConfigKey = keyof EffectiveValues;
type PartialValues = Partial<EffectiveValues>;

const fileKeyMap: Record<string, ConfigKey> = {
  harness: "harness",
  transport: "transport",
  model: "model",
  timeout: "timeout",
  output: "output",
  color: "color",
  verbose: "verbose",
  auth_profile: "authProfile",
  native_profile: "nativeProfile",
  native_max_turns: "nativeMaxTurns",
  native_timeout: "nativeTimeout",
  native_tool_timeout: "nativeToolTimeout",
  native_output_bytes: "nativeOutputBytes",
  native_add_dirs: "nativeAddDirs",
  native_allow_tools: "nativeAllowTools",
  native_log: "nativeLog",
  output_contract: "outputContract",
  permission_mode: "permissionMode",
};

/**
 * The configuration keys in their one validation order (the order of `values`
 * in shared/schemas/configuration-explanation.schema.json, with nativeLog
 * before authProfile), with each key's file key, variable and flag. The Rust
 * port validates in the same order.
 */
const SETTINGS: ReadonlyArray<readonly [ConfigKey, string, string | undefined, string]> = [
  ["harness", "harness", "PROSE_HARNESS", "--harness"],
  ["transport", "transport", "PROSE_TRANSPORT", "--transport"],
  ["model", "model", "PROSE_MODEL", "--model"],
  ["timeout", "timeout", "PROSE_TIMEOUT", "--timeout"],
  ["output", "output", "PROSE_OUTPUT", "--output"],
  ["color", "color", "PROSE_COLOR", "--no-color"],
  ["verbose", "verbose", "PROSE_VERBOSE", "--verbose"],
  ["outputContract", "output_contract", "PROSE_OUTPUT_CONTRACT", "--output-contract"],
  ["permissionMode", "permission_mode", "PROSE_PERMISSION_MODE", "--permission-mode"],
  ["nativeMaxTurns", "native_max_turns", "PROSE_NATIVE_MAX_TURNS", "--native-max-turns"],
  ["nativeTimeout", "native_timeout", "PROSE_NATIVE_TIMEOUT", "--native-timeout"],
  ["nativeToolTimeout", "native_tool_timeout", "PROSE_NATIVE_TOOL_TIMEOUT", "--native-tool-timeout"],
  ["nativeOutputBytes", "native_output_bytes", "PROSE_NATIVE_OUTPUT_BYTES", "--native-output-bytes"],
  ["nativeProfile", "native_profile", "PROSE_NATIVE_PROFILE", "--native-profile"],
  ["nativeAddDirs", "native_add_dirs", undefined, "--native-add-dir"],
  ["nativeAllowTools", "native_allow_tools", undefined, "--native-allow-tool"],
  ["nativeLog", "native_log", "PROSE_NATIVE_LOG", "--native-log"],
  ["authProfile", "auth_profile", "PROSE_AUTH_PROFILE", "--auth-profile"],
];

/** The longest accepted run timeout (maxTimeoutMs in shared/capabilities/transport-limits.v1.json). */
export const MAX_TIMEOUT_MS = 86_400_000;

/** Milliseconds of a run timeout, or the canonical reason it is rejected. */
export function timeoutMs(value: string): number | string {
  const match = /^([1-9][0-9]*)(ms|s|m|h)$/u.exec(value);
  if (match === null) return "timeout must be a positive duration such as 30s or 10m.";
  const unit = { ms: 1, s: 1000, m: 60_000, h: 3_600_000 }[match[2] as "ms" | "s" | "m" | "h"];
  const digits = match[1]!;
  // Compare as digits first: a huge number must not round to an accepted one.
  if (digits.length > 12 || Number(digits) * unit > MAX_TIMEOUT_MS) return "timeout must be at most 24h.";
  return Number(digits) * unit;
}

const defaults: EffectiveValues = {
  harness: "openprose",
  transport: "auto",
  model: null,
  timeout: "10m",
  output: "human",
  color: false,
  verbose: false,
  authProfile: null,
  outputContract: PUBLISHED_KERNEL_STARTUP ? "native" : "image-envelope",
  permissionMode: null,
};

const supportedHarnesses = new Set(["openprose", "agents-sdk", "prime", "omp", "codex", "claude", "mock"]);

function fail(message: string, source?: string): never {
  throw failure("CONFIG_INVALID", {
    reason: message,
    ...(source === undefined ? {} : { source }),
  });
}

export async function resolveConfiguration(
  flags: GlobalFlags,
  dependencies: ConfigDependencies,
): Promise<EffectiveConfiguration> {
  const requestedCwd = flags.cwd === undefined ? dependencies.processCwd : resolve(dependencies.processCwd, flags.cwd);
  const cwdLocation = flags.cwd === undefined ? "process cwd" : "--cwd";
  let cwd: string;
  try {
    const info = await stat(requestedCwd);
    if (!info.isDirectory()) fail(`Working directory is not a directory: ${requestedCwd}.`, cwdLocation);
    cwd = await realpath(requestedCwd);
  } catch (error) {
    if (error instanceof RunnerFailure) throw error;
    fail(`Working directory does not exist or cannot be read: ${requestedCwd}.`, cwdLocation);
  }

  const userConfigPath = dependencies.userConfigPath ?? defaultUserConfigPath(dependencies);
  const pathApi = (dependencies.platform ?? process.platform) === "win32" ? win32 : posix;
  if (userConfigPath.length === 0 || !pathApi.isAbsolute(userConfigPath)) {
    fail("OpenProse user configuration path must be absolute.");
  }
  const projectConfigPath = await discoverProjectConfig(cwd);
  const values: EffectiveValues = { ...defaults };
  const sources = Object.fromEntries(
    (Object.keys(defaults) as ConfigKey[]).map((key) => [key, { kind: "default", location: "built-in" }]),
  ) as { [K in ConfigKey]: ValueSource };

  // The same physical file is loaded once; in both roles the nearest
  // project role is authoritative.
  const userConfig = await regularFile(userConfigPath);
  if (userConfig !== null && userConfig !== projectConfigPath) {
    const user = await readConfig(userConfig, true);
    apply(values, sources, user.values, "user-config", user.locations);
  }
  if (projectConfigPath !== null) {
    const project = await readConfig(projectConfigPath);
    apply(values, sources, project.values, "project-config", project.locations);
  }

  const environment = parseEnvironment(dependencies.env);
  apply(values, sources, environment.values, "environment", environment.locations);
  const invocation = parseFlags(flags);
  apply(values, sources, invocation.values, "flag", invocation.locations);

  // The checks across keys, in the one order both ports use; each names the
  // source of the setting it rejects.
  const at = <T>(keys: ConfigKey[], check: () => T): T => {
    try { return check(); } catch (caught) {
      const location = keys.map((key) => sources[key]).find((source) => source.kind !== "default")?.location;
      if (caught instanceof RunnerFailure && caught.code === "CONFIG_INVALID" && location !== undefined && caught.details?.source === undefined) {
        throw failure("CONFIG_INVALID", { ...(caught.details ?? {}), source: location });
      }
      throw caught;
    }
  };
  at(["nativeMaxTurns", "nativeTimeout", "nativeToolTimeout"], () => nativeLimits(values));
  at(["nativeOutputBytes"], () => nativeOutputLimits(values));
  if (values.nativeProfile !== undefined || values.nativeAddDirs !== undefined || values.nativeAllowTools !== undefined) {
    const native: ConfigKey[] = ["nativeProfile", "nativeAddDirs", "nativeAllowTools"];
    at(native, () => nativeProfileArgv(values));
    try { await validateNativeConfiguration(values, cwd); }
    catch (caught) { at(["nativeAddDirs"], () => { throw caught; }); }
  }
  return {
    cwd,
    cwdSource: flags.cwd === undefined
      ? { kind: "default", location: "process cwd" }
      : { kind: "flag", location: "--cwd" },
    values,
    sources,
    projectConfigPath,
    userConfigPath,
  };
}

function defaultUserConfigPath(dependencies: ConfigDependencies): string {
  const platform = dependencies.platform ?? process.platform;
  const pathApi = platform === "win32" ? win32 : posix;
  const requireRoot = (value: string | undefined, name: string): string => {
    if (value === undefined || value.length === 0 || !pathApi.isAbsolute(value)) {
      fail(`${name} must be a non-empty absolute path to locate OpenProse user configuration.`);
    }
    return value;
  };
  const xdg = dependencies.env.XDG_CONFIG_HOME;
  if (xdg !== undefined) return pathApi.join(requireRoot(xdg, "XDG_CONFIG_HOME"), "openprose", "cli.toml");
  if (platform === "win32") {
    return pathApi.join(requireRoot(dependencies.env.APPDATA, "APPDATA"), "OpenProse", "cli.toml");
  }
  const home = requireRoot(dependencies.homeDir ?? dependencies.env.HOME, "HOME");
  if (platform === "darwin") return pathApi.join(home, "Library", "Application Support", "OpenProse", "cli.toml");
  return pathApi.join(home, ".config", "openprose", "cli.toml");
}

/** The canonical path of `path` when it is a regular file (symlinks followed), else null. */
async function regularFile(path: string): Promise<string | null> {
  try {
    if (!(await stat(path)).isFile()) return null;
  } catch { return null; }
  try { return await realpath(path); }
  catch { fail(`Cannot read configuration file: ${path}.`, path); }
}

async function discoverProjectConfig(cwd: string): Promise<string | null> {
  let directory = cwd;
  while (true) {
    const candidate = await regularFile(join(directory, ".prose", "cli.toml"));
    if (candidate !== null) return candidate;
    if (await exists(join(directory, ".git"))) return null;
    const parent = dirname(directory);
    if (parent === directory || directory === parse(directory).root) return null;
    directory = parent;
  }
}

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

interface ParsedValues {
  values: PartialValues;
  locations: Partial<Record<ConfigKey, string>>;
}

const CONFIG_ASSIGNMENT = /^([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*/u;

function configLineFailure(path: string, line: number, reason: string): never {
  fail(reason, `${path}:${line}`);
}

function invalidUtf8Offset(bytes: Uint8Array): number | null {
  const continuation = (index: number, minimum = 0x80, maximum = 0xbf): boolean => {
    const byte = bytes[index];
    return byte !== undefined && byte >= minimum && byte <= maximum;
  };
  let index = 0;
  while (index < bytes.length) {
    const lead = bytes[index]!;
    if (lead <= 0x7f) {
      index += 1;
      continue;
    }
    if (lead >= 0xc2 && lead <= 0xdf && continuation(index + 1)) {
      index += 2;
      continue;
    }
    if (lead === 0xe0 && continuation(index + 1, 0xa0) && continuation(index + 2)) {
      index += 3;
      continue;
    }
    if (((lead >= 0xe1 && lead <= 0xec) || (lead >= 0xee && lead <= 0xef))
      && continuation(index + 1) && continuation(index + 2)) {
      index += 3;
      continue;
    }
    if (lead === 0xed && continuation(index + 1, 0x80, 0x9f) && continuation(index + 2)) {
      index += 3;
      continue;
    }
    if (lead === 0xf0 && continuation(index + 1, 0x90) && continuation(index + 2) && continuation(index + 3)) {
      index += 4;
      continue;
    }
    if (lead >= 0xf1 && lead <= 0xf3
      && continuation(index + 1) && continuation(index + 2) && continuation(index + 3)) {
      index += 4;
      continue;
    }
    if (lead === 0xf4 && continuation(index + 1, 0x80, 0x8f)
      && continuation(index + 2) && continuation(index + 3)) {
      index += 4;
      continue;
    }
    return index;
  }
  return null;
}

function decodeConfiguration(bytes: Uint8Array, path: string): string {
  const invalidOffset = invalidUtf8Offset(bytes);
  if (invalidOffset !== null) {
    let line = 1;
    for (let index = 0; index < invalidOffset; index += 1) {
      if (bytes[index] === 0x0a) line += 1;
    }
    configLineFailure(path, line, "Configuration file is not valid UTF-8.");
  }
  return new TextDecoder("utf-8", { ignoreBOM: true }).decode(bytes);
}

function trailingContentIsAllowed(value: string, offset: number): boolean {
  let index = offset;
  while (value[index] === " " || value[index] === "\t") index += 1;
  return index === value.length || value[index] === "#";
}

function parseBasicString(value: string, path: string, line: number): { value: string; end: number } {
  let decoded = "";
  let index = 1;
  while (index < value.length) {
    const character = value[index]!;
    if (character === "\"") return { value: decoded, end: index + 1 };
    const codePoint = character.codePointAt(0)!;
    if (codePoint <= 0x1f || codePoint === 0x7f) {
      configLineFailure(path, line, "Configuration strings cannot contain control characters.");
    }
    if (character !== "\\") {
      decoded += character;
      index += character.length;
      continue;
    }
    const escape = value[index + 1];
    if (escape === undefined) {
      configLineFailure(path, line, "Basic string contains an unsupported escape.");
    }
    const namedEscapes: Record<string, string> = {
      "\"": "\"",
      "\\": "\\",
      b: "\b",
      t: "\t",
      n: "\n",
      f: "\f",
      r: "\r",
    };
    if (Object.hasOwn(namedEscapes, escape)) {
      decoded += namedEscapes[escape]!;
      index += 2;
      continue;
    }
    if (escape === "u" || escape === "U") {
      const width = escape === "u" ? 4 : 8;
      const digits = value.slice(index + 2, index + 2 + width);
      if (digits.length !== width || !/^[0-9A-Fa-f]+$/u.test(digits)) {
        configLineFailure(path, line, "Basic string contains an invalid Unicode escape.");
      }
      const escapedCodePoint = Number.parseInt(digits, 16);
      if (escapedCodePoint > 0x10ffff || (escapedCodePoint >= 0xd800 && escapedCodePoint <= 0xdfff)) {
        configLineFailure(path, line, "Basic string contains an invalid Unicode escape.");
      }
      decoded += String.fromCodePoint(escapedCodePoint);
      index += 2 + width;
      continue;
    }
    configLineFailure(path, line, "Basic string contains an unsupported escape.");
  }
  configLineFailure(path, line, "Basic string must close on the same physical line.");
}

function parseLiteralString(value: string, path: string, line: number): { value: string; end: number } {
  let decoded = "";
  for (let index = 1; index < value.length; index += 1) {
    const character = value[index]!;
    if (character === "'") return { value: decoded, end: index + 1 };
    const codePoint = character.codePointAt(0)!;
    if (codePoint <= 0x1f || codePoint === 0x7f) {
      configLineFailure(path, line, "Configuration strings cannot contain control characters.");
    }
    decoded += character;
  }
  configLineFailure(path, line, "Literal string must close on the same physical line.");
}

/** Checks one file value's type (in line order, with the syntax). */
function checkFileType(key: ConfigKey, rawKey: string, value: string | boolean | string[], location: string): void {
  if (key === "nativeAddDirs" || key === "nativeAllowTools") return;
  const requiresBoolean = key === "color" || key === "verbose";
  if (requiresBoolean && typeof value !== "boolean") fail(`Configuration key ${rawKey} requires a boolean value.`, location);
  if (!requiresBoolean && typeof value !== "string") fail(`Configuration key ${rawKey} requires a string value.`, location);
}

/** Validates the file's values in the one key order, with fixed reasons that never echo a value. */
function validateFileValues(raw: Partial<Record<ConfigKey, string | boolean | string[]>>, locations: Partial<Record<ConfigKey, string>>): PartialValues {
  const values: PartialValues = {};
  for (const [key, rawKey] of SETTINGS) {
    const value = raw[key];
    if (value === undefined) continue;
    const location = locations[key]!;
    if (typeof value === "string" && value.length === 0) fail(`Configuration key ${rawKey} must not be empty.`, location);
    if (key === "harness" && !supportedHarnesses.has(value as string)) fail("Configuration key harness contains an unsupported value.", location);
    if (key === "output" && value !== "human" && value !== "json" && value !== "jsonl") fail("Configuration key output contains an unsupported value.", location);
    if (key === "timeout" && typeof timeoutMs(value as string) === "string") fail("Configuration key timeout contains an invalid duration.", location);
    assignValidated(values, key, value, location);
  }
  return values;
}

/**
 * `service_environment` is a retired user-configuration key: earlier clients
 * saved a service selection there. The client now always talks to the
 * OpenProse service, so a user configuration that still carries the line is
 * accepted and the value ignored; a project configuration never allowed it.
 */
const RETIRED_USER_KEY = "service_environment";

function parseFlatToml(source: string, path: string, allowService = true): ParsedValues {
  const rawValues: Partial<Record<ConfigKey, string | boolean | string[]>> = {};
  const locations: Partial<Record<ConfigKey, string>> = {};
  const seen = new Set<string>();
  for (const [offset, rawPhysicalLine] of source.split("\n").entries()) {
    const lineNumber = offset + 1;
    const raw = rawPhysicalLine.endsWith("\r") ? rawPhysicalLine.slice(0, -1) : rawPhysicalLine;
    if (raw.includes("\r")) {
      configLineFailure(path, lineNumber, "Configuration line contains an unsupported carriage return.");
    }
    const line = raw.replace(/^[ \t]*/u, "");
    if (line.length === 0 || line.startsWith("#")) continue;
    const match = CONFIG_ASSIGNMENT.exec(line);
    if (match === null) {
      configLineFailure(path, lineNumber, "Configuration line must contain one known bare-key assignment.");
    }
    const rawKey = match[1]!;
    const key = Object.hasOwn(fileKeyMap, rawKey) ? fileKeyMap[rawKey] : undefined;
    if (key === undefined && rawKey !== RETIRED_USER_KEY) configLineFailure(path, lineNumber, "Configuration contains an unknown key.");
    if (seen.has(rawKey)) configLineFailure(path, lineNumber, `Duplicate configuration key: ${rawKey}.`);
    seen.add(rawKey);
    const rawValue = line.slice(match[0].length);
    let parsed: string | boolean | string[];
    let end: number;
    if (rawValue.startsWith("[") && (key === "nativeAddDirs" || key === "nativeAllowTools")) {
      ({value:parsed,end}=parseStringArray(rawValue,path,lineNumber));
    } else if (rawValue.startsWith("\"")) {
      ({ value: parsed, end } = parseBasicString(rawValue, path, lineNumber));
    } else if (rawValue.startsWith("'")) {
      ({ value: parsed, end } = parseLiteralString(rawValue, path, lineNumber));
      if (!trailingContentIsAllowed(rawValue, end)) {
        configLineFailure(path, lineNumber, "Literal strings cannot contain apostrophes.");
      }
    } else if (rawValue.startsWith("true")) {
      parsed = true;
      end = 4;
    } else if (rawValue.startsWith("false")) {
      parsed = false;
      end = 5;
    } else {
      configLineFailure(path, lineNumber, "Configuration value must be true, false, or a single-line string.");
    }
    if (!trailingContentIsAllowed(rawValue, end)) {
      configLineFailure(path, lineNumber, "Unexpected content after configuration value.");
    }
    const location = `${path}:${lineNumber}`;
    if (rawKey === RETIRED_USER_KEY) {
      if (!allowService) configLineFailure(path, lineNumber, "Configuration contains an unknown key.");
    } else {
      checkFileType(key!, rawKey, parsed, location);
      rawValues[key!] = parsed;
      locations[key!] = location;
    }
  }
  return { values: validateFileValues(rawValues, locations), locations };
}

async function readConfig(path: string, allowService = false): Promise<ParsedValues> {
  let text: string;
  try {
    text = decodeConfiguration(await readFile(path), path);
  } catch (caught) {
    if (caught instanceof RunnerFailure) throw caught;
    fail(`Cannot read configuration file: ${path}.`, path);
  }
  return parseFlatToml(text, path, allowService);
}

function parseEnvironment(env: Readonly<Record<string, string | undefined>>): ParsedValues {
  const values: PartialValues = {};
  const locations: Partial<Record<ConfigKey, string>> = {};
  for (const [key, , name] of SETTINGS) {
    if (name === undefined) continue;
    const value = env[name];
    if (value === undefined) continue;
    assignValidated(values, key, value, name);
    locations[key] = name;
  }
  return { values, locations };
}

function parseFlags(flags: GlobalFlags): ParsedValues {
  const values: PartialValues = {};
  const locations: Partial<Record<ConfigKey, string>> = {};
  for (const [key, , , location] of SETTINGS) {
    const value = flags[key as keyof GlobalFlags] as string | boolean | string[] | undefined;
    if (value === undefined) continue;
    assignValidated(values, key, value, location);
    locations[key] = location;
  }
  return { values, locations };
}

/** Validates one environment, flag or file value of `key`; every failure names `location`. */
function assignValidated(values: PartialValues, key: ConfigKey, raw: string | boolean | string[], location: string): void {
  if (key === "nativeAddDirs" || key === "nativeAllowTools") {
    if (!Array.isArray(raw) || raw.some(v=>typeof v!=="string" || !v.trim() || v.includes("\0"))) fail(`${key} must be an array of nonempty strings.`,location);
    values[key]=[...raw]; return;
  }
  if (Array.isArray(raw)) fail(`${key} must be a string.`, location);
  if (typeof raw === "string" && raw.includes("\ufffd")) fail(`${key} must be valid UTF-8.`, location);
  if (key === "color" || key === "verbose") {
    values[key] = typeof raw === "boolean" ? raw : parseBoolean(key, raw, location);
    return;
  }
  if (typeof raw !== "string") fail(`${key} must be a string.`, location);
  if (raw.length === 0) fail(`${key} must not be empty.`, location);
  const at = (check: () => unknown) => {
    try { check(); } catch (caught) {
      if (caught instanceof RunnerFailure && caught.code === "CONFIG_INVALID") throw failure("CONFIG_INVALID", { ...(caught.details ?? {}), source: location });
      throw caught;
    }
  };
  if (key === "output") {
    if (raw !== "human" && raw !== "json" && raw !== "jsonl") fail("output must be human, json, or jsonl.", location);
    values.output = raw;
    return;
  }
  if (key === "timeout") {
    const parsed = timeoutMs(raw);
    if (typeof parsed === "string") fail(parsed, location);
    values.timeout = raw;
    return;
  }
  if (key === "harness") {
    if (!supportedHarnesses.has(raw)) {
      fail(`Unsupported harness ${quote(raw)}; expected openprose, prime, omp, codex, claude, or mock.`, location);
    }
    values.harness = raw;
    return;
  }
  if (key === "nativeOutputBytes") { at(() => validateNativeOutputBytes(raw)); values[key]=raw; return; }
  if (key === "nativeMaxTurns" || key === "nativeTimeout" || key === "nativeToolTimeout") { at(() => nativeLimits({ harness: "agents-sdk", [key]: raw })); values[key]=raw; return; }
  if (key === "nativeProfile") {
    if (!["default","claude-workspace-tools"].includes(raw)) fail("Unknown native profile.",location);
    values.nativeProfile=raw;
  }
  else if (key === "model") values.model = raw;
  else if (key === "authProfile") values.authProfile = raw;
  else if (key === "nativeLog") values.nativeLog = raw;
  else if (key === "outputContract") {
    if (raw !== "native" && raw !== "image-envelope") fail("Output contract must be native or image-envelope.", location);
    values.outputContract = raw;
  }
  else if (key === "permissionMode") {
    if (!["default","acceptEdits","workspace-write","read-only"].includes(raw)) fail("Permission mode must be default, acceptEdits, workspace-write, or read-only.", location);
    values.permissionMode = raw;
  }
  else if (key === "transport") values.transport = raw;
}

function parseBoolean(key: string, raw: string, location: string): boolean {
  if (raw === "true" || raw === "1") return true;
  if (raw === "false" || raw === "0") return false;
  fail(`${key} must be true, false, 1, or 0.`, location);
}

function apply(
  values: EffectiveValues,
  sources: { [K in ConfigKey]: ValueSource },
  overlay: PartialValues,
  kind: SourceKind,
  locations: Partial<Record<ConfigKey, string>>,
): void {
  for (const key of Object.keys(overlay) as ConfigKey[]) {
    const value = overlay[key];
    if (value === undefined) continue;
    Object.assign(values, { [key]: value });
    sources[key] = { kind, location: locations[key] ?? kind };
  }
}

export interface UserHarnessSelection {
  model: string | null;
  authProfile: string | null;
}

async function secureUserConfigParent(parent: string): Promise<void> {
  try {
    await mkdir(parent, { recursive: true, mode: 0o700 });
  } catch {
    fail("OpenProse user configuration parent must be a real directory, not a symlink.", parent);
  }
  let parentInfo: Awaited<ReturnType<typeof lstat>>;
  try {
    parentInfo = await lstat(parent);
  } catch {
    fail("OpenProse user configuration parent cannot be authenticated.", parent);
  }
  if (!parentInfo.isDirectory() || parentInfo.isSymbolicLink()) {
    fail("OpenProse user configuration parent must be a real directory, not a symlink.", parent);
  }
  try {
    await chmod(parent, 0o700);
  } catch {
    fail("OpenProse user configuration parent cannot be secured for owner-only access.", parent);
  }
  let securedParentInfo: Awaited<ReturnType<typeof lstat>>;
  try {
    securedParentInfo = await lstat(parent);
  } catch {
    fail("OpenProse user configuration parent changed before it could be secured.", parent);
  }
  if (!securedParentInfo.isDirectory() || securedParentInfo.isSymbolicLink()) {
    fail("OpenProse user configuration parent changed before it could be secured.", parent);
  }
}

export async function writeUserHarnessSelection(
  path: string,
  harness: string,
  selection: UserHarnessSelection,
): Promise<boolean> {
  if (!supportedHarnesses.has(harness) || harness === "mock") {
    fail(`Unsupported harness selection: ${harness}.`, path);
  }
  if (selection.model !== null && selection.model.length === 0) fail("Saved model must not be empty.", path);
  if (selection.authProfile !== null && selection.authProfile.length === 0) fail("Saved auth profile must not be empty.", path);
  return writeUserSelection(path, new Set(["harness", "model", "auth_profile"]), [
    ...(selection.authProfile === null ? [] : [`auth_profile = ${JSON.stringify(selection.authProfile)}`]),
    `harness = ${JSON.stringify(harness)}`,
    ...(selection.model === null ? [] : [`model = ${JSON.stringify(selection.model)}`]),
  ]);
}

async function writeUserSelection(path: string, targetKeys: Set<string>, bundle: string[]): Promise<boolean> {
  const parent = dirname(path);
  await secureUserConfigParent(parent);

  let original = "";
  try {
    const info = await lstat(path);
    if (!info.isFile() || info.isSymbolicLink()) {
      fail("OpenProse user configuration must be a regular non-symlink file.", path);
    }
    original = decodeConfiguration(await readFile(path), path);
  } catch (caught) {
    if (caught instanceof RunnerFailure) throw caught;
    if ((caught as NodeJS.ErrnoException).code !== "ENOENT") {
      fail("OpenProse user configuration cannot be read safely.", path);
    }
  }

  const lines = original.length === 0 ? [] : original.replace(/\n$/u, "").split("\n");
  if (original.length > 0) parseFlatToml(original, path);
  const seen = new Set<string>();
  let insertionIndex: number | null = null;
  const updated: string[] = [];
  for (const line of lines) {
    const assignment = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/u.exec(line);
    const key = assignment?.[1];
    if (key === undefined || !targetKeys.has(key)) {
      updated.push(line);
      continue;
    }
    if (seen.has(key)) fail(`Duplicate ${key} key in OpenProse user configuration.`, path);
    seen.add(key);
    if (insertionIndex === null) insertionIndex = updated.length;
  }
  updated.splice(insertionIndex ?? updated.length, 0, ...bundle);
  const next = updated.length === 0 ? "" : `${updated.join("\n")}\n`;
  if (next === original) return false;

  const temporary = join(parent, `.cli.toml.openprose-${process.pid}-${randomUUID()}.tmp`);
  try {
    await writeFile(temporary, next, { encoding: "utf8", mode: 0o600, flag: "wx" });
    await chmod(temporary, 0o600);
    try {
      const current = await lstat(path);
      if (!current.isFile() || current.isSymbolicLink()) {
        fail("OpenProse user configuration changed to an unsafe file before replacement.", path);
      }
    } catch (caught) {
      if (caught instanceof RunnerFailure) throw caught;
      if ((caught as NodeJS.ErrnoException).code !== "ENOENT") throw caught;
    }
    await secureUserConfigParent(parent);
    await rename(temporary, path);
  } catch (caught) {
    try { await unlink(temporary); } catch { /* The temporary file was never created or already renamed. */ }
    if (caught instanceof RunnerFailure) throw caught;
    fail("OpenProse user configuration could not be written atomically.", path);
  }
  return true;
}

function parseStringArray(raw:string,path:string,line:number):{value:string[];end:number} {
  const value:string[]=[];let cursor=1;
  for (;;) {
    while (/\s/.test(raw[cursor]??"") && cursor<raw.length) cursor++;
    if(raw[cursor]==="]") return {value,end:cursor+1};
    const rest=raw.slice(cursor);
    const parsed=rest.startsWith('"')?parseBasicString(rest,path,line):rest.startsWith("'")?parseLiteralString(rest,path,line):configLineFailure(path,line,"Array entries must be strings.");
    value.push(parsed.value);cursor+=parsed.end;
    while (/\s/.test(raw[cursor]??"") && cursor<raw.length) cursor++;
    if(raw[cursor]==="]") return {value,end:cursor+1};
    if(raw[cursor]!==",") configLineFailure(path,line,"Expected comma or array end.");
    cursor++;
  }
}
