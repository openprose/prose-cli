import { runServiceAccount } from "./core/service-account";
import { runWeaveHost, writeHostBytes, stopHostOutput } from "./core/weave-host";
import { PUBLISHED_KERNEL_STARTUP } from "./core/build";
import { publishedKernel } from "./core/kernel-startup";
import {nativeOutputLimits} from "./adapters/output-budget";
import {nativeLimits} from "./adapters/sdk-limits";
import { nativeConfiguration } from "./adapters/native-profile";
import { parseEntrypoint, inferOutputMode } from "./core/args";
import { embeddedRuntimeImage } from "./assets/sentinel";
import runnerHelp from "../../conformance/cases/fixtures/runner-help.txt" with { type: "text" };
import deterministicMockDescriptor from "../../shared/fixtures/transport/deterministic-mock-adapter.json" with { type: "json" };
import fakeProcessDescriptor from "../../shared/fixtures/transport/mock-adapter.json" with { type: "json" };
import { resolveConfiguration, writeUserHarnessSelection } from "./core/config";
import { failure } from "./core/errors";
import { harnessById, harnesses, type HarnessDescriptor } from "./core/harnesses";
import { canonicalJson, sha256, verifyRuntimeImage } from "./core/image";
import { uuidV7 } from "./core/ids";
import { reportedConfigurationKeys, configurationExplanation, formatHumanError, humanAction, humanConfiguration, humanRunnerCommand, humanSafeMultiline, humanSafeScalar, humanVersionRepairDetails, jsonLine } from "./core/output";
import { HumanAssistantStream } from "./core/human-stream";
import { parseDurationMs, runFakeProcessTransport } from "./supervision/fake-transport";
import { fakeProtocolFailureDetails } from "./supervision/fake-failure";
import { collectSecretValues } from "./supervision/environment";
import { encodeRuntimeImage } from "./supervision/files";
import type { FakeProcessOptions, ProcessSupervisionResult, RawTransportEvent } from "./supervision/types";
import { installedAdapterDefinition } from "./adapters/recipes";
import { runInstalledAdapter, runProviderFreeInstalledAdapter } from "./adapters/runner";
import { recoverPrimeOwnedService } from "./adapters/prime-owned-service";
import type { InstalledAdapterId, InstalledAdapterOptions, ProviderFreeAdapterOptions } from "./adapters/types";
import { buildInstalledAdapterEnvironment } from "./adapters/environment";
import {
  assertInstalledAdapterRuntimePrerequisites,
  inspectInstalledAdapterRuntimePrerequisites,
  probeInstalledAdapterAuth,
  probeInstalledAdapterVersion,
  resolveInstalledExecutable,
} from "./adapters/executable";
import { nativeOutputText, recoverImageTerminalEnvelope } from "./adapters/terminal";
import { assertInstalledAdapterPlatform } from "./adapters/admission";
import providerFreeProbeSource from "../../shared/fixtures/adapters/bin/adapter_probe.py" with { type: "text" };
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute } from "node:path";
import {
  RUNNER_NAME,
  RunnerFailure,
  type EffectiveConfiguration,
  type GlobalFlags,
  type OutputMode,
  type RunnerErrorShape,
  type RunnerInvocation,
  type RunnerOperation,
  type RuntimePrerequisiteObservation,
  type RuntimeImageBundle,
  type VerifiedRuntimeImage,
} from "./core/types";
import { BUILD_PROFILE, RUNNER_BUILD_COMMIT, RUNNER_VERSION, TEST_SEAMS_ENABLED } from "./core/build";

export interface CliDependencies {
  env: Readonly<Record<string, string | undefined>>;
  processCwd: string;
  userConfigPath?: string;
  platform?: NodeJS.Platform;
  arch?: NodeJS.Architecture;
  homeDir?: string;
  clock: { now(): string; monotonicMs(): number };
  ids: { invocationId(): string };
  writeStdoutBytes?(bytes: Uint8Array): void | Promise<void>;
  writeStderrBytes?(bytes: Uint8Array): void | Promise<void>;
  stopHostOutput?(): void;
  writeStdout(text: string): void;
  writeStderr(text: string): void;
  imageBundle?: RuntimeImageBundle;
  fakeProcess?: FakeProcessOptions;
  installedAdapterProbe?: {
    executable: string;
    observationPath: string;
    credentialGroup: string;
    interpreter?: string;
    fixtureDigestRequired?: boolean;
  };
  /** Test-only override for resolving opaque failed Prime cleanup handles. */
  recoveryTemporaryRoot?: string;
  cancellationSignal?: AbortSignal;
  observeMockInvocation?(invocation: RunnerInvocation, image: VerifiedRuntimeImage): void | Promise<void>;
}

export async function runCli(args: readonly string[], dependencies: CliDependencies): Promise<number> {
  let mode = inferOutputMode(args);
  const invocationId = dependencies.ids.invocationId();
  try {
    const parsed = parseEntrypoint(args);
    if (parsed.global.serviceEnvironment !== undefined) {
      mode = parsed.kind === "operation" && parsed.json ? "json" : parsed.global.output ?? "human";
      if (parsed.kind !== "operation" || !["auth-status", "auth-login", "auth-logout", "org-list"].includes(parsed.operation) || Object.keys(parsed.global).some((key) => !["serviceEnvironment", "output", "color", "verbose"].includes(key))) throw failure("INVOCATION_INVALID");
      return await runServiceAccount(parsed.operation, mode, dependencies);
    }
    if (parsed.kind === "operation" && parsed.operation === "org-list") throw failure("INVOCATION_INVALID");
    if (parsed.kind === "weave") return await runWeaveHost(parsed.argv, parsed.global, dependencies);
    if (parsed.kind === "help") {
      dependencies.writeStdout(runnerHelp);
      return 0;
    }
    if (parsed.kind === "version") {
      dependencies.writeStdout(`prose ${RUNNER_VERSION} (bun)\n`);
      return 0;
    }
    if (parsed.kind === "operation" && parsed.operation === "prime-cleanup") {
      mode = parsed.json ? "json" : parsed.global.output ?? "human";
      return await runPrimeCleanup(parsed.value, mode, dependencies);
    }

    const config = await resolveConfiguration(parsed.global, dependencies);
    mode = parsed.kind === "operation" && parsed.json ? "json" : config.values.output;
    if (parsed.kind === "operation") {
      return await runOperation(parsed.operation, parsed.value, parsed.global, config, mode, dependencies);
    }

    const image = PUBLISHED_KERNEL_STARTUP && dependencies.imageBundle === undefined && ["codex", "claude", "prime", "omp", "agents-sdk"].includes(config.values.harness)
      ? await publishedKernel(undefined, dependencies.cancellationSignal)
      : await verifyRuntimeImage(dependencies.imageBundle ?? embeddedRuntimeImage);
    const verboseHuman = mode === "human" && config.values.verbose;
    try {
      if (verboseHuman) {
        const selected = selectHarness(config);
        const transport = negotiateTransport(selected, config.values.transport);
        dependencies.writeStderr(
          `[openprose:verbose] phase=selection harness=${humanSafeScalar(selected.id)} transport=${humanSafeScalar(transport)} execution=${parsed.global.dryRun === true ? "dry-run" : "run"}\n`,
        );
      }
      const exitCode = await runLanguage(
        parsed.argv,
        config,
        image,
        invocationId,
        mode,
        parsed.global.dryRun === true,
        dependencies,
      );
      if (verboseHuman) {
        dependencies.writeStderr(
          `[openprose:verbose] phase=settlement status=${exitCode === 0 ? "success" : "failed"} exitCode=${exitCode}\n`,
        );
      }
      return exitCode;
    } catch (caught) {
      if (verboseHuman) {
        const error = normalizeFailure(caught);
        dependencies.writeStderr(
          `[openprose:verbose] phase=settlement status=failed exitCode=${error.exitCode}\n`,
        );
      }
      throw caught;
    }
  } catch (caught) {
    const error = normalizeFailure(caught);
    emitFailure(error, mode, invocationId, dependencies);
    return error.exitCode;
  }
}

