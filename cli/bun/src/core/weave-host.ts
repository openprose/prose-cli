import weaveHelp from "../../../shared/fixtures/weave-help.txt" with { type: "text" };
import { constants, openSync, readSync, fstatSync, closeSync, statSync } from "node:fs";
import { realpath, access } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, isAbsolute } from "node:path";
import { constants as osConstants } from "node:os";
import type { GlobalFlags } from "./types";

/** `prose cli weave --help`, byte for byte the same in both ports. */
export const WEAVE_HELP: string = weaveHelp;
type Failure = "INVOCATION_INVALID" | "BINDING_INVALID" | "UNSUPPORTED_PLATFORM" | "START_FAILED" | "TIMEOUT" | "OUTPUT_LIMIT" | "IO_FAILED" | "CANCELLED";
interface Dependencies {
  env: Readonly<Record<string,string|undefined>>;
  platform?: NodeJS.Platform;
  cancellationSignal?: AbortSignal;
  writeStdout(text:string):void;
  writeStderr(text:string):void;
  writeStdoutBytes?(bytes:Uint8Array):void|Promise<void>;
  writeStderrBytes?(bytes:Uint8Array):void|Promise<void>;
  stopHostOutput?():void;
}
function scalar(s:string): boolean {
  for (let i=0;i<s.length;i++) {
    const c=s.charCodeAt(i);
    if(c>=0xd800&&c<=0xdbff){const d=s.charCodeAt(++i);if(!(d>=0xdc00&&d<=0xdfff))return false;}
    else if(c>=0xdc00&&c<=0xdfff)return false;
  }
  return true;
}
function text(s:unknown):s is string{return typeof s==="string"&&scalar(s)&&Buffer.byteLength(s)>0&&Buffer.byteLength(s)<=4096&&!s.includes("\0");}
function path(s:unknown):s is string{return text(s)&&isAbsolute(s);}
function value(s:unknown):s is string{return text(s)&&/[^\p{White_Space}\uFEFF]/u.test(s);}
function number(s:string|undefined,max:number):boolean{return s!==undefined&&/^[1-9][0-9]*$/.test(s)&&Number(s)<=max;}
export function weaveInvocation(args:readonly string[],global:GlobalFlags):{help:true}|{binding:string;argv:string[]}|null{
  if(Object.keys(global).length)return null;
  if(args.length===1&&args[0]==="--help")return {help:true};
  if(args[0]!=="--host-binding"||!path(args[1])||!path(args[3]))return null;
  const op=args[2];
  if(["check","status","step"].includes(op??"")&&args.length===4)return {binding:args[1],argv:args.slice(2)};
  if(op==="serve"&&args.length===8&&args[4]==="--poll-ms"&&number(args[5],3600000)&&args[6]==="--max-steps"&&number(args[7],1000000))return {binding:args[1],argv:args.slice(2)};
  if(op==="settle"&&args.length===12&&args[4]==="--binding"&&value(args[5])&&args[6]==="--attempt"&&value(args[7])&&args[8]==="--outcome"&&["completed","not-applied"].includes(args[9]??"")&&args[10]==="--receipt"&&value(args[11]))return {binding:args[1],argv:args.slice(2)};
  return null;
}
// This small JSON reader preserves lexical integer and duplicate-key information
// that JSON.parse discards. Binding input has a strict byte and nesting bound.
export function strictHostJson(source:string):unknown{
  let at=0;
  const bad=():never=>{throw Error("invalid");};
  const ws=()=>{while(/[\x20\t\r\n]/.test(source[at]??"!") )at++;};
  function string():string{
    const start=at++;
    while(at<source.length){const c=source[at++];if(c==='"'){const s=JSON.parse(source.slice(start,at));if(!scalar(s))bad();return s;}if(c==='\\')at++;}
    return bad();
  }
  function item(depth:number):unknown{
    if(depth>16)bad();ws();const c=source[at];
    if(c==='"')return string();
    if(c==='{'){
      at++;ws();const out:Record<string,unknown>=Object.create(null);if(source[at]==='}'){at++;return out;}
      while(true){ws();if(source[at]!== '"')bad();const key=string();if(Object.hasOwn(out,key))bad();ws();if(source[at++]!==':')bad();out[key]=item(depth+1);ws();const end=source[at++];if(end==='}')return out;if(end!==',')bad();}
    }
    if(c==='['){at++;ws();const out:unknown[]=[];if(source[at]===']'){at++;return out;}while(true){out.push(item(depth+1));ws();const end=source[at++];if(end===']')return out;if(end!==',')bad();}}
    for(const [literal,v] of [["true",true],["false",false],["null",null]] as const){if(source.startsWith(literal,at)){at+=literal.length;return v;}}
    const token=/^(?:0|[1-9][0-9]*)/.exec(source.slice(at))?.[0];if(!token)bad();at+=token!.length;const n=Number(token);if(!Number.isSafeInteger(n))bad();return n;
  }
  const result=item(0);ws();if(at!==source.length)bad();return result;
}
async function boundedFile(file:string,maximum:number,hashOnly=false):Promise<Buffer|string>{
  // Bun 1.3.5's promise-based open can wait on FIFOs despite O_NONBLOCK.
  // Check static kind before opening and use the synchronous nonblocking fd API.
  if(!statSync(file).isFile())throw Error("invalid");
  const fd=openSync(file,constants.O_RDONLY|constants.O_NONBLOCK);
  try{
    const info=fstatSync(fd);if(!info.isFile()||info.size>maximum)throw Error("invalid");
    const chunks:Buffer[]=[];const hash=createHash("sha256");let size=0;
    while(true){const buffer=Buffer.allocUnsafe(Math.min(65536,maximum-size+1));const bytesRead=readSync(fd,buffer,0,buffer.length,null);if(!bytesRead)break;size+=bytesRead;if(size>maximum)throw Error("invalid");const chunk=buffer.subarray(0,bytesRead);if(hashOnly)hash.update(chunk);else chunks.push(chunk);}
    return hashOnly?hash.digest("hex"):Buffer.concat(chunks,size);
  }finally{closeSync(fd);}
}

