/** Materialize an explicitly reviewed local BYOK configuration; never execute it. */
import {accessSync,constants,mkdirSync,lstatSync,realpathSync,writeFileSync,rmSync,openSync,fstatSync,readSync,closeSync} from 'node:fs';
import {resolve,join,dirname,isAbsolute,relative} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {strictJSON,loadConfiguration as loadJev,prepare as prepareJev} from '../providers/jev.mjs';
import {loadConfig as loadActor,validateSnapshot,kernelImageSha256} from '../integration/native-actor/actor.mjs';
import {prepareConfig} from '../integration/run.mjs';
const here=dirname(fileURLToPath(import.meta.url));
const fail=()=>{throw Error('CONFIGURATION_SETUP_REJECTED');};
const exact=(v,keys)=>v!==null&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).length===keys.length&&keys.every(k=>Object.hasOwn(v,k));
const nonempty=v=>typeof v==='string'&&v.trim().length>0&&!v.includes('\0');
const sha=value=>createHash('sha256').update(value).digest('hex');
const hex=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
const inside=(root,path)=>{const r=relative(root,path);return r!== '..'&&!r.startsWith('../')&&!isAbsolute(r);};
function read(path,max=262144){
 const fd=openSync(path,constants.O_RDONLY|constants.O_NONBLOCK);
 try {const info=fstatSync(fd);if(!info.isFile()||info.size>max)fail();const bytes=Buffer.alloc(max+1);let n=0,r;while(n<bytes.length&&(r=readSync(fd,bytes,n,bytes.length-n,null))>0)n+=r;if(n>max)fail();return bytes.subarray(0,n);}finally{closeSync(fd);}
}
function executable(path,expected){
 if(!nonempty(path)||!isAbsolute(path))fail();const canonical=realpathSync(path);accessSync(canonical,constants.X_OK);
 const fd=openSync(canonical,constants.O_RDONLY|constants.O_NONBLOCK),digest=createHash('sha256');let total=0;
 try {if(!fstatSync(fd).isFile())fail();const chunk=Buffer.alloc(65536);let count;while((count=readSync(fd,chunk,0,chunk.length,null))>0){total+=count;if(total>536870912)fail();digest.update(chunk.subarray(0,count));}}
 finally{closeSync(fd);}const identity=digest.digest('hex');if(expected&&identity!==expected)fail();return {path:canonical,sha256:identity};
}
const nextSteps={
 manifest:'Use an absolute readable setup JSON file with schema 1 and exactly the documented fields; require maxAttempts 0..100, an explicit action model and actor environment names including OPENAI_API_KEY.',
 root:'Select an existing canonical absolute source root and a new single configuration directory name.',
 sources:'Select existing bounded UTF-8 regular files inside the root; include the task among unique contract selections.',
 runtime:'Select an existing executable Bun file by absolute path. The helper does not execute or verify its runtime version.',
 'native-cli':'Select an existing executable CLI file and provide its matching lowercase SHA-256 and a fixed single-payload kernel image digest matching the selected kernel bytes.',
 question:'Review the explicit endpoint, pinned model, environment-variable name, decision thresholds and question JSON schema.',
 destination:'Choose a new configuration directory inside the selected root; existing destinations are never overwritten.',
 'generated-binding':'Check aggregate source size, canonical paths and required config/question/kernel/task evidence bindings. No capability was executed.'
};
export function configure(setupPath){
 let stage='manifest';
 try{return configureStages(setupPath,value=>{stage=value;});}
 catch{const error=Error('CONFIGURATION_SETUP_REJECTED');error.code='CONFIGURATION_SETUP_REJECTED';error.stage=stage;error.nextStep=nextSteps[stage];throw error;}
}
function configureStages(setupPath,stage){
 if(typeof Bun==='undefined'||!isAbsolute(setupPath))fail();const setupBytes=read(setupPath,65536),s=strictJSON(setupBytes);
 if(!exact(s,['schema','root','configDirectory','kernel','task','contracts','evidence','bun','nativeCli','nativeCliSha256','expectedImageSha256','actionModel','actorEnvironmentKeys','assessment','maxAttempts'])||s.schema!==1)fail();
 if(!Number.isSafeInteger(s.maxAttempts)||s.maxAttempts<0||s.maxAttempts>100||!nonempty(s.actionModel))fail();
 if(!Array.isArray(s.actorEnvironmentKeys)||new Set(s.actorEnvironmentKeys).size!==s.actorEnvironmentKeys.length||s.actorEnvironmentKeys.some(k=>typeof k!=='string'||!/^[A-Za-z_][A-Za-z0-9_]*$/.test(k))||!s.actorEnvironmentKeys.includes('OPENAI_API_KEY'))fail();
 stage('root');
 if(!nonempty(s.root)||!isAbsolute(s.root)||realpathSync(s.root)!==s.root||!lstatSync(s.root).isDirectory())fail();
 if(!nonempty(s.configDirectory)||['.','..'].includes(s.configDirectory)||/[\\/]/.test(s.configDirectory))fail();
 stage('sources');
 const source=name=>{if(!nonempty(name)||isAbsolute(name)||name.split(/[\\/]/).some(part=>!part||part==='.'||part==='..'))fail();const path=resolve(s.root,name),canonical=realpathSync(path);if(!inside(s.root,canonical)||path!==canonical)fail();new TextDecoder('utf-8',{fatal:true}).decode(read(path));return path;};
 const kernel=source(s.kernel),task=source(s.task);
 if(!Array.isArray(s.contracts)||!s.contracts.length||!s.contracts.includes(s.task)||!Array.isArray(s.evidence)||!s.evidence.length)fail();
 const selected=[kernel,...s.contracts.map(source),...s.evidence.map(source)];if(new Set(selected).size!==selected.length)fail();
 stage('question');
 const a=s.assessment;if(!exact(a,['endpoint','model','questionFile','decisionPolicy','apiKeyEnv'])||!nonempty(a.questionFile)||!isAbsolute(a.questionFile))fail();
 const questionBytes=read(a.questionFile,65536);strictJSON(questionBytes);
 stage('runtime');const runtime=executable(s.bun);
 stage('native-cli');if(!hex(s.nativeCliSha256)||!hex(s.expectedImageSha256)||kernelImageSha256(read(kernel))!==s.expectedImageSha256)fail();const native=executable(s.nativeCli,s.nativeCliSha256);
 const target=join(s.root,s.configDirectory);let owned;
 try {
  stage('destination');mkdirSync(target,{mode:0o700});owned=lstatSync(target);
  const write=(name,value)=>writeFileSync(join(target,name),value,{flag:'wx',mode:0o600});
  const provider={schema:'openprose.jev-process/1',endpoint:a.endpoint,model:a.model,apiKeyEnv:a.apiKeyEnv,questionFile:'question.json',decisionPolicy:a.decisionPolicy,limits:{timeoutMs:20000,maxInputBytes:1048576,maxRequestBytes:1048576,maxResponseBytes:65536,maxInputTokens:65536,maxOutputTokens:1024},receiptDirectory:null};
  const actor={schema:1,executable:native.path,executableSha256:native.sha256,cwd:s.root,kernel:s.kernel,task:s.task,expectedImageSha256:s.expectedImageSha256,harness:'agents-sdk',authProfile:'openai-api-key',model:s.actionModel,environmentKeys:s.actorEnvironmentKeys,maxTurns:8,nativeTimeoutMs:120000,toolTimeoutMs:15000,outerTimeoutMs:150000,processTimeoutMs:165000,readinessTimeoutMs:45000,maxCaptureBytes:4194304};
  stage('generated-binding');
  const codeFiles=['./configure.mjs','../providers/jev.mjs','../providers/common.mjs','../integration/native-actor/run.mjs','../integration/native-actor/actor.mjs','../integration/run.mjs','../integration/process.mjs','../integration/config.mjs','../integration/binding.mjs','../local/run.mjs','../local/coordinator.mjs','../local/check.mjs','../bun/index.mjs','../bun/host.mjs'].map(path=>sha(read(resolve(here,path))));
  const config={schema:1,root:s.root,kernel:s.kernel,contracts:s.contracts,evidence:[...s.evidence,...['question.json','jev.json','actor.json','config.json'].map(name=>join(s.configDirectory,name))],capabilityVersion:'explicit-local-setup-v1-'+sha(JSON.stringify({setup:sha(setupBytes),runtime:runtime.sha256,code:codeFiles})),assessor:[runtime.path,'--no-env-file',resolve(here,'../providers/jev.mjs'),'--config',join(target,'jev.json')],actor:[runtime.path,'--no-env-file',resolve(here,'../integration/native-actor/run.mjs'),'--config',join(target,'actor.json')],environmentKeys:[...new Set([a.apiKeyEnv,...s.actorEnvironmentKeys])],checkpointDirectory:'host',maxAttempts:s.maxAttempts,ttlMs:300000,timeoutMs:270000,maxOutputBytes:1048576};
  write('question.json',questionBytes);write('jev.json',JSON.stringify(provider,null,2)+'\n');write('actor.json',JSON.stringify(actor,null,2)+'\n');write('config.json',JSON.stringify(config,null,2)+'\n');
  // Reuse the actual adapters' configuration validators; no commands or provider calls.
  stage('question');const jev=loadJev(join(target,'jev.json'));
  stage('generated-binding');const nativeConfig=loadActor(join(target,'actor.json'));
  const prepared=prepareConfig(join(target,'config.json'),{allowMissingEnvironment:true}),now=Date.now(),evidence=prepared.bound.observe(now);if(evidence.gap)fail();
  prepareJev(Buffer.from(JSON.stringify({schema:'openprose.weave-input/1',evidence,attempt:null})),jev,now);
  validateSnapshot(nativeConfig,{schema:'openprose.weave-input/1',evidence,attempt:'offline-configuration-validation'},now);
  const command=(...args)=>[runtime.path,'--no-env-file',resolve(here,'../local/run.mjs'),...args];const path=join(target,'config.json');
  return {schema:'openprose.weave-setup/1',status:'configured-not-provider-verified',providerVerified:false,runtimeVersionVerified:false,root:s.root,configurationDirectory:target,config:path,commands:{check:command('check',path),status:command('status',path),step:command('step',path),serve:command('serve',path,'--poll-ms','1000','--max-steps','1')}};
 }catch(error){
  if(owned){const current=lstatSync(target);if(current.dev===owned.dev&&current.ino===owned.ino)rmSync(target,{recursive:true});}
  throw Error('CONFIGURATION_SETUP_REJECTED');
 }
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 try{if(process.argv.length!==3)fail();console.log(JSON.stringify(configure(process.argv[2])));}
 catch(error){console.error(JSON.stringify({error:'CONFIGURATION_SETUP_REJECTED',stage:error.stage??'manifest',nextStep:error.nextStep??nextSteps.manifest}));process.exitCode=1;}
}
