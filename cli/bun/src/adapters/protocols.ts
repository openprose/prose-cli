import {PrimeDrain} from "./prime-drain";
import {sdkNativeFailure,sdkSetupFailureAction} from "./sdk-limits";
import {sdkObservations,type SdkObservations} from "./sdk-observation";
import {hasFreshClaudeResult} from "./claude-shutdown";
import { isDeepStrictEqual } from "node:util";
import { NativeToolLifecycle, hasNativeTools, ompTaskDefaults } from "./native-tool-lifecycle";
import { failure } from "../core/errors";
import { RunnerFailure } from "../core/types";
import type {
  AdapterDiagnostic,
  DescendantIdentity,
  PrimeParserPhase,
  RawTransportEvent,
  StructuredProtocolState,
} from "../supervision/types";
import type { InstalledAdapterId } from "./types";

abstract class InstalledProtocol implements StructuredProtocolState {
  terminalEventObserved = false;
  terminalEnvelope: Record<string, unknown> | null = null;
  readonly harnessVersion: string | null;
  readonly descendants: readonly DescendantIdentity[] = [];
  readonly descendantPids: readonly number[] = [];
  protected started = false;

  get stdinCloseRequested(): boolean {
    return this.terminalEventObserved;
  }

  constructor(harnessVersion: string | null) {
    this.harnessVersion = harnessVersion;
  }

  abstract accept(value: unknown): RawTransportEvent | null;

  protected record(value: unknown): Record<string, unknown> {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      malformed("Harness emitted a structured value that is not an object.");
    }
    if (this.terminalEventObserved) malformed("Harness emitted a record after its terminal event.");
    return value as Record<string, unknown>;
  }

  protected start(): RawTransportEvent {
    if (this.started) malformed("Harness emitted a duplicate start event.");
    this.started = true;
    return { type: "session.started", ...(this.harnessVersion === null ? {} : { harnessVersion: this.harnessVersion }) };
  }

  protected message(text: unknown): RawTransportEvent {
    if (!this.started || typeof text !== "string") malformed("Harness emitted an invalid assistant message.");
    return { type: "assistant.message", text };
  }

  protected complete(): RawTransportEvent {
    if (!this.started) malformed("Harness emitted a terminal event before its start event.");
    this.terminalEventObserved = true;
    return { type: "session.completed" };
  }
}

class PrimeNativeProtocol extends InstalledProtocol {
  readonly stagedStdin=true;
  private phase:"state"|"ack"|"running"="state";
  private pending:Uint8Array|null;
  private lifecycle:NativeToolLifecycle|null=null;
  constructor(version:string|null,private readonly id:string,private readonly prompt:Uint8Array){
    super(version);this.pending=new TextEncoder().encode(JSON.stringify({id:`${id}.prime.state.1`,type:"get_state"})+"\n");
  }
  override get stdinCloseRequested(){return this.phase==="running";}
  takeStagedStdinBytes(){const p=this.pending;this.pending=null;return p;}
  accept(value:unknown):RawTransportEvent|null {
    const r=asRecord(value,"Prime native record is invalid.");
    if(!primeBoundedJson(r))malformed("Prime native record is over its bounded contract.");
    if(this.phase==="state"){
      if(r.type!=="response"||r.id!==`${this.id}.prime.state.1`||r.command!=="get_state"||r.success!==true||!hasExactKeys(r,["id","type","command","success","data"])||!r.data||typeof r.data!=="object"||Array.isArray(r.data))malformed("Prime native identity response is invalid.");
      const d=r.data as Record<string,unknown>;
      if(typeof d.sessionId!=="string"||!d.sessionId||d.isStreaming!==false||d.messageCount!==0)malformed("Prime native invocation requires a fresh identified session.");
      this.lifecycle=new NativeToolLifecycle(false,new PrimeDrain(d.sessionId));this.phase="ack";this.pending=this.prompt;return null;
    }
    if(this.phase==="ack"){
      if(r.type!=="response"||r.id!==this.id||r.command!=="prompt"||r.success!==true||!hasExactKeys(r,["id","type","command","success"]))malformed("Prime native prompt acknowledgement is invalid.");
      this.phase="running";return null;
    }
    return this.lifecycle!.accept(r);
  }
  settleProcess(exitCode:number|null):RawTransportEvent|null {
    if(this.phase!=="running"||!this.lifecycle)throw failure("PROTOCOL_TRUNCATED",{reason:"Prime native identity/prompt exchange is incomplete."});
    const result=this.lifecycle.settlePrime(exitCode);this.terminalEventObserved=this.lifecycle.ended;return result;
  }
}

class CodexProtocol extends InstalledProtocol {
  private turnStarted = false;

  accept(value: unknown): RawTransportEvent | null {
    const record = this.record(value);
    if (record.type === "thread.started") {
      if (typeof record.thread_id !== "string" || record.thread_id.length === 0) malformed("Codex thread.started is invalid.");
      return this.start();
    }
    if (record.type === "turn.started") {
      if (!this.started || this.turnStarted) malformed("Codex turn.started is out of order.");
      this.turnStarted = true;
      return null;
    }
    if (record.type === "item.started" || record.type === "item.updated") {
      if (!this.turnStarted) malformed("Codex item event arrived before turn.started.");
      return null;
    }
    if (record.type === "item.completed") {
      if (!this.turnStarted) malformed("Codex item.completed arrived before turn.started.");
      const item = asRecord(record.item, "Codex item.completed item is invalid.");
      if (item.type === "agent_message") return this.message(item.text);
      return null;
    }
    if (record.type === "turn.completed") {
      if (!this.turnStarted) malformed("Codex turn.completed arrived before turn.started.");
      return this.complete();
    }
    if (record.type === "turn.failed" || record.type === "error") {
      throw failure("HARNESS_FAILED", { reason: `Codex emitted ${String(record.type)}.` });
    }
    malformed(`Codex emitted an unsupported event type: ${String(record.type)}.`);
  }
}

