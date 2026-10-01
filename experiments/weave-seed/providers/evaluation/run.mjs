/** Explicit evaluator process for bound sources and all-required evaluation plans. */
import {createHash,randomUUID} from 'node:crypto';
import {realpathSync,lstatSync,openSync,writeFileSync,fsyncSync,closeSync} from 'node:fs';
import {resolve,dirname,isAbsolute} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {strictJSON,file,bindEvidence} from '../common.mjs';
import {adapters,templateVersion,validateProfile,buildRequest,parseResponse,applyPolicy,aggregate,fail,object,exact,text,integer,probability} from './adapters.mjs';
const sha=v=>createHash('sha256').update(v).digest('hex');
const id=v=>typeof v==='string'&&/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(v);

export function capabilityIdentity() {
  const files=['./run.mjs','./adapters.mjs','../common.mjs'].map(p=>sha(file(fileURLToPath(new URL(p,import.meta.url)),1048576)));
  return 'evaluation-process-1-'+sha(JSON.stringify(files));
}

function receiptDirectory(path) {
  if(path===null)return;
  if(!isAbsolute(path))fail();const st=lstatSync(path);
  if(!st.isDirectory()||st.isSymbolicLink()||realpathSync(path)!==path||(st.mode&0o077)!==0)fail();
}

export function loadConfiguration(path) {
  if(!isAbsolute(path))fail();path=realpathSync(path);
  const bytes=file(path,65536),c=strictJSON(bytes);
  if(!exact(c,['schema','planFile','profiles','limits','receiptDirectory'])||c.schema!=='openprose.evaluator-process/1'||!text(c.planFile)||!object(c.profiles)||!Object.keys(c.profiles).length||Object.keys(c.profiles).length>16)fail();
  for(const [name,p] of Object.entries(c.profiles)){if(!id(name))fail();validateProfile(p);}
  const limits=c.limits;
  if(!exact(limits,['timeoutMs','totalTimeoutMs','maxCalls','maxInputBytes','maxRequestBytes','maxResponseBytes','maxInputTokens','maxOutputTokens'])||!Object.values(limits).every(v=>integer(v)&&v>0)||limits.timeoutMs>300000||limits.totalTimeoutMs>300000||limits.timeoutMs>limits.totalTimeoutMs||limits.maxCalls>32||limits.maxOutputTokens>32768||['maxInputBytes','maxRequestBytes','maxResponseBytes'].some(k=>limits[k]>8388608))fail();
  const planPath=realpathSync(resolve(dirname(path),c.planFile)),planBytes=file(planPath,65536),plan=strictJSON(planBytes);
  if(!exact(plan,['schema','composition','evaluations'])||plan.schema!=='openprose.evaluation-plan/1'||plan.composition!=='all-required'||!Array.isArray(plan.evaluations)||!plan.evaluations.length||plan.evaluations.length>limits.maxCalls)fail();
  const ids=new Set();
  for(const e of plan.evaluations) {
    if(!exact(e,['id','subject','profile','questions','policy'])||!id(e.id)||ids.has(e.id)||!Object.hasOwn(c.profiles,e.profile))fail();ids.add(e.id);
    const s=e.subject;
    if(!exact(s,['instance','contracts','bindings'])||!text(s.instance)||!Array.isArray(s.contracts)||!s.contracts.length||!s.contracts.every(p=>text(p)&&isAbsolute(p))||new Set(s.contracts).size!==s.contracts.length||typeof s.bindings!=='string')fail();
    if(!object(e.questions)||!Object.keys(e.questions).length||Object.keys(e.questions).length>64)fail();
    for(const [name,q] of Object.entries(e.questions)) {
      if(!id(name)||!exact(q,['instructions','criteria','outcomes'])||!text(q.instructions)||!object(q.criteria))fail();
      const labels=Object.keys(q.criteria);
      if(labels.length<2||labels.length>64||!labels.every(id)||!Object.values(q.criteria).every(text)||!exact(q.outcomes,labels)||!Object.values(q.outcomes).every(v=>['satisfied','violated','unknown'].includes(v)))fail();
    }
    const policy=e.policy;
    if(!object(policy)||!text(policy.id))fail();
    if(policy.kind==='labels') {if(!exact(policy,['id','kind']))fail();}
    else if(policy.kind==='native-threshold') {
      if(!exact(policy,['id','kind','minProbability','minConfidence','minMargin'])||!adapters[c.profiles[e.profile].adapter].nativeProbabilities||![policy.minProbability,policy.minConfidence,policy.minMargin].every(probability))fail();
    }else fail();
  }
  receiptDirectory(c.receiptDirectory);
  // The shared source binder treats plan and configuration as evidence, never contracts.
  return {config:c,plan,configPath:path,configSha256:sha(bytes),questionPath:planPath,questionSha256:sha(planBytes)};
}

export function prepare(raw,loaded,now=Date.now()) {
  const bound=bindEvidence(raw,loaded,now);if(!bound)return null;
  const paths=new Set(bound.state.agreement.contracts.map(s=>s.path)),covered=new Set();
  for(const e of loaded.plan.evaluations)for(const path of e.subject.contracts) {
    if(!paths.has(path))fail();covered.add(path);
  }
  if(covered.size!==paths.size)fail();
  return bound;
}

async function body(response,max) {
  if(!response.body)fail();const reader=response.body.getReader();const parts=[];let size=0;
  try {for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>max)fail();parts.push(Buffer.from(value));}}
  catch(error){void reader.cancel().catch(()=>{});throw error;}
  finally{reader.releaseLock();}
  return Buffer.concat(parts,size);
}

