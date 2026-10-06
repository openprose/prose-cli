import { realpath, stat } from "node:fs/promises";
import transportLimits from "../../../shared/capabilities/transport-limits.v1.json" with { type: "json" };
import { failure } from "../core/errors";
import { RunnerFailure } from "../core/types";
import {
  buildChildEnvironment,
  buildVersionProbeEnvironment,
  collectSecretValues,
  redactDiagnostic,
} from "./environment";
import { BoundedJsonLineDecoder, normalizeProtocolFailure } from "./jsonl";
import type {
  DescendantIdentity,
  ProcessSupervisionRequest,
  ProcessSupervisionResult,
  RawTransportEvent,
  StructuredProtocolState,
  TransportLimits,
} from "./types";
import { probeWindowsExecutableVersion, superviseWindowsStructuredProcess } from "./windows-process";

const textDecoder = new TextDecoder("utf-8");
const POST_EXIT_READER_SETTLEMENT_MS = 250;
const POST_FAULT_PROCESS_SETTLEMENT_MS = 250;
const FORCED_READER_SETTLEMENT_MS = 250;

export async function superviseStructuredProcess(request: ProcessSupervisionRequest): Promise<ProcessSupervisionResult> {
  const platform = request.platform ?? process.platform;
  if (request.environment.OPENPROSE_RECURSION_TOKEN !== undefined) {
    throw failure("RECURSIVE_INVOCATION", { reason: "A recursion marker was already present in the wrapper environment." });
  }
  if (signalIsAborted(request.cancelSignal)) {
    throw failure("CANCELLED", { reason: "caller", phase: "before-spawn" });
  }
  const stdinLifecycle = request.stdinLifecycle ?? "close-after-write";
  const deferStdinClose = stdinLifecycle === "close-after-terminal-event";
  const stagedStdin = request.protocol?.stagedStdin === true;
  if (deferStdinClose && request.stdinBytes === undefined) {
    throw failure("CONFIG_INVALID", { reason: "A terminal-close stdin lifecycle requires request bytes." });
  }
  if (platform === "win32") {
    if (stagedStdin) {
      throw failure("TRANSPORT_UNSUPPORTED", { reason: "Staged installed-protocol stdin is not admitted by the Windows process host." });
    }
    if (deferStdinClose) {
      throw failure("TRANSPORT_UNSUPPORTED", { reason: "Terminal-event stdin closure is not admitted by the Windows process host." });
    }
    return superviseWindowsStructuredProcess(
      request,
      undefined,
      (processGroupId) => new FakeProtocolState(request.runNonce, processGroupId),
    );
  }

  const executable = await resolveExecutable(request.executable, request.wrapperExecutable);
  const cwd = await resolveWorkingDirectory(request.cwd);
  const limits = request.limits ?? transportLimits;
  const childEnvironment = buildChildEnvironment(request.environment, {
    invocationId: request.invocationId,
    recursionToken: request.recursionToken,
    runNonce: request.runNonce,
  }, request.additionalEnvironmentNames);
  const secrets = collectSecretValues(request.environment);

  let child: Bun.Subprocess;
  try {
    child = Bun.spawn({
      cmd: [executable, ...request.argv],
      cwd,
      env: childEnvironment,
      stdin: deferStdinClose ? "pipe" : request.stdinBytes ?? "ignore",
      stdout: "pipe",
      stderr: "pipe",
      detached: true,
    });
  } catch (caught) {
    throw failure("HARNESS_UNAVAILABLE", {
      reason: caught instanceof Error ? caught.message : "The harness process could not be started.",
      executable,
    });
  }

  const processGroupId = child.pid;
  const runDeadline = performance.now() + request.runTimeoutMs;
  const events: RawTransportEvent[] = [];
  const protocol: StructuredProtocolState = request.protocol ?? new FakeProtocolState(request.runNonce, processGroupId);
  let streamError: RunnerFailure | null = null;
  let stderrError: RunnerFailure | null = null;
  let stderr = "";
  let readersStopping = false;
  let firstRecordObserved = false;
  let resolveTrigger!: (trigger: Trigger) => void;
  let triggered = false;
  const triggerPromise = new Promise<Trigger>((resolve) => { resolveTrigger = resolve; });
  const trigger = (value: Trigger): void => {
    if (triggered) return;
    triggered = true;
    resolveTrigger(value);
  };
  const stdinWriter = deferStdinClose
    ? startManagedStdin(
      child.stdin as Bun.FileSink,
      stagedStdin ? protocol.takeStagedStdinBytes?.() ?? undefined : request.stdinBytes as Uint8Array,
    )
    : null;
  let stdinError: RunnerFailure | null = null;
  stdinWriter?.settle().catch((caught) => {
    stdinError = caught instanceof RunnerFailure ? caught : failure("HARNESS_FAILED", {
      reason: caught instanceof Error ? caught.message : "Harness stdin could not be written.",
    });
    trigger({ kind: "fault", error: stdinError });
  });

  const stdoutReader = startBoundedJsonLines(child.stdout as ReadableStream<Uint8Array>, limits, async (record) => {
    firstRecordObserved = true;
    await request.onNativeRecord?.(record);
    const parsed = protocol.accept(record);
    if (parsed !== null) {
      events.push(parsed);
      if (parsed.type === "assistant.message") {
        await request.onAcceptedAssistantMessage?.(parsed.text ?? "");
      }
    }
    if (stagedStdin) {
      const bytes = protocol.takeStagedStdinBytes?.();
      if (bytes !== null && bytes !== undefined) await stdinWriter?.write(bytes);
    }
    if (protocol.stdinCloseRequested === true) await stdinWriter?.close();
  });
  const stdoutPromise = stdoutReader.promise.catch((caught) => {
    if (readersStopping) return;
    streamError = withAdapterDiagnostic(
      normalizeProtocolFailure(caught),
      protocol,
      "jsonl-framing",
    );
    trigger({ kind: "fault", error: streamError });
  });
  const stderrReader = startBoundedTextRead(
    child.stderr as ReadableStream<Uint8Array>,
    limits.maxAggregateStderrBytes,
    () => failure("HARNESS_FAILED", { reason: "Harness stderr exceeded the diagnostic output limit." }),
  );
  const stderrPromise = stderrReader.promise
    .then((value) => { stderr = redactDiagnostic(value, secrets); })
    .catch((caught) => {
      if (readersStopping) return;
      stderrError = caught instanceof RunnerFailure ? caught : failure("HARNESS_FAILED", {
        reason: caught instanceof Error ? caught.message : "Harness stderr could not be read.",
      });
      trigger({ kind: "fault", error: stderrError });
    });

  const startupTimer = setTimeout(() => {
    if (!firstRecordObserved) trigger({ kind: "startup-timeout" });
  }, request.startupTimeoutMs);
  const runTimer = setTimeout(() => trigger({ kind: "run-timeout" }), request.runTimeoutMs);
  const testCancelTimer = request.cancelAfterMs === undefined
    ? undefined
    : setTimeout(() => trigger({ kind: "caller-cancel" }), request.cancelAfterMs);
  const abort = (): void => trigger({ kind: "caller-cancel" });
  request.cancelSignal?.addEventListener("abort", abort, { once: true });
  if (signalIsAborted(request.cancelSignal)) trigger({ kind: "caller-cancel" });

  const exitPromise = observeOriginalProcessExit(child, runDeadline)
    .then(() => ({ kind: "exit" as const, exitCode: child.exitCode ?? 0 }));
  const race = await Promise.race([exitPromise, triggerPromise]);
  let cancellationReason: "caller" | "timeout" | null = null;
  let triggerError: RunnerFailure | null = null;
  let originalGroupCleanupObserved = true;
  let readersSettled = false;
  let stdinSettled = stdinWriter === null;
  try {
    let completion: Trigger | { kind: "readers-settled" } | { kind: "reader-settlement-timeout" } = race;
    if (race.kind === "exit") {
      const settlementDeadline = Math.min(
        runDeadline,
        performance.now() + POST_EXIT_READER_SETTLEMENT_MS,
      );
      const settlement = await settleBeforeDeadline(Promise.race([
        Promise.all([stdoutPromise, stderrPromise]).then(() => ({ kind: "readers-settled" as const })),
        triggerPromise,
      ]), settlementDeadline);
      completion = settlement.kind === "timeout"
        ? { kind: "reader-settlement-timeout" }
        : settlement.result.status === "fulfilled"
          ? settlement.result.value
          : { kind: "fault", error: failure("HARNESS_FAILED", { reason: "Harness output settlement could not be observed." }) };
      readersSettled = completion.kind === "readers-settled";
    }

    if (completion.kind !== "readers-settled" && completion.kind !== "exit") {
      if (completion.kind === "caller-cancel") cancellationReason = "caller";
      else if (completion.kind === "run-timeout") {
        if (child.exitCode === null && isPidAlive(child.pid)) cancellationReason = "timeout";
        else triggerError = retainedPipeFailure("run", processGroupId);
      } else if (completion.kind === "startup-timeout") {
        triggerError = child.exitCode === null && isPidAlive(child.pid)
          ? failure("STARTUP_TIMEOUT")
          : retainedPipeFailure("startup", processGroupId);
      } else if (completion.kind === "reader-settlement-timeout") {
        triggerError = retainedPipeFailure("run", processGroupId);
      } else triggerError = completion.error;
    }

    if (readersSettled) {
      if (stdinWriter !== null) {
        const stdinSettlement = await settleBeforeDeadline(
          stdinWriter.close(),
          Math.min(runDeadline, performance.now() + POST_EXIT_READER_SETTLEMENT_MS),
        );
        stdinSettled = stdinSettlement.kind === "settled" && stdinSettlement.result.status === "fulfilled";
        if (!stdinSettled) {
          triggerError = failure("PROCESS_CLEANUP_FAILED", { phase: "stdin-settlement", processGroupId });
          readersSettled = false;
        }
      }
    }

    if (readersSettled) {
      const childSettlement = await settleBeforeDeadline(
        child.exited,
        Math.min(runDeadline, performance.now() + POST_EXIT_READER_SETTLEMENT_MS),
      );
      if (childSettlement.kind === "timeout" || childSettlement.result.status === "rejected") {
        triggerError = retainedPipeFailure("run-status", processGroupId);
        readersSettled = false;
      }
    }

    if (completion.kind === "fault" && child.exitCode === null && child.signalCode === null) {
      // A parser fault can be observed from the stdout pipe one scheduling
      // turn before Bun reaps a harness that is already exiting. Preserve its
      // natural status within a short bound before moving to forced cleanup;
      // otherwise a truthful zero exit can nondeterministically become null.
      await settleBeforeDeadline(
        child.exited,
        Math.min(runDeadline, performance.now() + POST_FAULT_PROCESS_SETTLEMENT_MS),
      );
    }

    if (!readersSettled) {
      readersStopping = true;
      [originalGroupCleanupObserved, readersSettled, stdinSettled] = await Promise.all([
        terminateOwnedProcessGroup(
          child,
          processGroupId,
          request.graceMs,
          request.hardKillAfterMs,
        ),
        cancelAndSettleReaders(
          [stdoutReader, stderrReader],
          FORCED_READER_SETTLEMENT_MS,
        ),
        stdinWriter === null
          ? Promise.resolve(true)
          : closeAndSettleStdin(stdinWriter, FORCED_READER_SETTLEMENT_MS),
      ]);
      const childSettlement = await settleBeforeDeadline(
        child.exited,
        performance.now() + POST_EXIT_READER_SETTLEMENT_MS,
      );
      if (childSettlement.kind === "timeout" || childSettlement.result.status === "rejected") {
        originalGroupCleanupObserved = false;
      }
      if (!readersSettled || !stdinSettled) originalGroupCleanupObserved = false;
    }
  } finally {
    clearTimeout(startupTimer);
    clearTimeout(runTimer);
    if (testCancelTimer !== undefined) clearTimeout(testCancelTimer);
    request.cancelSignal?.removeEventListener("abort", abort);
  }

  const unexpectedDescendants = await hasOwnedDescendants(processGroupId, protocol.descendants);
  if (unexpectedDescendants) {
    await terminateOwnedProcessGroup(child, processGroupId, request.graceMs, request.hardKillAfterMs);
    originalGroupCleanupObserved = false;
  }
  if (!originalGroupCleanupObserved || !readersSettled || !stdinSettled || await hasOwnedDescendants(processGroupId, protocol.descendants)) {
    triggerError = failure("PROCESS_CLEANUP_FAILED", {
      processGroupId,
      runNonce: request.runNonce,
      descendantPids: protocol.descendantPids,
    });
    cancellationReason = null;
  }

  let error = triggerError ?? stdinError ?? streamError ?? stderrError;
  if (error === null && cancellationReason !== null) {
    error = failure("CANCELLED", { reason: cancellationReason });
  }
  if(error===null && protocol.settleProcess){
    try { const settled=protocol.settleProcess(child.exitCode);if(settled)events.push(settled); }
    catch(caught){error=normalizeProtocolFailure(caught);}
  }
  if (error === null && !protocol.terminalEventObserved) {
    error = failure("PROTOCOL_TRUNCATED", {
      reason: "The process reached EOF without the required harness terminal record.",
      transportDiagnostic: {
        schema: "openprose.transport-diagnostic/1",
        reason: "lifecycle-rejection",
      },
      exitCode: child.exitCode,
      ...(protocol.diagnostic?.("prime-lifecycle") === null
        || protocol.diagnostic?.("prime-lifecycle") === undefined
        ? {}
        : { adapterDiagnostic: protocol.diagnostic("prime-lifecycle") }),
    });
  }
  if (error === null && child.exitCode !== 0) {
    error = failure("HARNESS_FAILED", { exitCode: child.exitCode, terminalEventObserved: true });
  }

  return {
    executable,
    pid: child.pid,
    processGroupId,
    exitCode: child.exitCode,
    signal: child.signalCode,
    events,
    terminalEventObserved: protocol.terminalEventObserved,
    terminalEnvelope: protocol.terminalEnvelope,
    harnessVersion: protocol.harnessVersion,
    stderr,
    error,
    cancellationReason,
    // A POSIX process group is only the original signaling boundary. A child
    // can call setsid() and escape it, so true is reserved for an authority
    // such as the native Windows Job Object host that accounts for descendants.
    cleanupVerified: false,
  };
}

