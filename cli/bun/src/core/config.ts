import { harnessById } from "./harnesses";
import { installedAdapterDefinition } from "../adapters/recipes";
import type { InstalledAdapterId } from "../adapters/types";
import { PUBLISHED_KERNEL_STARTUP } from "./build";
import {nativeOutputLimits,validateNativeOutputBytes} from "../adapters/output-budget";
import {nativeLimits} from "../adapters/sdk-limits";
import { access, chmod, lstat, mkdir, readFile, realpath, rename, stat, unlink, writeFile, link } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { dirname, join, parse, posix, resolve, win32 } from "node:path";
import type { GlobalFlags, EffectiveConfiguration, EffectiveValues, SourceKind, ValueSource } from "./types";
import { failure } from "./errors";
import { quote, configurationExplanation } from "./output";
import { RunnerFailure } from "./types";
import { nativeProfileArgv, validateNativeConfiguration } from "../adapters/native-profile";

export interface ConfigDependencies {
  processCwd: string;
  env: Readonly<Record<string, string | undefined>>;
  userConfigPath?: string;
  platform?: NodeJS.Platform;
  homeDir?: string;
  targetArgv?: string[];
  /** Pure target-bundle validation after source parsing, before execution-route checks. */
  validateSelection?: () => void;
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
  codex_compatibility: "codexCompatibility",
};

export const CONFIGURATION_KEYS = Object.keys(fileKeyMap);

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
  ["codexCompatibility", "codex_compatibility", "PROSE_CODEX_COMPATIBILITY", "--codex-compatibility"],
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
  harness: "agents-sdk",
  transport: "auto",
  model: null,
  timeout: "10m",
  output: "human",
  color: false,
  verbose: false,
  authProfile: null,
  outputContract: PUBLISHED_KERNEL_STARTUP ? "native" : "image-envelope",
  permissionMode: null,
  codexCompatibility: "qualified",
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
  try {return await resolveConfigured(flags,dependencies);}
  catch(caught) {if(caught instanceof SelectionValidationFailure)throw caught.original;throw earlyConfigurationFailure(caught,dependencies);}
}

/** Selection errors contain only the independently validated target, never unresolved source values. */
class SelectionValidationFailure extends Error {
  constructor(readonly original:unknown){super("Target harness selection is invalid.");}
}

function earlyConfigurationFailure(caught:unknown,dependencies:ConfigDependencies):unknown {
    if(!(caught instanceof RunnerFailure) || caught.code!=="CONFIG_INVALID" || caught.details?.configurationExplanation!==undefined)throw caught;
    const source=String(caught.details?.source ?? "configuration");
    const reason=String(caught.details?.reason ?? "Runner configuration is invalid.");
    const values=Object.fromEntries(SETTINGS.map(([key])=>[key,{value:defaults[key] ?? (key==="nativeProfile"?"default":["nativeAddDirs","nativeAllowTools"].includes(key)?[]:null),source:{kind:"default",location:"built-in"}}]));
    const candidates=Object.fromEntries(Object.entries(values).map(([key,entry])=>[key,[{...entry,selected:true}]]));
    return failure("CONFIG_INVALID",{...caught.details,configurationExplanation:{
      schema:"openprose.configuration-explanation/1",
      cwd:{value:dependencies.processCwd,source:{kind:"default",location:"process cwd"}},
      projectConfigPath:null,userConfigPath:null,values,
      target:dependencies.targetArgv===undefined?null:{argv:dependencies.targetArgv},
      locations:[],candidates,diagnostics:[{code:"CONFIG_INVALID",severity:"error",source,reason}],
      runtime:{transport:null,permissionMode:null,authProfile:null,billingOwner:null,nativeLimits:null,nativeOutputLimits:null},
    }});
}

