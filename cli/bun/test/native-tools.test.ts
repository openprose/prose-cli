import { test, expect } from "bun:test";
import { installedProtocol } from "../src/adapters/protocols";
import prime from "../../shared/fixtures/adapters/tool-lifecycle/prime.json";
import omp from "../../shared/fixtures/adapters/tool-lifecycle/omp.json";
for (const [id, frames] of [["prime/rpc",prime],["omp/rpc",omp]] as const) {
  const parser = () => installedProtocol(id,id==="prime/rpc"?"prime-agent 0.7.0":"omp/18.0.9","fixture-tools",new Uint8Array(),id==="omp/rpc");
  test(`${id} native tools settle only after actual terminal`,()=>{
    const p=parser();for(const f of frames.slice(0,frames.findIndex(f=>f.type==="agent_end"))) p.accept(f);
    expect(p.terminalEventObserved).toBe(false);p.accept(frames.find(f=>f.type==="agent_end"));expect(p.terminalEventObserved).toBe(true);
  });
  test(`${id} rejects unmatched tool completion`,()=>{
    const p=parser(), bad=structuredClone(frames) as any[];
    bad.find(f=>f.type==="tool_execution_end").toolCallId="wrong";
    expect(()=>bad.forEach(f=>p.accept(f))).toThrow();
  });
  test(`${id} rejects invented tool-result content`,()=>{
    const p=parser(), bad=structuredClone(frames) as any[];
    bad.find(f=>f.type==="message_end" && f.message?.role==="toolResult").message.content=[{type:"text",text:"invented"}];
    expect(()=>bad.forEach(f=>p.accept(f))).toThrow();
  });
  test(`${id} rejects terminal before tools settle`,()=>{
    const p=parser(), cut=frames.findIndex(f=>f.type==="tool_execution_start");
    expect(()=>[...frames.slice(0,cut+1),frames.find(f=>f.type==="agent_end")].forEach(f=>p.accept(f))).toThrow();
  });
}

test("OMP optional null omission preserves all non-null values",async()=>{
 const {nativeArgsMatch}=await import("../src/adapters/native-tool-lifecycle");
 const declared={op:"init",optional:null};
 expect(nativeArgsMatch({op:"init"},declared,true)).toBe(true);
 expect(nativeArgsMatch({op:"erase"},declared,true)).toBe(false);
 expect(nativeArgsMatch({op:"init",extra:"invented"},declared,true)).toBe(false);
 expect(nativeArgsMatch({op:"init"},declared,false)).toBe(false);
});

import implicit from "../../shared/fixtures/adapters/tool-lifecycle/prime-implicit-turn.json";
const primeParser=()=>installedProtocol("prime/rpc","prime-agent 0.7.0","fixture-tools",new Uint8Array());
test("Prime missing turn marker allows only a settled tool turn and real terminal",()=>{
 const p=primeParser();for(const f of implicit.slice(0,-1))p.accept(f);
 expect(p.terminalEventObserved).toBe(false);p.accept(implicit.at(-1));expect(p.terminalEventObserved).toBe(true);
 const boundary=implicit.findIndex((f,i)=>i>0&&f.type==="message_start"&&implicit[i-1]?.type==="turn_end");
 for(const message of [{role:"user",content:[]},{role:"assistant",content:[{type:"text",text:"unexpected"}]}]) {
  const bad=structuredClone(implicit) as any[];bad[boundary].message=message;const q=primeParser();expect(()=>bad.forEach(f=>q.accept(f))).toThrow();
  expect(q.diagnostic?.("prime-lifecycle")?.phase).toBe("tool-await-next-turn");
 }
 const incomplete=implicit.filter((_,i)=>i!==boundary-1),q=primeParser();expect(()=>incomplete.forEach(f=>q.accept(f))).toThrow();
 const strict=structuredClone(omp) as any[];const starts=strict.map((v,i)=>v.type==="turn_start"?i:-1).filter(i=>i>=0);strict.splice(starts[1]!,1);
 const o=installedProtocol("omp/rpc","omp/18.0.9","fixture-tools",new Uint8Array(),true);expect(()=>strict.forEach(f=>o.accept(f))).toThrow();
});

test.skipIf(!process.env.PRIME_REPLAY_PATH)("recorded Prime prefix remains incomplete, synthetic continuation needs actual terminal",async()=>{
 const frames=(await Bun.file(process.env.PRIME_REPLAY_PATH!).text()).trim().split("\n").map(l=>JSON.parse(l));
 const p=installedProtocol("prime/rpc","prime-agent 0.7.0",frames[0].id,new Uint8Array());frames.forEach(f=>p.accept(f));expect(p.terminalEventObserved).toBe(false);
 const message={...frames.at(-1).message,content:[{type:"text",text:"synthetic continuation"}],stopReason:"stop"};
 p.accept({type:"message_end",message});p.accept({type:"turn_end",message,toolResults:[]});expect(p.terminalEventObserved).toBe(false);
 p.accept({type:"agent_end",messages:[...frames.filter(f=>f.type==="message_end").map(f=>f.message),message]});expect(p.terminalEventObserved).toBe(true);
});
