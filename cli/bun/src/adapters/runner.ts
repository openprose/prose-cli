import transportLimits from "../../../shared/capabilities/transport-limits.v1.json" with {type:"json"};
import {nativeOutputBytes} from "./output-budget";
import { nativeConfiguration, observeNativeInit, type NativeObservation } from "./native-profile";
import { NativeCapture } from "./native-capture";
import { failure } from "../core/errors";
import {
  createOmpControlOverlay,
  createPrivateTransportDirectory,
  createPrivateTransportFiles,
  encodeRuntimeImage,
  PrivateTransportCleanupError,
} from "../supervision/files";
import { redactDiagnostic } from "../supervision/environment";
import { superviseStructuredProcess } from "../supervision/process";
import { buildInstalledAdapterEnvironment, installedAdapterProtectedValues } from "./environment";
import { buildInstalledLaunchPlan } from "./plan";
import { installedProtocol } from "./protocols";
import {
  preparePrimeRecovery,
  primeRecoveryFailure,
  settlePrimeOwnedService,
} from "./prime-owned-service";
import { tmpdir } from "node:os";
import { installedAdapterDefinition } from "./recipes";
import type {
  InstalledAdapterOptions,
  InstalledAdapterResult,
  ProviderFreeAdapterOptions,
  ProviderFreeAdapterResult,
} from "./types";
import type { RawTransportEvent } from "../supervision/types";
import { TEST_SEAMS_ENABLED } from "../core/build";

// Compile-time only (scripts/image-bundle.ts). The inline guards below let a
// release build fold the conformance failure seams, and their names, away.
declare const OPENPROSE_TEST_SEAMS: boolean | undefined;

