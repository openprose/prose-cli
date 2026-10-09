"""Mutation tests for source/check/package custody, using generated npm bytes."""
import hashlib
import json
import unittest
from pathlib import Path
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



def producer_command_log_fixture(report):
    """Opaque synthetic bytes only; no build, command or native execution authority."""
    import base64
    names = custody.COMMAND_LOG_MAC_MEMBERS if report['platform'].startswith('darwin-') else custody.COMMAND_LOG_LINUX_MEMBERS
    rows = []
    for i, name in enumerate(names):
        data = b'' if i == 0 else b'Synthetic opaque command bytes: ' + name.encode() + b'\x00\xff'
        rows.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                     'byteLength': len(data), 'base64': base64.b64encode(data).decode()})
    bundle = {'schema': 'openprose.kernel-rc-command-logs/1', 'platform': report['platform'],
              'sourceRevision': report['sourceRevision'], 'version': report['version'], 'members': rows}
    return {custody.COMMAND_LOG_EVIDENCE: json.dumps(bundle, sort_keys=True).encode()}


def linux_runtime_fixture(report, manifest, sdk_table, source_root):
    """Synthetic retained runtime proof from trusted source bytes; never container execution."""
    import base64
    platform = report['platform']
    if platform.startswith('darwin-'): return {}
    sources = {name: custody.read_command_log_bytes(Path(source_root) / name) for name in custody.LINUX_RUNTIME_SOURCE_PATHS}
    expected_sources = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    lock = json.loads(sources['cli/ci/agents-sdk-linux-runtime.lock.json']); target = lock['platforms'][platform]
    machine = target['machine']; trio = custody._runtime_sdk_trio(sdk_table); receipt = json.loads(trio[custody.SDK_NAMES[1]])
    runtime = {'schema': 'openprose.sdk-linux-clean-runtime/1', 'platform': platform, 'runtime': target['runtime'],
        'inputs': {'payload': {name: hashlib.sha256(data).hexdigest() for name, data in trio.items()},
            'driverSha256': expected_sources[custody.LINUX_RUNTIME_SOURCE_PATHS[0]],
            'lockSha256': expected_sources[custody.LINUX_RUNTIME_SOURCE_PATHS[1]],
            'lifecycleSha256': expected_sources[custody.LINUX_RUNTIME_SOURCE_PATHS[2]],
            'inventorySha256': expected_sources[custody.LINUX_RUNTIME_SOURCE_PATHS[3]]},
        'results': {'imports': receipt['selfTest'], 'tools': receipt['toolSelfTest'], 'libraries': receipt['linuxLibraries']},
        'version': 'prose-agents-sdk 0.1.0', 'modelCalls': 0, 'networkUsed': False,
        'networkUsageScope': 'runtime-probes-only', 'preparationNetworkEnabled': True,
        'cpuFloorQualified': False, 'qualification': 'native-clean-glibc-2.34-only', 'publicationAuthorized': False}
    files = {'sources/' + name: data for name, data in sources.items()}
    files['runtime-report.json'] = json.dumps(runtime, sort_keys=True).encode() + b'\n'
    files.update(custody._runtime_script_contract(sources['cli/ci/verify_agents_sdk_linux.py'], machine))
    files['job/pull-runtime.log'] = b''
    files['job/inspect-runtime.log'] = json.dumps({'Id': 'sha256:' + target['runtime']['configSha256'], 'Os': 'linux',
        'Architecture': target['dockerPlatform'].split('/')[1], 'RepoDigests': [target['runtime']['image']]}).encode() + b'\n'
    files['job/base.log'] = ('glibc=2.34\narchitecture=' + machine + '\nnoBuildTools=true\n').encode()
    files['job/version.log'] = b'prose-agents-sdk 0.1.0\n'
    for name, value in runtime['results'].items(): files['job/' + name + '.log'] = json.dumps(value, sort_keys=True).encode() + b'\n'
    packet = {'schema': 'openprose.sdk-linux-runtime-evidence/1', 'platform': platform,
        'sourceRevision': report['sourceRevision'], 'version': report['version'], 'runtimeReport': runtime,
        'members': [{'path': name, 'sha256': hashlib.sha256(files[name]).hexdigest(), 'byteLength': len(files[name]),
            'base64': base64.b64encode(files[name]).decode()} for name in custody.LINUX_RUNTIME_MEMBER_PATHS]}
    files[custody.LINUX_RUNTIME_EVIDENCE] = json.dumps(packet, sort_keys=True).encode()
    return files


