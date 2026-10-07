import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import build_agents_sdk_linux as d

class DriverControls(unittest.TestCase):
 def test_real_closed_pins(self):
  x=d.load_lock();self.assertEqual(len(x['platforms']),2)
 def test_floating_or_wrong_arch_image_rejected(self):
  for key in ('freezer','supplier'):
   for value in ('quay.io/pypa/manylinux_2_34_x86_64:latest','quay.io/pypa/manylinux_2_34_aarch64@sha256:'+'a'*64):
    x=copy.deepcopy(d.load_lock());x['platforms']['linux-x64-gnu'][key]['image']=value
    with tempfile.TemporaryDirectory() as t:
     p=Path(t).resolve()/'lock';p.write_text(json.dumps(x))
     with self.assertRaises(ValueError):d.load_lock(p)
 def test_variant_or_install_only_python_rejected(self):
  for field,value in [('targetTriple','x86_64_v2-unknown-linux-gnu'),('url','https://example.com/untrusted.tar.zst'),('byteLength',True)]:
   x=copy.deepcopy(d.load_lock());x['platforms']['linux-x64-gnu']['pythonArchive'][field]=value
   with tempfile.TemporaryDirectory() as t:
    p=Path(t).resolve()/'lock';p.write_text(json.dumps(x))
    with self.assertRaises(ValueError):d.load_lock(p)
 def test_mounts_are_closed_and_offline_freeze(self):
  r=d.load_lock()['platforms']['linux-x64-gnu'];c=d.container_command(r,'freezer',Path('/source-owned'),Path('/job-owned'),'freeze.sh')
  self.assertIn('--network=none',c);self.assertIn('--pull=never',c);self.assertIn('--read-only',c)
  self.assertIn('type=bind,src=/source-owned,dst=/source,readonly',c)
  self.assertNotIn('--privileged',c);self.assertNotIn('--env-file',c)
  self.assertEqual(c.count('--mount'),2);self.assertIn('--cap-drop=ALL',c)
 def test_no_ambient_credentials_passed(self):
  import sys
  with tempfile.TemporaryDirectory() as t, patch.dict(d.os.environ,{'OPENAI_API_KEY':'sentinel','GH_TOKEN':'sentinel'}):
   out=Path(t).resolve();(out/'home').mkdir()
   result=d.process([sys.executable,'-c','import os,json;print(json.dumps(dict(os.environ)))'],out,'env')
   env=json.loads(result);self.assertNotIn('OPENAI_API_KEY',env);self.assertNotIn('GH_TOKEN',env)
   self.assertEqual(env['HOME'],str(out/'home'))
 def test_host_or_emulated_target_cannot_be_native(self):
  with tempfile.TemporaryDirectory() as t:
   source=Path(t).resolve()/'source';source.mkdir()
   with patch.object(d.sys,'platform','darwin'),self.assertRaisesRegex(ValueError,'Native Linux'):
    d.plan(source,Path(t).resolve()/'out','linux-x64-gnu',Path(t).resolve()/'absent')
 def test_unknown_target_and_unsafe_mount_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   source=Path(t).resolve()/'source';source.mkdir()
   with self.assertRaises(ValueError):d.plan(source,Path(t).resolve()/'out','windows-x64',Path(t).resolve()/'absent',native=False)
   with self.assertRaises(ValueError):d.checked_path(Path(t).resolve()/'bad,mount')
   (Path(t).resolve()/'link').symlink_to(source)
   with self.assertRaises(ValueError):d.checked_path(Path(t).resolve()/'link')
 def test_wrong_python_bytes_rejected_before_any_command(self):
  with tempfile.TemporaryDirectory() as t,patch.object(d.subprocess,'run') as run:
   source=Path(t).resolve()/'source';source.mkdir();archive=Path(t).resolve()/'python';archive.write_bytes(b'wrong')
   with self.assertRaisesRegex(ValueError,'archive differs'):d.plan(source,Path(t).resolve()/'out','linux-x64-gnu',archive,native=False)
   run.assert_not_called();self.assertFalse((Path(t).resolve()/'out').exists())
 def test_required_selection_and_custody_hooks(self):
  self.assertIn('--linux-libgcc /job/supplier/libgcc_s.so.1',d.FREEZE)
  self.assertIn('--linux-native-origin /job/native-input.json',d.FREEZE)
  self.assertNotIn('exclude',d.FREEZE);self.assertNotIn('LD_LIBRARY_PATH',d.FREEZE)
  self.assertIn("digests.get(str(p))==digest",d.SUPPLIER_PY)
  self.assertIn('GCC RUNTIME LIBRARY EXCEPTION',d.SUPPLIER_PY)
  self.assertIn("x['libpython_link_mode']=='shared'",d.PREPARE)
 def test_preparation_network_is_explicit_and_freeze_is_not(self):
  r=d.load_lock()['platforms']['linux-arm64-gnu']
  self.assertIn('--network=bridge',d.container_command(r,'freezer',Path('/source'),Path('/job'),'prepare.sh',network='bridge'))
  with self.assertRaises(ValueError):d.container_command(r,'freezer',Path('/source'),Path('/job'),'freeze.sh',network='host')



