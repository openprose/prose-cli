import {PrimeDrain} from "./prime-drain";
import { isDeepStrictEqual } from "node:util";
import { failure } from "../core/errors";
import type { RawTransportEvent } from "../supervision/types";

const bad = (): never => { throw failure("PROTOCOL_MALFORMED", { reason: "Native tool lifecycle is malformed or out of order." }); };
const object = (v: unknown): Record<string, any> => v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, any> : bad();
const same = (a: unknown, b: unknown): boolean => isDeepStrictEqual(a,b);
// OMP's schema validation may omit optional null fields before tool execution.
// This is transport compatibility, not a claim of schema equivalence.
export function nativeArgsMatch(actual: unknown, declared: unknown, omp:boolean):boolean {
 if(!omp)return same(actual,declared);
 if(Array.isArray(actual)&&Array.isArray(declared))return actual.length===declared.length&&actual.every((v,i)=>nativeArgsMatch(v,declared[i],true));
 if(actual&&declared&&typeof actual==="object"&&typeof declared==="object"&&!Array.isArray(actual)&&!Array.isArray(declared)){
   const a=actual as Record<string,unknown>,d=declared as Record<string,unknown>;
   return Object.keys(a).every(k=>Object.hasOwn(d,k)&&nativeArgsMatch(a[k],d[k],true))&&Object.keys(d).every(k=>Object.hasOwn(a,k)||d[k]===null||d[k]==="null");
 }
 return same(actual,declared);
}

// Only the two advertised OMP task agent default locations are recognized.
export function ompTaskDefaults(tools: any[]): {root:boolean;items:boolean} {
 const tasks=tools.filter(t=>t?.name==="task");
 const p=tasks.length===1?tasks[0].parameters:null;
 const field=(v:any)=>v?.type==="string"&&v.default==="task";
 return {root:p?.type==="object"&&field(p.properties?.agent),items:p?.type==="object"&&p.properties?.tasks?.type==="array"&&p.properties.tasks.items?.type==="object"&&field(p.properties.tasks.items.properties?.agent)};
}
export function taskArgsMatch(actual:any,declared:any,omp:boolean,name:string,defaults:{root:boolean;items:boolean}):boolean {
 if(!omp||name!=="task"||(!defaults.root&&!defaults.items))return nativeArgsMatch(actual,declared,omp);
 const d=structuredClone(declared);
 const apply=(a:any,b:any)=>{if(a&&b&&typeof a==="object"&&typeof b==="object"&&!Array.isArray(a)&&!Array.isArray(b)&&!Object.hasOwn(b,"agent")&&a.agent==="task")b.agent="task";};
 if(defaults.root)apply(actual,d);
 if(defaults.items&&Array.isArray(actual?.tasks)&&Array.isArray(d?.tasks)&&actual.tasks.length===d.tasks.length)actual.tasks.forEach((a:any,i:number)=>apply(a,d.tasks[i]));
 return nativeArgsMatch(actual,d,true);
}

// Source-defined OMP input messages; content is opaque to the runner.
function validCustom(m: Record<string, any>): boolean {
 const keys=["role","customType","content","display","details","attribution","timestamp"];
 if(Object.keys(m).some(k=>!keys.includes(k))||m.role!=="custom"||typeof m.customType!=="string"||typeof m.display!=="boolean"||typeof m.timestamp!=="number"||!Number.isFinite(m.timestamp)||m.timestamp<0||("attribution" in m&&!["user","agent"].includes(m.attribution)))return false;
 const serial=(v:any):boolean=>v===null||typeof v==="string"||typeof v==="boolean"||(typeof v==="number"&&Number.isFinite(v))||(Array.isArray(v)?v.every(serial):v&&typeof v==="object"&&Object.values(v).every(serial));
 if("details" in m&&!serial(m.details))return false;
 return typeof m.content==="string"||(Array.isArray(m.content)&&m.content.every((b:any)=>{
  if(!b||typeof b!=="object"||Array.isArray(b))return false;
  if(b.type==="text")return Object.keys(b).every(k=>["type","text","textSignature"].includes(k))&&typeof b.text==="string"&&(!("textSignature" in b)||typeof b.textSignature==="string");
  if(b.type!=="image"||Object.keys(b).some(k=>!["type","data","mimeType","detail","providerFile","url"].includes(k))||typeof b.data!=="string"||typeof b.mimeType!=="string"||("url" in b&&typeof b.url!=="string")||("detail" in b&&!["auto","low","high","original"].includes(b.detail)))return false;
  if("providerFile" in b){const f=b.providerFile;if(!f||typeof f!=="object"||Array.isArray(f)||Object.keys(f).some(k=>!["provider","id","uri","expiresAt"].includes(k))||!["openai","anthropic","google"].includes(f.provider)||["id","uri"].some(k=>k in f&&typeof f[k]!=="string")||("expiresAt" in f&&(typeof f.expiresAt!=="number"||!Number.isFinite(f.expiresAt))))return false;}
  return true;
 }));
}

