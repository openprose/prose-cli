import {test,expect} from "bun:test";
import {installedProtocol} from "../src/adapters/protocols";
import fixture from "../../shared/fixtures/adapters/tool-lifecycle/prime-drain.json";
const copy=<T>(v:T):T=>structuredClone(v);
const make=()=>installedProtocol("prime/rpc","0.7.0",fixture.invocationId,new TextEncoder().encode("PROMPT\n"),true);
const start=()=>{const p=make();expect(new TextDecoder().decode(p.takeStagedStdinBytes!()!)).toContain('"get_state"');p.accept(fixture.stateResponse);expect(new TextDecoder().decode(p.takeStagedStdinBytes!()!)).toBe("PROMPT\n");p.accept(fixture.promptResponse);expect(p.stdinCloseRequested).toBe(true);return p;};
test("Prime native segments require actual final stop, drained queue and process settlement",()=>{
 const p=start();for(const f of fixture.frames){p.accept(f);expect(p.terminalEventObserved).toBe(false);}expect(p.settleProcess!(0)).toEqual({type:"session.completed"});expect(p.terminalEventObserved).toBe(true);
 for(const count of [0,13,14,17,18]){const q=start();for(const f of fixture.frames.slice(0,count))q.accept(f);expect(()=>q.settleProcess!(0)).toThrow();}
 const q=start();for(const f of fixture.frames)q.accept(f);expect(()=>q.settleProcess!(1)).toThrow();
});
test("failed or missing identity never dispatches a prompt",()=>{
 for(const mutate of [(x:any)=>x.id="wrong",(x:any)=>x.command="prompt",(x:any)=>x.success=false,(x:any)=>x.data.sessionId="",(x:any)=>x.data.isStreaming=true,(x:any)=>x.data.messageCount=1]){
  const p=make();p.takeStagedStdinBytes!();const r=copy(fixture.stateResponse);mutate(r);expect(()=>p.accept(r)).toThrow();expect(p.takeStagedStdinBytes!()).toBeNull();
 }
 const p=make();p.takeStagedStdinBytes!();expect(()=>p.settleProcess!(0)).toThrow();expect(p.takeStagedStdinBytes!()).toBeNull();
});
test("snapshot-only context requires independent target, known child, exact queued body and history",()=>{
 for(const mutate of [(a:any[])=>a[17].messages[0].details.target.sessionId="other",(a:any[])=>a[17].messages[0].details.from.activeSessionId="other",(a:any[])=>a[17].messages[0].details.from.sessionName="other",(a:any[])=>a[11].actions.steering=["wrong"],(a:any[])=>a[17].messages[0].content+="changed",(a:any[])=>a[17].messages[1].content[0].text="changed",(a:any[])=>a[17].messages[0].extra=true,(a:any[])=>a[17].messages[0].timestamp="2",(a:any[])=>a[15].message.stopReason="aborted"]){
  const frames=copy(fixture.frames);mutate(frames);const p=start();expect(()=>{for(const f of frames)p.accept(f);p.settleProcess!(0);}).toThrow();
 }
});
test("active/new work, pending tools and post-terminal observations cannot manufacture completion",()=>{
 for(const suffix of [{type:"session_action_update",actions:{queuedCount:0,steering:[],followUps:[],active:{kind:"turn",phase:"running"}}},{type:"message_start",message:{role:"assistant",content:[]}},{type:"turn_start"}]){
  const p=start();for(const f of fixture.frames)p.accept(f);expect(()=>p.accept(suffix)).toThrow();
 }
 const p=start();for(const f of fixture.frames.slice(0,7))p.accept(f);expect(()=>p.accept(fixture.frames[13])).toThrow();
 const q=start();for(const f of fixture.frames)q.accept(f);q.settleProcess!(0);expect(()=>q.accept(fixture.frames.at(-1))).toThrow();
});
import {primeHistorySame,PrimeDrain} from "../src/adapters/prime-drain";
test("native history accounting projection is typed and never changes execution evidence",()=>{
 const usage={input:1,output:2,cacheRead:3,cacheWrite:0,totalTokens:6,cost:{input:0.1,output:0.2,cacheRead:0.03,cacheWrite:0,total:0.33}};
 const a={role:"assistant",content:[{type:"text",text:"same"}],stopReason:"stop",model:"fixture",usage};const b=copy(a);b.usage.input=200;expect(primeHistorySame(a,b)).toBe(true);
 for(const mutate of [(x:any)=>delete x.usage.input,(x:any)=>x.usage.extra=1,(x:any)=>x.usage.input="1",(x:any)=>x.usage.cost.total=-1,(x:any)=>x.content[0].text="different",(x:any)=>x.model="other",(x:any)=>delete x.usage]){const x=copy(b);mutate(x);expect(primeHistorySame(a,x)).toBe(false);}
 const p=new PrimeDrain(fixture.sessionId);p.resumed=true;p.child((fixture.frames[10] as any).child);p.queue((fixture.frames[11] as any).actions);const ms=(fixture.frames[17] as any).messages;expect(p.history(ms,ms.slice(1))).toBe(true);expect(p.history(ms,ms.slice(1))).toBe(false);
});

