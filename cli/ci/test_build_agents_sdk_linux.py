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
       'supplierPath':'/usr/lib64/libgcc_s-actual.so.1','rpmFileDigestVerified':True,
       'rpmPayloadPath':'/lib64/libgcc_s-actual.so.1','rpmFileDigestAlgorithm':8,
       'rpmFileDigestSha256':d.sha(root/'libgcc_s.so.1')}
  package={'name':'libgcc','epoch':'0','version':'8.fixture','release':'test','architecture':'x86_64',
           'sourceRpm':'gcc-fixture.src.rpm','license':'fixture-not-production'}
  return {'schema':'openprose.sdk-native-origin/2','library':lib,'package':package,'licenses':[
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


class ExecutedSupplierControls(unittest.TestCase):
 def setUp(self):
  self.s={'__name__':'supplier_controls'}
  exec(compile(d.SUPPLIER_PY,'supplier.py','exec'),self.s)
  self.canonical='/usr/lib64/libgcc_s-fixture.so.1'
  self.alias='/lib64/libgcc_s-fixture.so.1'
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name)
  self.file=self.root/'usr/lib64/libgcc_s-fixture.so.1'
  self.file.parent.mkdir(parents=True);self.file.write_bytes(b'\x7fELFfixture-bytes')
  (self.root/'lib64').symlink_to(self.root/'usr/lib64',target_is_directory=True)
  self.data=self.file.read_bytes();self.digest=self.s['hashlib'].sha256(self.data).hexdigest()
  self.header='libgcc\t0\t8.fixture\t1\tx86_64\tgcc-fixture.src.rpm\tGPLv3+\t8\n'
  self.row={'path':self.alias,'digest':self.digest,'mode':self.file.stat().st_mode}
 def lstat(self,name):return (self.root/name.lstrip('/')).lstat()
 def select(self,rows=None,info=None):
  return self.s['select_payload'](rows if rows is not None else [self.row],self.canonical,info or self.file.stat(),self.lstat)
 def failure(self,code,fn,*args):
  with self.assertRaisesRegex(self.s['SupplierFailure'],'^'+code+'$'):fn(*args)
 def table(self,row=None):
  row=row or self.row
  return self.header+row['path']+'\t'+row['digest']+'\t'+str(row['mode'])+'\n'
 def test_canonical_and_alias_regular_payloads_accept_on_both_architectures(self):
  for arch in ('x86_64','aarch64'):
   for path in (self.canonical,self.alias):
    with self.subTest(arch=arch,path=path):
     self.header=self.header.replace('x86_64',arch)
     row={**self.row,'path':path}
     package,rows=self.s['parse_table'](self.table(row),arch)
     actual=self.select(rows)
     self.s['verify_owner'](self.header,package,arch)
     self.assertEqual(self.s['verify_digest'](self.data,actual),self.digest)
 def test_duplicate_rows_and_two_aliases_are_ambiguous_even_with_equal_digests(self):
  for rows in ([self.row,self.row],[self.row,{**self.row,'path':self.canonical}]):
   with self.subTest(rows=rows):self.failure('rpm-payload-ambiguity',self.select,rows)
  _,rows=self.s['parse_table'](self.table()+self.table().splitlines()[1]+'\n','x86_64')
  self.assertEqual(len(rows),2)
  self.failure('rpm-payload-ambiguity',self.select,rows)
 def test_same_basename_and_digest_at_distinct_file_reject(self):
  other=self.root/'other';other.write_bytes(self.data)
  self.failure('rpm-payload-ambiguity',self.s['select_payload'],[self.row],self.canonical,self.file.stat(),lambda _:other.stat())
 def test_same_inode_number_on_other_device_reject(self):
  from types import SimpleNamespace
  found=self.file.stat();other=SimpleNamespace(st_dev=found.st_dev+1,st_ino=found.st_ino,st_mode=found.st_mode)
  self.failure('rpm-payload-ambiguity',self.s['select_payload'],[self.row],self.canonical,found,lambda _:other)
 def test_terminal_symlink_and_rpm_nonregular_mode_reject(self):
  link=self.root/'leaf-link';link.symlink_to(self.file)
  self.failure('rpm-payload-nonregular',self.s['select_payload'],[self.row],self.canonical,self.file.stat(),lambda _:link.lstat())
  self.failure('rpm-mode-nonregular',self.select,[{**self.row,'mode':0o120777}])
  self.failure('supplier-canonical-file',self.select,[self.row],link.lstat())
 def test_zero_matching_rows_and_malformed_matching_digest_reject(self):
  self.failure('rpm-payload-ambiguity',self.select,[])
  for digest in ('','a'*63,'A'*64,'g'*64):
   with self.subTest(digest=digest):self.failure('rpm-payload-digest-format',self.select,[{**self.row,'digest':digest}])
 def test_alias_with_actual_byte_mismatch_still_rejects_digest(self):
  row=self.select()
  self.failure('rpm-payload-digest-mismatch',self.s['verify_digest'],self.data+b'changed',row)
  diag=json.loads(self.s['diagnostic']('rpm-payload-digest-mismatch'))
  self.assertEqual(diag['expectedSha256'],self.digest)
  self.assertNotEqual(diag['measuredSha256'],self.digest)
 def test_unexpected_missing_or_noninteger_rpm_algorithm_rejects(self):
  for value in ('(none)','1','2','9','True','8.0',''):
   with self.subTest(value=value):
    bad=self.header.rstrip('\n').rsplit('\t',1)[0]+'\t'+value
    code='rpm-package-ambiguous' if not value else 'rpm-digest-algorithm'
    self.failure(code,self.s['parse_header'],bad,'x86_64')
 def test_wrong_or_multiple_package_records_reject(self):
  self.failure('rpm-package-identity',self.s['parse_header'],self.header.replace('libgcc','other').rstrip(),'x86_64')
  self.failure('rpm-package-identity',self.s['parse_header'],self.header.rstrip(),'aarch64')
  self.failure('rpm-malformed-row',self.s['parse_table'],self.table()+self.header,'x86_64')
 def test_exact_owner_all_package_facts_and_uniqueness_required(self):
  package=self.s['parse_header'](self.header.rstrip(),'x86_64')
  for i,value in enumerate(('other','1','different','2','aarch64','other.src.rpm','other-license')):
   raw=self.header.rstrip().split('\t');raw[i]=value
   code='rpm-package-identity' if i in (0,4) else 'rpm-owner-mismatch'
   with self.subTest(field=i):self.failure(code,self.s['verify_owner'],'\t'.join(raw)+'\n',package,'x86_64')
  for output in ('',self.header+self.header):self.failure('rpm-owner-ambiguity',self.s['verify_owner'],output,package,'x86_64')
 def test_row_and_metadata_limits_are_enforced(self):
  for output,code in ((self.header+('x\t\t1\n'*129),'rpm-row-limit'),
                      (self.header+('x'*1025+'\t\t1\n'),'rpm-malformed-row'),
                      (self.header+'x\ty\t-1\n','rpm-malformed-row'),
                      (self.header+'x\ty\t999999\n','rpm-malformed-row')):
   with self.subTest(code=code):self.failure(code,self.s['parse_table'],output,'x86_64')
 def test_read_limits_nonregular_file_and_stale_identity_reject(self):
  import time
  self.failure('supplier-file-size',self.s['read_regular'],self.file,4,time.monotonic()+1)
  self.failure('supplier-timeout',self.s['read_regular'],self.file,100,time.monotonic()-1)
  link=self.root/'leaf-link';link.symlink_to(self.file)
  self.failure('supplier-nonregular-file',self.s['read_regular'],link,100,time.monotonic()+1)
  old=self.file.stat();self.file.write_bytes(self.data+b'changed')
  self.failure('supplier-identity-change',self.s['check_binding'],self.canonical,self.row,old,self.lstat)
 def test_actual_rpm_process_output_timeout_and_invalid_utf8_are_bounded(self):
  import subprocess,sys,time
  original=subprocess.Popen
  cases=(('print("x"*300000)','rpm-output-limit'),('import time;time.sleep(5)','rpm-timeout'),
         ('import sys;sys.stdout.buffer.write(bytes([255]))','rpm-malformed-output'),
         ('import sys;print("untrusted raw secret",file=sys.stderr);sys.exit(2)','rpm-query-failed'))
  for code,expected in cases:
   with self.subTest(expected=expected):
    def launch(command,**kw):return original([sys.executable,'-c',code],**kw)
    with patch.object(subprocess,'Popen',side_effect=launch):
     self.failure(expected,self.s['rpm'],['unused'],time.monotonic()+.5)
    diagnostic=self.s['diagnostic'](expected)
    self.assertLessEqual(len(diagnostic.encode()),4096);self.assertNotIn('untrusted',diagnostic)
 def test_payload_paths_have_exact_grammar(self):
  for path in ('/lib64//libgcc_s-fixture.so.1','/lib64/./libgcc_s-fixture.so.1',
               '/lib64/../lib64/libgcc_s-fixture.so.1','/tmp/libgcc_s-fixture.so.1',
               '/lib64/other.so.1','/lib64/libgcc_s-fixture.so.1\n'):
   with self.subTest(path=path):self.assertFalse(self.s['payload_path'](path,self.canonical))
  self.assertFalse(self.s['canonical_path']('/usr/lib64/libgcc_s-8~fixture.so.1'))
 def test_diagnostic_is_closed_sanitized_and_bounded(self):
  self.s['DIAG'].update(rawStderr='untrusted-secret',canonicalPath='bad\npath',rowCount=3)
  value=json.loads(self.s['diagnostic']('rpm-query-failed'))
  self.assertNotIn('rawStderr',value);self.assertNotIn('canonicalPath',value)
  self.assertEqual(value['rowCount'],3)
  for key in ('canonicalPath','payloadPath','ownerVersion','ownerRelease','architecture'):
   self.s['DIAG'][key]='x'*1024
  encoded=self.s['diagnostic']('rpm-query-failed')
  self.assertLessEqual(len(encoded.encode()),4096)
  self.assertEqual(json.loads(encoded),{'phase':'supplier','code':'rpm-query-failed'})
 def run_main(self,arch='x86_64',path=None,owner=None,mutation=None,license_count=1):
  import pathlib
  original=type(self.file)
  job=self.root/'job';job.mkdir(exist_ok=True)
  (job/'target.json').write_text(json.dumps({'machine':arch}))
  license_path='/usr/share/licenses/libgcc/COPYING.RUNTIME'
  license_file=self.root/license_path.lstrip('/');license_file.parent.mkdir(parents=True,exist_ok=True)
  license_file.write_bytes(b'GNU GENERAL PUBLIC LICENSE\nGCC RUNTIME LIBRARY EXCEPTION')
  requested=self.root/'usr/lib64/libgcc_s.so.1';requested.symlink_to(self.file.name)
  header=self.header.replace('x86_64',arch)
  row={**self.row,'path':path or self.alias}
  table=header+row['path']+'\t'+row['digest']+'\t'+str(row['mode'])+'\n'
  for i in range(license_count):
   name=license_path if i==0 else license_path+str(i)
   extra=self.root/name.lstrip('/');extra.write_bytes(license_file.read_bytes())
   table+=name+'\t\t'+str(extra.stat().st_mode)+'\n'
  calls=[]
  def query(args,deadline):
   calls.append(args)
   if args[0]=='-qf':
    self.assertEqual(args[-1],row['path'])
    if mutation=='bytes':self.file.write_bytes(self.data+b'changed')
    return owner or header
   return table if self.s['TABLE'] in args[2] else header.replace('8.fixture','changed') if mutation=='package' else header
  def map_path(value):
   p=original(value)
   return self.root/str(p).lstrip('/') if str(p).startswith(('/job','/usr/','/lib64')) else p
  class CanonicalFile:
   def __str__(inner):return self.canonical
   def __fspath__(inner):return str(self.file)
   def lstat(inner):return self.file.lstat()
  resolve=original.resolve
  def resolve_path(value,**kw):return CanonicalFile() if value==requested else resolve(value,**kw)
  with patch.object(original,'resolve',resolve_path),patch.object(pathlib,'Path',side_effect=map_path),patch.dict(self.s,{'rpm':query}):
   self.s['main']()
  return json.loads((job/'supplier/origin.json').read_text()),calls
 def test_actual_main_publishes_alias_and_canonical_exact_origin_on_both_targets(self):
  for arch in ('x86_64','aarch64'):
   for path in (self.alias,self.canonical):
    with self.subTest(arch=arch,path=path):
     # Fresh instance gives each publication a new output directory.
     fresh=ExecutedSupplierControls();fresh.setUp()
     try:
      origin,calls=fresh.run_main(arch,path)
      self.assertEqual(origin['schema'],'openprose.sdk-native-origin/2')
      lib=origin['library'];self.assertEqual(len(lib),8)
      self.assertEqual(lib['rpmPayloadPath'],path);self.assertEqual(lib['rpmFileDigestAlgorithm'],8)
      self.assertEqual(lib['rpmFileDigestSha256'],lib['sha256'])
      copied=fresh.root/'job/supplier/libgcc_s.so.1'
      self.assertEqual(copied.read_bytes(),fresh.data)
      self.assertEqual(len(calls),3)
     finally:fresh.temp.cleanup()
 def test_main_wrong_owner_and_changed_payload_fail_before_publication(self):
  for owner,mutation,expected in ((self.header.replace('8.fixture','other'),None,'rpm-owner-mismatch'),
                                (None,'bytes','supplier-identity-change'),
                                (None,'package','rpm-owner-mismatch')):
   fresh=ExecutedSupplierControls();fresh.setUp()
   try:
    fresh.failure(expected,fresh.run_main,'x86_64',None,owner,mutation)
    self.assertFalse((fresh.root/'job/supplier/origin.json').exists())
   finally:fresh.temp.cleanup()
 def test_main_license_aggregate_and_file_limits_fail_before_publication(self):
  for key in ('MAX_LICENSE_BYTES','MAX_LICENSE_TOTAL'):
   fresh=ExecutedSupplierControls();fresh.setUp()
   try:
    with patch.dict(fresh.s,{key:4}):
     expected='supplier-file-size' if key=='MAX_LICENSE_BYTES' else 'supplier-license-limit'
     fresh.failure(expected,fresh.run_main)
    self.assertFalse((fresh.root/'job/supplier/origin.json').exists())
   finally:fresh.temp.cleanup()
 def test_main_license_count_limit_rejects_without_receipt(self):
  self.failure('supplier-license-limit',self.run_main,'x86_64',None,None,None,33)
  self.assertFalse((self.root/'job/supplier/origin.json').exists())


class NativeOriginV2Controls(unittest.TestCase):
 fixture=NativeOriginControls.fixture
 def test_closed_algorithm_digest_and_path_poison_refused(self):
  row=d.load_lock()['platforms']['linux-x64-gnu']
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();origin=self.fixture(root)
   for key,value in (('rpmFileDigestAlgorithm',True),('rpmFileDigestAlgorithm','8'),('rpmFileDigestAlgorithm',9),
                     ('rpmFileDigestSha256','0'*64),('rpmPayloadPath','/tmp/libgcc_s-actual.so.1'),
                     ('rpmPayloadPath','/lib64//libgcc_s-actual.so.1'),('rpmPayloadPath','/lib64/other.so.1'),
                     ('supplierPath','/usr/lib64/../libgcc_s-actual.so.1'),('rpmFileDigestVerified',False)):
    x=copy.deepcopy(origin);x['library'][key]=value
    with self.subTest(key=key,value=value),self.assertRaises(ValueError):d.validate_origin(x,root,row)
   for key in ('rpmPayloadPath','rpmFileDigestAlgorithm','rpmFileDigestSha256'):
    x=copy.deepcopy(origin);del x['library'][key]
    with self.subTest(missing=key),self.assertRaises(ValueError):d.validate_origin(x,root,row)
   x=copy.deepcopy(origin);x['schema']='openprose.sdk-native-origin/1'
   with self.assertRaises(ValueError):d.validate_origin(x,root,row)
   for parent in ('/lib64','/usr/lib64'):
    x=copy.deepcopy(origin);x['library']['rpmPayloadPath']=parent+'/libgcc_s-actual.so.1'
    d.validate_origin(x,root,row)


if __name__ == '__main__':
 unittest.main()
