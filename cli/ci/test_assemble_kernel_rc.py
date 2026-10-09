import hashlib
import base64
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import assemble_kernel_rc as a
import package_local as package
import publication as p
import kernel_rc_evidence as custody


def linux_receipt_fixture(platform='linux-x64-gnu'):
    """Synthetic ELF/origin proof assembled locally; no native build/download claim."""
    import sdk_native_inventory as native
    from test_sdk_native_inventory import input_fixture, elf
    with tempfile.TemporaryDirectory() as directory:
        value, path, library, source = input_fixture(Path(directory), platform)
        machine = 62 if platform == 'linux-x64-gnu' else 183
        raw = native.elf_record('libgcc_s.so.1', elf(machine=machine))
        rows, origins = native.assign_origins([raw], value, [], lambda _: None)
        origins['wheels'] = [{'name': 'fixture', 'version': '1.0.0', 'wheelCandidateSha256s': ['e' * 64],
                              'recordSha256': 'a' * 64, 'licenses': []}]
        snapshot = value['sourceSnapshot']
        snapshot['sources']['harnesses/agents-sdk/run.py'] = 'f' * 64
        snapshot['sources']['harnesses/agents-sdk/requirements-build.txt'] = 'e' * 64
        # Source custody is real; the native bytes/results remain synthetic.
        snapshot['driverSha256'] = p.digest(package.CLI.parent / 'cli/ci/build_agents_sdk_linux.py')
        snapshot['sources']['cli/ci/sdk_native_inventory.py'] = p.digest(package.CLI.parent / 'cli/ci/sdk_native_inventory.py')
        return {'linuxBuildInputSha256': native.sha(('synthetic-native-input-' + platform).encode()), 'linuxBuildSourceSnapshot': snapshot,
                'nativeDependencies': {'libraries': rows, 'symbolClosureVerified': True, 'origins': origins,
                    'libgccSelection': {**native.record('libgcc_s.so.1', library.read_bytes()), 'analysisTocSha256': 'a' * 64}},
                'linuxLibraries': {'schema': 'openprose.sdk-packaged-libraries/1', 'elfCount': 1,
                    'requiredGlibcMaximum': raw['maximumRequiredGlibc'], 'libraries': [raw], 'modelCalls': 0}}


