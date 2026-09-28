import { expect, test } from "bun:test";
import { installedProtocol } from "../src/adapters/protocols";
import telemetry from "../../shared/fixtures/adapters/claude-thinking-tokens.json";
function ready() {
  const p = installedProtocol("claude/print-stream-json", "2.1.243", "fixture");
  p.accept({type: "system", subtype: "init", session_id: "fixture-session"});
  return p;
}
test("Claude telemetry is information, not completion", () => {
  const p = ready();
  expect(p.accept(telemetry)).toBeNull();
  expect(p.accept({type:"result", subtype:"success", is_error:false, session_id:"fixture-session"})).not.toBeNull();
});
test("Claude telemetry requires the active session and bounded valid fields", () => {
  for (const mutation of [{session_id:"other"}, {estimated_tokens:-1}, {estimated_tokens_delta:0.5}, {uuid:""}, {unexpected:true}]) {
    expect(() => ready().accept({...telemetry,...mutation})).toThrow();
  }
  const fresh = installedProtocol("claude/print-stream-json", "2.1.243", "fixture");
  expect(() => fresh.accept(telemetry)).toThrow();
});
test("permission denial is nonterminal and session-bound",()=>{
 const p=ready();
 const denial={type:"system",subtype:"permission_denied",session_id:"fixture-session",tool_name:"Bash",tool_use_id:"tool-1",message:"Denied"};
 expect(p.accept(denial)).toBeNull();expect(p.terminalEventObserved).toBe(false);
 expect(()=>ready().accept({...denial,session_id:"other"})).toThrow();
 expect(p.accept({type:"result",subtype:"success",is_error:false,session_id:"fixture-session"})).not.toBeNull();
});

import tasks from "../../shared/fixtures/adapters/claude-task-lifecycle.json";
test("native task lifecycle remains session-bound nonterminal telemetry",()=>{
 const p=ready();for(const task of tasks){expect(p.accept(task)).toBeNull();expect(p.terminalEventObserved).toBe(false);}
 expect(p.accept({type:"result",subtype:"success",is_error:false,session_id:"fixture-session"})).not.toBeNull();
 for(const task of tasks){expect(()=>ready().accept({...task,session_id:"other"})).toThrow();expect(()=>ready().accept({...task,task_id:""})).toThrow();}
 expect(()=>ready().accept({...tasks.find(t=>t.subtype==="task_progress"),usage:{total_tokens:-1,tool_uses:1,duration_ms:1}})).toThrow();
 expect(ready().accept({...tasks[0],subtype:"task_invented"})).toBeNull();
 expect(()=>ready().accept({...tasks[0],subtype:"task_invented",session_id:"other"})).toThrow();
});
import background from "../../shared/fixtures/adapters/claude-background-tasks.json";
test("native background inventory is validated nonterminal telemetry",()=>{
 const p=ready();expect(p.accept(background)).toBeNull();expect(p.terminalEventObserved).toBe(false);
 expect(()=>ready().accept({...background,tasks:[{}]})).toThrow();expect(()=>ready().accept({...background,session_id:"other"})).toThrow();
 expect(p.accept({...background,tasks:[]})).toBeNull();
});
test("identical init after native task notification resumes existing session only",()=>{
 const p=ready();p.accept(tasks.find(t=>t.subtype==="task_notification"));
 expect(p.accept({type:"system",subtype:"init",session_id:"fixture-session",uuid:"resumed"})).toBeNull();expect(p.terminalEventObserved).toBe(false);
 expect(()=>ready().accept({type:"system",subtype:"init",session_id:"fixture-session",uuid:"duplicate"})).toThrow();
 const q=ready();q.accept(tasks.find(t=>t.subtype==="task_notification"));expect(()=>q.accept({type:"system",subtype:"init",session_id:"fixture-session",uuid:"resumed",cwd:"changed"})).toThrow();
});