function withAdapterDiagnostic(
  error: RunnerFailure,
  protocol: StructuredProtocolState,
  stage: "jsonl-framing" | "prime-lifecycle",
): RunnerFailure {
  if (error.code !== "PROTOCOL_MALFORMED" && error.code !== "PROTOCOL_TRUNCATED") return error;
  if (error.details?.adapterDiagnostic !== undefined) return error;
  const diagnostic = protocol.diagnostic?.(stage);
  return diagnostic === null || diagnostic === undefined
    ? error
    : failure(error.code, { adapterDiagnostic: diagnostic });
}

export async function probeExecutableVersion(
  executableInput: string,
  cwd: string,
  ambient: Readonly<Record<string, string | undefined>>,
  wrapperExecutable: string | undefined,
  timeoutMs: number,
  expected: RegExp,
  platform: NodeJS.Platform = process.platform,
  probeArgv: readonly string[] = ["--version"],
  versionStream: "stdout" | "stderr" = "stdout",
): Promise<string> {
  const probeAmbient = buildVersionProbeEnvironment(ambient, platform);
  if (platform === "win32") {
    if (probeArgv.length !== 1 || probeArgv[0] !== "--version") {
      throw failure("TRANSPORT_UNSUPPORTED", { reason: "The Windows process host currently admits only a --version identity probe." });
    }
    return probeWindowsExecutableVersion(
      executableInput,
      cwd,
      probeAmbient,
      wrapperExecutable,
      timeoutMs,
      expected,
      versionStream,
    );
  }
  const executable = await resolveExecutable(executableInput, wrapperExecutable);
  const canonicalCwd = await resolveWorkingDirectory(cwd);
  const { exitCode, stdout, stderr } = await runBoundedExecutableProbe({
    executable,
    cwd: canonicalCwd,
    environment: buildChildEnvironment(probeAmbient, {
      invocationId: "probe",
      recursionToken: "openprose:probe",
      runNonce: "probe",
    }),
    argv: probeArgv,
    timeoutMs,
    platform,
    phase: "version-probe",
    outputOverflow: () => failure("HARNESS_FAILED", { reason: "Harness stderr exceeded the diagnostic output limit." }),
  });
  const version = (versionStream === "stderr" ? stderr : stdout).trim();
  if (exitCode !== 0 || !expected.test(version)) {
    throw failure("HARNESS_INCOMPATIBLE", {
      probeExitCode: exitCode,
      versionOutputAccepted: false,
    });
  }
  return version;
}

