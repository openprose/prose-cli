import { PUBLISHED_KERNEL_STARTUP } from "./build";
import {nativeOutputLimits,validateNativeOutputBytes} from "../adapters/output-budget";
import {nativeLimits} from "../adapters/sdk-limits";
import { access, chmod, lstat, mkdir, readFile, realpath, rename, stat, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { dirname, join, parse, posix, resolve, win32 } from "node:path";
import type { GlobalFlags, EffectiveConfiguration, EffectiveValues, SourceKind, ValueSource } from "./types";
import { failure } from "./errors";
import { RunnerFailure } from "./types";
import { validateNativeConfiguration } from "../adapters/native-profile";

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

const environmentKeyMap: Record<string, ConfigKey> = {
  PROSE_HARNESS: "harness",
  PROSE_TRANSPORT: "transport",
  PROSE_MODEL: "model",
  PROSE_TIMEOUT: "timeout",
  PROSE_OUTPUT: "output",
  PROSE_COLOR: "color",
  PROSE_VERBOSE: "verbose",
  PROSE_AUTH_PROFILE: "authProfile",
  PROSE_NATIVE_PROFILE: "nativeProfile",
  PROSE_NATIVE_MAX_TURNS: "nativeMaxTurns",
  PROSE_NATIVE_TIMEOUT: "nativeTimeout",
  PROSE_NATIVE_TOOL_TIMEOUT: "nativeToolTimeout",
  PROSE_NATIVE_OUTPUT_BYTES: "nativeOutputBytes",
  PROSE_NATIVE_LOG: "nativeLog",
  PROSE_OUTPUT_CONTRACT: "outputContract",
  PROSE_PERMISSION_MODE: "permissionMode",
};

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
  let cwd: string;
  try {
    const info = await stat(requestedCwd);
    if (!info.isDirectory()) fail(`Working directory is not a directory: ${requestedCwd}.`, "--cwd");
    cwd = await realpath(requestedCwd);
  } catch (error) {
    if (error instanceof RunnerFailure) throw error;
    fail(`Working directory does not exist or cannot be read: ${requestedCwd}.`, "--cwd");
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

  const user = await readConfigIfPresent(userConfigPath, true);
  apply(values, sources, user.values, "user-config", user.locations);
  if (projectConfigPath !== null) {
    const project = await readConfigIfPresent(projectConfigPath);
    apply(values, sources, project.values, "project-config", project.locations);
  }

  const environment = parseEnvironment(dependencies.env);
  apply(values, sources, environment.values, "environment", environment.locations);
  const invocation = parseFlags(flags);
  apply(values, sources, invocation.values, "flag", invocation.locations);

  nativeLimits(values);
  nativeOutputLimits(values);
  if (values.nativeProfile !== undefined || values.nativeAddDirs !== undefined || values.nativeAllowTools !== undefined) {
    await validateNativeConfiguration(values, cwd);
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

async function discoverProjectConfig(cwd: string): Promise<string | null> {
  let directory = cwd;
  while (true) {
    const candidate = join(directory, ".prose", "cli.toml");
    if (await exists(candidate)) return candidate;
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

export type ServiceEnvironment = "production" | "staging";

interface ParsedValues {
  serviceEnvironment?: ServiceEnvironment;
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

function assignFileValue(
  values: PartialValues,
  key: ConfigKey,
  rawKey: string,
  value: string | boolean | string[],
  location: string,
): void {
  if (key === "nativeAddDirs" || key === "nativeAllowTools") { assignValidated(values,key,value,location); return; }
  const requiresBoolean = key === "color" || key === "verbose";
  if (requiresBoolean && typeof value !== "boolean") {
    fail(`Configuration key ${rawKey} requires a boolean value.`, location);
  }
  if (!requiresBoolean && typeof value !== "string") {
    fail(`Configuration key ${rawKey} requires a string value.`, location);
  }
  if (typeof value === "string" && value.length === 0) {
    fail(`Configuration key ${rawKey} must not be empty.`, location);
  }
  if (key === "harness" && !supportedHarnesses.has(value as string)) {
    fail("Configuration key harness contains an unsupported value.", location);
  }
  if (key === "output" && value !== "human" && value !== "json" && value !== "jsonl") {
    fail("Configuration key output contains an unsupported value.", location);
  }
  if (key === "timeout" && !/^[1-9][0-9]*(?:ms|s|m|h)$/u.test(value as string)) {
    fail("Configuration key timeout contains an invalid duration.", location);
  }
  assignValidated(values, key, value, location);
}

function parseFlatToml(source: string, path: string, allowService = true): ParsedValues {
  let serviceEnvironment: ServiceEnvironment | undefined;
  const values: PartialValues = {};
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
    if (key === undefined && rawKey !== "service_environment") configLineFailure(path, lineNumber, "Configuration contains an unknown key.");
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
    if (rawKey === "service_environment") {
      if (!allowService) configLineFailure(path, lineNumber, "Service environment is only allowed in user configuration.");
      if (parsed !== "production" && parsed !== "staging") configLineFailure(path, lineNumber, "Service environment must be production or staging.");
      serviceEnvironment = parsed;
    } else {
      assignFileValue(values, key!, rawKey, parsed, location);
      locations[key!] = location;
    }
  }
  return { values, locations, ...(serviceEnvironment === undefined ? {} : { serviceEnvironment }) };
}

async function readConfigIfPresent(path: string, allowService = false): Promise<ParsedValues> {
  if (!(await exists(path))) return { values: {}, locations: {} };
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
  for (const [name, key] of Object.entries(environmentKeyMap)) {
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
  for (const key of ["harness", "transport", "model", "authProfile", "permissionMode", "nativeProfile", "nativeMaxTurns", "nativeTimeout", "nativeToolTimeout", "nativeOutputBytes", "nativeAddDirs", "nativeAllowTools", "outputContract", "nativeLog", "timeout", "output", "color", "verbose"] as const) {
    const value = flags[key];
    if (value === undefined) continue;
    const location = key === "outputContract" ? "--output-contract" : key === "nativeMaxTurns" ? "--native-max-turns" : key === "nativeTimeout" ? "--native-timeout" : key === "nativeToolTimeout" ? "--native-tool-timeout" : key === "nativeOutputBytes" ? "--native-output-bytes" : key === "nativeProfile" ? "--native-profile" : key === "nativeAddDirs" ? "--native-add-dir" : key === "nativeAllowTools" ? "--native-allow-tool" : key === "authProfile" ? "--auth-profile" : key === "permissionMode" ? "--permission-mode" : `--${key}`;
    assignValidated(values, key, value, location);
    locations[key] = location;
  }
  return { values, locations };
}

function assignValidated(values: PartialValues, key: ConfigKey, raw: string | boolean | string[], location: string): void {
  if (key === "nativeAddDirs" || key === "nativeAllowTools") {
    if (!Array.isArray(raw) || raw.some(v=>typeof v!=="string" || !v.trim() || v.includes("\0"))) fail(`${key} must be an array of nonempty strings.`,location);
    values[key]=[...raw]; return;
  }
  if (key === "color" || key === "verbose") {
    const parsed = typeof raw === "boolean" ? raw : typeof raw === "string" ? parseBoolean(raw, location) : fail("Expected boolean",location);
    values[key] = parsed;
    return;
  }
  if (typeof raw !== "string") fail(`${key} must be a string.`, location);
  if (raw.length === 0) fail(`${key} must not be empty.`, location);
  if (key === "output") {
    if (raw !== "human" && raw !== "json" && raw !== "jsonl") fail("output must be human, json, or jsonl.", location);
    values.output = raw;
    return;
  }
  if (key === "timeout") {
    if (!/^[1-9][0-9]*(?:ms|s|m|h)$/u.test(raw)) fail("timeout must be a positive duration such as 30s or 10m.", location);
    values.timeout = raw;
    return;
  }
  if (key === "harness") {
    if (!supportedHarnesses.has(raw)) {
      fail(`Unsupported harness ${JSON.stringify(raw)}; expected openprose, prime, omp, codex, claude, or mock.`, location);
    }
    values.harness = raw;
    return;
  }
  if (key === "nativeOutputBytes") { validateNativeOutputBytes(raw); values[key]=raw; return; }
  if (key === "nativeMaxTurns" || key === "nativeTimeout" || key === "nativeToolTimeout") { values[key]=raw; nativeLimits({...values,harness:"agents-sdk"}); return; }
  if (key === "nativeProfile") {
    if (!["default","claude-workspace-tools"].includes(raw)) fail("Unknown native profile.",location);
    values.nativeProfile=raw;
  }
  else if (key === "model") values.model = raw;
  else if (key === "authProfile") values.authProfile = raw;
  else if (key === "nativeLog") values.nativeLog = raw;
  else if (key === "outputContract") {
    if (raw !== "native" && raw !== "image-envelope") fail("Output contract must be native or image-envelope.");
    values.outputContract = raw;
  }
  else if (key === "permissionMode") {
    if (!["default","acceptEdits","workspace-write","read-only"].includes(raw)) fail("Permission mode must be default, acceptEdits, workspace-write, or read-only.");
    values.permissionMode = raw;
  }
  else if (key === "transport") values.transport = raw;
}

function parseBoolean(raw: string, location: string): boolean {
  if (raw === "true" || raw === "1") return true;
  if (raw === "false" || raw === "0") return false;
  fail("Expected true, false, 1, or 0.", location);
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

export async function resolveServiceEnvironment(dependencies: ConfigDependencies): Promise<{ environment: ServiceEnvironment; source: "default" | "user-config"; path: string }> {
  const path = dependencies.userConfigPath ?? defaultUserConfigPath(dependencies);
  const pathApi = (dependencies.platform ?? process.platform) === "win32" ? win32 : posix;
  if (!pathApi.isAbsolute(path)) fail("OpenProse user configuration path must be absolute.");
  const parsed = await readConfigIfPresent(path, true);
  return { environment: parsed.serviceEnvironment ?? "production", source: parsed.serviceEnvironment === undefined ? "default" : "user-config", path };
}

export async function writeUserServiceEnvironment(path: string, environment: ServiceEnvironment | null): Promise<boolean> {
  return writeUserSelection(path, new Set(["service_environment"]), environment === null ? [] : [`service_environment = ${JSON.stringify(environment)}`]);
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
