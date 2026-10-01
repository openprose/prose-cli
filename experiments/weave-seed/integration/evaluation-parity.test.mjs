/** Both independent runtime hosts call the actual evaluator through a process boundary. */
import assert from 'node:assert/strict';
import {mkdtempSync,realpathSync,writeFileSync,readFileSync,mkdirSync,rmSync,readdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
const seed=fileURLToPath(new URL('../',import.meta.url));
const rust=process.argv[2];if(!rust)throw Error('explicit Rust local binary required');
const corpus=JSON.parse(readFileSync(join(seed,'fixtures/evaluation-providers.json')));
const encode=JSON.stringify;
let runs=0;
for(const runtime of ['bun','rust'])for(const c of corpus.composition){
  const root=realpathSync(mkdtempSync(join(tmpdir(),'evaluation-parity-')));
  try{
    for(const [name,text] of Object.entries({'kernel.md':'Synthetic transport kernel.','contract.md':'Satisfy both declared contract instances.','result.json':encode(c.statuses)}))writeFileSync(join(root,name),text);
    mkdirSync(join(root,'receipts'),{mode:0o700});
    const questions={assessment:{instructions:'Assess this contract instance.',criteria:{satisfied:'Established',violated:'Known unmet',unknown:'Unresolved'},outcomes:{satisfied:'satisfied',violated:'violated',unknown:'unknown'}}};
    const plan={schema:'openprose.evaluation-plan/1',composition:'all-required',evaluations:[0,1].map(i=>({id:'evaluation'+i,subject:{instance:'instance'+i,contracts:[join(root,'contract.md')],bindings:''},profile:'primary',questions,policy:{id:'labels-1',kind:'labels'}}))};
    const config={schema:'openprose.evaluator-process/1',planFile:'plan.json',profiles:{primary:{adapter:'openai-responses',model:'synthetic',acceptedModels:['synthetic'],apiKeyEnv:'TEST_KEY',options:{reasoningEffort:'none'}}},limits:{timeoutMs:1000,totalTimeoutMs:3000,maxCalls:2,maxInputBytes:1048576,maxRequestBytes:65536,maxResponseBytes:65536,maxInputTokens:1000,maxOutputTokens:100},receiptDirectory:join(root,'receipts')};
    writeFileSync(join(root,'plan.json'),encode(plan));writeFileSync(join(root,'evaluator.json'),encode(config));
    const evaluator=join(root,'evaluator.mjs');
    writeFileSync(evaluator,`import {assess,loadConfiguration} from ${encode(pathToFileURL(join(seed,'providers/evaluation/run.mjs')).href)};
import {appendFileSync} from 'node:fs';
let raw='';for await(const p of process.stdin)raw+=p;
const result=await assess(Buffer.from(raw),loadConfiguration(${encode(join(root,'evaluator.json'))}),{environment:{TEST_KEY:'fake-key-for-offline-testing'},fetchImpl:async(_url,init)=>{
const context=JSON.parse(JSON.parse(init.body).input).state;
const statuses=JSON.parse(context.source_evidence.find(f=>f.path.endsWith('/result.json')).content);
const index=Number(context.evaluation_subject.instance.slice(-1));
appendFileSync(${encode(join(root,'calls.txt'))},'evaluation'+index+'\\n');
return new Response(JSON.stringify({model:'synthetic',status:'completed',output:[{type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:JSON.stringify({assessment:statuses[index]})}]}],usage:{input_tokens:100,output_tokens:10}}),{headers:{'content-type':'application/json'}});
}});console.log(JSON.stringify(result));`);
    const executor=join(root,'executor.mjs');
    writeFileSync(executor,`import {writeFileSync,appendFileSync} from 'node:fs';let raw='';for await(const p of process.stdin)raw+=p;const e=JSON.parse(raw);if(!e.attempt)throw Error('missing attempt');appendFileSync(${encode(join(root,'execution.txt'))},'execution\\n');writeFileSync(${encode(join(root,'result.json'))},JSON.stringify(['satisfied','satisfied']));`);
    const host={schema:1,root,kernel:'kernel.md',contracts:['contract.md'],evidence:['result.json','evaluator.json','plan.json'],capabilityVersion:'evaluation-parity-v1',assessor:[process.execPath,'--no-env-file',evaluator],actor:[process.execPath,'--no-env-file',executor],environmentKeys:[],checkpointDirectory:'host',maxAttempts:1,ttlMs:60000,timeoutMs:10000};
    writeFileSync(join(root,'host.json'),encode(host));
    const args=runtime==='rust'?['step',join(root,'host.json')]:['--no-env-file',join(seed,'local/run.mjs'),'step',join(root,'host.json')];
    const r=spawnSync(runtime==='rust'?rust:process.execPath,args,{env:{},encoding:'utf8',timeout:15000,maxBuffer:1048576});
    assert.ifError(r.error);assert.equal(r.status,0,r.stderr);
    const result=JSON.parse(r.stdout);
    assert.equal(result.status,c.judgment==='unknown'?'unknown':'satisfied',runtime+': '+c.name);
    assert.equal(result.attempts,c.judgment==='work-needed'?1:0);
    const records=readdirSync(join(root,'receipts')).map(p=>JSON.parse(readFileSync(join(root,'receipts',p))));
    const first=records.find(x=>x.evaluations[0].answers.assessment.choice===c.statuses[0]&&x.evaluations[1].answers.assessment.choice===c.statuses[1]);
    assert.ok(first);assert.equal(first.assessment,c.assessment);assert.equal(first.judgment,c.judgment);
    assert.equal(readFileSync(join(root,'calls.txt'),'utf8').trim().split('\n').length,c.judgment==='work-needed'?4:2);
    runs++;
  }finally{rmSync(root,{recursive:true,force:true});}
}
console.log(`PASS ${runs} Bun/Rust evaluator process sequences; all-required composition, uncertainty, execution and reassessment; zero model calls`);
