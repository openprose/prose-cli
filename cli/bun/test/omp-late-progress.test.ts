import { test,expect } from "bun:test";
import {installedProtocol} from "../src/adapters/protocols";
import frames from "../../shared/fixtures/adapters/tool-lifecycle/omp-late-progress.json";
const parse=()=>installedProtocol("omp/rpc","18.0.9","fixture-tools",new Uint8Array(),true);
const at=frames.findIndex((x:any,i)=>x.type==="tool_execution_update"&&frames[i-1]?.type==="tool_execution_end");
test("OMP async task progress does not alter tool history or settle parent",()=>{const p=parse();for(const x of frames.slice(0,-1))p.accept(x);expect(p.terminalEventObserved).toBe(false);p.accept(frames.at(-1));expect(p.terminalEventObserved).toBe(true);});
test("OMP late progress rejects unmatched admission and ordering",()=>{for(const mutate of [
 (f:any[])=>{f[at].partialResult.details.async.jobId="other"},
 (f:any[])=>{f[at].args={different:true}},
 (f:any[])=>{delete f[at-1].result.details.async},
 (f:any[])=>{f[at].toolName="read"},
 (f:any[])=>{f.splice(at,0,structuredClone(f[at-1]))},
 (f:any[])=>{f.push(f[at])},
]){const f=structuredClone(frames);mutate(f);const p=parse();expect(()=>f.forEach(x=>p.accept(x))).toThrow();}});