class AgentsSdkProtocol extends InstalledProtocol {
  sdkObservations:SdkObservations={};
  constructor(version:string|null,private readonly requestedModel:string|null){super(version);}
  accept(value: unknown): RawTransportEvent | null {
    const record = this.record(value);
    if (record.type === "start") {
      if (typeof record.model !== "string" || typeof record.cwd !== "string") malformed("SDK start identity missing.");
      const started=this.start();
      this.sdkObservations={...this.sdkObservations,...sdkObservations(record,this.requestedModel)};
      return started;
    }
    if (!this.started) malformed("SDK event before start.");
    if (record.type === "error") {
      this.sdkObservations={...this.sdkObservations,...sdkObservations(record,this.requestedModel)};
      const nativeFailure=sdkNativeFailure(record),problem=failure("HARNESS_FAILED",{nativeFailure,...this.sdkObservations});
      const action=sdkSetupFailureAction(nativeFailure);
      if(action===undefined)throw problem;
      const {schema:_schema,...shape}=problem.toJSON();
      throw new RunnerFailure({...shape,action});
    }
    if (record.type === "tool_call" || record.type === "tool_result") {
      if (typeof record.name !== "string") malformed("SDK tool identity missing.");
      this.sdkObservations={...this.sdkObservations,...sdkObservations(record,this.requestedModel)};
      return null;
    }
    if (record.type === "final") {
      if (typeof record.output !== "string") malformed("SDK final output must be text.");
      this.sdkObservations={...this.sdkObservations,...sdkObservations(record,this.requestedModel)};
      this.complete();
      return this.message(record.output);
    }
    malformed("Unsupported SDK event.");
  }
}

class ClaudeProtocol extends InstalledProtocol {
  private nativeRecords:Record<string,any>[]=[];
  constructor(version:string|null,private readonly nativeMode=false){super(version);}
  settleProcess(exitCode:number|null):RawTransportEvent|null {
    if(!this.nativeMode)return null;
    if(exitCode!==0)throw failure("HARNESS_FAILED",{reason:"Claude process did not exit successfully."});
    if(!hasFreshClaudeResult(this.nativeRecords))throw failure("PROTOCOL_TRUNCATED",{reason:"Claude has no fresh native result at process settlement."});
    return this.complete();
  }
  private sessionId: string | null = null;
  private initialRecord: Record<string, unknown> | null = null;
  private afterTaskNotification = false;

  accept(value: unknown): RawTransportEvent | null {
    const record = this.record(value);
    if(this.nativeMode)this.nativeRecords.push(record);
    if (record.type === "system" && record.subtype === "init") {
      if(this.nativeMode && Object.hasOwn(record,"messaging_socket_path") && (typeof record.messaging_socket_path!=="string" || !record.messaging_socket_path.length))malformed("Claude routing metadata is invalid.");
      if (typeof record.session_id !== "string" || record.session_id.length === 0) malformed("Claude init session identity is invalid.");
      if (this.started) {
        const {uuid: _old, ...initial}=this.initialRecord!;
        const {uuid, ...repeated}=record;
        if(this.nativeMode){delete initial.messaging_socket_path;delete repeated.messaging_socket_path;}
        if ((!this.nativeMode && !this.afterTaskNotification) || typeof uuid !== "string" || !uuid || !isDeepStrictEqual(initial,repeated)) malformed("Claude duplicate init is not a matching task resumption.");
        this.afterTaskNotification=false;
        return null;
      }
      this.initialRecord=record;
      this.sessionId = record.session_id;
      return this.start();
    }
    this.requireSession(record);
    this.afterTaskNotification=record.type==="system" && record.subtype==="task_notification";
    if (record.type === "system" && record.subtype === "background_tasks_changed") {
      if (typeof record.uuid !== "string" || !record.uuid || !Array.isArray(record.tasks)) malformed("Claude background inventory invalid.");
      for (const item of record.tasks as unknown[]) {
        const task=asRecord(item,"Claude background task invalid.");
        if (!["task_id","description","task_type"].every(key=>typeof task[key]==="string" && (task[key] as string).length>0)) malformed("Claude background task identity invalid.");
      }
      return null;
    }
    if (record.type === "system" && ["task_started", "task_progress", "task_updated", "task_notification"].includes(String(record.subtype))) {
      if (typeof record.task_id !== "string" || !record.task_id || typeof record.uuid !== "string" || !record.uuid) malformed("Claude task identity missing.");
      if (record.subtype === "task_started" && typeof record.description !== "string") malformed("Claude task description missing.");
      if (record.subtype === "task_updated") asRecord(record.patch, "Claude task patch invalid.");
      if (record.subtype === "task_notification" && !["completed", "failed", "stopped"].includes(String(record.status))) malformed("Claude task status invalid.");
      if (record.subtype === "task_progress") {
        const usage = asRecord(record.usage, "Claude task usage missing.");
        for (const key of ["total_tokens", "tool_uses", "duration_ms"]) if (!Number.isSafeInteger(usage[key]) || (usage[key] as number) < 0) malformed("Claude task usage invalid.");
      }
      return null; // Native task settlement never settles the outer invocation.
    }
    if (record.type === "system" && record.subtype === "permission_denied") {
      if (typeof record.tool_name !== "string" || typeof record.tool_use_id !== "string" || typeof record.message !== "string") malformed("Claude permission denial is invalid.");
      return null;
    }
    if (record.type === "system" && record.subtype === "thinking_tokens") {
      if (!hasExactKeys(record, ["type", "subtype", "estimated_tokens", "estimated_tokens_delta", "uuid", "session_id"])
        || typeof record.uuid !== "string" || record.uuid.length === 0
        || !Number.isSafeInteger(record.estimated_tokens) || (record.estimated_tokens as number) < 0
        || !Number.isSafeInteger(record.estimated_tokens_delta) || (record.estimated_tokens_delta as number) < 0) {
        malformed("Claude thinking-token telemetry is invalid.");
      }
      return null;
    }
    if (record.type === "assistant") {
      const message = asRecord(record.message, "Claude assistant message is invalid.");
      const content = Array.isArray(message.content) ? message.content : malformed("Claude assistant content is invalid.");
      const text = content
        .map((item) => asRecord(item, "Claude assistant content block is invalid."))
        .filter((item) => item.type === "text")
        .map((item) => typeof item.text === "string" ? item.text : malformed("Claude text block is invalid."))
        .join("");
      return this.message(text);
    }
    if (record.type === "result") {
      if (record.subtype !== "success" || record.is_error !== false) {
        throw failure("HARNESS_FAILED", { reason: "Claude emitted a non-success result." });
      }
      if(this.nativeMode)return null;
      return this.complete();
    }
    if (record.type === "user" || record.type === "tool_progress") return null;
    // Newer Claude releases add informational records (e.g. rate_limit_event, new
    // system subtypes). They are session-bound but never settle the invocation.
    if (typeof record.type !== "string" || !record.type) malformed("Claude record type is invalid.");
    if (record.type === "system" && (typeof record.subtype !== "string" || !record.subtype)) malformed("Claude system subtype is invalid.");
    return null;
  }