async function runOperation(
  operation: RunnerOperation,
  operationValue: string | undefined,
  explicitFlags: GlobalFlags,
  config: EffectiveConfiguration,
  mode: OutputMode,
  dependencies: CliDependencies,
): Promise<number> {
  if (operation === "harness-use") {
    if (operationValue === undefined) throw failure("INTERNAL_ERROR", { reason: "Harness selection is missing its value." });
    const selection = validateUserHarnessSelection(operationValue, explicitFlags);
    validateActiveHarnessSelection(operationValue, config);
    const changed = await writeUserHarnessSelection(config.userConfigPath, operationValue, selection);
    const report = {
      schema: "openprose.harness-selection/1",
      harness: operationValue,
      scope: "user",
      path: config.userConfigPath,
      changed,
    };
    if (mode === "human") {
      const savedRoute = selection.authProfile !== null
        ? `Route: ${humanSafeScalar(selection.authProfile)} (saved)`
        : operationValue === "codex"
          ? "Route: cached-chatgpt-login (Codex default; not saved)"
          : operationValue === "claude"
            ? "Route: claude-subscription (Claude default; not saved)"
            : "Route: OpenProse account (external route cleared)";
      const savedModel = selection.model !== null
        ? `Model: ${humanSafeScalar(selection.model)} (saved)`
        : operationValue === "codex" || operationValue === "claude"
          ? "Model: harness default (not saved)"
          : "Model: OpenProse default (external model cleared)";
      dependencies.writeStdout([
        `Default harness: ${humanSafeScalar(operationValue)} (${changed ? "updated" : "already selected"})`,
        savedRoute,
        savedModel,
        `Configuration: ${humanSafeScalar(config.userConfigPath)}`,
        `Next: ${humanRunnerCommand("cli doctor")}`,
        "",
      ].join("\n"));
    } else dependencies.writeStdout(jsonLine(report));
    return 0;
  }
  if (operation === "auth-status") {
    const problem = failure("HOSTED_UNAVAILABLE", { billingOwner: "openprose", fallbackSelected: false });
    const report = {
      schema: "openprose.account-status/1",
      availability: "unavailable",
      authenticated: null,
      authCategory: "openprose-account",
      billingOwner: "openprose",
      credentialStorage: "unavailable",
      problem: problem.toJSON(),
    };
    if (mode === "human") {
      dependencies.writeStdout([
        "OpenProse account: unavailable",
        "Authentication: unknown",
        "Credential storage: unavailable",
        `Problem: ${problem.code} — ${problem.message}`,
        `Action: ${humanAction(problem)}`,
        "",
      ].join("\n"));
    } else dependencies.writeStdout(jsonLine(report));
    return problem.exitCode;
  }
  if (operation === "auth-login" || operation === "auth-logout") {
    throw failure("HOSTED_UNAVAILABLE", { billingOwner: "openprose", fallbackSelected: false });
  }
  if (operation === "config-explain") {
    if (mode === "human") dependencies.writeStdout(humanConfiguration(config));
    else dependencies.writeStdout(jsonLine(configurationExplanation(config)));
    return 0;
  }
  const image = await verifyRuntimeImage(dependencies.imageBundle ?? embeddedRuntimeImage);
  if (operation === "harness-list") {
    const runtimeHarnesses = await harnessInventory(image, config, dependencies);
    const value = {
      schema: "openprose.harness-list/1",
      selected: config.values.harness,
      harnesses: runtimeHarnesses,
    };
    if (mode === "human") {
      dependencies.writeStdout(humanHarnessList(config.values.harness, runtimeHarnesses));
    } else dependencies.writeStdout(jsonLine(value));
    return 0;
  }

  const selected = selectHarness(config);
  const transport = negotiateTransport(selected, config.values.transport);
  let runtimeHarnesses = await harnessInventory(image, config, dependencies);
  const installed = tryInstalledAdapterDefinition(selected, transport);
  let selectedAuthReadiness: "ready" | "required" | "unknown" | "not-applicable" = selected.id === "mock"
    ? "not-applicable"
    : "unknown";
  let problem = harnessProblem(selected)
    ?? mockImageProblem(selected, image)
    ?? processTransportProblem(transport, dependencies);
  if (installed !== null && problem === null) {
    try {
      const readiness = await prepareInstalledAdapter(config, selected, transport, dependencies);
      selectedAuthReadiness = readiness.authReadiness;
      runtimeHarnesses = runtimeHarnesses.map((item) => item.id === selected.id ? {
        ...item,
        availability: "available",
        detectedVersion: readiness.version,
        ...(readiness.runtimePrerequisites.length === 0
          ? {}
          : { runtimePrerequisites: readiness.runtimePrerequisites }),
      } : item);
    } catch (caught) {
      problem = normalizeFailure(caught);
      if (problem.code === "HARNESS_NEEDS_AUTH") selectedAuthReadiness = "required";
      runtimeHarnesses = runtimeHarnesses.map((item) => item.id === selected.id ? {
        ...item,
        availability: problem!.code === "HARNESS_NEEDS_AUTH"
          ? "needs-auth"
          : problem!.code === "HARNESS_INCOMPATIBLE" ? "incompatible" : item.availability,
        ...runtimePrerequisitesFromFailure(problem!),
      } : item);
    }
  }
  const selectedStatus = runtimeHarnesses.find((item) => item.id === selected.id) ?? selected;
  const report = {
    schema: "openprose.doctor-report/1",
    runner: { name: RUNNER_NAME, version: RUNNER_VERSION, commit: RUNNER_BUILD_COMMIT },
    build: { profile: BUILD_PROFILE, testSeamsEnabled: TEST_SEAMS_ENABLED },
    ...(PUBLISHED_KERNEL_STARTUP ? { imageSource: "published-on-run" } : {}),
    ready: problem === null,
    cwd: config.cwd,
    selectedHarness: selected.id,
    selectedHarnessVersion: selectedStatus.detectedVersion,
    selectedTransport: transport,
    selectedAdapterId: adapterId(selected, transport),
    promptPlacement: selected.id === "mock"
      ? (problem === null ? "developer" : null)
      : installed?.recipe.launch.instructionPlacement.manifestPlacementId ?? null,
    isolation: selected.id === "mock"
      ? (problem === null ? "enforced" : "unsupported")
      : installed?.recipe.isolation.guarantee ?? "unsupported",
    authCategory: selected.authCategory,
    selectedAuthReadiness,
    billingOwner: selected.billingOwner,
    image: {
      formatVersion: image.manifest.imageFormatVersion,
      version: image.manifest.imageVersion,
      sha256: image.aggregateSha256,
      releaseEligible: image.manifest.releaseEligible,
    },
    configuration: configurationExplanation(config),
    harnesses: runtimeHarnesses,
    problems: problem === null ? [] : [problem.toJSON()],
  };
  if (mode === "human") {
    const humanReadiness = humanReadinessLabel(
      report.ready,
      report.selectedAuthReadiness,
      "not ready",
    );
    const detail = typeof problem?.details?.reason === "string"
      ? [`detail: ${humanSafeScalar(problem.details.reason)}`]
      : [];
    const versionRepair = problem === null ? [] : humanVersionRepairDetails(problem.toJSON());
    dependencies.writeStdout([
      `status: ${humanReadiness}`,
      `harness: ${humanSafeScalar(selected.id)}`,
      `transport: ${humanSafeScalar(transport)}`,
      `auth readiness: ${humanSafeScalar(report.selectedAuthReadiness)}`,
      `billing owner: ${humanSafeScalar(selected.billingOwner)}`,
      PUBLISHED_KERNEL_STARTUP ? "image source: published kernel (resolved on run; not fetched by doctor)" : `image: ${humanSafeScalar(image.manifest.imageVersion)} (${humanSafeScalar(image.aggregateSha256)})`,
      ...(problem === null ? [] : [
        `problem: ${humanSafeScalar(problem.code)} — ${humanSafeScalar(problem.message)}`,
        ...detail,
        ...versionRepair,
        `action: ${humanAction(problem)}`,
      ]),
      "",
    ].join("\n"));
  } else dependencies.writeStdout(jsonLine(report));
  return problem?.exitCode ?? 0;
}

function validateActiveHarnessSelection(
  harness: string,
  config: EffectiveConfiguration,
): void {
  const source = config.sources.harness;
  if (
    (source.kind === "project-config" || source.kind === "environment")
    && config.values.harness !== harness
  ) {
    throw failure("CONFIG_INVALID", {
      reason: `Saved default ${harness} would not be active because the higher-precedence ${source.kind} harness selects ${config.values.harness}; remove that override or select the same harness.`,
      selectedHarness: harness,
      effectiveHarness: config.values.harness,
      effectiveHarnessSource: source.kind,
    });
  }
}

function humanReadinessLabel(
  mechanicallyReady: boolean,
  authReadiness: string,
  blockedLabel: string,
): string {
  if (!mechanicallyReady) return blockedLabel;
  return authReadiness === "unknown"
    ? "mechanically ready; authentication unverified"
    : "ready";
}

function validateUserHarnessSelection(
  harness: string,
  explicitFlags: GlobalFlags,
): { model: string | null; authProfile: string | null } {
  const model = explicitFlags.model ?? null;
  const authProfile = explicitFlags.authProfile ?? null;
  if (harness === "openprose") {
    if (model !== null || authProfile !== null) {
      throw failure("CONFIG_INVALID", {
        reason: "The OpenProse-hosted harness does not accept an external model or auth profile selection.",
      });
    }
    return { model: null, authProfile: null };
  }

  const descriptor = harnessById(harness);
  const transport = descriptor?.transports[0];
  if (descriptor === undefined || transport === undefined || descriptor.runtime !== "installed-process") {
    throw failure("CONFIG_INVALID", { reason: `Unsupported harness selection: ${harness}.` });
  }
  const adapterId = `${harness}/${transport}` as InstalledAdapterId;
  const definition = installedAdapterDefinition(adapterId);
  if ((harness === "prime" || harness === "omp") && (model === null || authProfile === null)) {
    throw new RunnerFailure({
      code: "INVOCATION_INVALID",
      boundary: "invocation",
      message: "Runner invocation is invalid.",
      action: `Invoke the \`cli harness use ${harness}\` runner operation with both the \`--model\` and \`--auth-profile\` options, then retry.`,
      exitCode: 2,
      retryable: false,
      details: {
        adapterId,
        reason: "Prime and OMP selection requires explicit CLI --model and --auth-profile options; inherited configuration does not select a credential route.",
        requiredOptions: ["--model", "--auth-profile"],
        supportedAuthProfiles: Object.keys(definition.credentialGroups),
      },
    });
  }
  if ((harness === "prime" || harness === "omp") && !isFullyQualifiedProviderModel(model!)) {
    throw failure("CONFIG_INVALID", {
      adapterId,
      reason: "Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments.",
    });
  }
  if (authProfile !== null && definition.credentialGroups[authProfile] === undefined) {
    throw failure("CONFIG_INVALID", {
      adapterId,
      reason: `Unknown auth_profile for ${adapterId}: ${authProfile}.`,
      supportedAuthProfiles: Object.keys(definition.credentialGroups),
    });
  }
  return { model, authProfile };
}

async function runPrimeCleanup(
  handle: string | undefined,
  mode: OutputMode,
  dependencies: CliDependencies,
): Promise<number> {
  if (handle === undefined) throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is missing." });
  await recoverPrimeOwnedService({
    temporaryRoot: dependencies.recoveryTemporaryRoot ?? tmpdir(),
    handle,
  });
  const report = {
    schema: "openprose.prime-cleanup/1",
    adapterId: "prime/rpc",
    cleanupHandle: handle,
    status: "cleaned",
    serviceSettlement: "verified",
    sensitiveFilesRemoved: true,
    directoryRemoved: true,
  };
  if (mode === "human") dependencies.writeStdout([
    "Prime cleanup: complete",
    `Handle: ${humanSafeScalar(handle)}`,
    "Owned service: settled",
    "Private directory: removed",
    "",
  ].join("\n"));
  else dependencies.writeStdout(jsonLine(report));
  return 0;
}

