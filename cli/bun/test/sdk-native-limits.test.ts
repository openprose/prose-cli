import {test,expect} from "bun:test";
import {mkdtemp,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import fixture from "../../shared/fixtures/adapters/sdk-native-limits.json";
import production from "../../shared/fixtures/adapters/sdk-production.json";
import {nativeLimits,nativeLimitsArgv,sdkNativeFailure} from "../src/adapters/sdk-limits";
import {parseEntrypoint} from "../src/core/args";
import {resolveConfiguration} from "../src/core/config";
import {installedProtocol} from "../src/adapters/protocols";
import {buildInstalledLaunchPlan} from "../src/adapters/plan";
import {verifyRuntimeImage,canonicalJson,sha256} from "../src/core/image";
import {sentinelFixtureImage} from "./sentinel-fixture";
import {encodeRuntimeImage} from "../src/supervision/files";
import type {RunnerInvocation} from "../src/core/types";
test("shared SDK budgets preserve defaults, validate explicit inputs and reject unsupported hosts",()=>{
 expect(nativeLimits({harness:"agents-sdk"})).toEqual(production.nativeLimits);expect(nativeLimitsArgv({harness:"agents-sdk"})).toEqual([]);
 expect(nativeLimits({harness:"agents-sdk",...fixture.override})).toEqual({...production.nativeLimits,...fixture.override.limits,maxAggregateRequests:40,maxAggregateHostedWebCalls:40});expect(nativeLimitsArgv({harness:"agents-sdk",...fixture.override})).toEqual(fixture.override.argv);
 for(const nativeMaxTurns of fixture.invalidTurns)expect(()=>nativeLimits({harness:"agents-sdk",nativeMaxTurns})).toThrow();
 for(const nativeTimeout of fixture.invalidTimeouts)expect(()=>nativeLimits({harness:"agents-sdk",nativeTimeout})).toThrow();
 for(const harness of ["claude","codex","prime","omp","mock","openprose"]){expect(nativeLimits({harness})).toBeUndefined();expect(()=>nativeLimits({harness,nativeMaxTurns:"2"})).toThrow();expect(()=>nativeLimits({harness,nativeTimeout:"1s"})).toThrow();}
 for(const flag of ["--native-max-turns","--native-timeout"])expect(()=>parseEntrypoint([flag,"2",flag,"3","task"])).toThrow();
 expect(nativeLimits({harness:"agents-sdk",nativeTimeout:"1ms"})?.timeoutSeconds).toBe(.001);
});
test("SDK configuration flags override environment and quoted TOML, leaving outer timeout distinct",async()=>{
 const root=await mkdtemp(join(tmpdir(),"sdk-limits-"));try{
 const userConfigPath=join(root,"user.toml");await writeFile(userConfigPath,'harness="agents-sdk"\nnative_max_turns="25"\nnative_timeout="2m"\n');
 const parsed=parseEntrypoint(["--native-max-turns","40","--native-timeout","5m","--timeout","10m","task"]);
 const c=await resolveConfiguration(parsed.global,{processCwd:root,userConfigPath,env:{PROSE_NATIVE_MAX_TURNS:"30",PROSE_NATIVE_TIMEOUT:"3m"}});
 expect(nativeLimits(c.values)).toEqual({...production.nativeLimits,...fixture.override.limits,maxAggregateRequests:40,maxAggregateHostedWebCalls:40});expect(c.values.timeout).toBe("10m");expect(c.sources.nativeMaxTurns?.kind).toBe("flag");
 await expect(resolveConfiguration({harness:"claude",nativeMaxTurns:"2"},{processCwd:root,userConfigPath:join(root,"none"),env:{}})).rejects.toThrow();
 }finally{await rm(root,{recursive:true,force:true});}
});
test("SDK actual launch forwards only explicit native budgets without changing task",async()=>{
 const image=await verifyRuntimeImage(sentinelFixtureImage), task={schema:image.manifest.taskEnvelope.schemaId,argv:["task"],interactionMode:"non-interactive" as const};
 const invocation:RunnerInvocation={schema:"openprose.runner-invocation/1",invocationId:"sdk-budget-test",cwd:"/tmp",languageImage:{formatVersion:image.manifest.imageFormatVersion,version:image.manifest.imageVersion,sha256:image.aggregateSha256},runner:{name:"bun",version:"test",commit:"test"},harness:"agents-sdk",transport:"jsonl",recursionToken:"fixture",task,taskDigestSha256:await sha256(canonicalJson(task))};
 const input={adapterId:"agents-sdk/jsonl" as const,executable:"/sdk",model:"fixture",invocation,imageBytes:encodeRuntimeImage(image),expectedImageByteLength:image.manifest.modelVisibleBytes.byteLength,expectedImageSha256:image.manifest.modelVisibleBytes.sha256,framingTemplateBytes:image.files.get(image.manifest.oneFieldFraming.path)!,imagePath:"/image",taskPath:"/task",credentialGroup:"openai-api-key",platform:"darwin" as const,arch:"arm64" as const};
 const base=await buildInstalledLaunchPlan(input), selected=await buildInstalledLaunchPlan({...input,...fixture.override});
 expect(base.argv).not.toContain("--max-turns");expect(base.argv).not.toContain("--timeout");expect(selected.argv.slice(1,5)).toEqual(fixture.override.argv);expect(selected.argv.slice(5)).toEqual(base.argv.slice(1));
});
test("SDK native errors preserve only closed diagnostic data and never settle",()=>{
 for(const sample of fixture.errorCases){const p=installedProtocol("agents-sdk/jsonl","0.1.0","fixture");p.accept({type:"start",model:"fixture",cwd:"/tmp"});let caught:any;try{p.accept({type:"error",error_type:sample.error_type,elapsed_seconds:1.5,limits:fixture.historicalLimits});}catch(e){caught=e;}expect(caught.details.nativeFailure).toEqual({kind:sample.kind,elapsedSeconds:1.5,limits:fixture.historicalLimits});expect(p.terminalEventObserved).toBe(false);}
 expect(sdkNativeFailure({error_type:"secret",elapsed_seconds:Infinity,limits:{...fixture.defaults,secret:"value"}})).toEqual({kind:"execution"});
});

test("optional SDK tool timeout keeps parent and outer limits independent",async()=>{
 for(const sample of fixture.toolOverrides){
  const selected={harness:"agents-sdk",nativeToolTimeout:sample.value};
  expect(nativeLimits(selected)).toEqual({...production.nativeLimits,toolTimeoutSeconds:sample.seconds});
  expect(nativeLimitsArgv(selected)).toEqual(["--tool-timeout",String(sample.seconds)]);
 }
 for(const nativeToolTimeout of fixture.invalidTimeouts)expect(()=>nativeLimits({harness:"agents-sdk",nativeToolTimeout})).toThrow();
 for(const harness of ["claude","codex","prime","omp","mock"])expect(()=>nativeLimits({harness,nativeToolTimeout:"30s"})).toThrow();
 expect(()=>parseEntrypoint(["--native-tool-timeout","1s","--native-tool-timeout","2s","task"])).toThrow();
 const root=await mkdtemp(join(tmpdir(),"sdk-tool-limits-"));try{
  const userConfigPath=join(root,"user.toml");await writeFile(userConfigPath,'harness="agents-sdk"\nnative_tool_timeout="1s"\n');
  const parsed=parseEntrypoint(["--native-tool-timeout","180s","--timeout","10m","task"]);
  const c=await resolveConfiguration(parsed.global,{processCwd:root,userConfigPath,env:{PROSE_NATIVE_TOOL_TIMEOUT:"2s"}});
  expect(nativeLimits(c.values)).toEqual({...production.nativeLimits,toolTimeoutSeconds:180});
  expect(c.sources.nativeToolTimeout?.kind).toBe("flag");expect(c.values.timeout).toBe("10m");
 }finally{await rm(root,{recursive:true,force:true});}
});