import turns from "../../shared/fixtures/adapters/claude-native-turns.json";
function native(){return installedProtocol("claude/print-stream-json","2.1.243","fixture",null,true);}
test("native Claude turn results defer completion until process settlement; legacy stays strict",()=>{
 const p=native();for(const r of turns)p.accept(r);
 expect(p.terminalEventObserved).toBe(false);expect(p.stdinCloseRequested).toBe(false);
 expect(p.settleProcess?.(0)?.type).toBe("session.completed");expect(p.terminalEventObserved).toBe(true);
 const old=installedProtocol("claude/print-stream-json","2.1.243","fixture");expect(()=>turns.forEach(r=>old.accept(r))).toThrow();
});
test("native Claude requires fresh successful candidate and matching session",()=>{
 const activity={type:"assistant",session_id:"fixture-session",message:{role:"assistant",content:[{type:"text",text:"more activity"}]}};
 const p=native();turns.forEach(r=>p.accept(r));p.accept(activity);expect(()=>p.settleProcess?.(0)).toThrow();expect(p.terminalEventObserved).toBe(false);
 p.accept(turns[2]);expect(p.settleProcess?.(0)?.type).toBe("session.completed");
 for(const exit of [1,143,null]){const q=native();turns.forEach(r=>q.accept(r));expect(()=>q.settleProcess?.(exit)).toThrow();expect(q.terminalEventObserved).toBe(false);}
 const q=native();q.accept(turns[0]);expect(()=>q.settleProcess?.(0)).toThrow();
 expect(()=>q.accept({...turns[1],is_error:true,subtype:"error"})).toThrow();
 expect(()=>q.accept({...turns[1],session_id:"other"})).toThrow();
 expect(q.accept({type:"rate_limit_event",session_id:"fixture-session",uuid:"u"})).toBeNull();
 expect(()=>q.accept({type:"rate_limit_event",session_id:"other"})).toThrow();
 expect(()=>q.accept({type:"",session_id:"fixture-session"})).toThrow();
});
test.skipIf(!process.env.CLAUDE_REPLAY_PATH)("recorded native Claude stream remains provisional until exit",async()=>{
 const records=(await Bun.file(process.env.CLAUDE_REPLAY_PATH!).text()).trim().split("\n").map(l=>JSON.parse(l));const p=native();records.forEach(r=>p.accept(r));expect(p.terminalEventObserved).toBe(false);if(process.env.CLAUDE_REPLAY_EXPECT_COMPLETE==="true" || records.at(-1).type==="result")expect(p.settleProcess?.(0)?.type).toBe("session.completed");else expect(()=>p.settleProcess?.(0)).toThrow();
});

test("native repeated init requires exact metadata and invalidates prior result",()=>{
 const init={...turns[0],uuid:"initial",tools:["Read"],model:"fixture-model",apiKeySource:"fixture-auth"};
 const p=native();p.accept(init);p.accept(turns[1]);p.accept({...init,uuid:"resumed"});
 expect(()=>p.settleProcess?.(0)).toThrow();p.accept(turns[2]);expect(p.settleProcess?.(0)?.type).toBe("session.completed");
 for(const mutation of [{uuid:""},{tools:["Write"]},{model:"other"},{apiKeySource:"other"},{session_id:"other"}]){
  const q=native();q.accept(init);q.accept(turns[1]);expect(()=>q.accept({...init,uuid:"resumed",...mutation})).toThrow();
 }
 const old=installedProtocol("claude/print-stream-json","2.1.243","fixture");old.accept(init);expect(()=>old.accept({...init,uuid:"resumed"})).toThrow();
});

test("native routing metadata is typed mutable metadata only",()=>{
 for(const initial of [undefined,"/tmp/old"]){for(const next of [undefined,"/tmp/new"]){
  const p=native();p.accept({...turns[0],uuid:"first",...(initial?{messaging_socket_path:initial}:{})});p.accept(turns[1]);
  p.accept({...turns[0],uuid:"next",...(next?{messaging_socket_path:next}:{})});expect(()=>p.settleProcess?.(0)).toThrow();p.accept(turns[2]);expect(p.settleProcess?.(0)?.type).toBe("session.completed");
 }}
 for(const bad of [null,0,{},""]){const p=native();expect(()=>p.accept({...turns[0],messaging_socket_path:bad})).toThrow();const q=native();q.accept(turns[0]);expect(()=>q.accept({...turns[0],uuid:"next",messaging_socket_path:bad})).toThrow();}
});

import shutdown from "../../shared/fixtures/adapters/claude-shutdown.json";
test("native correlated shutdown closes bookkeeping without manufacturing result",()=>{
 const p=native();shutdown.forEach(r=>p.accept(r));expect(p.terminalEventObserved).toBe(false);expect(p.settleProcess?.(0)?.type).toBe("session.completed");
 const old=installedProtocol("claude/print-stream-json","2.1.243","fixture");expect(()=>shutdown.forEach(r=>old.accept(r))).toThrow();
 for(const mutate of [
  (r:any[])=>r.push({type:"assistant",session_id:"fixture-session",message:{role:"assistant",content:[{type:"text",text:"new work"}]}}),
  (r:any[])=>r.push({type:"tool_progress",session_id:"fixture-session"}),
  (r:any[])=>r.at(-1).task_id="other",
  (r:any[])=>r.at(-1).tool_use_id="other",
  (r:any[])=>r.at(-1).session_id="other",
  (r:any[])=>r.pop(),
  (r:any[])=>r[2].task_type="agent",
  (r:any[])=>r[5].tasks=[{task_id:"new",description:"new",task_type:"local_bash"}],
  (r:any[])=>r[6].patch.end_time=-1,
 ]){const records=structuredClone(shutdown) as any[];mutate(records);const q=native();expect(()=>{records.forEach(r=>q.accept(r));q.settleProcess?.(0);}).toThrow();expect(q.terminalEventObserved).toBe(false);}
 const q=native();shutdown.forEach(r=>q.accept(r));expect(()=>q.settleProcess?.(1)).toThrow();
});
