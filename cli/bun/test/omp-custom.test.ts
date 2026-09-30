import {test,expect} from 'bun:test';
import {installedProtocol} from '../src/adapters/protocols';
import fixture from '../../shared/fixtures/adapters/tool-lifecycle/omp-custom.json';
const run=(fs:any[])=>{const p=installedProtocol('omp/rpc','omp/18.0.9','fixture-tools',new Uint8Array(),true);fs.forEach(f=>p.accept(f));return p};
test('OMP custom input is paired history, not a terminal',()=>{expect(run(fixture.slice(0,-1)).terminalEventObserved).toBe(false);expect(run(fixture).terminalEventObserved).toBe(true)});
for(const content of ['opaque',[{type:'text',text:'opaque',textSignature:'sig'}],[{type:'image',data:'AA==',mimeType:'image/png',detail:'original',providerFile:{provider:'openai',id:'x'},url:'https://example.invalid/x'}]])test('custom typed content '+JSON.stringify(content),()=>{const f=structuredClone(fixture) as any[];for(const m of [f[9].message,f[10].message,f[14].messages[1]])m.content=content;expect(run(f).terminalEventObserved).toBe(true)});
for(const [name,change] of Object.entries({
 'changed end':(f:any[]):any=>f[10].message.content='changed',
 'changed history':(f:any[]):any=>f[14].messages[1].display=true,
 'unknown field':(f:any[]):any=>f[9].message.extra=true,
 'bad content':(f:any[]):any=>f[9].message.content=[{type:'toolCall',id:'x'}],
 'bad attribution':(f:any[]):any=>f[9].message.attribution='system',
 'bad timestamp':(f:any[]):any=>f[9].message.timestamp=-1,
 'no start':(f:any[]):any=>f.splice(9,1),
 'double start':(f:any[]):any=>f.splice(10,0,structuredClone(f[9])),
 'double end':(f:any[]):any=>f.splice(11,0,structuredClone(f[10])),
 'before turn':(f:any[]):any=>f.splice(6,0,structuredClone(f[9])),
 'after terminal':(f:any[]):any=>f.push(structuredClone(f[9])),
 'custom cannot complete':(f:any[]):any=>f.splice(11,3),
}))test('reject custom '+name,()=>{const f=structuredClone(fixture) as any[];change(f);expect(()=>run(f)).toThrow()});
import {NativeToolLifecycle} from '../src/adapters/native-tool-lifecycle';
import tools from '../../shared/fixtures/adapters/tool-lifecycle/omp.json';
test('custom does not grant Prime admission or bypass pending tools',()=>{
 const p=new NativeToolLifecycle(false);expect(()=>fixture.slice(5).forEach(f=>p.accept(f))).toThrow();
 const f=structuredClone(tools) as any[];const i=f.findIndex(x=>x.type==='tool_execution_start');f.splice(i,0,structuredClone(fixture[9]),structuredClone(fixture[10]));expect(()=>run(f)).toThrow();
});
test('opaque details and optional attribution',()=>{const f=structuredClone(fixture) as any[];for(const m of [f[9].message,f[10].message,f[14].messages[1]]){delete m.attribution;m.details={arbitrary:[1,null,true]};}expect(run(f).terminalEventObserved).toBe(true)});