class LinuxRuntimeEvidenceTests(unittest.TestCase):
    SOURCE_ROOT = Path(__file__).resolve().parents[2]

    def fixture(self, platform='linux-x64-gnu'):
        report = {'schema': 'openprose.kernel-rc-build/1', 'platform': platform, 'sourceRevision': 'a' * 40,
                  'version': '0.15.0-rc.4', 'evidence': {}}
        sdk, direct = sdk_fixture(platform)
        table = {'files': {'root/prose': (b'fixture-cli', 0o755), **{'root/' + k: v for k, v in direct['files'].items()}},
                 'directories': {}, 'symlinks': {}}
        manifest = {'schema': 'openprose.local-release-manifest/1', 'mode': 'kernel-rc', 'platform': platform,
                    'version': report['version'], 'source': {'revision': report['sourceRevision'], 'verification': 'matched-product-doctor'},
                    'agentsSdk': sdk}
        files = linux_runtime_fixture(report, manifest, table, self.SOURCE_ROOT)
        self.bind(report, files)
        return report, manifest, table, files

    def bind(self, report, files):
        raw = files[custody.LINUX_RUNTIME_EVIDENCE]
        report['evidence'][custody.LINUX_RUNTIME_EVIDENCE] = {'sha256': hashlib.sha256(raw).hexdigest(), 'byteLength': len(raw)}

    def verify(self, report, manifest, table, files, expected=None):
        return custody.validate_linux_runtime_evidence(report, manifest, table, files.__getitem__,
            expected_sources=expected if expected is not None else custody.read_linux_runtime_sources(self.SOURCE_ROOT))

    def changed(self, mutate):
        report, manifest, table, files = self.fixture(); packet = json.loads(files[custody.LINUX_RUNTIME_EVIDENCE])
        mutate(packet); files[custody.LINUX_RUNTIME_EVIDENCE] = json.dumps(packet).encode(); self.bind(report, files)
        with self.assertRaises(ValueError): self.verify(report, manifest, table, files)

    def set_member(self, packet, name, data):
        import base64
        row = next(r for r in packet['members'] if r['path'] == name)
        row.update(sha256=hashlib.sha256(data).hexdigest(), byteLength=len(data), base64=base64.b64encode(data).decode())

    def claims(self, packet, mutate):
        mutate(packet['runtimeReport']); self.set_member(packet, 'runtime-report.json', json.dumps(packet['runtimeReport']).encode())

    def test_both_targets_complete_runtime_packet_and_real_source_lookup(self):
        for platform in ('linux-x64-gnu', 'linux-arm64-gnu'):
            report, manifest, table, files = self.fixture(platform)
            observed = self.verify(report, manifest, table, files)
            self.assertEqual(observed['platform'], platform)
            self.assertEqual(len(json.loads(files[custody.LINUX_RUNTIME_EVIDENCE])['members']), 17)
            self.assertLess(len(files[custody.LINUX_RUNTIME_EVIDENCE]), custody.LINUX_RUNTIME_MAX_BYTES)

    def test_mac_absence_and_unexpected_claim_and_missing_linux_before_callback(self):
        from unittest import mock
        report, manifest, _, _ = self.fixture()
        reader = mock.Mock(side_effect=AssertionError('Must not read'))
        report['platform'] = manifest['platform'] = 'darwin-arm64'; report['evidence'].clear()
        self.assertIsNone(custody.validate_linux_runtime_evidence(report, manifest, None, reader, expected_sources=None))
        report['evidence'][custody.LINUX_RUNTIME_EVIDENCE] = {}
        with self.assertRaises(ValueError): custody.validate_linux_runtime_evidence(report, manifest, None, reader, expected_sources=None)
        report['platform'] = manifest['platform'] = 'linux-x64-gnu'; report['evidence'].clear()
        with self.assertRaises(ValueError): custody.validate_linux_runtime_evidence(report, manifest, None, reader,
            expected_sources=custody.read_linux_runtime_sources(self.SOURCE_ROOT))
        reader.assert_not_called()

    def test_bad_headers_trusted_sources_and_packet_size_refuse_before_read(self):
        from unittest import mock
        for poison in ('header', 'source-map', 'source-hash', 'bool-size', 'oversize'):
            report, manifest, table, files = self.fixture(); sources = custody.read_linux_runtime_sources(self.SOURCE_ROOT)
            if poison == 'header': manifest['source']['verification'] = 'unverified'
            elif poison == 'source-map': sources.pop(custody.LINUX_RUNTIME_SOURCE_PATHS[0])
            elif poison == 'source-hash': sources[custody.LINUX_RUNTIME_SOURCE_PATHS[0]] = False
            else: report['evidence'][custody.LINUX_RUNTIME_EVIDENCE]['byteLength'] = True if poison == 'bool-size' else custody.LINUX_RUNTIME_MAX_BYTES + 1
            reader = mock.Mock(side_effect=AssertionError('Must not read'))
            with self.assertRaises(ValueError): custody.validate_linux_runtime_evidence(report, manifest, table, reader, expected_sources=sources)
            reader.assert_not_called()

    def test_rehashed_source_input_image_and_runtime_claims_rejected(self):
        mutations = [lambda r: r.update(cpuFloorQualified=True), lambda r: r.update(networkUsed=True),
            lambda r: r.update(networkUsageScope='all'), lambda r: r.update(preparationNetworkEnabled=False),
            lambda r: r.update(publicationAuthorized=True), lambda r: r.update(modelCalls=False),
            lambda r: r['inputs']['payload'].update({'prose-agents-sdk': '0' * 64}),
            lambda r: r['inputs'].update(inventorySha256='0' * 64),
            lambda r: r['runtime'].update(configSha256='0' * 64)]
        for mutate in mutations:
            self.changed(lambda p: self.claims(p, mutate))
        self.changed(lambda p: self.set_member(p, 'sources/cli/ci/verify_agents_sdk_linux.py', b'not trusted source'))
        self.changed(lambda p: self.set_member(p, 'job/inspect-runtime.log', b'{}'))

    def test_raw_report_probe_numeric_types_cannot_coerce_into_equal_receipt(self):
        def raw_report(packet):
            value = dict(packet['runtimeReport']); value['modelCalls'] = False
            self.set_member(packet, 'runtime-report.json', json.dumps(value).encode())
        self.changed(raw_report)
        for name, value in [('imports', False), ('libraries', 0.0)]:
            def raw_probe(packet, name=name, value=value):
                result = dict(packet['runtimeReport']['results'][name]); result['modelCalls'] = value
                self.set_member(packet, 'job/' + name + '.log', json.dumps(result).encode())
            self.changed(raw_probe)
        self.changed(lambda p: self.claims(p, lambda r: r['results']['libraries'].update(elfCount=1.0)))

    def test_coherently_rehashed_sdk_numeric_identities_rejected(self):
        # Mutate both authorities and regenerate runtime facts: hashes alone must not
        # turn Python's bool/int/float equality into a valid fixed SDK qualification.
        for field, key, value in [('selfTest', 'modelCalls', False),
                                  ('selfTest', 'modelCalls', 0.0),
                                  ('selfTest', 'certificates', 1),
                                  ('toolSelfTest', 'modelCalls', False),
                                  ('toolSelfTest', 'networkUsed', 0),
                                  ('toolSelfTest', 'shellEffects', 1.0),
                                  ('helper', 'byteLength', 'float'),
                                  ('notices', 'byteLength', 'float')]:
            with self.subTest(field=field, key=key, value=value):
                import copy
                report, manifest, table, _ = self.fixture()
                manifest, table = copy.deepcopy(manifest), copy.deepcopy(table)
                receipt = json.loads(table['files']['root/agents-sdk-build.json'][0])
                receipt[field][key] = float(receipt[field][key]) if value == 'float' else value
                if field in ('selfTest', 'toolSelfTest'):
                    manifest['agentsSdk'][field][key] = value
                encoded = json.dumps(receipt).encode()
                table['files']['root/agents-sdk-build.json'] = (encoded, 0o644)
                manifest['agentsSdk']['receiptSha256'] = hashlib.sha256(encoded).hexdigest()
                files = linux_runtime_fixture(report, manifest, table, self.SOURCE_ROOT)
                self.bind(report, files)
                with self.assertRaisesRegex(ValueError, 'Packaged SDK policy|Packaged SDK build receipt|SDK notices receipt'):
                    self.verify(report, manifest, table, files)

    def test_rehashed_scripts_base_version_and_probe_values_rejected(self):
        controls = [('job/version.sh', b'timeout --kill-after=1s 30s /source/prose-agents-sdk --version\n'),
                    ('job/base.log', b'glibc=2.34\nnoBuildTools=false\n'),
                    ('job/version.log', b'prose-agents-sdk 0.2.0\n'), ('job/tools.log', b'{}')]
        for name, data in controls: self.changed(lambda p, name=name, data=data: self.set_member(p, name, data))

    def test_rehashed_packet_member_and_json_envelope_poisons_rejected(self):
        for mutate in [lambda p: p.update(extra=True), lambda p: p.update(sourceRevision='b' * 40),
            lambda p: p['members'].pop(), lambda p: p['members'].reverse(),
            lambda p: p['members'][0].update(path='unknown'), lambda p: p['members'][0].update(byteLength=False),
            lambda p: p['members'][0].update(base64='!!!!', byteLength=1),
            lambda p: p['members'][0].update(sha256='0' * 64)]: self.changed(mutate)

    def materialize(self, root, files, table):
        (root / 'job/home').mkdir(parents=True); (root / 'job/docker-config').mkdir(); (root / 'payload').mkdir()
        for name in ('runtime-report.json', *custody.LINUX_RUNTIME_JOB_PATHS): (root / name).write_bytes(files[name])
        for name, data in custody._runtime_sdk_trio(table).items():
            path = root / 'payload' / name; path.write_bytes(data); path.chmod(0o555 if name == custody.SDK_NAMES[0] else 0o444)
        return json.loads(files['runtime-report.json'])

    def test_physical_tree_exact_payload_modes_and_unknowns_refuse(self):
        import tempfile
        for poison in ('good', 'extra-payload', 'extra-job', 'cleanup', 'mode', 'alias', 'nested', 'bytes', 'empty-home'):
            report, manifest, table, files = self.fixture()
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve(); runtime = self.materialize(root, files, table)
                if poison == 'extra-payload': (root / 'payload/unknown').write_bytes(b'x')
                elif poison == 'extra-job': (root / 'job/unexpected').write_bytes(b'x')
                elif poison == 'cleanup': (root / 'job/version-cleanup-error.txt').write_bytes(b'failure')
                elif poison == 'mode': (root / 'payload/prose-agents-sdk').chmod(0o755)
                elif poison == 'alias':
                    path = root / 'payload/agents-sdk-build.json'; path.unlink(); path.symlink_to('../runtime-report.json')
                elif poison == 'nested': (root / 'payload/nested').mkdir()
                elif poison == 'bytes':
                    path = root / 'payload/prose-agents-sdk'; path.chmod(0o755); path.write_bytes(b'changed'); path.chmod(0o555)
                elif poison == 'empty-home': (root / 'job/home/state').write_bytes(b'x')
                if poison == 'good': custody.validate_linux_runtime_tree(root, runtime, table)
                else:
                    with self.assertRaises(ValueError): custody.validate_linux_runtime_tree(root, runtime, table)

    def test_literal_script_extraction_never_executes_python(self):
        source = custody.read_command_log_bytes(self.SOURCE_ROOT / custody.LINUX_RUNTIME_SOURCE_PATHS[0])
        scripts = custody._runtime_script_contract(source + b'\nraise RuntimeError("must never execute")\n', 'x86_64')
        self.assertIn(b' 5s ', scripts['job/version.sh'])
        with self.assertRaises(ValueError): custody._runtime_script_contract(b'BASE = str("dynamic")\nPROBES=()\n', 'x86_64')