export async function probeExecutableCommand(input: {
  executable: string;
  cwd: string;
  environment: Readonly<Record<string, string>>;
  argv: readonly string[];
  timeoutMs: number;
  platform?: NodeJS.Platform;
  phase: string;
  maxOutputBytes?: number;
}): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const executable = await resolveExecutable(input.executable);
  const cwd = await resolveWorkingDirectory(input.cwd);
  return runBoundedExecutableProbe({
    executable,
    cwd,
    environment: input.environment,
    argv: input.argv,
    timeoutMs: input.timeoutMs,
    platform: input.platform ?? process.platform,
    phase: input.phase,
    ...(input.maxOutputBytes === undefined ? {} : {maxOutputBytes:input.maxOutputBytes}),
    outputOverflow: () => failure("HARNESS_INCOMPATIBLE", { reason: "Harness readiness output exceeded its bound." }),
  });
}

export async function resolveExecutable(executable: string, wrapperExecutable?: string): Promise<string> {
  let resolved: string;
  try {
    resolved = await realpath(executable);
    if (!(await stat(resolved)).isFile()) throw new Error("not a regular file");
  } catch (caught) {
    throw failure("HARNESS_UNAVAILABLE", {
      executable,
      reason: caught instanceof Error ? caught.message : "Executable path cannot be resolved.",
    });
  }
  if (wrapperExecutable !== undefined) {
    let wrapper: string;
    try {
      wrapper = await realpath(wrapperExecutable);
    } catch {
      wrapper = wrapperExecutable;
    }
    if (resolved === wrapper) {
      throw failure("RECURSIVE_INVOCATION", { executable: resolved, wrapperExecutable: wrapper });
    }
  }
  return resolved;
}

