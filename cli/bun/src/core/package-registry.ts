import { failure } from "./errors";
import { RunnerFailure, type OutputMode } from "./types";
import { jsonLine, humanSafeScalar } from "./output";
import { Service, fixtureFor, type Dependencies } from "./service-account";
import type { PackageCommand } from "./package-args";
import { canonicalPackageJSON, closed, digest, identity, invalidPackage, matchReceipt, packageHash, parsePackageJSON, preparePackage, receipt, version, type PackageReceipt } from "./package-format";
import { materializePackage, prepareSource } from "./package-files";
function referenceInput(input: string): { organization: string; package: string; version: string } {
  const match = /^([^/]+)\/([^/@]+)@([^/]+)$/u.exec(input);
  if (match === null) return invalidPackage();
  return { organization: identity(match[1]), package: identity(match[2]), version: version(match[3]) };
}
function cursor(input: unknown): string {
  if (typeof input !== "string" || !input.length || input.length > 210 || !/^public:[a-z0-9-]+:[0-9A-Za-z.+-]+$/u.test(input)) return invalidPackage();
  return input;
}
function checkStatus(status: number, allowed = [200]): void {
  if (status === 401 || status === 403) throw failure("SERVICE_AUTH_REQUIRED");
  if (status === 409) throw failure("SERVICE_PROTOCOL_INVALID");
  if (!allowed.includes(status)) throw failure("SERVICE_UNAVAILABLE");
}
function matchesIdentity(value: PackageReceipt, expected: { organization: string; package: string; version: string }): void {
  if (value.reference.organization !== expected.organization || value.reference.package !== expected.package || value.reference.version !== expected.version) invalidPackage();
}
export async function runPackageCommand(command: PackageCommand, mode: OutputMode, deps: Dependencies & { processCwd: string }, environment: "production" | "staging"): Promise<number> {
  const report: { schema: string; environment: string; operation: string; result: unknown; problem: unknown } = { schema: "openprose.package-operation/1", environment, operation: command.operation, result: null, problem: null };
  if (mode === "human" && environment === "staging") deps.writeStderr("OpenProse staging environment\n");
  let exitCode = 0, remote = false;
  let selectedCredential: string | undefined;
  try {
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
      const value = receipt(parsePackageJSON(response.bytes)); matchReceipt(value, prepared!); report.result = value;
    } else if (command.operation === "list") {
      const response = await service.registryRequest("GET", `${prefix}${command.cursor === undefined ? "" : `?cursor=${encodeURIComponent(command.cursor)}`}`, selectedCredential);
      checkStatus(response.status);
      const page = closed(parsePackageJSON(response.bytes), ["packages", "nextCursor"]);
      if (!Array.isArray(page.packages) || page.packages.length > 25) invalidPackage();
      const packages = (page.packages as unknown[]).map(receipt);
      if (packages.some(value => value.reference.organization !== organization || value.visibility !== "public")) invalidPackage();
      if (page.nextCursor !== null) cursor(page.nextCursor);
      report.result = { packages, nextCursor: page.nextCursor };
    } else {
      const path = `${prefix}/${expected!.package}/versions/${expected!.version}`;
      if (command.operation === "withdraw") {
        const response = await service.registryRequest("POST", `${path}/withdraw`, selectedCredential); checkStatus(response.status);
        const result = closed(parsePackageJSON(response.bytes), ["receipt", "withdrawn"]);
        if (result.withdrawn !== true) invalidPackage();
        const value = receipt(result.receipt); matchesIdentity(value, expected!); report.result = { receipt: value, withdrawn: true };
      } else {
        const response = await service.registryRequest("GET", path, selectedCredential); checkStatus(response.status);
        const value = receipt(parsePackageJSON(response.bytes)); matchesIdentity(value, expected!);
        if (command.sha256 !== undefined && command.sha256 !== value.reference.sha256) invalidPackage();
        const artifact = await service.registryRequest("GET", `${path}/artifact`, selectedCredential); checkStatus(artifact.status);
        if (packageHash(artifact.bytes) !== value.reference.sha256) invalidPackage();
        const contents = preparePackage(parsePackageJSON(artifact.bytes));
        if (!Buffer.from(contents.bytes).equals(Buffer.from(artifact.bytes))) invalidPackage();
        matchReceipt(value, contents);
        if (selectedCredential !== undefined && canonicalPackageJSON(value).includes(selectedCredential)) throw failure("SERVICE_PROTOCOL_INVALID");
        remote = false;
        await materializePackage(contents, value, command.outputDir!, deps.processCwd); report.result = value;
      }
    }
    if (selectedCredential !== undefined && canonicalPackageJSON(report.result).includes(selectedCredential)) throw failure("SERVICE_PROTOCOL_INVALID");
  } catch (caught) {
    const error = caught instanceof RunnerFailure ? caught : failure(remote ? "SERVICE_PROTOCOL_INVALID" : "CONFIG_INVALID");
    report.result = null; report.problem = error.toJSON(); exitCode = error.exitCode;
  }
  if (mode !== "human") deps.writeStdout(jsonLine(report));
  else if (report.problem !== null) {
    const problem = report.problem as { code: string; message: string; action: string };
    deps.writeStderr(`${problem.code}: ${problem.message}\n${problem.action}\n`);
  } else if (command.operation === "list") {
    const result = report.result as { packages: PackageReceipt[]; nextCursor: string | null };
    for (const value of result.packages) deps.writeStdout(`${value.reference.organization}/${value.reference.package}@${value.reference.version}\n`);
    if (result.nextCursor !== null) deps.writeStdout(`Next cursor: ${humanSafeScalar(result.nextCursor)}\n`);
  } else {
    const value = command.operation === "withdraw" ? (report.result as { receipt: PackageReceipt }).receipt : report.result as PackageReceipt;
    deps.writeStdout(`OpenProse ${environment} package ${command.operation}: ${value.reference.organization}/${value.reference.package}@${value.reference.version}\n`);
  }
  return exitCode;
}
