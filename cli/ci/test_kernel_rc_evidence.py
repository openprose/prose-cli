"""Mutation tests for source/check/package custody, using generated npm bytes."""
import hashlib
import json
import unittest
import kernel_rc_evidence as custody
import publication as pub
import test_assemble_kernel_rc as assembly_tests


SDK_SYNTHETIC_COLLECT=b'Synthetic COLLECT declaration for nonexecuted consumer fixtures; no PyInstaller invocation.'

def sdk_fixture(platform):
    """Complete nonexecuted SDK table for consumer tests; never native release proof."""
    sdk, rows = assembly_tests.sdk_fixture(platform)
    table={'files':{name:(data,mode) for name,data,mode in rows},'directories':{},'symlinks':{}}
    if platform.startswith('darwin'):
        import copy
        from test_sdk_native_inventory import onedir_fixture
        view=onedir_fixture('arm64' if platform=='darwin-arm64' else 'x86_64')
        receipt=json.loads(table['files']['agents-sdk-build.json'][0])
        receipt['payload']=copy.deepcopy(view['payload'])
        receipt['payload']['collectTocSha256']=hashlib.sha256(SDK_SYNTHETIC_COLLECT).hexdigest()
        helper=view['files']['prose-agents-sdk'][0]
        receipt['helper']={'path':'prose-agents-sdk','byteLength':len(helper),'sha256':hashlib.sha256(helper).hexdigest()}
        encoded=json.dumps(receipt,sort_keys=True).encode()
        sdk.update(receipt['helper']);sdk['receiptSha256']=hashlib.sha256(encoded).hexdigest()
        table['files'].update(view['files']);table['files']['agents-sdk-build.json']=(encoded,0o644)
        table['directories']=view['directories'];table['symlinks']=view['symlinks']
    return sdk,table


def sdk_producer_fixture(table,include_raw=False):
    """Synthetic retained-record fixture ONLY; never signature/execution authority."""
    import base64
    prefix='' if 'prose-agents-sdk' in table['files'] else custody.sdk_archive_prefix(table)
    receipt=json.loads(table['files'][prefix+'agents-sdk-build.json'][0]);payload=receipt['payload']
    assert payload['collectTocSha256']==hashlib.sha256(SDK_SYNTHETIC_COLLECT).hexdigest()
    checks=[];outputs={}
    expected=[('codesign-'+str(i).zfill(4)+'.log',path,'strict') for i,path in enumerate(payload['codeSignaturePaths'])]
    expected.append(('codesign.log','prose-agents-sdk','strict-deep'))
    for log,path,verification in expected:
        raw={};count=0
        if include_raw:
            data=b'Explicit synthetic signature stderr, no native execution.';name=log+'.stderr';count=len(data);digest=hashlib.sha256(data).hexdigest()
            raw['stderr']={'path':name,'sha256':digest,'byteLength':count}
            outputs[name]={'sha256':digest,'byteLength':count,'base64':base64.b64encode(data).decode()}
        proof={'schema':'openprose.sdk-code-signature-check/1','phase':'code-signature','path':path,'verification':verification,
               'exitCode':0,'stdoutBytes':0,'stderrBytes':count,'timedOut':False,'outputLimitExceeded':False,
               'timeoutSeconds':30,'success':True,'outputComplete':True,'rawOutput':raw}
        checks.append({'log':log,'proof':proof})
    bundle={'schema':'openprose.sdk-code-signatures/1','architecture':receipt['architecture'],
            'payloadSha256':hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'checks':checks,'rawOutputs':outputs}
    return {custody.SDK_COLLECT_EVIDENCE:SDK_SYNTHETIC_COLLECT,custody.SDK_SIGNATURE_EVIDENCE:json.dumps(bundle,sort_keys=True).encode()}


def sdk_archive_fixture(table,prefix='root/',cli=b'fixture-cli',extra_files=None):
    """Pure typed tar fixture encoder; not a release builder or executable proof."""
    import io,tarfile
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        files={prefix+'prose':(cli,0o755),**{prefix+n:v for n,v in table['files'].items()},**(extra_files or {})}
        for name,(data,mode) in files.items():
            info=tarfile.TarInfo(name);info.mode=mode;info.size=len(data)
            archive.addfile(info,io.BytesIO(data))
        for name,mode in table['directories'].items():
            info=tarfile.TarInfo(prefix+name);info.type=tarfile.DIRTYPE;info.mode=mode;archive.addfile(info)
        for name,target in table['symlinks'].items():
            info=tarfile.TarInfo(prefix+name);info.type=tarfile.SYMTYPE;info.mode=0o777;info.linkname=target;archive.addfile(info)
    return stream.getvalue()



