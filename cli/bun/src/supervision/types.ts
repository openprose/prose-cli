import type { RunnerFailure } from "../core/types";

export interface TransportLimits {
  maxRecordBytes: number;
  maxAggregateStdoutBytes: number;
  maxAggregateStderrBytes: number;
  maxQueuedRecords: number;
}

export interface DescendantIdentity {
  childPid: number;
  grandchildPid: number;
  processGroupId: number | null;
  attemptedDetachment: boolean;
  runNonce: string | null;
}

export interface RawTransportEvent {
  type: "session.started" | "assistant.message" | "session.completed" | "fixture.descendants";
  harnessVersion?: string;
  text?: string;
  terminalEnvelope?: Record<string, unknown>;
  identities?: DescendantIdentity;
}

export interface ProcessSupervisionRequest {
  executable: string;
  argv: readonly string[];
  cwd: string;
  environment: Readonly<Record<string, string | undefined>>;
  invocationId: string;
  recursionToken: string;
  runNonce: string;
  wrapperExecutable?: string;
  startupTimeoutMs: number;
  runTimeoutMs: number;
  graceMs: number;
  hardKillAfterMs: number;
  cancelSignal?: AbortSignal;
  cancelAfterMs?: number;
  platform?: NodeJS.Platform;
  limits?: TransportLimits;
  stdinBytes?: Uint8Array;
  stdinLifecycle?: "close-after-write" | "close-after-terminal-event";
  additionalEnvironmentNames?: readonly string[];
  protocol?: StructuredProtocolState;
  /**
   * Receives only protocol-validated assistant text. The reader awaits this
   * hook so a human output sink cannot be outrun by the harness stream.
   */
  onNativeRecord?(record: unknown): void | Promise<void>;
  onAcceptedAssistantMessage?(text: string): void | Promise<void>;
}

export type CancellationReason = "caller" | "timeout" | null;

export interface ProcessSupervisionResult {
  executable: string;
  pid: number;
  processGroupId: number | null;
  exitCode: number | null;
  signal: string | null;
  events: RawTransportEvent[];
  terminalEventObserved: boolean;
  terminalEnvelope: Record<string, unknown> | null;
  harnessVersion: string | null;
  stderr: string;
  error: RunnerFailure | null;
  cancellationReason: CancellationReason;
  /** True only when an OS authority accounts for every descendant. */
  cleanupVerified: boolean;
}

export type PrimeParserPhase =
  | "tool-await-agent-start"
  | "tool-await-next-turn"
  | "tool-message-open"
  | "tool-turn-open"
  | "tool-await-agent-end"

  | "await-prompt-ack"
  | "await-agent-start"
  | "await-turn-start"
  | "await-user-message-start"
  | "await-user-message-end"
  | "await-assistant-message-start"
  | "await-thinking-or-text-start"
  | "await-thinking-delta-or-end"
  | "await-text-start"
  | "await-text-delta-or-end"
  | "await-assistant-message-end"
  | "await-turn-end"
  | "await-agent-end"
  | "complete";

export interface AdapterDiagnostic {
  schema: "openprose.adapter-diagnostic/1";
  adapterId: "prime/rpc";
  stage: "jsonl-framing" | "prime-lifecycle";
  phase: "record-boundary" | PrimeParserPhase;
  counters: {
    acceptedRecords: number;
    thinkingDeltas: number;
    textDeltas: number;
    saturated: boolean;
  };
}

export interface StructuredProtocolState {
  readonly sdkObservations?: import("../adapters/sdk-observation").SdkObservations;
  /** Optional final validation after natural process/pipe settlement. */
  settleProcess?(exitCode:number|null):RawTransportEvent|null;
  readonly terminalEventObserved: boolean;
  readonly terminalEnvelope: Record<string, unknown> | null;
  readonly harnessVersion: string | null;
  readonly descendants: readonly DescendantIdentity[];
  readonly descendantPids: readonly number[];
  /** Requests half-close of the child stdin independently of protocol completion. */
  readonly stdinCloseRequested?: boolean;
  /** True only for a protocol that owns bounded writes after accepted records. */
  readonly stagedStdin?: boolean;
  /** Transfers one already-bounded protocol request without exposing its bytes. */
  takeStagedStdinBytes?(): Uint8Array | null;
  /** Returns only the closed, non-payload parser state admitted for diagnostics. */
  diagnostic?(stage: AdapterDiagnostic["stage"]): AdapterDiagnostic | null;
  accept(value: unknown): RawTransportEvent | null;
}

export interface FakeProcessOptions {
  executable: string;
  scenario?: string;
  delayMs?: number;
  observationFile?: string;
  descendantPidFile?: string;
  cancelAfterMs?: number;
  cancelSignal?: AbortSignal;
  startupTimeoutMs?: number;
  platform?: NodeJS.Platform;
  wrapperExecutable?: string;
}

export interface FakeProcessResult extends ProcessSupervisionResult {
  deliveredImageSha256: string;
  observationFile: string | null;
}