class ProducerCommandLogTests(unittest.TestCase):
    def fixture(self, platform='darwin-arm64'):
        report = {'schema': 'openprose.kernel-rc-build/1', 'platform': platform,
                  'sourceRevision': 'a' * 40, 'version': '0.15.0-rc.4', 'evidence': {}}
        manifest = {'schema': 'openprose.local-release-manifest/1', 'mode': 'kernel-rc',
                    'platform': platform, 'version': report['version'],
                    'source': {'revision': report['sourceRevision'], 'verification': 'matched-product-doctor'}}
        files = producer_command_log_fixture(report)
        self.bind(report, files)
        return report, manifest, files

    def bind(self, report, files):
        report['evidence'][custody.COMMAND_LOG_EVIDENCE] = {
            'sha256': hashlib.sha256(files[custody.COMMAND_LOG_EVIDENCE]).hexdigest(),
            'byteLength': len(files[custody.COMMAND_LOG_EVIDENCE])}

    def poison_bundle(self, mutate):
        report, manifest, files = self.fixture()
        bundle = json.loads(files[custody.COMMAND_LOG_EVIDENCE]); mutate(bundle)
        files[custody.COMMAND_LOG_EVIDENCE] = json.dumps(bundle).encode(); self.bind(report, files)
        with self.assertRaises(ValueError): custody.validate_producer_command_logs(report, manifest, files.__getitem__)

    def test_four_platforms_exact_opaque_bytes_and_zero_are_admitted(self):
        import base64
        for platform in custody.COMMAND_LOG_PLATFORMS:
            report, manifest, files = self.fixture(platform)
            observed = custody.validate_producer_command_logs(report, manifest, files.__getitem__)
            rows = json.loads(files[custody.COMMAND_LOG_EVIDENCE])['members']
            self.assertEqual(observed, {r['path']: base64.b64decode(r['base64']) for r in rows})
            self.assertEqual(next(iter(observed.values())), b'')
            self.assertEqual(len(observed), 8 if platform.startswith('darwin-') else 5)

    def test_headers_and_mandatory_evidence_refuse_before_read(self):
        from unittest import mock
        for part, key, value in [('report', 'schema', 'other'), ('report', 'platform', 'win-x64'),
                ('report', 'sourceRevision', 'not-source'), ('report', 'version', True),
                ('manifest', 'mode', 'development'), ('manifest', 'platform', 'darwin-x64'),
                ('manifest', 'version', 'different'), ('manifest', 'source', {'revision': 'a' * 40})]:
            report, manifest, _ = self.fixture(); (report if part == 'report' else manifest)[key] = value
            reader = mock.Mock(side_effect=AssertionError('Reader must not run'))
            with self.assertRaises(ValueError): custody.validate_producer_command_logs(report, manifest, reader)
            reader.assert_not_called()
        for poison in ('absent', 'original', 'bool', 'negative', 'oversize', 'hash', 'extra'):
            report, manifest, _ = self.fixture(); record = report['evidence'][custody.COMMAND_LOG_EVIDENCE]
            if poison == 'absent': report['evidence'].clear()
            elif poison == 'original': report['evidence'][custody.COMMAND_LOG_MAC_MEMBERS[0]] = record
            elif poison == 'hash': record['sha256'] = 'BAD'
            elif poison == 'extra': record['extra'] = 1
            else: record['byteLength'] = {'bool': True, 'negative': -1, 'oversize': custody.COMMAND_LOG_MAX_BYTES + 1}[poison]
            reader = mock.Mock(side_effect=AssertionError('Reader must not run'))
            with self.assertRaises(ValueError): custody.validate_producer_command_logs(report, manifest, reader)
            reader.assert_not_called()

    def test_rehashed_closed_header_and_member_coverage_poisons_refuse(self):
        controls = [lambda b: b.update(extra=True), lambda b: b.update(schema='other'),
            lambda b: b.update(platform='linux-x64-gnu'), lambda b: b.update(sourceRevision='b' * 40),
            lambda b: b.update(version='other'), lambda b: b['members'].pop(),
            lambda b: b['members'].append(b['members'][0]), lambda b: b['members'].reverse(),
            lambda b: b['members'][1].update(path=b['members'][0]['path']),
            lambda b: b['members'][0].update(path='logs/unapproved.log'),
            lambda b: b['members'][0].update(extra=True)]
        for mutate in controls:
            with self.subTest(mutate=mutate): self.poison_bundle(mutate)

    def test_rehashed_inner_bytes_types_and_noncanonical_base64_refuse(self):
        import base64
        controls = [lambda b: b['members'][0].update(byteLength=False),
            lambda b: b['members'][0].update(byteLength=-1),
            lambda b: b['members'][0].update(byteLength=custody.COMMAND_LOG_MAX_BYTES + 1),
            lambda b: b['members'][0].update(sha256='0' * 64),
            lambda b: b['members'][1].update(base64=base64.b64encode(b'changed').decode(), byteLength=7),
            lambda b: b['members'][1].update(base64='Zh==', byteLength=1, sha256=hashlib.sha256(b'f').hexdigest()),
            lambda b: b['members'][1].update(base64='Zg=\n', byteLength=1),
            lambda b: b['members'][0].update(base64='!!!!', byteLength=1),
            lambda b: b['members'][0].update(base64='éééé', byteLength=1)]
        for mutate in controls:
            with self.subTest(mutate=mutate): self.poison_bundle(mutate)

    def test_outer_hash_length_and_duplicate_json_key_refuse(self):
        for poison in ('hash', 'length', 'duplicate-json'):
            report, manifest, files = self.fixture()
            if poison == 'hash': report['evidence'][custody.COMMAND_LOG_EVIDENCE]['sha256'] = '0' * 64
            elif poison == 'length': report['evidence'][custody.COMMAND_LOG_EVIDENCE]['byteLength'] -= 1
            else:
                raw = files[custody.COMMAND_LOG_EVIDENCE]
                files[custody.COMMAND_LOG_EVIDENCE] = raw.replace(b'{', b'{"schema":"duplicate",', 1); self.bind(report, files)
            with self.assertRaises(ValueError): custody.validate_producer_command_logs(report, manifest, files.__getitem__)

    def test_aggregate_budget_refuses_before_read_or_json_decode(self):
        from unittest import mock
        report, manifest, files = self.fixture()
        with mock.patch.object(custody, 'COMMAND_LOG_MAX_BYTES', len(files[custody.COMMAND_LOG_EVIDENCE]) - 1):
            reader = mock.Mock(side_effect=AssertionError('Reader must not run'))
            with self.assertRaises(ValueError): custody.validate_producer_command_logs(report, manifest, reader)
            reader.assert_not_called()

    def test_reader_nofollow_refuses_leaf_replaced_before_open(self):
        import tempfile, os
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); path = root / 'log'; path.write_bytes(b'original')
            target = root / 'target'; target.write_bytes(b'outside')
            real = os.open
            def replace_then_open(*args, **kwargs):
                path.unlink(); path.symlink_to('target'); return real(*args, **kwargs)
            with mock.patch.object(custody.os, 'open', side_effect=replace_then_open):
                with self.assertRaises(OSError): custody.read_command_log_bytes(path)
            self.assertEqual(target.read_bytes(), b'outside')

    def test_unknown_logs_remain_bound_separately(self):
        report, manifest, files = self.fixture(); data = b'unknown retained bytes'
        report['evidence']['logs/unknown.log'] = {'sha256': hashlib.sha256(data).hexdigest(), 'byteLength': len(data)}
        self.assertEqual(len(custody.validate_producer_command_logs(report, manifest, files.__getitem__)), 8)
        self.assertIn('logs/unknown.log', report['evidence'])

    def test_stable_reader_accepts_zero_and_refuses_alias_special_oversize(self):
        import os, tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); path = root / 'log'; path.write_bytes(b'')
            self.assertEqual(custody.read_command_log_bytes(path), b'')
            alias = root / 'alias'; alias.symlink_to('log')
            with self.assertRaises(ValueError): custody.read_command_log_bytes(alias)
            fifo = root / 'fifo'; os.mkfifo(fifo)
            with self.assertRaises(ValueError): custody.read_command_log_bytes(fifo)
            path.write_bytes(b'12345')
            with mock.patch.object(custody, 'COMMAND_LOG_MAX_BYTES', 4), self.assertRaises(ValueError):
                custody.read_command_log_bytes(path)

    def test_stable_reader_refuses_actual_replacement_during_read(self):
        import tempfile, os
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); path = root / 'log'; path.write_bytes(b'original')
            real = os.fdopen
            class ReplacingReader:
                def __init__(self, stream): self.stream = stream
                def __enter__(self): return self
                def __exit__(self, *args): self.stream.close()
                def read(self, limit):
                    data = self.stream.read(limit); path.unlink(); path.write_bytes(data); return data
            with mock.patch.object(custody.os, 'fdopen', side_effect=lambda *a, **k: ReplacingReader(real(*a, **k))):
                with self.assertRaisesRegex(ValueError, 'changed during'): custody.read_command_log_bytes(path)


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
