import { failure } from "../core/errors";
import type { InstalledAdapterDefinition } from "./types";
import { adapterAlwaysStrip, adapterBaseEnvironmentAllowlist, adapterOwnedEnvironmentControls } from "./recipes";

export interface AdapterEnvironmentInput {
  definition: InstalledAdapterDefinition;
  ambient: Readonly<Record<string, string | undefined>>;
  credentialGroup: string;
  additionalNames?: readonly string[];
  controls?: Readonly<Record<string, string>>;
  credentialConfigDirectory?: string;
  platform?: NodeJS.Platform;
}

export function buildInstalledAdapterEnvironment(input: AdapterEnvironmentInput): Record<string, string> {
  const credentialNames = input.definition.credentialGroups[input.credentialGroup];
  if (credentialNames === undefined) {
    throw failure("CONFIG_INVALID", {
      adapterId: input.definition.id,
      reason: `Unknown or ambiguous credential group: ${input.credentialGroup}.`,
    });
  }
  const caseInsensitive = (input.platform ?? process.platform) === "win32";
  const normalize = (name: string): string => caseInsensitive ? name.toUpperCase() : name;
  const requirement = input.definition.credentialRequirements[input.credentialGroup];
  if (requirement === undefined) {
    throw failure("INTERNAL_ERROR", { adapterId: input.definition.id, reason: "Credential requirement is missing from the shared oracle." });
  }
  const present = (expected: string): boolean => Object.entries(input.ambient).some(([name, value]) =>
    normalize(name) === normalize(expected) && value !== undefined && value.length > 0);
  if (requirement.kind !== "probe-owned" && !requirement.alternatives.some((alternative) => alternative.every(present))) {
    throw failure("HARNESS_NEEDS_AUTH", {
      adapterId: input.definition.id,
      authProfile: input.credentialGroup,
    });
  }
  const alwaysStrip = new Set([...adapterAlwaysStrip, "OPENPROSE_API_KEY"].map(normalize));
  const selected = new Set([
    ...adapterBaseEnvironmentAllowlist,
    ...credentialNames,
    ...(input.additionalNames ?? []),
  ].map(normalize));

  const result: Record<string, string> = {};
  for (const [name, value] of Object.entries(input.ambient)) {
    if (value === undefined || alwaysStrip.has(normalize(name)) || !selected.has(normalize(name))) continue;
    result[name] = value;
  }
  for (const [name, value] of Object.entries(input.controls ?? {})) {
    if (alwaysStrip.has(normalize(name))) {
      throw failure("CONFIG_INVALID", { adapterId: input.definition.id, reason: `Control environment name is forbidden: ${name}.` });
    }
    result[name] = value;
  }
  if (input.credentialConfigDirectory !== undefined) {
    const configName = input.definition.id === "prime/rpc"
      ? "PRIME_AGENT_CODING_AGENT_DIR"
      : input.definition.id === "claude/print-stream-json" && input.credentialGroup === "anthropic-api-key"
        ? "CLAUDE_CONFIG_DIR"
        : input.definition.id === "omp/rpc"
        ? "PI_CODING_AGENT_DIR"
        : undefined;
    if (configName === undefined || input.credentialGroup.endsWith("-harness-login")) {
      throw failure("INTERNAL_ERROR", {
        adapterId: input.definition.id,
        reason: "A private credential config directory was assigned to a non-isolated route.",
      });
    }
    result[configName] = input.credentialConfigDirectory;
  }
  Object.assign(result, adapterOwnedEnvironmentControls(input.definition.id));
  return result;
}

export function installedAdapterProtectedValues(input: {
  definition: InstalledAdapterDefinition;
  credentialGroup: string;
  environment: Readonly<Record<string, string>>;
  controlNames?: readonly string[];
  platform?: NodeJS.Platform;
}): string[] {
  if (input.definition.credentialGroups[input.credentialGroup] === undefined) {
    throw failure("INTERNAL_ERROR", {
      adapterId: input.definition.id,
      reason: "The final launch environment has an unknown credential group.",
    });
  }
  const normalize = (name: string): string =>
    (input.platform ?? process.platform) === "win32" ? name.toUpperCase() : name;
  const controlNames = new Set([
    ...Object.keys(adapterOwnedEnvironmentControls(input.definition.id)),
    ...(input.controlNames ?? []),
  ].map(normalize));
  const values = new Set<string>();
  for (const [name, value] of Object.entries(input.environment)) {
    if (value.length > 0 && !controlNames.has(normalize(name))) values.add(value);
  }
  return [...values].sort((left, right) => right.length - left.length);
}