export function humanHarnessList(
  selectedHarness: string,
  inventory: readonly HarnessDescriptor[],
): string {
  const render = (item: HarnessDescriptor): string => {
    const selected = item.id === selectedHarness;
    const primary = [
      `${selected ? "*" : " "} ${humanSafeScalar(item.id)}`,
      `availability=${humanSafeScalar(item.availability)}`,
      `transport=${humanSafeScalar(item.transports.join(","))}`,
      ...(item.detectedVersion === null ? [] : [`version=${humanSafeScalar(item.detectedVersion)}`]),
    ].join(" ") + (selected ? " (selected)" : "");
    const prerequisites = (item.runtimePrerequisites ?? []).flatMap((prerequisite) => [
      `    runtime prerequisite: ${humanSafeScalar(prerequisite.runtime)}; availability: ${humanSafeScalar(prerequisite.availability)}; detected version: ${humanSafeScalar(prerequisite.detectedVersion ?? "missing")}; required version: ${humanSafeScalar(prerequisite.versionRange === ">=1.3.14" ? "1.3.14 or newer" : prerequisite.versionRange)}`,
      `    runtime repair: ${humanSafeScalar(prerequisite.repairCommand)}`,
    ]);
    return [primary, ...prerequisites].join("\n");
  };
  const lines = [
    "Harnesses:",
    ...inventory.filter((item) => !item.testOnly).map(render),
  ];
  if (TEST_SEAMS_ENABLED) {
    const testOnly = inventory.filter((item) => item.testOnly);
    if (testOnly.length > 0) lines.push("", "Test-only harnesses:", ...testOnly.map(render));
  }
  lines.push(
    "",
    `Choose Codex: ${humanRunnerCommand("cli harness use codex")}`,
    `Choose Claude: ${humanRunnerCommand("cli harness use claude")}`,
    "Prime and OMP: set PROSE_MODEL to a fully qualified provider/model installed in that harness; unset or invalid values are refused.",
    `Choose Prime: ${humanRunnerCommand('cli harness use prime --model "$PROSE_MODEL" --auth-profile prime-harness-login')}`,
    `Choose OMP: ${humanRunnerCommand('cli harness use omp --model "$PROSE_MODEL" --auth-profile omp-harness-login')}`,
    `Then verify: ${humanRunnerCommand("cli doctor")}`,
    "",
  );
  return lines.join("\n");
}

async function runLanguage(
  argv: string[],
  config: EffectiveConfiguration,
  image: VerifiedRuntimeImage,
  invocationId: string,
  mode: OutputMode,
  dryRun: boolean,
  dependencies: CliDependencies,
): Promise<number> {
  const harness = selectHarness(config);
  const transport = negotiateTransport(harness, config.values.transport);
  const admissionProblem = harnessProblem(harness) ?? processTransportProblem(transport, dependencies);
  const adapterProbe = installedAdapterProbe(dependencies, harness, transport);
  const installedDefinition = tryInstalledAdapterDefinition(harness, transport);
  const problem = adapterProbe === null ? admissionProblem : null;
  const startedAt = dependencies.clock.now();
  const startedMonotonic = dependencies.clock.monotonicMs();
  const task = {
    schema: image.manifest.taskEnvelope.schemaId,
    argv,
    interactionMode: "non-interactive" as const,
  };
  const taskDigestSha256 = await sha256(canonicalJson(task));
  const invocation: RunnerInvocation = {
    schema: "openprose.runner-invocation/1",
    ...(nativeLimits(config.values)?{nativeLimits:nativeLimits(config.values)!}:{}),
    ...(nativeOutputLimits(config.values)?{nativeOutputLimits:nativeOutputLimits(config.values)!}:{}),
    ...(nativeConfiguration({...config.values,authProfile:config.values.authProfile??"claude-subscription"})?{nativeConfiguration:nativeConfiguration({...config.values,authProfile:config.values.authProfile??"claude-subscription"})!}:{}),
    invocationId,
    cwd: config.cwd,
    languageImage: {
      formatVersion: image.manifest.imageFormatVersion,
      version: image.manifest.imageVersion,
      sha256: image.aggregateSha256,
    },
    runner: { name: RUNNER_NAME, version: RUNNER_VERSION, commit: RUNNER_BUILD_COMMIT },
    harness: harness.id,
    transport,
    recursionToken: `openprose:${invocationId}`,
    task,
    taskDigestSha256,
  };
  const invocationRecordSha256 = await sha256(canonicalJson(invocation));
  if (harness.id === "mock" && problem !== null) {
    const result = await buildResult({
      invocation,
      invocationRecordSha256,
      image,
      harness,
      transport,
      startedAt,
      startedMonotonic,
      terminalAt: dependencies.clock.now(),
      events: [],
      error: problem,
      dependencies,
      terminalEnvelope: null,
      ...(problem.code === "HARNESS_UNAVAILABLE" ? unavailableMockResultFacts() : {}),
    });
    emitAttemptFailure(result, problem, mode, invocationId, [], dependencies);
    return problem.exitCode;
  }
  let mockTerminalEnvelope: Record<string, unknown> | null = null;
  if (harness.id === "mock") {
    try {
      mockTerminalEnvelope = sentinelTerminalEnvelope(image);
    } catch (caught) {
      const imageProblem = normalizeFailure(caught);
      const result = await buildResult({
        invocation,
        invocationRecordSha256,
        image,
        harness,
        transport,
        startedAt,
        startedMonotonic,
        terminalAt: dependencies.clock.now(),
        events: [],
        error: imageProblem,
        dependencies,
        terminalEnvelope: null,
        ...(imageProblem.code === "HARNESS_UNAVAILABLE" ? unavailableMockResultFacts() : {}),
      });
      emitAttemptFailure(result, imageProblem, mode, invocationId, [], dependencies);
      return imageProblem.exitCode;
    }
  }

  if (dryRun) {
    if (adapterProbe !== null) return emitDryRun(config, harness, transport, image, admissionProblem, mode, dependencies);
    let readiness: InstalledAdapterReadiness | null = null;
    let dryRunProblem = admissionProblem;
    if (installedDefinition !== null && dryRunProblem === null) {
      try {
        readiness = await prepareInstalledAdapter(config, harness, transport, dependencies);
      } catch (caught) {
        dryRunProblem = normalizeFailure(caught);
      }
    }
    return emitDryRun(config, harness, transport, image, dryRunProblem, mode, dependencies, readiness);
  }

  if (dependencies.env.OPENPROSE_RECURSION_TOKEN !== undefined) {
    const recursion = failure("RECURSIVE_INVOCATION", { reason: "The wrapper inherited an active OpenProse recursion marker." });
    const result = await buildResult({
      invocation, invocationRecordSha256, image, harness, transport, startedAt, startedMonotonic,
      terminalAt: dependencies.clock.now(), events: [], error: recursion, dependencies,
    });
    emitAttemptFailure(result, recursion, mode, invocationId, [], dependencies);
    return recursion.exitCode;
  }

  if (problem !== null) {
    const result = await buildResult({
      invocation,
      invocationRecordSha256,
      image,
      harness,
      transport,
      startedAt,
      startedMonotonic,
      terminalAt: dependencies.clock.now(),
      events: [],
      error: problem,
      dependencies,
    });
    emitAttemptFailure(result, problem, mode, invocationId, [], dependencies);
    return problem.exitCode;
  }

  if (adapterProbe !== null) {
    return runInstalledProbeInvocation(
      invocation,
      invocationRecordSha256,
      config,
      image,
      harness,
      transport,
      adapterProbe,
      startedAt,
      startedMonotonic,
      mode,
      dependencies,
    );
  }

  if (installedDefinition !== null) {
    let readiness: InstalledAdapterReadiness;
    try {
      readiness = await prepareInstalledAdapter(config, harness, transport, dependencies);
    } catch (caught) {
      const readinessFailure = normalizeFailure(caught);
      const result = await buildResult({
        invocation, invocationRecordSha256, image, harness, transport, startedAt, startedMonotonic,
        terminalAt: dependencies.clock.now(), events: [], error: readinessFailure, dependencies,
        descriptor: { id: installedDefinition.id },
        capabilities: installedCapabilities(installedDefinition, dependencies.platform),
      });
      emitAttemptFailure(result, readinessFailure, mode, invocation.invocationId, [], dependencies);
      return readinessFailure.exitCode;
    }
    return runInstalledInvocation(
      invocation,
      invocationRecordSha256,
      config,
      image,
      harness,
      transport,
      readiness,
      startedAt,
      startedMonotonic,
      mode,
      dependencies,
    );
  }

  if (mockTerminalEnvelope === null) {
    throw failure("INTERNAL_ERROR", { reason: "An admitted non-mock invocation reached the mock execution path." });
  }
  await dependencies.observeMockInvocation?.(invocation, image);
  if (transport === "fake-process") {
    return runFakeProcessInvocation(
      invocation,
      invocationRecordSha256,
      config,
      image,
      harness,
      startedAt,
      startedMonotonic,
      mode,
      dependencies,
    );
  }

  const terminalAt = dependencies.clock.now();
  const baseEvents = [
    event(0, "runner.started", invocationId, startedAt, {
      kind: "runner.started",
      runnerName: RUNNER_NAME,
      runnerVersion: RUNNER_VERSION,
    }),
    event(1, "harness.started", invocationId, startedAt, {
      kind: "harness.started",
      harness: "mock",
      transport,
      harnessVersion: "1.0.0",
    }),
    event(2, "harness.completed", invocationId, terminalAt, {
      kind: "harness.completed",
      terminalEventObserved: true,
      exitCode: 0,
      signal: null,
    }),
  ];
  const result = await buildResult({
    invocation,
    invocationRecordSha256,
    image,
    harness,
    transport,
    startedAt,
    startedMonotonic,
    terminalAt,
    events: baseEvents,
    error: null,
    dependencies,
    harnessVersion: "1.0.0",
    descriptor: deterministicMockDescriptor,
    terminalEnvelope: mockTerminalEnvelope,
  });

  if (mode === "human") {
    dependencies.writeStdout("OpenProse run completed (mock).\n");
  } else if (mode === "json") {
    dependencies.writeStdout(jsonLine(result));
  } else {
    for (const record of baseEvents) dependencies.writeStdout(jsonLine(record));
    dependencies.writeStdout(jsonLine(event(3, "runner.completed", invocationId, terminalAt, {
      kind: "runner.completed",
      result,
    })));
  }
  return 0;
}