function validPrimeQueue(r: Record<string, any>): boolean {
 if(Object.keys(r).some(k=>!["type","actions"].includes(k)))return false;
 const a=r.actions;if(!a||typeof a!=="object"||Array.isArray(a)||Object.keys(a).some(k=>!["queuedCount","steering","followUps","active"].includes(k)))return false;
 if(!Number.isSafeInteger(a.queuedCount)||a.queuedCount<0||["steering","followUps"].some(k=>!Array.isArray(a[k])||a[k].some((v:unknown)=>typeof v!=="string")))return false;
 if("active" in a){const x=a.active;if(!x||typeof x!=="object"||Array.isArray(x)||Object.keys(x).some(k=>!["kind","phase","label"].includes(k))||!["turn","session_command"].includes(x.kind)||!["preparing","committing","running"].includes(x.phase)||("label" in x&&typeof x.label!=="string"))return false;}
 return true;
}

function validPrimeChildUpdate(r: Record<string, any>): boolean {
 if(Object.keys(r).some(k=>!["type","child"].includes(k)))return false;
 const c=r.child;
 if(!c||typeof c!=="object"||Array.isArray(c))return false;
 const strings=["parentId","activeSessionId","sessionName","model","answerPreview","recap","error"];
 const fields=["id","label","status","sessionDir","durationMs","toolUseCount","tokenCount","activity","repliedSinceTask",...strings];
 if(Object.keys(c).some(k=>!fields.includes(k))||typeof c.id!=="string"||!c.id||typeof c.sessionDir!=="string"||!c.sessionDir||typeof c.label!=="string"||!["queued","running","done","error","cancelled"].includes(c.status))return false;
 if(strings.some(k=>k in c&&typeof c[k]!=="string"))return false;
 if("durationMs" in c&&(typeof c.durationMs!=="number"||!Number.isFinite(c.durationMs)||c.durationMs<0))return false;
 if(["toolUseCount","tokenCount"].some(k=>k in c&&(!Number.isSafeInteger(c[k])||c[k]<0)))return false;
 if("repliedSinceTask" in c&&typeof c.repliedSinceTask!=="boolean")return false;
 if("activity" in c){const a=c.activity;if(!a||typeof a!=="object"||Array.isArray(a)||Object.keys(a).some(k=>!["kind","toolName"].includes(k))||!["waiting","writing","executing"].includes(a.kind)||("toolName" in a&&typeof a.toolName!=="string"))return false;}
 return true;
}

/** Native transport bookkeeping only. No language meaning or synthetic settlement. */
export class NativeToolLifecycle {
  started = false;
  ended = false;
  private turn = false;
  private asyncTasks = new Map<string,{args:unknown;job:string}>();
  private user = false;
  private open: Record<string, any> | null = null;
  private assistant: Record<string, any> | null = null;
  private history: Record<string, any>[] = [];
  private omittedResults = new Map<number, {id:string; name:string; result:any; isError:boolean; started:Record<string,any>|null}>();

  private corroboratedHistory(messages: any[]): any[] | null {
    if(this.omittedResults.size===0)return this.history;
    const count=this.history.length+this.omittedResults.size;
    const offset=messages.length-count;
    if(offset!==0 && !(offset===1 && this.primeDrain?.resumed))return null;
    const projected:any[]=[];let observed=0;
    for(let i=0;i<count;i++){
      const expected=this.omittedResults.get(i);
      if(!expected){projected.push(this.history[observed++]);continue;}
      const m=messages[offset+i];
      if(!m||typeof m!=="object"||Array.isArray(m)
        ||Object.keys(m).some(k=>!["role","toolCallId","toolName","content","isError","details","timestamp"].includes(k))
        ||(expected.started!==null&&!same(m,expected.started))
        ||m.role!=="toolResult"||m.toolCallId!==expected.id||m.toolName!==expected.name
        ||m.isError!==expected.isError||!same(m.content,expected.result.content)
        ||!same(m.details,expected.result.details)
        ||("timestamp" in m && (typeof m.timestamp!=="number"||!Number.isFinite(m.timestamp)||m.timestamp<0)))return null;
      // This is a projection for checking producer history, not an emitted event.
      projected.push(m);
    }
    return projected;
  }

