import { describe, expect, test } from "bun:test";
import { installedProtocol } from "../src/adapters/protocols";

const invocationId = "fixture-invocation-0001";
const stateRequestId = `${invocationId}.omp.state.1`;
const promptRequestId = `${invocationId}.omp.prompt.1`;
const promptBytes = new TextEncoder().encode(`${JSON.stringify({ id: promptRequestId, type: "prompt", message: "task" })}\n`);
const assistant = { role: "assistant", content: [{ type: "text", text: "hello" }] };
const user = { role: "user", content: [{ type: "text", text: "task" }] };
const ack = { id: promptRequestId, type: "response", command: "prompt", success: true };
const stateResponse = {
  id: stateRequestId,
  type: "response",
  command: "get_state",
  success: true,
  data: { dumpTools: [] },
};
const telemetry = {
  chats: { total: 1, byStopReason: { stop: 1 }, totalLatencyMs: 12.5 },
  tools: {
    total: 0,
    ok: 0,
    error: 0,
    skipped: 0,
    blocked: 0,
    timeout: 0,
    aborted: 0,
    totalLatencyMs: 0,
    byName: {},
  },
  usage: {
    inputTokens: 4,
    outputTokens: 2,
    cachedInputTokens: 0,
    cacheWriteTokens: 0,
    reasoningOutputTokens: 0,
    totalTokens: 6,
  },
  cost: { estimatedUsd: 0, unavailableReasons: ["fixture-provider"] },
  errors: { total: 0, byType: {} },
  stepCount: 1,
};
const coverage = {
  toolsAvailable: [],
  toolsInvoked: [],
  toolsUnused: [],
  modelsUsed: ["fixture-model"],
  providersUsed: ["openrouter"],
};

const widget = {
  type: "extension_ui_request",
  id: "widget-request-0001",
  method: "setWidget",
  widgetKey: "autoresearch",
  widgetLines: ["status", "running"],
  widgetPlacement: "aboveEditor",
};

function ready(current = true): Record<string, unknown> {
  return current ? {
    type: "ready",
    protocolVersion: 1,
    supportedProtocolVersions: [1, 2],
    maxFrameBytes: 1_048_576,
    maxReassembledFrameBytes: 67_108_864,
  } : { type: "ready" };
}

function lifecycle(): Array<Record<string, unknown>> {
  return [
    { type: "agent_start" },
    { type: "turn_start" },
    { type: "message_start", message: user },
    { type: "message_end", message: user },
    { type: "message_start", message: { role: "assistant", content: [] } },
    { type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "hello" }, message: { role: "assistant", content: [] } },
    { type: "message_end", message: assistant },
    { type: "turn_end", message: assistant, toolResults: [] },
    { type: "agent_end", messages: [user, assistant], telemetry, coverage, isTerminal: true },
  ];
}

function started(current = true) {
  const protocol = installedProtocol("omp/rpc", "omp/18.0.9", invocationId, promptBytes);
  expect(protocol.accept(ready(current))).toBeNull();
  expect(protocol.accept({ type: "available_commands_update", commands: [{ name: "help", source: "builtin" }] })).toBeNull();
  expect(new TextDecoder().decode(protocol.takeStagedStdinBytes?.() ?? new Uint8Array())).toBe(
    `${JSON.stringify({ id: stateRequestId, type: "get_state" })}\n`,
  );
  expect(protocol.accept(stateResponse)).toBeNull();
  expect(protocol.takeStagedStdinBytes?.()).toEqual(promptBytes);
  return protocol;
}

function beforeStateProof(nativeMode = false) {
  const protocol = installedProtocol("omp/rpc", "omp/18.0.9", invocationId, promptBytes, nativeMode);
  protocol.accept(ready());
  protocol.accept({ type: "available_commands_update", commands: [] });
  protocol.takeStagedStdinBytes?.();
  return protocol;
}

function expectFailure(action: () => unknown, code: string): void {
  try {
    action();
    throw new Error("expected protocol failure");
  } catch (caught) {
    expect(caught).toMatchObject({ code });
  }
}