async function runFakeProcessInvocation(
  invocation: RunnerInvocation,
  invocationRecordSha256: string,
  config: EffectiveConfiguration,
  image: VerifiedRuntimeImage,
  harness: HarnessDescriptor,
  startedAt: string,
  startedMonotonic: number,
  mode: OutputMode,
  dependencies: CliDependencies,
): Promise<number> {
  const options = fakeProcessOptions(dependencies);
  if (options === null) {
    const unavailable = failure("TRANSPORT_UNSUPPORTED", {
      harness: "mock",
      transport: "fake-process",
      reason: "The test-only fake harness executable was not supplied.",
    });
    const result = await buildResult({
      invocation, invocationRecordSha256, image, harness, transport: "fake-process", startedAt, startedMonotonic,
      terminalAt: dependencies.clock.now(), events: [], error: unavailable, dependencies,
      descriptor: fakeProcessDescriptor,
    });
    emitAttemptFailure(result, unavailable, mode, invocation.invocationId, [], dependencies);
    return unavailable.exitCode;
  }

  let outcome: ProcessSupervisionResult;
  try {
    outcome = await runFakeProcessTransport(
      invocation,
      image,
      dependencies.env,
      parseDurationMs(config.values.timeout),
      options,
    );
  } catch (caught) {
    const processFailure = normalizeFailure(caught);
    const result = await buildResult({
      invocation, invocationRecordSha256, image, harness, transport: "fake-process", startedAt, startedMonotonic,
      terminalAt: dependencies.clock.now(), events: [], error: processFailure, dependencies,
      descriptor: fakeProcessDescriptor,
    });
    emitAttemptFailure(result, processFailure, mode, invocation.invocationId, [], dependencies);
    return processFailure.exitCode;
  }

  if (outcome.stderr.length > 0) {
    dependencies.writeStderr(mode === "human" ? humanSafeMultiline(outcome.stderr) : outcome.stderr);
  }
  let attemptedError = outcome.error === null ? null : failure(outcome.error.code, {
    ...fakeProtocolFailureDetails(outcome),
    processExit: outcome.exitCode,
    processSignal: outcome.signal,
    terminalEventObserved: outcome.terminalEnvelope !== null,
  });
  const terminalAt = dependencies.clock.now();
  const baseEvents = processEvents(invocation.invocationId, startedAt, terminalAt, outcome);
  const result = await buildResult({
    invocation,
    invocationRecordSha256,
    image,
    harness,
    transport: "fake-process",
    startedAt,
    startedMonotonic,
    terminalAt,
    events: baseEvents,
    error: attemptedError,
    dependencies,
    harnessVersion: outcome.harnessVersion,
    descriptor: fakeProcessDescriptor,
    terminalEnvelope: outcome.terminalEnvelope,
    process: outcome,
  });
  if (attemptedError !== null) {
    emitAttemptFailure(result, attemptedError, mode, invocation.invocationId, baseEvents, dependencies);
    return attemptedError.exitCode;
  }
  emitSuccess(result, baseEvents, mode, invocation.invocationId, terminalAt, dependencies, "OpenProse transport fixture completed. No language semantics were evaluated.\n");
  return 0;
}

async function runInstalledProbeInvocation(
  invocation: RunnerInvocation,
  invocationRecordSha256: string,
  config: EffectiveConfiguration,
  image: VerifiedRuntimeImage,
  harness: HarnessDescriptor,
  transport: string,
  probe: NonNullable<CliDependencies["installedAdapterProbe"]>,
  startedAt: string,
  startedMonotonic: number,
  mode: OutputMode,
  dependencies: CliDependencies,
): Promise<number> {
  const adapterId = `${harness.id}/${transport}` as InstalledAdapterId;
  const definition = installedAdapterDefinition(adapterId);
  assertInstalledAdapterPlatform(adapterId, { platform: dependencies.platform, arch: dependencies.arch });
  if (probe.fixtureDigestRequired === true) await verifyProviderFreeProbeControl(probe);
  let outcome: Awaited<ReturnType<typeof runProviderFreeInstalledAdapter>>;
  try {
    await assertInstalledAdapterRuntimePrerequisites({
      adapterId,
      cwd: config.cwd,
      ambient: dependencies.env,
      wrapperExecutable: process.execPath,
      ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
    });
    const options: ProviderFreeAdapterOptions = {
      adapterId,
      executable: probe.executable,
      observationPath: probe.observationPath,
      credentialGroup: probe.credentialGroup,
      invocation,
      image,
      ambient: dependencies.env,
      timeoutMs: parseDurationMs(config.values.timeout),
      wrapperExecutable: process.execPath,
      ...(dependencies.cancellationSignal === undefined
        ? {}
        : { cancelSignal: dependencies.cancellationSignal }),
      ...(probe.interpreter === undefined ? {} : { fixtureInterpreter: probe.interpreter }),
      ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
      ...(dependencies.arch === undefined ? {} : { arch: dependencies.arch }),
    };
    outcome = await runProviderFreeInstalledAdapter(options);
  } catch (caught) {
    const processFailure = normalizeFailure(caught);
    const result = await buildResult({
      invocation, invocationRecordSha256, image, harness, transport, startedAt, startedMonotonic,
      terminalAt: dependencies.clock.now(), events: [], error: processFailure, dependencies,
      descriptor: { id: adapterId },
      capabilities: installedCapabilities(definition, dependencies.platform),
    });
    emitAttemptFailure(result, processFailure, mode, invocation.invocationId, [], dependencies);
    return processFailure.exitCode;
  }

  if (outcome.publicStderr.length > 0) {
    dependencies.writeStderr(mode === "human" ? humanSafeMultiline(outcome.publicStderr) : outcome.publicStderr);
  }
  const attemptedError = outcome.process.error === null
    ? failure("SEMANTIC_STATUS_UNKNOWN", {
      adapterId,
      admissionStatus: "blocked",
      transportTerminalObserved: true,
      fallbackAttempted: false,
    })
    : failure(
      outcome.process.error.code,
      installedProcessFailureDetails(outcome.process, adapterId),
    );
  const terminalAt = dependencies.clock.now();
  const baseEvents = processEvents(
    invocation.invocationId,
    startedAt,
    terminalAt,
    outcome.process,
    harness.id,
    transport,
    false,
    outcome.publicEvents,
  );
  const result = await buildResult({
    invocation,
    invocationRecordSha256,
    image,
    harness,
    transport,
    startedAt,
    startedMonotonic,
    terminalAt,
    events: baseEvents,
    error: attemptedError,
    dependencies,
    harnessVersion: null,
    descriptor: { id: adapterId },
    terminalEnvelope: null,
    process: outcome.process,
    capabilities: installedCapabilities(definition, dependencies.platform),
    deliveredImageSha256: outcome.plan.imageSha256,
    renderedPayloadSha256: outcome.plan.renderedPayloadSha256,
  });
  emitAttemptFailure(result, attemptedError, mode, invocation.invocationId, baseEvents, dependencies);
  return attemptedError.exitCode;
}

interface InstalledAdapterReadiness {
  adapterId: InstalledAdapterId;
  executable: string;
  version: string;
  credentialGroup: string;
  authReadiness: "ready" | "unknown";
  runtimePrerequisites: RuntimePrerequisiteObservation[];
}

async function prepareInstalledAdapter(
  config: EffectiveConfiguration,
  harness: HarnessDescriptor,
  transport: string,
  dependencies: CliDependencies,
): Promise<InstalledAdapterReadiness> {
  const adapterId = `${harness.id}/${transport}` as InstalledAdapterId;
  const definition = installedAdapterDefinition(adapterId);
  assertInstalledAdapterPlatform(adapterId, { platform: dependencies.platform, arch: dependencies.arch });
  if ((dependencies.platform ?? process.platform) === "win32") {
    throw failure("TRANSPORT_UNSUPPORTED", {
      adapterId,
      reason: "Functional-alpha installed-process execution is not yet admitted on Windows.",
      fallbackAttempted: false,
    });
  }
  if (adapterId === "prime/rpc" || adapterId === "omp/rpc") {
    if (config.values.model === null) {
      throw failure("CONFIG_INVALID", {
        adapterId,
        reason: "Prime and OMP require an explicit fully qualified provider/model for functional-alpha execution (`--model` or PROSE_MODEL).",
      });
    }
    if (!isFullyQualifiedProviderModel(config.values.model)) {
      throw failure("CONFIG_INVALID", {
        adapterId,
        reason: "Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments.",
      });
    }
  }
  const credentialGroup = selectedCredentialGroup(adapterId, config.values.authProfile);
  const executable = await resolveInstalledExecutable({
    adapterId,
    ambient: dependencies.env,
    wrapperExecutable: process.execPath,
    ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
    ...(dependencies.arch === undefined ? {} : { arch: dependencies.arch }),
  });
  const runtimePrerequisites = await assertInstalledAdapterRuntimePrerequisites({
    adapterId,
    cwd: config.cwd,
    ambient: dependencies.env,
    wrapperExecutable: process.execPath,
    ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
  });
  const version = await probeInstalledAdapterVersion({
    adapterId,
    executable,
    cwd: config.cwd,
    ambient: dependencies.env,
    wrapperExecutable: process.execPath,
    ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
    ...(dependencies.arch === undefined ? {} : { arch: dependencies.arch }),
  });
  const environment = buildInstalledAdapterEnvironment({
    definition,
    ambient: dependencies.env,
    credentialGroup,
    ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
  });
  const authReadiness = adapterId === "prime/rpc" || adapterId === "omp/rpc"
    ? "unknown"
    : await probeInstalledAdapterAuth({
      adapterId,
      executable,
      cwd: config.cwd,
      credentialGroup,
      environment,
      ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
      ...(dependencies.arch === undefined ? {} : { arch: dependencies.arch }),
    });
  return { adapterId, executable, version, credentialGroup, authReadiness, runtimePrerequisites };
}

function isFullyQualifiedProviderModel(model: string): boolean {
  const segments = model.split("/");
  return segments.length >= 2 && segments.every((segment) =>
    segment.length > 0 && !/[\s\p{Cc}]/u.test(segment));
}

function selectedCredentialGroup(adapterId: InstalledAdapterId, configured: string | null): string {
  const definition = installedAdapterDefinition(adapterId);
  const fallback: Partial<Record<InstalledAdapterId, string>> = {
    "codex/exec-json": "cached-chatgpt-login",
    "claude/print-stream-json": "claude-subscription",
  };
  const selected = configured ?? fallback[adapterId];
  if (selected === undefined) {
    throw failure("CONFIG_INVALID", {
      adapterId,
      reason: "This harness requires an explicit auth_profile (or PROSE_AUTH_PROFILE); credential routes are never guessed.",
      supportedAuthProfiles: Object.keys(definition.credentialGroups),
    });
  }
  if (definition.credentialGroups[selected] === undefined) {
    throw failure("CONFIG_INVALID", {
      adapterId,
      reason: `Unknown auth_profile for ${adapterId}: ${selected}.`,
      supportedAuthProfiles: Object.keys(definition.credentialGroups),
    });
  }
  return selected;
}

