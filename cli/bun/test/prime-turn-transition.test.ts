import {test,expect} from "bun:test";
import {installedProtocol} from "../src/adapters/protocols";
import {NativeToolLifecycle} from "../src/adapters/native-tool-lifecycle";
import fixture from "../../shared/fixtures/adapters/tool-lifecycle/prime-turn-transition.json";
const make=()=>{const p=installedProtocol("prime/rpc","0.7.0",fixture.invocationId,new Uint8Array([10]),true);p.takeStagedStdinBytes!();p.accept(fixture.stateResponse);p.takeStagedStdinBytes!();p.accept(fixture.promptResponse);return p;};
test("missing both turn markers admits only completed tools and never completes a prefix",()=>{
 const p=make();for(const r of fixture.frames)p.accept(r);expect(p.terminalEventObserved).toBe(false);p.settleProcess!(0);expect(p.terminalEventObserved).toBe(true);
 const q=make();for(const r of fixture.frames.slice(0,13))q.accept(r);expect(()=>q.settleProcess!(0)).toThrow();
 for(const omp of [false,true]){const strict=new NativeToolLifecycle(omp);for(const r of fixture.frames.slice(0,10))strict.accept(r);expect(()=>strict.accept(fixture.frames[12])).toThrow();}
});
test("inferred transition rejects pending, corrupted, nonempty and incomplete sequences",()=>{
 for(const edit of [(a:any[])=>a.splice(7,1),(a:any[])=>{a.splice(9,1);a[14].messages[2].content=[{type:"text",text:"uncorroborated"}];},(a:any[])=>a.splice(10,0,a[9]),(a:any[])=>a[9].message.toolCallId="other",(a:any[])=>a[12].message.content=[{type:"text",text:"not empty"}],(a:any[])=>a[12].message.role="user",(a:any[])=>a[5].message.stopReason="stop",(a:any[])=>a[15].messages[1].content[0].arguments.path="changed",(a:any[])=>a.pop(),(a:any[])=>a.splice(14,1)]){
  const a=structuredClone(fixture.frames);edit(a);const p=make();expect(()=>{for(const r of a)p.accept(r);p.settleProcess!(0);}).toThrow();
 }
});