describe("upstream OMP v18 RPC grammar", () => {
  test.each([true, false])("accepts %s ready and an acknowledgement before the lifecycle", (current) => {
    const protocol = started(current);
    expect(protocol.accept(ack)).toBeNull();
    const events = lifecycle().map((record) => protocol.accept(record)).filter((event) => event !== null);
    expect(events).toEqual([
      { type: "session.started", harnessVersion: "omp/18.0.9" },
      { type: "assistant.message", text: "hello" },
      { type: "session.completed" },
    ]);
    expect(protocol.stdinCloseRequested).toBeTrue();
    expect(protocol.terminalEventObserved).toBeTrue();
  });

  test("stages prompt delivery only after the correlated tool inventory", () => {
    for (const invalid of [
      { ...stateResponse, id: `${stateRequestId}-wrong` },
      { ...stateResponse, command: "get_messages" },
      { id: stateRequestId, type: "response", command: "get_state", data: { dumpTools: [] } },
      { ...stateResponse, success: true, data: {} },
      { ...stateResponse, success: true, data: { dumpTools: null } },
      { ...stateResponse, extra: true },
    ]) {
      const protocol = beforeStateProof();
      expectFailure(() => protocol.accept(invalid), "PROTOCOL_MALFORMED");
    }
    for (const invalid of [
      { id: stateRequestId, type: "response", command: "get_state", success: false, error: "denied" },
    ]) {
      const protocol = beforeStateProof();
      expectFailure(() => protocol.accept(invalid), "HARNESS_FAILED");
    }
    const envelope = beforeStateProof();
    expectFailure(() => envelope.accept({...stateResponse,data:{dumpTools:[{name:"read"}]}}), "HARNESS_FAILED");
    const enabled = beforeStateProof(true);
    expect(enabled.accept({...stateResponse,data:{dumpTools:[{name:"read"}]}})).toBeNull();
    const malformedInventory = beforeStateProof();
    expectFailure(()=>malformedInventory.accept({...stateResponse,data:{dumpTools:[{}]}}),"PROTOCOL_MALFORMED");
    const reordered = beforeStateProof();
    expectFailure(() => reordered.accept({ type: "agent_start" }), "PROTOCOL_MALFORMED");
    const duplicate = started();
    expectFailure(() => duplicate.accept(stateResponse), "PROTOCOL_MALFORMED");
  });

  test("closes stdin at agent_end and accepts only the still-missing correlated ack afterward", () => {
    const protocol = started();
    const events = lifecycle().map((record) => protocol.accept(record)).filter((event) => event !== null);
    expect(events).toEqual([
      { type: "session.started", harnessVersion: "omp/18.0.9" },
      { type: "assistant.message", text: "hello" },
    ]);
    expect(protocol.stdinCloseRequested).toBeTrue();
    expect(protocol.terminalEventObserved).toBeFalse();
    expect(protocol.accept(ack)).toEqual({ type: "session.completed" });
    expect(protocol.terminalEventObserved).toBeTrue();
  });

  test("accepts only the documented terminal metadata variants and bounded compaction count", () => {
    for (const terminal of [
      { type: "agent_end", messages: [user, assistant], isTerminal: true },
      { type: "agent_end", messages: [user, assistant], telemetry, coverage, isTerminal: true },
      { type: "agent_end", messages: [], messageCount: 2, telemetry, coverage, isTerminal: true },
    ]) {
      const protocol = started();
      protocol.accept(ack);
      const frames = lifecycle();
      for (const frame of frames.slice(0, -1)) protocol.accept(frame);
      expect(protocol.accept(terminal)).toEqual({ type: "session.completed" });
    }
  });

  test("rejects absent startup inventory, duplicate or enriched ack, and nonterminal agent_end", () => {
    const missingInventory = installedProtocol("omp/rpc", null, invocationId, promptBytes);
    missingInventory.accept(ready());
    expectFailure(() => missingInventory.accept(ack), "PROTOCOL_MALFORMED");

    const duplicate = started();
    duplicate.accept(ack);
    expectFailure(() => duplicate.accept(ack), "PROTOCOL_MALFORMED");

    const enriched = started();
    expectFailure(() => enriched.accept({ ...ack, data: { agentInvoked: true } }), "PROTOCOL_MALFORMED");

    for (const isTerminal of [null, "true", 1, undefined]) {
      const nonterminal = started();
      nonterminal.accept(ack);
      const frames = lifecycle();
      for (const frame of frames.slice(0, -1)) nonterminal.accept(frame);
      expectFailure(() => nonterminal.accept({ ...frames[frames.length - 1]!, isTerminal }), "PROTOCOL_MALFORMED");
    }

    const nonterminal = started();
    nonterminal.accept(ack);
    const frames = lifecycle();
    for (const frame of frames.slice(0, -1)) nonterminal.accept(frame);
    try {
      nonterminal.accept({ ...frames.at(-1), isTerminal: false });
      throw new Error("nonterminal OMP settlement unexpectedly passed");
    } catch (caught) {
      expect(caught).toMatchObject({
        code: "HARNESS_FAILED",
        details: { reason: "unsupported_nonterminal_settlement" },
      });
    }
  });

  test.each([
    { type: "tool_execution_start", toolCallId: "tool-1" },
    { type: "auto_retry_start", attempt: 1 },
    { type: "auto_compaction_start", reason: "threshold" },
    { type: "command_output", text: "side channel" },
  ])("rejects non-simple-turn flow $type", (record) => {
    const protocol = started();
    protocol.accept({ type: "agent_start" });
    expectFailure(() => protocol.accept(record), "PROTOCOL_MALFORMED");
  });

  test.each(["toolcall_start", "toolcall_delta", "toolcall_end"])(
    "rejects tool behavior hidden in assistant message progress: %s",
    (type) => {
      const protocol = started();
      for (const frame of lifecycle().slice(0, 5)) protocol.accept(frame);
      expectFailure(
        () => protocol.accept({
          type: "message_update",
          assistantMessageEvent: { type, contentIndex: 0 },
          message: { role: "assistant", content: [] },
        }),
        "PROTOCOL_MALFORMED",
      );
    },
  );

  test("fails interactive requests and every event after agent_end", () => {
    const interactive = started();
    for (const method of ["select", "confirm", "input", "editor", "open_url"]) {
      expectFailure(
        () => interactive.accept({ type: "extension_ui_request", id: "ui", method }),
        "HARNESS_FAILED",
      );
    }

    const postEnd = started();
    for (const frame of lifecycle()) postEnd.accept(frame);
    expectFailure(() => postEnd.accept({ type: "notice", message: "late" }), "PROTOCOL_MALFORMED");
  });

  test("accepts exact fire-and-forget setWidget presentation before inventory, during a turn, and after agent_end", () => {
    const protocol = installedProtocol("omp/rpc", "omp/18.0.9", invocationId, promptBytes);
    expect(protocol.accept(ready())).toBeNull();
    expect(protocol.accept(widget)).toBeNull();
    expect(protocol.accept({
      type: "extension_ui_request",
      id: "widget-request-0002",
      method: "setWidget",
      widgetKey: "autoresearch",
    })).toBeNull();
    expect(protocol.accept({ type: "available_commands_update", commands: [] })).toBeNull();
    protocol.takeStagedStdinBytes?.();
    expect(protocol.accept(stateResponse)).toBeNull();
    expect(protocol.takeStagedStdinBytes?.()).toEqual(promptBytes);
    expect(protocol.accept(ack)).toBeNull();
    const frames = lifecycle();
    for (const frame of frames.slice(0, -2)) protocol.accept(frame);
    expect(protocol.accept({
      type: "extension_ui_request",
      id: "widget-request-0003",
      method: "setWidget",
      widgetKey: "autoresearch",
      widgetLines: [],
      widgetPlacement: "belowEditor",
    })).toBeNull();
    expect(protocol.accept(frames.at(-2))).toBeNull();
    expect(protocol.accept(frames.at(-1))).toEqual({ type: "session.completed" });
    expect(protocol.accept(widget)).toBeNull();
    for (const presentation of [
      { type: "extension_ui_request", id: "notify", method: "notify", message: "status", notifyType: "info" },
      { type: "extension_ui_request", id: "status", method: "setStatus", statusKey: "run", statusText: "active" },
      { type: "extension_ui_request", id: "title", method: "setTitle", title: "Run" },
      { type: "extension_ui_request", id: "editor", method: "set_editor_text", text: "presentation only" },
    ]) expect(protocol.accept(presentation)).toBeNull();
    expectFailure(() => protocol.accept({ type: "notice", message: "late" }), "PROTOCOL_MALFORMED");
  });

  test("rejects malformed, unknown, and oversized setWidget presentation frames", () => {
    const invalid = [
      { type: "extension_ui_request", id: "ui", method: "setWidget" },
      { ...widget, id: "" },
      { ...widget, id: "x".repeat(65_537) },
      { ...widget, method: "set_widget" },
      { ...widget, extra: true },
      { ...widget, widgetKey: 1 },
      { ...widget, widgetKey: "x".repeat(65_537) },
      { ...widget, widgetLines: null },
      { ...widget, widgetLines: ["ok", 1] },
      { ...widget, widgetLines: Array.from({ length: 4_097 }, () => "line") },
      { ...widget, widgetLines: ["x".repeat(65_537)] },
      { ...widget, widgetPlacement: "sidebar" },
    ];
    for (const frame of invalid) {
      const protocol = installedProtocol("omp/rpc", "omp/18.0.9", invocationId, promptBytes);
      protocol.accept(ready());
      expectFailure(() => protocol.accept(frame), "PROTOCOL_MALFORMED");
    }
  });

  test("rejects unknown or drifted ready metadata", () => {
    for (const invalid of [
      { type: "agent_start" },
      { ...ready(), maxFrameBytes: 2 },
      { ...ready(), extra: true },
    ]) {
      const protocol = installedProtocol("omp/rpc", null, invocationId, promptBytes);
      expectFailure(() => protocol.accept(invalid), "PROTOCOL_MALFORMED");
    }
  });

  test("rejects unknown lifecycle fields and malformed terminal metadata", () => {
    const protocol = started();
    expectFailure(() => protocol.accept({ type: "agent_start", unexpected: true }), "PROTOCOL_MALFORMED");

    for (const invalid of [
      { type: "agent_end", messages: [], telemetry },
      { type: "agent_end", messages: [], coverage },
      { type: "agent_end", messages: [], telemetry: {}, coverage },
      { type: "agent_end", messages: [], telemetry: { ...telemetry, extra: true }, coverage },
      { type: "agent_end", messages: [user], messageCount: 0 },
      { type: "agent_end", messages: [], messageCount: -1 },
      { type: "agent_end", messages: [], messageCount: "2" },
      { type: "agent_end", messages: [], telemetry, coverage: { ...coverage, modelsUsed: ["z", "a"] } },
      { type: "agent_end", messages: [], telemetry, coverage, unexpected: true },
    ]) {
      const terminal = started();
      terminal.accept(ack);
      const frames = lifecycle();
      for (const frame of frames.slice(0, -1)) terminal.accept(frame);
      expectFailure(() => terminal.accept(invalid), "PROTOCOL_MALFORMED");
    }

    const tooMany = started();
    tooMany.accept(ack);
    const frames = lifecycle();
    for (const frame of frames.slice(0, -1)) tooMany.accept(frame);
    expectFailure(
      () => tooMany.accept({
        type: "agent_end",
        messages: [],
        telemetry,
        coverage: { ...coverage, toolsAvailable: Array.from({ length: 4_097 }, (_, index) => `tool-${index.toString().padStart(4, "0")}`) },
      }),
      "PROTOCOL_MALFORMED",
    );
  });
});