export async function assess(raw,loaded,{fetchImpl=fetch,environment=process.env,now=Date.now,monotonic=()=>performance.now()}={}) {
  const {config,plan}=loaded,l=config.limits;
  const record={schema:'openprose.evaluation-receipt/1',id:randomUUID(),capabilityIdentity:capabilityIdentity(),templateVersion,startedAt:now(),configSha256:loaded.configSha256,planSha256:loaded.questionSha256,inputSha256:sha(raw),composition:plan.composition,evaluations:[]};
  const secrets=[...new Set(plan.evaluations.map(e=>environment[config.profiles[e.profile].apiKeyEnv]).filter(v=>typeof v==='string'&&v.length))];
  const start=monotonic();let timer;
  try {
    const prepared=prepare(raw,loaded,now());
    if(!prepared){record.judgment='unknown';record.reason='evidence-gap-or-not-current';return {judgment:'unknown'};}
    record.evidenceIdentity=prepared.evidenceIdentity;
    // Preflight every request and key before charging for any member of the plan.
    const calls=plan.evaluations.map(e=>{
      const p=config.profiles[e.profile],key=environment[p.apiKeyEnv];
      if(!text(key)||/[\r\n]/.test(key))fail();
      const request=buildRequest(p,{...prepared.state,evaluation_subject:e.subject},e.questions,l.maxOutputTokens,key);
      if(Buffer.byteLength(request.wire)>l.maxRequestBytes||secrets.some(s=>request.wire.includes(s)))fail();
      return {evaluation:e,profile:p,request};
    });
    for(const {evaluation:e,profile:p,request} of calls) {
      if(!prepare(raw,loaded,now())){record.judgment='unknown';record.reason='evidence-expired-before-request';return {judgment:'unknown'};}
      const remaining=l.totalTimeoutMs-(monotonic()-start);if(remaining<=0)fail();
      const row={id:e.id,subject:e.subject,profile:e.profile,adapter:p.adapter,requestedModel:p.model,acceptedModels:p.acceptedModels,policy:e.policy,requestSha256:sha(request.wire),providerCalled:false};
      // Bind opaque subject text by digest instead of retaining it in a private receipt.
      row.subject={instance:e.subject.instance,contracts:e.subject.contracts,bindingsSha256:sha(e.subject.bindings)};
      record.evaluations.push(row);
      const controller=new AbortController();
      const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('EVALUATION_FAILED'));},Math.min(l.timeoutMs,remaining));});
      row.providerCalled=true;
      const rawResponse=await Promise.race([deadline,(async()=>{
        const response=await fetchImpl(request.endpoint,{method:'POST',headers:request.headers,body:request.wire,redirect:'error',signal:controller.signal});
        if(!response.ok||!/^application\/json(?:;|$)/i.test(response.headers.get('content-type')??''))fail();
        return await body(response,l.maxResponseBytes);
      })()]);
      clearTimeout(timer);timer=undefined;
      row.responseSha256=sha(rawResponse);
      const parsed=parseResponse(rawResponse,p,e.questions,l);Object.assign(row,parsed,applyPolicy(parsed,e.questions,e.policy));
      if(monotonic()-start>=l.totalTimeoutMs)fail();
      if(now()>=prepared.validUntil){record.judgment='unknown';record.reason='evidence-expired-during-request';return {judgment:'unknown'};}
    }
    Object.assign(record,aggregate(record.evaluations.flatMap(e=>Object.values(e.statuses))));
    return {judgment:record.judgment};
  }catch{record.error='EVALUATION_FAILED';throw Error('EVALUATION_FAILED');}
  finally {
    clearTimeout(timer);record.finishedAt=now();
    if(config.receiptDirectory!==null) {
      receiptDirectory(config.receiptDirectory);
      const bytes=JSON.stringify(record,(_key,value)=>typeof value==='string'?secrets.reduce((v,s)=>v.replaceAll(s,'[REDACTED]'),value):value)+'\n';
      if(Buffer.byteLength(bytes)>1048576)fail();
      const fd=openSync(resolve(config.receiptDirectory,record.id+'.json'),'wx',0o600);
      try{writeFileSync(fd,bytes);fsyncSync(fd);}finally{closeSync(fd);}
    }
  }
}

async function main() {
  try {
    if(process.argv.length===3&&process.argv[2]==='--capabilities') {
      process.stdout.write(JSON.stringify({schema:'openprose.evaluator-capabilities/1',capabilityIdentity:capabilityIdentity(),templateVersion,adapters,providerCalled:false})+'\n');return;
    }
    if(process.argv.length===4&&process.argv[2]==='--check') {
      const loaded=loadConfiguration(process.argv[3]);
      process.stdout.write(JSON.stringify({status:'configured-not-provider-verified',capabilityIdentity:capabilityIdentity(),evaluations:loaded.plan.evaluations.length,providerCalled:false})+'\n');return;
    }
    if(process.argv.length!==4||process.argv[2]!=='--config')fail();
    const loaded=loadConfiguration(process.argv[3]),parts=[];let total=0;
    for await(const part of process.stdin){total+=part.length;if(total>loaded.config.limits.maxInputBytes)fail();parts.push(part);}
    process.stdout.write(JSON.stringify(await assess(Buffer.concat(parts,total),loaded))+'\n');
  }catch{process.stderr.write('EVALUATION_FAILED\n');process.exitCode=1;}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)await main();
