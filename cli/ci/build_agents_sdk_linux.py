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
import stat
import time
import uuid
import shutil
import sys
import sdk_native_inventory as native_inventory

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

def check_output(output, *, live=False):
    """Account without following aliases; only active entries may disappear."""
    count=0; size=0
    # Root disappearance/aliasing always refuses, including active scans.
    root=os.open(output,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        require(os.fstat(root).st_uid==os.getuid(),'Owned output directory required')
        def scan(directory):
            nonlocal count,size
            with os.scandir(directory) as entries:
                for entry in entries:
                    # Count each observed entry even if an active writer removes it.
                    count+=1;require(count<=MAX_FILES,'Owned output file limit exceeded')
                    try:
                        info=os.stat(entry.name,dir_fd=directory,follow_symlinks=False)
                    except FileNotFoundError:
                        if not live: raise
                        continue
                    if stat.S_ISREG(info.st_mode):
                        size+=info.st_size
                        require(size<=MAX_RETAINED_BYTES,'Owned output byte limit exceeded')
                    elif stat.S_ISDIR(info.st_mode):
                        try:
                            child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                        except FileNotFoundError:
                            if not live: raise
                            continue
                        try:
                            opened=os.fstat(child)
                            require((opened.st_dev,opened.st_ino)==(info.st_dev,info.st_ino),
                                    'Owned output directory changed during accounting')
                            scan(child)
                        finally: os.close(child)
                    else:
                        # Vendor aliases are accounted as entries, never traversed.
                        require(stat.S_ISLNK(info.st_mode),'Unexpected owned output file type')
        scan(root)
    finally: os.close(root)
    return size

def verify_archive(path, row):
    p=checked_path(path);r=row['pythonArchive']
    require(p.is_file() and p.stat().st_size==r['byteLength'] and sha(p)==r['sha256'],
            'Python archive differs from pin')

def validate_origin(origin, root, row):
    require(isinstance(origin,dict) and set(origin)=={'schema','package','library','licenses'}
            and origin['schema']=='openprose.sdk-native-origin/2','Closed native origin required')
    package=origin['package']
    require(set(package)=={'name','epoch','version','release','architecture','sourceRpm','license'}
            and all(isinstance(v,str) and v and len(v)<=1024 for v in package.values())
            and package['name']=='libgcc' and package['architecture']==row['machine']
            and re.fullmatch('[A-Za-z0-9_.+~-]+[.]src[.]rpm',package['sourceRpm']),
            'Actual supplying RPM and source RPM facts required')
    lib=origin['library'];path=checked_path(root/'libgcc_s.so.1')
    native_inventory.validate_supplier_library(lib)
    require(sha(path)==lib["sha256"] and path.stat().st_size==lib["byteLength"],
            "Supplier origin does not bind exact libgcc bytes")
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
    require(isinstance(image_key,str) and image_key in ('supplier','freezer','runtime'),
            'Closed container image role required')
    require(network in ('none','bridge'),'Closed network mode')
    require(image_key != 'runtime' or network == 'none', 'Runtime network must be none')
    tmpfs = '/tmp:rw,exec,nosuid,nodev,size=536870912' if image_key == 'runtime' else '/tmp:rw,nosuid,nodev,size=536870912'
    owner=owner or uuid.uuid4().hex
    require(re.fullmatch('[0-9a-f]{32}',owner), 'Invalid container owner')
    return ['docker','--config',str(output/'docker-config'),'run','--rm','--pull=never',
            '--name','prose-sdk-'+owner+'-'+script.removesuffix('.sh'),
            '--label',OWNER_LABEL+'='+owner,
            '--platform',row['dockerPlatform'],'--network='+network,'--read-only',
            '--cap-drop=ALL','--security-opt=no-new-privileges','--user',str(os.getuid())+':'+str(os.getgid()),
            '--tmpfs',tmpfs,
            '--mount','type=bind,src='+str(source)+',dst=/source,readonly',
            '--mount','type=bind,src='+str(output)+',dst=/job',
            '--env','HOME=/job/home','--env','TMPDIR=/tmp','--env','PYTHONNOUSERSITE=1',
            '--entrypoint','/bin/bash',row[image_key]['image'],'-euo','pipefail','/job/'+script]

SUPPLIER = r'''/opt/python/cp310-cp310/bin/python /job/supplier.py
'''
SUPPLIER_PY = r'''
import hashlib,json,os,pathlib,re,selectors,signal,stat,subprocess,sys,time

FIELDS=('name','epoch','version','release','architecture','sourceRpm','license')
HEADER='%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCERPM}\t%{LICENSE}\t%{FILEDIGESTALGO}\n'
TABLE='[%{FILENAMES}\t%{FILEDIGESTS}\t%{FILEMODES}\n]'
MAX_RPM_BYTES=262144
MAX_ROWS=128
MAX_LIBRARY_BYTES=8*1024*1024
MAX_LICENSE_BYTES=1048576
MAX_LICENSE_TOTAL=2*1048576
DIAG={}

class SupplierFailure(ValueError):
 def __init__(self,code):
  self.code=code
  super().__init__(code)

def require(ok,code):
 if not ok:raise SupplierFailure(code)

def remaining(deadline):
 value=deadline-time.monotonic()
 require(value>0,'supplier-timeout')
 return value

def rpm(args,deadline):
 end=time.monotonic()+min(5,remaining(deadline))
 env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'}
 p=subprocess.Popen(['rpm',*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    env=env,start_new_session=True)
 data=bytearray()
 try:
  with selectors.DefaultSelector() as selector:
   selector.register(p.stdout,selectors.EVENT_READ)
   while selector.get_map():
    require(time.monotonic()<end,'rpm-timeout')
    for key,_ in selector.select(min(.05,max(0,end-time.monotonic()))):
     chunk=os.read(key.fd,8192)
     if not chunk:selector.unregister(key.fileobj);continue
     require(len(data)+len(chunk)<=MAX_RPM_BYTES,'rpm-output-limit')
     data.extend(chunk)
   try:p.wait(timeout=max(.001,end-time.monotonic()))
   except subprocess.TimeoutExpired:raise SupplierFailure('rpm-timeout')
   require(p.returncode==0,'rpm-query-failed')
   try:return bytes(data).decode('utf-8')
   except UnicodeDecodeError:raise SupplierFailure('rpm-malformed-output')
 finally:
  # Kill the complete query group, including descendants retaining the pipe.
  try:os.killpg(p.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  p.stdout.close()
  try:p.wait(timeout=1)
  except subprocess.TimeoutExpired:pass

def text(value):
 return isinstance(value,str) and 0<len(value)<=1024 and all(32<=ord(c)<127 for c in value)

def direct_path(value,parents):
 if not text(value):return False
 p=pathlib.PurePosixPath(value)
 return str(p)==value and str(p.parent) in parents and p.name not in ('','.','..')

def canonical_path(value):
 return direct_path(value,('/usr/lib64',)) and re.fullmatch(r'libgcc_s(?:-[A-Za-z0-9._+\-]+)?\.so\.1',pathlib.PurePosixPath(value).name) is not None

def payload_path(value,canonical):
 return canonical_path(canonical) and direct_path(value,('/lib64','/usr/lib64')) and pathlib.PurePosixPath(value).name==pathlib.PurePosixPath(canonical).name

def parse_header(line,arch):
 raw=line.split('\t')
 require(len(raw)==8 and all(text(x) for x in raw),'rpm-package-ambiguous')
 package=dict(zip(FIELDS,raw[:7]))
 require(package['name']=='libgcc' and package['architecture']==arch and
         re.fullmatch('[0-9]+',package['epoch']) is not None and
         package['sourceRpm'].endswith('.src.rpm'),'rpm-package-identity')
 if re.fullmatch('[0-9]+',raw[7]):DIAG['algorithm']=int(raw[7])
 require(raw[7]=='8','rpm-digest-algorithm')
 DIAG.update(algorithm=8,packageCount=1)
 return package

def parse_table(output,arch):
 lines=output.splitlines()
 require(0<len(lines)<=MAX_ROWS+1,'rpm-row-limit')
 package=parse_header(lines[0],arch)
 rows=[]
 for line in lines[1:]:
  parts=line.split('\t')
  require(len(parts)==3 and text(parts[0]) and (not parts[1] or text(parts[1])) and len(parts[1])<=128 and
          re.fullmatch('[0-9]+',parts[2]) is not None,'rpm-malformed-row')
  mode=int(parts[2]);require(0<=mode<=65535,'rpm-malformed-row')
  rows.append({'path':parts[0],'digest':parts[1],'mode':mode})
 DIAG['rowCount']=len(rows)
 return package,rows

def identity(info):return info.st_dev,info.st_ino
def stable(info):return identity(info)+(info.st_mode,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def select_payload(rows,canonical,info,lstat=None):
 require(canonical_path(canonical) and stat.S_ISREG(info.st_mode),'supplier-canonical-file')
 lstat=lstat or (lambda name:pathlib.Path(name).lstat())
 matches=[]
 for row in rows:
  if not payload_path(row['path'],canonical):continue
  try:found=lstat(row['path'])
  except FileNotFoundError:continue
  require(stat.S_ISREG(found.st_mode),'rpm-payload-nonregular')
  if identity(found)!=identity(info):continue
  require(stat.S_ISREG(row['mode']),'rpm-mode-nonregular')
  matches.append(row)
 DIAG['matchCount']=len(matches)
 require(len(matches)==1,'rpm-payload-ambiguity')
 row=matches[0]
 require(re.fullmatch('[0-9a-f]{64}',row['digest']) is not None,'rpm-payload-digest-format')
 DIAG.update(payloadPath=row['path'],expectedSha256=row['digest'])
 return row

def verify_owner(output,package,arch):
 lines=output.splitlines()
 require(len(lines)==1,'rpm-owner-ambiguity')
 owner=parse_header(lines[0],arch)
 DIAG.update(ownerName=owner['name'],ownerArchitecture=owner['architecture'],ownerVersion=owner['version'],ownerRelease=owner['release'])
 require(owner==package,'rpm-owner-mismatch')

def read_regular(path,maximum,deadline):
 remaining(deadline)
 before=path.lstat()
 require(stat.S_ISREG(before.st_mode),'supplier-nonregular-file')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as f:
  opened=os.fstat(f.fileno())
  require(stable(opened)==stable(before),'supplier-identity-change')
  require(0<opened.st_size<=maximum,'supplier-file-size')
  data=f.read(maximum+1)
  require(len(data)==opened.st_size and len(data)<=maximum,'supplier-file-size')
  require(stable(os.fstat(f.fileno()))==stable(opened) and stable(path.lstat())==stable(opened),'supplier-identity-change')
 remaining(deadline)
 return data,opened

def check_binding(canonical,row,info,lstat=None):
 lstat=lstat or (lambda name:pathlib.Path(name).lstat())
 require(stable(lstat(canonical))==stable(info) and stable(lstat(row['path']))==stable(info),'supplier-identity-change')

def verify_digest(data,row):
 digest=hashlib.sha256(data).hexdigest()
 DIAG['measuredSha256']=digest
 require(digest==row['digest'],'rpm-payload-digest-mismatch')
 return digest

def diagnostic(code):
 record={'phase':DIAG.get('phase','supplier'),'code':code}
 for key in ('architecture','algorithm','packageCount','rowCount','matchCount','canonicalPath','payloadPath','expectedSha256','measuredSha256','ownerName','ownerArchitecture','ownerVersion','ownerRelease'):
  value=DIAG.get(key)
  if type(value) is int or text(value):record[key]=value
 encoded=json.dumps(record,sort_keys=True)
 if len(encoded.encode())>4096:encoded=json.dumps({'phase':'supplier','code':code})
 return encoded

def main():
 deadline=time.monotonic()+30
 DIAG['phase']='target'
 target=pathlib.Path('/job/target.json')
 require(target.stat().st_size<=65536,'supplier-target-size')
 arch=json.loads(target.read_text())['machine']
 require(arch in ('x86_64','aarch64'),'supplier-target-architecture')
 DIAG['architecture']=arch
 p=pathlib.Path('/usr/lib64/libgcc_s.so.1').resolve(strict=True)
 require(canonical_path(str(p)),'supplier-canonical-path')
 DIAG.update(phase='rpm-package',canonicalPath=str(p))
 package,rows=parse_table(rpm(['-q','--qf',HEADER+TABLE,'libgcc'],deadline),arch)
 info=p.lstat();row=select_payload(rows,str(p),info)
 DIAG['phase']='rpm-owner'
 verify_owner(rpm(['-qf','--qf',HEADER,row['path']],deadline),package,arch)
 DIAG['phase']='payload'
 b,read_info=read_regular(p,MAX_LIBRARY_BYTES,deadline)
 require(stable(read_info)==stable(info),'supplier-identity-change')
 check_binding(str(p),row,info)
 require(b[:4]==b'\x7fELF','supplier-not-elf')
 digest=verify_digest(b,row)
 DIAG['phase']='licenses'
 licenses=[];license_bytes=[];seen=set();total=0
 for item in rows:
  name=item['path']
  if not direct_path(name,('/usr/share/licenses',)) and not name.startswith('/usr/share/licenses/'):continue
  require(text(name) and str(pathlib.PurePosixPath(name))==name and '..' not in pathlib.PurePosixPath(name).parts,'supplier-license-path')
  require(name not in seen,'supplier-license-duplicate');seen.add(name)
  q=pathlib.Path(name)
  if not stat.S_ISREG(item['mode']):continue
  data,_=read_regular(q,MAX_LICENSE_BYTES,deadline)
  total+=len(data)
  require(len(licenses)<32 and total<=MAX_LICENSE_TOTAL,'supplier-license-limit')
  licenses.append({'packagePath':name,'path':'license-'+str(len(licenses))+'.txt','sha256':hashlib.sha256(data).hexdigest(),'byteLength':len(data)})
  license_bytes.append(data)
 texts=b'\n'.join(license_bytes)
 require(b'GCC RUNTIME LIBRARY EXCEPTION' in texts and b'GNU GENERAL PUBLIC LICENSE' in texts,'supplier-license-texts')
 DIAG['phase']='publish'
 verify_owner(rpm(['-q','--qf',HEADER,'libgcc'],deadline),package,arch)
 check_binding(str(p),row,info)
 remaining(deadline)
 out=pathlib.Path('/job/supplier');out.mkdir()
 (out/'libgcc_s.so.1').write_bytes(b)
 copied=(out/'libgcc_s.so.1').read_bytes()
 require(len(copied)==len(b) and hashlib.sha256(copied).hexdigest()==digest,'supplier-copy-digest')
 for item,data in zip(licenses,license_bytes):(out/item['path']).write_bytes(data)
 record={'schema':'openprose.sdk-native-origin/2','package':package,'library':{
  'path':'libgcc_s.so.1','sha256':digest,'byteLength':len(b),'supplierPath':str(p),
  'rpmPayloadPath':row['path'],'rpmFileDigestAlgorithm':8,'rpmFileDigestSha256':row['digest'],
  'rpmFileDigestVerified':True},'licenses':licenses}
 remaining(deadline)
 (out/'origin.json').write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')

if __name__=='__main__':
 try:main()
 except Exception as error:
  code=error.code if isinstance(error,SupplierFailure) else 'supplier-internal-failure'
  try:print(diagnostic(code),file=sys.stderr)
  except Exception:pass
  sys.exit(1)
'''
TRANSPORT_PIN = r'''import hashlib,json,pathlib,re

def verify_transport(root):
 root=pathlib.Path(root)
 def read(name,limit):
  p=root/name
  assert p.is_file() and not p.is_symlink() and p.stat().st_size<=limit, 'Transport metadata path invalid'
  return json.loads(p.read_text())
 r=read('target.json',65536)['pythonArchive']
 x=read('python-transport.json',16384)
 assert set(x)=={'schema','archiveSha256','archiveByteLength','tarSha256','tarByteLength','decoder'}
 assert x['schema']=='openprose.sdk-python-transport/1'
 assert x['archiveSha256']==r['sha256'] and type(x['archiveByteLength']) is int and x['archiveByteLength']==r['byteLength']
 assert re.fullmatch('[0-9a-f]{64}',x['tarSha256']) and type(x['tarByteLength']) is int and 0<x['tarByteLength']<=4*1024**3
 decoder=x['decoder']
 assert set(decoder)=={'path','sha256','version'}
 assert isinstance(decoder['path'],str) and decoder['path'].startswith('/') and len(decoder['path'])<=4096 and not any(ord(c)<32 for c in decoder['path'])
 assert re.fullmatch('[0-9a-f]{64}',decoder['sha256'])
 assert isinstance(decoder['version'],str) and 0<len(decoder['version'])<=4096 and not any(ord(c)<32 for c in decoder['version'])
 for name,length,digest in [('python-full.tar.zst',r['byteLength'],r['sha256']),('python-full.tar',x['tarByteLength'],x['tarSha256'])]:
  p=root/name
  assert p.is_file() and not p.is_symlink() and p.stat().st_size==length, 'Transport byte length differs'
  h=hashlib.sha256()
  with p.open('rb') as f:
   for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
  assert h.hexdigest()==digest, 'Transport digest differs'
 return x
'''
PREPARE = "/opt/python/cp310-cp310/bin/python - <<'PIN'\n" + TRANSPORT_PIN + "\nverify_transport('/job')\nPIN\n" + r'''
mkdir /job/python-full
cd /job/python-full
tar -xf /job/python-full.tar
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

def cleanup_failed_process(child, output, label, primary):
    """Keep the primary failure when process-group cleanup itself is refused."""
    try:stop_process_tree(child)
    except Exception as cleanup:
        facts={'schema':'openprose.sdk-process-cleanup-failure/1','pid':child.pid,
               'primaryType':type(primary).__name__,'cleanupType':type(cleanup).__name__,
               'errno':getattr(cleanup,'errno',None),'cleanupVerified':False}
        primary.cleanup_failure=facts
        try:
            (output/(label+'-cleanup-error.json')).write_text(json.dumps(facts,sort_keys=True)+'\n')
        except OSError:
            # Attribute remains available even when the owned disk cannot retain diagnostics.
            pass


def process(command, output, label, *, timeout=1200, max_bytes=MAX_LOG_BYTES, deadline=None):
    env={'PATH':os.environ['PATH'],'HOME':str(output/'home'),'LANG':'C.UTF-8'}
    deadline=min(time.monotonic()+timeout,deadline) if deadline is not None else time.monotonic()+timeout
    require(time.monotonic()<deadline,'Process deadline exceeded: '+label)
    p=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT,start_new_session=True)
    total=0; next_audit=0
    try:
        with (output/(label+'.log')).open('wb') as log, selectors.DefaultSelector() as events:
            events.register(p.stdout,selectors.EVENT_READ)
            while events.get_map():
                require(time.monotonic()<deadline,'Process deadline exceeded: '+label)
                if time.monotonic()>=next_audit:
                    check_output(output,live=p.poll() is None)
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
    except BaseException as primary:
        cleanup_failed_process(p,output,label,primary); raise
    finally:
        if p.stdout: p.stdout.close()
    return (output/(label+'.log')).read_bytes()

def stream_transport(command, output, deadline, audit):
    """Keep binary stdout separate from bounded diagnostics in fresh owned files."""
    env={'PATH':os.environ['PATH'],'HOME':str(output/'home'),'LANG':'C.UTF-8'}
    require(time.monotonic()<deadline,'Preparation deadline exceeded')
    with (output/'python-full.tar').open('xb') as tar, (output/'decode-python.log').open('xb') as log:
        p=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,start_new_session=True)
        total=0; next_audit=0
        try:
            with selectors.DefaultSelector() as events:
                events.register(p.stdout,selectors.EVENT_READ,'tar')
                events.register(p.stderr,selectors.EVENT_READ,'log')
                while events.get_map():
                    require(time.monotonic()<deadline,'Preparation deadline exceeded')
                    if time.monotonic()>=next_audit:
                        audit();check_output(output,live=p.poll() is None)
                        require(shutil.disk_usage(output).free>=512*1024**2,'Owned scratch reserve exhausted')
                        next_audit=time.monotonic()+1
                    for key,_ in events.select(min(.1,max(0,deadline-time.monotonic()))):
                        data=os.read(key.fileobj.fileno(),65536)
                        if not data: events.unregister(key.fileobj);continue
                        # Flush both streams before aggregate accounting; never write a chunk
                        # that would exceed the existing entire-job retained-byte quota.
                        tar.flush();log.flush()
                        require(check_output(output,live=p.poll() is None)+len(data)<=MAX_RETAINED_BYTES,'Owned output byte limit exceeded')
                        if key.data=='tar':tar.write(data)
                        else:
                            total+=len(data)
                            require(total<=MAX_LOG_BYTES,'Decoder diagnostic limit exceeded')
                            log.write(data)
                p.wait(timeout=max(.01,deadline-time.monotonic()))
                require(p.returncode==0,'Inspect retained decode-python.log')
                tar.flush();log.flush();audit();check_output(output)
                require(time.monotonic()<deadline,'Preparation deadline exceeded')
        except BaseException as primary:
            cleanup_failed_process(p,output,'decode-python',primary);raise
        finally:
            p.stdout.close();p.stderr.close()
    require((output/'python-full.tar').stat().st_size>0,'Empty decoded Python archive')


def prepare_transport(output, row, deadline, audit):
    audit();verify_archive(output/'python-full.tar.zst',row)
    executable=shutil.which('zstd')
    require(executable is not None,'Host zstd prerequisite missing')
    executable=Path(executable).resolve()
    require(executable.is_file() and executable.stat().st_size<=32*1024**2,'Host zstd prerequisite invalid')
    identity=sha(executable)
    remaining=deadline-time.monotonic();require(remaining>0,'Preparation deadline exceeded')
    raw=process([str(executable),'--version'],output,'decoder-version',timeout=min(5,remaining),max_bytes=4096,deadline=deadline)
    version=raw.decode('utf-8').strip()
    require(version and len(version)<=4096 and not any(ord(c)<32 for c in version),'Host zstd version invalid')
    require(sha(executable)==identity,'Host zstd identity changed')
    audit();verify_archive(output/'python-full.tar.zst',row)
    stream_transport([str(executable),'--decompress','--stdout','--quiet','-M128MB',str(output/'python-full.tar.zst')],output,deadline,audit)
    audit();verify_archive(output/'python-full.tar.zst',row)
    require(sha(executable)==identity,'Host zstd identity changed')
    plain=checked_path(output/'python-full.tar')
    descriptor={'schema':'openprose.sdk-python-transport/1','archiveSha256':row['pythonArchive']['sha256'],
                'archiveByteLength':row['pythonArchive']['byteLength'],'tarSha256':sha(plain),
                'tarByteLength':plain.stat().st_size,'decoder':{'path':str(executable),'sha256':identity,'version':version}}
    require(time.monotonic()<deadline,'Preparation deadline exceeded')
    encoded=json.dumps(descriptor,sort_keys=True)
    require(check_output(output)+len(encoded.encode())<=MAX_RETAINED_BYTES,'Owned output byte limit exceeded')
    require(sum(1 for _ in output.rglob('*'))<MAX_FILES,'Owned output file limit exceeded')
    with (output/'python-transport.json').open('x') as f:f.write(encoded)
    validate_transport(output);audit();check_output(output)
    return descriptor


def validate_transport(output):
    namespace={};exec(TRANSPORT_PIN,namespace)
    return namespace['verify_transport'](output)


def prepare_python(output, row, command, audit, execute):
    deadline=time.monotonic()+1200
    transport=prepare_transport(output,row,deadline,audit)
    require(validate_transport(output)==transport,'Python transport descriptor changed')
    require(time.monotonic()<deadline,'Preparation deadline exceeded')
    execute(command,'prepare',deadline=deadline)
    require(validate_transport(output)==transport,'Python transport descriptor changed')
    require(time.monotonic()<deadline,'Preparation deadline exceeded')


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

def run(command, output, label, *, timeout=1200, max_bytes=MAX_LOG_BYTES, deadline=None):
    try: return process(command,output,label,timeout=timeout,max_bytes=max_bytes,deadline=deadline)
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
    prepare_python(output,r,commands[1],lambda:assert_stable(source,snapshot),guarded)
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