interface Binding{executable:string;cwd:string;environment:Record<string,string>;timeoutMs:number;maxOutputBytes:number;}
export async function admitHost(file:string,environment:Readonly<Record<string,string|undefined>>):Promise<Binding>{
  if(!statSync(file).isFile())throw Error("invalid");
  const selected=await realpath(file);const bytes=await boundedFile(selected,65536) as Buffer;
  if(bytes.subarray(0,3).equals(Buffer.from([239,187,191])))throw Error("invalid");
  const obj=strictHostJson(new TextDecoder("utf-8",{fatal:true}).decode(bytes)) as Record<string,unknown>;
  const keys=["schema","executable","sha256","environmentKeys","timeoutMs","maxOutputBytes"];
  if(!obj||typeof obj!=="object"||Array.isArray(obj)||Object.keys(obj).length!==keys.length||keys.some(k=>!Object.hasOwn(obj,k))||obj.schema!=="openprose.weave-host-binding/1"||!path(obj.executable)||typeof obj.sha256!=="string"||! /^[a-f0-9]{64}$/.test(obj.sha256))throw Error("invalid");
  const names=obj.environmentKeys;
  if(!Array.isArray(names)||names.length>128||new Set(names).size!==names.length||names.some(n=>typeof n!=="string"||!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(n)))throw Error("invalid");
  if(typeof obj.timeoutMs!=="number"||!Number.isSafeInteger(obj.timeoutMs)||obj.timeoutMs<1||obj.timeoutMs>86400000||typeof obj.maxOutputBytes!=="number"||!Number.isSafeInteger(obj.maxOutputBytes)||obj.maxOutputBytes<1||obj.maxOutputBytes>16777216)throw Error("invalid");
  if(!statSync(obj.executable).isFile())throw Error("invalid");
  const executable=await realpath(obj.executable);await access(executable,constants.X_OK);
  if(await boundedFile(executable,536870912,true)!==obj.sha256)throw Error("invalid");
  const env:Record<string,string>=Object.create(null);for(const name of names){if(Object.hasOwn(environment,name)&&environment[name]!==undefined)env[name]=environment[name]!;}
  return {executable,cwd:dirname(selected),environment:env,timeoutMs:obj.timeoutMs,maxOutputBytes:obj.maxOutputBytes};
}
const outputSinks = new Map<number,{sink:Bun.FileSink;stopped:boolean}>();
export async function writeHostBytes(fd:number,bytes:Uint8Array):Promise<void>{
  let selected=outputSinks.get(fd);
  if(!selected){selected={sink:Bun.file(fd).writer({highWaterMark:65536}),stopped:false};outputSinks.set(fd,selected);}
  for(let offset=0;offset<bytes.length;offset+=65536){
    if(selected.stopped)throw Error("closed");
    // Bun 1.3.5 can return boolean true on EPIPE instead of throwing.
    const written:unknown=selected.sink.write(bytes.subarray(offset,offset+65536));
    if(typeof written!=="number"||written<0)throw Error("write");
    const flushed:unknown=await selected.sink.flush();
    if(typeof flushed!=="number"||flushed<0)throw Error("flush");
  }
}
export function stopHostOutput():void{
  for(const selected of outputSinks.values()){
    selected.stopped=true;selected.sink.unref();
    try{Promise.resolve(selected.sink.end(Error("closed"))).catch(()=>{});}catch{}
  }
}
export async function runWeaveHost(args:readonly string[],global:GlobalFlags,deps:Dependencies):Promise<number>{
  async function fail(code:Failure,exit?:number):Promise<number>{
    let timer:ReturnType<typeof setTimeout>|undefined;
    try{
      const message=`WEAVE_HOST_${code}\n`;
      const write=deps.writeStderrBytes?deps.writeStderrBytes(Buffer.from(message)):deps.writeStderr(message);
      await Promise.race([Promise.resolve(write),new Promise<void>(r=>{timer=setTimeout(r,100);})]);
    }catch{}finally{if(timer)clearTimeout(timer);deps.stopHostOutput?.();}
    return exit??({START_FAILED:126,TIMEOUT:124,OUTPUT_LIMIT:125,IO_FAILED:125,CANCELLED:130} as Partial<Record<Failure,number>>)[code]??2;
  }

  const invocation=weaveInvocation(args,global);if(!invocation)return fail("INVOCATION_INVALID");
  if("help" in invocation){try{if(deps.writeStdoutBytes)await deps.writeStdoutBytes(Buffer.from(WEAVE_HELP));else deps.writeStdout(WEAVE_HELP);return 0;}catch{return fail("IO_FAILED");}}
  if(!["darwin","linux","freebsd","openbsd","netbsd","aix","sunos"].includes(deps.platform??process.platform))return fail("UNSUPPORTED_PLATFORM");
  let binding:Binding;try{binding=await admitHost(invocation.binding,deps.env);}catch{return fail("BINDING_INVALID");}
  const state:{first?:{code:Failure;exit?:number}}={};
  let wake!:()=>void;const fault=new Promise<void>(r=>{wake=r;});
  let child:Bun.Subprocess<"ignore","pipe","pipe">|undefined;
  let exited=false;
  const set=(code:Failure,exit?:number)=>{if(!state.first){state.first=exit===undefined?{code}:{code,exit};wake();}};
  const cancel=()=>set("CANCELLED",deps.cancellationSignal?.reason==="SIGTERM"?143:130);
  deps.cancellationSignal?.addEventListener("abort",cancel);
  if(deps.cancellationSignal?.aborted)cancel();
  if(state.first){deps.cancellationSignal?.removeEventListener("abort",cancel);return fail(state.first.code,state.first.exit);}
  const timer=setTimeout(()=>set("TIMEOUT"),binding.timeoutMs);
  try{child=Bun.spawn({cmd:[binding.executable,...invocation.argv],cwd:binding.cwd,env:binding.environment,stdin:"ignore",stdout:"pipe",stderr:"pipe",detached:true});}
  catch{clearTimeout(timer);deps.cancellationSignal?.removeEventListener("abort",cancel);return fail("START_FAILED");}
  const owned=child;
  const exit=owned.exited.then(code=>{exited=true;return code;},()=>{exited=true;set("IO_FAILED");return 125;});
  let count=0;
  const readers=[owned.stdout.getReader(),owned.stderr.getReader()];
  const pumps=readers.map(async(reader,index)=>{
    try{
      while(!state.first){
        const {value:bytes,done}=await reader.read();
        if(done||state.first)return;
        const take=Math.min(binding.maxOutputBytes-count,bytes.length);
        count+=take;
        // Latch excess before invoking even a synchronously failing sink.
        if(bytes.length>take)set("OUTPUT_LIMIT");
        let forwarded:void|Promise<void>=undefined;
        if(take){
          const write=index===0?deps.writeStdoutBytes:deps.writeStderrBytes;
          forwarded=write?write(bytes.subarray(0,take)):(index===0?deps.writeStdout:deps.writeStderr)(Buffer.from(bytes.subarray(0,take)).toString("utf8"));
        }
        await Promise.race([Promise.resolve(forwarded),fault]);
      }
    }catch{if(!state.first)set("IO_FAILED");}
  });
  const finished=Promise.all([exit,...pumps]);
  await Promise.race([finished,fault]);
  if(state.first){
    // Never signal a group whose direct child has exited: its PID may be reused.
    const signal=(name:NodeJS.Signals)=>{if(!exited&&owned.exitCode===null&&owned.signalCode===null){try{process.kill(-owned.pid,name);}catch{}}};
    signal("SIGTERM");
    let grace:ReturnType<typeof setTimeout>|undefined;
    await Promise.race([exit,new Promise<void>(r=>{grace=setTimeout(r,1000);})]);
    if(grace)clearTimeout(grace);signal("SIGKILL");
    await Promise.all(readers.map(reader=>reader.cancel().catch(()=>{})));
    await exit;await Promise.all(pumps);
  }
  clearTimeout(timer);deps.cancellationSignal?.removeEventListener("abort",cancel);
  const result = (():{code:Failure;exit?:number}|undefined=>state.first)();
  if(result)return fail(result.code,result.exit);
  const code=await exit;const sig=owned.signalCode;
  return sig?128+(osConstants.signals[sig]??0):code;
}
