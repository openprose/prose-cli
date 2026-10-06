import {afterEach,expect,test} from "bun:test";
import Ajv2020 from "ajv/dist/2020";
import observationSchema from "../../shared/schemas/sdk-observation.schema.json";
import {mkdtemp,mkdir,writeFile,readFile,realpath,rm,symlink} from "node:fs/promises";
import {join} from "node:path";
import {tmpdir} from "node:os";
import oracle from "../../shared/fixtures/adapters/sdk-production.json";
import {resolveInstalledExecutable} from "../src/adapters/executable";
import {sdkObservations} from "../src/adapters/sdk-observation";
import {installedProtocol} from "../src/adapters/protocols";
import {nativeLimits,sdkNativeFailure} from "../src/adapters/sdk-limits";
import {runCli} from "../src/cli";
import {buildInstalledAdapterEnvironment} from "../src/adapters/environment";
import {installedAdapterDefinition} from "../src/adapters/recipes";
import {assertInstalledAdapterPlatform} from "../src/adapters/admission";
import {runInstalledAdapter} from "../src/adapters/runner";
import {verifyRuntimeImage,canonicalJson,sha256} from "../src/core/image";
import {sentinelFixtureImage} from "./sentinel-fixture";
import type {RunnerInvocation} from "../src/core/types";
const roots:string[]=[];
const validateObservation=new Ajv2020({strict:true,allErrors:true}).compile(observationSchema);
async function workspace(){const root=await realpath(await mkdtemp(join(tmpdir(),"prose-sdk-production-")));roots.push(root);await mkdir(join(root,".git"));return root;}
afterEach(async()=>{for(const root of roots.splice(0))await rm(root,{recursive:true,force:true});});
test("packaged SDK native support admits both GNU Linux architectures and rejects musl",()=>{
  for(const arch of ["arm64","x64"] as const){expect(assertInstalledAdapterPlatform("agents-sdk/jsonl",{platform:"linux",arch,libc:"gnu"})).toBe(`linux-${arch}-gnu`);expect(()=>assertInstalledAdapterPlatform("agents-sdk/jsonl",{platform:"linux",arch,libc:"musl"})).toThrow();}
});
test("packaged SDK discovery follows the CLI symlink, relocates together and refuses PATH fallback",async()=>{
  const root=await workspace(),payload=join(root,"payload");await mkdir(payload);await writeFile(join(payload,"prose"),"fixture",{mode:0o755});await writeFile(join(payload,"prose-agents-sdk"),"fixture",{mode:0o755});await symlink(join(payload,"prose"),join(root,"prose-link"));
  const input={adapterId:"agents-sdk/jsonl" as const,ambient:{PATH:"/wrong"},wrapperExecutable:join(root,"prose-link"),platform:"darwin" as const,arch:"arm64" as const};
  expect(await resolveInstalledExecutable(input)).toBe(join(payload,"prose-agents-sdk"));
  await rm(join(payload,"prose-agents-sdk"));await writeFile(join(root,"prose-agents-sdk"),"wrong install",{mode:0o755});
  await expect(resolveInstalledExecutable({...input,ambient:{PATH:root}})).rejects.toMatchObject({code:"HARNESS_UNAVAILABLE",details:{fallbackAttempted:false,repairCommand:oracle.discovery.repairCommand}});
  expect(await resolveInstalledExecutable({...input,ambient:{PATH:root},allowPathDiscovery:true})).toBe(join(root,"prose-agents-sdk"));
  await symlink(join(root,"prose-agents-sdk"),join(payload,"prose-agents-sdk"));
  await expect(resolveInstalledExecutable(input)).rejects.toMatchObject({code:"HARNESS_UNAVAILABLE"});
  const relocated=join(root,"relocated");await mkdir(relocated);await writeFile(join(relocated,"prose"),"fixture",{mode:0o755});await writeFile(join(relocated,"prose-agents-sdk"),"fixture",{mode:0o755});
  expect(await resolveInstalledExecutable({...input,wrapperExecutable:join(relocated,"prose")})).toBe(join(relocated,"prose-agents-sdk"));
});
for(const key of [undefined,"","  "])test(`SDK missing/blank key ${JSON.stringify(key)} fails before helper resolution`,async()=>{
  const root=await workspace();let stdout="",stderr="";
  const exit=await runCli(["--output","json","run","input.prose.md"],{processCwd:root,env:{HOME:join(root,"home"),...(key===undefined?{}:{OPENAI_API_KEY:key})},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:text=>{stderr+=text;}});
  expect(exit).toBe(10);expect(stderr).toBe("");expect(JSON.parse(stdout)).toMatchObject({code:"HARNESS_NEEDS_AUTH",details:{adapterId:"agents-sdk/jsonl",authProfile:"openai-api-key"}});expect(JSON.parse(stdout).action).toContain("OPENAI_API_KEY");
});
test("pure exact SDK defaults resolve without credentials or executable discovery",async()=>{
  const root=await workspace();let stdout="";
  expect(await runCli(["cli","config","explain","--json","--","run","input.prose.md"],{processCwd:root,env:{HOME:join(root,"home")},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:()=>{}})).toBe(0);
  expect(JSON.parse(stdout)).toMatchObject({values:{harness:{value:oracle.defaults.harness},model:{value:oracle.defaults.model},authProfile:{value:oracle.defaults.authProfile}},runtime:{transport:oracle.defaults.transport,billingOwner:oracle.defaults.billingOwner,nativeLimits:oracle.nativeLimits}});
});
test("target bundle errors never expose unresolved inherited credential-profile values",async()=>{
  const root=await workspace();let stdout="";
  expect(await runCli(["cli","harness","use","codex","--auth-profile","target-profile-secret-sentinel","--json"],{processCwd:root,env:{HOME:join(root,"home"),PROSE_AUTH_PROFILE:"inherited-profile-secret-sentinel"},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:()=>{}})).toBe(2);
  expect(stdout).not.toContain("target-profile-secret-sentinel");expect(stdout).not.toContain("inherited-profile-secret-sentinel");expect(JSON.parse(stdout)).toMatchObject({code:"CONFIG_INVALID",details:{source:"--auth-profile",reason:"Authentication profile is incompatible with the selected harness."}});expect(JSON.parse(stdout).details).not.toHaveProperty("configurationExplanation");
});
test("SDK native observation groups are bounded, invocation owned and omit invalid counters",()=>{
  const tainted={usageObservation:{...oracle.observation.completedUsage,raw_usage:"secret",observedTokenTotals:{...oracle.observation.completedUsage.observedTokenTotals,secret:"sdk-unrecognized-secret-sentinel",output_tokens:-1}},modelIdentity:{requested:"untrusted-request",observed:["z","a","a","sdk-unrecognized-secret-sentinel\n"],serviceTier:{requested:"default",observed:["priority","default","priority","sdk-unrecognized-secret-sentinel"]}}};
  const result=sdkObservations(tainted,"gpt-6.1-sol");expect(result).toMatchObject({usageObservation:{observedTokenTotals:{input_tokens:9,total_tokens:15}},modelIdentity:{requested:"gpt-6.1-sol",observed:["a","z"],serviceTier:{requested:"default",observed:["default","priority"]}}});expect(JSON.stringify(result)).not.toContain("sdk-unrecognized-secret-sentinel");expect(result.usageObservation!.observedTokenTotals).not.toHaveProperty("output_tokens");
  expect(validateObservation(result),JSON.stringify(validateObservation.errors)).toBe(true);
  expect(sdkObservations({usageObservation:{...oracle.observation.completedUsage,startedCallCount:-1}},"gpt-6.1-sol")).toEqual({});
  expect(sdkObservations({},"gpt-6.1-sol")).toEqual({});
  for(const modelIdentity of [{observed:[]},{observed:[],serviceTier:{requested:"flex",observed:[]}},{observed:"invalid",serviceTier:{requested:"default",observed:[]}}])expect(sdkObservations({modelIdentity},"gpt-6.1-sol")).toEqual({});
  const bounded=sdkObservations({modelIdentity:{observed:Array.from({length:256},(_,index)=>`gpt-${index}`),serviceTier:{requested:"default",observed:[]}}},"gpt-6.1-sol");expect((bounded.modelIdentity!.observed as unknown[]).length).toBe(128);expect(validateObservation(bounded),JSON.stringify(validateObservation.errors)).toBe(true);
});
test("SDK final and failure retain observations while usage remains a separate unavailable concept",()=>{
  for(const type of ["final","error"]){const protocol=installedProtocol("agents-sdk/jsonl","0.1.0","fixture",null,true,"gpt-6.1-sol");protocol.accept({type:"start",model:"untrusted",cwd:"/tmp"});const terminal={type,output:"done",error_type:"ExecutionError",usageObservation:oracle.observation.completedUsage,modelIdentity:oracle.observation.modelIdentity,limits:oracle.nativeLimits};if(type==="final")protocol.accept(terminal);else expect(()=>protocol.accept(terminal)).toThrow();expect(protocol.sdkObservations).toEqual({usageObservation:oracle.observation.completedUsage,modelIdentity:oracle.observation.modelIdentity});}
  expect(nativeLimits({harness:"agents-sdk"})).toEqual(oracle.nativeLimits);expect(sdkNativeFailure({error_type:"ExecutionError",limits:oracle.nativeLimits})).toMatchObject({limits:oracle.nativeLimits});expect(sdkNativeFailure({limits:{...oracle.nativeLimits,maxChildDepth:2}})).not.toHaveProperty("limits");
  expect(()=>buildInstalledAdapterEnvironment({definition:installedAdapterDefinition("agents-sdk/jsonl"),ambient:{OPENAI_API_KEY:"  "},credentialGroup:"openai-api-key"})).toThrow();
});
for(const scenario of ["success","failure","cancel"])test(`actual supervised SDK ${scenario} preserves completed observations`,async()=>{
  const root=await workspace(),helper=join(root,"prose-agents-sdk"),image=await verifyRuntimeImage(sentinelFixtureImage);
  const observation=scenario==="success"?oracle.observation.completedUsage:oracle.observation.failureUsage;
  const terminal={type:scenario==="success"?"final":"error",output:"native result",error_type:scenario==="cancel"?"CancelledError":"ExecutionError",limits:oracle.nativeLimits,usageObservation:observation,modelIdentity:oracle.observation.modelIdentity};
  const source=`#!/Users/mm/openprose/.scratch/imp-086-tools/bun-darwin-aarch64/bun\nconsole.log(JSON.stringify({type:'start',model:'gpt-6.1-sol',cwd:${JSON.stringify(root)}}));\nconst terminal=${JSON.stringify(terminal)};\n${scenario==="cancel"?"console.log(JSON.stringify({...terminal,type:'tool_result',name:'execute_shell'}));process.on('SIGTERM',()=>{console.log(JSON.stringify(terminal));process.exit(1)});setInterval(()=>{},1000);":"console.log(JSON.stringify(terminal));process.exit("+(scenario==="success"?0:1)+");"}`;
  await writeFile(helper,source,{mode:0o755});const task={schema:image.manifest.taskEnvelope.schemaId,argv:["prose","run","input.prose.md"],interactionMode:"non-interactive" as const};
  const invocation:RunnerInvocation={schema:"openprose.runner-invocation/1",invocationId:"fixture-invocation-0001",cwd:root,languageImage:{formatVersion:image.manifest.imageFormatVersion,version:image.manifest.imageVersion,sha256:image.aggregateSha256},runner:{name:"bun",version:"test",commit:"test"},harness:"agents-sdk",transport:"jsonl",recursionToken:"fixture-recursion",task,taskDigestSha256:await sha256(canonicalJson(task))};
  const controller=new AbortController(),capture=join(root,"native.jsonl");
  const timer=scenario==="cancel"?setInterval(async()=>{if((await readFile(capture,"utf8").catch(()=>"")).includes('"tool_result"'))controller.abort();},25):undefined;
  try {
    const outcome=await runInstalledAdapter({adapterId:"agents-sdk/jsonl",executable:helper,harnessVersion:"0.1.0",credentialGroup:"openai-api-key",invocation,image,ambient:{OPENAI_API_KEY:"fixture-key"},model:"gpt-6.1-sol",outputContract:"native",timeoutMs:2000,temporaryRoot:root,cancelSignal:controller.signal,...(scenario==="cancel"?{nativeLog:capture}:{})});
    expect(outcome.sdkObservations).toEqual({usageObservation:observation,modelIdentity:oracle.observation.modelIdentity});expect(outcome.process.exitCode).toBe(scenario==="success"?0:1);
    if(scenario==="cancel")expect(outcome.process.cancellationReason).toBe("caller");else expect(outcome.process.error===null).toBe(scenario==="success");
  }finally{if(timer!==undefined)clearInterval(timer);}
});