class RealOrchestratorControls(unittest.TestCase):
 def setup_inputs(self,root):
  source=root/'source';source.mkdir()
  for name in d.SOURCES:
   p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture source')
  driver=root/'driver.py';driver.write_text('fixture driver')
  archive=root/'python';archive.write_bytes(b'not an executed archive')
  lock=d.load_lock();r=lock['platforms']['linux-x64-gnu'];r['pythonArchive']['byteLength']=archive.stat().st_size;r['pythonArchive']['sha256']=d.sha(archive)
  path=root/'lock.json';path.write_text(json.dumps(lock))
  return source,driver,archive,path,lock
 def exercise(self,mutation):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();source,driver,archive,lockpath,lock=self.setup_inputs(root);output=root/'out';calls=[]
   original=d.shutil.copyfile
   def copy(src,dest):
    original(src,dest)
    if mutation=='copy':Path(dest).write_bytes(b'mutated after plan')
   def fake(command,out,label,**kw):
    calls.append(label)
    if label=='pull-supplier':
     if mutation=='source':(source/d.SOURCES[0]).write_text('changed source')
     if mutation=='tests':(source/'harnesses/agents-sdk/test_run.py').write_text('changed tests')
     if mutation=='native-module':(source/'cli/ci/sdk_native_inventory.py').write_text('changed native inventory module')
     if mutation=='driver':driver.write_text('changed driver')
     if mutation=='lock':lockpath.write_text(lockpath.read_text()+' ')
     if mutation=='wrong-image':return b''
    if label.startswith('inspect-'):
     key=label.removeprefix('inspect-');r=lock['platforms']['linux-x64-gnu']
     return json.dumps({'Id':'sha256:'+r[key]['configSha256'],'Os':'linux','Architecture':'arm64',
                        'RepoDigests':[r[key]['image']]}).encode()
    return b''
   with patch.object(d,'__file__',str(driver)),patch.object(d,'LOCK',lockpath),patch.object(d.sys,'platform','linux'),patch.object(d.platform,'machine',return_value='x86_64'),patch.object(d.shutil,'disk_usage') as disk,patch.object(d.shutil,'copyfile',side_effect=copy),patch.object(d,'run',side_effect=fake):
    disk.return_value.free=16*1024**3
    with self.assertRaises(ValueError):d.build(source,output,'linux-x64-gnu',archive)
   return calls
 def test_copied_archive_mutation_refused_before_container(self):self.assertEqual(self.exercise('copy'),[])
 def test_sources_tests_driver_and_lock_are_rechecked(self):
  for kind in ('source','tests','native-module','driver','lock'):
   with self.subTest(kind=kind):self.assertEqual(self.exercise(kind),['pull-supplier'])
 def test_native_inventory_module_mutation_refused_before_inspect(self):self.assertEqual(self.exercise('native-module'),['pull-supplier'])
 def test_actual_inspect_architecture_is_checked(self):self.assertEqual(self.exercise('wrong-image'),['pull-supplier','inspect-supplier'])
 def test_actual_process_timeout_kills_delayed_descendant_effect(self):
  import sys,time
  with tempfile.TemporaryDirectory() as t:
   out=Path(t).resolve();(out/'home').mkdir();effect=out/'late-effect'
   child='import time,pathlib;time.sleep(.6);pathlib.Path('+repr(str(effect))+').write_text("escaped")'
   code='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);time.sleep(20)'
   with self.assertRaisesRegex(ValueError,'deadline'):d.process([sys.executable,'-c',code],out,'timeout',timeout=.1)
   time.sleep(.7);self.assertFalse(effect.exists())
 def test_timeout_only_removes_label_verified_owned_container(self):
  with tempfile.TemporaryDirectory() as t:
   out=Path(t).resolve();row=d.load_lock()['platforms']['linux-x64-gnu'];command=d.container_command(row,'freezer',Path('/source'),out,'freeze.sh',owner='a'*32);calls=[]
   def fake(c,o,label,**kw):
    calls.append((c,label,kw))
    if label=='freeze':raise subprocess.TimeoutExpired(c,.1)
    if label.endswith('inspect'):return ('a'*32+'\n').encode()
    return b''
   import subprocess
   with patch.object(d,'process',side_effect=fake),self.assertRaises(subprocess.TimeoutExpired):d.run(command,out,'freeze',timeout=.1)
   self.assertEqual([x[1] for x in calls],['freeze','freeze-cleanup-inspect','freeze-cleanup-remove'])
   self.assertEqual(calls[-1][0][-1],'prose-sdk-'+('a'*32)+'-freeze')
   self.assertEqual(calls[-1][2]['timeout'],10)
 def test_wrong_ownership_label_never_removes_container(self):
  with tempfile.TemporaryDirectory() as t:
   out=Path(t).resolve();row=d.load_lock()['platforms']['linux-x64-gnu'];command=d.container_command(row,'freezer',Path('/source'),out,'freeze.sh',owner='a'*32);calls=[]
   def fake(c,o,label,**kw):
    calls.append(label)
    if label=='freeze':raise ValueError('original')
    return b'other-owner\n'
   with patch.object(d,'process',side_effect=fake),self.assertRaisesRegex(ValueError,'original'):d.run(command,out,'freeze')
   self.assertEqual(calls,['freeze','freeze-cleanup-inspect']);self.assertTrue((out/'freeze-cleanup-error.txt').is_file())
 def test_actual_process_output_is_bounded(self):
  import sys
  with tempfile.TemporaryDirectory() as t:
   out=Path(t).resolve();(out/'home').mkdir()
   with self.assertRaisesRegex(ValueError,'output limit'):d.process([sys.executable,'-c','print("x"*10000)'],out,'large',max_bytes=64)
   self.assertLessEqual((out/'large.log').stat().st_size,64)
 def test_low_space_refuses_before_any_command(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();source,driver,archive,lockpath,lock=self.setup_inputs(root)
   with patch.object(d,'__file__',str(driver)),patch.object(d,'LOCK',lockpath),patch.object(d.sys,'platform','linux'),patch.object(d.platform,'machine',return_value='x86_64'),patch.object(d.shutil,'disk_usage') as disk,patch.object(d,'run') as run:
    disk.return_value.free=1024
    with self.assertRaisesRegex(ValueError,'8GiB'):d.build(source,root/'out','linux-x64-gnu',archive)
    run.assert_not_called();self.assertFalse((root/'out').exists())


class NativeOriginControls(unittest.TestCase):
 def fixture(self,root):
  (root/'libgcc_s.so.1').write_bytes(b'fixture native bytes')
  text='GNU GENERAL PUBLIC LICENSE\nGCC RUNTIME LIBRARY EXCEPTION'
  (root/'license-0.txt').write_text(text)
  lib={'path':'libgcc_s.so.1','sha256':d.sha(root/'libgcc_s.so.1'),'byteLength':len(b'fixture native bytes'),
       'supplierPath':'/usr/lib64/libgcc_s-actual.so.1','rpmFileDigestVerified':True}
  package={'name':'libgcc','epoch':'0','version':'8.fixture','release':'test','architecture':'x86_64',
           'sourceRpm':'gcc-fixture.src.rpm','license':'fixture-not-production'}
  return {'schema':'openprose.sdk-native-origin/1','library':lib,'package':package,'licenses':[
          {'packagePath':'/usr/share/licenses/libgcc/COPYING.RUNTIME','path':'license-0.txt',
           'sha256':d.sha(root/'license-0.txt'),'byteLength':(root/'license-0.txt').stat().st_size}]}
 def test_missing_source_rpm_and_actual_license_fact_refused(self):
  row=d.load_lock()['platforms']['linux-x64-gnu']
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();origin=self.fixture(root);d.validate_origin(origin,root,row)
   for mutation in ('sourceRpm','license','architecture'):
    x=copy.deepcopy(origin);x['package'][mutation]=''
    with self.assertRaises(ValueError):d.validate_origin(x,root,row)
   x=copy.deepcopy(origin);x['licenses']=[]
   with self.assertRaises(ValueError):d.validate_origin(x,root,row)
 def test_mutated_library_or_license_refused(self):
  row=d.load_lock()['platforms']['linux-x64-gnu']
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();origin=self.fixture(root)
   (root/'libgcc_s.so.1').write_bytes(b'changed')
   with self.assertRaises(ValueError):d.validate_origin(origin,root,row)
   origin=self.fixture(root);(root/'license-0.txt').write_text('changed')
   with self.assertRaises(ValueError):d.validate_origin(origin,root,row)
 def test_output_byte_and_file_caps_enforced(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();(root/'big').write_bytes(b'12345')
   with patch.object(d,'MAX_RETAINED_BYTES',4),self.assertRaises(ValueError):d.check_output(root)
   with patch.object(d,'MAX_FILES',0),self.assertRaises(ValueError):d.check_output(root)


if __name__ == '__main__':
 unittest.main()
