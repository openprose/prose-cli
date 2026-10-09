import copy,hashlib,io,json,tempfile,unittest,sys,time
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch
import prepare_agents_sdk_linux as fetcher
import verify_agents_sdk_linux as verifier
import build_agents_sdk_linux as d

class Response(io.BytesIO):
 def __init__(self,data,url,headers=None,status=200):super().__init__(data);self.url=url;self.headers=headers or {};self.status=status
 def geturl(self):return self.url
class Opener:
 def __init__(self,response):self.response=response;self.requests=[]
 def open(self,request,**kwargs):self.requests.append((request,kwargs));return self.response
class FetchControls(unittest.TestCase):
 def fixture(self,data=b'pinned test bytes'):
  lock=copy.deepcopy(d.load_lock());r=lock['platforms']['linux-x64-gnu']['pythonArchive'];r.update(byteLength=len(data),sha256=hashlib.sha256(data).hexdigest())
  return lock,r
 def transfer(self,data=b'pinned test bytes',kind=None):
  lock,r=self.fixture();url=r['url'] if kind!='redirect' else 'http://untrusted.invalid/artifact';headers={}
  if kind=='length':headers['Content-Length']='999'
  if kind=='encoding':headers['Content-Encoding']='gzip'
  opener=Opener(Response(data,url,headers))
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();out=root/'python-full.tar.zst'
   with patch.object(d,'load_lock',return_value=lock),patch.object(fetcher.platform,'system',return_value='Linux'),patch.object(fetcher.platform,'machine',return_value='x86_64'):
    if kind:
     with self.assertRaises(ValueError):fetcher.fetch('linux-x64-gnu',out,opener=opener)
     self.assertFalse(out.exists());self.assertLessEqual(out.with_name(out.name+'.part').stat().st_size,r['byteLength'])
    else:
     report=fetcher.fetch('linux-x64-gnu',out,opener=opener);self.assertEqual(out.read_bytes(),data);self.assertEqual(report['sha256'],r['sha256'])
   return opener
 def test_pinned_bounded_transfer(self):self.assertEqual(self.transfer().requests[0][1]['timeout'],10)
 def test_bad_hash_or_length_and_encoding_refused(self):
  for kind,data in [('hash',b'changed'),('overflow',b'x'*100),('length',b'pinned test bytes'),('encoding',b'pinned test bytes'),('redirect',b'pinned test bytes')]:
   with self.subTest(kind=kind):self.transfer(data,kind)
 def test_deadline_and_existing_output_refused(self):
  lock,r=self.fixture()
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();out=root/'python';opener=Opener(Response(b'pinned test bytes',r['url']))
   with patch.object(d,'load_lock',return_value=lock),patch.object(fetcher.platform,'system',return_value='Linux'),patch.object(fetcher.platform,'machine',return_value='x86_64'):
    with self.assertRaises(ValueError):fetcher.fetch('linux-x64-gnu',out,opener=opener,clock=iter([0,121]).__next__)
    out.write_bytes(b'owned existing')
    with self.assertRaises(ValueError):fetcher.fetch('linux-x64-gnu',out,opener=opener)
    self.assertEqual(out.read_bytes(),b'owned existing')
 def test_signed_http_error_diagnostic_redacts_url(self):
  lock,r=self.fixture();signed='https://release-assets.githubusercontent.com/file?secret=signed-sentinel'
  class FailedOpener:
   def open(self,*a,**kw):raise HTTPError(signed,403,'signed-sentinel',{},None)
  with tempfile.TemporaryDirectory() as t,patch.object(d,'load_lock',return_value=lock),patch.object(fetcher.platform,'system',return_value='Linux'),patch.object(fetcher.platform,'machine',return_value='x86_64'):
   with self.assertRaises(fetcher.ArtifactPreparationError) as e:fetcher.fetch('linux-x64-gnu',Path(t).resolve()/'archive',opener=FailedOpener())
   self.assertEqual(str(e.exception),'Python artifact preparation failed during opening (HTTPError)')
   self.assertNotIn('signed-sentinel',str(e.exception));self.assertNotIn('https://',str(e.exception))
 def test_actual_delayed_read_is_terminated_by_external_bounded_runner(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();(root/'home').mkdir();effect=root/'read-completed'
   code="""import io,time,pathlib,hashlib
import sys;sys.path.insert(0, MODULE_DIRECTORY)
import prepare_agents_sdk_linux as f
import build_agents_sdk_linux as d
x=d.load_lock();r=x['platforms']['linux-x64-gnu']['pythonArchive'];r['byteLength']=4;r['sha256']=hashlib.sha256(b'test').hexdigest()
d.load_lock=lambda:x;f.platform.system=lambda:'Linux';f.platform.machine=lambda:'x86_64'
class Slow(io.BytesIO):
 status=200;headers={}
 def geturl(self):return r['url']
 def read(self,n):
  if not getattr(self,'started',False):
   self.started=True;pathlib.Path(STARTED).write_text('inside delayed read');time.sleep(1)
  return super().read(n)
class Opener:
 def open(self,*a,**kw):return Slow(b'test')
f.fetch('linux-x64-gnu',pathlib.Path(DEST),opener=Opener())
pathlib.Path(EFFECT).write_text('completed')
""".replace('MODULE_DIRECTORY',repr(str(Path(fetcher.__file__).parent))).replace('DEST',repr(str(root/'archive'))).replace('EFFECT',repr(str(effect))).replace('STARTED',repr(str(root/'read-started')))
   with self.assertRaisesRegex(ValueError,'deadline'):d.process([sys.executable,'-c',code],root,'slow-read',timeout=.5)
   self.assertTrue((root/'read-started').is_file());time.sleep(1.1);self.assertFalse(effect.exists());self.assertFalse((root/'archive').exists())
 def test_https_redirect_authority_is_closed(self):
  for url in ('http://github.com/file','https://evil.invalid/file','https://user:password@github.com/file','https://github.com:444/file'):
   with self.assertRaises(ValueError):fetcher.checked_url(url)
  self.assertEqual(fetcher.checked_url('https://release-assets.githubusercontent.com/file?signed=fixture'),'https://release-assets.githubusercontent.com/file?signed=fixture')

class RuntimeControls(unittest.TestCase):
 def fixture(self,root):
  build=root/'build';build.mkdir();header=bytearray(20);header[:6]=b'\x7fELF\x02\x01';header[18:20]=(62).to_bytes(2,'little');(build/verifier.NAMES[0]).write_bytes(header)
  (build/verifier.NAMES[2]).write_text('fixture notices')
  receipt={'platform':'linux','architecture':'x86_64','helper':{'sha256':d.sha(build/verifier.NAMES[0]),'byteLength':20},
    'notices':{'sha256':d.sha(build/verifier.NAMES[2])},'nativeDependencies':{'symbolClosureVerified':True},
    'selfTest':{'modelCalls':0},'toolSelfTest':{'modelCalls':0,'networkUsed':False},'linuxLibraries':{'requiredGlibcMaximum':'2.34'}}
  (build/verifier.NAMES[1]).write_text(json.dumps(receipt));return build,receipt
 def exercise(self,kind=None):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();build,receipt=self.fixture(root);out=root/'runtime';calls=[];row=verifier.load_lock()['platforms']['linux-x64-gnu']
   def fake(command,output,label,**kw):
    calls.append((command,label,kw))
    if kind=='mutation' and label=='pull-runtime':(build/verifier.NAMES[2]).write_text('changed')
    if label=='inspect-runtime':return json.dumps({'Id':'sha256:'+row['runtime']['configSha256'],'Os':'linux','Architecture':'arm64' if kind=='arch' else 'amd64','RepoDigests':[row['runtime']['image']]}).encode()
    if label=='base':return (('glibc=2.35' if kind=='floor' else 'glibc=2.34')+'\narchitecture=x86_64\nnoBuildTools=true\n').encode()
    if label=='version':return b'prose-agents-sdk 0.1.0\n'
    if label in ('imports','tools','libraries'):return json.dumps(receipt[{'imports':'selfTest','tools':'toolSelfTest','libraries':'linuxLibraries'}[label]]).encode()
    return b''
   with patch.object(verifier.sys,'platform','linux'),patch.object(verifier.platform,'machine',return_value='x86_64'),patch.object(verifier.shutil,'disk_usage') as disk,patch.object(d,'run',side_effect=fake),patch.object(verifier.inventory,'validate_linux_receipt') as validate:
    disk.return_value.free=8*1024**3
    if kind:
     with self.assertRaises(ValueError):verifier.verify(build,out,'linux-x64-gnu')
     self.assertFalse((out/'runtime-report.json').exists())
    else:
     report=verifier.verify(build,out,'linux-x64-gnu');self.assertFalse(report['cpuFloorQualified']);self.assertFalse(report['publicationAuthorized'])
     self.assertEqual(report['inputs']['payload'][verifier.NAMES[0]],receipt['helper']['sha256'])
     self.assertEqual(report['networkUsageScope'],'runtime-probes-only');self.assertTrue(report['preparationNetworkEnabled'])
    validate.assert_called_once_with(receipt)
   return calls
 def test_exact_native_runtime_orchestration_and_five_second_version(self):
  calls=self.exercise();self.assertEqual([x[1] for x in calls],['pull-runtime','inspect-runtime','base','version','imports','tools','libraries'])
  for command,label,kwargs in calls[2:]:
   self.assertIn('--network=none',command);self.assertEqual(command.count('--mount'),2)
   self.assertIn('--read-only',command);self.assertEqual(kwargs['timeout'],25 if label=='version' else 50)
   source_mount=next(c for c in command if c.startswith('type=bind,') and ',dst=/source,readonly' in c)
   job_mount=next(c for c in command if c.startswith('type=bind,') and c.endswith(',dst=/job'))
   self.assertIn('/runtime/payload,dst=/source,readonly',source_mount)
   self.assertIn('/runtime/job,dst=/job',job_mount)
   self.assertNotIn('/runtime,dst=/job',job_mount)
 def test_wrong_architecture_floor_and_payload_mutation_refused(self):
  for kind in ('arch','floor','mutation'):
   with self.subTest(kind=kind):self.exercise(kind)
 def test_invalid_native_receipt_is_rejected_before_docker_or_output(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();build,receipt=self.fixture(root)
   with patch.object(verifier.sys,'platform','linux'),patch.object(verifier.platform,'machine',return_value='x86_64'),patch.object(verifier.shutil,'disk_usage') as disk,patch.object(d,'run') as run:
    disk.return_value.free=8*1024**3
    with self.assertRaises(ValueError):verifier.verify(build,root/'runtime','linux-x64-gnu')
    run.assert_not_called();self.assertFalse((root/'runtime').exists())
 def test_runtime_not_build_image_and_no_cpu_claim(self):
  self.assertIn('python3',verifier.BASE);self.assertIn('gcc',verifier.BASE)
  for row in verifier.load_lock()['platforms'].values():self.assertIn('almalinuxorg/9-minimal@sha256:',row['runtime']['image'])

if __name__=='__main__':unittest.main()