  private requireSession(record: Record<string, unknown>): void {
    if (!this.started || this.sessionId === null || record.session_id !== this.sessionId) {
      malformed("Claude record carried an unknown or out-of-order session identity.");
    }
  }
}

type PrimeLifecycle =
  | "agent-start" | "turn-start" | "user-start" | "user-end"
  | "assistant-start" | "content-start" | "thinking-end" | "text-start"
  | "text-delta" | "assistant-end" | "turn-end" | "agent-end" | "complete";

class PrimeProtocol extends InstalledProtocol {
  private static readonly MAX_COUNTER = 0xffff_ffff;
  private readonly invocationId: string;
  private acknowledged = false;
  private native: NativeToolLifecycle | null = null;
  private bufferedNative: unknown[] = [];
  private lifecycle: PrimeLifecycle = "agent-start";
  private userMessage: Record<string, unknown> | null = null;
  private assistantMessage: Record<string, unknown> | null = null;
  private assistantThinking = "";
  private thinkingDeltaObserved = false;
  private assistantText = "";
  private textIndex: 0 | 1 | null = null;
  private acceptedRecords = 0;
  private thinkingDeltas = 0;
  private textDeltas = 0;
  private countersSaturated = false;

  constructor(harnessVersion: string | null, invocationId: string) {
    super(harnessVersion);
    this.invocationId = invocationId;
  }

  diagnostic(stage: AdapterDiagnostic["stage"]): AdapterDiagnostic {
    return {
      schema: "openprose.adapter-diagnostic/1",
      adapterId: "prime/rpc",
      stage,
      phase: stage === "jsonl-framing" ? "record-boundary" : this.parserPhase(),
      counters: {
        acceptedRecords: this.acceptedRecords,
        thinkingDeltas: this.thinkingDeltas,
        textDeltas: this.textDeltas,
        saturated: this.countersSaturated,
      },
    };
  }

  accept(value: unknown): RawTransportEvent | null {
    try {
      if (!primeBoundedJson(value)) malformed("Prime RPC record exceeds the bounded lifecycle contract.");
      let event: RawTransportEvent | null;
      if (this.native) {
        event = this.native.accept(value);
        this.terminalEventObserved = this.native.ended;
      } else if (hasNativeTools(value)) {
        this.native = new NativeToolLifecycle(false);
        for (const prior of this.bufferedNative) this.native.accept(prior);
        event = this.native.accept(value);
      } else {
        event = this.acceptRecord(value);
        if ((value as any)?.type !== "response") this.bufferedNative.push(value);
      }
      this.noteAccepted(value);
      return event;
    } catch (caught) {
      if (
        caught instanceof RunnerFailure
        && (caught.code === "PROTOCOL_MALFORMED" || caught.code === "PROTOCOL_TRUNCATED")
      ) {
        throw failure(caught.code, { adapterDiagnostic: this.diagnostic("prime-lifecycle") });
      }
      throw caught;
    }
  }

  private acceptRecord(value: unknown): RawTransportEvent | null {
    const record = this.record(value);
    if (!primeBoundedJson(record)) malformed("Prime RPC record exceeds the bounded lifecycle contract.");
    if (record.type === "extension_ui_request") {
      throw failure("HARNESS_FAILED", { reason: "Prime requested unresolved interactive input; the wrapper did not approve or guess." });
    }
    if (record.type === "response") {
      if (
        this.acknowledged
        || record.id !== this.invocationId
        || record.command !== "prompt"
        || !hasExactKeys(record, ["id", "type", "command", "success"])
        || this.lifecycle !== "agent-start"
      ) malformed("Prime prompt acknowledgement is invalid.");
      if (record.success === false) {
        throw failure("HARNESS_FAILED", { reason: "Prime rejected the prompt command." });
      }
      if (record.success !== true) malformed("Prime prompt acknowledgement is invalid.");
      this.acknowledged = true;
      return null;
    }
    if (!this.acknowledged) malformed("Prime emitted an event before its prompt acknowledgement.");
    if (record.type === "agent_start") {
      this.expectLifecycle(record, "agent-start", ["type"]);
      this.lifecycle = "turn-start";
      return this.start();
    }
    if (record.type === "turn_start") {
      this.expectLifecycle(record, "turn-start", ["type"]);
      this.lifecycle = "user-start";
      return null;
    }
    if (record.type === "message_start") {
      if (!hasExactKeys(record, ["type", "message"])) malformed("Prime message_start carried unknown or missing fields.");
      const message = asRecord(record.message, "Prime message_start payload is invalid.");
      if (this.lifecycle === "user-start" && validPrimeUserMessage(message)) {
        this.userMessage = message;
        this.lifecycle = "user-end";
        return null;
      }
      if (
        this.lifecycle === "assistant-start"
        && validPrimeAssistantMessage(message)
        && primeContentHasShape(message, "empty")
      ) {
        this.lifecycle = "content-start";
        return null;
      }
      malformed("Prime message_start role or ordering is invalid.");
    }
    if (record.type === "message_update") {
      this.acceptMessageUpdate(record);
      return null;
    }
    if (record.type === "message_end") {
      if (!hasExactKeys(record, ["type", "message"])) malformed("Prime message_end carried unknown or missing fields.");
      const message = asRecord(record.message, "Prime message_end payload is invalid.");
      if (this.lifecycle === "user-end" && this.userMessage !== null && deepEqualJson(message, this.userMessage)) {
        this.lifecycle = "assistant-start";
        return null;
      }
      if (
        this.lifecycle === "assistant-end"
        && validPrimeAssistantMessage(message)
        && this.textIndex !== null
        && primeAssistantText(message, this.textIndex) === this.assistantText
      ) {
        this.assistantMessage = message;
        this.lifecycle = "turn-end";
        return this.message(this.assistantText);
      }
      malformed("Prime message_end role or ordering is invalid.");
    }
    if (record.type === "turn_end") {
      if (
        this.lifecycle !== "turn-end"
        || !hasExactKeys(record, ["type", "message", "toolResults"])
        || this.assistantMessage === null
        || !deepEqualJson(record.message, this.assistantMessage)
        || !Array.isArray(record.toolResults)
        || record.toolResults.length !== 0
      ) malformed("Prime turn_end is invalid or outside the no-tool contract.");
      this.lifecycle = "agent-end";
      return null;
    }
    if (record.type === "agent_end") {
      if (
        this.lifecycle !== "agent-end"
        || !hasExactKeys(record, ["type", "messages"])
        || this.userMessage === null
        || this.assistantMessage === null
        || !Array.isArray(record.messages)
        || record.messages.length !== 2
        || !deepEqualJson(record.messages[0], this.userMessage)
        || !deepEqualJson(record.messages[1], this.assistantMessage)
      ) malformed("Prime agent_end is invalid or non-terminal.");
      this.lifecycle = "complete";
      return this.complete();
    }
    malformed(`Prime emitted a flow outside the no-tool lifecycle: ${String(record.type)}.`);
  }

