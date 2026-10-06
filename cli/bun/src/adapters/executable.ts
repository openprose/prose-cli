import codexCompatibilityJson from "../../../shared/capabilities/adapters/codex-compatibility.v1.json" with { type: "json" };
import { buildVersionProbeEnvironment } from "../supervision/environment";
import { access } from "node:fs/promises";
import { constants } from "node:fs";
import { delimiter, isAbsolute, join } from "node:path";
import { failure } from "../core/errors";
import { probeExecutableCommand, probeExecutableVersion, resolveExecutable } from "../supervision/process";
import { installedAdapterDefinition } from "./recipes";
import type { RuntimePrerequisiteObservation } from "../core/types";
import { RunnerFailure } from "../core/types";
import type { InstalledAdapterId, InstalledAdapterRecipe, RuntimePrerequisiteRequirement } from "./types";
import { assertInstalledAdapterPlatform } from "./admission";

export interface ResolveInstalledExecutableInput {
  adapterId: InstalledAdapterId;
  ambient: Readonly<Record<string, string | undefined>>;
  explicitPath?: string;
  wrapperExecutable?: string;
  platform?: NodeJS.Platform;
  arch?: NodeJS.Architecture;
}

export async function resolveInstalledExecutable(input: ResolveInstalledExecutableInput): Promise<string> {
  assertInstalledAdapterPlatform(input.adapterId, { platform: input.platform, arch: input.arch });
  const definition = installedAdapterDefinition(input.adapterId);
  if (input.explicitPath !== undefined) return resolveExecutable(input.explicitPath, input.wrapperExecutable);
  const path = input.ambient.PATH;
  if (path === undefined || path.length === 0) {
    throw failure("HARNESS_UNAVAILABLE", {
      adapterId: input.adapterId,
      executableNames: definition.recipe.identity.executableNames,
      admittedVersions: definition.recipe.support.admittedVersions,
      repairCommand: definition.recipe.support.repairCommand,
      fallbackAttempted: false,
    });
  }
  const resolved = await resolvePathExecutable(
    definition.recipe.identity.executableNames,
    input.ambient,
    input.wrapperExecutable,
    input.platform,
  );
  if (resolved !== null) return resolved;
  throw failure("HARNESS_UNAVAILABLE", {
    adapterId: input.adapterId,
    executableNames: definition.recipe.identity.executableNames,
    admittedVersions: definition.recipe.support.admittedVersions,
    repairCommand: definition.recipe.support.repairCommand,
    fallbackAttempted: false,
  });
}

async function resolvePathExecutable(
  names: readonly string[],
  ambient: Readonly<Record<string, string | undefined>>,
  wrapperExecutable?: string,
  requestedPlatform?: NodeJS.Platform,
): Promise<string | null> {
  const path = ambient.PATH;
  if (path === undefined || path.length === 0) return null;
  const platform = requestedPlatform ?? process.platform;
  const extensions = platform === "win32"
    ? (ambient.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";").filter(Boolean)
    : [""];
  for (const name of names) {
    for (const directory of path.split(delimiter).filter((entry) => entry.length > 0)) {
      for (const extension of extensions) {
        const candidate = isAbsolute(name) ? name : join(directory, platform === "win32" ? `${name}${extension}` : name);
        try {
          await access(candidate, platform === "win32" ? constants.F_OK : constants.X_OK);
        } catch {
          continue;
        }
        // The first executable candidate in ordered PATH is authoritative.
        // Identity/symlink/recursion failure must never fall through to a
        // later, more convenient candidate.
        return await resolveExecutable(candidate, wrapperExecutable);
      }
    }
  }
  return null;
}

export async function inspectInstalledAdapterRuntimePrerequisites(input: {
  adapterId: InstalledAdapterId;
  cwd: string;
  ambient: Readonly<Record<string, string | undefined>>;
  wrapperExecutable?: string;
  platform?: NodeJS.Platform;
}): Promise<RuntimePrerequisiteObservation[]> {
  const requirements = installedAdapterDefinition(input.adapterId).runtimePrerequisites;
  const observations: RuntimePrerequisiteObservation[] = [];
  for (const requirement of requirements) {
    let executable: string | null;
    try {
      executable = await resolvePathExecutable(
        [requirement.runtime],
        input.ambient,
        input.wrapperExecutable,
        input.platform,
      );
    } catch (caught) {
      rethrowCleanupAuthority(caught);
      executable = null;
      observations.push(runtimeObservation(requirement, null, "incompatible"));
      continue;
    }
    if (executable === null) {
      observations.push(runtimeObservation(requirement, null, "missing"));
      continue;
    }
    let detectedVersion: string;
    try {
      detectedVersion = await probeExecutableVersion(
        executable,
        input.cwd,
        input.ambient,
        input.wrapperExecutable,
        5_000,
        /^(?:0|[1-9][0-9]{0,5}|1000000)\.(?:0|[1-9][0-9]{0,5}|1000000)\.(?:0|[1-9][0-9]{0,5}|1000000)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/u,
        input.platform,
      );
    } catch (caught) {
      rethrowCleanupAuthority(caught);
      observations.push(runtimeObservation(requirement, null, "incompatible"));
      continue;
    }
    observations.push(runtimeObservation(
      requirement,
      detectedVersion,
      bunVersionAdmitted(detectedVersion) ? "available" : "incompatible",
    ));
  }
  return observations;
}

