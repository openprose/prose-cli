/** Shared strict input decoding and immutable evidence binding for provider processes. */
import {createHash} from 'node:crypto';
import {readSync,fstatSync,constants,realpathSync,openSync,closeSync} from 'node:fs';
import {resolve} from 'node:path';
const sha=v=>createHash('sha256').update(v).digest('hex');
const fail=()=>{throw Error('EVALUATION_INPUT_INVALID');};
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const exact=(v,keys)=>object(v)&&Object.keys(v).length===keys.length&&keys.every(k=>Object.hasOwn(v,k));
const text=v=>typeof v==='string'&&v.trim().length>0&&!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(v);
const integer=v=>Number.isSafeInteger(v)&&v>=0;
/** Reject ambiguous duplicate fields at every level, malformed UTF-8 and BOM. */
export function strictJSON(bytes) {
  const s = new TextDecoder('utf-8', {fatal:true, ignoreBOM:true}).decode(bytes);
  if (s.charCodeAt(0) === 0xfeff) fail();
  const parsed = JSON.parse(s);
  let i = 0;
  const ws = () => { while (/\s/.test(s[i] ?? '') && i < s.length) i++; };
  function string() {
    const start = i++;
    while (i < s.length) { const ch = s[i++]; if (ch === '\\') i++; else if (ch === '"') break; }
    const value = JSON.parse(s.slice(start,i));
    if (!text(value) && value.length !== 0 && /[\uD800-\uDFFF]/u.test(value)) fail();
    return value;
  }
  function walk(depth = 0) {
    if (depth > 64) fail();
    ws();
    if (s[i] === '{') {
      i++; ws(); const seen = new Set();
      if (s[i] === '}') { i++; return; }
      for (;;) { ws(); const key = string(); if (seen.has(key)) fail(); seen.add(key); ws(); i++; walk(depth+1); ws(); if(s[i++] === '}') return; }
    }
    if (s[i] === '[') { i++; ws(); if (s[i] === ']') {i++;return;} for (;;) {walk(depth+1);ws();if(s[i++] === ']') return;} }
    if (s[i] === '"') { string(); return; }
    const start=i; while (i<s.length && !/[\s,}\]]/.test(s[i])) i++;
    if (!Number.isFinite(Number(s.slice(start,i))) && !['true','false','null'].includes(s.slice(start,i))) fail();
  }
  walk(); return parsed;
}
export function file(path, max) {
  const fd=openSync(path,constants.O_RDONLY|constants.O_NONBLOCK);
  try {
    const info=fstatSync(fd);if(!info.isFile() || info.size>max)fail();
    const buffer=Buffer.alloc(max+1);let size=0,count;
    while(size<buffer.length && (count=readSync(fd,buffer,size,buffer.length-size,null))>0)size+=count;
    if(size>max)fail();return buffer.subarray(0,size);
  } finally {closeSync(fd);}
}

export function bindEvidence(raw,loaded,now=Date.now()) {
  const {config}=loaded;
  if(!integer(now)||raw.length>config.limits.maxInputBytes)fail();
  const envelope=strictJSON(raw);
  if(!exact(envelope,['schema','evidence','attempt']) || envelope.schema!=='openprose.weave-input/1' || envelope.attempt!==null) fail();
  const e=envelope.evidence;
  if(!exact(e,['identity','payload','observedAt','validUntil','gap']) || typeof e.payload!=='string' || !/^[0-9a-f]{64}$/.test(e.identity) || sha(e.payload)!==e.identity || typeof e.gap!=='boolean' || !integer(e.observedAt) || !integer(e.validUntil) || e.validUntil<=e.observedAt) fail();
  if(e.gap || now<e.observedAt || now>=e.validUntil) return null;
  const payload=strictJSON(Buffer.from(e.payload));
  if(!exact(payload,['version','policy','files']) || payload.version!==1 || !text(payload.policy) || !Array.isArray(payload.files)) fail();
  const groups={kernel:[],contract:[],evidence:[]}, paths=new Set();
  for(const source of payload.files) {
    if(!exact(source,['role','path','sha256','content']) || !Object.hasOwn(groups,source.role) || !text(source.path) || paths.has(source.path) || typeof source.content!=='string' || sha(source.content)!==source.sha256) fail();
    paths.add(source.path); groups[source.role].push(source);
  }
  if(groups.kernel.length!==1 || !groups.contract.length || !groups.evidence.length) fail();
  // These files are data, not adopted contracts. Their exact bytes bind policy to the cached evidence.
  for(const [path,digest] of [[loaded.configPath,loaded.configSha256],[loaded.questionPath,loaded.questionSha256]]) {
    if(!groups.evidence.some(source=>source.sha256===digest && realpathSync(resolve(source.path))===path) || sha(file(path,65536))!==digest) fail();
  }
  return {state:{agreement:{kernel:groups.kernel[0],contracts:groups.contract},source_evidence:groups.evidence,binding_policy_identity:payload.policy,evidence_identity:e.identity},evidenceIdentity:e.identity,validUntil:e.validUntil};
}
