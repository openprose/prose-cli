#!/usr/bin/env python3
"""Provider-free compiled Codex append check. Inputs must be explicit image builds."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

parser=argparse.ArgumentParser()
parser.add_argument('--bun',required=True,type=Path)
parser.add_argument('--rust',required=True,type=Path)
args=parser.parse_args()
results=[]
for implementation,binary in [('bun',args.bun.resolve()),('rust',args.rust.resolve())]:
    with tempfile.TemporaryDirectory(prefix='kernel-startup-process-') as temporary:
        root=Path(temporary)
        executable=root/'codex'
        observation=root/'observation.json'
        executable.write_text('#!'+sys.executable+'\n'+'''import json,sys,os
from pathlib import Path
args=sys.argv[1:]
if args==['--version']:
 print('codex-cli 0.149.0-alpha.4.1');sys.exit(0)
if args==['login','status']:
 print('Logged in using ChatGPT');sys.exit(0)
configs=[args[i+1] for i,a in enumerate(args[:-1]) if a=='-c']
selected=[c for c in configs if c.startswith('developer_instructions=')]
assert len(selected)==1
image=json.loads(selected[0].split('=',1)[1])
task=json.load(sys.stdin)
assert task['argv']==['prose','run','hello.prose.md']
assert image and image not in json.dumps(task)
assert args.index(selected[0])>args.index('exec')
assert args[args.index('--model')+1]=='fixture-model'
assert args[args.index('--sandbox')+1]=='workspace-write'
Path('''+repr(str(observation))+''').write_text(json.dumps({'image_bytes':len(image.encode()),'task':task,'placement':'developer','mode':os.environ.get('KERNEL_STARTUP_CASE','success')}))
case=Path('case.txt').read_text()
if case=='failure': sys.exit(7)
if case=='timeout':
 import time;time.sleep(30);sys.exit(1)
print(json.dumps({'type':'thread.started','thread_id':'fixture'}))
print(json.dumps({'type':'turn.started'}))
print(json.dumps({'type':'item.completed','item':{'id':'0','type':'agent_message','text':'Provider-free fixture'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':0,'output_tokens':0}}))
''')
        executable.chmod(0o755)
        env={'PATH':str(root)+os.pathsep+os.defpath,'HOME':str(root),'USER':'fixture','LANG':'C.UTF-8'}
        for case in ['success','failure','timeout']:
            (root/'case.txt').write_text(case)
            command=[str(binary),'--harness','codex','--model','fixture-model','--permission-mode','workspace-write','--cwd',str(root),'--output-contract','native','--output','json','--timeout','1s','run','hello.prose.md']
            result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,env=env,timeout=12)
            value=json.loads(result.stdout)
            assert (result.returncode==0)==(case=='success'),(implementation,case,result.returncode,value)
            if case=='success':
                assert value['negotiatedCapabilities']['promptPlacement']=='developer',value
                assert value['terminal']['transportCompleted'],value
                assert value['digests']['taskSha256']==value['digests']['renderedPayloadSha256'],value
            assert observation.exists(),(implementation,case,result.stderr.decode())
            results.append({'implementation':implementation,'case':case,'exit_code':result.returncode,'placement_verified':True})
print(json.dumps(results,indent=2))