async function resolveWorkingDirectory(input: string): Promise<string> {
  try {
    const resolved = await realpath(input);
    if (!(await stat(resolved)).isDirectory()) throw new Error("not a directory");
    return resolved;
  } catch (caught) {
    throw failure("CONFIG_INVALID", {
      reason: caught instanceof Error ? caught.message : "Working directory cannot be resolved.",
      cwd: input,
    });
  }
}

class FakeProtocolState implements StructuredProtocolState {
  private started = false;
  private terminal = false;
  readonly descendants: DescendantIdentity[] = [];
  terminalEnvelope: Record<string, unknown> | null = null;
  harnessVersion: string | null = null;
  readonly runNonce: string;
  readonly processGroupId: number;

  constructor(runNonce: string, processGroupId: number) {
    this.runNonce = runNonce;
    this.processGroupId = processGroupId;
  }

  get descendantPids(): number[] {
    return this.descendants.flatMap((item) => [item.childPid, item.grandchildPid]);
  }

  get terminalEventObserved(): boolean {
    return this.terminal;
  }

  accept(value: unknown): RawTransportEvent {
    const record = asRecord(value);
    if (record.schema !== "openprose.fake-harness-event/1" || record.sessionId !== "fake-session-0001") {
      malformed("Unexpected fake-harness schema or session identity.");
    }
    if (this.terminal) malformed("A record was emitted after session.completed.");
    if (record.type === "session.started") {
      if (!hasExactKeys(record, ["schema", "type", "sessionId", "harnessVersion"])) malformed("session.started carried unknown or missing fields.");
      if (this.started || record.harnessVersion !== "1.0.0") malformed("Duplicate or invalid session.started record.");
      this.started = true;
      this.harnessVersion = record.harnessVersion;
      return { type: "session.started", harnessVersion: record.harnessVersion };
    }
    if (!this.started) malformed("A harness record was emitted before session.started.");
    if (record.type === "assistant.message") {
      if (!hasExactKeys(record, ["schema", "type", "sessionId", "text"])) malformed("assistant.message carried unknown or missing fields.");
      if (typeof record.text !== "string") malformed("assistant.message text is invalid.");
      return { type: "assistant.message", text: record.text };
    }
    if (record.type === "session.completed") {
      if (!hasExactKeys(record, ["schema", "type", "sessionId", "terminalEnvelope"])) malformed("session.completed carried unknown or missing fields.");
      const terminal = asRecord(record.terminalEnvelope);
      if (
        !hasExactKeys(terminal, ["schema", "semanticStatus", "marker"])
        ||
        terminal.schema !== "openprose.sentinel-terminal-envelope/1"
        || terminal.semanticStatus !== "not-applicable"
        || terminal.marker !== "OPENPROSE_SENTINEL_TERMINAL_V1"
      ) malformed("session.completed carried an invalid terminal envelope.");
      this.terminal = true;
      this.terminalEnvelope = terminal;
      return { type: "session.completed", terminalEnvelope: terminal };
    }
    if (record.type === "fixture.descendants") {
      if (!hasExactKeys(record, ["schema", "type", "sessionId", "identities"])) malformed("fixture.descendants carried unknown or missing fields.");
      const identities = validateDescendant(record.identities, this.runNonce, this.processGroupId);
      this.descendants.push(identities);
      return { type: "fixture.descendants", identities };
    }
    malformed("Harness emitted an unsupported structured event type.");
  }
}