async function runInstalledInvocation(
  invocation: RunnerInvocation,
  invocationRecordSha256: string,
  config: EffectiveConfiguration,
  image: VerifiedRuntimeImage,
  harness: HarnessDescriptor,
  transport: string,
  readiness: InstalledAdapterReadiness,
  startedAt: string,
  startedMonotonic: number,
  mode: OutputMode,
  dependencies: CliDependencies,
): Promise<number> {
  const definition = installedAdapterDefinition(readiness.adapterId);
  const humanOutput: { stream: HumanAssistantStream | null } = { stream: null };
  const humanProtectedLiterals = [
    ...collectSecretValues(dependencies.env),
    ...invocation.task.argv,
    ...(config.values.model === null ? [] : [config.values.model]),
    invocation.cwd,
    invocation.invocationId,
    invocation.recursionToken,
    canonicalJson(invocation.task),
    new TextDecoder().decode(encodeRuntimeImage(image)),
  ];
  let outcome: Awaited<ReturnType<typeof runInstalledAdapter>>;
  try {
    const options: InstalledAdapterOptions = {
      outputContract: config.values.outputContract ?? "image-envelope",
      adapterId: readiness.adapterId,
      executable: readiness.executable,
      harnessVersion: readiness.version,
      credentialGroup: readiness.credentialGroup,
      invocation,
      image,
      ambient: dependencies.env,
      timeoutMs: parseDurationMs(config.values.timeout),
      model: config.values.model,
      ...(config.values.nativeLog ? {nativeLog:config.values.nativeLog}:{}),
      permissionMode: config.values.permissionMode ?? null,
      ...(config.values.nativeMaxTurns===undefined?{}:{nativeMaxTurns:config.values.nativeMaxTurns}),
      ...(config.values.nativeTimeout===undefined?{}:{nativeTimeout:config.values.nativeTimeout}),
      ...(config.values.nativeToolTimeout===undefined?{}:{nativeToolTimeout:config.values.nativeToolTimeout}),
      ...(config.values.nativeOutputBytes===undefined?{}:{nativeOutputBytes:config.values.nativeOutputBytes}),
      ...(config.values.nativeProfile===undefined?{}:{nativeProfile:config.values.nativeProfile}),
      ...(config.values.nativeAddDirs===undefined?{}:{nativeAddDirs:config.values.nativeAddDirs}),
      ...(config.values.nativeAllowTools===undefined?{}:{nativeAllowTools:config.values.nativeAllowTools}),
      wrapperExecutable: process.execPath,
      ...(mode !== "human"
        ? {}
        : {
          createAssistantMessageSink: (protectedValues: readonly string[]) => {
            humanOutput.stream = new HumanAssistantStream({
              write: dependencies.writeStdout,
              protectedLiterals: [...humanProtectedLiterals, ...protectedValues],
            });
            return (text: string) => humanOutput.stream?.accept(text);
          },
        }),
      ...(dependencies.cancellationSignal === undefined ? {} : { cancelSignal: dependencies.cancellationSignal }),
      ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
      ...(dependencies.arch === undefined ? {} : { arch: dependencies.arch }),
    };
    outcome = await runInstalledAdapter(options);
  } catch (caught) {
    humanOutput.stream?.abort();
    const processFailure = normalizeFailure(caught);
    const result = await buildResult({
      invocation, invocationRecordSha256, image, harness, transport, startedAt, startedMonotonic,
      terminalAt: dependencies.clock.now(), events: [], error: processFailure, dependencies,
      harnessVersion: readiness.version,
      descriptor: { id: readiness.adapterId },
      capabilities: installedCapabilities(definition, dependencies.platform),
    });
    emitAttemptFailure(result, processFailure, mode, invocation.invocationId, [], dependencies);
    return processFailure.exitCode;
  }

  if (outcome.publicStderr.length > 0) {
    dependencies.writeStderr(mode === "human" ? humanSafeMultiline(outcome.publicStderr) : outcome.publicStderr);
  }
  let attemptedError = outcome.process.error === null
    ? null
    : failure(
      outcome.process.error.code,
      installedProcessFailureDetails(outcome.process, readiness.adapterId),
    );
  let terminalEnvelope: Record<string, unknown> | null = null;
  const nativeOutput = config.values.outputContract === "native";
  if (attemptedError === null && !nativeOutput) {
    try {
      terminalEnvelope = recoverImageTerminalEnvelope(image, outcome.process.events, invocation);
    } catch (caught) {
      attemptedError = normalizeFailure(caught);
    }
  }
  const terminalAt = dependencies.clock.now();
  const baseEvents = processEvents(
    invocation.invocationId,
    startedAt,
    terminalAt,
    outcome.process,
    harness.id,
    transport,
    !nativeOutput && terminalEnvelope !== null,
    outcome.publicEvents,
  );
  const result = await buildResult({
    invocation,
    invocationRecordSha256,
    image,
    harness,
    transport,
    startedAt,
    startedMonotonic,
    terminalAt,
    events: baseEvents,
    error: attemptedError,
    dependencies,
    harnessVersion: readiness.version,
    descriptor: { id: readiness.adapterId },
    terminalEnvelope,
    nativeOutput,
    process: outcome.process,
    capabilities: installedCapabilities(definition, dependencies.platform),
    deliveredImageSha256: outcome.plan.imageSha256,
    renderedPayloadSha256: outcome.plan.renderedPayloadSha256,
  });
  if(outcome.nativeConfiguration) result.nativeConfiguration=outcome.nativeConfiguration;
  if (attemptedError !== null) {
    humanOutput.stream?.abort();
    emitAttemptFailure(result, attemptedError, mode, invocation.invocationId, baseEvents, dependencies);
    return attemptedError.exitCode;
  }
  const humanMessage = nativeOutput
    ? nativeOutputText(outcome.publicEvents.filter(item => item.type === "assistant.message").map(item => item.text ?? ""))
    : placeholderHumanOutput(outcome.publicEvents, harness.id);
  humanOutput.stream?.completeSuccess(humanMessage);
  emitSuccess(
    result,
    baseEvents,
    mode,
    invocation.invocationId,
    terminalAt,
    dependencies,
    humanOutput.stream === null ? humanSafeMultiline(humanMessage) : "",
  );
  return 0;
}

function placeholderHumanOutput(events: readonly RawTransportEvent[], harnessId: string): string {
  const assistant = events
    .filter((item) => item.type === "assistant.message")
    .map((item) => item.text ?? "")
    .join("\n");
  const lines = assistant.split("\n");
  while (lines.length > 0 && lines.at(-1)!.trim().length === 0) lines.pop();
  if (lines.length > 0) lines.pop();
  const visible = lines.join("\n").trimEnd();
  if (visible.length > 0) {
    const maximumCharacters = 64 * 1024;
    const bounded = visible.length <= maximumCharacters
      ? visible
      : `${visible.slice(0, maximumCharacters)}\n[OpenProse truncated harness output]`;
    return `${bounded}\n`;
  }
  return `OpenProse placeholder run completed with ${harnessId}. No language semantics were evaluated.\n`;
}

function processEvents(
  invocationId: string,
  startedAt: string,
  terminalAt: string,
  outcome: ProcessSupervisionResult,
  harness = "mock",
  transport = "fake-process",
  stripImageTerminal = false,
  publicEvents: readonly RawTransportEvent[] = outcome.events,
): Array<Record<string, unknown>> {
  const records = [event(0, "runner.started", invocationId, startedAt, {
    kind: "runner.started", runnerName: RUNNER_NAME, runnerVersion: RUNNER_VERSION,
  })];
  let finalAssistantIndex = -1;
  if (stripImageTerminal) {
    for (let index = publicEvents.length - 1; index >= 0; index -= 1) {
      if (publicEvents[index]!.type === "assistant.message") {
        finalAssistantIndex = index;
        break;
      }
    }
  }
  for (const [index, raw] of publicEvents.entries()) {
    if (raw.type === "session.started") {
      records.push(event(records.length, "harness.started", invocationId, startedAt, {
        kind: "harness.started", harness, transport, harnessVersion: raw.harnessVersion ?? null,
      }));
    } else if (raw.type === "assistant.message") {
      const text = index === finalAssistantIndex ? withoutFinalNonblankLine(raw.text ?? "") : raw.text ?? "";
      if (text.length === 0) continue;
      records.push(event(records.length, "assistant.message", invocationId, startedAt, {
        kind: "assistant.message", text,
      }));
    }
  }
  if (outcome.cancellationReason !== null) {
    records.push(event(records.length, "runner.cancelled", invocationId, terminalAt, {
      kind: "runner.cancelled", reason: outcome.cancellationReason,
    }));
  }
  records.push(event(records.length, "harness.completed", invocationId, terminalAt, {
    kind: "harness.completed",
    terminalEventObserved: outcome.terminalEventObserved,
    exitCode: outcome.exitCode,
    signal: outcome.signal,
  }));
  return records;
}

export function installedProcessFailureDetails(
  process: ProcessSupervisionResult,
  adapterId: InstalledAdapterId,
): Record<string, unknown> {
  const adapterDiagnostic = process.error?.details?.adapterDiagnostic;
  const transportDiagnostic = process.error?.details?.transportDiagnostic;
  const nonterminalReason = process.error?.details?.reason === "unsupported_nonterminal_settlement"
    ? "unsupported_nonterminal_settlement"
    : undefined;
  return {
    processExit: process.exitCode,
    processSignal: process.signal,
    terminalEventObserved: process.terminalEventObserved,
    adapterId,
    fallbackAttempted: false,
    ...(adapterDiagnostic === undefined ? {} : { adapterDiagnostic }),
    ...(transportDiagnostic === undefined ? {} : { transportDiagnostic }),
    ...(process.error?.details?.nativeFailure===undefined?{}:{nativeFailure:process.error.details.nativeFailure}),
    ...(nonterminalReason === undefined ? {} : { reason: nonterminalReason }),
  };
}

function withoutFinalNonblankLine(value: string): string {
  const lines = value.split("\n");
  while (lines.length > 0 && lines.at(-1)!.trim().length === 0) lines.pop();
  if (lines.length > 0) lines.pop();
  return lines.join("\n").trimEnd();
}