export async function runInstalledAdapter(options: InstalledAdapterOptions): Promise<InstalledAdapterResult> {
  if (options.ambient.OPENPROSE_RECURSION_TOKEN !== undefined) {
    throw failure("RECURSIVE_INVOCATION", { reason: "A recursion marker was already present in the wrapper environment." });
  }
  const definition = installedAdapterDefinition(options.adapterId);
  const creationFailure = (typeof OPENPROSE_TEST_SEAMS === "boolean" && !OPENPROSE_TEST_SEAMS)
    ? undefined
    : TEST_SEAMS_ENABLED && options.observationPath !== undefined
      ? options.ambient.OPENPROSE_CONFORMANCE_ADAPTER_CREATION_FAILURE
      : undefined;
  const afterTransportDirectoryCreated = TEST_SEAMS_ENABLED
    ? options.testHooks?.afterTransportDirectoryCreated
    : undefined;
  const files = await createPrivateTransportFiles(
    options.image,
    options.invocation,
    options.temporaryRoot,
    options.adapterId === "prime/rpc" ? "openprose-prime-" : "openprose-transport-",
    creationFailure === undefined && afterTransportDirectoryCreated === undefined
      ? undefined
      : {
          afterDirectoryCreated: async (directory) => {
            await afterTransportDirectoryCreated?.(directory);
            if (creationFailure !== undefined) {
              throw new Error("injected private transport creation failure");
            }
          },
          ...(creationFailure === "cleanup-failure"
            ? {
                beforeCleanup: () => {
                  throw new Error("injected private transport cleanup failure");
                },
              }
            : {}),
        },
  ).catch((caught) => {
    if (caught instanceof PrivateTransportCleanupError) {
      throw privateTransportCleanupFailure(options.adapterId);
    }
    throw privateTransportCreationFailure(options.adapterId);
  });
  const injectPrivateCleanupFailure = (typeof OPENPROSE_TEST_SEAMS === "boolean" && !OPENPROSE_TEST_SEAMS)
    ? false
    : options.observationPath !== undefined
      && options.ambient.OPENPROSE_CONFORMANCE_ADAPTER_CLEANUP_FAILURE === "1";
  try {
    let renderedConfigPath: string | undefined;
    try {
      renderedConfigPath = options.adapterId === "omp/rpc"
        ? await createOmpControlOverlay(files)
        : undefined;
    } catch {
      throw privateTransportCreationFailure(options.adapterId);
    }
    const plan = await buildInstalledLaunchPlan({
      adapterId: options.adapterId,
      executable: options.executable,
      invocation: options.invocation,
      imageBytes: encodeRuntimeImage(options.image),
      expectedImageByteLength: options.image.manifest.modelVisibleBytes.byteLength,
      expectedImageSha256: options.image.manifest.modelVisibleBytes.sha256,
      framingTemplateBytes: requiredImageArtifact(options.image, options.image.manifest.oneFieldFraming.path),
      imagePath: files.imagePath,
      taskPath: files.taskPath,
      daemonSocketPath: files.daemonSocketPath,
      ...(renderedConfigPath === undefined ? {} : { renderedConfigPath }),
      credentialGroup: options.credentialGroup,
      ...(options.nativeMaxTurns===undefined?{}:{nativeMaxTurns:options.nativeMaxTurns}),
      ...(options.nativeTimeout===undefined?{}:{nativeTimeout:options.nativeTimeout}),
      ...(options.nativeToolTimeout===undefined?{}:{nativeToolTimeout:options.nativeToolTimeout}),
      ...(options.nativeProfile === undefined ? {} : {nativeProfile:options.nativeProfile}),
      ...(options.nativeAddDirs === undefined ? {} : {nativeAddDirs:options.nativeAddDirs}),
      ...(options.nativeAllowTools === undefined ? {} : {nativeAllowTools:options.nativeAllowTools}),
      ...(options.model === undefined ? {} : { model: options.model }),
      ...(options.permissionMode === undefined ? {} : { permissionMode: options.permissionMode }),
      ...(options.platform === undefined ? {} : { platform: options.platform }),
      ...(options.arch === undefined ? {} : { arch: options.arch }),
    });
    if (plan.argv[0] !== options.executable) {
      throw failure("INTERNAL_ERROR", { adapterId: options.adapterId, reason: "The launch recipe changed executable identity." });
    }
    const controls = options.observationPath === undefined ? undefined : {
      OPENPROSE_ADAPTER_OBSERVATION_PATH: options.observationPath,
      OPENPROSE_ADAPTER_EXPECTED_ID: options.adapterId,
    };
    let credentialConfigDirectory: string | undefined;
    try {
      credentialConfigDirectory = (
        (options.adapterId === "claude/print-stream-json" && options.nativeProfile === "claude-workspace-tools" && options.credentialGroup === "anthropic-api-key")
        || (options.adapterId === "prime/rpc" && options.credentialGroup !== "prime-harness-login")
        || (options.adapterId === "omp/rpc" && options.credentialGroup !== "omp-harness-login")
      )
        ? await createPrivateTransportDirectory(files, "credential-config")
        : undefined;
    } catch {
      throw privateTransportCreationFailure(options.adapterId);
    }
    const environment = buildInstalledAdapterEnvironment({
      definition,
      ambient: options.ambient,
      credentialGroup: options.credentialGroup,
      ...(controls === undefined ? {} : { controls }),
      ...(credentialConfigDirectory === undefined ? {} : { credentialConfigDirectory }),
      ...(options.platform === undefined ? {} : { platform: options.platform }),
    });
    const runNonce = `adapter-probe:${options.invocation.invocationId}`;
    const protectedValues = [...new Set([
      ...installedAdapterProtectedValues({
        definition,
        credentialGroup: options.credentialGroup,
        environment,
        controlNames: Object.keys(controls ?? {}),
        ...(options.platform === undefined ? {} : { platform: options.platform }),
      }),
      options.invocation.recursionToken,
      runNonce,
    ])]
      .filter((value) => value.length > 0)
      .sort((left, right) => right.length - left.length);
    const assistantMessageSink = options.createAssistantMessageSink?.(protectedValues);
    const supervisedExecutable = options.fixtureInterpreter ?? options.executable;
    const supervisedArgv = options.fixtureInterpreter === undefined ? plan.argv.slice(1) : plan.argv;
    const capture = new NativeCapture(options.nativeLog,protectedValues,nativeOutputBytes(options));
    let observed: NativeObservation | null = null;
    const protocol=installedProtocol(options.adapterId, options.harnessVersion ?? null, options.invocation.invocationId, (options.adapterId === "omp/rpc" || options.adapterId === "prime/rpc") ? plan.stdinBytes : null, options.outputContract === "native");
    if(options.nativeProfile === "claude-workspace-tools") {
      const accept=protocol.accept.bind(protocol);
      protocol.accept=(record)=>{ observed=observeNativeInit(record,options.credentialGroup) ?? observed; return accept(record); };
    }
    let process;
    try { process = await superviseStructuredProcess({
      executable: supervisedExecutable,
      argv: supervisedArgv,
      cwd: options.invocation.cwd,
      environment,
      additionalEnvironmentNames: Object.keys(environment),
      invocationId: options.invocation.invocationId,
      recursionToken: options.invocation.recursionToken,
      runNonce,
      ...(plan.stdinBytes === null ? {} : { stdinBytes: plan.stdinBytes }),
      stdinLifecycle: options.adapterId === "prime/rpc" && options.outputContract === "native" ? "close-after-terminal-event" : plan.stdinLifecycle,
      ...(options.wrapperExecutable === undefined ? {} : { wrapperExecutable: options.wrapperExecutable }),
      startupTimeoutMs: Math.min(30_000, options.timeoutMs),
      runTimeoutMs: options.timeoutMs,
      graceMs: definition.recipe.cancellation.graceMs,
      hardKillAfterMs: definition.recipe.cancellation.hardKillAfterMs,
      onNativeRecord:record=>capture.write(record),
      ...(options.outputContract === "native" ? {limits:{...transportLimits,maxAggregateStdoutBytes:nativeOutputBytes(options)}} : {}),
      protocol,
      ...(assistantMessageSink === undefined
        ? {}
        : { onAcceptedAssistantMessage: assistantMessageSink }),
      ...(options.cancelSignal === undefined ? {} : { cancelSignal: options.cancelSignal }),
      ...(options.platform === undefined ? {} : { platform: options.platform }),
    }); } finally {capture.close();}
    return {
      plan,
      process,
      publicEvents: redactInstalledAdapterEvents(process.events, protectedValues),
      publicStderr: redactDiagnostic(process.stderr, protectedValues),
      nativeConfiguration:nativeConfiguration({...options,authProfile:options.credentialGroup},observed),
    };
  } finally {
    if (options.adapterId === "prime/rpc") {
      try {
        await settlePrimeOwnedService({
          directory: files.directory,
          socketPath: files.daemonSocketPath,
          detectedVersion: options.harnessVersion ?? null,
        });
      } catch (caught) {
        let recovery = null;
        try {
          recovery = await preparePrimeRecovery({
            temporaryRoot: options.temporaryRoot ?? tmpdir(),
            directory: files.directory,
            socketPath: files.daemonSocketPath,
            detectedVersion: options.harnessVersion ?? null,
          });
        } catch {
          // The ordinary guard remains the safe fallback when no recovery
          // authority can be authenticated and persisted.
        }
        if (recovery !== null) throw primeRecoveryFailure(caught, recovery);
        await finalizePrivateTransportFiles(
          files,
          options.adapterId,
          injectPrivateCleanupFailure,
        );
        throw caught;
      }
    }
    await finalizePrivateTransportFiles(
      files,
      options.adapterId,
      injectPrivateCleanupFailure,
    );
  }
}