class OnedirCustodyTests(unittest.TestCase):
    def manifest(self,platform='darwin-x64'):
        sdk,table=sdk_fixture(platform)
        return {'platform':platform,'agentsSdk':sdk,'source':{'revision':'a'*40}},table

    def test_complete_tree_roundtrips_with_real_directory_and_file_aliases(self):
        import tempfile
        from pathlib import Path
        for platform in ('darwin-arm64','darwin-x64','linux-x64-gnu'):
            with self.subTest(platform=platform):
                manifest,table=self.manifest(platform)
                encoded=sdk_archive_fixture(table)
                decoded=pub.decode_sdk_archive(encoded,manifest)
                with tempfile.TemporaryDirectory() as temporary:
                    destination=Path(temporary).resolve()
                    observed=custody.materialize_sdk_members(manifest,decoded,destination)
                    self.assertEqual(observed,custody.sdk_scoped_table(decoded,'root/'))
                    if platform.startswith('darwin'):
                        self.assertTrue((destination/'prose-agents-sdk-runtime/Python.framework/Python').is_symlink())
                        self.assertEqual((destination/'prose-agents-sdk-runtime/Python.framework/Python').read_bytes(),table['files']['prose-agents-sdk-runtime/Python.framework/Versions/3.10/Python'][0])

    def test_rehashed_support_alias_mode_omission_and_extra_rows_are_rejected(self):
        import copy
        for poison in ('bytes','mode','omission','alias','extra-directory','extra-file','foreign-alias'):
            manifest,table=self.manifest();bad=copy.deepcopy(table)
            name='prose-agents-sdk-runtime/base_library.zip'
            if poison=='bytes':bad['files'][name]=(b'poison',0o644)
            elif poison=='mode':bad['files'][name]=(bad['files'][name][0],0o755)
            elif poison=='omission':del bad['files'][name]
            elif poison=='alias':bad['symlinks']['prose-agents-sdk-runtime/Python.framework/Python']='../../../outside'
            elif poison=='extra-directory':bad['directories']['prose-agents-sdk-runtime/unlisted']=0o755
            elif poison=='extra-file':bad['files']['prose-agents-sdk-runtime/unlisted']=(b'x',0o644)
            else:bad['symlinks']['unexpected']='prose-agents-sdk'
            with self.subTest(poison=poison),self.assertRaises(ValueError):
                pub.decode_sdk_archive(sdk_archive_fixture(bad),manifest)

    def test_foreign_macho_and_missing_signature_path_rejected(self):
        from test_sdk_native_inventory import macho_fixture
        for poison in ('cpu','signature'):
            manifest,table=self.manifest();receipt=json.loads(table['files']['agents-sdk-build.json'][0])
            name='prose-agents-sdk-runtime/Python.framework/Versions/3.10/Python'
            if poison=='cpu':
                data=macho_fixture('arm64');table['files'][name]=(data,0o755)
                row=next(r for r in receipt['payload']['entries'] if r['path']==name)
                row.update(sha256=hashlib.sha256(data).hexdigest(),byteLength=len(data))
            else:receipt['payload']['codeSignaturePaths'].remove(name)
            encoded=json.dumps(receipt).encode();table['files']['agents-sdk-build.json']=(encoded,0o644)
            manifest['agentsSdk']['receiptSha256']=hashlib.sha256(encoded).hexdigest()
            with self.subTest(poison=poison),self.assertRaises(ValueError):pub.decode_sdk_archive(sdk_archive_fixture(table),manifest)

    def test_missing_payload_fails_and_exact_historical_context_is_required(self):
        manifest,table=self.manifest();old,rows=assembly_tests.sdk_fixture('darwin-x64')
        # The root assembly fixture may now be current; remove the additive tree
        # from this explicitly synthetic old source scope only.
        if isinstance(rows,dict):rows=[(n,d,m) for n,(d,m) in rows['files'].items() if n in custody.SDK_NAMES]
        oldtable={'files':{n:(d,m) for n,d,m in rows},'directories':{},'symlinks':{}}
        receipt=json.loads(oldtable['files']['agents-sdk-build.json'][0]);receipt.pop('payload',None)
        data=json.dumps(receipt).encode();oldtable['files']['agents-sdk-build.json']=(data,0o644);old['receiptSha256']=hashlib.sha256(data).hexdigest();manifest['agentsSdk']=old
        encoded=sdk_archive_fixture(oldtable)
        with self.assertRaisesRegex(ValueError,'complete onedir'):pub.decode_sdk_archive(encoded,manifest)
        history={'sourceRevision':'a'*40,'archiveSha256':hashlib.sha256(encoded).hexdigest(),'receiptSha256':old['receiptSha256']}
        pub.decode_sdk_archive(encoded,manifest,historical=history)
        history['archiveSha256']='0'*64
        with self.assertRaisesRegex(ValueError,'Historical archive'):pub.decode_sdk_archive(encoded,manifest,historical=history)

    def test_existing_destination_refuses_before_any_sdk_tree_write(self):
        import tempfile
        from pathlib import Path
        manifest,table=self.manifest();decoded=pub.decode_sdk_archive(sdk_archive_fixture(table),manifest)
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary).resolve();(destination/'AGENTS-SDK-NOTICES.txt').write_bytes(b'old')
            with self.assertRaisesRegex(ValueError,'fresh'):custody.materialize_sdk_members(manifest,decoded,destination)
            self.assertEqual(sorted(p.name for p in destination.iterdir()),['AGENTS-SDK-NOTICES.txt'])

    def test_sdk_entry_allowance_never_enlarges_generic_reader(self):
        import tempfile
        from pathlib import Path
        manifest,table=self.manifest()
        encoded=sdk_archive_fixture(table)
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'sdk.tgz';path.write_bytes(encoded)
            with self.assertRaisesRegex(ValueError,'Links'):pub.archive_members(path)
        with self.assertRaisesRegex(ValueError,'identity'):pub.decode_sdk_archive(encoded,{'platform':'darwin-x64'})
        extra={f'root/ordinary-{i}':(b'',0o644) for i in range(129)}
        with self.assertRaisesRegex(ValueError,'generic member budget'):pub.decode_sdk_archive(sdk_archive_fixture(table,extra_files=extra),manifest)


