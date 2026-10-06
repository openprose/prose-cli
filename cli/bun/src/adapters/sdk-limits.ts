import {failure} from "../core/errors";
type Selection={harness?:string;adapterId?:string;nativeMaxTurns?:string;nativeTimeout?:string;nativeToolTimeout?:string};
export function nativeLimits(input:Selection):Record<string,number>|undefined {
 const sdk=input.harness==="agents-sdk"||input.adapterId==="agents-sdk/jsonl";
 if(!sdk){if(input.nativeMaxTurns!==undefined||input.nativeTimeout!==undefined||input.nativeToolTimeout!==undefined)throw failure("CONFIG_INVALID",{reason:"Native budget options are supported only by agents-sdk."});return undefined;}
 let turns=20,timeoutMs=180000,toolTimeoutMs=30000;
 if(input.nativeMaxTurns!==undefined){turns=Number(input.nativeMaxTurns);if(!/^[1-9][0-9]*$/.test(input.nativeMaxTurns)||!Number.isSafeInteger(turns))throw failure("CONFIG_INVALID",{reason:"Native max turns must be a positive safe integer."});}
 if(input.nativeTimeout!==undefined){const match=/^([1-9][0-9]*)(ms|s|m|h)$/.exec(input.nativeTimeout);if(!match)throw failure("CONFIG_INVALID",{reason:"Native timeout must be a positive duration."});timeoutMs=Number(match[1])*({ms:1,s:1000,m:60000,h:3600000}[match[2]!]!);if(!Number.isSafeInteger(timeoutMs))throw failure("CONFIG_INVALID",{reason:"Native timeout is outside the supported range."});}
 if(input.nativeToolTimeout!==undefined){const match=/^([1-9][0-9]*)(ms|s|m|h)$/.exec(input.nativeToolTimeout);if(!match)throw failure("CONFIG_INVALID",{reason:"Native timeout must be a positive duration."});toolTimeoutMs=Number(match[1])*({ms:1,s:1000,m:60000,h:3600000}[match[2]!]!);if(!Number.isSafeInteger(toolTimeoutMs))throw failure("CONFIG_INVALID",{reason:"Native timeout is outside the supported range."});}
 return {maxTurns:turns,timeoutSeconds:timeoutMs/1000,toolTimeoutSeconds:toolTimeoutMs/1000,maxOutputTokens:12000,maxAggregateRequests:turns,maxAggregateHostedWebCalls:turns,maxAggregateFunctionTools:80,maxObservedTotalTokens:500000,maxRequestInputBytes:256000,maxChildren:8,maxChildDepth:1};
}
export function nativeLimitsArgv(input:Selection):string[]{const limits=nativeLimits(input);if(!limits)return [];return [...(input.nativeMaxTurns===undefined?[]:["--max-turns",String(limits.maxTurns)]),...(input.nativeTimeout===undefined?[]:["--timeout",String(limits.timeoutSeconds)]),...(input.nativeToolTimeout===undefined?[]:["--tool-timeout",String(limits.toolTimeoutSeconds)])];}
export function sdkNativeFailure(record:Record<string,unknown>):Record<string,unknown>{
 const output:Record<string,unknown>={kind:record.error_type==="MaxTurnsExceeded"?"max-turns":record.error_type==="TimeoutError"?"timeout":"execution"};
 const elapsed=record.elapsed_seconds;if(typeof elapsed==="number"&&Number.isFinite(elapsed)&&elapsed>=0)output.elapsedSeconds=elapsed;
 const limits=record.limits;
 if(limits!==null&&typeof limits==="object"&&!Array.isArray(limits)){
  const v=limits as Record<string,unknown>,base=["maxTurns","timeoutSeconds","toolTimeoutSeconds","maxOutputTokens"],expanded=[...base,"maxAggregateRequests","maxAggregateHostedWebCalls","maxAggregateFunctionTools","maxObservedTotalTokens","maxRequestInputBytes","maxChildren","maxChildDepth"];
  const keys=Object.keys(v).length===4?base:expanded;
  if(Object.keys(v).length===keys.length&&keys.every(k=>typeof v[k]==="number"&&Number.isFinite(v[k])&&(v[k] as number)>0)&&keys.filter(k=>!["timeoutSeconds","toolTimeoutSeconds"].includes(k)).every(k=>Number.isSafeInteger(v[k]))&&(v.timeoutSeconds as number)<=Number.MAX_SAFE_INTEGER/1000&&(v.toolTimeoutSeconds as number)<=Number.MAX_SAFE_INTEGER/1000&&(keys===base||(v.maxChildren===8&&v.maxChildDepth===1)))output.limits=Object.fromEntries(keys.map(k=>[k,v[k]]));
 }
 return output;
}