  private noteAccepted(value: unknown): void {
    this.acceptedRecords = this.increment(this.acceptedRecords);
    if (!isRecord(value) || value.type !== "message_update" || !isRecord(value.assistantMessageEvent)) return;
    if (value.assistantMessageEvent.type === "thinking_delta") {
      this.thinkingDeltas = this.increment(this.thinkingDeltas);
    } else if (value.assistantMessageEvent.type === "text_delta") {
      this.textDeltas = this.increment(this.textDeltas);
    }
  }

  private increment(value: number): number {
    if (value === PrimeProtocol.MAX_COUNTER) {
      this.countersSaturated = true;
      return value;
    }
    return value + 1;
  }

  private parserPhase(): PrimeParserPhase {
    if (this.native) return this.native.phase;
    if (!this.acknowledged) return "await-prompt-ack";
    const phases: Record<PrimeLifecycle, PrimeParserPhase> = {
      "agent-start": "await-agent-start",
      "turn-start": "await-turn-start",
      "user-start": "await-user-message-start",
      "user-end": "await-user-message-end",
      "assistant-start": "await-assistant-message-start",
      "content-start": "await-thinking-or-text-start",
      "thinking-end": "await-thinking-delta-or-end",
      "text-start": "await-text-start",
      "text-delta": "await-text-delta-or-end",
      "assistant-end": "await-assistant-message-end",
      "turn-end": "await-turn-end",
      "agent-end": "await-agent-end",
      complete: "complete",
    };
    return phases[this.lifecycle];
  }

  private expectLifecycle(record: Record<string, unknown>, lifecycle: PrimeLifecycle, keys: readonly string[]): void {
    if (this.lifecycle !== lifecycle || !hasExactKeys(record, keys)) {
      malformed(`Prime ${String(record.type)} is duplicate or out of order.`);
    }
  }

  private acceptMessageUpdate(record: Record<string, unknown>): void {
    if (!hasExactKeys(record, ["type", "assistantMessageEvent", "message"])) {
      malformed("Prime message_update carried unknown or missing fields.");
    }
    const message = asRecord(record.message, "Prime message_update payload is invalid.");
    const update = asRecord(record.assistantMessageEvent, "Prime assistantMessageEvent is invalid.");
    if (!validPrimeAssistantMessage(message)) malformed("Prime message_update current assistant message is invalid.");
    if (
      this.lifecycle === "content-start" && update.type === "thinking_start"
      && hasExactKeys(update, ["type", "contentIndex"])
      && update.contentIndex === 0 && primeContentHasShape(message, "thinking-empty")
    ) {
      this.lifecycle = "thinking-end";
      return;
    }
    if (
      this.lifecycle === "content-start" && update.type === "text_start"
      && hasExactKeys(update, ["type", "contentIndex"])
      && update.contentIndex === 0 && primeContentHasShape(message, "text-empty")
    ) {
      this.textIndex = 0;
      this.lifecycle = "text-delta";
      return;
    }
    if (
      this.lifecycle === "thinking-end" && update.type === "thinking_delta"
      && hasExactKeys(update, ["type", "contentIndex", "delta"])
      && update.contentIndex === 0 && typeof update.delta === "string"
      && primeContentHasShape(message, "thinking")
    ) {
      const candidate = this.assistantThinking + update.delta;
      if (Buffer.byteLength(candidate, "utf8") > PRIME_RPC_STRING_BYTES_LIMIT || primeThinkingText(message) !== candidate) {
        malformed("Prime thinking_delta does not match its current assistant message.");
      }
      this.assistantThinking = candidate;
      this.thinkingDeltaObserved = true;
      return;
    }
    if (
      this.lifecycle === "thinking-end" && update.type === "thinking_end"
      && hasExactKeys(update, ["type", "contentIndex", "content"])
      && update.contentIndex === 0 && primeContentHasShape(message, "thinking")
      && update.content === primeThinkingText(message)
    ) {
      if (
        typeof update.content !== "string"
        || (this.thinkingDeltaObserved
          && update.content !== this.assistantThinking
          && `${update.content}\n\n` !== this.assistantThinking)
      ) {
        malformed("Prime thinking_end does not settle its cumulative thinking stream.");
      }
      this.assistantThinking = update.content;
      this.lifecycle = "text-start";
      return;
    }
    if (
      this.lifecycle === "text-start" && update.type === "text_start"
      && hasExactKeys(update, ["type", "contentIndex"])
      && update.contentIndex === 1 && primeContentHasShape(message, "thinking-empty-text")
    ) {
      this.textIndex = 1;
      this.lifecycle = "text-delta";
      return;
    }
    if (
      this.lifecycle === "text-delta" && update.type === "text_delta"
      && hasExactKeys(update, ["type", "contentIndex", "delta"])
      && this.textIndex !== null && update.contentIndex === this.textIndex && typeof update.delta === "string"
      && primeContentHasTextShape(message, this.textIndex, false)
    ) {
      const candidate = this.assistantText + update.delta;
      if (
        Buffer.byteLength(candidate, "utf8") > PRIME_RPC_STRING_BYTES_LIMIT
        || primeAssistantText(message, this.textIndex) !== candidate
      ) {
        malformed("Prime text_delta does not match its current assistant message.");
      }
      this.assistantText = candidate;
      return;
    }
    if (
      this.lifecycle === "text-delta" && update.type === "text_end"
      && hasExactKeys(update, ["type", "contentIndex", "content"])
      && this.textIndex !== null && update.contentIndex === this.textIndex
      && typeof update.content === "string" && update.content.length > 0
      && (this.assistantText.length === 0 || update.content === this.assistantText)
      && primeContentHasTextShape(message, this.textIndex, false)
      && primeAssistantText(message, this.textIndex) === update.content
    ) {
      this.assistantText = update.content;
      this.lifecycle = "assistant-end";
      return;
    }
    malformed("Prime assistant stream update is missing, duplicate, or out of order.");
  }
}

