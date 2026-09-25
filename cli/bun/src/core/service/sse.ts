// Bounded server-sent events reader. Lines end at LF (a
// preceding CR is dropped); `event`, `data` and `id` are kept; comment lines
// (`: heartbeat`) are ignored; a blank line dispatches an event whose data
// buffer is non-empty; an unterminated event at end of stream is discarded.
// Event data is at most `maxEventBytes` (UTF-8); larger events fail with
// SERVICE_RESPONSE_TOO_LARGE. The reader never retries.
import { failure } from "../errors";
import type { Json, JsonObject } from "./manifest";
import { canonicalJson } from "./render";
import { parseJsonText } from "./http";

export type StreamEnd = "closed" | "disconnected" | "idle";
export interface SseEvent { id?: string; event: string; data: string }
export type SseItem = { kind: "event"; event: SseEvent } | { kind: "end"; end: StreamEnd };
export type Chunk = { kind: "data"; bytes: Uint8Array } | { kind: "end"; end: StreamEnd };

export interface ChunkSource { next(signal: AbortSignal): Promise<Chunk> }

/** Parses the event data as JSON, or SERVICE_PROTOCOL_INVALID. */
export function eventJson(event: SseEvent): Json {
  const value = parseJsonText(event.data);
  if (value === undefined) throw failure("SERVICE_PROTOCOL_INVALID");
  return value;
}

/** Reads a network body; the idle timeout and cancellation end the wait. */
export class StreamSource implements ChunkSource {
  private pending: Promise<{ done: boolean; value?: Uint8Array | undefined }> | undefined;
  constructor(private readonly reader: ReadableStreamDefaultReader<Uint8Array>, private readonly idleMs: number) {}
  async next(signal: AbortSignal): Promise<Chunk> {
    const pending = this.pending ?? this.reader.read();
    this.pending = pending;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let onAbort: (() => void) | undefined;
    const outcome = await Promise.race([
      pending.then((item) => ({ item }), () => ({ error: true as const })),
      new Promise<{ idle: true }>((resolve) => { timer = setTimeout(() => resolve({ idle: true }), this.idleMs); }),
      new Promise<{ aborted: true }>((resolve) => {
        onAbort = () => resolve({ aborted: true });
        if (signal.aborted) onAbort();
        else signal.addEventListener("abort", onAbort, { once: true });
      }),
    ]);
    if (timer !== undefined) clearTimeout(timer);
    if (onAbort !== undefined) signal.removeEventListener("abort", onAbort);
    if ("idle" in outcome || "aborted" in outcome) {
      await this.reader.cancel().catch(() => {});
      return { kind: "end", end: "aborted" in outcome ? "disconnected" : "idle" };
    }
    this.pending = undefined;
    if ("error" in outcome) return { kind: "end", end: "disconnected" };
    if (outcome.item.done || outcome.item.value === undefined) return { kind: "end", end: "closed" };
    return { kind: "data", bytes: outcome.item.value };
  }
}

/** Replays fixture frames; runs `onComplete` once every frame was delivered. */
export class FixtureSource implements ChunkSource {
  private index = 0;
  private completed = false;
  constructor(private readonly frames: Uint8Array[], private readonly end: StreamEnd, private readonly onComplete: () => void) {}
  async next(): Promise<Chunk> {
    const frame = this.frames[this.index];
    if (frame !== undefined) {
      this.index += 1;
      return { kind: "data", bytes: frame };
    }
    if (!this.completed) {
      this.completed = true;
      this.onComplete();
    }
    return { kind: "end", end: this.end };
  }
}

/** Serializes fixture frames (`sse.frames`). JSON data uses sorted keys. */
export function fixtureBytes(frames: Json): Uint8Array[] {
  if (!Array.isArray(frames)) return [];
  return frames.map((raw) => {
    const frame = (raw ?? {}) as JsonObject;
    if (typeof frame.raw === "string") return new TextEncoder().encode(frame.raw);
    let text = "";
    if (typeof frame.comment === "string") text += `: ${frame.comment}\n`;
    if (typeof frame.id === "string") text += `id: ${frame.id}\n`;
    if (typeof frame.event === "string") text += `event: ${frame.event}\n`;
    if ("data" in frame) {
      const data = frame.data!;
      if (typeof data === "string") for (const line of data.split("\n")) text += `data: ${line}\n`;
      else text += `data: ${canonicalJson(data)}\n`;
    }
    return new TextEncoder().encode(`${text}\n`);
  });
}

/** A pull parser over a chunk source. */
export class SseReader {
  private pending = new Uint8Array();
  private id: string | undefined;
  private event: string | undefined;
  private data: string | undefined;
  private dataBytes = 0;
  private ended: StreamEnd | undefined;
  constructor(private readonly source: ChunkSource, private readonly signal: AbortSignal, private readonly maxEvent: number) {}

  /** The next event or the end of the stream. */
  async next(): Promise<SseItem> {
    for (;;) {
      if (this.signal.aborted) throw failure("CANCELLED");
      const newline = this.pending.indexOf(10);
      if (newline >= 0) {
        let line = this.pending.subarray(0, newline);
        this.pending = this.pending.slice(newline + 1);
        if (line.length > 0 && line[line.length - 1] === 13) line = line.subarray(0, line.length - 1);
        const event = this.line(line);
        if (event !== undefined) return { kind: "event", event };
        continue;
      }
      if (this.pending.length > this.maxEvent + 64) throw failure("SERVICE_RESPONSE_TOO_LARGE");
      if (this.ended !== undefined) return { kind: "end", end: this.ended };
      const chunk = await this.source.next(this.signal);
      if (chunk.kind === "data") {
        const merged = new Uint8Array(this.pending.length + chunk.bytes.length);
        merged.set(this.pending);
        merged.set(chunk.bytes, this.pending.length);
        this.pending = merged;
      } else {
        if (this.signal.aborted) throw failure("CANCELLED");
        this.pending = new Uint8Array();
        this.ended = chunk.end;
      }
    }
  }

  private line(bytes: Uint8Array): SseEvent | undefined {
    if (bytes.length === 0) {
      const data = this.data;
      const event: SseEvent | undefined = data === undefined ? undefined : { event: this.event ?? "message", data, ...(this.id === undefined ? {} : { id: this.id }) };
      this.id = undefined;
      this.event = undefined;
      this.data = undefined;
      this.dataBytes = 0;
      return event;
    }
    if (bytes[0] === 58) return undefined;
    let text: string;
    try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
    catch { throw failure("SERVICE_PROTOCOL_INVALID"); }
    const colon = text.indexOf(":");
    const field = colon >= 0 ? text.slice(0, colon) : text;
    let value = colon >= 0 ? text.slice(colon + 1) : "";
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "id" && !value.includes("\0")) this.id = value;
    else if (field === "data") {
      const size = (this.data === undefined ? 0 : this.dataBytes + 1) + new TextEncoder().encode(value).length;
      if (size > this.maxEvent) throw failure("SERVICE_RESPONSE_TOO_LARGE");
      this.data = this.data === undefined ? value : `${this.data}\n${value}`;
      this.dataBytes = size;
    }
    return undefined;
  }
}