type Trigger =
  | { kind: "exit"; exitCode: number }
  | { kind: "startup-timeout" }
  | { kind: "run-timeout" }
  | { kind: "caller-cancel" }
  | { kind: "fault"; error: RunnerFailure };

interface ManagedReader<T> {
  readonly promise: Promise<T>;
  cancel(): Promise<void>;
}

interface ManagedStdin {
  write(bytes: Uint8Array): Promise<void>;
  settle(): Promise<void>;
  close(): Promise<void>;
}

interface BoundedExecutableProbeRequest {
  executable: string;
  cwd: string;
  environment: Readonly<Record<string, string>>;
  argv: readonly string[];
  timeoutMs: number;
  platform: NodeJS.Platform;
  phase: string;
  maxOutputBytes?: number;
  outputOverflow: () => RunnerFailure;
}

function startManagedReader<T>(
  stream: ReadableStream<Uint8Array>,
  consume: (reader: ReadableStreamDefaultReader<Uint8Array>) => Promise<T>,
): ManagedReader<T> {
  const reader = stream.getReader();
  let released = false;
  const promise = consume(reader).finally(() => {
    released = true;
    reader.releaseLock();
  });
  return {
    promise,
    async cancel(): Promise<void> {
      if (released) return;
      try {
        await reader.cancel();
      } catch {
        // Reader settlement below is the authoritative cleanup observation.
      }
    },
  };
}

