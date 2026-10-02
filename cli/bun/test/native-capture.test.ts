import {test,expect} from "bun:test";
import {mkdtempSync,readFileSync,statSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {NativeCapture} from "../src/adapters/native-capture";
test("capture is new private bounded JSON and redacts known strings",()=>{
 const dir=mkdtempSync(join(tmpdir(),"prose-capture-")),path=join(dir,"native.jsonl");
 const capture=new NativeCapture(path,["fixture-secret"],80);
 try{
 capture.write({type:"tool_call",value:"fixture-secret"});
 expect(JSON.parse(readFileSync(path,"utf8")).value).toBe("[REDACTED]");
 expect(statSync(path).mode&0o777).toBe(0o600);
 expect(()=>new NativeCapture(path,[])).toThrow();
 expect(()=>capture.write({payload:"x".repeat(80)})).toThrow();
 }finally{capture.close();rmSync(dir,{recursive:true});}
});

import fixture from "../../shared/fixtures/adapters/native-output.v1.json";
import { installedProcessFailureDetails } from "../src/cli";
import { failure } from "../src/core/errors";
import type { ProcessSupervisionResult } from "../src/supervision/types";

for (const item of fixture.captureCases) test(`capture diagnostic: ${item.name}`, () => {
  const dir = mkdtempSync(join(tmpdir(), "prose-capture-diagnostic-"));
  const path = join(dir, "native.jsonl");
  const capture = new NativeCapture(path, [item.secret], item.limitBytes);
  let caught: any;
  try {
    for (const record of item.records) capture.write(record);
  } catch (error) { caught = error; }
  finally { capture.close(); }
  try {
    expect(caught.code).toBe("HARNESS_FAILED");
    const details = installedProcessFailureDetails({error: caught, exitCode: null, signal: "SIGTERM", terminalEventObserved: false} as ProcessSupervisionResult, "codex/exec-json");
    expect(details.transportDiagnostic).toEqual({schema: "openprose.transport-diagnostic/1", reason: "native-capture-limit", observedBytes: item.observedBytes, limitBytes: item.limitBytes, saturated: false});
    expect(details).not.toHaveProperty("reason");
    const bytes = readFileSync(path);
    expect(bytes.length).toBeLessThanOrEqual(item.limitBytes);
    expect(bytes.toString()).not.toContain(item.secret);
    expect(bytes.toString().split("\n").filter(Boolean)).toHaveLength(item.retainedRecords);
  } finally { rmSync(dir, {recursive: true}); }
});

test("public failure details do not forward arbitrary observer reasons", () => {
  const error = failure("HARNESS_FAILED", {reason: "private arbitrary content"});
  const details = installedProcessFailureDetails({error, exitCode: 1, signal: null, terminalEventObserved: false} as ProcessSupervisionResult, "codex/exec-json");
  expect(JSON.stringify(details)).not.toContain("private arbitrary content");
});
