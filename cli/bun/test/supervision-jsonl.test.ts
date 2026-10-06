import { describe, expect, test } from "bun:test";
import { readBoundedJsonLines } from "../src/supervision/jsonl";
import type { TransportLimits } from "../src/supervision/types";

const limits: TransportLimits = {
  maxRecordBytes: 128,
  maxAggregateStdoutBytes: 512,
  maxAggregateStderrBytes: 128,
  maxQueuedRecords: 2,
};

function chunks(...values: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const value of values) controller.enqueue(encoder.encode(value));
      controller.close();
    },
  });
}

describe("bounded JSONL framing", () => {
  test("accepts fragmented UTF-8 records and CRLF without buffering the full stream", async () => {
    const records: unknown[] = [];
    await readBoundedJsonLines(
      chunks('{"message":"雪', ' 🚀"}\r', '\n{"value":', "2}\n"),
      limits,
      (record) => { records.push(record); },
    );
    expect(records).toEqual([{ message: "雪 🚀" }, { value: 2 }]);
  });

  test("applies natural backpressure with only one consumer callback in flight", async () => {
    let active = 0;
    let maximumActive = 0;
    const seen: number[] = [];
    await readBoundedJsonLines(chunks('{"n":1}\n{"n":2}\n{"n":3}\n'), limits, async (record) => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await Bun.sleep(2);
      seen.push((record as { n: number }).n);
      active -= 1;
    });
    expect(seen).toEqual([1, 2, 3]);
    expect(maximumActive).toBe(1);
  });

  test.each([
    ["malformed JSON", chunks('{"bad":}\n'), "PROTOCOL_MALFORMED"],
    ["truncated EOF", chunks('{"unfinished":'), "PROTOCOL_TRUNCATED"],
    ["empty record", chunks("\n"), "PROTOCOL_MALFORMED"],
    ["oversized record", chunks(`${JSON.stringify({ value: "x".repeat(130) })}\n`), "HARNESS_FAILED"],
    ["aggregate overflow", chunks(`${JSON.stringify({ a: "x".repeat(80) })}\n`, `${JSON.stringify({ b: "y".repeat(80) })}\n`), "PROTOCOL_MALFORMED"],
  ])("rejects %s deterministically", async (_label, stream, code) => {
    const constrained = _label === "aggregate overflow" ? { ...limits, maxAggregateStdoutBytes: 150 } : limits;
    await expect(readBoundedJsonLines(stream, constrained, () => {})).rejects.toMatchObject({ code });
  });
});

import diagnosticCases from "../../shared/fixtures/transport-diagnostics.json";
for (const fixture of diagnosticCases) test(`safe transport diagnostic: ${fixture.name}`, async()=>{
 let caught:any;
 try {await readBoundedJsonLines(chunks(fixture.input), {...limits,maxRecordBytes:fixture.recordLimit,maxAggregateStdoutBytes:fixture.aggregateLimit},()=>{});}catch(e){caught=e;}
 expect(caught.code).toBe(fixture.reason === "record-byte-limit" ? "HARNESS_FAILED" : "PROTOCOL_MALFORMED");
 expect(caught.details.transportDiagnostic.reason).toBe(fixture.reason);
 if("limitBytes" in fixture)expect(caught.details.transportDiagnostic.limitBytes).toBe(fixture.limitBytes);
 if("observedBytes" in fixture)expect(caught.details.transportDiagnostic.observedBytes).toBe(fixture.observedBytes);
 expect(JSON.stringify(caught.details)).not.toContain("secret-invalid");
});

import {normalizeProtocolFailure} from "../src/supervision/jsonl";
import {failure} from "../src/core/errors";
test("lifecycle diagnostic preserves existing adapter evidence",()=>{
 const adapterDiagnostic={stage:"prime-lifecycle",phase:"tool-turn-open"};
 const error=normalizeProtocolFailure(failure("PROTOCOL_MALFORMED",{adapterDiagnostic}));
 expect(error.details?.adapterDiagnostic).toEqual(adapterDiagnostic);
 expect(error.details?.transportDiagnostic).toEqual({schema:"openprose.transport-diagnostic/1",reason:"lifecycle-rejection"});
});

import {installedProcessFailureDetails} from "../src/cli";
test("invalid UTF8 is distinct and emitted CLI details retain safe diagnostics",async()=>{
 const stream=new ReadableStream<Uint8Array>({start(c){c.enqueue(new Uint8Array([255,10]));c.close();}});
 let error:any;try{await readBoundedJsonLines(stream,limits,()=>{});}catch(e){error=e;}
 expect(error.details.transportDiagnostic.reason).toBe("invalid-utf8");
 const details=installedProcessFailureDetails({error,exitCode:0,signal:null,terminalEventObserved:false} as any,"omp/rpc");
 expect(details.transportDiagnostic).toEqual(error.details.transportDiagnostic);
 expect(JSON.stringify(details)).not.toContain("255");
});
