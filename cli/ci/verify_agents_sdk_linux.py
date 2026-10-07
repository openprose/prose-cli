"""Verify the exact frozen SDK in a native minimal GNU 2.34 runtime."""
import argparse,hashlib,json,os,platform,re,shutil,sys
from pathlib import Path
import build_agents_sdk_linux as driver
import sdk_native_inventory as inventory
LOCK=Path(__file__).with_name('agents-sdk-linux-runtime.lock.json')
NAMES=('prose-agents-sdk','agents-sdk-build.json','AGENTS-SDK-NOTICES.txt')
PROBES=(('version','--version',5),('imports','--packaged-self-test',30),
        ('tools','--packaged-tool-self-test',30),('libraries','--packaged-library-test',30))
BASE='''test "$(getconf GNU_LIBC_VERSION)" = 'glibc 2.34'
test "$(uname -m)" = '{machine}'
for tool in python python3 pip pip3 gcc g++ cc clang make rustc cargo bun node; do
  if command -v "$tool" >/dev/null 2>&1; then echo "Unexpected build/interpreter tool: $tool" >&2; exit 1; fi
done
printf 'glibc=2.34\\narchitecture={machine}\\nnoBuildTools=true\\n'
'''
def load_lock():
    x=json.loads(LOCK.read_text());driver.require(set(x)=={'schema','platforms'} and x['schema']=='openprose.sdk-linux-runtime-inputs/1','Closed runtime lock required')
    driver.require(set(x['platforms'])=={'linux-x64-gnu','linux-arm64-gnu'},'Closed runtime targets')
    for target,machine,arch in [('linux-x64-gnu','x86_64','amd64'),('linux-arm64-gnu','aarch64','arm64')]:
        r=x['platforms'][target];v=r['runtime']
        driver.require(set(r)=={'machine','dockerPlatform','runtime'} and r['machine']==machine and r['dockerPlatform']=='linux/'+arch,
                       'Runtime native target mismatch')
        driver.require(set(v)=={'image','configSha256','metadataUrl','indexDigest'} and re.fullmatch('quay.io/almalinuxorg/9-minimal@sha256:[0-9a-f]{64}',v['image'])
                       and re.fullmatch('[0-9a-f]{64}',v['configSha256']) and re.fullmatch('sha256:[0-9a-f]{64}',v['indexDigest']), 'Pinned minimal runtime required')
    return x

def snapshot(build):
    files={name:driver.checked_path(build/name) for name in NAMES}
    driver.require(all(p.is_file() and 0<p.stat().st_size<=256*1024**2 for p in files.values()),'Regular bounded frozen trio required')
    driver.require(files[NAMES[1]].stat().st_size<=2*1024**2,'Bounded build receipt required')
    return {'payload':{name:driver.sha(p) for name,p in files.items()},'driverSha256':driver.sha(Path(__file__)),
            'lockSha256':driver.sha(LOCK),'lifecycleSha256':driver.sha(Path(driver.__file__)), 'inventorySha256':driver.sha(Path(inventory.__file__))}

def verify(build,output,target):
    build=driver.checked_path(build);out=driver.checked_path(output);row=load_lock()['platforms'].get(target)
    driver.require(row is not None and sys.platform=='linux' and platform.machine()==row['machine'],'Native runtime host required')
    driver.require(build.is_dir() and out.parent.is_dir() and not out.exists(),'Fresh runtime output required')
    driver.require(shutil.disk_usage(out.parent).free>=2*1024**3,'Runtime verification needs2GiB free')
    initial=snapshot(build);receipt=json.loads((build/NAMES[1]).read_text())
    inventory.validate_linux_receipt(receipt)
    driver.require(receipt['helper']['sha256']==initial['payload'][NAMES[0]] and receipt['helper']['byteLength']==(build/NAMES[0]).stat().st_size
                   and receipt['platform']=='linux' and receipt['architecture']==row['machine'] and receipt['notices']['sha256']==initial['payload'][NAMES[2]]
                   and receipt.get('nativeDependencies',{}).get('symbolClosureVerified') is True,'Frozen native receipt custody mismatch')
    with (build/NAMES[0]).open('rb') as stream:header=stream.read(20)
    driver.require(header[:6]==b'\x7fELF\x02\x01' and int.from_bytes(header[18:20],'little')==({'x86_64':62,'aarch64':183}[row['machine']]),'Actual helper ELF architecture differs')
    out.mkdir();job=out/'job';job.mkdir();(job/'home').mkdir();(job/'docker-config').mkdir();payload=out/'payload';payload.mkdir()
    for name in NAMES:shutil.copyfile(build/name,payload/name);(payload/name).chmod(0o555 if name==NAMES[0] else 0o444)
    def stable():
        driver.require(snapshot(build)==initial,'Input trio/runtime driver/lock/lifecycle changed')
        driver.require({name:driver.sha(driver.checked_path(payload/name)) for name in NAMES}==initial['payload'],'Copied frozen trio changed')
    def run(command,label,**kwargs):
        stable()
        try:return driver.run(command,job,label,**kwargs)
        finally:stable()
    stable();image=row['runtime']['image'];prefix=['docker','--config',str(job/'docker-config')]
    run(prefix+['pull','--platform',row['dockerPlatform'],image],'pull-runtime')
    info=json.loads(run(prefix+['image','inspect','--format','{{json .}}',image],'inspect-runtime',timeout=15,max_bytes=65536));driver.validate_image(info,row,'runtime')
    results={};all_probes=[('base',BASE.format(machine=row['machine']),30)]+[(name,'timeout --kill-after=1s '+str(seconds)+'s /source/prose-agents-sdk '+flag+'\n',seconds) for name,flag,seconds in PROBES]
    for label,script,seconds in all_probes:
        (job/(label+'.sh')).write_text(script)
        c=driver.container_command(row,'runtime',payload,job,label+'.sh',network='none')
        raw=run(c,label,timeout=seconds+20,max_bytes=2*1024**2)
        if label=='base':driver.require(raw.decode()=='glibc=2.34\narchitecture='+row['machine']+'\nnoBuildTools=true\n','Actual runtime facts mismatch')
        elif label=='version':driver.require(raw.decode().strip()=='prose-agents-sdk 0.1.0','Cold version probe differs')
        else:
            result=json.loads(raw);expected=receipt[{'imports':'selfTest','tools':'toolSelfTest','libraries':'linuxLibraries'}[label]]
            driver.require(result==expected,'Runtime '+label+' differs from exact construction receipt');results[label]=result
            if label=='libraries':driver.require(tuple(int(v) for v in result['requiredGlibcMaximum'].split('.'))<=(2,34),'Actual runtime requires newer GNU libc')
    stable();report={'schema':'openprose.sdk-linux-clean-runtime/1','platform':target,'runtime':row['runtime'],'inputs':initial,
      'results':results,'version':'prose-agents-sdk 0.1.0','modelCalls':0,'networkUsed':False,
      'networkUsageScope':'runtime-probes-only','preparationNetworkEnabled':True,'cpuFloorQualified':False,
      'qualification':'native-clean-glibc-2.34-only','publicationAuthorized':False}
    (out/'runtime-report.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n');return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--sdk-build',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--platform',required=True)
    a=p.parse_args();print(json.dumps(verify(a.sdk_build,a.out,a.platform),sort_keys=True))
