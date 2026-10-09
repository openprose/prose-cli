"""Prefetch the exact native full Python archive before offline SDK freezing.

The transfer deadline is cooperative. Native workflows additionally enforce a
130-second GNU timeout with forced termination one second later.
"""
import argparse,hashlib,json,os,platform,time,sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener,ProxyHandler,HTTPRedirectHandler,Request
import build_agents_sdk_linux as driver

HOSTS={'github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}
def checked_url(url):
    u=urlsplit(url)
    driver.require(u.scheme=='https' and u.hostname in HOSTS and u.port in (None,443)
                   and u.username is None and u.password is None and not u.fragment,
                   'Unapproved Python artifact URL')
    return url
class PinnedRedirects(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        checked_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def _fetch(target,destination,*,opener=None,clock=time.monotonic,timeout=120,phase):
    phase[0]='selection'
    row=driver.load_lock()['platforms'].get(target);driver.require(row is not None,'Unsupported native target')
    driver.require(os.name=='posix' and platform.system()=='Linux' and platform.machine()==row['machine'],'Native Linux target required')
    p=driver.checked_path(destination);driver.require(p.parent.is_dir() and not p.exists(),'Fresh owned archive destination required')
    partial=driver.checked_path(p.with_name(p.name+'.part'));driver.require(not partial.exists(),'Fresh owned partial required')
    r=row['pythonArchive'];checked_url(r['url']);deadline=clock()+timeout
    # Explicit opener excludes proxy discovery, cookie jars and auth handlers.
    opener=opener or build_opener(ProxyHandler({}),PinnedRedirects())
    h=hashlib.sha256();size=0
    phase[0]='opening'
    with partial.open('xb') as out:
        with opener.open(Request(r['url'],headers={'User-Agent':'OpenProse-pinned-build-input/1','Accept-Encoding':'identity'}),timeout=min(10,timeout)) as response:
            checked_url(response.geturl());driver.require(response.status==200,'Python artifact HTTP response failed')
            encoding=response.headers.get('Content-Encoding','identity');driver.require(encoding=='identity','Encoded artifact response refused')
            length=response.headers.get('Content-Length')
            driver.require(length is None or (length.isdigit() and int(length)==r['byteLength']),'Python artifact HTTP size differs from pin')
            phase[0]='reading'
            while True:
                driver.require(clock()<deadline,'Python artifact transfer deadline exceeded')
                b=response.read(min(1024*1024,r['byteLength']-size+1))
                if not b:break
                driver.require(size+len(b)<=r['byteLength'],'Python artifact exceeds pinned length')
                out.write(b);h.update(b);size+=len(b)
    phase[0]='verification'
    driver.require(size==r['byteLength'] and h.hexdigest()==r['sha256'],'Python artifact bytes differ from pin')
    driver.verify_archive(partial,row)
    # Do not replace a destination created concurrently after the fresh check.
    phase[0]='publication'
    os.link(partial,p);partial.unlink();driver.verify_archive(p,row)
    return {'platform':target,'path':str(p),'byteLength':size,'sha256':h.hexdigest(),'url':r['url']}

class ArtifactPreparationError(ValueError):
    pass

def fetch(target,destination,*,opener=None,clock=time.monotonic,timeout=120):
    phase=['selection']
    try:return _fetch(target,destination,opener=opener,clock=clock,timeout=timeout,phase=phase)
    except Exception as error:
        # Never stringify network exceptions: HTTPError/URLError may contain a
        # signed redirect URL or headers. Only closed phase/class diagnostics.
        allowed={'HTTPError','URLError','TimeoutError','OSError','FileExistsError',
                 'FileNotFoundError','PermissionError','ValueError','KeyError',
                 'SSLError','IncompleteRead','ConnectionResetError','BrokenPipeError'}
        name=type(error).__name__
        if name not in allowed:name='Exception'
        raise ArtifactPreparationError('Python artifact preparation failed during '+phase[0]+' ('+name+')') from None

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--platform',required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    try:print(json.dumps(fetch(a.platform,a.out),sort_keys=True))
    except ArtifactPreparationError as error:
        print(str(error),file=sys.stderr);raise SystemExit(1) from None