function emitSuccess(
  result: Record<string, unknown>,
  baseEvents: Array<Record<string, unknown>>,
  mode: OutputMode,
  invocationId: string,
  terminalAt: string,
  dependencies: CliDependencies,
  humanMessage: string,
): void {
  if (mode === "human") dependencies.writeStdout(humanMessage);
  else if (mode === "json") dependencies.writeStdout(jsonLine(result));
  else {
    for (const record of baseEvents) dependencies.writeStdout(jsonLine(record));
    dependencies.writeStdout(jsonLine(event(baseEvents.length, "runner.completed", invocationId, terminalAt, {
      kind: "runner.completed", result,
    })));
  }
}

interface ResultInput {
  invocation: RunnerInvocation;
  invocationRecordSha256: string;
  image: VerifiedRuntimeImage;
  harness: HarnessDescriptor;
  transport: string;
  startedAt: string;
  startedMonotonic: number;
  terminalAt: string;
  events: Array<Record<string, unknown>>;
  error: RunnerFailure | null;
  dependencies: CliDependencies;
  harnessVersion?: string | null;
  descriptor?: Record<string, unknown>;
  nativeOutput?: boolean;
  terminalEnvelope?: Record<string, unknown> | null;
  process?: ProcessSupervisionResult;
  capabilities?: Record<string, string>;
  deliveredImageSha256?: string | null;
  renderedPayloadSha256?: string | null;
}

function unavailableMockResultFacts(): Pick<
  ResultInput,
  "harnessVersion" | "descriptor" | "capabilities"
> {
  return {
    harnessVersion: null,
    descriptor: { id: "mock/unavailable" },
    capabilities: {
      promptPlacement: "unsupported",
      isolation: "unsupported",
      streaming: "unsupported",
      cancellation: "unsupported",
      terminal: "unsupported",
    },
  };
}

async function buildResult(input: ResultInput): Promise<Record<string, unknown>> {
  const failed = input.error !== null;
  const descriptor = input.descriptor ?? mockDescriptor(input.harness, input.transport);
  const installed = tryInstalledAdapterDefinition(input.harness, input.transport);
  const descriptorDigestSha256 = installed !== null
    ? installed.recipeSha256
    : input.harness.id === "mock" && descriptor.id !== "mock/unavailable"
      ? await sha256(canonicalJson(descriptor))
      : await sha256(String(descriptor.id));
  const cwdIdentitySha256 = await sha256(input.invocation.cwd);
  const normalizedEventsSha256 = await sha256(input.events.map((item) => `${canonicalJson(item)}\n`).join(""));
  const terminalEnvelope = input.terminalEnvelope === undefined
    ? (failed ? null : sentinelTerminalEnvelope(input.image))
    : input.terminalEnvelope;
  const processTransport = input.transport === "fake-process";
  const fixtureProcessGroupActive = input.process !== undefined
    && input.harness.id === "mock"
    && input.transport === "fake-process";
  const terminalClassification = classifyTerminal(input.error, input.process, terminalEnvelope);
  // A terminal envelope is accepted as semantic evidence only after its
  // transport settles successfully. Preserve the schema-authoritative value
  // exactly on success; a failed transport has no accepted semantic result.
  const semanticStatus = input.nativeOutput && !failed ? "not-applicable" : !failed && typeof terminalEnvelope?.semanticStatus === "string"
    ? terminalEnvelope.semanticStatus
    : "unknown";
  const runnerExitCode = input.error?.exitCode ?? (semanticStatus === "semantic-failed" ? 30 : 0);
  return {
    schema: "openprose.runner-result/1",
    ...(input.invocation.nativeLimits?{nativeLimits:input.invocation.nativeLimits}:{}),
    ...(input.invocation.nativeOutputLimits?{nativeOutputLimits:input.invocation.nativeOutputLimits}:{}),
    ...(input.invocation.nativeConfiguration?{nativeConfiguration:input.invocation.nativeConfiguration}:{}),
    invocationId: input.invocation.invocationId,
    runner: { name: RUNNER_NAME, version: RUNNER_VERSION, commit: RUNNER_BUILD_COMMIT },
    adapter: {
      id: descriptor.id,
      harnessVersion: input.harnessVersion !== undefined
        ? input.harnessVersion
        : (input.harness.id === "mock" && !processTransport ? "1.0.0" : null),
      descriptorDigestSha256,
    },
    transport: input.transport,
    negotiatedCapabilities: input.capabilities ?? {
      promptPlacement: input.harness.id === "mock" ? "developer" : "unsupported",
      isolation: input.harness.id === "mock" ? "test-fixture" : "unsupported",
      streaming: input.harness.id === "mock" ? "structured" : "unsupported",
      cancellation: fixtureProcessGroupActive ? "process-tree" : "unsupported",
      terminal: input.harness.id === "mock" ? "structured" : "unsupported",
    },
    languageImage: {
      formatVersion: input.image.manifest.imageFormatVersion,
      version: input.image.manifest.imageVersion,
      sha256: input.image.aggregateSha256,
    },
    digests: {
      invocationSha256: input.invocationRecordSha256,
      taskSha256: input.invocation.taskDigestSha256,
      normalizedEventsSha256,
      deliveredImageSha256: input.deliveredImageSha256 === undefined
        ? (input.harness.id === "mock" && (input.process !== undefined || !failed) ? input.image.aggregateSha256 : null)
        : input.deliveredImageSha256,
      renderedPayloadSha256: input.renderedPayloadSha256 ?? null,
    },
    cwd: { path: input.invocation.cwd, identitySha256: cwdIdentitySha256 },
    timing: {
      startedAt: input.startedAt,
      firstEventAt: input.events.length === 0 ? null : input.startedAt,
      cancellationAt: input.process?.cancellationReason === null || input.process === undefined ? null : input.terminalAt,
      terminalAt: input.terminalAt,
      durationMs: Math.max(0, Math.round(input.dependencies.clock.monotonicMs() - input.startedMonotonic)),
    },
    terminal: {
      classification: terminalClassification,
      transportCompleted: input.process === undefined
        ? !failed
        : input.process.terminalEventObserved && input.process.exitCode === 0,
      terminalEventObserved: input.process?.terminalEventObserved ?? terminalEnvelope !== null,
      exitCode: input.process?.exitCode ?? (failed ? null : 0),
      signal: input.process?.signal ?? null,
    },
    semantic: {
      status: semanticStatus,
      terminalSchemaSha256: input.image.manifest.terminalEnvelope.sha256,
      terminalEnvelopeDigestSha256: terminalEnvelope === null ? null : await sha256(canonicalJson(terminalEnvelope)),
    },
    usage: { status: "unavailable" },
    billing: { owner: input.harness.billingOwner, authCategory: input.harness.authCategory },
    diagnosticRefs: [],
    runnerExitCode,
    ...(input.error === null ? {} : { error: input.error.toJSON() }),
  };
}

function classifyTerminal(
  error: RunnerFailure | null,
  processResult: ProcessSupervisionResult | undefined,
  terminalEnvelope: Record<string, unknown> | null,
): "success" | "semantic-failed" | "cancelled" | "timeout" | "exit-code" | "runner-error" {
  if (error === null) return terminalEnvelope?.semanticStatus === "semantic-failed" ? "semantic-failed" : "success";
  if (error.code === "CANCELLED") return "cancelled";
  if (error.code === "STARTUP_TIMEOUT") return "timeout";
  if (error.code === "HARNESS_FAILED" && processResult?.exitCode !== null) return "exit-code";
  return "runner-error";
}

function sentinelTerminalEnvelope(image: VerifiedRuntimeImage): Record<string, unknown> {
  if (image.manifest.purpose !== "sentinel-transport-test" || image.manifest.releaseEligible) {
    throw failure("HARNESS_UNAVAILABLE", {
      harness: "mock",
      admissionStatus: "blocked",
      admissionBlock: "release-image",
      reason: "The deterministic mock is available only with the release-ineligible sentinel image.",
    });
  }
  const artifact = image.files.get(image.manifest.terminalEnvelope.path);
  if (artifact === undefined) throw failure("IMAGE_INVALID", { reason: "The terminal schema artifact is missing." });
  let schema: unknown;
  try {
    schema = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(artifact));
  } catch {
    throw failure("IMAGE_INVALID", { reason: "The terminal schema artifact is not canonical JSON." });
  }
  const schemaRecord = plainRecord(schema);
  const properties = plainRecord(schemaRecord?.properties);
  const required = schemaRecord?.required;
  const exactFields = ["schema", "semanticStatus", "marker"];
  const closedShape = schemaRecord?.type === "object"
    && schemaRecord.additionalProperties === false
    && properties !== undefined
    && hasExactFields(properties, exactFields)
    && Array.isArray(required)
    && required.length === exactFields.length
    && exactFields.every((field) => required.includes(field));
  if (!closedShape || properties === undefined) {
    throw failure("IMAGE_INVALID", { reason: "The sentinel terminal schema is not the closed three-field contract." });
  }
  const schemaId = plainRecord(properties.schema)?.const;
  const status = plainRecord(properties.semanticStatus)?.const;
  const marker = plainRecord(properties.marker)?.const;
  if (
    typeof schemaId !== "string"
    || schemaId.length === 0
    || schemaId !== image.manifest.terminalEnvelope.schemaId
  ) {
    throw failure("IMAGE_INVALID", { reason: "The sentinel terminal schema ID does not match the manifest authority." });
  }
  if (status !== "not-applicable") {
    throw failure("IMAGE_INVALID", { reason: "The sentinel terminal schema does not authorize not-applicable." });
  }
  if (typeof marker !== "string" || marker.length === 0) {
    throw failure("IMAGE_INVALID", { reason: "The sentinel terminal schema does not const-authorize a marker." });
  }
  return {
    schema: schemaId,
    semanticStatus: status,
    marker,
  };
}

function plainRecord(value: unknown): Record<string, unknown> | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function hasExactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === fields.length && fields.every((field) => Object.hasOwn(value, field));
}