class OmpProtocol extends InstalledProtocol {
  private readonly stateRequestId: string;
  private readonly promptRequestId: string;
  private readonly promptBytes: Uint8Array;
  private pendingStdinBytes: Uint8Array | null = null;
  private ready = false;
  private commandsAdvertised = false;
  private toolsProvedEmpty = false;
  private native: NativeToolLifecycle | null = null;
  private acknowledged = false;
  private agentEnded = false;
  private turnStarted = false;
  private userStarted = false;
  private userEnded = false;
  private assistantStarted = false;
  private assistantEnded = false;
  private turnEnded = false;

  readonly stagedStdin = true;

  constructor(harnessVersion: string | null, invocationId: string, promptBytes: Uint8Array, private readonly nativeMode: boolean) {
    super(harnessVersion);
    this.stateRequestId = `${invocationId}.omp.state.1`;
    this.promptRequestId = `${invocationId}.omp.prompt.1`;
    this.promptBytes = promptBytes;
  }

  override get stdinCloseRequested(): boolean {
    return this.agentEnded;
  }

  takeStagedStdinBytes(): Uint8Array | null {
    const bytes = this.pendingStdinBytes;
    this.pendingStdinBytes = null;
    return bytes;
  }

  accept(value: unknown): RawTransportEvent | null {
    const record = asRecord(value, "OMP emitted a structured value that is not an object.");
    if (!this.ready) {
      this.acceptReady(record);
      this.ready = true;
      return null;
    }
    if (record.type === "extension_ui_request") {
      const disposition = ompExtensionUiDisposition(record);
      if (disposition === "presentation") return null;
      if (disposition === "blocked") {
        throw failure("HARNESS_FAILED", { reason: "OMP requested unsupported extension UI behavior; the wrapper did not approve or guess." });
      }
      malformed("OMP extension UI request is malformed or unknown.");
    }
    if (record.type === "available_commands_update") {
      if (
        this.commandsAdvertised || this.started || this.acknowledged
        || !hasExactKeys(record, ["type", "commands"])
        || !Array.isArray(record.commands)
      ) {
        malformed("OMP available_commands_update is invalid or out of order.");
      }
      this.commandsAdvertised = true;
      this.pendingStdinBytes = new TextEncoder().encode(`${JSON.stringify({ id: this.stateRequestId, type: "get_state" })}\n`);
      return null;
    }
    if (!this.commandsAdvertised) malformed("OMP emitted a frame before its startup command inventory.");
    if (record.type === "response") {
      if (!this.toolsProvedEmpty) {
        if (record.id === this.stateRequestId && record.command === "get_state" && record.success === false) {
          throw failure("HARNESS_FAILED", { reason: "OMP could not prove its registered tool state before prompt delivery." });
        }
        if (
          record.id !== this.stateRequestId
          || record.command !== "get_state"
          || record.success !== true
          || !hasExactKeys(record, ["id", "type", "command", "success", "data"])
        ) malformed("OMP get_state response is invalid or uncorrelated.");
        const data = asRecord(record.data, "OMP get_state data is invalid.");
        if (!Array.isArray(data.dumpTools)) malformed("OMP get_state omitted its tool inventory.");
        if (data.dumpTools.some((tool) => !isRecord(tool) || typeof tool.name !== "string" || tool.name.length === 0)) malformed("OMP tool inventory is invalid.");
        if (data.dumpTools.length !== 0) {
          if (!this.nativeMode) throw failure("HARNESS_FAILED", { reason: "OMP tool inventory is not empty for image-envelope output." });
          this.native = new NativeToolLifecycle(true,null,ompTaskDefaults(data.dumpTools));
        }
        this.toolsProvedEmpty = true;
        this.pendingStdinBytes = this.promptBytes;
        return null;
      }
      if (
        this.acknowledged
        || record.id !== this.promptRequestId
        || record.command !== "prompt"
        || record.success !== true
        || !hasExactKeys(record, ["id", "type", "command", "success"])
      ) malformed("OMP prompt acknowledgement is invalid.");
      this.acknowledged = true;
      return this.agentEnded ? this.complete() : null;
    }
    if (!this.toolsProvedEmpty) malformed("OMP emitted lifecycle data before the empty-tool state proof.");
    if (this.agentEnded) malformed("OMP emitted an event after terminal agent_end.");
    if (this.native) {
      const event = this.native.accept(record);
      if (event?.type === "session.started") this.started = true;
      if (event?.type === "session.completed") {
        this.agentEnded = true;
        return this.acknowledged ? this.complete() : null;
      }
      return event;
    }
    if (record.type === "agent_start") {
      if (this.started || !hasExactKeys(record, ["type"])) malformed("OMP agent_start is duplicate or out of order.");
      return this.start();
    }
    if (record.type === "turn_start") {
      if (!this.started || this.turnStarted || !hasExactKeys(record, ["type"])) malformed("OMP turn_start is out of order.");
      this.turnStarted = true;
      return null;
    }
    if (record.type === "message_start") {
      if (!this.turnStarted || this.turnEnded || !hasExactKeys(record, ["type", "message"])) malformed("OMP message_start is out of order.");
      const role = messageRole(record.message, "OMP message_start payload is invalid.");
      if (role === "user" && !this.userStarted && !this.assistantStarted) {
        this.userStarted = true;
        return null;
      }
      if (role === "assistant" && this.userEnded && !this.assistantStarted) {
        this.assistantStarted = true;
        return null;
      }
      malformed("OMP message_start role or ordering is invalid.");
    }
    if (record.type === "message_update") {
      if (
        !this.assistantStarted || this.assistantEnded
        || !hasExactKeys(record, ["type", "assistantMessageEvent", "message"])
        || messageRole(record.message, "OMP message_update payload is invalid.") !== "assistant"
      ) {
        malformed("OMP message_update is out of order.");
      }
      const update = asRecord(record.assistantMessageEvent, "OMP message_update delta is invalid.");
      if (
        typeof update.type !== "string"
        || !["text_start", "text_delta", "text_end", "thinking_start", "thinking_delta", "thinking_end"].includes(update.type)
      ) {
        malformed("OMP message_update is outside the simple text/reasoning turn contract.");
      }
      return null;
    }
    if (record.type === "message_end") {
      if (!hasExactKeys(record, ["type", "message"])) malformed("OMP message_end carried unknown or missing fields.");
      const role = messageRole(record.message, "OMP message_end payload is invalid.");
      if (role === "user" && this.userStarted && !this.userEnded && !this.assistantStarted) {
        this.userEnded = true;
        return null;
      }
      if (role === "assistant" && this.assistantStarted && !this.assistantEnded) {
        this.assistantEnded = true;
        return this.message(messageText(record.message));
      }
      malformed("OMP message_end role or ordering is invalid.");
    }
    if (record.type === "turn_end") {
      if (
        !this.assistantEnded || this.turnEnded
        || !hasExactKeys(record, ["type", "message", "toolResults"])
        || messageRole(record.message, "OMP turn_end message is invalid.") !== "assistant"
      ) {
        malformed("OMP turn_end is out of order.");
      }
      if (!Array.isArray(record.toolResults) || record.toolResults.length !== 0) {
        malformed("OMP simple-turn contract does not admit tool results.");
      }
      this.turnEnded = true;
      return null;
    }
    if (record.type === "agent_end") {
      const disposition = ompAgentEndDisposition(record);
      if (!this.turnEnded || this.agentEnded || disposition === "invalid") {
        malformed("OMP agent_end is invalid or non-terminal.");
      }
      if (disposition === "nonterminal") {
        throw failure("HARNESS_FAILED", { reason: "unsupported_nonterminal_settlement" });
      }
      this.agentEnded = true;
      return this.acknowledged ? this.complete() : null;
    }
    malformed(`OMP emitted a flow outside the simple-turn contract: ${String(record.type)}.`);
  }