function startManagedStdin(sink: Bun.FileSink, initialBytes?: Uint8Array): ManagedStdin {
  let writes = Promise.resolve();
  let closePromise: Promise<void> | null = null;
  const managed: ManagedStdin = {
    write(bytes: Uint8Array): Promise<void> {
      if (closePromise !== null) return Promise.reject(failure("HARNESS_FAILED", { reason: "Harness stdin was already closed." }));
      writes = writes.then(async () => {
        sink.write(bytes);
        await sink.flush();
      });
      return writes;
    },
    settle(): Promise<void> {
      return writes;
    },
    close(): Promise<void> {
      if (closePromise !== null) return closePromise;
      closePromise = writes.then(
        () => { sink.end(); },
        (caught: unknown) => {
          try { sink.end(); } catch { /* Settlement below remains authoritative. */ }
          throw caught;
        },
      );
      return closePromise;
    },
  };
  if (initialBytes !== undefined) void managed.write(initialBytes);
  return managed;
}

async function closeAndSettleStdin(stdin: ManagedStdin, timeoutMs: number): Promise<boolean> {
  const settlement = await settleBeforeDeadline(
    Promise.allSettled([stdin.settle(), stdin.close()]),
    performance.now() + timeoutMs,
  );
  return settlement.kind === "settled"
    && settlement.result.status === "fulfilled"
    && settlement.result.value.every((item) => item.status === "fulfilled");
}

function startBoundedJsonLines(
  stream: ReadableStream<Uint8Array>,
  limits: TransportLimits,
  onRecord: (record: unknown) => void | Promise<void>,
): ManagedReader<void> {
  return startManagedReader(stream, async (reader) => {
    const decoder = new BoundedJsonLineDecoder(limits);
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      await decoder.push(value, onRecord);
    }
    decoder.finish();
  });
}

function startBoundedTextRead(
  stream: ReadableStream<Uint8Array>,
  maximum: number,
  outputOverflow: () => RunnerFailure,
): ManagedReader<string> {
  return startManagedReader(stream, async (reader) => readBoundedText(reader, maximum, outputOverflow));
}

async function readBoundedText(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  maximum: number,
  outputOverflow: () => RunnerFailure,
): Promise<string> {
  const chunks: Uint8Array[] = [];
  let aggregate = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    aggregate += value.byteLength;
    if (aggregate > maximum) {
      throw outputOverflow();
    }
    chunks.push(value.slice());
  }
  const bytes = new Uint8Array(aggregate);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return textDecoder.decode(bytes);
}