function emitDryRun(
  config: EffectiveConfiguration,
  harness: HarnessDescriptor,
  transport: string,
  image: VerifiedRuntimeImage,
  problem: RunnerFailure | null,
  mode: OutputMode,
  dependencies: CliDependencies,
  readiness: InstalledAdapterReadiness | null = null,
): number {
  const installed = tryInstalledAdapterDefinition(harness, transport);
  const report = {
    schema: "openprose.runner-dry-run-report/1",
    ...(nativeLimits(config.values)?{nativeLimits:nativeLimits(config.values)!}:{}),
    ...(nativeOutputLimits(config.values)?{nativeOutputLimits:nativeOutputLimits(config.values)!}:{}),
    wouldStartModel: false,
    ...(nativeConfiguration({...config.values,authProfile:readiness?.credentialGroup??config.values.authProfile}) ? {nativeConfiguration:nativeConfiguration({...config.values,authProfile:readiness?.credentialGroup??config.values.authProfile})}:{}),
    cwd: config.cwd,
    selection: {
      harness: harness.id,
      transport,
      adapterId: harness.id === "mock" ? (transport === "fake-process" ? "mock/fake-process" : "mock/in-memory") : `${harness.id}/${transport}`,
      runtimeVersion: harness.id === "mock" ? "1.0.0" : readiness?.version ?? null,
      model: config.values.model,
    },
    prompt: {
      placement: harness.id === "mock"
        ? "developer"
        : installed?.recipe.launch.instructionPlacement.manifestPlacementId ?? null,
      strictness: harness.id === "mock"
        ? "strict"
        : installed?.recipe.launch.instructionPlacement.strictness ?? "unsupported",
    },
    isolation: harness.id === "mock"
      ? "test-fixture"
      : installed === null ? "unsupported" : reportedIsolation(installed.recipe.isolation.guarantee),
    auth: {
      category: harness.authCategory,
      readiness: harness.id === "mock" ? "not-applicable" : readiness?.authReadiness ?? "unknown",
    },
    billingOwner: harness.billingOwner,
    languageImage: {
      formatVersion: image.manifest.imageFormatVersion,
      version: image.manifest.imageVersion,
      sha256: image.aggregateSha256,
      releaseEligible: image.manifest.releaseEligible,
    },
    configuration: configurationProvenance(config),
    readiness: problem === null ? "ready" : "blocked",
    blockingError: problem?.toJSON() ?? null,
  };
  if (mode === "human") {
    const humanReadiness = humanReadinessLabel(
      problem === null,
      report.auth.readiness,
      "blocked",
    );
    dependencies.writeStdout([
      `Dry run: ${humanReadiness}`,
      `Harness: ${humanSafeScalar(harness.id)}`,
      `Transport: ${humanSafeScalar(transport)}`,
      ...(report.nativeLimits?[`Native limits: ${JSON.stringify(report.nativeLimits)}`]:[]),
      ...(report.nativeOutputLimits?[`Native output limits: ${JSON.stringify(report.nativeOutputLimits)}`]:[]),
      ...(report.nativeConfiguration?[`Native profile: ${humanSafeScalar(config.values.nativeProfile??"default")} (requested; observed tools unavailable in dry run)`,`Native configuration: ${humanSafeScalar(JSON.stringify(report.nativeConfiguration))}`]:[]),
      `Working directory: ${humanSafeScalar(config.cwd)}`,
      `Prompt placement: ${humanSafeScalar(report.prompt.placement ?? "unavailable")} (${humanSafeScalar(report.prompt.strictness)})`,
      `Isolation: ${humanSafeScalar(report.isolation)}`,
      `Auth: ${humanSafeScalar(report.auth.category)} (${humanSafeScalar(report.auth.readiness)})`,
      `Billing owner: ${humanSafeScalar(harness.billingOwner)}`,
      `Image: ${humanSafeScalar(image.manifest.imageVersion)} (${humanSafeScalar(image.aggregateSha256)})`,
      ...(problem === null ? [] : [
        `Blocking error: ${problem.code}`,
        ...humanVersionRepairDetails(problem.toJSON()),
        `Action: ${humanAction(problem)}`,
      ]),
      "",
    ].join("\n"));
  } else dependencies.writeStdout(jsonLine(report));
  return problem?.exitCode ?? 0;
}

function configurationProvenance(config: EffectiveConfiguration): Array<Record<string, unknown>> {
  const source = (kind: string): string => {
    if (kind === "project-config") return "project";
    if (kind === "user-config") return "user";
    return kind;
  };
  return [
    { key: "cwd", source: source(config.cwdSource.kind), location: config.cwdSource.location, redacted: false },
    ...reportedConfigurationKeys(config).map((key) => ({
      key,
      source: source(config.sources[key]?.kind ?? "default"),
      location: config.sources[key]?.location ?? "built-in",
      redacted: key === "authProfile",
    })),
  ];
}

function mockDescriptor(harness: HarnessDescriptor, transport: string): Record<string, unknown> {
  if (harness.id !== "mock") return { id: `${harness.id}/${transport}`, availability: harness.availability };
  return transport === "fake-process" ? fakeProcessDescriptor : deterministicMockDescriptor;
}

function emitAttemptFailure(
  result: Record<string, unknown>,
  error: RunnerFailure,
  mode: OutputMode,
  invocationId: string,
  baseEvents: Array<Record<string, unknown>>,
  dependencies: CliDependencies,
): void {
  if (mode === "human") dependencies.writeStderr(formatHumanError(error.toJSON()));
  else if (mode === "json") dependencies.writeStdout(jsonLine(result));
  else {
    for (const record of baseEvents) dependencies.writeStdout(jsonLine(record));
    dependencies.writeStdout(jsonLine(event(baseEvents.length, "runner.failed", invocationId, dependencies.clock.now(), {
      kind: "runner.failed",
      error: error.toJSON(),
    })));
  }
}

function fakeProcessOptions(dependencies: CliDependencies): FakeProcessOptions | null {
  if (!TEST_SEAMS_ENABLED) return null;
  if (dependencies.fakeProcess !== undefined) {
    return {
      ...dependencies.fakeProcess,
      ...(dependencies.cancellationSignal === undefined ? {} : { cancelSignal: dependencies.cancellationSignal }),
    };
  }
  const executable = dependencies.env.OPENPROSE_CONFORMANCE_FAKE_HARNESS
    ?? dependencies.env.OPENPROSE_TEST_FAKE_HARNESS;
  if (executable === undefined || executable.length === 0) return null;
  const numeric = (name: string): number | undefined => {
    const raw = dependencies.env[name];
    if (raw === undefined || raw.length === 0) return undefined;
    const value = Number(raw);
    return Number.isSafeInteger(value) && value >= 0 ? value : undefined;
  };
  const scenario = dependencies.env.OPENPROSE_CONFORMANCE_FAKE_SCENARIO
    ?? dependencies.env.OPENPROSE_TEST_SCENARIO;
  const delayMs = numeric("OPENPROSE_CONFORMANCE_FAKE_DELAY_MS") ?? numeric("OPENPROSE_TEST_DELAY_MS");
  const cancelAfterMs = numeric("OPENPROSE_CONFORMANCE_CANCEL_AFTER_MS") ?? numeric("OPENPROSE_TEST_CANCEL_AFTER_MS");
  const observationFile = dependencies.env.OPENPROSE_CONFORMANCE_FAKE_OBSERVATION
    ?? dependencies.env.OPENPROSE_TEST_OBSERVATION_FILE;
  const descendantPidFile = dependencies.env.OPENPROSE_CONFORMANCE_DESCENDANT_IDENTITIES
    ?? dependencies.env.OPENPROSE_TEST_DESCENDANT_PID_FILE;
  return {
    executable,
    ...(scenario === undefined ? {} : { scenario }),
    ...(delayMs === undefined ? {} : { delayMs }),
    ...(cancelAfterMs === undefined ? {} : { cancelAfterMs }),
    ...(observationFile === undefined ? {} : { observationFile }),
    ...(descendantPidFile === undefined ? {} : { descendantPidFile }),
    ...(dependencies.cancellationSignal === undefined
      ? {}
      : { cancelSignal: dependencies.cancellationSignal }),
    ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
    wrapperExecutable: process.execPath,
  };
}

function event(sequence: number, type: string, invocationId: string, timestamp: string, payload: Record<string, unknown>): Record<string, unknown> {
  return {
    schema: "openprose.normalized-event/1",
    sequence,
    type,
    invocationId,
    timestamp,
    payload,
  };
}

function selectHarness(config: EffectiveConfiguration): HarnessDescriptor {
  const harness = harnessById(config.values.harness);
  if (harness !== undefined) return harness;
  throw failure("HARNESS_UNAVAILABLE", { harness: config.values.harness, reason: "unknown harness id" });
}

function negotiateTransport(harness: HarnessDescriptor, requested: string): string {
  if (requested === "auto") return harness.transports[0] ?? "unavailable";
  if (harness.transports.includes(requested)) return requested;
  throw failure("TRANSPORT_UNSUPPORTED", {
    harness: harness.id,
    requested,
    supported: harness.transports,
  });
}

function adapterId(harness: HarnessDescriptor, transport: string): string {
  if (harness.id === "mock") return transport === "fake-process" ? "mock/fake-process" : "mock/in-memory";
  return `${harness.id}/${transport}`;
}

function harnessProblem(harness: HarnessDescriptor): RunnerFailure | null {
  if (harness.id === "openprose") {
    return failure("HOSTED_UNAVAILABLE", { fallbackSelected: false, billingOwner: "openprose" });
  }
  if (harness.admissionBlock === "test-seams-disabled") {
    return failure("HARNESS_UNAVAILABLE", {
      harness: harness.id,
      admissionStatus: "blocked",
      admissionBlock: harness.admissionBlock,
      fallbackAttempted: false,
    });
  }
  if (harness.admissionBlock !== undefined) {
    const details = {
      adapterId: `${harness.id}/${harness.transports[0]}`,
      admissionStatus: "blocked",
      fallbackAttempted: false,
    };
    return harness.admissionBlock === "prompt-channel"
      ? failure("PROMPT_CHANNEL_UNSUPPORTED", details)
      : failure("HARNESS_INCOMPATIBLE", details);
  }
  if (harness.runtime === "installed-process") return null;
  if (harness.availability !== "available") {
    return failure("HARNESS_UNAVAILABLE", { harness: harness.id, phase: 1 });
  }
  return null;
}

function mockImageProblem(
  harness: HarnessDescriptor,
  image: VerifiedRuntimeImage,
): RunnerFailure | null {
  if (harness.id !== "mock") return null;
  try {
    sentinelTerminalEnvelope(image);
    return null;
  } catch (caught) {
    return normalizeFailure(caught);
  }
}

