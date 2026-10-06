import {test,expect} from "bun:test";
import {mkdtemp,writeFile,rm,mkdir,readFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import fixture from "../../shared/fixtures/native-output-budget.json";
import {nativeOutputBytes,nativeOutputLimits,validateNativeOutputBytes} from "../src/adapters/output-budget";
import {parseEntrypoint} from "../src/core/args";
import {resolveConfiguration} from "../src/core/config";
import {NativeCapture} from "../src/adapters/native-capture";
test("native output shared bounds and mode",()=>{
 expect(nativeOutputBytes({})).toBe(fixture.default);
 for(const v of fixture.valid)expect(validateNativeOutputBytes(v)).toBe(Number(v));
 for(const v of fixture.invalid)expect(()=>validateNativeOutputBytes(v)).toThrow();
 expect(()=>nativeOutputLimits({nativeOutputBytes:"1048576"})).toThrow();
 expect(nativeOutputLimits({outputContract:"native"})).toEqual({maxRecordBytes:1048576,maxAggregateStdoutBytes:fixture.default,maxNativeCaptureBytes:fixture.default,captureEnabled:false});
 expect(()=>parseEntrypoint(["--native-output-bytes","1048576","--native-output-bytes","2097152","task"])).toThrow();
});
test("native output provenance follows file environment flag precedence",async()=>{
 const root=await mkdtemp(join(tmpdir(),"output-budget-"));try{
 const userConfigPath=join(root,"user.toml");await writeFile(userConfigPath,'output_contract="native"\nnative_output_bytes="1048576"\n');
 const dep={processCwd:root,userConfigPath,env:{}};
 expect(nativeOutputBytes((await resolveConfiguration({},dep)).values)).toBe(1048576);
 const env={PROSE_NATIVE_OUTPUT_BYTES:"134217728"};
 expect(nativeOutputBytes((await resolveConfiguration({},{...dep,env})).values)).toBe(134217728);
 const c=await resolveConfiguration(parseEntrypoint(["--native-output-bytes=268435456","task"]).global,{...dep,env});
 expect(nativeOutputBytes(c.values)).toBe(268435456);expect(c.sources.nativeOutputBytes?.location).toBe("--native-output-bytes");
 await expect(resolveConfiguration({outputContract:"image-envelope"},{...dep,env})).rejects.toThrow();
 }finally{await rm(root,{recursive:true,force:true});}
});
test("native capture counts serialized UTF8 and redaction independently without partial overflow",async()=>{
 const root=await mkdtemp(join(tmpdir(),"capture-budget-"));try{
 const p=join(root,"capture");const r={text:"éx"};const expected=JSON.stringify({text:"é[REDACTED]"})+"\n";const n=Buffer.byteLength(expected);
 const c=new NativeCapture(p,["x"],n);c.write(r);expect(()=>c.write({text:"next"})).toThrow();c.close();expect(await readFile(p,"utf8")).toBe(expected);
 const disabled=new NativeCapture(undefined,[],1);disabled.write({text:"unlogged"});disabled.close();
 }finally{await rm(root,{recursive:true,force:true});}
});