export async function runProviderFreeInstalledAdapter(
  options: ProviderFreeAdapterOptions,
): Promise<ProviderFreeAdapterResult> {
  return runInstalledAdapter(options);
}

async function finalizePrivateTransportFiles(
  files: Pick<Awaited<ReturnType<typeof createPrivateTransportFiles>>, "cleanup">,
  adapterId: InstalledAdapterOptions["adapterId"],
  injectFailure: boolean,
): Promise<void> {
  try {
    if (injectFailure) throw new Error("injected private transport cleanup failure");
    await files.cleanup();
  } catch {
    throw privateTransportCleanupFailure(adapterId);
  }
}

function privateTransportCleanupFailure(
  adapterId: InstalledAdapterOptions["adapterId"],
) {
  return failure("PROCESS_CLEANUP_FAILED", {
    phase: "private-file-finalization",
    resource: "owned-private-transport-files",
    adapterId,
    fallbackAttempted: false,
  });
}

function privateTransportCreationFailure(
  adapterId: InstalledAdapterOptions["adapterId"],
) {
  return failure("INTERNAL_ERROR", {
    adapterId,
    reason: "Cannot create private installed-adapter transport files.",
  });
}

function requiredImageArtifact(image: ProviderFreeAdapterOptions["image"], path: string): Uint8Array {
  const bytes = image.files.get(path);
  if (bytes === undefined) throw failure("IMAGE_INVALID", { reason: `Missing verified image artifact: ${path}.` });
  return bytes;
}

