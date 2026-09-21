const operatingSystemNames = new Set([
  "PATH", "HOME", "USERPROFILE", "SystemRoot", "WINDIR", "COMSPEC",
  "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
  "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
]);

const versionProbeOperatingSystemNames = new Set([
  "PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC",
  "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
]);

const secretName = /(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|CREDENTIAL|AUTH)/iu;
const exactProtectedNames = new Set(["PRIME_AGENT_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR"]);
const assignmentSecret = /\b(PRIME_AGENT_CODING_AGENT_DIR|PI_CODING_AGENT_DIR|[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|CREDENTIAL)[A-Z0-9_]*)=([^\s]+)/giu;

export interface ChildMetadata {
  invocationId: string;
  recursionToken: string;
  runNonce: string;
}

export function buildChildEnvironment(
  ambient: Readonly<Record<string, string | undefined>>,
  metadata: ChildMetadata,
  additionalNames: readonly string[] = [],
): Record<string, string> {
  const permitted = new Set([...operatingSystemNames, ...additionalNames]);
  const result: Record<string, string> = {};
  for (const name of permitted) {
    if (name.toUpperCase() === "OPENPROSE_STAGING_API_KEY") continue;
    const value = ambient[name];
    if (value !== undefined) result[name] = value;
  }
  result.OPENPROSE_INVOCATION_ID = metadata.invocationId;
  result.OPENPROSE_RECURSION_TOKEN = metadata.recursionToken;
  result.OPENPROSE_RUN_NONCE = metadata.runNonce;
  return result;
}

/**
 * Version identity is established before an adapter receives any selected
 * credential route or harness-login configuration. Keep this environment
 * deliberately smaller than the execution environment: HOME/XDG locations
 * and authenticated proxy settings can themselves grant access.
 */
export function buildVersionProbeEnvironment(
  ambient: Readonly<Record<string, string | undefined>>,
  platform: NodeJS.Platform = process.platform,
): Record<string, string> {
  const normalize = (name: string): string => platform === "win32" ? name.toUpperCase() : name;
  const admitted = new Set([...versionProbeOperatingSystemNames].map(normalize));
  const result: Record<string, string> = {};
  for (const [name, value] of Object.entries(ambient)) {
    if (value !== undefined && admitted.has(normalize(name))) result[name] = value;
  }
  return result;
}

export function collectSecretValues(ambient: Readonly<Record<string, string | undefined>>): string[] {
  const values = new Set<string>();
  for (const [name, value] of Object.entries(ambient)) {
    if (
      value !== undefined
      && value.length > 0
      && (secretName.test(name) || exactProtectedNames.has(name.toUpperCase()))
    ) values.add(value);
  }
  return [...values].sort((left, right) => right.length - left.length);
}

export function redactDiagnostic(text: string, secretValues: readonly string[]): string {
  let result = text.replace(assignmentSecret, (_match, name: string) => `${name}=[REDACTED]`);
  for (const secret of secretValues) result = result.split(secret).join("[REDACTED]");
  return result;
}