def sdk_fixture(platform):
    """Hermetic bytes with production-shaped custody; never executable release evidence."""
    helper = ('fixture-sdk-' + platform).encode(); notices = b'fixture-notices'
    if platform.startswith('linux'):
        from test_sdk_native_inventory import elf
        helper = elf(machine=62 if platform == 'linux-x64-gnu' else 183)
    sha = lambda data: hashlib.sha256(data).hexdigest()
    sdk = {'path': 'prose-agents-sdk', 'byteLength': len(helper), 'sha256': sha(helper),
           'noticesSha256': sha(notices), 'python': '3.10.20', 'pyinstaller': '6.22.3', 'version': '0.1.0',
           'discovery': 'canonical-cli-sibling', 'selfTest': custody.SDK_IMPORT_TEST,
           'toolSelfTest': custody.SDK_TOOL_TEST, 'dependencyLockSha256': 'e'*64}
    os_name, arch = platform.split('-')[:2]
    arch = {'darwin-arm64': 'arm64', 'darwin-x64': 'x86_64', 'linux-arm64-gnu': 'aarch64', 'linux-x64-gnu': 'x86_64'}.get(platform, arch)
    receipt = {'schema': 'openprose.agents-sdk-build/1', 'platform': os_name, 'architecture': arch,
               'python': sdk['python'], 'pyinstaller': sdk['pyinstaller'],
               'helper': {k:sdk[k] for k in ('path','byteLength','sha256')},
               'notices': {'path': 'AGENTS-SDK-NOTICES.txt', 'byteLength': len(notices), 'sha256': sha(notices)},
               'sources': {'harnesses/agents-sdk/run.py': 'f'*64, 'harnesses/agents-sdk/requirements-build.txt': 'e'*64},
               'dependencies': [{'name': 'fixture', 'version': '1.0.0', 'wheelSha256': ['e'*64]}],
               'selfTest': custody.SDK_IMPORT_TEST, 'toolSelfTest': custody.SDK_TOOL_TEST,
               'modelCalls': 0, 'publicationAuthorized': False,
               'authority': 'hermetic-test-fixture-not-release-evidence',
               'linuxLibraries': {'requiredGlibcMaximum': '2.34'} if os_name == 'linux' else 'not-applicable'}
    if os_name == 'linux':
        receipt.update(linux_receipt_fixture(platform))
    encoded = json.dumps(receipt).encode(); sdk['receiptSha256'] = sha(encoded)
    return sdk, [('prose-agents-sdk', helper, 0o755), ('agents-sdk-build.json', encoded, 0o644),
                 ('AGENTS-SDK-NOTICES.txt', notices, 0o644)]


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.roots = []
        self.source = 'a' * 40
        self.version = '0.15.0-rc.1'
        image, manifest_sha = package.read_image_manifest(package.CLI / 'shared/image/echo-v0/manifest.json')
        self.image = package.image_identity(image, manifest_sha)
        for platform in p.PLATFORMS:
            root = self.root / platform
            output = root / 'package'
            output.mkdir(parents=True)
            self.roots.append(root)
            artifacts = []
            from test_kernel_rc_evidence import sdk_fixture as complete_sdk_fixture
            sdk, sdk_members = complete_sdk_fixture(platform)
            for implementation in ('bun', 'rust'):
                name = implementation + '-' + platform + '.tgz'
                data = (implementation + platform).encode()
                package.tar_gz(output / name, [('root/prose', data, 0o755),
                    *[('root/' + member, value, mode) for member, (value, mode) in sdk_members['files'].items()]],
                    0, sdk_table=sdk_members, sdk_prefix='root/')
                artifacts.append(self.record(output / name, implementation, 'standalone-archive', platform))
            runtime = {'minimumGlibc': '2.34', 'requiredGlibcMaximum': {'rust': '2.34', 'bun': '2.34'}, 'executionEvidence': 'ubuntu-22.04-only'} if platform.startswith('linux-') else 'not-applicable'
            meta, native = package.npm_packages(output, ('bun' + platform).encode(), self.version, platform, 0, self.source, self.image, runtime, 'kernel-rc', package.HELLO_EXAMPLE.read_bytes(), publication_platforms='posix-four', sdk_members=sdk_members)
            artifacts += [self.record(meta, 'bun', 'npm-meta', None), self.record(native, 'bun', 'npm-platform', platform)]
            manifest = {'schema': 'openprose.local-release-manifest/1', 'mode': 'kernel-rc', 'platform': platform, 'version': self.version, 'source': {'revision': self.source, 'verification': 'matched-product-doctor'}, 'releaseEligible': False, 'publicationAuthorized': False, 'buildProfiles': {r: {'profile': 'release', 'testSeamsEnabled': False} for r in ('bun', 'rust')}, 'imageSource': 'published-on-run', 'embeddedDiagnosticImage': {k:v for k,v in self.image.items() if k != 'releaseEligible'}, 'kernelPolicy': package.PUBLISHED_KERNEL_POLICY, 'artifacts': artifacts, 'agentsSdk': sdk}
            (output / 'release-manifest.json').write_text(json.dumps(manifest))
            logs = root / 'logs'; logs.mkdir()
            with tarfile.open(meta) as archive:
                launcher_hash = hashlib.sha256(archive.extractfile('package/bin/prose.js').read()).hexdigest()
            for name in custody.CHECKS:
                runner = 'rust' if name.endswith('-rust') else 'bun'
                check = {'schema': 'openprose.published-release-check/1', 'status': 'passed-offline-release-check', 'runner': runner, 'commit': self.source, 'version': self.version, 'binarySha256': launcher_hash if name == 'installed-npm' else hashlib.sha256((runner+platform).encode()).hexdigest(), 'imageSource': 'published-on-run', 'testSeamsEnabled': False, 'modelCalls': 0, 'networkCalls': 0}
                if name == 'installed-npm':
                    check['nodeInterpreterSha256'] = 'd'*64
                (logs / (name + '.json')).write_text(json.dumps(check))
            for relative in custody.SDK_PROBES:
                (root / relative).write_text(json.dumps(custody.SDK_TOOL_TEST if 'sdk-tools-' in relative else custody.SDK_IMPORT_TEST))
            identities = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'byteLength': len(data)}
                          for name, (data, _) in sdk_members['files'].items()
                          if name in ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt')}
            sdk_receipt = json.loads(sdk_members['files']['agents-sdk-build.json'][0])
            if platform.startswith('darwin'):
                payload = sdk_receipt['payload']
                identities['supportTree'] = {'payload': payload,
                    'sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                    'byteLength': payload['totalRegularBytes'], 'entryCount': len(payload['entries'])}
            (logs / 'installed-sdk-payloads.json').write_text(json.dumps([
                {'surface': 'installed-' + surface, 'before': identities, 'after': identities}
                for surface in ('bun', 'rust', 'npm')], sort_keys=True) + '\n')
            if platform.startswith('darwin'):
                from test_kernel_rc_evidence import sdk_producer_fixture
                for relative, encoded in sdk_producer_fixture(sdk_members).items():
                    (root / relative).write_bytes(encoded)
            evidence = {str(f.relative_to(root)): {'sha256': p.digest(f), 'byteLength': f.stat().st_size} for directory in (output, logs) for f in directory.iterdir()}
            report = {'schema': 'openprose.kernel-rc-build/1', 'platform': platform, 'version': self.version, 'sourceRevision': self.source, 'imageSource': 'published-on-run', 'testSeamsEnabled': False, 'qualification': 'offline-install-only', 'publicationAuthorized': False, 'modelCalls': 0, 'kernelFetches': 0, 'checks': [{'name': n, 'status': 'passed'} for n in ('built-bun','built-rust','installed-bun','installed-rust','installed-npm')], 'evidence': evidence}
            from test_kernel_rc_evidence import producer_command_log_fixture
            for relative, encoded in producer_command_log_fixture(report).items():
                (root / relative).write_bytes(encoded)
                report['evidence'][relative] = {'sha256': p.digest(root / relative), 'byteLength': len(encoded)}
            if platform.startswith('linux'):
                from test_kernel_rc_evidence import linux_runtime_fixture
                relative = custody.LINUX_RUNTIME_EVIDENCE
                encoded = linux_runtime_fixture(report, manifest, sdk_members, package.CLI.parent)[relative]
                (root / relative).write_bytes(encoded)
                report['evidence'][relative] = {'sha256': p.digest(root / relative), 'byteLength': len(encoded)}
            (root / 'build-report.json').write_text(json.dumps(report))
        self.evidence = 'https://github.com/openprose/example-evidence/tree/' + 'b'*40 + '/test'

    def record(self, path, implementation, kind, platform):
        return {'path': path.name, 'sha256': p.digest(path), 'byteLength': path.stat().st_size, 'implementation': implementation, 'kind': kind, 'platform': platform}

    def live_report(self):
        release = 'fixture-rc.1'
        kernel_bytes = b'# Kernel fixture\n'
        kernel_sha = hashlib.sha256(kernel_bytes).hexdigest()
        aggregate = hashlib.sha256(b'payload/kernel.md\0' + str(len(kernel_bytes)).encode() + b'\0' + kernel_bytes + b'\0').hexdigest()
        inventory = json.dumps({'README.md': {'mode': '100644', 'sha256': kernel_sha}}).encode()
        descriptor = {'identity': 'openprose/core', 'release': release, 'source': {'commit': 'e'*40}, 'exports': {'entry': 'README.md'}, 'inventory': 'releases/'+release+'/core/inventory.json', 'inventory_sha256': hashlib.sha256(inventory).hexdigest()}
        live = {'schema': 'openprose.kernel-rc-live-smoke/1', 'sourceSha': self.source, 'version': self.version, 'status': 'pass', 'platform': 'darwin-arm64', 'runners': {}}
        for runner in ('bun', 'rust'):
            observation = {'exit_code': 0, 'outer_watchdog_triggered': False, 'accepted': True, 'hello_exact': True, 'changed_original_files': [], 'new_files': ['hello.txt'], 'validation_failures': [], 'after': {'hello.txt': {'type': 'file', 'links': 1, 'sha256': hashlib.sha256(b'Hello World\n').hexdigest()}}}
            kernel = {'version': 'kernel-'+release, 'sha256': aggregate, 'entrypoint': 'https://pkg.prose.md/kernel.md', 'resolvedUrl': 'https://pkg.prose.md/releases/'+release+'/core/README.md', 'sourceRevision': 'e'*40, 'kernelSha256': kernel_sha}
            result = {'schema': 'openprose.runner-result/1', 'runner': {'name': runner, 'commit': self.source, 'version': self.version}, 'runnerExitCode': 0, 'terminal': {'classification': 'success', 'transportCompleted': True, 'terminalEventObserved': True}, 'languageImage': {'formatVersion': 'openprose.skill-runtime-image/1', 'version': kernel['version'], 'sha256': aggregate}, 'digests': {'deliveredImageSha256': kernel_sha}}
            values = {'observation': json.dumps(observation).encode(), 'native': b'{"fixture":true}\n', 'runner': json.dumps({'schema': 'openprose.normalized-event/1', 'type': 'runner.completed', 'payload': {'result': result}}).encode()+b'\n', 'readiness': b'{}', 'selection': b'{}', 'kernel': kernel_bytes, 'inventory': inventory, 'descriptor': json.dumps(descriptor).encode()}
            records = {}
            for role, data in values.items():
                name = runner+'-'+role+'.json'
                path = self.root/name
                path.write_bytes(data)
                records[role] = {'path': name, 'sha256': p.digest(path), 'byteLength': len(data)}
            live['runners'][runner] = {'accepted': True, 'helloExact': True, 'binarySha256': hashlib.sha256((runner+'darwin-arm64').encode()).hexdigest(), 'kernel': kernel, 'evidence': records}
        return live

    def test_generated_packages_assemble_without_claiming_live_qualification(self):
        plan = a.assemble(self.roots, self.root/'assembly', self.evidence)
        self.assertEqual(plan['qualification']['status'], 'development')
        self.assertEqual(len([x for x in plan['artifacts'] if x['kind'] == 'npm']), 5)
        with self.assertRaisesRegex(ValueError, 'Kernel qualification'):
            p.load_plan(self.root/'assembly/publication-plan.json')

    def test_command_bundle_is_required_and_rehashed_raw_poison_refuses_before_output(self):
        root = self.roots[0]; report_path = root / 'build-report.json'
        report = json.loads(report_path.read_bytes()); path = root / custody.COMMAND_LOG_EVIDENCE
        original = path.read_bytes(); del report['evidence'][custody.COMMAND_LOG_EVIDENCE]
        report_path.write_text(json.dumps(report))
        missing = self.root / 'missing-command-bundle'
        with self.assertRaises(ValueError): a.assemble(self.roots, missing, self.evidence)
        self.assertFalse(missing.exists())
        bundle = json.loads(original); bundle['members'][0]['base64'] = 'eA=='
        path.write_text(json.dumps(bundle)); report['evidence'][custody.COMMAND_LOG_EVIDENCE] = {
            'sha256': p.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        poisoned = self.root / 'poisoned-command-bundle'
        with self.assertRaises(ValueError): a.assemble(self.roots, poisoned, self.evidence)
        self.assertFalse(poisoned.exists())

    def test_actual_cohort_reserves_all_live_names_and_refuses_before_copy(self):
        baseline = a.assemble(self.roots, self.root / 'budget-baseline', self.evidence)
        reserved_live = len(custody.LIVE_ROLES) * 2
        self.assertEqual(reserved_live, 16)
        available = 128 - reserved_live - len(baseline['artifacts'])
        self.assertGreater(available, 0)
        root = self.roots[0]; report_path = root / 'build-report.json'; report = json.loads(report_path.read_bytes())
        for index in range(available):
            relative = 'logs/extra-opaque-' + str(index) + '.log'; path = root / relative
            path.write_bytes(b'unknown log retained separately')
            report['evidence'][relative] = {'sha256': p.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        offline = a.assemble(self.roots, self.root / 'budget-boundary', self.evidence)
        self.assertEqual(len(offline['artifacts']) + reserved_live, 128)
        live_path = self.root / 'budget-live.json'; live_path.write_text(json.dumps(self.live_report()))
        complete = a.assemble(self.roots, self.root / 'budget-with-live', self.evidence, live_path)
        self.assertEqual(len(complete['artifacts']), 128)
        self.assertEqual(len([row for row in complete['artifacts'] if row['name'].startswith('live-')]), 16)
        names = {row['name'] for row in complete['artifacts']}
        for platform in p.PLATFORMS:
            for relative in (*custody.CHECK_PATHS, *custody.SDK_PROBES,
                             custody.SDK_PAYLOAD_EVIDENCE, custody.COMMAND_LOG_EVIDENCE):
                self.assertIn(platform + '-logs-' + Path(relative).name, names)
            if platform.startswith('darwin'):
                for relative in (custody.SDK_SIGNATURE_EVIDENCE, custody.SDK_COLLECT_EVIDENCE):
                    self.assertIn(platform + '-logs-' + Path(relative).name, names)
            else:
                self.assertIn(platform + '-logs-' + Path(custody.LINUX_RUNTIME_EVIDENCE).name, names)
        relative = 'logs/one-too-many-opaque.log'; path = root / relative; path.write_bytes(b'kept')
        report['evidence'][relative] = {'sha256': p.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        output = self.root / 'over-budget-before-copy'
        with self.assertRaises(ValueError): a.assemble(self.roots, output, self.evidence)
        self.assertFalse(output.exists())

    def test_linux_runtime_packet_is_mandatory_before_output(self):
        for platform in ('linux-x64-gnu', 'linux-arm64-gnu'):
            root = next(root for root in self.roots if root.name == platform)
            path = root / 'build-report.json'; original = path.read_bytes(); report = json.loads(original)
            del report['evidence'][custody.LINUX_RUNTIME_EVIDENCE]; path.write_text(json.dumps(report))
            output = self.root / ('missing-runtime-' + platform)
            with self.assertRaises(ValueError): a.assemble(self.roots, output, self.evidence)
            self.assertFalse(output.exists()); path.write_bytes(original)

    def test_rehashed_linux_runtime_source_result_script_or_record_poison_refuses(self):
        root = next(root for root in self.roots if root.name == 'linux-x64-gnu')
        path = root / custody.LINUX_RUNTIME_EVIDENCE; original = path.read_bytes()
        report_path = root / 'build-report.json'; original_report = report_path.read_bytes()
        def replace(bundle, name, data):
            row = next(row for row in bundle['members'] if row['path'] == name)
            row.update(base64=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest(), byteLength=len(data))
        for poison in ('source', 'result', 'script', 'member', 'oversize'):
            bundle = json.loads(original)
            if poison == 'source': replace(bundle, 'sources/cli/ci/sdk_native_inventory.py', b'wrong trusted source')
            elif poison == 'result':
                bundle['runtimeReport']['results']['imports']['modelCalls'] = 1
                replace(bundle, 'runtime-report.json', json.dumps(bundle['runtimeReport']).encode())
                replace(bundle, 'job/imports.log', json.dumps(bundle['runtimeReport']['results']['imports']).encode())
            elif poison == 'script': replace(bundle, 'job/version.sh', b'timeout --kill-after=1s 6s /source/prose-agents-sdk --version\n')
            elif poison == 'member': bundle['members'].pop()
            encoded = json.dumps(bundle).encode()
            if poison == 'oversize': encoded += b' ' * (16 * 1024 * 1024 + 1 - len(encoded))
            path.write_bytes(encoded); report = json.loads(original_report)
            report['evidence'][custody.LINUX_RUNTIME_EVIDENCE] = {'sha256': p.digest(path), 'byteLength': len(encoded)}
            report_path.write_text(json.dumps(report)); output = self.root / ('poison-runtime-' + poison)
            with self.subTest(poison=poison), self.assertRaises(ValueError): a.assemble(self.roots, output, self.evidence)
            self.assertFalse(output.exists())
        path.write_bytes(original); report_path.write_bytes(original_report)

    def resize_installed_sdk_evidence(self, byte_length):
        root = next(root for root in self.roots if root.name == 'darwin-arm64')
        path = root / custody.SDK_PAYLOAD_EVIDENCE
        original = path.read_bytes()
        self.assertLess(len(original), byte_length)
        path.write_bytes(original + b' ' * (byte_length - len(original)))
        self.assertEqual(path.stat().st_size, byte_length)
        self.assertEqual(json.loads(path.read_bytes()), json.loads(original))
        report_path = root / 'build-report.json'
        report = json.loads(report_path.read_bytes())
        report['evidence'][custody.SDK_PAYLOAD_EVIDENCE] = {
            'sha256': p.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        return root, path

    def test_complete_sdk_evidence_above_generic_json_limit_assembles(self):
        _, path = self.resize_installed_sdk_evidence(1024 * 1024 + 128)
        with self.assertRaisesRegex(ValueError, 'Invalid JSON file'):
            p.read_json(path)
        plan = a.assemble(self.roots, self.root / 'large-sdk-assembly', self.evidence)
        self.assertEqual(plan['qualification']['status'], 'development')
        self.assertTrue((self.root / 'large-sdk-assembly/publication-plan.json').is_file())

    def test_complete_sdk_evidence_above_specific_json_limit_refuses_assembly(self):
        self.resize_installed_sdk_evidence(16 * 1024 * 1024 + 1)
        output = self.root / 'oversize-sdk-assembly'
        with self.assertRaisesRegex(ValueError, 'Invalid JSON file'):
            a.assemble(self.roots, output, self.evidence)
        self.assertFalse(output.exists())

    def test_aggregate_checksums_bind_only_all_install_archives(self):
        output = self.root / 'checksums'
        plan = a.assemble(self.roots, output, self.evidence)
        archives = sorted((item for item in plan['artifacts'] if item['kind'] in ('standalone', 'npm')), key=lambda item: item['name'])
        expected = ''.join(p.digest(output / item['name']) + '  ' + item['name'] + '\n' for item in archives).encode('ascii')
        self.assertEqual(len(archives), 13)
        self.assertEqual((output / 'SHA256SUMS').read_bytes(), expected)
        record = next(item for item in plan['artifacts'] if item['name'] == 'SHA256SUMS')
        self.assertEqual(record, {'name': 'SHA256SUMS', 'sha256': hashlib.sha256(expected).hexdigest(), 'size': len(expected), 'kind': 'evidence', 'platform': 'all', 'implementation': 'shared'})
        reversed_output = self.root / 'reversed-checksums'
        a.assemble(list(reversed(self.roots)), reversed_output, self.evidence)
        self.assertEqual((reversed_output / 'SHA256SUMS').read_bytes(), expected)

    def test_missing_complete_installed_payload_evidence_is_refused(self):
        root = self.roots[0]
        report_path = root / 'build-report.json'
        report = json.loads(report_path.read_text())
        del report['evidence'][custody.SDK_PAYLOAD_EVIDENCE]
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'installed SDK payload evidence'):
            a.assemble(self.roots, self.root / 'assembly', self.evidence)
        self.assertFalse((self.root / 'assembly').exists())

    def test_rehashed_installed_support_claim_must_match_packaged_tree(self):
        root = next(root for root in self.roots if root.name == 'darwin-arm64')
        path = root / custody.SDK_PAYLOAD_EVIDENCE
        records = json.loads(path.read_text())
        records[0]['after']['supportTree']['entryCount'] += 1
        path.write_text(json.dumps(records))
        report_path = root / 'build-report.json'
        report = json.loads(report_path.read_text())
        report['evidence'][custody.SDK_PAYLOAD_EVIDENCE] = {'sha256': p.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'packaged complete tree'):
            a.assemble(self.roots, self.root / 'assembly', self.evidence)
        self.assertFalse((self.root / 'assembly').exists())

    def test_rehashed_failed_signature_proof_cannot_assemble(self):
        root = next(root for root in self.roots if root.name == 'darwin-arm64')
        path = root / custody.SDK_SIGNATURE_EVIDENCE
        bundle = json.loads(path.read_text())
        bundle['checks'][0]['proof']['success'] = False
        path.write_text(json.dumps(bundle))
        report_path = root / 'build-report.json'
        report = json.loads(report_path.read_text())
        report['evidence'][custody.SDK_SIGNATURE_EVIDENCE] = {'sha256': p.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'signature verification'):
            a.assemble(self.roots, self.root / 'assembly', self.evidence)
        self.assertFalse((self.root / 'assembly').exists())

    def test_damaged_generated_package_refused(self):
        next((self.roots[0]/'package').glob('*.tgz')).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'evidence bytes changed'):
            a.assemble(self.roots, self.root/'assembly', self.evidence)
        self.assertFalse((self.root/'assembly').exists())

    def test_duplicate_platform_refused(self):
        with self.assertRaisesRegex(ValueError, 'duplicate native'):
            a.assemble([self.roots[0]]*4, self.root/'assembly', self.evidence)

    def test_live_evidence_must_bind_exact_both_binary_bytes(self):
        live = self.live_report()
        path = self.root/'live.json'
        path.write_text(json.dumps(live))
        plan = a.assemble(self.roots, self.root/'assembly', self.evidence, path)
        self.assertEqual(plan['qualification']['status'], 'kernel-smoke-qualified')
        _, hashes = p.verify_local(plan, self.root/'assembly')
        for platform in p.PLATFORMS:
            custody.verify_platform_evidence(plan, self.root/'assembly', platform, platform+'-build-report.json', hashes)
        live['runners']['rust']['binarySha256'] = '0'*64
        path.write_text(json.dumps(live))
        with self.assertRaisesRegex(ValueError, 'differs from release bytes'):
            a.assemble(self.roots, self.root/'changed-live', self.evidence, path)


class NativeReceiptConsumerTests(unittest.TestCase):
    def receipt(self, platform='linux-x64-gnu'):
        sdk, members = sdk_fixture(platform)
        return json.loads(next(data for name, data, mode in members if name == 'agents-sdk-build.json'))

    def test_complete_native_projection_and_nonshipped_declarations(self):
        receipt = self.receipt()
        components = custody.sdk_native_sbom_components(receipt)
        self.assertEqual([r['name'] for r in components], ['libgcc_s.so.1', 'libgcc'])
        self.assertFalse(any('fixture' == r['name'] for r in components))
        custody.validate_sdk_native_sbom(components, receipt)
        self.assertEqual(custody.sdk_native_sbom_components(self.receipt('darwin-arm64')), [])

    def test_receipt_omission_source_origin_and_runtime_poison(self):
        import copy
        mutations = [lambda r: r.pop('nativeDependencies'),
                     lambda r: r['linuxBuildSourceSnapshot']['sources'].pop('cli/ci/sdk_native_inventory.py'),
                     lambda r: r['sources'].update({'harnesses/agents-sdk/run.py': 'b' * 64}),
                     lambda r: r['nativeDependencies']['libraries'][0].update(origin='wheel:missing'),
                     lambda r: r['nativeDependencies']['origins']['supplier']['library'].update(sha256='b' * 64),
                     lambda r: r['linuxLibraries'].update(elfCount=2)]
        for mutate in mutations:
            receipt = copy.deepcopy(self.receipt()); mutate(receipt)
            with self.subTest(mutate=mutate), self.assertRaises((ValueError, KeyError, TypeError)):
                custody.validate_sdk_native_receipt(receipt)
        mac = self.receipt('darwin-arm64'); mac['nativeDependencies'] = {}
        with self.assertRaises(ValueError): custody.validate_sdk_native_receipt(mac)

    def test_python_and_wheel_origins_do_not_promote_declarations_or_candidates(self):
        import copy
        receipt = self.receipt(); native = receipt['nativeDependencies']
        python = native['origins']['python']
        python['declaredExtensionLicenses'] = [{'name': 'unshipped-extension', 'licenses': ['fixture'], 'licensePaths': ['LICENSE']}]
        wheel = native['origins']['wheels'][0]
        wheel['licenses'] = [{'path': 'fixture.dist-info/licenses/LICENSE', 'sha256': 'b' * 64, 'byteLength': 3}]
        for path, origin, digest in [('libpython.so', 'python', 'c' * 64), ('fixture.so', 'wheel:fixture', 'd' * 64)]:
            row = copy.deepcopy(native['libraries'][0]); row.update(path=path, origin=origin, sha256=digest)
            native['libraries'].append(row)
        native['libraries'].sort(key=lambda row: row['path'])
        receipt['linuxLibraries']['libraries'] = [{key: value for key, value in row.items() if key != 'origin'} for row in native['libraries']]
        receipt['linuxLibraries']['elfCount'] = 3
        components = custody.sdk_native_sbom_components(receipt)
        origins = [row for row in components if row['type'] == 'library']
        self.assertEqual({row['name'] for row in origins}, {'CPython', 'libgcc', 'fixture'})
        self.assertTrue(all('hashes' not in row for row in origins))
        self.assertNotIn('unshipped-extension', json.dumps(components))
        self.assertIn('wheelCandidateSha256s', json.dumps(components))

    def test_sbom_omission_duplicate_substitution_and_license_poison(self):
        import copy
        receipt = self.receipt(); original = custody.sdk_native_sbom_components(receipt)
        poisoned = [original[:-1], original + [original[0]], []]
        changed = copy.deepcopy(original); changed[0]['hashes'][0]['content'] = 'b' * 64; poisoned.append(changed)
        changed = copy.deepcopy(original); changed[-1]['properties'][-1]['value'] = '{}'; poisoned.append(changed)
        for components in poisoned:
            with self.subTest(components=components), self.assertRaises(ValueError):
                custody.validate_sdk_native_sbom(components, receipt)
        with self.assertRaises(ValueError): custody.validate_sdk_native_sbom(original)


if __name__ == '__main__':
    unittest.main()
