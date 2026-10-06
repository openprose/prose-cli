import Ajv2020 from "ajv/dist/2020";
import commonSchema from "../../shared/schemas/common.schema.json" with { type: "json" };
import explanationSchema from "../../shared/schemas/configuration-explanation.schema.json" with { type: "json" };
import { afterEach, expect, test } from "bun:test";
import { mkdtemp, mkdir, readFile, readdir, realpath, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import corpus from "../../shared/fixtures/config/production-v2.json" with { type: "json" };
import { runCli } from "../src/cli";
const ajv=new Ajv2020({allErrors:true,strict:true,strictTypes:false});
ajv.addSchema(commonSchema);const validateExplanation=ajv.compile(explanationSchema);
const roots: string[]=[];
async function treeFiles(root: string, prefix=""): Promise<string[]> {
  const result:string[]=[];
  for(const entry of await readdir(join(root,prefix),{withFileTypes:true})) {
    const path=prefix ? `${prefix}/${entry.name}` : entry.name;
    if(entry.isDirectory())result.push(...await treeFiles(root,path));else result.push(path);
  }
  return result.sort();
}
afterEach(async()=>{for(const root of roots.splice(0))await rm(root,{recursive:true,force:true});});
for(const fixture of corpus.cases) test(`production config black box: ${fixture.id}`,async()=>{
  const root=await realpath(await mkdtemp(join(tmpdir(),"prose-config-production-")));roots.push(root);
  for(const [path,bytes] of Object.entries(fixture.setup.files)) {
    const target=join(root,path);await mkdir(join(target,".."),{recursive:true});await writeFile(target,bytes);
  }
  for(const directory of fixture.setup.directories)await mkdir(join(root,directory),{recursive:true});
  const number=fixture.id.split("-").at(-1)!;
  const manifest=JSON.parse(await readFile(join(import.meta.dir,`../../conformance/cases/operations/config-production-${number}.json`),"utf8"));
  const expand=(value:string)=>value.replaceAll("{{WORKSPACE}}",root);
  let stdout="",stderr="",started=false;
  const exit=await runCli(manifest.invocation.argv,{processCwd:root,env:Object.fromEntries(Object.entries(manifest.invocation.environment).map(([key,value])=>[key,expand(value as string)])),clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:text=>{stderr+=text;},observeMockInvocation:()=>{started=true;}});
  expect(exit).toBe(manifest.expected.exitCode);expect(stderr).toBe("");expect(started).toBe(false);
  const report=JSON.parse(stdout);
  expect(report).toMatchObject(manifest.expected.resultMatches);
  const explanation=manifest.expected.exitCode===0 ? report:report.details?.configurationExplanation;
  expect(explanation).toBeDefined();expect(validateExplanation(explanation),JSON.stringify(validateExplanation.errors)).toBe(true);
  const checks=fixture.setup.checks as {unchangedFiles?:true|string[];absent?:string[];files?:Record<string,string>;outputAbsent?:string[]};
  const preserved=checks.unchangedFiles === true ? Object.keys(fixture.setup.files) : checks.unchangedFiles ?? [];
  for(const path of preserved)expect(await readFile(join(root,path),"utf8")).toBe((fixture.setup.files as Record<string,string>)[path]!);
  for(const [path,bytes] of Object.entries(checks.files ?? {}))expect(await readFile(join(root,path),"utf8")).toBe(bytes);
  expect(await treeFiles(root)).toEqual([...new Set([...Object.keys(fixture.setup.files),...Object.keys(checks.files ?? {})])].sort());
  for(const path of checks.absent ?? [])expect(await stat(join(root,path)).then(()=>true,()=>false)).toBe(false);
  for(const sentinel of checks.outputAbsent ?? [])expect(stdout+stderr).not.toContain(sentinel);
});

import { resolveConfiguration, mutateUserConfiguration } from "../src/core/config";
import { parseEntrypoint } from "../src/core/args";
import { nativeLimits } from "../src/adapters/sdk-limits";
import { nativeOutputLimits } from "../src/adapters/output-budget";
import { symlink } from "node:fs/promises";
async function freshConfigurationRoot(): Promise<string> {
  const root=await realpath(await mkdtemp(join(tmpdir(),"prose-config-boundaries-")));roots.push(root);return root;
}
test("Windows home and configuration-root discovery are static and environment-case independent",async()=>{
  const root=await freshConfigurationRoot();
  const configured=await resolveConfiguration({}, {processCwd:root,platform:"win32",env:{userprofile:"C:\\Users\\Fixture"}});
  expect(configured.userConfigPath).toBe("C:\\Users\\Fixture\\.prose\\cli.toml");
  const overridden=await resolveConfiguration({}, {processCwd:root,platform:"win32",env:{prose_config_dir:"D:\\Config Space"}});
  expect(overridden.userConfigPath).toBe("D:\\Config Space\\cli.toml");
  await expect(resolveConfiguration({}, {processCwd:root,platform:"win32",env:{USERPROFILE:"relative"}})).rejects.toMatchObject({code:"CONFIG_INVALID"});
});
test("unset absent settings creates neither settings nor parent and repeated unset is unchanged",async()=>{
  const root=await freshConfigurationRoot();const dependencies={processCwd:root,env:{HOME:join(root,"home")}};
  const absent=await resolveConfiguration({},dependencies);
  expect(await mutateUserConfiguration(absent,"unset",["model"])).toMatchObject({changed:false});
  expect(await stat(join(root,"home")).then(()=>true,()=>false)).toBe(false);
  await mkdir(join(root,"home/.prose"),{recursive:true});await writeFile(absent.userConfigPath,'# retained\nverbose = true\n');
  expect(await mutateUserConfiguration(await resolveConfiguration({},dependencies),"unset",["model"])).toMatchObject({changed:false});
  expect(await readFile(absent.userConfigPath,"utf8")).toBe('# retained\nverbose = true\n');
});
test("legacy-backed multiple unset preserves CRLF and unrelated explicit bytes",async()=>{
  const root=await freshConfigurationRoot();const dependencies={processCwd:root,env:{HOME:join(root,"home"),XDG_CONFIG_HOME:join(root,"legacy")}};
  const original='# original comment\r\nharness = "codex"\r\nmodel = "gpt-6.1-sol"\r\nauth_profile = "openai-api-key"\r\nverbose = true';
  const legacy=join(root,"legacy/openprose/cli.toml");await mkdir(join(legacy,".."),{recursive:true});await writeFile(legacy,original);
  const config=await resolveConfiguration({},dependencies);const receipt=await mutateUserConfiguration(config,"unset",["model","auth_profile"]);
  expect(receipt).toMatchObject({changed:true,sourcePath:legacy});expect(await readFile(legacy,"utf8")).toBe(original);
  expect(await readFile(config.userConfigPath,"utf8")).toBe('# original comment\r\nharness = "codex"\r\nverbose = true');
  const updated=await resolveConfiguration({},dependencies);expect(updated.values.authProfile).toBe("cached-chatgpt-login");expect(updated.values.model).toBe(null);
});
test("explicit unset refuses file and direct-parent symlinks and nonregular destinations",async()=>{
  const root=await freshConfigurationRoot();const dependencies={processCwd:root,env:{HOME:join(root,"home")}};
  const destination=join(root,"home/.prose/cli.toml");await mkdir(join(destination,".."),{recursive:true});
  const target=join(root,"original.toml");await writeFile(target,'verbose = true\n');await symlink(target,destination);
  await expect(mutateUserConfiguration(await resolveConfiguration({},dependencies),"unset",["verbose"])).rejects.toMatchObject({code:"CONFIG_INVALID"});
  expect(await readFile(target,"utf8")).toBe('verbose = true\n');await rm(destination);await mkdir(destination);
  await expect(mutateUserConfiguration(await resolveConfiguration({},dependencies),"unset",["verbose"])).rejects.toMatchObject({code:"CONFIG_INVALID"});
});
test("same-layer replacements establish a coherent SDK route and execution-derived budgets",async()=>{
  const root=await freshConfigurationRoot();const user=join(root,"home/.prose/cli.toml");await mkdir(join(user,".."),{recursive:true});await writeFile(user,'harness = "codex"\nmodel = "native-choice"\nauth_profile = "cached-chatgpt-login"\n');
  const config=await resolveConfiguration({harness:"agents-sdk",model:"gpt-6.1-sol",authProfile:"openai-api-key",nativeTimeout:"5m",nativeToolTimeout:"10s",nativeMaxTurns:"8",outputContract:"native",nativeOutputBytes:"1048576"},{processCwd:root,env:{HOME:join(root,"home")}});
  expect(config.runtime?.nativeLimits).toEqual(nativeLimits(config.values));expect(config.runtime?.nativeOutputLimits).toEqual(nativeOutputLimits(config.values));
  expect(config.values.harness).toBe("agents-sdk");expect(config.candidates?.authProfile?.at(-1)?.selected).toBe(true);
  expect(parseEntrypoint(["cli","config","unset","harness","model","auth_profile","--json"])).toMatchObject({configKeys:["harness","model","auth_profile"]});
});

test("unset repairs a removed invalid value before effective resolution",async()=>{
  const root=await freshConfigurationRoot();const user=join(root,"home/.prose/cli.toml");await mkdir(join(user,".."),{recursive:true});await writeFile(user,'# preserve\ntimeout = "invalid-duration"\nverbose = true\n');
  let stdout="";
  expect(await runCli(["cli","config","unset","timeout","--json"],{processCwd:root,env:{HOME:join(root,"home")},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:()=>{}})).toBe(0);
  expect(JSON.parse(stdout)).toMatchObject({mutation:{changed:true,operation:"unset"},values:{timeout:{value:"10m"},verbose:{value:true}}});
  expect(await readFile(user,"utf8")).toBe('# preserve\nverbose = true\n');
});
test("post-migration execution-override errors retain the successful mutation receipt",async()=>{
  const root=await freshConfigurationRoot();const legacy=join(root,"legacy/openprose/cli.toml");await mkdir(join(legacy,".."),{recursive:true});await writeFile(legacy,'# explicit preference\ntimeout = "2m"\n');
  let stdout="";
  expect(await runCli(["cli","config","migrate","--json"],{processCwd:root,env:{HOME:join(root,"home"),XDG_CONFIG_HOME:join(root,"legacy"),PROSE_OUTPUT:"invalid"},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:()=>{}})).toBe(2);
  expect(JSON.parse(stdout)).toMatchObject({code:"CONFIG_INVALID",details:{source:"PROSE_OUTPUT",mutation:{operation:"migrate",changed:true}}});
  expect(await readFile(join(root,"home/.prose/cli.toml"),"utf8")).toBe(await readFile(legacy,"utf8"));
});
test("a semantically rejected credential-profile value never appears in partial diagnostics",async()=>{
  const root=await freshConfigurationRoot();let stdout="",stderr="";
  expect(await runCli(["cli","config","explain","--json","--","--harness","agents-sdk","--auth-profile","fixture-secret-profile-value","run","x"],{processCwd:root,env:{HOME:join(root,"home")},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:text=>{stderr+=text;}})).toBe(2);
  expect(stdout+stderr).not.toContain("fixture-secret-profile-value");expect(JSON.parse(stdout)).toMatchObject({details:{source:"--auth-profile",configurationExplanation:{diagnostics:[{code:"CONFIG_INVALID",severity:"error"}]}}});
});
test("an overridden invalid credential-profile candidate is omitted with a safe source diagnostic",async()=>{
  const root=await freshConfigurationRoot();const user=join(root,"home/.prose/cli.toml");await mkdir(join(user,".."),{recursive:true});await writeFile(user,'harness = "agents-sdk"\nauth_profile = "rejected-profile-sentinel"\n');
  let stdout="",stderr="";
  expect(await runCli(["cli","config","explain","--json","--","--auth-profile","openai-api-key","run","x"],{processCwd:root,env:{HOME:join(root,"home")},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:text=>{stderr+=text;}})).toBe(0);
  expect(stdout+stderr).not.toContain("rejected-profile-sentinel");const report=JSON.parse(stdout);
  expect(report).toMatchObject({values:{authProfile:{value:"openai-api-key"}},diagnostics:[{code:"CONFIG_CANDIDATE_INVALID",severity:"warning",source:`${user}:2`,reason:"Incompatible overridden authentication profile is omitted."}]});
  expect(report.candidates.authProfile).toHaveLength(2);expect(validateExplanation(report),JSON.stringify(validateExplanation.errors)).toBe(true);
});
for(const permission of ["default","acceptEdits","workspace-write","read-only"])test(`SDK explanation rejects unsupported explicit permission ${permission} before runtime inference`,async()=>{
  const root=await freshConfigurationRoot();let stdout="",stderr="";
  expect(await runCli(["cli","config","explain","--json","--","--harness","agents-sdk","--permission-mode",permission,"run","x"],{processCwd:root,env:{HOME:join(root,"home")},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:text=>{stderr+=text;}})).toBe(2);
  const report=JSON.parse(stdout);expect(report).toMatchObject({code:"CONFIG_INVALID",details:{adapterId:"agents-sdk/jsonl",source:"--permission-mode",reason:"Unsupported explicit permission mode for this harness.",configurationExplanation:{values:{permissionMode:{value:null}},runtime:{transport:null,permissionMode:null,authProfile:null,billingOwner:null,nativeLimits:null,nativeOutputLimits:null}}}});
  expect(validateExplanation(report.details.configurationExplanation),JSON.stringify(validateExplanation.errors)).toBe(true);
});
test("SDK saved harness selection reports contextual defaults without persisting them",async()=>{
  const root=await freshConfigurationRoot();let stdout="";
  expect(await runCli(["cli","harness","use","agents-sdk"],{processCwd:root,env:{HOME:join(root,"home")},clock:{now:()=>"2025-01-01T00:00:00Z",monotonicMs:()=>0},ids:{invocationId:()=>"fixture-invocation-0001"},writeStdout:text=>{stdout+=text;},writeStderr:()=>{}})).toBe(0);
  expect(stdout).toContain("Route: openai-api-key (Agents SDK default; not saved)");expect(stdout).toContain("Model: gpt-6.1-sol (Agents SDK default; not saved)");
  expect(await readFile(join(root,"home/.prose/cli.toml"),"utf8")).toBe('harness = "agents-sdk"\n');
});