async function resolveConfigured(flags: GlobalFlags,dependencies: ConfigDependencies):Promise<EffectiveConfiguration> {
  if((dependencies.platform ?? process.platform) === "win32") dependencies={...dependencies,env:Object.fromEntries(Object.entries(dependencies.env).map(([key,value])=>[key.toUpperCase(),value]))};
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
    fail("OpenProse user configuration path must be absolute.","user configuration path");
  }
  const locations: NonNullable<EffectiveConfiguration["locations"]> = [];
  const diagnostics: NonNullable<EffectiveConfiguration["diagnostics"]> = [];
  let projectConfigPath: string | null = null;
  const legacyConfigPath = dependencies.userConfigPath === undefined && dependencies.env.PROSE_CONFIG_DIR === undefined
    ? legacyUserConfigPath(dependencies) : null;
  if(dependencies.userConfigPath === undefined && dependencies.env.PROSE_CONFIG_DIR === undefined && dependencies.env.XDG_CONFIG_HOME !== undefined && legacyConfigPath === null) diagnostics.push({code:"LEGACY_CONFIG_ROOT_INVALID",severity:"warning",source:"XDG_CONFIG_HOME",reason:"Legacy configuration root is not a non-empty absolute path and is ignored."});
  const values: EffectiveValues = { ...defaults };
  const sources = Object.fromEntries(
    SETTINGS.map(([key]) => [key, { kind: "default", location: "built-in" }]),
  ) as { [K in ConfigKey]: ValueSource };
  const candidates: NonNullable<EffectiveConfiguration["candidates"]> = Object.fromEntries(SETTINGS.map(([key]) => [key, [{value: values[key] ?? (key === "nativeProfile" ? "default" : ["nativeAddDirs","nativeAllowTools"].includes(key) ? [] : null), source: sources[key], selected: true}]]));
  const config: EffectiveConfiguration = { cwd, cwdSource: flags.cwd === undefined ? {kind:"default",location:"process cwd"} : {kind:"flag",location:"--cwd"}, values, sources, projectConfigPath, userConfigPath, legacyConfigPath, activeUserConfigPath:null, target:dependencies.targetArgv === undefined ? null : {argv:dependencies.targetArgv}, locations, candidates, diagnostics };
  const owners: Partial<Record<ConfigKey,string>> = {};
  const candidateHarnesses = new WeakMap<object,string>();
  const ranks: Partial<Record<ConfigKey,number>> = {};
  let currentHarness = values.harness, harnessRank = 0;
  const overlay = (parsed: ParsedValues, kind: SourceKind, rank: number) => {
    if (parsed.values.harness !== undefined) {currentHarness=parsed.values.harness;harnessRank=rank;}
    for (const key of Object.keys(parsed.values) as ConfigKey[]) {
      const source = {kind,location:parsed.locations[key] ?? kind};
      for (const candidate of candidates[key]!) candidate.selected=false;
      const candidate={value:parsed.values[key],source,selected:true};
      candidateHarnesses.set(candidate,currentHarness);
      candidates[key]!.push(candidate);
      owners[key]=currentHarness; ranks[key]=rank;
    }
    apply(values,sources,parsed.values,kind,parsed.locations);
  };
  try {
    projectConfigPath=await discoverProjectConfig(cwd,locations);config.projectConfigPath=projectConfigPath;
    locations.unshift({role:"user",path:userConfigPath,present:false,selected:false});
    const userConfig = await strictConfigFile(userConfigPath);
    const legacyConfig = legacyConfigPath === null || legacyConfigPath === userConfigPath ? null : await regularFile(legacyConfigPath);
    locations[0]={role:"user",path:userConfigPath,present:userConfig!==null,selected:userConfig!==null};
    if (legacyConfigPath !== null && legacyConfigPath !== userConfigPath) locations.splice(1,0,{role:"legacy-user",path:legacyConfigPath,present:legacyConfig!==null,selected:userConfig===null&&legacyConfig!==null});
    config.activeUserConfigPath = userConfig !== null ? userConfigPath : legacyConfig !== null ? legacyConfigPath : null;
    if (config.activeUserConfigPath !== null && (userConfig ?? legacyConfig) !== projectConfigPath) overlay(await readConfig(config.activeUserConfigPath,true),"user-config",1);
    if (legacyConfig !== null && userConfig === null) diagnostics.push({code:"LEGACY_CONFIG_ACTIVE",severity:"warning",source:legacyConfigPath!,reason:"Legacy user configuration is active; run prose cli config migrate to copy explicit settings."});
    if (legacyConfig !== null && userConfig !== null && legacyConfig !== userConfig) {
      let reason="Canonical user configuration is authoritative; legacy values are ignored.";
      try {
        const old=await readConfig(legacyConfig,true); const now=await readConfig(userConfig,true);
        const differing=SETTINGS.filter(([key]) => old.values[key]!==undefined&&JSON.stringify(old.values[key])!==JSON.stringify(now.values[key])).map(([,key])=>key);
        if(differing.length) reason+=` Differing explicit keys: ${differing.join(", ")}.`;
      } catch {reason+=" Ignored legacy configuration is invalid.";}
      diagnostics.push({code:"LEGACY_CONFIG_IGNORED",severity:"warning",source:legacyConfigPath!,reason});
    }
    if (projectConfigPath !== null) overlay(await readConfig(projectConfigPath),"project-config",2);
    overlay(parseEnvironment(dependencies.env),"environment",3);
    overlay(parseFlags(flags),"flag",4);
    try {dependencies.validateSelection?.();}catch(caught){throw new SelectionValidationFailure(caught);}
    candidates.authProfile=candidates.authProfile!.filter(candidate=>{
      if(candidate.selected || candidate.value===null)return true;
      const owner=candidateHarnesses.get(candidate);
      const descriptor=owner===undefined ? undefined : harnessById(owner);
      if(descriptor?.runtime!=="installed-process")return true;
      const definition=installedAdapterDefinition(`${owner}/${descriptor.transports[0]}` as InstalledAdapterId);
      if(typeof candidate.value==="string" && definition.credentialGroups[candidate.value]!==undefined)return true;
      diagnostics.push({code:"CONFIG_CANDIDATE_INVALID",severity:"warning",source:candidate.source.location,reason:"Incompatible overridden authentication profile is omitted."});
      return false;
    });
    for (const key of ["model","authProfile"] as const) {
      if (sources[key].kind === "default") {
        const value = key === "model" ? (values.harness === "agents-sdk" ? "gpt-6.1-sol" : null)
          : ({"agents-sdk":"openai-api-key",codex:"cached-chatgpt-login",claude:"claude-subscription"} as Record<string,string>)[values.harness] ?? null;
        values[key]=value;
        if (value!==null) sources[key]={kind:"default",location:`built-in:${values.harness}`};
        candidates[key]![0]={value,source:sources[key],selected:true};
      } else if (owners[key] !== values.harness && (ranks[key] ?? 0)<harnessRank) {
        fail(`Inherited ${key === "authProfile" ? "auth_profile" : key} belongs to a different harness; replace it at the harness-selecting layer.`,sources[key].location);
      }
    }
    const descriptor=harnessById(values.harness);
    const transport=values.transport === "auto" ? descriptor?.transports[0] ?? null : values.transport;
    if (descriptor?.runtime === "installed-process" && transport!==null) {
      const definition=installedAdapterDefinition(`${values.harness}/${descriptor.transports[0]}` as InstalledAdapterId);
      if(["prime","omp"].includes(values.harness) && values.model!==null && !values.model.split("/").every(segment=>segment.length>0&&!/[\s\p{Cc}]/u.test(segment))) throw failure("CONFIG_INVALID",{adapterId:`${values.harness}/${descriptor.transports[0]}`,source:sources.model.location,reason:"Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments."});
      if(["prime","omp"].includes(values.harness) && values.model!==null && !values.model.includes("/")) throw failure("CONFIG_INVALID",{adapterId:`${values.harness}/${descriptor.transports[0]}`,source:sources.model.location,reason:"Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments."});
      if(values.authProfile !== null && definition.credentialGroups[values.authProfile] === undefined) throw failure("CONFIG_INVALID",{adapterId:`${values.harness}/${descriptor.transports[0]}`,reason:"Authentication profile is incompatible with the selected harness.",source:sources.authProfile.location,supportedAuthProfiles:Object.keys(definition.credentialGroups)});
    }
    if(values.harness==="agents-sdk" && values.permissionMode!==null)throw failure("CONFIG_INVALID",{adapterId:"agents-sdk/jsonl",source:sources.permissionMode.location,reason:"Unsupported explicit permission mode for this harness."});

  if (values.codexCompatibility !== "qualified" && values.harness !== "codex") fail("Codex compatibility probe requires the codex harness.", sources.codexCompatibility?.location);

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
    config.runtime = {
      transport: values.transport === "auto" ? descriptor?.transports[0] ?? null : values.transport,
      permissionMode: values.permissionMode ?? null,
      authProfile: values.authProfile,
      billingOwner: descriptor?.billingOwner ?? null,
      nativeLimits: nativeLimits(values) ?? null,
      nativeOutputLimits: nativeOutputLimits(values) ?? null,
    };
    return config;
  } catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "CONFIG_INVALID") {
      const rejectedSource=String(caught.details?.source ?? "configuration");
      for(const [key] of SETTINGS) if(sources[key].location===rejectedSource && sources[key].kind!=="default") {
        const safe=(candidates[key] ?? []).filter(candidate=>candidate.source.location!==rejectedSource);
        const previous=safe.at(-1);
        if(previous) {
          safe.forEach((candidate,index)=>{candidate.selected=index===safe.length-1;});
          sources[key]=previous.source;
          if(previous.value===null && !["model","authProfile","permissionMode"].includes(key))delete values[key];
          else Object.assign(values,{[key]:previous.value});
        }
        candidates[key]=safe;
      }
      diagnostics.push({code:"CONFIG_INVALID",severity:"error",source:rejectedSource,reason:String(caught.details?.reason ?? "Runner configuration is invalid.")});
      throw failure("CONFIG_INVALID",{...(caught.details ?? {}),configurationExplanation:configurationExplanation(config)});
    }
    throw caught;
  }
}