  private calls = new Map<string, { name: string; args: unknown; state: string; result?: any; isError?: boolean }>();
  private results: Record<string, any>[] = [];
  private blockTypes = new Map<number,string>();
  private lastStop: string | null = null;
  constructor(private readonly omp: boolean, readonly primeDrain:PrimeDrain|null=null, private readonly taskDefaults={root:false,items:false}) {}

  settlePrime(exitCode:number|null):RawTransportEvent|null {
    if(!this.primeDrain)return null;
    if(exitCode!==0||!this.primeDrain.candidate||((this.primeDrain.resumed||this.primeDrain.queueObserved)&&!this.primeDrain.queueEmpty)||this.turn||this.open)throw failure("PROTOCOL_TRUNCATED",{reason:"Prime native operation did not drain with a fresh final result."});
    this.ended=true;return {type:"session.completed"};
  }

  get phase(): "tool-await-agent-start" | "tool-await-next-turn" | "tool-message-open" | "tool-turn-open" | "tool-await-agent-end" | "complete" {
    return this.ended ? "complete" : !this.started ? "tool-await-agent-start" : this.open ? "tool-message-open" : this.turn ? "tool-turn-open" : this.lastStop === "toolUse" ? "tool-await-next-turn" : "tool-await-agent-end";
  }

