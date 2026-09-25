#!/usr/bin/env python3
"""Exercise both public CLI implementations against exact registry byte fixtures."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from canonical_json import canonicality_problem

FIXTURES = Path(__file__).resolve().parents[2] / 'shared' / 'fixtures' / 'registry'
TOKEN = 'rr_test_' + '1' * 32


def run(command):
    count = 0
    for name in ('single-file', 'directory'):
        source = json.loads((FIXTURES / (name + '.json')).read_text())
        receipt = json.loads((FIXTURES / (name + '.receipt.json')).read_text())
        canonical = (FIXTURES / (name + '.canonical.json')).read_text()
        m = source['manifest']
        prefix = '/registry/v1/organizations/' + m['organization'] + '/packages/' + m['package']
        exact = prefix + '/versions/' + m['version']
        for selected in ('production',):
            with tempfile.TemporaryDirectory(prefix='prose-registry-oracle-') as directory:
                root = Path(directory).resolve()
                directory = str(root)
                package = root / 'source'; package.mkdir()
                for file in source['files']:
                    path = package / file['path']; path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(file['content'].encode() if file['encoding'] == 'utf8' else base64.b64decode(file['content']))
                source_path = package / source['files'][0]['path']
                if name == 'directory':
                    (package / 'prose-package.json').write_text(json.dumps({'schema':'prose-package-directory-v1','files':[f['path'] for f in source['files']], 'exports':m['exports'], 'dependencies':m['dependencies']}))
                    source_path = package
                env = {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':directory,'XDG_CONFIG_HOME':str(root/'config'),'TMPDIR':directory,'HTTP_PROXY':'http://127.0.0.1:9','HTTPS_PROXY':'http://127.0.0.1:9','ALL_PROXY':'http://127.0.0.1:9','NO_PROXY':''}
                fixture = root / 'service.json'; env['PROSE_TEST_SERVICE_FIXTURE'] = str(fixture)
                def invoke(args, exchanges, expected_code=0, human=False):
                    fixture.write_text(json.dumps({'environment':'production','credentials':{'production':TOKEN},'storeAvailable':True,'exchanges':exchanges}))
                    p = subprocess.run([*command,'--output','human' if human else 'json','cli','package',*args],cwd=root,env=env,capture_output=True,timeout=15)
                    assert p.returncode == expected_code, (args,p.returncode,p.stdout.decode(),p.stderr.decode())
                    if human:
                        assert p.stderr.decode() == ""
                        return p.stdout.decode()
                    report = json.loads(p.stdout)
                    assert canonicality_problem(p.stdout) is None, (args, canonicality_problem(p.stdout))
                    assert report['schema']=='openprose.service-operation/1' and report['operation']=='package.'+args[0]
                    assert TOKEN.encode() not in p.stdout+p.stderr and not p.stderr
                    assert set(report)=={'schema','operation','interaction','result','problem'}
                    if expected_code==0: assert report['problem'] is None
                    return report
                post={'method':'POST','path':prefix+'/versions','status':201,'body':receipt,'expectedBody':canonical,'expectedSha256':receipt['reference']['sha256']}
                args=['publish',str(source_path),'--organization',m['organization'],'--name',m['package'],'--version',m['version']]
                if m.get('visibility')=='public': args+=['--public']
                assert invoke(args,[post])['result']==receipt; count+=1
                ref=m['organization']+'/'+m['package']+'@'+m['version']
                reads=[{'method':'GET','path':exact,'status':200,'body':receipt},{'method':'GET','path':exact+'/artifact','status':200,'body':canonical}]
                dest=root/'installed'
                assert invoke(['fetch',ref,'--output-dir',str(dest)],reads)['result']==receipt
                for file in source['files']:
                    assert (dest/file['path']).read_bytes()==(package/file['path']).read_bytes()
                assert json.loads((dest/'.prose-package-receipt.json').read_text())==receipt;count+=1
                listing={'packages':[receipt] if receipt['visibility']=='public' else [],'nextCursor':None}
                assert invoke(['list',m['organization']],[{'method':'GET','path':'/registry/v1/organizations/'+m['organization']+'/packages','status':200,'body':listing}])['result']==listing;count+=1
                result={'receipt':receipt,'withdrawn':True}
                assert invoke(['withdraw',ref],[{'method':'POST','path':exact+'/withdraw','status':200,'body':result}])['result']==result;count+=1
                tampered=[reads[0],{**reads[1],'body':canonical.replace('prose-package-v1','prose-package-v2')}]
                bad=root/'tampered';assert invoke(['fetch',ref,'--output-dir',str(bad)],tampered,10)['result'] is None;assert not bad.exists();count+=1
                for status in (404, 413, 429):
                    absent=root/('absent-'+str(status))
                    error=invoke(['fetch',ref,'--output-dir',str(absent)],[{'method':'GET','path':exact,'status':status,'body':{'message':'untrusted upstream details'}}],10)
                    # A 404 names the missing version and is not retryable.
                    if status == 404:
                        problem=error['problem']
                        assert problem['code']=='SERVICE_RESOURCE_NOT_FOUND' and problem['retryable'] is False, problem
                        assert problem['details']['resource']=={'kind':'package','id':ref}, problem
                        assert problem['details']['suggestedArgv']==['--output','json','cli','package','list',m['organization']], problem
                        assert ref in problem['details']['reason'], problem
                    else:
                        assert error['problem']['code']=='SERVICE_UNAVAILABLE'
                    assert not absent.exists();count+=1
                assert invoke(['withdraw',ref],[{'method':'POST','path':exact+'/withdraw','status':200,'body':result}],human=True)==f'OpenProse package withdraw: {ref}\n';count+=1
                expected_list=ref+'\n' if receipt['visibility']=='public' else 'No public packages in '+m['organization']+'.\n'
                assert invoke(['list',m['organization']],[{'method':'GET','path':'/registry/v1/organizations/'+m['organization']+'/packages','status':200,'body':listing}],human=True)==expected_list;count+=1
                if name=='directory':
                    manifest=package/'prose-package.json'
                    text=manifest.read_text();manifest.write_text(text.replace('"schema":', '"schema":"duplicate", "schema":',1))
                    error=invoke(args,[],2)
                    assert error['problem']['code']=='CONFIG_INVALID';count+=1
                print('PASS',name,selected,'registry publication, integrity, HTTP errors and human output')
    print('PASS',count,'registry process cases')

if __name__=='__main__':
    args=sys.argv[1:]
    if args[:1]==['--']:args=args[1:]
    if not args:raise SystemExit('Supply a test-seam CLI after --')
    run(args)
