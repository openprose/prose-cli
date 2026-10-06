/** Closed, bounded public projections; provider records and identifiers stay private. */
export interface SdkObservations {
  usageObservation?: Record<string,unknown>;
  modelIdentity?: Record<string,unknown>;
}
const tokenKeys=["input_tokens","output_tokens","total_tokens","input_tokens_details.cached_tokens","input_tokens_details.cache_write_tokens","output_tokens_details.reasoning_tokens"];
const countKeys=["startedCallCount","completedResponseCount","duplicateResponseCallbackCount","outstandingCallCount"];
const tiers=new Set(["auto","default","flex","scale","priority","fast","ultrafast"]);
const record=(value:unknown):value is Record<string,unknown>=>value!==null&&typeof value==="object"&&!Array.isArray(value);
const counter=(value:unknown):value is number=>Number.isSafeInteger(value)&&typeof value==="number"&&value>=0;
const identifier=(value:unknown):value is string=>typeof value==="string"&&value.length<=128&&/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(value);
function counters(value:Record<string,unknown>):Record<string,number>{return Object.fromEntries(tokenKeys.filter(key=>counter(value[key])).map(key=>[key,value[key] as number]));}
export function sdkObservations(value:Record<string,unknown>,requestedModel:string|null):SdkObservations {
  const result:SdkObservations={};
  const usage=value.usageObservation;
  if(record(usage)&&usage.source==="sdk_completed_response_raw_usage"&&usage.aggregationScope==="unique_completed_responses_in_parent_and_children"&&countKeys.every(key=>counter(usage[key]))&&usage.outstandingProviderRequestCount===null&&usage.totalRunUsageKnown===false&&record(usage.observedTokenTotals)&&record(usage.fieldResponseCounts)) {
    result.usageObservation={source:usage.source,aggregationScope:usage.aggregationScope,...Object.fromEntries(countKeys.map(key=>[key,usage[key]])),outstandingProviderRequestCount:null,observedTokenTotals:counters(usage.observedTokenTotals),fieldResponseCounts:counters(usage.fieldResponseCounts),totalRunUsageKnown:false};
  }
  const model=value.modelIdentity;
  if(record(model)&&identifier(requestedModel)&&Array.isArray(model.observed)&&record(model.serviceTier)&&model.serviceTier.requested==="default"&&Array.isArray(model.serviceTier.observed)) {
    const observed=[...new Set(model.observed.filter(identifier))].sort().slice(0,128);
    const observedTiers=[...new Set(model.serviceTier.observed.filter((tier):tier is string=>typeof tier==="string"&&tiers.has(tier)))].sort();
    result.modelIdentity={requested:requestedModel,observed,serviceTier:{requested:"default",observed:observedTiers}};
  }
  return result;
}
