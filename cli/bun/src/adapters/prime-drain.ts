import {isDeepStrictEqual as same} from "node:util";
export function primeHistorySame(a:any,b:any):boolean {
  if(a?.role!=="assistant"||b?.role!=="assistant"||(!("usage" in a)&&!("usage" in b)))return same(a,b);
  const nums=(v:any,keys:string[])=>v&&typeof v==="object"&&!Array.isArray(v)&&Object.keys(v).length===keys.length&&keys.every(k=>typeof v[k]==="number"&&Number.isFinite(v[k])&&v[k]>=0);
  const valid=(u:any)=>u&&typeof u==="object"&&!Array.isArray(u)&&Object.keys(u).length===6&&nums(Object.fromEntries(Object.entries(u).filter(([k])=>k!=="cost")),["input","output","cacheRead","cacheWrite","totalTokens"])&&nums(u.cost,["input","output","cacheRead","cacheWrite","total"]);
  if(!valid(a.usage)||!valid(b.usage))return false;
  const x={...a},y={...b};delete x.usage;delete y.usage;return same(x,y);
}

type QueuedPhase = "preparing" | "committing" | "agent-start" | "running"
  | "turn-start" | "custom-start" | "custom-end";

/** Native producer observations only; this is not authenticated agent identity. */
export class PrimeDrain {
  segmentClosed=false; resumed=false; candidate=false; queueEmpty=false; queueObserved=false;
  private children=new Map<string,string>();
  private previews=new Set<string>();
  private used=new Set<string>();
  private usedPreviews=new Set<string>();
  private currentPreview:string|null=null;
  private queuedWork=false;
  private continuationUsed=false;
  private pending:QueuedPhase|null=null;
  private frozenPreview:string|null=null;
  private frozenChildren=new Map<string,string>();

  constructor(readonly sessionId:string){}

  child(c:any){
    if(typeof c.activeSessionId==="string"&&typeof c.sessionName==="string")
      this.children.set(c.activeSessionId,c.sessionName);
  }

  get queuedContinuationPending():boolean { return this.pending!==null; }

  permitsPendingEvent(type:string):boolean {
    if(this.pending===null)return true;
    const expected:Record<QueuedPhase,string>={
      preparing:"session_action_update", committing:"session_action_update",
      "agent-start":"agent_start", running:"session_action_update",
      "turn-start":"turn_start", "custom-start":"message_start", "custom-end":"message_end",
    };
    return type===expected[this.pending];
  }

  queue(a:any):boolean {
    if(this.pending!==null){
      const next:Partial<Record<QueuedPhase,QueuedPhase>>={preparing:"committing",committing:"agent-start",running:"turn-start"};
      const following=next[this.pending];
      if(!following||a.queuedCount!==0||a.steering.length!==0||a.followUps.length!==0
        ||a.active?.kind!=="turn"||a.active.phase!==this.pending)return false;
      this.pending=following;
      this.queueEmpty=false;
      return true;
    }
    this.queueObserved=true;
    const queued=a.queuedCount!==0||a.steering.length!==0||a.followUps.length!==0;
    if(this.candidate&&(a.active||queued))return false;
    if(this.continuationUsed&&(queued||(a.active
      &&(a.active.kind!=="turn"||a.active.phase!=="running"))))return false;
    this.queueEmpty=a.queuedCount===0&&a.steering.length===0&&a.followUps.length===0&&!a.active;
    this.queuedWork=queued;
    const current=[...a.steering,...a.followUps];
    this.currentPreview=!a.active&&a.queuedCount===1&&current.length===1
      &&current[0].startsWith("Agent message received: ")&&!this.usedPreviews.has(current[0])
      ?current[0]:null;
    for(const preview of current)if(!this.usedPreviews.has(preview))this.previews.add(preview);
    return true;
  }

  closeSegment(stop:string):boolean {
    if(this.pending!==null)return false;
    this.segmentClosed=true;
    this.candidate=stop==="stop";
    if(stop!=="stop")return true;
    if(this.queuedWork){
      if(this.continuationUsed||this.currentPreview===null||this.children.size===0)return false;
      this.frozenPreview=this.currentPreview;
      this.frozenChildren=new Map(this.children);
      this.pending="preparing";
      this.candidate=false;
    }
    // A prior idle snapshot cannot settle a later stopped segment.
    this.queueEmpty=false;
    return true;
  }

