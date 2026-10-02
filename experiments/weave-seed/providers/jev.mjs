/** Explicit, single-request Jev process capability. No credential discovery or retries. */
import { createHash, randomUUID } from 'node:crypto';
import { lstatSync, realpathSync, openSync, writeFileSync, fsyncSync, closeSync } from 'node:fs';
import { resolve, dirname, isAbsolute } from 'node:path';
import { pathToFileURL } from 'node:url';
import {strictJSON as decodeJSON,file as readBoundFile,bindEvidence} from './common.mjs';
const sha = value => createHash('sha256').update(value).digest('hex');
const choices = ['satisfied', 'violated', 'unknown'];
const fail = () => { throw Error('JEV_ASSESSMENT_FAILED'); };
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const exact = (v, keys) => object(v) && Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k));
const text = v => typeof v === 'string' && v.trim().length > 0 && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(v);
const integer = v => Number.isSafeInteger(v) && v >= 0;
const probability = v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1;

export function strictJSON(bytes) {try{return decodeJSON(bytes);}catch{fail();}}
function file(path,max) {try{return readBoundFile(path,max);}catch{fail();}}
export function loadConfiguration(path) {
  if (!isAbsolute(path)) fail();
  path=realpathSync(path);
  const bytes = file(path, 65536), config = strictJSON(bytes);
  if (!exact(config, ['schema','endpoint','model','apiKeyEnv','questionFile','decisionPolicy','limits','receiptDirectory']) || config.schema !== 'openprose.jev-process/1') fail();
  const endpoint = new URL(config.endpoint);
  if(endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.hash || endpoint.search) fail();
  if (!/^jev-\d+\.\d+\.\d+$/.test(config.model) || !/^[A-Z][A-Z0-9_]{0,127}$/.test(config.apiKeyEnv) || !text(config.questionFile)) fail();
  const policy = config.decisionPolicy;
  if(!exact(policy,['id','minProbability','minConfidence','minMargin']) || !text(policy.id) || ![policy.minProbability,policy.minConfidence,policy.minMargin].every(probability)) fail();
  const limits = config.limits;
  if(!exact(limits,['timeoutMs','maxInputBytes','maxRequestBytes','maxResponseBytes','maxInputTokens','maxOutputTokens']) || !Object.values(limits).every(n=>integer(n)&&n>0)) fail();
  if(limits.timeoutMs>300000 || ['maxInputBytes','maxRequestBytes','maxResponseBytes'].some(k=>limits[k]>8388608)) fail();
  const questionPath = realpathSync(resolve(dirname(path), config.questionFile)), questionBytes = file(questionPath,65536), question = strictJSON(questionBytes);
  if(!exact(question,['scope','type','instructions','criteria']) || !text(question.scope) || question.type!=='choice' || !text(question.instructions) || !exact(question.criteria,choices) || !Object.values(question.criteria).every(text)) fail();
  if(config.receiptDirectory!==null) {
    if(!isAbsolute(config.receiptDirectory)) fail();
    const st=lstatSync(config.receiptDirectory);
    if(!st.isDirectory() || st.isSymbolicLink() || realpathSync(config.receiptDirectory)!==config.receiptDirectory || (st.mode & 0o077)!==0) fail();
  }
  return {config,question,configPath:path,configSha256:sha(bytes),questionSha256:sha(questionBytes),questionPath};
}
export function prepare(raw, loaded, now = Date.now()) {
  const {config,question}=loaded;
  if(!integer(now) || raw.length>config.limits.maxInputBytes) fail();
  let prepared;try{prepared=bindEvidence(raw,loaded,now);}catch{fail();}
  if(!prepared)return null;
  const request={model:config.model,state:prepared.state,questions:{assessment:{type:'choice',instructions:question.instructions,criteria:question.criteria}}};
  const wire=JSON.stringify(request); if(Buffer.byteLength(wire)>config.limits.maxRequestBytes) fail();
  return {wire,evidenceIdentity:prepared.evidenceIdentity,validUntil:prepared.validUntil};
}
export function decide(response, config) {
  if(!object(response) || response.model!==config.model || !exact(response.answers,['assessment'])) fail();
  const a=response.answers.assessment, usage=response.usage;
  if(!object(a) || a.type!=='choice' || !choices.includes(a.choice) || !probability(a.confidence) || !exact(a.probabilities,choices) || !Object.values(a.probabilities).every(probability) || Math.abs(Object.values(a.probabilities).reduce((x,y)=>x+y,0)-1)>0.02) fail();
  if(!object(usage) || !integer(usage.input_tokens) || usage.input_tokens>config.limits.maxInputTokens || !integer(usage.output_tokens) || usage.output_tokens>config.limits.maxOutputTokens) fail();
  const selected=a.probabilities[a.choice], next=Math.max(...choices.filter(c=>c!==a.choice).map(c=>a.probabilities[c])), p=config.decisionPolicy;
  const admitted=a.choice!=='unknown' && selected>next && selected>=p.minProbability && a.confidence>=p.minConfidence && selected-next>=p.minMargin;
  return {judgment:admitted ? (a.choice==='violated'?'work-needed':'satisfied'):'unknown',rawChoice:a.choice,confidence:a.confidence,probabilities:a.probabilities,usage:{input_tokens:usage.input_tokens,output_tokens:usage.output_tokens}};
}
async function boundedBody(response,max) {
  if(!response.body) fail(); const reader=response.body.getReader(); const parts=[]; let total=0;
  try {for(;;) {const {done,value}=await reader.read(); if(done) break; total+=value.byteLength;if(total>max)fail();parts.push(Buffer.from(value));}}
  catch(error) {void reader.cancel().catch(()=>{});throw error;}
  finally {reader.releaseLock();}
  return Buffer.concat(parts,total);
}
export async function assess(raw,loaded,{fetchImpl=fetch,environment=process.env,now=Date.now}={}) {
  const {config,question}=loaded;
  const record={schema:'openprose.jev-receipt/1',id:randomUUID(),startedAt:now(),providerCalled:false,model:config.model,endpoint:config.endpoint,configSha256:loaded.configSha256,questionSha256:loaded.questionSha256,scope:question.scope,decisionPolicy:config.decisionPolicy,inputSha256:sha(raw)};
  let timer;
  const secret=environment[config.apiKeyEnv];
  try {
    const prepared=prepare(raw,loaded,now());
    if(!prepared) {record.judgment='unknown';record.reason='evidence-gap-or-not-current';return {judgment:'unknown'};}
    const credential=environment[config.apiKeyEnv];
    if(!text(credential) || /[\r\n]/.test(credential) || prepared.wire.includes(credential)) fail();
    record.requestSha256=sha(prepared.wire);record.evidenceIdentity=prepared.evidenceIdentity;
    const controller=new AbortController();
    const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('JEV_ASSESSMENT_FAILED'));},config.limits.timeoutMs);});
    record.providerCalled=true;
    const rawResponse=await Promise.race([timeout,(async()=>{
      const response=await fetchImpl(config.endpoint,{method:'POST',headers:{Authorization:`Bearer ${credential}`,'Content-Type':'application/json'},body:prepared.wire,redirect:'error',signal:controller.signal});
      if(!response.ok || !/^application\/json(?:;|$)/i.test(response.headers.get('content-type')??'')) fail();
      return await boundedBody(response,config.limits.maxResponseBytes);
    })()]);
    clearTimeout(timer);
    record.responseSha256=sha(rawResponse);
    const decision=decide(strictJSON(rawResponse),config);Object.assign(record,decision);
    if(now()>=prepared.validUntil) {record.judgment='unknown';record.reason='evidence-expired-during-request';}
    return {judgment:record.judgment};
  } catch {record.error='JEV_ASSESSMENT_FAILED';throw Error('JEV_ASSESSMENT_FAILED');}
  finally {
    clearTimeout(timer);record.finishedAt=now();
    if(config.receiptDirectory!==null) {
      const fd=openSync(resolve(config.receiptDirectory,record.id+'.json'),'wx',0o600);
      try {
        const bytes=JSON.stringify(record,(_key,value)=>typeof value==='string' && typeof secret==='string' && secret.length ? value.replaceAll(secret,'[REDACTED]') : value)+'\n';
        if(Buffer.byteLength(bytes)>262144) fail();
        writeFileSync(fd,bytes);fsyncSync(fd);
      } finally {closeSync(fd);}
    }
  }
}
async function main() {
  try {
    if(process.argv.length!==4 || process.argv[2]!=='--config') fail();
    const loaded=loadConfiguration(process.argv[3]);const parts=[];let total=0;
    for await(const part of process.stdin) {total+=part.length;if(total>loaded.config.limits.maxInputBytes)fail();parts.push(part);}
    const result=await assess(Buffer.concat(parts,total),loaded);
    process.stdout.write(JSON.stringify(result)+'\n');
  } catch {process.stderr.write('JEV_ASSESSMENT_FAILED\n');process.exitCode=1;}
}
if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) await main();