function redactInstalledAdapterEvents(
  events: readonly RawTransportEvent[],
  protectedValues: readonly string[],
): RawTransportEvent[] {
  const assistant = events.flatMap((event, eventIndex) =>
    event.type === "assistant.message"
      ? [{ eventIndex, text: event.text ?? "" }]
      : []);
  const combined = assistant.map((item) => item.text).join("");
  const globalSpans: Array<[number, number]> = [];
  const offsets: number[] = [];
  let offset = 0;
  for (const item of assistant) {
    offsets.push(offset);
    offset += item.text.length;
  }
  for (const value of protectedValues) {
    if (value.length === 0) continue;
    let start = 0;
    while (start <= combined.length - value.length) {
      const match = combined.indexOf(value, start);
      if (match < 0) break;
      globalSpans.push([match, match + value.length]);
      start = match + 1;
    }
  }
  const mergedGlobalSpans = mergeRanges(globalSpans);
  const spans: Array<Array<[number, number]>> = assistant.map(() => []);
  let firstCandidate = 0;
  for (let index = 0; index < assistant.length; index += 1) {
    const eventStart = offsets[index]!;
    const eventEnd = eventStart + assistant[index]!.text.length;
    while (firstCandidate < mergedGlobalSpans.length && mergedGlobalSpans[firstCandidate]![1] <= eventStart) {
      firstCandidate += 1;
    }
    for (let spanIndex = firstCandidate; spanIndex < mergedGlobalSpans.length; spanIndex += 1) {
      const [start, end] = mergedGlobalSpans[spanIndex]!;
      if (start >= eventEnd) break;
      spans[index]!.push([
        Math.max(start, eventStart) - eventStart,
        Math.min(end, eventEnd) - eventStart,
      ]);
    }
  }
  const publicText = new Map<number, string>();
  for (let index = 0; index < assistant.length; index += 1) {
    const text = redactRanges(assistant[index]!.text, spans[index]!);
    publicText.set(assistant[index]!.eventIndex, redactDiagnostic(text, protectedValues));
  }
  return events.map((event, eventIndex) => event.type === "assistant.message"
    ? { ...event, text: publicText.get(eventIndex) ?? "" }
    : { ...event });
}

function redactRanges(text: string, ranges: readonly [number, number][]): string {
  if (ranges.length === 0) return text;
  const merged = mergeRanges(ranges);
  let result = "";
  let cursor = 0;
  for (const [start, end] of merged) {
    result += `${text.slice(cursor, start)}[REDACTED]`;
    cursor = end;
  }
  return result + text.slice(cursor);
}

function mergeRanges(ranges: readonly [number, number][]): Array<[number, number]> {
  const ordered = [...ranges].sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  const merged: Array<[number, number]> = [];
  for (const range of ordered) {
    const previous = merged.at(-1);
    if (previous === undefined || range[0] > previous[1]) merged.push([...range]);
    else previous[1] = Math.max(previous[1], range[1]);
  }
  return merged;
}
