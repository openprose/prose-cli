import hashlib
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
        return {'linuxBuildInputSha256': native.sha(('synthetic-native-input-' + platform).encode()), 'linuxBuildSourceSnapshot': snapshot,
                'nativeDependencies': {'libraries': rows, 'symbolClosureVerified': True, 'origins': origins,
                    'libgccSelection': {**native.record('libgcc_s.so.1', library.read_bytes()), 'analysisTocSha256': 'a' * 64}},
                'linuxLibraries': {'schema': 'openprose.sdk-packaged-libraries/1', 'elfCount': 1,
                    'requiredGlibcMaximum': raw['maximumRequiredGlibc'], 'libraries': [raw], 'modelCalls': 0}}


def sdk_fixture(platform):
    """Hermetic bytes with production-shaped custody; never executable release evidence."""
    helper = ('fixture-sdk-' + platform).encode(); notices = b'fixture-notices'
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
        self.root = Path(self.temp.name)
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
            sdk, sdk_members = sdk_fixture(platform)
            for implementation in ('bun', 'rust'):
                name = implementation + '-' + platform + '.tgz'
                data = (implementation + platform).encode()
                with tarfile.open(output / name, 'w:gz') as archive:
                    member = tarfile.TarInfo('root/prose')
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                    for sdk_name, sdk_data, sdk_mode in sdk_members:
                        item = tarfile.TarInfo('root/' + sdk_name); item.size = len(sdk_data); item.mode = sdk_mode
                        archive.addfile(item, io.BytesIO(sdk_data))
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
            evidence = {str(f.relative_to(root)): {'sha256': p.digest(f), 'byteLength': f.stat().st_size} for directory in (output, logs) for f in directory.iterdir()}
            report = {'schema': 'openprose.kernel-rc-build/1', 'platform': platform, 'version': self.version, 'sourceRevision': self.source, 'imageSource': 'published-on-run', 'testSeamsEnabled': False, 'qualification': 'offline-install-only', 'publicationAuthorized': False, 'modelCalls': 0, 'kernelFetches': 0, 'checks': [{'name': n, 'status': 'passed'} for n in ('built-bun','built-rust','installed-bun','installed-rust','installed-npm')], 'evidence': evidence}
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