export async function assertInstalledAdapterRuntimePrerequisites(input: {
  adapterId: InstalledAdapterId;
  cwd: string;
  ambient: Readonly<Record<string, string | undefined>>;
  wrapperExecutable?: string;
  platform?: NodeJS.Platform;
}): Promise<RuntimePrerequisiteObservation[]> {
  const observations = await inspectInstalledAdapterRuntimePrerequisites(input);
  const blocked = observations.find((item) => item.availability !== "available");
  if (blocked !== undefined) {
    throw failure("HARNESS_INCOMPATIBLE", {
      adapterId: input.adapterId,
      runtimePrerequisite: blocked,
      fallbackAttempted: false,
    });
  }
  return observations;
}

function runtimeObservation(
  requirement: RuntimePrerequisiteRequirement,
  detectedVersion: string | null,
  availability: RuntimePrerequisiteObservation["availability"],
): RuntimePrerequisiteObservation {
  return {
    runtime: requirement.runtime,
    versionRange: requirement.versionRange,
    detectedVersion,
    availability,
    repairCommand: requirement.repairCommand,
  };
}

function bunVersionAdmitted(version: string): boolean {
  const match = /^(0|[1-9][0-9]{0,5}|1000000)\.(0|[1-9][0-9]{0,5}|1000000)\.(0|[1-9][0-9]{0,5}|1000000)$/u.exec(version);
  if (match === null) return false;
  const observed = match.slice(1).map(Number);
  const required = [1, 3, 14];
  for (let index = 0; index < required.length; index += 1) {
    if (observed[index]! !== required[index]!) return observed[index]! > required[index]!;
  }
  return true;
}

function rethrowCleanupAuthority(caught: unknown): void {
  if (caught instanceof RunnerFailure && caught.code === "PROCESS_CLEANUP_FAILED") throw caught;
}

export async function probeInstalledAdapterVersion(input: {
  codexCompatibility?: string;
  adapterId: InstalledAdapterId;
  executable: string;
  cwd: string;
  ambient: Readonly<Record<string, string | undefined>>;
  wrapperExecutable?: string;
  platform?: NodeJS.Platform;
  arch?: NodeJS.Architecture;
}): Promise<string> {
  assertInstalledAdapterPlatform(input.adapterId, { platform: input.platform, arch: input.arch });
  const recipe = installedAdapterDefinition(input.adapterId).recipe;
  const probeArgv = recipe.probe.argv;
  if (
    probeArgv.length < 2
    || !("value" in probeArgv[0]!)
    || probeArgv[0]!.value !== "executable"
    || probeArgv.slice(1).some((token) => !("literal" in token))
  ) {
    throw failure("INTERNAL_ERROR", { adapterId: input.adapterId, reason: "The frozen version-probe recipe is unsupported." });
  }
  const argv = probeArgv.slice(1).map((token) => (token as { literal: string }).literal);
  let version: string;
  try {
    version = await probeExecutableVersion(
      input.executable,
      input.cwd,
      input.ambient,
      input.wrapperExecutable,
      recipe.probe.timeoutMs,
      new RegExp(recipe.probe.versionPattern, "u"),
      input.platform,
      argv,
      recipe.probe.versionStream,
    );
  } catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "HARNESS_INCOMPATIBLE") {
      throw failure("HARNESS_INCOMPATIBLE", {
        adapterId: input.adapterId,
        admittedVersions: recipe.support.admittedVersions,
        repairCommand: recipe.support.repairCommand,
        fallbackAttempted: false,
      });
    }
    throw caught;
  }
  const detectedVersion = observedVersion(input.adapterId, version);
  if (input.adapterId === "codex/exec-json" && detectedVersion !== null) {
    if (!versionIsAdmitted(recipe.support, detectedVersion) || input.codexCompatibility === "probe") {
      await probeCodexCapabilities(input, version);
    }
    if (!versionIsAdmitted(recipe.support, detectedVersion) && input.codexCompatibility !== "probe") {
      throw codexCompatibilityFailure({
        adapterId: input.adapterId,
        detectedVersion: version,
        compatibilityStatus: "unqualified",
        reason: "This Codex version is not qualified. Required native options are present; no protocol failure has been observed.",
        admittedVersions: recipe.support.admittedVersions,
        repairCommand: "prose --harness codex --codex-compatibility probe cli doctor --json",
        recovery: "Explicitly use --codex-compatibility probe to attempt this version with runtime protocol checks, or update Prose when qualification is available.",
        fallbackAttempted: false,
      });
    }
    return version;
  }
  if (detectedVersion === null || !versionIsAdmitted(recipe.support, detectedVersion)) {
    throw failure("HARNESS_INCOMPATIBLE", {
      adapterId: input.adapterId,
      detectedVersion: version,
      admittedVersions: recipe.support.admittedVersions,
      repairCommand: recipe.support.repairCommand,
      fallbackAttempted: false,
    });
  }
  return version;
}