async function runBoundedExecutableProbe(
  request: BoundedExecutableProbeRequest,
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const deadline = performance.now() + request.timeoutMs;
  let child: Bun.Subprocess<"ignore", "pipe", "pipe">;
  try {
    child = Bun.spawn({
      cmd: [request.executable, ...request.argv],
      cwd: request.cwd,
      env: request.environment,
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe",
      detached: request.platform !== "win32",
    });
  } catch (caught) {
    throw failure("HARNESS_UNAVAILABLE", {
      executable: request.executable,
      reason: caught instanceof Error ? caught.message : "Harness probe could not be started.",
    });
  }

  const stdoutReader = startBoundedTextRead(child.stdout, request.maxOutputBytes ?? 64 * 1024, request.outputOverflow);
  const stderrReader = startBoundedTextRead(child.stderr, request.maxOutputBytes ?? 64 * 1024, request.outputOverflow);
  const readers = [stdoutReader, stderrReader] as const;
  const combined = Promise.all([child.exited, stdoutReader.promise, stderrReader.promise]);
  const lifecycle = Promise.race([
    combined.then(
      (value) => ({ kind: "complete" as const, value }),
      (reason: unknown) => ({ kind: "read-failure" as const, reason }),
    ),
    observeOriginalProcessExit(child, deadline).then(() => ({ kind: "process-exit" as const })),
  ]);
  const outcome = await settleBeforeDeadline(lifecycle, deadline);

  if (outcome.kind === "timeout") {
    // Close our read ends before signaling. Bun can defer `exited` while an
    // escaped descendant owns inherited pipe write ends; settling the readers
    // first lets us distinguish that condition from a genuinely live timeout.
    const readersSettled = await cancelAndSettleReaders(readers, FORCED_READER_SETTLEMENT_MS);
    await settleBeforeDeadline(child.exited, performance.now() + POST_EXIT_READER_SETTLEMENT_MS);
    const exitedBeforeTermination = child.exitCode !== null || !isPidAlive(child.pid);
    const processSettled = await terminateProbeOriginalProcess(child, request.platform);
    if (!readersSettled || !processSettled || exitedBeforeTermination) {
      throw retainedPipeFailure(request.phase, child.pid);
    }
    throw failure("STARTUP_TIMEOUT", { phase: request.phase, executable: request.executable });
  }

  if (outcome.result.status === "rejected") {
    const cleaned = await terminateProbeProcess(child, request.platform, readers);
    if (!cleaned) {
      throw failure("PROCESS_CLEANUP_FAILED", { phase: request.phase, processGroupId: child.pid });
    }
    throw failure("HARNESS_INCOMPATIBLE", {
      executable: request.executable,
      reason: "Harness probe lifecycle could not be observed.",
    });
  }

  const lifecycleOutcome = outcome.result.value;
  if (lifecycleOutcome.kind === "read-failure") {
    const cleaned = await terminateProbeProcess(child, request.platform, readers);
    if (!cleaned) {
      throw failure("PROCESS_CLEANUP_FAILED", { phase: request.phase, processGroupId: child.pid });
    }
    const caught = lifecycleOutcome.reason;
    if (caught instanceof RunnerFailure) throw caught;
    throw failure("HARNESS_INCOMPATIBLE", {
      executable: request.executable,
      reason: caught instanceof Error ? caught.message : "Harness probe output could not be read.",
    });
  }

  let value: [number, string, string];
  if (lifecycleOutcome.kind === "process-exit") {
    const readerDeadline = Math.min(deadline, performance.now() + POST_EXIT_READER_SETTLEMENT_MS);
    const readerOutcome = await settleBeforeDeadline(combined, readerDeadline);
    if (readerOutcome.kind === "timeout") {
      await terminateProbeProcess(child, request.platform, readers);
      throw retainedPipeFailure(request.phase, child.pid);
    }
    if (readerOutcome.result.status === "rejected") {
      const cleaned = await terminateProbeProcess(child, request.platform, readers);
      if (!cleaned) {
        throw failure("PROCESS_CLEANUP_FAILED", { phase: request.phase, processGroupId: child.pid });
      }
      const caught = readerOutcome.result.reason;
      if (caught instanceof RunnerFailure) throw caught;
      throw failure("HARNESS_INCOMPATIBLE", {
        executable: request.executable,
        reason: caught instanceof Error ? caught.message : "Harness probe output could not be read.",
      });
    }
    value = readerOutcome.result.value;
  } else {
    value = lifecycleOutcome.value;
  }

  const [exitCode, stdout, stderr] = value;
  // `child.exited` can resolve a scheduling turn before the kernel stops
  // reporting the now-empty process group. Give that successful-reap edge a
  // short bounded settlement window; a genuinely retained group still fails
  // closed and is terminated below.
  const retainedGroup = request.platform !== "win32"
    && !(await waitForGroupExit(child.pid, POST_EXIT_READER_SETTLEMENT_MS));
  if (retainedGroup) {
    await terminateProbeProcess(child, request.platform, readers);
    throw retainedPipeFailure(request.phase, child.pid);
  }
  return { exitCode, stdout, stderr };
}

async function terminateProbeProcess(
  child: Bun.Subprocess,
  platform: NodeJS.Platform,
  readers: readonly ManagedReader<unknown>[],
): Promise<boolean> {
  const [processSettled, readersSettled] = await Promise.all([
    terminateProbeOriginalProcess(child, platform),
    cancelAndSettleReaders(readers, FORCED_READER_SETTLEMENT_MS),
  ]);
  return processSettled && readersSettled;
}

async function terminateProbeOriginalProcess(
  child: Bun.Subprocess,
  platform: NodeJS.Platform,
): Promise<boolean> {
  return platform === "win32"
    ? terminateSingleProcess(child)
    : terminateOwnedProcessGroup(child, child.pid, 50, 1_000);
}

