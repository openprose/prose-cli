import {test,expect} from 'bun:test';
import {installedProtocol} from '../src/adapters/protocols';
import {taskArgsMatch} from '../src/adapters/native-tool-lifecycle';
import cases from '../../shared/fixtures/adapters/tool-lifecycle/omp-task-defaults.json';
for(const c of cases)test(`OMP task default ${c.name}`,()=>{const p=installedProtocol('omp/rpc','omp/18.0.9','fixture-tools',new Uint8Array(),true);let failed=false;try{for(const r of c.records)p.accept(r)}catch{failed=true}expect(!failed&&p.terminalEventObserved).toBe(c.expected===0)});
test('Prime never adopts OMP defaults',()=>expect(taskArgsMatch({agent:'task'},{},false,'task',{root:true,items:true})).toBe(false));