  private acceptReady(record: Record<string, unknown>): void {
    if (record.type !== "ready") malformed("OMP did not emit ready as its first frame.");
    if (hasExactKeys(record, ["type"])) return;
    if (
      !hasExactKeys(record, ["type", "protocolVersion", "supportedProtocolVersions", "maxFrameBytes", "maxReassembledFrameBytes"])
      || record.protocolVersion !== 1
      || !Array.isArray(record.supportedProtocolVersions)
      || record.supportedProtocolVersions.length !== 2
      || record.supportedProtocolVersions[0] !== 1
      || record.supportedProtocolVersions[1] !== 2
      || record.maxFrameBytes !== 1_048_576
      || record.maxReassembledFrameBytes !== 67_108_864
    ) malformed("OMP ready metadata is outside the frozen protocol contract.");
  }
}

export function installedProtocol(
  adapterId: InstalledAdapterId,
  harnessVersion: string | null,
  invocationId: string,
  ompPromptBytes: Uint8Array | null = null,
  nativeMode=false,
  requestedModel:string|null=null,
): StructuredProtocolState {
  if (adapterId === "codex/exec-json") return new CodexProtocol(harnessVersion);
  if (adapterId === "agents-sdk/jsonl") return new AgentsSdkProtocol(harnessVersion,requestedModel);
  if (adapterId === "claude/print-stream-json") return new ClaudeProtocol(harnessVersion,nativeMode);
  if (adapterId === "omp/rpc") {
    if (ompPromptBytes === null) malformed("OMP staged prompt bytes are unavailable.");
    return new OmpProtocol(harnessVersion, invocationId, ompPromptBytes, nativeMode);
  }
  if(nativeMode){if(ompPromptBytes===null)malformed("Prime native prompt bytes are unavailable.");return new PrimeNativeProtocol(harnessVersion,invocationId,ompPromptBytes);}
  return new PrimeProtocol(harnessVersion, invocationId);
}

const PRIME_RPC_COLLECTION_LIMIT = 4_096;
const PRIME_RPC_STRING_BYTES_LIMIT = 65_536;
const PRIME_RPC_NESTING_LIMIT = 32;
const PRIME_ASSISTANT_FIELDS = new Set([
  "role", "content", "api", "provider", "model", "responseModel", "responseId",
  "diagnostics", "usage", "stopReason", "stopReasonRaw", "errorMessage", "timestamp",
]);
const PRIME_ASSISTANT_REQUIRED_FIELDS = [
  "role", "content", "api", "provider", "model", "usage", "stopReason", "timestamp",
] as const;
type PrimeContentShape =
  | "empty" | "text-empty" | "text"
  | "thinking-empty" | "thinking" | "thinking-empty-text" | "thinking-text";

function primeBoundedJson(value: unknown, depth = 0): boolean {
  if (depth > PRIME_RPC_NESTING_LIMIT) return false;
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value === "string") return Buffer.byteLength(value, "utf8") <= PRIME_RPC_STRING_BYTES_LIMIT;
  if (Array.isArray(value)) {
    return value.length <= PRIME_RPC_COLLECTION_LIMIT && value.every((item) => primeBoundedJson(item, depth + 1));
  }
  if (!isRecord(value) || Object.keys(value).length > PRIME_RPC_COLLECTION_LIMIT) return false;
  return Object.entries(value).every(([key, item]) => (
    Buffer.byteLength(key, "utf8") <= PRIME_RPC_STRING_BYTES_LIMIT && primeBoundedJson(item, depth + 1)
  ));
}