test("a prior segment empty queue cannot settle resumed work",()=>{const frames=copy(fixture.frames);const empty=frames.pop()!;frames.splice(13,0,empty);const p=start();for(const f of frames)p.accept(f);expect(()=>p.settleProcess!(0)).toThrow();});

import queuedFixture from "../../shared/fixtures/adapters/tool-lifecycle/prime-queued-continuation.json";
const queuedStart=()=>{
 const p=installedProtocol("prime/rpc","0.7.0",queuedFixture.invocationId,new TextEncoder().encode("PROMPT\n"),true);
 p.takeStagedStdinBytes!();p.accept(queuedFixture.stateResponse);p.takeStagedStdinBytes!();p.accept(queuedFixture.promptResponse);return p;
};
test("pre-observed queued child input requires a complete fresh continuation",()=>{
 const p=queuedStart();for(const frame of queuedFixture.frames){p.accept(frame);expect(p.terminalEventObserved).toBe(false);}
 expect(p.settleProcess!(0)).toEqual({type:"session.completed"});
 for(let count=0;count<queuedFixture.frames.length;count++){
  const q=queuedStart();expect(()=>{for(const f of queuedFixture.frames.slice(0,count))q.accept(f);q.settleProcess!(0);}).toThrow();
 }
 const q=queuedStart();for(const f of queuedFixture.frames)q.accept(f);expect(()=>q.settleProcess!(1)).toThrow();
});