function defaultUserConfigPath(dependencies: ConfigDependencies): string {
  const platform=dependencies.platform ?? process.platform;
  const pathApi=platform === "win32" ? win32 : posix;
  const override=dependencies.env.PROSE_CONFIG_DIR;
  if(override!==undefined) {
    if(!override || !pathApi.isAbsolute(override)) fail("PROSE_CONFIG_DIR must be a non-empty absolute path to locate OpenProse user configuration.","PROSE_CONFIG_DIR");
    return pathApi.join(override,"cli.toml");
  }
  const home=dependencies.homeDir ?? (platform === "win32" ? dependencies.env.USERPROFILE ?? dependencies.env.HOME : dependencies.env.HOME);
  if(!home || !pathApi.isAbsolute(home)) fail(`${platform === "win32" ? "USERPROFILE" : "HOME"} must be a non-empty absolute path to locate OpenProse user configuration.`,platform === "win32" ? "USERPROFILE" : "HOME");
  return pathApi.join(home,".prose","cli.toml");
}
function legacyUserConfigPath(dependencies: ConfigDependencies): string | null {
  const platform=dependencies.platform ?? process.platform;
  const pathApi=platform === "win32" ? win32 : posix;
  const xdg=dependencies.env.XDG_CONFIG_HOME;
  if(xdg!==undefined) return xdg && pathApi.isAbsolute(xdg) ? pathApi.join(xdg,"openprose","cli.toml") : null;
  if(platform === "win32") return dependencies.env.APPDATA && pathApi.isAbsolute(dependencies.env.APPDATA) ? pathApi.join(dependencies.env.APPDATA,"OpenProse","cli.toml") : null;
  const home=dependencies.homeDir ?? dependencies.env.HOME;
  if(!home || !pathApi.isAbsolute(home)) return null;
  return platform === "darwin" ? pathApi.join(home,"Library","Application Support","OpenProse","cli.toml") : pathApi.join(home,".config","openprose","cli.toml");
}
async function strictConfigFile(path: string): Promise<string | null> {
  try { const info=await stat(path); if(!info.isFile()) return null; return await realpath(path); }
  catch(caught) {if(caught instanceof RunnerFailure)throw caught;if((caught as NodeJS.ErrnoException).code === "ENOENT")return null;fail("Cannot read configuration file.",path);}
}