function validPrimeUserMessage(message: Record<string, unknown>): boolean {
  const content = message.content;
  const block = Array.isArray(content) && content.length === 1 ? recordOrNull(content[0]) : null;
  return hasExactKeys(message, ["role", "content", "timestamp"])
    && message.role === "user"
    && block !== null
    && hasExactKeys(block, ["type", "text"])
    && block.type === "text"
    && typeof block.text === "string"
    && block.text.length > 0
    && Buffer.byteLength(block.text, "utf8") <= PRIME_RPC_STRING_BYTES_LIMIT
    && nonnegativeInteger(message.timestamp);
}

function validPrimeAssistantMessage(message: Record<string, unknown>): boolean {
  if (
    Object.keys(message).some((key) => !PRIME_ASSISTANT_FIELDS.has(key))
    || PRIME_ASSISTANT_REQUIRED_FIELDS.some((field) => !(field in message))
    || message.role !== "assistant"
    || message.stopReason !== "stop"
    || ![message.api, message.provider, message.model].every((value) => typeof value === "string" && value.length > 0)
    || ![message.responseModel, message.responseId, message.stopReasonRaw, message.errorMessage]
      .every((value) => value === undefined || typeof value === "string")
    || !nonnegativeInteger(message.timestamp)
    || !validPrimeUsage(message.usage)
    || !Array.isArray(message.content)
    || message.content.length > PRIME_RPC_COLLECTION_LIMIT
    || !message.content.every(validPrimeAssistantContent)
    || (message.diagnostics !== undefined && (!Array.isArray(message.diagnostics) || !primeBoundedJson(message.diagnostics)))
  ) return false;
  return true;
}

function validPrimeAssistantContent(value: unknown): boolean {
  const content = recordOrNull(value);
  if (content === null) return false;
  if (content.type === "text") {
    return Object.keys(content).every((key) => ["type", "text", "textSignature"].includes(key))
      && typeof content.text === "string"
      && (content.textSignature === undefined || typeof content.textSignature === "string");
  }
  if (content.type === "thinking") {
    return Object.keys(content).every((key) => ["type", "thinking", "thinkingSignature", "redacted"].includes(key))
      && typeof content.thinking === "string"
      && (content.thinkingSignature === undefined || typeof content.thinkingSignature === "string")
      && (content.redacted === undefined || typeof content.redacted === "boolean");
  }
  return false;
}

function validPrimeUsage(value: unknown): boolean {
  const usage = recordOrNull(value);
  if (usage === null || !hasExactKeys(usage, ["input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"])) return false;
  const cost = recordOrNull(usage.cost);
  return [usage.input, usage.output, usage.cacheRead, usage.cacheWrite, usage.totalTokens].every(nonnegativeNumber)
    && cost !== null
    && hasExactKeys(cost, ["input", "output", "cacheRead", "cacheWrite", "total"])
    && Object.values(cost).every(nonnegativeNumber);
}

function primeContentHasShape(message: Record<string, unknown>, shape: PrimeContentShape): boolean {
  const content = message.content;
  if (!Array.isArray(content)) return false;
  if (shape === "empty") return content.length === 0;
  if (shape === "text-empty" || shape === "text") {
    const text = recordOrNull(content[0]);
    return content.length === 1 && text?.type === "text" && (shape === "text" || text.text === "");
  }
  const thinking = recordOrNull(content[0]);
  if (thinking === null || thinking.type !== "thinking") return false;
  if (shape === "thinking-empty") return content.length === 1 && thinking.thinking === "";
  if (shape === "thinking") return content.length === 1;
  const text = recordOrNull(content[1]);
  if (content.length !== 2 || text === null || text.type !== "text") return false;
  if (shape === "thinking-empty-text") return text.text === "";
  return shape === "thinking-text";
}

function primeContentHasTextShape(message: Record<string, unknown>, textIndex: 0 | 1, empty: boolean): boolean {
  return primeContentHasShape(
    message,
    textIndex === 0 ? (empty ? "text-empty" : "text") : (empty ? "thinking-empty-text" : "thinking-text"),
  );
}

function primeThinkingText(message: Record<string, unknown>): unknown {
  return recordOrNull(Array.isArray(message.content) ? message.content[0] : null)?.thinking;
}

function primeAssistantText(message: Record<string, unknown>, textIndex: 0 | 1): string | null {
  if (!primeContentHasTextShape(message, textIndex, false)) return null;
  const text = recordOrNull((message.content as unknown[])[textIndex])?.text;
  return typeof text === "string" ? text : null;
}

function deepEqualJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => deepEqualJson(item, right[index]));
  }
  const leftRecord = recordOrNull(left);
  const rightRecord = recordOrNull(right);
  if (leftRecord === null || rightRecord === null) return false;
  const leftKeys = Object.keys(leftRecord).sort();
  const rightKeys = Object.keys(rightRecord).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => key === rightKeys[index] && deepEqualJson(leftRecord[key], rightRecord[key]));
}

function messageRole(value: unknown, reason: string): unknown {
  return asRecord(value, reason).role;
}

function messageText(value: unknown): string {
  const message = asRecord(value, "OMP assistant message is invalid.");
  const content = Array.isArray(message.content) ? message.content : malformed("OMP assistant content is invalid.");
  return content
    .map((item) => asRecord(item, "OMP assistant content block is invalid."))
    .filter((item) => item.type === "text")
    .map((item) => typeof item.text === "string" ? item.text : malformed("OMP text block is invalid."))
    .join("");
}

