import {test, expect} from "bun:test";
import {installedProtocol} from "../src/adapters/protocols";
import fixture from "../../shared/fixtures/adapters/tool-lifecycle/prime-omitted-result.json";
const copy = <T>(v:T):T => structuredClone(v);
const start = () => {
  const p=installedProtocol("prime/rpc","0.7.0",fixture.invocationId,new TextEncoder().encode("PROMPT\n"),true);
  p.takeStagedStdinBytes!(); p.accept(fixture.stateResponse); p.takeStagedStdinBytes!(); p.accept(fixture.promptResponse);
  return p;
};
const finish = (frames:any[], exit=0) => {
  const p=start(); for(const frame of frames)p.accept(frame);
  return p.settleProcess!(exit);
};
test("omitted result notifications require corroborating final history", () => {
  expect(finish(fixture.frames)).toEqual({type:"session.completed"});
  for(let n=0;n<=11;n++)expect(()=>finish(fixture.frames.slice(0,n))).toThrow();
  expect(()=>finish(fixture.frames,1)).toThrow();
});
test("corroboration rejects wrong identity, content, error, details and missing completion", () => {
  const mutations:Array<(frames:any[])=>void>=[
    f=>{f.splice(7,1);}, f=>{f.splice(6,1);},
    f=>{f[8].message.content=[{type:"text",text:"not empty"}];},
    f=>{f[11].messages.splice(2,1);},
    f=>{f[11].messages[2].toolCallId="other";},
    f=>{f[11].messages[2].toolName="other";},
    f=>{f[11].messages[2].content[0].text="changed";},
    f=>{f[11].messages[2].isError=true;},
    f=>{f[11].messages[2].details={invented:true};},
    f=>{f[11].messages[2].extra="unknown";},
    f=>{f[11].messages[2].timestamp=-1;},
    f=>{f[11].messages.push(copy(f[11].messages[2]));},
    f=>{f[11].messages[0].content[0].text="changed user";},
    f=>{f.splice(8,0,copy(f[7]));},
  ];
  for(const mutate of mutations){const frames:any[]=copy(fixture.frames);mutate(frames);expect(()=>finish(frames)).toThrow();}
});
test("matching detail and timestamp do not change the result's identity",()=>{
  const f:any[]=copy(fixture.frames);f[7].result.details={status:"ok"};f[11].messages[2].details={status:"ok"};f[11].messages[2].timestamp=123;
  expect(finish(f)).toEqual({type:"session.completed"});
});

test("multiple omissions in separate turns keep distinct corroboration positions",()=>{
  expect(finish(fixture.repeatedFrames)).toEqual({type:"session.completed"});
  const f:any[]=copy(fixture.repeatedFrames);f.at(-2).messages[4].content[0].text="value";
  expect(()=>finish(f)).toThrow();
});