class SdkProducerEvidenceTests(unittest.TestCase):
    def fixture(self,raw=False):
        sdk,table=sdk_fixture('darwin-x64');manifest={'platform':'darwin-x64','agentsSdk':sdk}
        complete=pub.decode_sdk_archive(sdk_archive_fixture(table),manifest)
        files=sdk_producer_fixture(table,raw);return manifest,complete,files
    def verify(self,manifest,table,files):
        evidence={name:{'sha256':hashlib.sha256(data).hexdigest(),'byteLength':len(data)} for name,data in files.items()}
        return custody.validate_sdk_producer_evidence(manifest,table,evidence,files.__getitem__)
    def test_silent_and_retained_raw_records_have_complete_strict_target_closure(self):
        for raw in (False,True):
            with self.subTest(raw=raw):
                manifest,table,files=self.fixture(raw);bundle=self.verify(manifest,table,files)
                self.assertEqual(len(bundle['checks']),3);self.assertEqual(bundle['checks'][-1]['proof']['verification'],'strict-deep')
    def test_rehashed_signature_bundle_target_status_raw_and_digest_poison_rejected(self):
        for poison in ('omit','duplicate','order','target','deep','timeout','success','complete','bool-exit','bool-count','payload','raw-bytes','raw-omit','raw-extra','raw-path','raw-size'):
            manifest,table,files=self.fixture(True);bundle=json.loads(files[custody.SDK_SIGNATURE_EVIDENCE]);proof=bundle['checks'][0]['proof']
            if poison=='omit':bundle['checks'].pop()
            elif poison=='duplicate':bundle['checks'][1]=bundle['checks'][0]
            elif poison=='order':bundle['checks'].reverse()
            elif poison=='target':proof['path']='unexpected'
            elif poison=='deep':bundle['checks'][-1]['proof']['verification']='strict'
            elif poison=='timeout':proof['timedOut']=True
            elif poison=='success':proof['success']=False
            elif poison=='complete':proof['outputComplete']=False
            elif poison=='bool-exit':proof['exitCode']=False
            elif poison=='bool-count':proof['stdoutBytes']=False
            elif poison=='payload':bundle['payloadSha256']='0'*64
            elif poison=='raw-omit':bundle['rawOutputs'].clear()
            elif poison=='raw-extra':bundle['rawOutputs']['undeclared']={}
            elif poison=='raw-path':proof['rawOutput']['stderr']['path']='../unexpected'
            elif poison=='raw-size':proof['stderrBytes']=65537
            else:next(iter(bundle['rawOutputs'].values()))['base64']='cG9pc29u'
            files[custody.SDK_SIGNATURE_EVIDENCE]=json.dumps(bundle).encode()
            with self.subTest(poison=poison),self.assertRaisesRegex(ValueError,'SDK producer'):self.verify(manifest,table,files)
    def test_rehashed_collect_or_missing_bundle_rejected(self):
        for poison in ('collect','missing'):
            manifest,table,files=self.fixture()
            if poison=='collect':files[custody.SDK_COLLECT_EVIDENCE]=b'poison'
            else:del files[custody.SDK_SIGNATURE_EVIDENCE]
            with self.subTest(poison=poison),self.assertRaisesRegex(ValueError,'SDK producer'):self.verify(manifest,table,files)
    def test_linux_forbids_mac_producer_claims(self):
        sdk,table=sdk_fixture('linux-x64-gnu');manifest={'platform':'linux-x64-gnu','agentsSdk':sdk}
        complete=pub.decode_sdk_archive(sdk_archive_fixture(table),manifest)
        self.assertIsNone(self.verify(manifest,complete,{}))
        with self.assertRaisesRegex(ValueError,'Linux must not claim'):self.verify(manifest,complete,{custody.SDK_COLLECT_EVIDENCE:b'poison'})
        receipt=json.loads(table['files']['agents-sdk-build.json'][0]);receipt['payload']={'layout':'pyinstaller-onedir/1'}
        encoded=json.dumps(receipt).encode();table['files']['agents-sdk-build.json']=(encoded,0o644);manifest['agentsSdk']['receiptSha256']=hashlib.sha256(encoded).hexdigest()
        complete=pub.decode_sdk_archive(sdk_archive_fixture(table),manifest)
        with self.assertRaisesRegex(ValueError,'Linux must not claim'):self.verify(manifest,complete,{})