/** The canonical path of `path` when it is a regular file (symlinks followed), else null. */
async function regularFile(path: string): Promise<string | null> {
  try {
    if (!(await stat(path)).isFile()) return null;
  } catch { return null; }
  try { return await realpath(path); }
  catch { fail(`Cannot read configuration file: ${path}.`, path); }
}

async function discoverProjectConfig(cwd: string, locations: NonNullable<EffectiveConfiguration["locations"]>): Promise<string | null> {
  let directory = cwd;
  while (true) {
    const considered = join(directory, ".prose", "cli.toml");
    const candidate = await strictConfigFile(considered);
    locations.push({role:"project",path:considered,present:candidate!==null,selected:candidate!==null});
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

function parseFlatToml(source: string, path: string, allowService = true, syntaxOnly = false): ParsedValues {
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
      if(!syntaxOnly) checkFileType(key!, rawKey, parsed, location);
      rawValues[key!] = parsed;
      locations[key!] = location;
    }
  }
  return { values: syntaxOnly ? rawValues as PartialValues : validateFileValues(rawValues, locations), locations };
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
  else if (key === "codexCompatibility") {
    if (raw !== "qualified" && raw !== "probe") fail("Codex compatibility must be qualified or probe.", location);
    values.codexCompatibility = raw;
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
      if(decodeConfiguration(await readFile(path),path)!==original) fail("OpenProse user configuration changed before replacement.",path);
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

/** Only explicit user settings are copied or removed; inherited values are never saved. */
export async function mutateUserConfiguration(config: EffectiveConfiguration, operation: "migrate" | "unset", keys: string[]): Promise<NonNullable<EffectiveConfiguration["mutation"]>> {
  const destination=config.userConfigPath;
  if(operation === "unset") {
    try {const info=await lstat(destination);if(!info.isFile() || info.isSymbolicLink())fail("OpenProse user configuration must be a regular non-symlink file.",destination);}
    catch(caught) {if(caught instanceof RunnerFailure)throw caught;if((caught as NodeJS.ErrnoException).code!=="ENOENT")fail("OpenProse user configuration cannot be read safely.",destination);}
  }
  const legacy=config.activeUserConfigPath !== destination ? config.activeUserConfigPath ?? null : null;
  if(operation === "migrate") {
    try {await lstat(destination);fail("Canonical user configuration already exists; migration never overwrites it.",destination);}
    catch(caught) {if(caught instanceof RunnerFailure)throw caught;if((caught as NodeJS.ErrnoException).code!=="ENOENT")fail("Canonical user configuration cannot be read safely.",destination);}
    if(legacy===null) fail("No legacy user configuration exists to migrate.",destination);
    const bytes=await safeUserBytes(legacy);
    validateUserSettingsBundle(bytes,legacy);
    await createUserConfiguration(destination,bytes);
    return {operation,changed:true,path:destination,sourcePath:legacy,keys:[]};
  }
  if(config.activeUserConfigPath===null || config.activeUserConfigPath===undefined) return {operation,changed:false,path:destination,sourcePath:null,keys};
  const active=config.activeUserConfigPath;
  const bytes=await safeUserBytes(active,operation !== "unset");
  const original=decodeConfiguration(bytes,active);
  const retained=original.split(/(?<=\n)/u).filter(line=>{
    const match=/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/u.exec(line); return match?.[1]===undefined || !keys.includes(match[1]);
  }).join("");
  validateUserSettingsBundle(new TextEncoder().encode(retained),destination);
  const changed=retained!==original || legacy!==null;
  if(changed) {
    if(legacy!==null) await createUserConfiguration(destination,new TextEncoder().encode(retained));
    else await replaceUserBytes(destination,new TextEncoder().encode(retained),bytes);
  }
  return {operation,changed,path:destination,sourcePath:legacy,keys};
}
async function safeUserBytes(path: string, validateValues=true): Promise<Uint8Array> {
  const info=await lstat(path);
  if(!info.isFile() || info.isSymbolicLink()) fail("OpenProse user configuration must be a regular non-symlink file.",path);
  const bytes=await readFile(path);parseFlatToml(decodeConfiguration(bytes,path),path,true,!validateValues);return bytes;
}
async function createUserConfiguration(path: string, bytes: Uint8Array): Promise<void> {
  const parent=dirname(path);await secureUserConfigParent(parent);
  const temporary=join(parent,`.cli.toml.openprose-${process.pid}-${randomUUID()}.tmp`);
  try {
    await writeFile(temporary,bytes,{mode:0o600,flag:"wx"});
    await secureUserConfigParent(parent);
    // Atomic no-replace creation. A concurrently created destination is never overwritten.
    await link(temporary,path);
  } catch(caught) {
    fail((caught as NodeJS.ErrnoException).code === "EEXIST" ? "Canonical user configuration already exists; migration never overwrites it." : "OpenProse user configuration could not be created atomically.",path);
  } finally {try {await unlink(temporary);}catch{/* no temporary file */}}
}
async function replaceUserBytes(path: string, bytes: Uint8Array, original: Uint8Array): Promise<void> {
  const parent=dirname(path);await secureUserConfigParent(parent);
  const temporary=join(parent,`.cli.toml.openprose-${process.pid}-${randomUUID()}.tmp`);
  try {
    await writeFile(temporary,bytes,{mode:0o600,flag:"wx"});
    const current=await safeUserBytes(path,false);
    if(!Buffer.from(current).equals(Buffer.from(original))) fail("OpenProse user configuration changed before replacement.",path);
    await secureUserConfigParent(parent);await rename(temporary,path);
  } catch(caught) {if(caught instanceof RunnerFailure)throw caught;fail("OpenProse user configuration could not be written atomically.",path);}
  finally {try {await unlink(temporary);}catch{/* already renamed */}}
}

/** Mutation preflight examines only the user locations, never project or execution overrides. */
export async function prepareUserConfigurationMutation(dependencies: ConfigDependencies): Promise<EffectiveConfiguration> {
  try {return await prepareUserMutation(dependencies);}
  catch(caught) {throw earlyConfigurationFailure(caught,dependencies);}
}
async function prepareUserMutation(dependencies:ConfigDependencies):Promise<EffectiveConfiguration> {
  let cwd:string;
  try {
    if(!(await stat(dependencies.processCwd)).isDirectory())fail(`Working directory is not a directory: ${dependencies.processCwd}.`,"process cwd");
    cwd=await realpath(dependencies.processCwd);
  }catch(caught){if(caught instanceof RunnerFailure)throw caught;fail(`Working directory does not exist or cannot be read: ${dependencies.processCwd}.`,"process cwd");}
  if((dependencies.platform ?? process.platform) === "win32")dependencies={...dependencies,env:Object.fromEntries(Object.entries(dependencies.env).map(([key,value])=>[key.toUpperCase(),value]))};
  const userConfigPath=dependencies.userConfigPath ?? defaultUserConfigPath(dependencies);
  const pathApi=(dependencies.platform ?? process.platform) === "win32" ? win32:posix;
  if(!pathApi.isAbsolute(userConfigPath))fail("OpenProse user configuration path must be absolute.","user configuration path");
  const legacyConfigPath=dependencies.userConfigPath===undefined&&dependencies.env.PROSE_CONFIG_DIR===undefined ? legacyUserConfigPath(dependencies):null;
  const user=await strictConfigFile(userConfigPath);
  const legacy=legacyConfigPath===null ? null:await regularFile(legacyConfigPath);
  const values={...defaults};
  const sources=Object.fromEntries(SETTINGS.map(([key])=>[key,{kind:"default",location:"built-in"}])) as EffectiveConfiguration["sources"];
  return {cwd,cwdSource:{kind:"default",location:"process cwd"},values,sources,userConfigPath,legacyConfigPath,activeUserConfigPath:user!==null ? userConfigPath:legacy!==null ? legacyConfigPath:null,projectConfigPath:null,target:null,diagnostics:[],locations:[{role:"user",path:userConfigPath,present:user!==null,selected:user!==null},...(legacyConfigPath===null ? []:[{role:"legacy-user" as const,path:legacyConfigPath,present:legacy!==null,selected:user===null&&legacy!==null}])],candidates:{}};
}
function validateUserSettingsBundle(bytes: Uint8Array, path: string): void {
  const parsed=parseFlatToml(decodeConfiguration(bytes,path),path,true);
  const harness=parsed.values.harness;
  if(harness===undefined)return;
  const descriptor=harnessById(harness);
  if(descriptor?.runtime!=="installed-process")return;
  const adapterId=`${harness}/${descriptor.transports[0]}` as InstalledAdapterId;
  const definition=installedAdapterDefinition(adapterId);
  if(parsed.values.authProfile!==undefined && parsed.values.authProfile!==null && definition.credentialGroups[parsed.values.authProfile]===undefined) fail("Authentication profile is incompatible with the selected harness.",parsed.locations.authProfile);
  const model=parsed.values.model;
  if(["prime","omp"].includes(harness)&&model!==undefined&&model!==null&&(!model.includes("/")||model.split("/").some(segment=>!segment || /[\s\p{Cc}]/u.test(segment))))fail("Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments.",parsed.locations.model);
}