/** Exact audited versions, or a stable release at or above the recipe's floor within its major version. */
export function versionIsAdmitted(
  support: Pick<InstalledAdapterRecipe["support"], "admittedVersions" | "minimumVersion">,
  version: string,
): boolean {
  if (support.admittedVersions.includes(version)) return true;
  if (support.minimumVersion === undefined) return false;
  const parse = (value: string) => /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/u.exec(value)?.slice(1).map(Number) ?? null;
  const floor = parse(support.minimumVersion);
  const candidate = parse(version);
  if (floor === null || candidate === null || candidate[0] !== floor[0]) return false;
  for (let index = 1; index < 3; index++) {
    if (candidate[index]! !== floor[index]!) return candidate[index]! > floor[index]!;
  }
  return true;
}

function observedVersion(adapterId: InstalledAdapterId, output: string): string | null {
  const patterns: Record<InstalledAdapterId, RegExp> = {
    "agents-sdk/jsonl": /^prose-agents-sdk ([0-9]+\.[0-9]+\.[0-9]+)$/,
    "codex/exec-json": /^codex-cli ([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)$/u,
    "claude/print-stream-json": /^([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)(?: \(Claude Code\))?$/u,
    "prime/rpc": /^(?:prime-agent )?([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)$/u,
    "omp/rpc": /^omp\/([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)$/u,
  };
  return patterns[adapterId].exec(output.trim())?.[1] ?? null;
}

export async function probeInstalledAdapterAuth(input: {
  adapterId: InstalledAdapterId;
  executable: string;
  cwd: string;
  credentialGroup: string;
  environment: Readonly<Record<string, string>>;
  timeoutMs?: number;
  platform?: NodeJS.Platform;
  arch?: NodeJS.Architecture;
}): Promise<"ready" | "unknown"> {
  assertInstalledAdapterPlatform(input.adapterId, { platform: input.platform, arch: input.arch });
  if (input.adapterId === "prime/rpc" || input.adapterId === "omp/rpc") {
    throw failure("INTERNAL_ERROR", {
      adapterId: input.adapterId,
      reason: "Actual execution is the first authentication authority for this adapter.",
    });
  }
  if (input.adapterId === "agents-sdk/jsonl") return "unknown";
  if (input.adapterId === "codex/exec-json") {
    if (input.credentialGroup === "openai-api-key" || input.credentialGroup === "codex-access-token") {
      return "unknown";
    }
    if (input.credentialGroup !== "cached-chatgpt-login") {
      throw failure("INTERNAL_ERROR", {
        adapterId: input.adapterId,
        reason: "The selected credential group has no admitted auth-readiness policy.",
      });
    }
  } else if (input.credentialGroup === "anthropic-api-key") {
    return "unknown";
  } else if (input.credentialGroup !== "claude-subscription") {
    throw failure("INTERNAL_ERROR", {
      adapterId: input.adapterId,
      reason: "The selected credential group has no admitted auth-readiness policy.",
    });
  }
  const recipe = installedAdapterDefinition(input.adapterId).recipe;
  const expectedProbe = input.adapterId === "codex/exec-json"
    ? "codex login status"
    : "claude auth status --json";
  if (recipe.auth.readinessProbe !== expectedProbe) {
    throw failure("INTERNAL_ERROR", { adapterId: input.adapterId, reason: "The frozen auth-readiness recipe is unsupported." });
  }
  const argv = input.adapterId === "codex/exec-json"
    ? ["login", "status"]
    : ["auth", "status", "--json"];
  const { exitCode, stdout, stderr } = await probeExecutableCommand({
    executable: input.executable,
    cwd: input.cwd,
    environment: {
      ...input.environment,
      OPENPROSE_INVOCATION_ID: "auth-probe",
      OPENPROSE_RECURSION_TOKEN: "openprose:auth-probe",
      OPENPROSE_RUN_NONCE: "auth-probe",
    },
    argv,
    timeoutMs: input.timeoutMs ?? recipe.probe.timeoutMs,
    phase: "auth-probe",
  });
  const ready = authProbeReady(input.adapterId, exitCode, stdout, stderr);
  if (!ready) {
    throw failure("HARNESS_NEEDS_AUTH", {
      adapterId: input.adapterId,
      fallbackAttempted: false,
      readinessProbeExitCode: exitCode,
    });
  }
  return "ready";
}

function authProbeReady(adapterId: InstalledAdapterId, exitCode: number, stdout: string, stderr: string): boolean {
  if (exitCode !== 0) return false;
  if (adapterId === "claude/print-stream-json") {
    try {
      const value = JSON.parse(stdout) as { loggedIn?: unknown };
      return value.loggedIn === true;
    } catch {
      return false;
    }
  }
  if (adapterId === "codex/exec-json") {
    const output = `${stdout}\n${stderr}`;
    return /logged in/iu.test(output) && !/not logged in/iu.test(output);
  }
  return stdout.trim().length > 0 || stderr.trim().length > 0;
}

/** Advertised options are a prerequisite, never a protocol negotiation or qualification. */
export function missingCodexOptions(help: string): string[] {
  const advertised = new Set(help.split("\n").flatMap(line => {
    const match = /^\s*(?:-[A-Za-z],\s*)?(--[a-z][a-z0-9-]*)(?=\s|=|$)/u.exec(line);
    return match === null ? [] : [match[1]!];
  }));
  return codexCompatibilityJson.requiredExecOptions.filter(option => !advertised.has(option));
}

export function codexQualification(version: string | null | undefined): "qualified" | "unqualified" | "unknown" {
  if (version == null) return "unknown";
  const parsed = observedVersion("codex/exec-json", version);
  if (parsed === null) return "unknown";
  return versionIsAdmitted(installedAdapterDefinition("codex/exec-json").recipe.support, parsed) ? "qualified" : "unqualified";
}

async function probeCodexCapabilities(input: {
  executable: string;
  cwd: string;
  ambient: Readonly<Record<string, string | undefined>>;
  platform?: NodeJS.Platform;
}, version: string): Promise<void> {
  if ((input.platform ?? process.platform) === "win32") {
    throw failure("TRANSPORT_UNSUPPORTED", {
      adapterId: "codex/exec-json",
      reason: "Codex compatibility capability probing is not qualified on Windows.",
      fallbackAttempted: false,
    });
  }
  let outcome;
  try {
    outcome = await probeExecutableCommand({
      executable: input.executable,
      cwd: input.cwd,
      environment: buildVersionProbeEnvironment(input.ambient, input.platform),
      argv: codexCompatibilityJson.probeArgv,
      timeoutMs: codexCompatibilityJson.timeoutMs,
      maxOutputBytes: codexCompatibilityJson.maxOutputBytes,
      phase: "codex-capability-probe",
      ...(input.platform === undefined ? {} : { platform: input.platform }),
    });
  } catch (caught) {
    if (caught instanceof RunnerFailure && caught.code === "PROCESS_CLEANUP_FAILED") throw caught;
    throw codexCompatibilityFailure({
      adapterId: "codex/exec-json",
      detectedVersion: version,
      compatibilityStatus: "probe-failed",
      reason: "The bounded native Codex capability probe failed; compatibility is unknown.",
      fallbackAttempted: false,
    });
  }
  const missing = missingCodexOptions(outcome.stdout);
  if (outcome.exitCode !== 0 || missing.length !== 0) {
    throw codexCompatibilityFailure({
      adapterId: "codex/exec-json",
      detectedVersion: version,
      compatibilityStatus: outcome.exitCode === 0 ? "incompatible" : "probe-failed",
      reason: outcome.exitCode === 0
        ? "Codex does not advertise required native options."
        : "Codex capability probe exited unsuccessfully; compatibility is unknown.",
      missingCapabilities: missing,
      capabilityProbeExitCode: outcome.exitCode,
      fallbackAttempted: false,
    });
  }
}

function codexCompatibilityFailure(details: Record<string, unknown>): RunnerFailure {
  const {schema: _schema, ...shape} = failure("HARNESS_INCOMPATIBLE", details).toJSON();
  const unqualified = details.compatibilityStatus === "unqualified";
  const failed = details.compatibilityStatus === "probe-failed";
  return new RunnerFailure({
    ...shape,
    message: unqualified ? "The selected Codex version is not qualified by this Prose CLI." : failed ? "Codex compatibility could not be determined because its capability probe failed." : "The selected Codex interface lacks required native capabilities.",
    action: unqualified ? "Explicitly select --codex-compatibility probe to attempt this version with runtime protocol validation, or update Prose after qualification is available." : "Inspect `codex exec --help` and the reported missing capabilities; update Codex or Prose to a compatible interface. No fallback was attempted.",
  });
}