async function harnessInventory(
  image: VerifiedRuntimeImage,
  config: EffectiveConfiguration,
  dependencies: CliDependencies,
): Promise<readonly HarnessDescriptor[]> {
  const mock = harnesses.find((item) => item.id === "mock");
  const mockProblem = mock === undefined || mock.availability !== "available" ? null : mockImageProblem(mock, image);
  return Promise.all(harnesses.map(async (item): Promise<HarnessDescriptor> => {
    if (item.id === "mock" && mockProblem !== null) {
      return {
        ...item,
        availability: "blocked",
        detectedVersion: null,
        strictWrapperConformant: false,
        admissionBlock: mockProblem.code === "HARNESS_UNAVAILABLE" ? "release-image" : "terminal-envelope",
      };
    }
    if (item.runtime !== "installed-process") return item;
    const transport = item.transports[0]!;
    const adapterId = `${item.id}/${transport}` as InstalledAdapterId;
    let runtimePrerequisites = item.runtimePrerequisites ?? [];
    let executable: string;
    try {
      assertInstalledAdapterPlatform(adapterId, { platform: dependencies.platform, arch: dependencies.arch });
      executable = await resolveInstalledExecutable({
        adapterId,
        ambient: dependencies.env,
        wrapperExecutable: process.execPath,
        ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
        ...(dependencies.arch === undefined ? {} : { arch: dependencies.arch }),
      });
    } catch (caught) {
      if (caught instanceof RunnerFailure && caught.code === "PROCESS_CLEANUP_FAILED") throw caught;
      if (adapterId === "omp/rpc") {
        runtimePrerequisites = await inspectInstalledAdapterRuntimePrerequisites({
          adapterId,
          cwd: config.cwd,
          ambient: dependencies.env,
          wrapperExecutable: process.execPath,
          ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
        });
      }
      const error = normalizeFailure(caught);
      return {
        ...item,
        availability: error.code === "HARNESS_INCOMPATIBLE" ? "incompatible" : "missing",
        detectedVersion: typeof error.details?.detectedVersion === "string" ? error.details.detectedVersion : null,
        ...(runtimePrerequisites.length === 0 ? {} : { runtimePrerequisites }),
      };
    }
    try {
      runtimePrerequisites = await assertInstalledAdapterRuntimePrerequisites({
        adapterId,
        cwd: config.cwd,
        ambient: dependencies.env,
        wrapperExecutable: process.execPath,
        ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
      });
      const version = await probeInstalledAdapterVersion({
        adapterId,
        executable,
        cwd: config.cwd,
        ambient: dependencies.env,
        wrapperExecutable: process.execPath,
        ...(dependencies.platform === undefined ? {} : { platform: dependencies.platform }),
        ...(dependencies.arch === undefined ? {} : { arch: dependencies.arch }),
      });
      return {
        ...item,
        availability: "available",
        detectedVersion: version,
        ...(runtimePrerequisites.length === 0 ? {} : { runtimePrerequisites }),
      };
    } catch (caught) {
      const error = normalizeFailure(caught);
      if (error.code === "PROCESS_CLEANUP_FAILED") throw error;
      const failurePrerequisites = runtimePrerequisitesFromFailure(error).runtimePrerequisites;
      return {
        ...item,
        availability: error.code === "HARNESS_INCOMPATIBLE" ? "incompatible" : "missing",
        detectedVersion: typeof error.details?.detectedVersion === "string" ? error.details.detectedVersion : null,
        ...((failurePrerequisites ?? runtimePrerequisites).length === 0
          ? {}
          : { runtimePrerequisites: failurePrerequisites ?? runtimePrerequisites }),
      };
    }
  }));
}

function processTransportProblem(transport: string, dependencies: CliDependencies): RunnerFailure | null {
  if (transport !== "fake-process") return null;
  const platform = dependencies.platform ?? process.platform;
  if (platform === "win32") {
    return failure("TRANSPORT_UNSUPPORTED", {
      platform,
      strictProcessMode: "unsupported",
      reason: "Race-free Windows Job Object containment is not implemented in the Bun runner.",
    });
  }
  if (fakeProcessOptions(dependencies) === null) {
    return failure("TRANSPORT_UNSUPPORTED", {
      harness: "mock",
      transport,
      reason: "The fake-process transport is available only to the hermetic conformance fixture.",
    });
  }
  return null;
}

function installedAdapterProbe(
  dependencies: CliDependencies,
  harness: HarnessDescriptor,
  transport: string,
): NonNullable<CliDependencies["installedAdapterProbe"]> | null {
  if (!TEST_SEAMS_ENABLED) return null;
  const adapterId = `${harness.id}/${transport}`;
  try {
    installedAdapterDefinition(adapterId);
  } catch {
    return null;
  }
  if (dependencies.installedAdapterProbe !== undefined) return dependencies.installedAdapterProbe;
  const mode = dependencies.env.OPENPROSE_CONFORMANCE_ADAPTER_MODE;
  const executable = dependencies.env.OPENPROSE_CONFORMANCE_ADAPTER_PROBE;
  const observationPath = dependencies.env.OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION;
  const credentialGroup = dependencies.env.OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP;
  const anyControl = [mode, executable, observationPath, credentialGroup].some((value) => value !== undefined);
  if (!anyControl) return null;
  if (
    mode !== "provider-free-v1"
    || executable === undefined || executable.length === 0
    || observationPath === undefined || observationPath.length === 0
    || credentialGroup === undefined || credentialGroup.length === 0
  ) {
    throw failure("CONFIG_INVALID", { reason: "The internal provider-free adapter seam requires all conformance controls." });
  }
  return {
    executable,
    observationPath,
    credentialGroup,
    fixtureDigestRequired: true,
  };
}

async function verifyProviderFreeProbeControl(
  probe: NonNullable<CliDependencies["installedAdapterProbe"]>,
): Promise<void> {
  if (!isAbsolute(probe.executable) || !isAbsolute(probe.observationPath) || probe.interpreter !== undefined) {
    throw failure("CONFIG_INVALID", { reason: "Provider-free controls require absolute paths and the closed fixture executable." });
  }
  let actual: Uint8Array;
  try {
    actual = await readFile(probe.executable);
  } catch {
    throw failure("CONFIG_INVALID", { reason: "The provider-free probe fixture cannot be read." });
  }
  const expectedDigest = await sha256(providerFreeProbeSource);
  if (await sha256(actual) !== expectedDigest) {
    throw failure("CONFIG_INVALID", { reason: "The provider-free executable is not the frozen shared probe fixture." });
  }
}

function tryInstalledAdapterDefinition(
  harness: HarnessDescriptor,
  transport: string,
): ReturnType<typeof installedAdapterDefinition> | null {
  try {
    return installedAdapterDefinition(`${harness.id}/${transport}`);
  } catch {
    return null;
  }
}

function reportedIsolation(guarantee: string): string {
  if (guarantee === "enforced") return "strict";
  if (guarantee === "advisory") return "partial";
  return "unsupported";
}

function installedCapabilities(
  definition: ReturnType<typeof installedAdapterDefinition>,
  platform: NodeJS.Platform = process.platform,
): Record<string, string> {
  return {
    promptPlacement: definition.recipe.launch.instructionPlacement.manifestPlacementId,
    isolation: definition.recipe.isolation.guarantee,
    streaming: "structured",
    cancellation: platform === "win32" ? "unsupported" : "process-group-best-effort",
    terminal: "structured",
  };
}

function normalizeFailure(caught: unknown): RunnerFailure {
  if (caught instanceof RunnerFailure) return caught;
  return failure("INTERNAL_ERROR", {
    reason: caught instanceof Error ? caught.message : "Unexpected internal runner failure.",
  });
}

function runtimePrerequisitesFromFailure(
  error: RunnerFailure,
): { runtimePrerequisites?: RuntimePrerequisiteObservation[] } {
  const value = error.details?.runtimePrerequisite;
  if (
    typeof value !== "object" || value === null
    || !("runtime" in value) || value.runtime !== "bun"
    || !("versionRange" in value) || value.versionRange !== ">=1.3.14"
    || !("availability" in value)
    || !["available", "missing", "incompatible"].includes(String(value.availability))
    || !("detectedVersion" in value)
    || !(value.detectedVersion === null || typeof value.detectedVersion === "string")
    || !("repairCommand" in value)
    || value.repairCommand !== "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9"
  ) return {};
  return { runtimePrerequisites: [{
    runtime: "bun",
    versionRange: ">=1.3.14",
    availability: value.availability as RuntimePrerequisiteObservation["availability"],
    detectedVersion: value.detectedVersion,
    repairCommand: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
  }] };
}

function emitFailure(
  error: RunnerFailure,
  mode: OutputMode,
  invocationId: string,
  dependencies: CliDependencies,
): void {
  const shape: RunnerErrorShape = error.toJSON();
  if (mode === "human") {
    dependencies.writeStderr(formatHumanError(shape));
    return;
  }
  if (mode === "json") {
    dependencies.writeStdout(jsonLine(shape));
    return;
  }
  dependencies.writeStdout(jsonLine(event(0, "runner.failed", invocationId, dependencies.clock.now(), {
    kind: "runner.failed",
    error: shape,
  })));
}

export function defaultDependencies(): CliDependencies {
  const cancellation = new AbortController();
  const cancel = (signal: "SIGINT" | "SIGTERM") => {
    if (!cancellation.signal.aborted) cancellation.abort(signal);
  };
  // Keep both handlers installed after cancellation. A foreground terminal
  // signal and an npm launcher's relay may deliver the same signal twice; a
  // one-shot handler would expose the second delivery to the default action
  // before original process-group cleanup and terminal normalization finish.
  process.on("SIGINT", () => cancel("SIGINT"));
  process.on("SIGTERM", () => cancel("SIGTERM"));
  return {
    env: process.env,
    processCwd: process.cwd(),
    clock: {
      now: () => new Date().toISOString(),
      monotonicMs: () => performance.now(),
    },
    ids: { invocationId: () => uuidV7() },
    cancellationSignal: cancellation.signal,
    stopHostOutput,
    writeStdoutBytes: (bytes) => writeHostBytes(1, bytes),
    writeStderrBytes: (bytes) => writeHostBytes(2, bytes),
    writeStdout: (text) => process.stdout.write(text),
    writeStderr: (text) => process.stderr.write(text),
  };
}