class EvidenceTests(unittest.TestCase):
    setUp = assembly_tests.AssemblyTests.setUp
    record = assembly_tests.AssemblyTests.record

    def assembled(self):
        import assemble_kernel_rc
        output = self.root / 'assembly'
        plan = assemble_kernel_rc.assemble(self.roots, output, self.evidence)
        hashes = {(runner, platform): hashlib.sha256((runner + platform).encode()).hexdigest()
                  for runner in ('bun', 'rust') for platform in pub.PLATFORMS}
        return plan, output, hashes

    def verify(self, plan, output, hashes):
        return custody.verify_platform_evidence(plan, output, 'darwin-arm64', 'darwin-arm64-build-report.json', hashes)

    def update_bound(self, plan, output, relative, value):
        report_path = output / 'darwin-arm64-build-report.json'
        report = pub.read_json(report_path)
        manifest = pub.read_json(output / 'darwin-arm64-release-manifest.json')
        name = custody.asset_name('darwin-arm64', relative, {a['path'] for a in manifest['artifacts']})
        path = output / name
        path.write_text(json.dumps(value))
        report['evidence'][relative] = {'sha256': pub.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        for name, changed in ((name, path), (report_path.name, report_path)):
            record = next(item for item in plan['artifacts'] if item['name'] == name)
            record.update(sha256=pub.digest(changed), size=changed.stat().st_size)

    def test_all_structured_logs_retained_and_verified(self):
        plan, output, hashes = self.assembled()
        self.verify(plan, output, hashes)
        names = {a['name'] for a in plan['artifacts']}
        for platform in pub.PLATFORMS:
            for check in custody.CHECKS:
                self.assertIn(platform + '-logs-' + check + '.json', names)

    def test_production_manifest_cannot_omit_or_rehash_sdk_identity(self):
        for mutation in ('omit', 'digest'):
            with self.subTest(mutation=mutation):
                plan, output, hashes = self.assembled()
                manifest = pub.read_json(output / 'darwin-arm64-release-manifest.json')
                if mutation == 'omit':
                    manifest.pop('agentsSdk')
                else:
                    manifest['agentsSdk']['sha256'] = '0'*64
                self.update_bound(plan, output, 'package/release-manifest.json', manifest)
                with self.assertRaisesRegex(ValueError, 'SDK'):
                    self.verify(plan, output, hashes)
                import shutil
                shutil.rmtree(output)

    def test_rehashed_installed_sdk_probe_cannot_claim_skipped_tools(self):
        plan, output, hashes = self.assembled()
        self.update_bound(plan, output, 'logs/installed-sdk-tools-bun.json', {})
        with self.assertRaisesRegex(ValueError, 'SDK probe'):
            self.verify(plan, output, hashes)

    def test_rehashed_passing_log_cannot_describe_other_binary(self):
        plan, output, hashes = self.assembled()
        check = pub.read_json(output / 'darwin-arm64-logs-installed-bun.json')
        check['binarySha256'] = '0'*64
        self.update_bound(plan, output, 'logs/installed-bun.json', check)
        with self.assertRaisesRegex(ValueError, 'exact release bytes'):
            self.verify(plan, output, hashes)

    def test_rehashed_checks_reject_fixed_startup_wrong_source_and_test_seams(self):
        plan, output, hashes = self.assembled()
        original = pub.read_json(output / 'darwin-arm64-logs-built-rust.json')
        for mutation in ({'imageSource': 'embedded'}, {'commit': 'f'*40}, {'testSeamsEnabled': True}):
            with self.subTest(mutation=mutation):
                self.update_bound(plan, output, 'logs/built-rust.json', dict(original, **mutation))
                with self.assertRaisesRegex(ValueError, 'exact release bytes'):
                    self.verify(plan, output, hashes)

    def test_rehashed_installed_payload_capture_cannot_omit_or_mutate_support(self):
        import copy,shutil
        for poison in ('omit','alias','order','after','count'):
            with self.subTest(poison=poison):
                plan,output,hashes=self.assembled()
                rows=copy.deepcopy(pub.read_json(output/'darwin-arm64-logs-installed-sdk-payloads.json'))
                if poison=='omit':rows[0]['before'].pop('supportTree')
                elif poison=='alias':rows[0]['before']['supportTree']['payload']['entries'][-1]['path']='poison'
                elif poison=='order':rows.reverse()
                elif poison=='after':rows[0]['after']['prose-agents-sdk']['sha256']='0'*64
                else:rows[0]['before']['supportTree']['entryCount']=False
                self.update_bound(plan,output,custody.SDK_PAYLOAD_EVIDENCE,rows)
                with self.assertRaisesRegex(ValueError,'Installed SDK'):self.verify(plan,output,hashes)
                shutil.rmtree(output)

    def test_missing_retained_evidence_cannot_pass(self):
        plan, output, hashes = self.assembled()
        plan['artifacts'] = [a for a in plan['artifacts'] if a['name'] != 'darwin-arm64-logs-installed-npm.json']
        with self.assertRaisesRegex(ValueError, 'Missing or colliding'):
            self.verify(plan, output, hashes)

    def test_rehashed_manifest_cannot_change_native_platform(self):
        plan, output, hashes = self.assembled()
        manifest = pub.read_json(output / 'darwin-arm64-release-manifest.json')
        manifest['platform'] = 'linux-x64-gnu'
        self.update_bound(plan, output, 'package/release-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'manifest identity'):
            self.verify(plan, output, hashes)

    def test_rehashed_manifest_cannot_authorize_or_enable_test_seams(self):
        plan, output, hashes = self.assembled()
        original = pub.read_json(output / 'darwin-arm64-release-manifest.json')
        for mutation in ({'publicationAuthorized': True}, {'releaseEligible': True},
                         {'buildProfiles': {'bun': {'profile': 'release', 'testSeamsEnabled': True}, 'rust': {'profile': 'release', 'testSeamsEnabled': False}}}):
            with self.subTest(mutation=mutation):
                self.update_bound(plan, output, 'package/release-manifest.json', dict(original, **mutation))
                with self.assertRaisesRegex(ValueError, 'manifest identity'):
                    self.verify(plan, output, hashes)

    def test_evidence_paths_are_closed(self):
        for name in ('../key', '/tmp/key', 'logs/../key', 'logs/nested/key', 'package/./file', 'logs/a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                custody.asset_name('darwin-arm64', name, set())


class LiveEvidenceTests(unittest.TestCase):
    setUp = assembly_tests.AssemblyTests.setUp
    record = assembly_tests.AssemblyTests.record
    live_report = assembly_tests.AssemblyTests.live_report

    def live(self):
        live = self.live_report()
        paths = {(runner, role): self.root/record['path'] for runner, attempt in live['runners'].items() for role, record in attempt['evidence'].items()}
        hashes = {(runner, 'darwin-arm64'): hashlib.sha256((runner+'darwin-arm64').encode()).hexdigest() for runner in ('bun', 'rust')}
        return live, paths, hashes

    def verify(self, live, paths, hashes):
        custody.validate_live_smoke(live, self.source, self.version, hashes, paths)

    def rewrite(self, live, paths, role, value):
        path = paths[('bun', role)]
        path.write_text(json.dumps(value))
        live['runners']['bun']['evidence'][role].update(sha256=pub.digest(path), byteLength=path.stat().st_size)

    def test_retained_kernel_terminal_and_observation_agree(self):
        live, paths, hashes = self.live()
        self.verify(live, paths, hashes)

    def test_rehashed_observation_does_not_override_failure(self):
        live, paths, hashes = self.live()
        observation = pub.read_json(paths[('bun', 'observation')])
        observation['accepted'] = False
        self.rewrite(live, paths, 'observation', observation)
        with self.assertRaisesRegex(ValueError, 'observation did not accept'):
            self.verify(live, paths, hashes)

    def test_rehashed_hello_claim_requires_exact_file_hash(self):
        live, paths, hashes = self.live()
        observation = pub.read_json(paths[('bun', 'observation')])
        observation['after']['hello.txt']['sha256'] = '0'*64
        self.rewrite(live, paths, 'observation', observation)
        with self.assertRaisesRegex(ValueError, 'exact Hello World file'):
            self.verify(live, paths, hashes)

    def test_rehashed_raw_failure_cannot_pass_summary(self):
        live, paths, hashes = self.live()
        event = json.loads(paths[('bun', 'runner')].read_text())
        event['payload']['result']['terminal']['classification'] = 'failure'
        self.rewrite(live, paths, 'runner', event)
        with self.assertRaisesRegex(ValueError, 'Raw runner terminal'):
            self.verify(live, paths, hashes)

    def test_report_cannot_substitute_kernel_identity(self):
        live, paths, hashes = self.live()
        live['runners']['bun']['kernel']['sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'Retained kernel bytes'):
            self.verify(live, paths, hashes)

    def test_rehashed_descriptor_cannot_change_kernel_source(self):
        live, paths, hashes = self.live()
        descriptor = pub.read_json(paths[('bun', 'descriptor')])
        descriptor['source']['commit'] = 'f'*40
        self.rewrite(live, paths, 'descriptor', descriptor)
        with self.assertRaisesRegex(ValueError, 'Kernel descriptor'):
            self.verify(live, paths, hashes)

    def test_native_capture_is_digest_bound_and_required(self):
        live, paths, hashes = self.live()
        paths[('bun', 'native')].write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Live evidence bytes changed'):
            self.verify(live, paths, hashes)
        del live['runners']['bun']['evidence']['native']
        with self.assertRaisesRegex(ValueError, 'Complete retained live evidence'):
            self.verify(live, paths, hashes)

    def test_unbound_live_file_rejected_at_publication(self):
        import assemble_kernel_rc
        live, paths, hashes = self.live()
        live_path = self.root/'live.json'; live_path.write_text(json.dumps(live))
        output = self.root/'complete'
        plan = assemble_kernel_rc.assemble(self.roots, output, self.evidence, live_path)
        plan['artifacts'] = [a for a in plan['artifacts'] if a['name'] != custody.live_asset_name('bun', 'native', live['runners']['bun']['evidence']['native'])]
        with self.assertRaisesRegex(ValueError, 'not bound to the reviewed plan'):
            custody.verify_live_evidence(plan, output, hashes)


if __name__ == '__main__':
    unittest.main()
