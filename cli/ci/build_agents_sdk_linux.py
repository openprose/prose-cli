"""Native, digest-bound Linux SDK construction driver.

Uses the explicit supplier hooks and requires native source and symbol custody
in the frozen receipt before accepting construction.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import selectors
import signal
import time
import uuid
import shutil
import sys

LOCK = Path(__file__).with_name('agents-sdk-linux-build.lock.json')
SOURCES = ('harnesses/agents-sdk/run.py', 'harnesses/agents-sdk/requirements-build.txt',
           'cli/ci/build_agents_sdk.py', 'harnesses/agents-sdk/test_run.py', 'cli/ci/sdk_native_inventory.py')
NAME = 'prose-agents-sdk'
RECEIPT = 'agents-sdk-build.json'
MIN_FREE_BYTES = 8 * 1024**3
MAX_RETAINED_BYTES = 4 * 1024**3
MAX_FILES = 100000
MAX_LOG_BYTES = 32 * 1024**2
OWNER_LABEL = 'org.openprose.sdk-builder.owner'


def require(ok, message):
    if not ok: raise ValueError(message)

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def load_lock(path=None):
    path=LOCK if path is None else path
    x=json.loads(path.read_text())
    require(set(x)=={'schema','python','platforms'} and x['schema']=='openprose.sdk-linux-build-inputs/1'
            and x['python']=='3.10.20', 'Invalid Linux input lock')
    require(set(x['platforms'])=={'linux-x64-gnu','linux-arm64-gnu'}, 'Closed native target set required')
    for target, machine, oci in [('linux-x64-gnu','x86_64','amd64'),('linux-arm64-gnu','aarch64','arm64')]:
        r=x['platforms'][target]
        require(set(r)=={'machine','dockerPlatform','freezer','supplier','pythonArchive'} and
                r['machine']==machine and r['dockerPlatform']=='linux/'+oci, 'Native target identity mismatch')
        for key,floor in [('freezer','2_34'),('supplier','2_28')]:
            v=r[key]
            require(set(v)=={'image','configSha256','metadataUrl'} and re.fullmatch(
                'quay.io/pypa/manylinux_'+floor+'_'+machine+'@sha256:[0-9a-f]{64}',v['image'])
                and re.fullmatch('[0-9a-f]{64}',v['configSha256']), 'Digest-bound image required')
        p=r['pythonArchive'];triple=machine+'-unknown-linux-gnu'
        require(set(p)=={'url','sha256','byteLength','targetTriple','metadataUrl'} and p['targetTriple']==triple
                and re.fullmatch('https://github.com/astral-sh/python-build-standalone/releases/download/[0-9]{8}/cpython-3[.]10[.]20%2B[0-9]{8}-'+triple+'-pgo%2Blto-full[.]tar[.]zst',p['url'])
                and re.fullmatch('[0-9a-f]{64}',p['sha256']) and type(p['byteLength']) is int
                and 0<p['byteLength']<=256*1024*1024,'Exact full shared baseline Python artifact required')
    return x

def checked_path(path):
    p=Path(path).absolute()
    require(not any(c in str(p) for c in ',\n\r'), 'Unsafe mount path')
    require(p.resolve()==p, 'Symlink mount forbidden')
    return p

def source_hashes(source):
    files={name:checked_path(source/name) for name in SOURCES}
    require(all(path.is_file() and path.stat().st_size <= 8*1024**2 for path in files.values()),
            'Missing or oversized source input')
    return {'sources':{name:sha(path) for name,path in files.items()},
            'driverSha256':sha(checked_path(Path(__file__).absolute())), 'lockSha256':sha(checked_path(LOCK))}

def assert_stable(source, snapshot):
    require(source_hashes(source)==snapshot, 'Source, runtime tests, driver or lock changed')

def check_output(output):
    count=0; size=0
    for path in output.rglob('*'):
        count+=1;require(count <= MAX_FILES, 'Owned output file limit exceeded')
        # Vendor archive symlinks may exist internally; never follow these for
        # accounting, and never allow symlinked owned top-level stages.
        if path.is_file() and not path.is_symlink():
            size+=path.stat().st_size
            require(size<=MAX_RETAINED_BYTES,'Owned output byte limit exceeded')
    return size

def verify_archive(path, row):
    p=checked_path(path);r=row['pythonArchive']
    require(p.is_file() and p.stat().st_size==r['byteLength'] and sha(p)==r['sha256'],
            'Python archive differs from pin')

def validate_origin(origin, root, row):
    require(isinstance(origin,dict) and set(origin)=={'schema','package','library','licenses'}
            and origin['schema']=='openprose.sdk-native-origin/1','Closed native origin required')
    package=origin['package']
    require(set(package)=={'name','epoch','version','release','architecture','sourceRpm','license'}
            and all(isinstance(v,str) and v and len(v)<=1024 for v in package.values())
            and package['name']=='libgcc' and package['architecture']==row['machine']
            and re.fullmatch('[A-Za-z0-9_.+~-]+[.]src[.]rpm',package['sourceRpm']),
            'Actual supplying RPM and source RPM facts required')
    lib=origin['library'];path=checked_path(root/'libgcc_s.so.1')
    require(set(lib)=={'path','sha256','byteLength','supplierPath','rpmFileDigestVerified'}
            and lib['path']=='libgcc_s.so.1' and lib['supplierPath'].startswith('/usr/lib64/')
            and lib['rpmFileDigestVerified'] is True and sha(path)==lib['sha256']
            and type(lib['byteLength']) is int and path.stat().st_size==lib['byteLength'],
            'Supplier origin does not bind exact libgcc bytes')
    licenses=origin['licenses'];require(isinstance(licenses,list) and 0<len(licenses)<=128,'Native license evidence required')
    text=[]
    for i,license in enumerate(licenses):
        require(set(license)=={'packagePath','path','sha256','byteLength'}
                and license['path']=='license-'+str(i)+'.txt'
                and license['packagePath'].startswith('/usr/share/licenses/'), 'Closed supplying-package license path required')
        path=checked_path(root/license['path'])
        require(type(license['byteLength']) is int and 0<license['byteLength']<=1048576
                and path.stat().st_size==license['byteLength'] and sha(path)==license['sha256'],
                'Actual license text bytes differ')
        text.append(path.read_text(errors='replace'))
    require('GCC RUNTIME LIBRARY EXCEPTION' in '\n'.join(text)
            and 'GNU GENERAL PUBLIC LICENSE' in '\n'.join(text),'Actual GPL and runtime exception texts required')

def validate_image(info,row,key):
    oci=row['dockerPlatform'].split('/')[1]
    require(isinstance(info,dict) and info.get('Id')=='sha256:'+row[key]['configSha256']
            and info.get('Os')=='linux' and info.get('Architecture')==oci
            and row[key]['image'] in info.get('RepoDigests',[]),
            'Actual local OCI config, architecture or digest mismatch')


def container_command(row, image_key, source, output, script, *, network='none', owner=None):
    require(network in ('none','bridge'),'Closed network mode')
    owner=owner or uuid.uuid4().hex
    require(re.fullmatch('[0-9a-f]{32}',owner), 'Invalid container owner')
    return ['docker','--config',str(output/'docker-config'),'run','--rm','--pull=never',
            '--name','prose-sdk-'+owner+'-'+script.removesuffix('.sh'),
            '--label',OWNER_LABEL+'='+owner,
            '--platform',row['dockerPlatform'],'--network='+network,'--read-only',
            '--cap-drop=ALL','--security-opt=no-new-privileges','--user',str(os.getuid())+':'+str(os.getgid()),
            '--tmpfs','/tmp:rw,nosuid,nodev,size=536870912',
            '--mount','type=bind,src='+str(source)+',dst=/source,readonly',
            '--mount','type=bind,src='+str(output)+',dst=/job',
            '--env','HOME=/job/home','--env','TMPDIR=/tmp','--env','PYTHONNOUSERSITE=1',
            '--entrypoint','/bin/bash',row[image_key]['image'],'-euo','pipefail','/job/'+script]

SUPPLIER = r'''/opt/python/cp310-cp310/bin/python /job/supplier.py
'''
SUPPLIER_PY = r'''
import hashlib,json,pathlib,subprocess
out=pathlib.Path('/job/supplier');out.mkdir()
p=pathlib.Path('/usr/lib64/libgcc_s.so.1').resolve(strict=True)
assert p.parent==pathlib.Path('/usr/lib64') and p.is_file()
b=p.read_bytes();assert b[:4]==b'\x7fELF'
raw=subprocess.check_output(['rpm','-qf','--qf','%{NAME}\n%{EPOCHNUM}\n%{VERSION}\n%{RELEASE}\n%{ARCH}\n%{SOURCERPM}\n%{LICENSE}\n',str(p)],text=True).splitlines()
assert len(raw)==7 and all(raw) and raw[0]=='libgcc' and raw[5].endswith('.src.rpm')
files=subprocess.check_output(['rpm','-qf','--qf','[%{FILENAMES}\t%{FILEDIGESTS}\n]',str(p)],text=True).splitlines()
digests=dict(line.split('\t') for line in files);digest=hashlib.sha256(b).hexdigest()
assert digests.get(str(p))==digest, 'Supplier libgcc differs from installed RPM digest'
(out/'libgcc_s.so.1').write_bytes(b)
licenses=[]
for name in subprocess.check_output(['rpm','-ql',raw[0]],text=True).splitlines():
 q=pathlib.Path(name)
 if str(q).startswith('/usr/share/licenses/') and q.is_file():
  data=q.read_bytes();assert len(data)<=1048576
  dest=out/('license-'+str(len(licenses))+'.txt');dest.write_bytes(data)
  licenses.append({'packagePath':str(q),'path':dest.name,'sha256':hashlib.sha256(data).hexdigest(),'byteLength':len(data)})
texts='\n'.join((out/x['path']).read_text(errors='replace') for x in licenses)
assert 'GCC RUNTIME LIBRARY EXCEPTION' in texts and 'GNU GENERAL PUBLIC LICENSE' in texts, 'Actual supplier license texts missing'
record={'schema':'openprose.sdk-native-origin/1','package':dict(zip(['name','epoch','version','release','architecture','sourceRpm','license'],raw)),
 'library':{'path':'libgcc_s.so.1','sha256':digest,'byteLength':len(b),'supplierPath':str(p),'rpmFileDigestVerified':True},'licenses':licenses}
(out/'origin.json').write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
'''
PREPARE = r'''/opt/python/cp310-cp310/bin/python - <<'PIN'
import hashlib,json,pathlib
r=json.loads(pathlib.Path('/job/target.json').read_text())['pythonArchive']
p=pathlib.Path('/job/python-full.tar.zst')
assert p.is_file() and not p.is_symlink() and p.stat().st_size==r['byteLength']
h=hashlib.sha256()
with p.open('rb') as f:
 for b in iter(lambda:f.read(1048576),b''):h.update(b)
assert h.hexdigest()==r['sha256'], 'Copied Python archive changed before tar'
PIN
mkdir /job/python-full
cd /job/python-full
tar --zstd -xf /job/python-full.tar.zst
/job/python-full/python/install/bin/python3 - <<'CHECK'
import json,sys,pathlib
x=json.loads(pathlib.Path('/job/python-full/python/PYTHON.json').read_text())
r=json.loads(pathlib.Path('/job/target.json').read_text())
assert x['python_version']=='3.10.20' and sys.version_info[:3]==(3,10,20)
assert x['target_triple']==r['pythonArchive']['targetTriple'] and x['libpython_link_mode']=='shared'
assert x.get('license_path') and x.get('licenses')
CHECK
/job/python-full/python/install/bin/python3 -m venv /job/sdk-python
/job/sdk-python/bin/python3 -m pip install --require-hashes --only-binary=:all: -r /source/harnesses/agents-sdk/requirements-build.txt
'''
FREEZE = r'''/job/sdk-python/bin/python3 -m unittest discover -s /source/harnesses/agents-sdk -p test_run.py
/job/sdk-python/bin/python3 /source/cli/ci/build_agents_sdk.py --out /job/frozen --source-date-epoch "$(cat /job/epoch.txt)" --linux-libgcc /job/supplier/libgcc_s.so.1 --linux-native-origin /job/native-input.json
'''

def plan(source, output, target, archive, *, epoch=0, native=True):
    source=checked_path(source); output=checked_path(output); archive=checked_path(archive)
    require(source.is_dir() and not output.exists(),'Fresh owned output and existing source required')
    require(epoch>=0 and type(epoch) is int,'Nonnegative epoch required')
    x=load_lock();require(target in x['platforms'],'Unsupported native target');r=x['platforms'][target]
    if native: require(sys.platform=='linux' and platform.machine()==r['machine'],'Native Linux architecture required')
    verify_archive(archive,r)
    require(shutil.disk_usage(output.parent).free>=MIN_FREE_BYTES,'Linux freeze requires at least8GiB free owned scratch')
    snapshot=source_hashes(source)
    owner=uuid.uuid4().hex
    return r,snapshot,[container_command(r,'supplier',source,output,'supplier.sh',owner=owner),
                       container_command(r,'freezer',source,output,'prepare.sh',network='bridge',owner=owner),
                       container_command(r,'freezer',source,output,'freeze.sh',owner=owner)]

def stop_process_tree(process):
    try: os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=2)
    # The direct child may exit while its descendants keep a pipe open.
    try: os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError: pass

def process(command, output, label, *, timeout=1200, max_bytes=MAX_LOG_BYTES):
    env={'PATH':os.environ['PATH'],'HOME':str(output/'home'),'LANG':'C.UTF-8'}
    p=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT,start_new_session=True)
    deadline=time.monotonic()+timeout; total=0; next_audit=0
    try:
        with (output/(label+'.log')).open('wb') as log, selectors.DefaultSelector() as events:
            events.register(p.stdout,selectors.EVENT_READ)
            while events.get_map():
                require(time.monotonic()<deadline,'Process deadline exceeded: '+label)
                if time.monotonic()>=next_audit:
                    check_output(output)
                    require(shutil.disk_usage(output).free>=512*1024**2,'Owned scratch reserve exhausted')
                    next_audit=time.monotonic()+1
                for key,_ in events.select(min(.1,max(0,deadline-time.monotonic()))):
                    data=os.read(key.fileobj.fileno(),65536)
                    if not data: events.unregister(key.fileobj); continue
                    log.write(data[:max(0,max_bytes-total)])
                    total+=len(data)
                    require(total<=max_bytes,'Process output limit exceeded: '+label)
            p.wait(timeout=max(.01,deadline-time.monotonic()))
            require(p.returncode==0,'Inspect retained '+label+'.log')
    except BaseException:
        stop_process_tree(p); raise
    finally:
        if p.stdout: p.stdout.close()
    return (output/(label+'.log')).read_bytes()

def cleanup_owned(command, output, label):
    if '--name' not in command: return
    name=command[command.index('--name')+1]
    owner=command[command.index('--label')+1].split('=',1)[1]
    require(re.fullmatch('prose-sdk-'+owner+'-[a-z]+',name), 'Unowned container cleanup forbidden')
    prefix=['docker','--config',str(output/'docker-config')]
    try:
        actual=process(prefix+['container','inspect','--format','{{index .Config.Labels "'+OWNER_LABEL+'"}}',name],
                       output,label+'-cleanup-inspect',timeout=10,max_bytes=65536).decode().strip()
        require(actual==owner,'Container ownership label differs; cleanup refused')
        process(prefix+['container','rm','--force',name],output,label+'-cleanup-remove',timeout=10,max_bytes=65536)
    except Exception as e:
        (output/(label+'-cleanup-error.txt')).write_text(type(e).__name__+': '+str(e)+'\n')
        # Preserve the original construction failure; never claim cleanup passed.

def run(command, output, label, *, timeout=1200, max_bytes=MAX_LOG_BYTES):
    try: return process(command,output,label,timeout=timeout,max_bytes=max_bytes)
    except BaseException:
        cleanup_owned(command,output,label); raise

def build(source,output,target,archive,*,epoch=0):
    source=checked_path(source);output=checked_path(output)
    r,snapshot,commands=plan(source,output,target,archive,epoch=epoch)
    output.mkdir();(output/'home').mkdir();(output/'docker-config').mkdir()
    def guarded(command,label,**kwargs):
        assert_stable(source,snapshot);check_output(output)
        try: return run(command,output,label,**kwargs)
        finally:
            assert_stable(source,snapshot);check_output(output)
    (output/'target.json').write_text(json.dumps(r));(output/'epoch.txt').write_text(str(epoch))
    # Caller downloads exact archive beforehand; never copy an unverified input.
    assert_stable(source,snapshot)
    shutil.copyfile(archive,output/'python-full.tar.zst')
    verify_archive(output/'python-full.tar.zst',r)
    assert_stable(source,snapshot)
    for name,content in [('supplier.sh',SUPPLIER),('supplier.py',SUPPLIER_PY),('prepare.sh',PREPARE),('freeze.sh',FREEZE)]:
        (output/name).write_text(content)
    for key in ('supplier','freezer'):
        # Pull by digest during preparation only; verify local config identity.
        guarded(['docker','--config',str(output/'docker-config'),'pull','--platform',r['dockerPlatform'],r[key]['image']],'pull-'+key)
        raw=guarded(['docker','--config',str(output/'docker-config'),'image','inspect','--format','{{json .}}',r[key]['image']],
                    'inspect-'+key,timeout=15,max_bytes=65536)
        validate_image(json.loads(raw),r,key)
    guarded(commands[0],'supplier')
    origin=json.loads((output/'supplier/origin.json').read_text())
    validate_origin(origin,output/'supplier',r)
    inputs={'schema':'openprose.sdk-linux-native-input/1','target':target,'images':{k:r[k] for k in ('supplier','freezer')},
            'pythonArchive':r['pythonArchive'],'sourceSnapshot':snapshot,'libgcc':origin,
            'driverSha256':snapshot['driverSha256'],'lockSha256':snapshot['lockSha256']}
    (output/'native-input.json').write_text(json.dumps(inputs,indent=2,sort_keys=True)+'\n')
    verify_archive(output/'python-full.tar.zst',r)
    guarded(commands[1],'prepare')
    metadata_path=output/'python-full/python/PYTHON.json'
    require(metadata_path.is_file() and metadata_path.stat().st_size<=8*1024*1024,'Retained full Python metadata required')
    provider=json.loads(metadata_path.read_text())
    require(provider['python_version']=='3.10.20' and provider['target_triple']==r['pythonArchive']['targetTriple']
            and provider['libpython_link_mode']=='shared','Retained Python metadata identity mismatch')
    inputs['pythonDistribution']={'metadataPath':'/job/python-full/python/PYTHON.json',
                                 'root':'/job/python-full/python','metadataSha256':sha(metadata_path)}
    (output/'native-input.json').write_text(json.dumps(inputs,indent=2,sort_keys=True)+'\n')
    verify_archive(output/'python-full.tar.zst',r)
    guarded(commands[2],'freeze')
    receipt=json.loads((output/'frozen'/RECEIPT).read_text())
    native=receipt.get('nativeDependencies',{})
    require(native.get('symbolClosureVerified') is True,'Actual native symbol closure required')
    members=native.get('libraries',[])
    require(sum(m.get('path')=='libgcc_s.so.1' and m.get('sha256')==origin['library']['sha256'] for m in members)==1,
            'Embedded libgcc must equal supplier; no host fallback')
    require(receipt.get('linuxBuildInputSha256')==sha(output/'native-input.json'),'Frozen receipt must bind input custody')
    require(receipt.get('linuxBuildSourceSnapshot')==snapshot,'Frozen receipt must bind exact source snapshot')
    require(receipt['helper']['sha256']==sha(output/'frozen'/NAME),'Helper bytes changed')
    assert_stable(source,snapshot);check_output(output)
    return inputs

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--platform',required=True)
    p.add_argument('--python-archive',type=Path,required=True);p.add_argument('--source-date-epoch',type=int,default=0)
    a=p.parse_args()
    print(json.dumps(build(a.source,a.out,a.platform,a.python_archive,epoch=a.source_date_epoch),sort_keys=True))
