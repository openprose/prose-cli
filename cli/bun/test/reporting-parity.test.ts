import {test,expect} from "bun:test";
import {mkdtemp,rm} from "node:fs/promises";
import {join} from "node:path";
import {tmpdir} from "node:os";
import f from "../../shared/fixtures/config/optional-reporting.json";
import {resolveConfiguration} from "../src/core/config";
import {configurationExplanation,reportedConfigurationKeys} from "../src/core/output";
test("explanation reports inherited defaults while concise execution summaries omit optional defaults",async()=>{
 const dir=await mkdtemp(join(tmpdir(),"reporting-"));try{
 const deps={processCwd:dir,userConfigPath:join(dir,"absent"),env:{}};
 const base=await resolveConfiguration({},deps);expect(base.values.outputContract).toBe("image-envelope");expect(base.values.permissionMode).toBeNull();
 const values=(configurationExplanation(base).values as Record<string,unknown>);for(const [key,value] of Object.entries(f.explanationDefaults))expect(values[key]).toMatchObject({value,source:{kind:"default",location:"built-in"}});for(const key of f.defaultsOmitted){expect(values[key]).toMatchObject({source:{kind:"default",location:"built-in"}});expect(reportedConfigurationKeys(base)).not.toContain(key);}
 for(const c of f.cases){const config=await resolveConfiguration({...c,harness:"claude"},deps),report=configurationExplanation(config).values as any;for(const key of f.defaultsOmitted)expect(report[key]).toMatchObject({value:c[key as 'outputContract'|'permissionMode'],source:{kind:"flag"}});expect(report.outputContract.source.location).toBe("--output-contract");expect(report.permissionMode.source.location).toBe("--permission-mode");expect(config.values.outputContract).toBe(c.outputContract);expect(config.values.permissionMode).toBe(c.permissionMode);}
 }finally{await rm(dir,{recursive:true,force:true});}
});