  private sameMessage(a: any,b: any): boolean {
    if (!this.omp || a?.role === "custom" || b?.role === "custom") return same(a,b);
    const x={...a},y={...b};delete x.completedAt;delete y.completedAt;
    if(x.role==="toolResult" && typeof x.prunedAt==="number" && Number.isFinite(x.prunedAt) && x.prunedAt>=0 && ["[Superseded by a newer read of this file]","[Uneventful result elided]"].some(text=>same(x.content,[{type:"text",text}]))) {delete x.prunedAt;x.content=y.content;}
    return same(x,y);
  }
  accept(value: unknown): RawTransportEvent | null {
    const r = object(value);
    if (this.ended) bad();
    if(this.omp && r.type==="tool_execution_update" && this.asyncTasks.has(r.toolCallId)) {
      const task=this.asyncTasks.get(r.toolCallId)!;const a=r.partialResult?.details?.async;
      if(!this.started||r.toolName!=="task"||!taskArgsMatch(r.args,task.args,true,"task",this.taskDefaults)||!a||a.type!=="task"||a.jobId!==task.job||!["running","completed","failed"].includes(a.state))bad();
      return null;
    }
    if(this.primeDrain?.queuedContinuationPending){
      if(!this.primeDrain.permitsPendingEvent(r.type))bad();
      const keys=r.type==="session_action_update"?["type","actions"]:
        r.type==="message_start"||r.type==="message_end"?["type","message"]:["type"];
      if(Object.keys(r).length!==keys.length||Object.keys(r).some(k=>!keys.includes(k)))bad();
    }
    if(this.primeDrain?.candidate&&r.type!=="session_action_update")bad();
    switch (r.type) {
      case "session_action_update":
        if(this.omp||!this.started||!validPrimeQueue(r))bad();
        if(this.primeDrain&&!this.primeDrain.queue(r.actions))bad();
        return null;
      case "rlm_child_update":
        if(this.omp||!this.started||!validPrimeChildUpdate(r))bad();
        if(this.primeDrain){if(this.primeDrain.candidate)bad();this.primeDrain.child(r.child);}
        return null;
      case "agent_start":
        if(this.primeDrain?.queuedContinuationPending){
          if(!this.started||this.turn||this.open||!this.primeDrain.beginQueuedAgent())bad();
          this.history=[];this.omittedResults.clear();this.user=false;this.assistant=null;this.lastStop=null;
          this.calls.clear();this.results=[];this.blockTypes.clear();return null;
        }
        if (this.started) bad();
        this.started = true;
        return { type: "session.started" };
      case "turn_start":
        if(this.primeDrain?.queuedContinuationPending&&!this.primeDrain.beginQueuedTurn())bad();
        if (!this.started || this.turn || this.open || (this.lastStop !== null && this.lastStop !== "toolUse")) bad();
        this.turn = true; this.assistant = null; this.calls.clear(); this.results = [];
        return null;
      case "message_start": {
        const m = object(r.message);
        if(this.primeDrain?.segmentClosed){
          if(this.lastStop!=="toolUse")bad();
          this.primeDrain.segmentClosed=false;this.primeDrain.resumed=true;this.primeDrain.candidate=false;this.primeDrain.queueEmpty=false;this.history=[];this.omittedResults.clear();
        }
        // A single completed tool can lose both result notifications and turn
        // markers. Admit continuation provisionally; final producer history must
        // corroborate its identity, content, details and error before settlement.
        if(this.primeDrain && this.started && this.turn && (!this.open||this.open.role==="toolResult")
          && this.assistant?.stopReason==="toolUse" && m.role==="assistant"
          && Array.isArray(m.content) && m.content.length===0
          && this.calls.size===1 && this.results.length===0){
          const [id,c]=[...this.calls][0]!;
          if(c.state==="ended" && Array.isArray(c.result?.content) && typeof c.isError==="boolean"){
            this.omittedResults.set(this.history.length+this.omittedResults.size,{id,name:c.name,result:c.result,isError:c.isError,started:this.open});
            this.open=null;
            this.lastStop="toolUse";this.turn=true;this.assistant=null;this.calls.clear();this.results=[];
          }
        }
        // Native Prime may lose both markers after all observed tool results.
        // Infer parser state only; preserve history and emit no replacement events.
        if(this.primeDrain && this.started && this.turn && !this.open
          && this.assistant?.stopReason==="toolUse" && m.role==="assistant"
          && Array.isArray(m.content) && m.content.length===0 && this.calls.size>0
          && this.results.length===this.calls.size && [...this.calls.values()].every(c=>c.state==="reported")) {
          this.lastStop="toolUse";this.turn=false;
        }
        // Prime 0.7 has emitted this boundary without its turn_start marker.
        // Only a fully settled tool turn permits this empty assistant start.
        if (!this.omp && this.started && !this.turn && !this.open && this.lastStop === "toolUse"
          && m.role === "assistant" && Array.isArray(m.content) && m.content.length === 0
          && this.calls.size > 0 && [...this.calls.values()].every(c=>c.state==="reported")) {
          this.turn=true; this.assistant=null; this.calls.clear(); this.results=[];
        }
        if (!this.turn || this.open) bad();
        if (m.role === "user") { if (this.user || this.assistant) bad(); }
        else if (m.role === "custom") {
          if(this.omp){if(this.assistant||!validCustom(m))bad();}
          else if(this.assistant||!this.primeDrain?.startQueuedMessage(m))bad();
        }
        else if (m.role === "assistant") { if (!this.user || this.assistant) bad(); this.blockTypes.clear(); }
        else if (m.role === "toolResult") {
          const c = this.calls.get(m.toolCallId) ?? bad();
          if (!c || c.state !== "ended" || c.name !== m.toolName) bad();
        } else bad();
        this.open = m; return null;
      }
      case "message_update": {
        const m = object(r.message), e = object(r.assistantMessageEvent);
        if (!this.open || this.open.role !== "assistant" || m.role !== "assistant") bad();
        if (!["text_start","text_delta","text_end","thinking_start","thinking_delta","thinking_end","toolcall_start","toolcall_delta","toolcall_end"].includes(e.type)) bad();
        if (!Number.isInteger(e.contentIndex) || e.contentIndex < 0 || !Array.isArray(m.content)) bad();
        const block = m.content[e.contentIndex];
        const expected = e.type.startsWith("toolcall_") ? "toolCall" : e.type.split("_")[0];
        if (!block || block.type !== expected || (this.blockTypes.has(e.contentIndex) && this.blockTypes.get(e.contentIndex)!==expected)) bad();
        this.blockTypes.set(e.contentIndex,expected);
        if (e.type.endsWith("_delta") && typeof e.delta !== "string") bad();
        return null;
      }
      case "message_end": {
        const m = object(r.message);
        if (!this.open || m.role !== this.open.role || (m.role !== "custom" && !Array.isArray(m.content))) bad();
        if (m.role === "user") { if (!same(m,this.open)) bad(); this.user = true; }
        else if (m.role === "custom") {
          if(!same(m,this.open))bad();
          if(this.omp){if(!validCustom(m))bad();}
          else {if(!this.primeDrain?.endQueuedMessage(m))bad();this.user=true;}
        }
        else if (m.role === "assistant") {
          if ([...this.blockTypes].some(([i,t])=>m.content[i]?.type!==t)) bad();
          if (!["stop","toolUse"].includes(m.stopReason)) throw failure("HARNESS_FAILED", { reason: "Native assistant did not finish normally." });
          for (const b0 of m.content) {
            const b = object(b0);
            if (b.type === "toolCall") {
              if (typeof b.id !== "string" || !b.id || typeof b.name !== "string" || !b.name || this.calls.has(b.id)) bad();
              object(b.arguments);
              const args = {...b.arguments};
              if (this.omp && typeof b.intent === "string" && args.i === b.intent) delete args.i;
              this.calls.set(b.id,{name:b.name,args,state:"declared"});
            } else if (b.type === "text") { if (typeof b.text !== "string") bad(); }
            else if (b.type === "thinking") { if (typeof b.thinking !== "string") bad(); }
            else bad();
          }
          if ((m.stopReason === "toolUse") !== (this.calls.size > 0)) bad();
          this.assistant = m;
        } else {
          const c = this.calls.get(m.toolCallId) ?? bad();
          if (!same(m,this.open) || !c || c.state !== "ended" || c.name !== m.toolName || typeof m.isError !== "boolean") bad();
          if (!same(m.content,c.result?.content) || m.isError!==c.isError) bad();
          c.state = "reported"; this.results.push(m);
        }
        this.history.push(m); this.open = null;
        return m.role === "assistant" ? {type:"assistant.message",text:m.content.filter((b:any)=>b.type==="text").map((b:any)=>b.text).join("")} : null;
      }
      case "tool_execution_start": case "tool_execution_update": case "tool_execution_end": {
        const c = this.calls.get(r.toolCallId) ?? bad();
        if (!this.turn || this.open || !c || r.toolName !== c.name) bad();
        if (r.type === "tool_execution_start") {
          if (c.state !== "declared" || !taskArgsMatch(r.args,c.args,this.omp,c.name,this.taskDefaults)) bad(); c.state = "started";
        } else if (r.type === "tool_execution_update") {
          if (c.state !== "started" || !taskArgsMatch(r.args,c.args,this.omp,c.name,this.taskDefaults)) bad(); object(r.partialResult);
        } else {
          if (c.state !== "started" || typeof r.isError !== "boolean") bad(); object(r.result); c.result=r.result; c.isError=r.isError; c.state = "ended";
          const a=r.result.details?.async;
          if(this.omp&&c.name==="task"&&!r.isError&&a?.type==="task"&&a.state==="running"&&typeof a.jobId==="string"&&a.jobId) this.asyncTasks.set(r.toolCallId,{args:c.args,job:a.jobId});
        }
        return null;
      }
      case "turn_end":
        if (!this.turn || this.open || !this.assistant || !this.sameMessage(r.message,this.assistant) || !same(r.toolResults,this.results) || [...this.calls.values()].some(c=>c.state!=="reported")) bad();
        this.lastStop = this.assistant!.stopReason; this.turn = false; return null;
      case "agent_end":
        if(this.primeDrain){
          if(!this.started||this.turn||this.open||!["stop","toolUse"].includes(this.lastStop??"")||[...this.calls.values()].some(c=>c.state!=="reported")||this.primeDrain.segmentClosed||!Array.isArray(r.messages))bad();
          const corroborated=this.corroboratedHistory(r.messages);
          if(corroborated===null||!this.primeDrain.history(r.messages,corroborated))bad();
          if(!this.primeDrain.closeSegment(this.lastStop!))bad();return null;
        }
        if (!this.started || this.turn || this.open || this.lastStop !== "stop" || (!Array.isArray(r.messages) || r.messages.length!==this.history.length || r.messages.some((m:any,i:number)=>!this.sameMessage(m,this.history[i])))) bad();
        if (this.omp && r.isTerminal !== true) throw failure("HARNESS_FAILED", {reason:"unsupported_nonterminal_settlement"});
        this.ended = true; return {type:"session.completed"};
      default: return bad();
    }
  }
}

export function hasNativeTools(value: unknown): boolean {
  const r = value as any;
  return Array.isArray(r?.message?.content) && r.message.content.some((b:any)=>b?.type==="toolCall" || Number.isInteger(b?.index))
    || typeof r?.assistantMessageEvent?.type === "string" && r.assistantMessageEvent.type.startsWith("toolcall_")
    || typeof r?.type === "string" && r.type.startsWith("tool_execution_");
}