  beginQueuedAgent():boolean {
    if(this.pending!=="agent-start")return false;
    this.pending="running";
    this.continuationUsed=true;
    this.segmentClosed=false;
    this.resumed=true;
    this.candidate=false;
    this.queueEmpty=false;
    this.queuedWork=false;
    this.currentPreview=null;
    return true;
  }

  beginQueuedTurn():boolean {
    if(this.pending!=="turn-start")return false;
    this.pending="custom-start";
    return true;
  }

  startQueuedMessage(message:any):boolean {
    if(this.pending!=="custom-start"||this.frozenPreview===null
      ||!this.matchesMessage(message,new Set([this.frozenPreview]),this.frozenChildren))return false;
    this.pending="custom-end";
    return true;
  }

  endQueuedMessage(message:any):boolean {
    if(this.pending!=="custom-end"||this.frozenPreview===null
      ||!this.matchesMessage(message,new Set([this.frozenPreview]),this.frozenChildren))return false;
    this.consumeMessage(message);
    this.pending=null;
    this.frozenPreview=null;
    this.frozenChildren.clear();
    return true;
  }

  private matchesMessage(m:any,previews:Set<string>,children:Map<string,string>):boolean {
    const d=m?.details,f=d?.from,t=d?.target;
    const exact=(v:any,keys:string[])=>v&&typeof v==="object"&&!Array.isArray(v)&&Object.keys(v).every(k=>keys.includes(k));
    const strings=(v:any,keys:string[])=>keys.every(k=>typeof v?.[k]==="string"&&v[k].length>0);
    if(!exact(m,["role","customType","content","display","details","timestamp"])||m.role!=="custom"||m.customType!=="agent_message"||m.display!==true||!Number.isFinite(m.timestamp)||m.timestamp<0
      ||!exact(d,["id","message","from","fromRelationship","target"])||!strings(d,["id","message"])||d.fromRelationship!=="child"
      ||!exact(f,["activeSessionId","sessionId","sessionName","runtimeKind","clientId"])||!strings(f,["activeSessionId","sessionId","sessionName","clientId"])||f.runtimeKind!=="subagent"
      ||!exact(t,["activeSessionId","sessionId","sessionName","runtimeKind"])||!strings(t,["activeSessionId","sessionId"])||t.runtimeKind!=="top-level"||("sessionName" in t&&typeof t.sessionName!=="string")
      ||t.sessionId!==this.sessionId||children.get(f.activeSessionId)!==f.sessionName||this.used.has(d.id))return false;
    const preview=`Agent message received: ${d.message}`;
    if(!previews.has(preview)||this.usedPreviews.has(preview))return false;
    const fmt=(s:string)=>s.replace(/[\s,[\]]+/g," ").trim();
    const sender=[fmt(f.sessionName),`active ${fmt(f.activeSessionId)}`,`session ${fmt(f.sessionId)}`,`client ${fmt(f.clientId)}`].filter(Boolean).join(", ");
    const endpoint=`${t.sessionName?fmt(t.sessionName)+", ":""}active ${fmt(t.activeSessionId)}, session ${fmt(t.sessionId)}`;
    const content=`[from child:${fmt(f.sessionName)}]\nAgent-to-agent message received.\nSource: agent_message\nFrom: ${sender}\nTo: ${endpoint}\nMessage id: ${d.id}\n\n${d.message}`;
    return m.content===content;
  }

  private consumeMessage(message:any):void {
    const preview=`Agent message received: ${message.details.message}`;
    this.used.add(message.details.id);
    this.usedPreviews.add(preview);
    this.previews.delete(preview);
    if(this.currentPreview===preview){
      this.currentPreview=null;
      this.queuedWork=false;
    }
  }

  history(messages:any[],observed:any[]):boolean {
    const equal=(a:any[],b:any[])=>a.length===b.length&&a.every((m,i)=>primeHistorySame(m,b[i]));
    if(equal(messages,observed))return true;
    if(!this.resumed||messages.length!==observed.length+1||!equal(messages.slice(1),observed)
      ||!this.matchesMessage(messages[0],this.previews,this.children))return false;
    this.consumeMessage(messages[0]);
    return true;
  }
}