async function terminateSingleProcess(child: Bun.Subprocess): Promise<boolean> {
  try { child.kill("SIGKILL"); } catch { /* The probe may already have exited. */ }
  const outcome = await settleBeforeDeadline(child.exited, performance.now() + 1_000);
  return outcome.kind === "settled";
}

async function cancelAndSettleReaders(
  readers: readonly ManagedReader<unknown>[],
  timeoutMs: number,
): Promise<boolean> {
  const cancellation = readers.map(async (reader) => reader.cancel());
  const settlement = Promise.allSettled([
    ...cancellation,
    ...readers.map((reader) => reader.promise),
  ]);
  return (await settleBeforeDeadline(settlement, performance.now() + timeoutMs)).kind === "settled";
}

async function settleBeforeDeadline<T>(
  promise: Promise<T>,
  deadline: number,
): Promise<{ kind: "settled"; result: PromiseSettledResult<T> } | { kind: "timeout" }> {
  const remaining = deadline - performance.now();
  if (remaining <= 0) return { kind: "timeout" };
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve({ kind: "timeout" }), Math.ceil(remaining));
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve({ kind: "settled", result: { status: "fulfilled", value } });
      },
      (reason: unknown) => {
        clearTimeout(timer);
        resolve({ kind: "settled", result: { status: "rejected", reason } });
      },
    );
  });
}

function retainedPipeFailure(phase: string, processGroupId: number): RunnerFailure {
  return failure("PROCESS_CLEANUP_FAILED", {
    phase,
    processGroupId,
    reason: "A harness descendant retained an output pipe after the original process exited.",
  });
}

function observeOriginalProcessExit(child: Bun.Subprocess, deadline: number): Promise<void> {
  return new Promise((resolve) => {
    const poll = (): void => {
      if (child.exitCode !== null || !isPidAlive(child.pid)) {
        resolve();
        return;
      }
      if (performance.now() < deadline) setTimeout(poll, 5);
    };
    poll();
  });
}

async function terminateOwnedProcessGroup(
  child: Bun.Subprocess,
  processGroupId: number,
  graceMs: number,
  hardKillAfterMs: number,
): Promise<boolean> {
  signalGroup(processGroupId, "SIGTERM", child);
  if (await waitForGroupExit(processGroupId, graceMs)) return true;
  signalGroup(processGroupId, "SIGKILL", child);
  return waitForGroupExit(processGroupId, hardKillAfterMs);
}

function signalGroup(processGroupId: number, signal: NodeJS.Signals, child: Bun.Subprocess): void {
  try {
    process.kill(-processGroupId, signal);
  } catch (caught) {
    const code = (caught as NodeJS.ErrnoException).code;
    if (code === "ESRCH") return;
    try { child.kill(signal); } catch { /* audited below */ }
  }
}

async function waitForGroupExit(processGroupId: number, timeoutMs: number): Promise<boolean> {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() <= deadline) {
    if (!isProcessGroupAlive(processGroupId)) return true;
    await Bun.sleep(10);
  }
  return !isProcessGroupAlive(processGroupId);
}

function isProcessGroupAlive(processGroupId: number): boolean {
  try {
    process.kill(-processGroupId, 0);
    return true;
  } catch (caught) {
    return (caught as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

async function hasOwnedDescendants(processGroupId: number, identities: readonly DescendantIdentity[]): Promise<boolean> {
  if (isProcessGroupAlive(processGroupId)) return true;
  return identities.some((identity) => isPidAlive(identity.childPid) || isPidAlive(identity.grandchildPid));
}

function isPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (caught) {
    return (caught as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

function validateDescendant(value: unknown, runNonce: string, processGroupId: number): DescendantIdentity {
  const record = asRecord(value);
  if (
    !hasExactKeys(record, ["childPid", "grandchildPid", "processGroupId", "attemptedDetachment", "runNonce"])
    || !positiveInteger(record.childPid)
    || !positiveInteger(record.grandchildPid)
    || record.childPid === record.grandchildPid
    || record.childPid === processGroupId
    || record.grandchildPid === processGroupId
    || record.processGroupId !== processGroupId
    || record.attemptedDetachment !== false
    || record.runNonce !== runNonce
  ) malformed("Descendant audit identities did not match the owned run nonce and containment record.");
  return {
    childPid: record.childPid,
    grandchildPid: record.grandchildPid,
    processGroupId: record.processGroupId,
    attemptedDetachment: false,
    runNonce,
  };
}

function hasExactKeys(record: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(record).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) malformed("Expected a structured object record.");
  return value as Record<string, unknown>;
}

function malformed(reason: string): never {
  throw failure("PROTOCOL_MALFORMED", { reason });
}

function signalIsAborted(signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true;
}