const qi=queuedFixture.indexMap;
const idle={type:"session_action_update",actions:{queuedCount:0,steering:[],followUps:[]}};
const mutateCustom=(frames:any[],change:(m:any)=>void)=>{
 for(const index of [qi.customStart,qi.customEnd])change(frames[index].message);
 change(frames[qi.continuationStopEnd].messages[0]);
};
test("queued continuation rejects stale, uncorrelated, duplicated and incomplete work",()=>{
 const mutations:Array<[string,(frames:any[])=>void]>=[
  ["no preview",a=>{a.splice(qi.currentQueuedDelivery,1);}],
  ["removed preview",a=>{a.splice(qi.parentStopEnd,0,copy(idle));}],
  ["post-stop preview",a=>{const [q]=a.splice(qi.currentQueuedDelivery,1);a.splice(qi.parentStopEnd,0,q);}],
  ["wrong count",a=>{a[qi.currentQueuedDelivery].actions.queuedCount=0;}],
  ["multiple previews",a=>{a[qi.currentQueuedDelivery].actions.queuedCount=2;a[qi.currentQueuedDelivery].actions.steering.push("Agent message received: Other.");}],
  ["non-child preview",a=>{a[qi.currentQueuedDelivery].actions.steering=["Unrelated work"]; }],
  ["active pre-stop snapshot",a=>{a[qi.currentQueuedDelivery].actions.active={kind:"turn",phase:"running"};}],
  ["no known child",a=>{for(let i=a.length-1;i>=0;i--)if(a[i].type==="rlm_child_update")a.splice(i,1);}],
  ["wrong sender",a=>mutateCustom(a,m=>{m.details.from.activeSessionId="other-active";m.content=m.content.replaceAll("fixture-child-active","other-active");})],
  ["wrong target",a=>mutateCustom(a,m=>{m.details.target.sessionId="other-parent";m.content=m.content.replaceAll("fixture-parent-session","other-parent");})],
  ["altered body",a=>mutateCustom(a,m=>{m.details.message="Changed child body.";m.content=m.content.replace("Synthetic child result is available.","Changed child body.");})],
  ["altered format",a=>mutateCustom(a,m=>{m.content+=" extra";})],
  ["unknown custom field",a=>mutateCustom(a,m=>{m.extra=true;})],
  ["negative custom time",a=>mutateCustom(a,m=>{m.timestamp=-1;})],
  ["changed custom end",a=>{a[qi.customEnd].message.content+="changed";}],
  ["missing custom end",a=>{a.splice(qi.customEnd,1);}],
  ["assistant before delivery",a=>{a[qi.customStart]=copy(a[qi.continuationAssistantStart]);}],
  ["unrelated user input",a=>{a[qi.customStart]=copy(a[qi.userStart]);}],
  ["duplicate agent start",a=>{a.splice(qi.continuationAgentStart,0,copy(a[qi.continuationAgentStart]));}],
  ["duplicate turn start",a=>{a.splice(qi.continuationTurnStart,0,copy(a[qi.continuationTurnStart]));}],
  ["missing committing",a=>{a.splice(qi.committing,1);}],
  ["session command",a=>{a[qi.preparing].actions.active.kind="session_command";}],
  ["empty queue instead of delivery",a=>{a.splice(qi.preparing,a.length-qi.preparing,copy(idle));}],
  ["wrong earlier history",a=>{a[qi.parentStopEnd].messages[0].content[0].text="Changed";}],
  ["wrong later history",a=>{a[qi.continuationStopEnd].messages[1].content[0].text="Changed";}],
  ["invalid historical usage",a=>{a[qi.parentStopEnd].messages[1].usage.input="13";}],
  ["old idle cannot finish new segment",a=>{a.pop();a.splice(qi.continuationStopEnd,0,copy(idle));}],
  ["reused delivery",a=>{a.splice(qi.continuationStopEnd,0,copy(a[qi.currentQueuedDelivery]));}],
  ["new work after completed drain",a=>{a.push(copy(a[qi.preparing]));}],
  ["new queued work after completed drain",a=>{a.push(copy(a[qi.currentQueuedDelivery]),copy(idle));}],
  ["new queued work after admitted delivery",a=>{a.splice(qi.continuationAssistantStart,0,copy(a[qi.currentQueuedDelivery]),copy(idle));}],
  ["new preparing phase after admitted delivery",a=>{a.splice(qi.continuationAssistantStart,0,copy(a[qi.preparing]));}],
 ];
 for(const key of ["continuationAgentStart","continuationTurnStart","customStart","customEnd"] as const){
  mutations.push([`unknown outer field on ${key}`,a=>{a[qi[key]].extra=true;}]);
 }
 for(const [name,mutate] of mutations){
  const frames:any[]=copy(queuedFixture.frames);mutate(frames);const p=queuedStart();
  expect(()=>{for(const f of frames)p.accept(f);p.settleProcess!(0);},name).toThrow();
 }
});

test("active display labels do not supply queued-delivery identity",()=>{
 const frames:any[]=copy(queuedFixture.frames);
 for(const i of [qi.preparing,qi.committing,qi.running])frames[i].actions.active.label="Opaque display label";
 const p=queuedStart();for(const f of frames)p.accept(f);
 expect(p.settleProcess!(0)).toEqual({type:"session.completed"});
});

import markerFixture from "../../shared/fixtures/adapters/tool-lifecycle/prime-turn-transition.json";
test("queue disappearance without correlated delivery cannot settle a marker-loss trace",()=>{
 const p=installedProtocol("prime/rpc","0.7.0",markerFixture.invocationId,new TextEncoder().encode("PROMPT\n"),true);
 p.takeStagedStdinBytes!();p.accept(markerFixture.stateResponse);p.takeStagedStdinBytes!();p.accept(markerFixture.promptResponse);
 const frames:any[]=copy(markerFixture.frames);
 frames[11].actions={queuedCount:1,steering:["Agent message received: Child observation is available."],followUps:[]};
 expect(()=>{for(const f of frames)p.accept(f);p.settleProcess!(0);}).toThrow();
});

test("a stopped current parent action can drain without a queued child continuation",()=>{
 const frames:any[]=copy(queuedFixture.frames.slice(0,qi.parentStopEnd+1));
 frames[qi.currentQueuedDelivery].actions={queuedCount:0,steering:[],followUps:[],active:{kind:"turn",phase:"running"}};
 const p=queuedStart();for(const f of frames)p.accept(f);
 expect(()=>p.settleProcess!(0)).toThrow();
 p.accept(copy(idle));expect(p.settleProcess!(0)).toEqual({type:"session.completed"});
});
