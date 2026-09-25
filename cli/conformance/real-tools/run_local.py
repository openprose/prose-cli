"""Explicit live native file-tool canary. Credentials stay in child environment."""
import pathlib,os,subprocess,json,shlex,concurrent.futures,time,signal,argparse,hashlib
p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--products',nargs='+',default=['rust','bun']);p.add_argument('--harnesses',nargs='+',default=['prime','omp']);a=p.parse_args()
repo=pathlib.Path(__file__).resolve().parents[3];out=pathlib.Path(a.output);out.mkdir(parents=True,exist_ok=False)
env=os.environ.copy()
env_file=os.environ.get('OPENPROSE_REAL_TOOLS_ENV_FILE')
for line in (pathlib.Path(env_file).read_text().splitlines() if env_file else []):
 line=line.strip().removeprefix('export ')
 if '=' not in line:continue
 k,v=line.split('=',1)
 if k in ('ANTHROPIC_API_KEY','OPENAI_API_KEY'):
  words=shlex.split(v,comments=True)
  if len(words)==1:env[k]=words[0]
private=os.environ.get('OPENPROSE_REAL_TOOLS_HARNESS_BIN')
if private:env['PATH']=private+os.pathsep+env['PATH']
env.pop('ANTHROPIC_OAUTH_TOKEN',None)
def one(cell):
 product,harness,provider,model=cell;name='-'.join([product,harness,provider]);run=out/name;run.mkdir();ws=run/'workspace';ws.mkdir()
 (ws/'README.md').write_text('# File canary\nRead the requested program and fulfill its instructions using native tools.\n')
 (ws/'input.md').write_text('Code: CROCUS-492\nQuantity: 31\nAdjustment: -8\n')
 (ws/'program.md').write_text('# Task\nRead input.md and write result.md with its exact code and net quantity after adjustment. Use actual filesystem tools. Keep input.md unchanged.\n')
 binary=repo/('prose-bun-tools' if product=='bun' else 'cli/rust/target/debug/prose')
 cmd=[str(binary),'--harness',harness,'--auth-profile',provider,'--model',provider+'/'+model,'--cwd',str(ws),'--timeout','150s','--output','jsonl','run','program.md']
 meta={'cell':cell,'argv':cmd,'started':time.time(),'auth':'explicit provider API profile; child env only','binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest()}
 with (run/'stream.jsonl').open('w') as stdout,(run/'stderr.txt').open('w') as stderr:
  proc=subprocess.Popen(cmd,cwd=ws,env=env,stdout=stdout,stderr=stderr,start_new_session=True)
  try:code=proc.wait(timeout=180)
  except subprocess.TimeoutExpired:
   os.killpg(proc.pid,signal.SIGTERM);code=proc.wait(timeout=8);meta['timedOut']=True
 meta['exitCode']=code;meta['elapsed']=round(time.time()-meta['started'],2)
 result=ws/'result.md';meta['actualFile']=result.exists()
 if result.exists():(run/'result.md').write_bytes(result.read_bytes())
 meta['inputIntact']=(ws/'input.md').read_text()=='Code: CROCUS-492\nQuantity: 31\nAdjustment: -8\n'
 (run/'metadata.json').write_text(json.dumps(meta,indent=2));print(name,code,meta['actualFile'],flush=True)
cells=[(p,h,provider,m) for p in a.products for h in a.harnesses for provider,m in [('anthropic','claude-haiku-4-5-20251001'),('openai','gpt-5.4-mini')]]
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(one,cells))
