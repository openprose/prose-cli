import { failure, invocationFailure } from "./errors";
import { RunnerFailure, type OutputMode } from "./types";
import { jsonLine, humanSafeScalar } from "./output";
import { Service, fixtureFor, type Dependencies } from "./service-account";
import type { PackageCommand } from "./package-args";
import { canonicalPackageJSON, closed, digest, identity, invalidPackage, matchReceipt, PACKAGE_LIMITS, packageHash, preparePackage, receipt, version, type PackageReceipt } from "./package-format";
import { parseJson } from "./service/http";
import { materializePackage, prepareSource } from "./package-files";
import { teach } from "./service/index";
import { environmentLabel, type Environment } from "./service/manifest";
import { explainNotFound } from "./service/not-found";
import { humanError, structuredOutputFor } from "./service/render";
import type { Json } from "./service/manifest";
function referenceInput(input: string): { organization: string; package: string; version: string } {
  const match = /^([^/]+)\/([^/@]+)@([^/]+)$/u.exec(input);
  if (match === null) return invalidPackage();
  return { organization: identity(match[1]), package: identity(match[2]), version: version(match[3]) };
}
function cursor(input: unknown): string {
  if (typeof input !== "string" || !input.length || input.length > 210 || !/^public:[a-z0-9-]+:[0-9A-Za-z.+-]+$/u.test(input)) return invalidPackage();
  return input;
}
/** A registry response body as JSON under the shared service rules (shared/fixtures/transport/json-parse.json). */
function responseJSON(bytes: Uint8Array): unknown {
  if (bytes.length > PACKAGE_LIMITS.request) return invalidPackage();
  const value = parseJson(bytes);
  if (value === undefined) throw failure("SERVICE_PROTOCOL_INVALID");
  return value;
}
function checkStatus(status: number, allowed = [200]): void {
  if (status === 401 || status === 403) throw failure("SERVICE_AUTH_REQUIRED");
  if (status === 409) throw failure("SERVICE_PROTOCOL_INVALID");
  // A registry 404 is a missing organization or version, never retryable.
  if (status === 404) throw failure("SERVICE_RESOURCE_NOT_FOUND", { serviceStatus: 404 });
  if (!allowed.includes(status)) throw failure("SERVICE_UNAVAILABLE", { serviceStatus: status });
}
/** What a registry 404 names (mirrors Rust `registry::missing`): the version, or the organization of a listing or publication. */
function missing(command: PackageCommand): { kind: string; id: string; reason: string; list: string[] } {
  if (command.operation === "fetch" || command.operation === "withdraw") {
    const organization = command.input.split("/")[0]!;
    return { kind: "package", id: command.input, reason: `package ${command.input} was not found; the version does not exist, or this key cannot read it`, list: ["package", "list", organization] };
  }
  const organization = command.operation === "list" ? command.input : command.organization!;
  return { kind: "organization", id: organization, reason: `organization ${organization} was not found`, list: ["org", "list"] };
}
function matchesIdentity(value: PackageReceipt, expected: { organization: string; package: string; version: string }): void {
  if (value.reference.organization !== expected.organization || value.reference.package !== expected.package || value.reference.version !== expected.version) invalidPackage();
}
export async function runPackageCommand(command: PackageCommand, mode: OutputMode, deps: Dependencies & { processCwd: string }, environment: Environment): Promise<number> {
  const report: { result: unknown } = { result: null };
  let exitCode = 0, remote = false;
  let failed: RunnerFailure | undefined;
  let selectedCredential: string | undefined;
  const selected = environment;
  try {
    if (command.invalid !== undefined) {
      throw teach(invocationFailure(command.invalid), { kind: "command", action: "Correct the value named in Detail; `{command}` shows the accepted syntax", words: ["package", command.operation, "--help"] }, selected, mode);
    }
    let prepared: ReturnType<typeof preparePackage> | undefined;
    let expected: ReturnType<typeof referenceInput> | undefined;
    let organization: string;
    if (command.operation === "publish") { prepared = await prepareSource(command, deps.processCwd); organization = prepared.reference.organization; }
    else if (command.operation === "list") { organization = identity(command.input); if (command.cursor !== undefined) cursor(command.cursor); }
    else { expected = referenceInput(command.input); organization = expected.organization; if (command.sha256 !== undefined) digest(command.sha256); }
    const prefix = `/registry/v1/organizations/${organization}/packages`;
    const service = new Service(deps, environment, await fixtureFor(deps));
    selectedCredential = await service.registryCredential(command.operation === "publish" || command.operation === "withdraw");
    remote = true;
    if (command.operation === "publish") {
      const response = await service.registryRequest("POST", `${prefix}/${prepared!.reference.package}/versions`, selectedCredential, prepared!.bytes);
      checkStatus(response.status, [200, 201]);
      const value = receipt(responseJSON(response.bytes)); matchReceipt(value, prepared!); report.result = value;
    } else if (command.operation === "list") {
      const response = await service.registryRequest("GET", `${prefix}${command.cursor === undefined ? "" : `?cursor=${encodeURIComponent(command.cursor)}`}`, selectedCredential);
      checkStatus(response.status);
      const page = closed(responseJSON(response.bytes), ["packages", "nextCursor"]);
      if (!Array.isArray(page.packages) || page.packages.length > 25) invalidPackage();
      const packages = (page.packages as unknown[]).map(receipt);
      if (packages.some(value => value.reference.organization !== organization || value.visibility !== "public")) invalidPackage();
      if (page.nextCursor !== null) cursor(page.nextCursor);
      report.result = { packages, nextCursor: page.nextCursor };
    } else {
      const path = `${prefix}/${expected!.package}/versions/${expected!.version}`;
      if (command.operation === "withdraw") {
        const response = await service.registryRequest("POST", `${path}/withdraw`, selectedCredential); checkStatus(response.status);
        const result = closed(responseJSON(response.bytes), ["receipt", "withdrawn"]);
        if (result.withdrawn !== true) invalidPackage();
        const value = receipt(result.receipt); matchesIdentity(value, expected!); report.result = { receipt: value, withdrawn: true };
      } else {
        const response = await service.registryRequest("GET", path, selectedCredential); checkStatus(response.status);
        const value = receipt(responseJSON(response.bytes)); matchesIdentity(value, expected!);
        if (command.sha256 !== undefined && command.sha256 !== value.reference.sha256) invalidPackage();
        const artifact = await service.registryRequest("GET", `${path}/artifact`, selectedCredential); checkStatus(artifact.status);
        if (packageHash(artifact.bytes) !== value.reference.sha256) invalidPackage();
        const contents = preparePackage(responseJSON(artifact.bytes));
        if (!Buffer.from(contents.bytes).equals(Buffer.from(artifact.bytes))) invalidPackage();
        matchReceipt(value, contents);
        if (selectedCredential !== undefined && canonicalPackageJSON(value).includes(selectedCredential)) throw failure("SERVICE_PROTOCOL_INVALID");
        remote = false;
        await materializePackage(contents, value, command.outputDir!, deps.processCwd); report.result = value;
      }
    }
    if (selectedCredential !== undefined && canonicalPackageJSON(report.result).includes(selectedCredential)) throw failure("SERVICE_PROTOCOL_INVALID");
  } catch (caught) {
    // A local package failure names its reason as Rust `registry::invalid` does.
    let error = caught instanceof RunnerFailure ? caught : remote ? failure("SERVICE_PROTOCOL_INVALID") : failure("CONFIG_INVALID", { reason: "Invalid or unsafe package input." });
    if (error.code === "SERVICE_RESOURCE_NOT_FOUND") {
      const what = missing(command);
      error = explainNotFound(failure("SERVICE_RESOURCE_NOT_FOUND", { ...(error.details ?? {}), reason: what.reason }), selected, mode, what.kind, what.id, what.list);
    }
    report.result = null; exitCode = error.exitCode; failed = error;
  }
  // The service-operation/1 envelope every `cli` command prints.
  if (mode !== "human") { deps.writeStdout(structuredOutputFor(`package.${command.operation}`, mode, failed ?? report.result as Json)); return exitCode; }
  // A failure's first line carries the label; a dev build's custom endpoint also prints a banner on success.
  if (failed !== undefined) { deps.writeStderr(humanError(environmentLabel(environment), failed)); return exitCode; }
  if (environment.name !== "production") deps.writeStderr(`${environmentLabel(environment)}\n`);
  if (command.operation === "list") {
    const result = report.result as { packages: PackageReceipt[]; nextCursor: string | null };
    if (result.packages.length === 0) deps.writeStdout(`No public packages in ${command.input}.\n`);
    for (const value of result.packages) deps.writeStdout(`${value.reference.organization}/${value.reference.package}@${value.reference.version}\n`);
    if (result.nextCursor !== null) deps.writeStdout(`Next cursor: ${humanSafeScalar(result.nextCursor)}\n`);
  } else {
    const value = command.operation === "withdraw" ? (report.result as { receipt: PackageReceipt }).receipt : report.result as PackageReceipt;
    deps.writeStdout(`OpenProse package ${command.operation}: ${value.reference.organization}/${value.reference.package}@${value.reference.version}\n`);
  }
  return exitCode;
}
