/** Hermetic provider conformance. No network, ambient keys or customer data. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,realpathSync,writeFileSync,readFileSync,readdirSync,rmSync,statSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {adapters,aggregate,parseResponse,applyPolicy} from './adapters.mjs';
import {loadConfiguration,prepare,assess} from './run.mjs';
const here=fileURLToPath(new URL('.',import.meta.url));
const corpus=JSON.parse(readFileSync(new URL('../../fixtures/evaluation-providers.json',import.meta.url)));
const sha=v=>createHash('sha256').update(v).digest('hex');
const key='offline-fake-credential-never-a-real-key';
const question={instructions:'Is the required report present?',criteria:{yes:'Established present',no:'Established absent',unobserved:'Evidence is insufficient'},outcomes:{yes:'satisfied',no:'violated',unobserved:'unknown'}};
export function fixture(adapter='openai-responses') {
  const dir=realpathSync(mkdtempSync(join(tmpdir(),'evaluation-test-'))),contract=join(dir,'contract.md');
  const config={schema:'openprose.evaluator-process/1',planFile:'plan.json',profiles:{primary:{adapter,model:'test-model',acceptedModels:['test-model'],apiKeyEnv:'TEST_PROVIDER_KEY',options:adapter==='openai-responses'?{reasoningEffort:'none'}:{}}},limits:{timeoutMs:1000,totalTimeoutMs:3000,maxCalls:4,maxInputBytes:1048576,maxRequestBytes:65536,maxResponseBytes:65536,maxInputTokens:4096,maxOutputTokens:1024},receiptDirectory:dir};
  const plan={schema:'openprose.evaluation-plan/1',composition:'all-required',evaluations:[{id:'report',subject:{instance:'report-1',contracts:[contract],bindings:'first binding'},profile:'primary',questions:{assessment:structuredClone(question)},policy:{id:'explicit-label-policy',kind:'labels'}}]};
  const save=()=>{writeFileSync(join(dir,'config.json'),JSON.stringify(config));writeFileSync(join(dir,'plan.json'),JSON.stringify(plan));};save();
  const input=(patch={},change=x=>x)=>{
    const files=[{role:'kernel',path:join(dir,'kernel.md'),content:'Synthetic kernel.'},{role:'contract',path:contract,content:'Produce the report.'},{role:'evidence',path:join(dir,'result.md'),content:'Result includes the report.'},...['config.json','plan.json'].map(n=>({role:'evidence',path:join(dir,n),content:readFileSync(join(dir,n),'utf8')}))].map(s=>({...s,sha256:sha(s.content)}));
    const payload=JSON.stringify({version:1,policy:'all-required-explicit-v1',files:change(files)});
    return Buffer.from(JSON.stringify({schema:'openprose.weave-input/1',attempt:null,evidence:{identity:sha(payload),payload,observedAt:1000,validUntil:5000,gap:false,...patch}}));
  };
  return {dir,config,plan,save,input,load:()=>loadConfiguration(join(dir,'config.json')),close:()=>rmSync(dir,{recursive:true,force:true})};
}
export function response(adapter,answers={assessment:'yes'}) {
  const r={model:'test-model',id:'synthetic-id',usage:{input_tokens:100,output_tokens:10}};
  if(adapters[adapter].nativeProbabilities)r.answers=Object.fromEntries(Object.entries(answers).map(([id,choice])=>[id,{type:'choice',choice,confidence:.96,probabilities:Object.fromEntries(['yes','no','unobserved'].map(k=>[k,k===choice?.98:.01]))}]));
  else if(adapter==='openai-responses')Object.assign(r,{status:'completed',output:[{type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:JSON.stringify(answers)}]}]});
  else Object.assign(r,{type:'message',role:'assistant',stop_reason:'end_turn',content:[{type:'text',text:JSON.stringify(answers)}]});
  return r;
}
const http=v=>new Response(JSON.stringify(v),{headers:{'content-type':'application/json'}});
const options=fetchImpl=>({environment:{TEST_PROVIDER_KEY:key},now:()=>1500,fetchImpl});
const receipts=f=>readdirSync(f.dir).filter(p=>/^[\da-f-]{36}\.json$/.test(p)).map(p=>JSON.parse(readFileSync(join(f.dir,p))));

test('shared aggregation corpus preserves nonfulfillment separately from action eligibility',()=>{
  for(const c of corpus.composition)assert.deepEqual(aggregate(c.statuses),{assessment:c.assessment,judgment:c.judgment},c.name);
  assert.throws(()=>aggregate([]));
});
test('all four native adapters transmit explicit questions and authenticate correctly',async()=>{
  for(const c of corpus.adapters){const f=fixture(c.id);try{
    let calls=0;
    assert.deepEqual(await assess(f.input(),f.load(),options(async(url,init)=>{
      calls++;assert.equal(url,c.endpoint);assert.equal(init.redirect,'error');assert.ok(!init.body.includes(key));
      assert.equal(init.headers[c.id==='anthropic-messages'?'x-api-key':'Authorization'],c.id==='anthropic-messages'?key:'Bearer '+key);
      const request=JSON.parse(init.body);assert.equal(request.model,'test-model');
      if(c.nativeProbabilities)assert.equal(request.questions.assessment.instructions,question.instructions);
      else assert.ok(init.body.includes('Established present'));
      return http(response(c.id));
    })),{judgment:'satisfied'});
    assert.equal(calls,1);const r=receipts(f)[0];assert.equal(r.evaluations[0].assessment,'satisfied');
    assert.equal(Object.hasOwn(r.evaluations[0].answers.assessment,'distribution'),c.nativeProbabilities);
    assert.ok(!JSON.stringify(r).includes(key));assert.ok(!JSON.stringify(r).includes('Result includes the report.'));
    assert.equal(statSync(join(f.dir,r.id+'.json')).mode&0o777,0o600);
  }finally{f.close();}}
});
test('missing or changed binding and uncovered contracts prevent calls',async()=>{
  const f=fixture();try{
    const loaded=f.load(),raw=f.input();
    assert.throws(()=>prepare(f.input({},xs=>xs.filter(x=>!x.path.endsWith('plan.json'))),loaded,1500));
    const extra={role:'contract',path:join(f.dir,'other.md'),content:'Other requirement'};extra.sha256=sha(extra.content);
    assert.throws(()=>prepare(f.input({},xs=>[...xs,extra]),loaded,1500));
    writeFileSync(join(f.dir,'plan.json'),readFileSync(join(f.dir,'plan.json'),'utf8')+'\n');
    await assert.rejects(assess(raw,loaded,options(()=>assert.fail('HTTP must not run'))));
  }finally{f.close();}
});
test('composition calls distinct contract instances and retains unresolved questions',async()=>{
  const f=fixture();try{
    f.plan.evaluations.push({...structuredClone(f.plan.evaluations[0]),id:'review',subject:{...f.plan.evaluations[0].subject,instance:'report-review',bindings:'second binding'}});f.save();
    let calls=0;
    assert.deepEqual(await assess(f.input(),f.load(),options(async()=>http(response('openai-responses',{assessment:++calls===1?'no':'unobserved'})))),{judgment:'unknown'});
    const r=receipts(f)[0];assert.equal(calls,2);assert.equal(r.assessment,'violated');assert.deepEqual(r.evaluations.map(e=>e.subject.instance),['report-1','report-review']);
    assert.notEqual(r.evaluations[0].subject.bindingsSha256,r.evaluations[1].subject.bindingsSha256);
  }finally{f.close();}
});
test('named questions have exhaustive choice mappings and native policy requires capability',()=>{
  const f=fixture();try{
    f.plan.evaluations[0].questions.review=structuredClone(question);f.save();
    const l=f.load(),q=l.plan.evaluations[0].questions;
    const parsed=parseResponse(Buffer.from(JSON.stringify(response('openai-responses',{assessment:'yes',review:'unobserved'}))),l.config.profiles.primary,q,l.config.limits);
    assert.equal(applyPolicy(parsed,q,{kind:'labels'}).judgment,'unknown');
    delete f.plan.evaluations[0].questions.review.outcomes.no;f.save();assert.throws(f.load);
    f.plan.evaluations[0].questions.review=structuredClone(question);
    f.plan.evaluations[0].policy={id:'p',kind:'native-threshold',minProbability:.9,minConfidence:.8,minMargin:.5};f.save();assert.throws(f.load);
  }finally{f.close();}
});
test('native confidence and margins are optional capabilities, not fabricated label confidence',()=>{
  const f=fixture('openrouter-decisions');try{
    const e=f.plan.evaluations[0];e.policy={id:'p',kind:'native-threshold',minProbability:.9,minConfidence:.8,minMargin:.5};f.save();const l=f.load();
    let r=response('openrouter-decisions');let p=parseResponse(Buffer.from(JSON.stringify(r)),l.config.profiles.primary,e.questions,l.config.limits);
    assert.equal(applyPolicy(p,e.questions,e.policy).judgment,'satisfied');
    r.answers.assessment.confidence=.2;p=parseResponse(Buffer.from(JSON.stringify(r)),l.config.profiles.primary,e.questions,l.config.limits);
    assert.equal(applyPolicy(p,e.questions,e.policy).judgment,'unknown');
  }finally{f.close();}
});
test('strict native bytes, refusal, incomplete output, models, choices and usage are validated',()=>{
  for(const adapter of Object.keys(adapters)){const f=fixture(adapter);try{
    const l=f.load(),p=l.config.profiles.primary,q=l.plan.evaluations[0].questions;
    const parse=r=>parseResponse(Buffer.from(typeof r==='string'?r:JSON.stringify(r)),p,q,l.config.limits);
    for(const mutate of [r=>r.model='other',r=>delete r.usage,r=>r.usage.output_tokens=2000,r=>r.usage.input_tokens=true]){const r=response(adapter);mutate(r);assert.throws(()=>parse(r));}
    assert.throws(()=>parse(JSON.stringify(response(adapter)).replace('"model":"test-model"','"model":"test-model","model":"test-model"')));
    assert.throws(()=>parse(response(adapter,{extra:'yes'})));
    assert.throws(()=>parse(response(adapter,{assessment:'not-a-choice'})));
    const r=response(adapter);
    if(adapter==='openai-responses'){r.status='incomplete';assert.throws(()=>parse(r));r.status='completed';r.output[0].content[0]={type:'refusal',refusal:'No'};assert.throws(()=>parse(r));}
    else if(adapter==='anthropic-messages'){r.stop_reason='max_tokens';assert.throws(()=>parse(r));}
    else{r.answers.assessment.probabilities.yes=.5;assert.throws(()=>parse(r));}
  }finally{f.close();}}
});
test('preflight every credential, bound timeout, gap and expiry prevent unintended continuation',async()=>{
  const f=fixture();try{
    for(const patch of [{gap:true},{observedAt:2000},{validUntil:1500}])assert.deepEqual(await assess(f.input(patch),f.load(),{environment:{},now:()=>1500,fetchImpl:()=>assert.fail('HTTP')}),{judgment:'unknown'});
    f.config.profiles.second={...f.config.profiles.primary,apiKeyEnv:'ABSENT_KEY'};
    f.plan.evaluations.push({...structuredClone(f.plan.evaluations[0]),id:'second',profile:'second'});f.save();
    await assert.rejects(assess(f.input(),f.load(),options(()=>assert.fail('first call before complete preflight'))));
    f.plan.evaluations.pop();f.config.limits.timeoutMs=10;f.save();
    await assert.rejects(assess(f.input(),f.load(),options(()=>new Promise(()=>{}))),/^Error: EVALUATION_FAILED$/);
    let now=1500;
    assert.deepEqual(await assess(f.input(),f.load(),{...options(async()=>{now=5000;return http(response('openai-responses'));}),now:()=>now}),{judgment:'unknown'});
    let elapsed=0;
    await assert.rejects(assess(f.input(),f.load(),{...options(async()=>{elapsed=4000;return http(response('openai-responses'));}),monotonic:()=>elapsed}));
  }finally{f.close();}
});
test('later provider failure retains earlier completed assessment without retry',async()=>{
  const f=fixture();try{
    f.plan.evaluations.push({...structuredClone(f.plan.evaluations[0]),id:'second'});f.save();let calls=0;
    await assert.rejects(assess(f.input(),f.load(),options(async()=>++calls===1?http(response('openai-responses')):new Response('private upstream body',{status:429}))));
    assert.equal(calls,2);const r=receipts(f)[0];assert.equal(r.evaluations[0].assessment,'satisfied');assert.equal(r.evaluations[1].providerCalled,true);assert.equal(r.error,'EVALUATION_FAILED');assert.ok(!JSON.stringify(r).includes('private upstream body'));
  }finally{f.close();}
});
test('process stdout remains compatible and errors expose no source data',()=>{
  const f=fixture();try{
    const argv=[join(here,'run.mjs'),'--config',join(f.dir,'config.json')];
    const checked=spawnSync(process.execPath,[join(here,'run.mjs'),'--check',join(f.dir,'config.json')],{env:{},encoding:'utf8',timeout:5000});
    assert.equal(checked.status,0);assert.equal(JSON.parse(checked.stdout).providerCalled,false);
    const capabilities=spawnSync(process.execPath,[join(here,'run.mjs'),'--capabilities'],{env:{},encoding:'utf8',timeout:5000});
    assert.equal(capabilities.status,0);assert.match(JSON.parse(capabilities.stdout).capabilityIdentity,/^evaluation-process-1-[a-f0-9]{64}$/);
    const good=spawnSync(process.execPath,argv,{input:f.input({gap:true}),env:{},encoding:'utf8',timeout:5000});
    assert.equal(good.status,0,good.stderr);assert.equal(good.stdout,'{"judgment":"unknown"}\n');
    const bad=spawnSync(process.execPath,argv,{input:'private malformed input',env:{},encoding:'utf8',timeout:5000});
    assert.equal(bad.status,1);assert.equal(bad.stdout,'');assert.equal(bad.stderr,'EVALUATION_FAILED\n');
  }finally{f.close();}
});