function hasExactKeys(record: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(record).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

const OMP_TERMINAL_COLLECTION_LIMIT = 4_096;
const OMP_TERMINAL_STRING_BYTES_LIMIT = 65_536;
const OMP_COUNTER_FIELDS = ["total", "ok", "error", "skipped", "blocked", "timeout", "aborted"] as const;
const OMP_BLOCKED_EXTENSION_UI_METHODS = new Set([
  "select", "confirm", "input", "editor", "open_url", "cancel",
]);

function ompExtensionUiDisposition(record: Record<string, unknown>): "presentation" | "blocked" | "malformed" {
  if (record.method === "notify") {
    return exactBoundedPresentation(record, ["message"], ["notifyType"])
      && (record.notifyType === undefined || ["info", "warning", "error"].includes(record.notifyType as string))
      ? "presentation" : "malformed";
  }
  if (record.method === "setStatus") {
    return exactBoundedPresentation(record, ["statusKey"], ["statusText"])
      ? "presentation" : "malformed";
  }
  if (record.method === "setTitle") {
    return exactBoundedPresentation(record, ["title"], []) ? "presentation" : "malformed";
  }
  if (record.method === "set_editor_text") {
    return exactBoundedPresentation(record, ["text"], []) ? "presentation" : "malformed";
  }
  if (record.method !== "setWidget") {
    return typeof record.method === "string" && OMP_BLOCKED_EXTENSION_UI_METHODS.has(record.method)
      ? "blocked"
      : "malformed";
  }
  const allowed = new Set(["type", "id", "method", "widgetKey", "widgetLines", "widgetPlacement"]);
  if (
    Object.keys(record).some((key) => !allowed.has(key))
    || typeof record.id !== "string"
    || record.id.length === 0
    || !boundedString(record.id)
    || !boundedString(record.widgetKey)
  ) return "malformed";
  if (
    "widgetLines" in record
    && (
      !Array.isArray(record.widgetLines)
      || record.widgetLines.length > OMP_TERMINAL_COLLECTION_LIMIT
      || !record.widgetLines.every(boundedString)
    )
  ) return "malformed";
  if (
    "widgetPlacement" in record
    && record.widgetPlacement !== "aboveEditor"
    && record.widgetPlacement !== "belowEditor"
  ) return "malformed";
  return "presentation";
}

function exactBoundedPresentation(
  record: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[],
): boolean {
  const allowed = new Set(["type", "id", "method", ...required, ...optional]);
  return Object.keys(record).every((key) => allowed.has(key))
    && typeof record.id === "string"
    && record.id.length > 0
    && boundedString(record.id)
    && required.every((field) => boundedString(record[field]))
    && optional.every((field) => record[field] === undefined || boundedString(record[field]));
}

function ompAgentEndDisposition(record: Record<string, unknown>): "terminal" | "nonterminal" | "invalid" {
  const allowed = new Set(["type", "messages", "isTerminal", "messageCount", "telemetry", "coverage"]);
  if (
    Object.keys(record).some((key) => !allowed.has(key))
    || !Array.isArray(record.messages)
    || (record.isTerminal !== true && record.isTerminal !== false)
  ) return "invalid";

  if ("messageCount" in record) {
    if (!nonnegativeInteger(record.messageCount) || record.messageCount < record.messages.length) return "invalid";
  }

  const hasTelemetry = "telemetry" in record;
  const hasCoverage = "coverage" in record;
  if (!(hasTelemetry === hasCoverage
    && (!hasTelemetry || (validOmpTelemetry(record.telemetry) && validOmpCoverage(record.coverage))))) return "invalid";
  return record.isTerminal === false ? "nonterminal" : "terminal";
}

function validOmpTelemetry(value: unknown): boolean {
  if (!isRecord(value) || !hasExactKeys(value, ["chats", "tools", "usage", "cost", "errors", "stepCount"])) return false;
  const chats = recordOrNull(value.chats);
  const tools = recordOrNull(value.tools);
  const usage = recordOrNull(value.usage);
  const cost = recordOrNull(value.cost);
  const errors = recordOrNull(value.errors);
  if (chats === null || tools === null || usage === null || cost === null || errors === null) return false;

  return hasExactKeys(chats, ["total", "byStopReason", "totalLatencyMs"])
    && nonnegativeInteger(chats.total)
    && nonnegativeNumber(chats.totalLatencyMs)
    && validCounterMap(chats.byStopReason)
    && hasExactKeys(tools, [...OMP_COUNTER_FIELDS, "totalLatencyMs", "byName"])
    && OMP_COUNTER_FIELDS.every((field) => nonnegativeInteger(tools[field]))
    && nonnegativeNumber(tools.totalLatencyMs)
    && validToolCounterMap(tools.byName)
    && hasExactKeys(usage, ["inputTokens", "outputTokens", "cachedInputTokens", "cacheWriteTokens", "reasoningOutputTokens", "totalTokens"])
    && Object.values(usage).every(nonnegativeInteger)
    && hasExactKeys(cost, ["estimatedUsd", "unavailableReasons"])
    && nonnegativeNumber(cost.estimatedUsd)
    && validSortedStringList(cost.unavailableReasons)
    && hasExactKeys(errors, ["total", "byType"])
    && nonnegativeInteger(errors.total)
    && validCounterMap(errors.byType)
    && nonnegativeInteger(value.stepCount);
}

function validOmpCoverage(value: unknown): boolean {
  if (!isRecord(value) || !hasExactKeys(value, ["toolsAvailable", "toolsInvoked", "toolsUnused", "modelsUsed", "providersUsed"])) return false;
  return Object.values(value).every(validSortedStringList);
}

function validToolCounterMap(value: unknown): boolean {
  const record = recordOrNull(value);
  if (record === null || Object.keys(record).length > OMP_TERMINAL_COLLECTION_LIMIT) return false;
  return Object.entries(record).every(([key, counters]) => {
    const item = recordOrNull(counters);
    return boundedString(key)
      && item !== null
      && hasExactKeys(item, [...OMP_COUNTER_FIELDS, "totalLatencyMs"])
      && OMP_COUNTER_FIELDS.every((field) => nonnegativeInteger(item[field]))
      && nonnegativeNumber(item.totalLatencyMs);
  });
}

function validCounterMap(value: unknown): boolean {
  const record = recordOrNull(value);
  return record !== null
    && Object.keys(record).length <= OMP_TERMINAL_COLLECTION_LIMIT
    && Object.entries(record).every(([key, count]) => boundedString(key) && nonnegativeInteger(count));
}

function validSortedStringList(value: unknown): boolean {
  if (!Array.isArray(value) || value.length > OMP_TERMINAL_COLLECTION_LIMIT) return false;
  let previous: string | null = null;
  for (const item of value) {
    if (!boundedString(item) || (previous !== null && previous >= item)) return false;
    previous = item;
  }
  return true;
}

function boundedString(value: unknown): value is string {
  return typeof value === "string" && Buffer.byteLength(value, "utf8") <= OMP_TERMINAL_STRING_BYTES_LIMIT;
}

function nonnegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function nonnegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function recordOrNull(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function asRecord(value: unknown, reason: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) malformed(reason);
  return value as Record<string, unknown>;
}

function malformed(reason: string): never {
  throw failure("PROTOCOL_MALFORMED", { reason });
}
