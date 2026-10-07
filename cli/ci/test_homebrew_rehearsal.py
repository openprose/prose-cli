"""Archive admission for the non-publishing native Homebrew rehearsal."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homebrew_rehearsal as rehearsal


class ArchiveAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.package = Path(self.root.name)
        self.manifest = {'schema': 'openprose.local-release-manifest/1', 'version': '0.15.0-dev.0',
                         'platform': 'darwin-arm64', 'artifacts': []}
        for implementation in ('bun', 'rust'):
            path = self.package / f'{implementation}.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                member = tarfile.TarInfo('package/prose')
                data = implementation.encode()
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            self.manifest['artifacts'].append({'kind': 'standalone-archive', 'implementation': implementation,
                'platform': 'darwin-arm64', 'path': path.name, 'byteLength': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

    def select(self, manifest=None):
        (self.package / 'release-manifest.json').write_text(json.dumps(manifest or self.manifest))
        return rehearsal.select_archives(self.package)

    def test_binds_both_native_archives_and_version(self):
        manifest, selected = self.select()
        self.assertEqual(set(selected), {'bun', 'rust'})
        self.assertEqual(manifest['version'], '0.15.0-dev.0')
        rendered = rehearsal.formula(manifest['version'], 'bun', self.package / 'bun.tar.gz', selected['bun']['sha256'])
        self.assertIn('bin.install "prose"', rendered)
        self.assertIn('bin.install "prose-agents-sdk"', rendered)
        self.assertIn('bin.install "prose-agents-sdk-runtime"', rendered)
        self.assertIn((self.package / 'bun.tar.gz').as_uri(), rendered)
        self.assertNotIn('pkg.prose.md', rendered)

    def test_sdk_archive_identity_is_required_before_homebrew_mutation(self):
        from test_kernel_rc_evidence import sdk_fixture
        import package_local as package
        sdk, table = sdk_fixture('darwin-arm64')
        self.manifest['agentsSdk'] = sdk
        with self.assertRaises(ValueError):
            self.select()
        for item in self.manifest['artifacts']:
            path = self.package / item['path']
            package.tar_gz(path, [('package/prose', item['implementation'].encode(), 0o755),
                *[('package/' + name, data, mode) for name, (data, mode) in table['files'].items()]],
                0, sdk_table=table, sdk_prefix='package/')
            item.update(byteLength=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.select()
        self.manifest['agentsSdk']['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.select()

    def test_changed_bytes_and_size_are_rejected(self):
        with (self.package / 'bun.tar.gz').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.select()

    def test_wrong_digest_is_rejected(self):
        self.manifest['artifacts'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.select()

    def test_missing_duplicate_or_mixed_implementation_is_rejected(self):
        for mutation in ('missing', 'duplicate', 'mixed'):
            manifest = copy.deepcopy(self.manifest)
            if mutation == 'missing': manifest['artifacts'].pop()
            elif mutation == 'duplicate': manifest['artifacts'].append(manifest['artifacts'][0])
            else: manifest['artifacts'][0]['platform'] = 'linux-x64-gnu'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.select(manifest)

    def test_unsafe_version_and_archive_path_are_rejected(self):
        for version in ('0.15.0"; system("unsafe")', 'not-a-version'):
            manifest = copy.deepcopy(self.manifest); manifest['version'] = version
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'version'):
                self.select(manifest)
        self.manifest['artifacts'][0]['path'] = '../outside.tar.gz'
        with self.assertRaisesRegex(ValueError, 'archive name'):
            self.select()

    def test_symlinked_archive_is_rejected(self):
        path = self.package / 'bun.tar.gz'; path.rename(self.package / 'original.tar.gz')
        path.symlink_to('original.tar.gz')
        with self.assertRaisesRegex(ValueError, 'regular native archive'):
            self.select()

    def test_unsafe_archive_member_is_rejected(self):
        path = self.package / 'bun.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            member = tarfile.TarInfo('../prose'); member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
        item = self.manifest['artifacts'][0]
        item.update(byteLength=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, 'Unsafe archive member'):
            self.select()

    def test_non_regular_manifest_is_rejected(self):
        target = self.package / 'original.json'; target.write_text(json.dumps(self.manifest))
        (self.package / 'release-manifest.json').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'regular package manifest'):
            rehearsal.select_archives(self.package)


class CompleteSdkEvidenceJsonAdmissionTests(unittest.TestCase):
    def setUp(self):
        # Reuse the complete authenticated multi-platform package factory only;
        # its native observations are synthetic and no executables are run.
        from test_assemble_kernel_rc import AssemblyTests
        self.fixture = AssemblyTests('test_generated_packages_assemble_without_claiming_live_qualification')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_complete_sdk_evidence_above_generic_limit_verifies(self):
        root, path = self.fixture.resize_installed_sdk_evidence(1024 * 1024 + 128)
        with self.assertRaisesRegex(ValueError, 'Invalid JSON file'):
            rehearsal.pub.read_json(path)
        verified, manifest, archives = rehearsal.verify_kernel_rc(root, self.fixture.source, self.fixture.version)
        self.assertEqual(verified['source'], self.fixture.source)
        self.assertEqual(manifest['platform'], 'darwin-arm64')
        self.assertEqual(set(archives), {'bun', 'rust'})

    def test_complete_sdk_evidence_above_specific_limit_refuses(self):
        root, _ = self.fixture.resize_installed_sdk_evidence(16 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'Invalid JSON file'):
            rehearsal.verify_kernel_rc(root, self.fixture.source, self.fixture.version)

    def test_linux_runtime_packet_missing_or_rehashed_source_refuses(self):
        import base64
        root = next(root for root in self.fixture.roots if root.name == 'linux-x64-gnu')
        report_path = root / 'build-report.json'; report = json.loads(report_path.read_bytes())
        path = root / rehearsal.custody.LINUX_RUNTIME_EVIDENCE; original = path.read_bytes()
        del report['evidence'][rehearsal.custody.LINUX_RUNTIME_EVIDENCE]; report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError): rehearsal.verify_kernel_rc(root, self.fixture.source, self.fixture.version)
        bundle = json.loads(original); row = next(row for row in bundle['members'] if row['path'] == 'sources/cli/ci/sdk_native_inventory.py')
        data = b'altered source despite recomputed inner and outer digests'
        row.update(base64=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest(), byteLength=len(data))
        path.write_text(json.dumps(bundle)); report['evidence'][rehearsal.custody.LINUX_RUNTIME_EVIDENCE] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError): rehearsal.verify_kernel_rc(root, self.fixture.source, self.fixture.version)

    def test_complete_linux_runtime_packets_verify_both_targets(self):
        for platform in ('linux-x64-gnu', 'linux-arm64-gnu'):
            root = next(root for root in self.fixture.roots if root.name == platform)
            with self.subTest(platform=platform):
                verified, manifest, archives = rehearsal.verify_kernel_rc(root, self.fixture.source, self.fixture.version)
                self.assertEqual(verified['source'], self.fixture.source)
                self.assertEqual(manifest['platform'], platform)
                self.assertEqual(set(archives), {'bun', 'rust'})

    def test_mac_runtime_claim_refuses_before_packet_callback(self):
        from unittest.mock import Mock
        root = next(root for root in self.fixture.roots if root.name == 'darwin-arm64')
        report_path = root / 'build-report.json'; report = json.loads(report_path.read_bytes())
        path = root / rehearsal.custody.LINUX_RUNTIME_EVIDENCE; path.write_bytes(b'unexpected Mac runtime claim')
        report['evidence'][rehearsal.custody.LINUX_RUNTIME_EVIDENCE] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        callback = Mock(side_effect=AssertionError('Mac packet callback must not be used'))
        actual = rehearsal.custody.validate_linux_runtime_evidence
        def validate(report, manifest, table, read_bytes, *, expected_sources):
            return actual(report, manifest, table, callback, expected_sources=expected_sources)
        with patch.object(rehearsal.custody, 'validate_linux_runtime_evidence', side_effect=validate), self.assertRaises(ValueError):
            rehearsal.verify_kernel_rc(root, self.fixture.source, self.fixture.version)
        callback.assert_not_called()


class KernelRcAdmissionTests(unittest.TestCase):
    SOURCE = 'a' * 40
    VERSION = '0.15.0-rc.3'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'package').mkdir()
        (self.root / 'logs').mkdir()
        self.manifest = {'schema': 'openprose.local-release-manifest/1', 'mode': 'kernel-rc',
                         'platform': 'darwin-arm64', 'version': self.VERSION,
                         'source': {'revision': self.SOURCE, 'verification': 'matched-product-doctor'},
                         'imageSource': 'published-on-run', 'releaseEligible': False, 'publicationAuthorized': False,
                         'buildProfiles': {i: {'profile': 'release', 'testSeamsEnabled': False} for i in ('bun', 'rust')},
                         'artifacts': []}
        for implementation in ('bun', 'rust'):
            self.archive(implementation + '.tar.gz', 'standalone-archive', implementation,
                         'darwin-arm64', 'package/prose', implementation.encode())
        self.archive('npm-platform.tgz', 'npm-platform', 'bun', 'darwin-arm64', 'package/bin/prose', b'bun')
        self.archive('npm-meta.tgz', 'npm-meta', 'bun', None, 'package/bin/prose.js', b'launcher')
        self.checks = {}
        for name in rehearsal.custody.CHECKS:
            runner = 'rust' if name.endswith('-rust') else 'bun'
            payload = b'launcher' if name == 'installed-npm' else runner.encode()
            self.checks[name] = {'schema': 'openprose.published-release-check/1',
                'status': 'passed-offline-release-check', 'runner': runner, 'commit': self.SOURCE,
                'version': self.VERSION, 'binarySha256': hashlib.sha256(payload).hexdigest(),
                'imageSource': 'published-on-run', 'testSeamsEnabled': False, 'modelCalls': 0, 'networkCalls': 0}
            if name == 'installed-npm':
                self.checks[name]['nodeInterpreterSha256'] = 'b' * 64
        self.report = {'schema': 'openprose.kernel-rc-build/1', 'sourceRevision': self.SOURCE,
                       'version': self.VERSION, 'platform': 'darwin-arm64', 'imageSource': 'published-on-run',
                       'testSeamsEnabled': False, 'publicationAuthorized': False, 'qualification': 'offline-install-only',
                       'modelCalls': 0, 'kernelFetches': 0,
                       'checks': [{'name': name, 'status': 'passed'} for name in rehearsal.custody.CHECKS]}
        self.write()

    def archive(self, filename, kind, implementation, platform, member_name, payload):
        path = self.root / 'package' / filename
        with tarfile.open(path, 'w:gz') as archive:
            member = tarfile.TarInfo(member_name)
            member.mode = 0o755
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        item = {'kind': kind, 'implementation': implementation, 'platform': platform, 'path': filename,
                'byteLength': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.manifest['artifacts'] = [a for a in self.manifest['artifacts'] if a['path'] != filename] + [item]

    def write(self):
        (self.root / 'package/release-manifest.json').write_text(json.dumps(self.manifest))
        for name, check in self.checks.items():
            (self.root / 'logs' / (name + '.json')).write_text(json.dumps(check))
        from test_kernel_rc_evidence import producer_command_log_fixture
        for relative, encoded in producer_command_log_fixture(self.report).items():
            (self.root / relative).write_bytes(encoded)
        self.report['evidence'] = {str(path.relative_to(self.root)): {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'byteLength': path.stat().st_size}
            for directory in ('package', 'logs') for path in (self.root / directory).iterdir()}
        (self.root / 'build-report.json').write_text(json.dumps(self.report))

    def verify(self, source=None, version=None):
        return rehearsal.verify_kernel_rc(self.root, source or self.SOURCE, version or self.VERSION)

    def test_accepts_exact_offline_native_custody_without_execution(self):
        verified, manifest, selected = self.verify()
        self.assertEqual(verified['source'], self.SOURCE)
        self.assertEqual(verified['custodyKind'], 'kernel-rc-native')
        self.assertEqual(len(verified['archiveIdentities']), 4)
        self.assertEqual(verified['packageIdentity']['bunBinarySha256'], hashlib.sha256(b'bun').hexdigest())
        self.assertEqual(manifest['version'], self.VERSION)
        self.assertEqual(set(selected), {'bun', 'rust'})

    def test_command_bundle_missing_or_rehashed_raw_poison_is_refused(self):
        report_path = self.root / 'build-report.json'; path = self.root / rehearsal.custody.COMMAND_LOG_EVIDENCE
        original = path.read_bytes(); report = json.loads(report_path.read_bytes())
        del report['evidence'][rehearsal.custody.COMMAND_LOG_EVIDENCE]; report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError): self.verify()
        bundle = json.loads(original); bundle['members'][0]['base64'] = 'eA=='
        path.write_text(json.dumps(bundle)); report['evidence'][rehearsal.custody.COMMAND_LOG_EVIDENCE] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError): self.verify()

    def test_expected_source_and_version_are_mandatory_exact_anchors(self):
        for source, version in [('bad', self.VERSION), ('c' * 40, self.VERSION),
                                (self.SOURCE, '0.15.0'), (self.SOURCE, '0.15.0-rc.4')]:
            with self.subTest(source=source, version=version), self.assertRaises(ValueError):
                self.verify(source, version)

    def test_invalid_expected_version_lexemes_rejected_before_report_comparison(self):
        for version in ('0.015.0-rc.3', '0.15.00-rc.3', '0.15.0-rc.03', '00.15.0-rc.3',
                        '0.15.0-rc.-1', '0.15.0-rc.3+build'):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'explicit expected 0.x RC version'):
                self.verify(version=version)
        # rc.0 is valid SemVer: it reaches the separate exact-candidate check.
        with self.assertRaisesRegex(ValueError, 'report differs from expected source/version'):
            self.verify(version='0.15.0-rc.0')

    def test_mixed_manifest_source_version_and_platform_rejected(self):
        original = copy.deepcopy(self.manifest)
        for field, value in [('source', {'revision': 'c' * 40, 'verification': 'matched-product-doctor'}),
                             ('version', '0.15.0-rc.4'), ('platform', 'linux-x64-gnu')]:
            self.manifest = copy.deepcopy(original)
            self.manifest[field] = value
            self.write()
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify()

    def test_probe_must_bind_final_binary_and_source_even_when_rehashed(self):
        original = copy.deepcopy(self.checks)
        for field, value in [('binarySha256', '0' * 64), ('commit', 'c' * 40), ('version', '0.15.0-rc.4'),
                             ('networkCalls', 1), ('testSeamsEnabled', True)]:
            self.checks = copy.deepcopy(original)
            self.checks['built-bun'][field] = value
            self.write()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Structured native check'): self.verify()

    def test_report_claims_do_not_authorize_publication_or_model_calls(self):
        for field, value in [('publicationAuthorized', True), ('modelCalls', 1), ('kernelFetches', 1),
                             ('qualification', 'kernel-smoke-qualified')]:
            original = self.report[field]
            self.report[field] = value
            self.write()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'native build claims'): self.verify()
            self.report[field] = original

    def test_modified_retained_probe_or_manifest_rejected(self):
        for relative in ('logs/installed-rust.json', 'package/release-manifest.json'):
            path = self.root / relative
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, 'evidence identity'): self.verify()
            path.write_bytes(original)

    def test_modified_archive_rejected(self):
        for name in ('bun.tar.gz', 'npm-meta.tgz', 'npm-platform.tgz'):
            path = self.root / 'package' / name
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(name=name), self.assertRaises(ValueError): self.verify()
            path.write_bytes(original)

    def test_rehashed_different_npm_binary_rejected(self):
        self.archive('npm-platform.tgz', 'npm-platform', 'bun', 'darwin-arm64', 'package/bin/prose', b'other')
        self.write()
        with self.assertRaisesRegex(ValueError, 'npm native binary differs'): self.verify()

    def test_optional_original_compiled_binaries_are_bound(self):
        binaries = self.root / 'binaries'; binaries.mkdir()
        for implementation in ('bun', 'rust'):
            (binaries / ('prose-' + implementation)).write_bytes(implementation.encode())
        self.verify()
        (binaries / 'prose-bun').write_bytes(b'other')
        with self.assertRaisesRegex(ValueError, 'Compiled binary'): self.verify()

    def test_missing_structured_evidence_and_symlink_directories_rejected(self):
        del self.report['evidence']['logs/built-bun.json']
        (self.root / 'build-report.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'Required native evidence'): self.verify()
        self.write()
        (self.root / 'logs').rename(self.root / 'real-logs')
        (self.root / 'logs').symlink_to('real-logs')
        with self.assertRaisesRegex(ValueError, 'directories'): self.verify()

    def test_unsafe_report_path_rejected_before_read(self):
        self.report['evidence']['../outside'] = {'sha256': '0' * 64, 'byteLength': 1}
        (self.root / 'build-report.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'Unsafe evidence path'): self.verify()


class PreviousReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = {'schema': 'openprose.cli-distribution/1', 'version': rehearsal.PREVIOUS_VERSION, 'artifacts': []}
        for implementation in ('bun', 'rust'):
            path = self.root / (implementation + '.tar.gz')
            with tarfile.open(path, 'w:gz') as archive:
                member = tarfile.TarInfo('package/prose'); member.size = len(implementation)
                archive.addfile(member, io.BytesIO(implementation.encode()))
            self.manifest['artifacts'].append({'kind': 'standalone', 'implementation': implementation,
                'platform': 'darwin-arm64', 'name': path.name, 'size': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))
        self.pin = hashlib.sha256((self.root / 'manifest.json').read_bytes()).hexdigest()

    def verify(self, version='0.15.0-rc.4'):
        with patch.object(rehearsal, 'PREVIOUS_MANIFEST_SHA256', self.pin):
            return rehearsal.verify_previous_release(self.root, 'darwin-arm64', version)

    def test_actual_prior_bytes_have_no_sdk_requirement(self):
        previous = self.verify()
        self.assertEqual(previous['version'], '0.15.0-rc.3')
        self.assertEqual(set(previous['archives']), {'bun', 'rust'})
        self.assertEqual(previous['binaryHashes']['bun'], hashlib.sha256(b'bun').hexdigest())

    def test_mutated_manifest_or_archive_is_refused(self):
        for name in ('manifest.json', 'bun.tar.gz'):
            path = self.root / name; original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'identity mismatch'):
                self.verify()
            path.write_bytes(original)

    def test_same_version_downgrade_and_non_rc_candidate_are_refused(self):
        for version in ('0.15.0-rc.3', '0.15.0-rc.2', '0.14.0-rc.9', '0.15.0'):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'newer RC'):
                self.verify(version)

    def test_symlinked_prior_archive_refused_before_mutation(self):
        path = self.root / 'bun.tar.gz'; path.rename(self.root / 'real.tar.gz'); path.symlink_to('real.tar.gz')
        with self.assertRaisesRegex(ValueError, 'regular previous archive'):
            self.verify()


class NativeExerciseTests(unittest.TestCase):
    """Exercise genuine install/upgrade transitions with an inert fake brew."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.prefix = self.root / 'brew'
        (self.prefix / 'bin').mkdir(parents=True)
        self.package = self.root / 'candidate'; self.package.mkdir()
        self.output = self.root / 'receipt'
        self.installed_tap = self.prefix / 'Homebrew/Library/Taps/openprose/homebrew-cli-rehearsal'
        self.active = self.prefix / 'bin/prose'
        self.manifest = {'version': '0.15.0-rc.4', 'platform': 'darwin-arm64',
                         'agentsSdk': {}}
        from test_kernel_rc_evidence import sdk_fixture
        sdk, self.sdk_table = sdk_fixture('darwin-arm64')
        self.manifest['agentsSdk'] = sdk
        self.archives = {i: {'path': i + '.tar.gz', 'sha256': 'a' * 64} for i in ('bun', 'rust')}
        self.verified = {'custodyKind': 'kernel-rc-native', 'manifestSha256': 'b' * 64,
                         'source': 'c' * 40, 'packageIdentity': {
                             i + 'BinarySha256': hashlib.sha256(('new-' + i).encode()).hexdigest() for i in ('bun', 'rust')}}
        self.verified['sdkBuild'] = json.loads(self.sdk_table['files']['agents-sdk-build.json'][0])
        self.previous = {'version': '0.15.0-rc.3', 'root': self.root, 'manifestSha256': 'd' * 64,
                         'archives': self.archives,
                         'binaryHashes': {i: hashlib.sha256(('old-' + i).encode()).hexdigest() for i in ('bun', 'rust')}}
        self.retained_versions = {}; self.retained_before_force = []; self.leave_retained_keg = False
        self.kegs = {}; self.tapped = False; self.calls = []; self.corrupt_settings = False
        self.bad_dry_run = False
        self.runtime_banner = 'prose-agents-sdk 0.1.0'

    def execute(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs['env'])))
        out = ''; code = 0
        if argv[0] == 'brew':
            verb = argv[1]
            if verb == '--prefix':
                out = str(self.prefix if len(argv) == 2 else self.kegs[argv[2].split('-')[-1]][0]) + '\n'
            elif verb == '--repo': out = str(self.installed_tap) + '\n'
            elif verb == 'tap':
                if len(argv) == 2: out = rehearsal.TAP + '\n' if self.tapped else ''
                else:
                    self.tapped = True
                    (self.installed_tap / 'Formula').mkdir(parents=True)
                    for implementation in ('bun', 'rust'):
                        filename = 'prose-' + implementation + '.rb'
                        (self.installed_tap / 'Formula' / filename).write_bytes((self.output / 'tap/Formula' / filename).read_bytes())
            elif verb == 'untap': self.tapped = False
            elif verb == 'list':
                out = ''.join('prose-' + i + ' ' + ' '.join(sorted(self.retained_versions.get(i, {state[1]}))) + '\n' for i, state in self.kegs.items())
            elif verb in {'install', 'upgrade'}:
                implementation = argv[-1].split('-')[-1]
                formula = (self.installed_tap / 'Formula' / ('prose-' + implementation + '.rb')).read_text()
                version = rehearsal.re.search(r'  version "([^"]+)"', formula).group(1)
                if verb == 'install' and version in self.retained_versions.get(implementation, set()):
                    return subprocess.CompletedProcess(argv, 0, 'Already installed, not linked\n', '')
                if verb == 'upgrade':
                    self.assertIn(implementation, self.kegs, 'Upgrade requires an installed previous keg')
                    self.assertEqual(self.kegs[implementation][1], '0.15.0-rc.3', 'Upgrade must replace an actual previous version')
                keg = self.prefix / 'Cellar' / implementation / version / 'bin'; keg.mkdir(parents=True, exist_ok=True)
                binary = keg / 'prose'
                binary.write_bytes((('new-' if version.endswith('.4') else 'old-') + implementation).encode())
                if version.endswith('.4'):
                    import sdk_native_inventory as native
                    view = self.sdk_table
                    files = {name: row for name, row in view['files'].items()
                             if name not in ('agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt')}
                    if (keg / 'prose-agents-sdk-runtime').exists():
                        native.read_macos_payload(keg, self.verified['sdkBuild']['payload'], 'arm64')
                    else:
                        native.materialize_macos_payload(keg, self.verified['sdkBuild']['payload'], files,
                                                         view['directories'], view['symlinks'], 'arm64')
                self.kegs[implementation] = (keg.parent, version)
                self.retained_versions.setdefault(implementation, set()).add(version)
                self.assertFalse(self.active.exists() or self.active.is_symlink(), 'Install/upgrade must vacate shared command')
                self.active.symlink_to(binary)
                for name in ('prose-agents-sdk', 'prose-agents-sdk-runtime'):
                    if (keg / name).exists(): (self.prefix / 'bin' / name).symlink_to(keg / name)
                if self.corrupt_settings and verb == 'upgrade':
                    (Path(kwargs['env']['PROSE_CONFIG_DIR']) / 'cli.toml').write_text('timeout = "1m"\n')
            elif verb == 'unlink':
                for name in ('prose', 'prose-agents-sdk', 'prose-agents-sdk-runtime'):
                    link = self.prefix / 'bin' / name
                    if link.is_symlink(): link.unlink()
            elif verb == 'link':
                binary = self.kegs[argv[-1].split('-')[-1]][0] / 'bin/prose'
                if self.active.is_symlink():
                    if self.active.resolve() != binary.resolve(): out = 'Could not symlink'; code = 1
                else:
                    self.active.symlink_to(binary)
                    for name in ('prose-agents-sdk', 'prose-agents-sdk-runtime'):
                        if (binary.parent / name).exists(): (self.prefix / 'bin' / name).symlink_to(binary.parent / name)
            elif verb == 'uninstall':
                force = '--force' in argv
                for name in [arg for arg in argv[2:] if arg != '--force']:
                    self.assertTrue(name.startswith(rehearsal.TAP + '/prose-'), 'All-version cleanup must stay in the owned tap')
                    implementation = name.split('-')[-1]
                    if implementation in self.kegs:
                        versions = self.retained_versions.get(implementation, {self.kegs[implementation][1]})
                        if force: self.retained_before_force.append((implementation, set(versions)))
                        if self.active.is_symlink() and self.active.resolve().parent.parent == self.kegs[implementation][0].resolve():
                            for name in ('prose', 'prose-agents-sdk', 'prose-agents-sdk-runtime'):
                                link = self.prefix / 'bin' / name
                                if link.is_symlink(): link.unlink()
                        remaining = set() if force else versions - {self.kegs[implementation][1]}
                        if self.leave_retained_keg and force and len(versions) > 1:
                            remaining = {'0.15.0-rc.3'}
                        if remaining:
                            self.retained_versions[implementation] = remaining
                            version = sorted(remaining)[-1]
                            self.kegs[implementation] = (self.prefix / 'Cellar' / implementation / version, version)
                        else:
                            self.retained_versions.pop(implementation, None)
                            del self.kegs[implementation]
            elif verb != 'test': self.fail('Unexpected brew command: ' + str(argv))
        elif argv[0] == 'git': pass  # Inert fake: no Git operation occurs in this test.
        elif Path(argv[0]).name == 'prose-agents-sdk': out = '{}\n'
        elif Path(argv[0]).name == 'prose':
            resolved = Path(argv[0]).resolve(); version = resolved.parent.parent.name; implementation = resolved.parent.parent.parent.name
            if argv[1:] == ['--version']: out = f'prose {version} ({implementation})\n'
            elif 'explain' in argv:
                out = json.dumps({'schema': 'openprose.configuration-explanation/1', 'diagnostics': [], 'values': {
                    k: {'value': v} for k, v in [('harness', 'agents-sdk'), ('model', 'gpt-6.1-sol'), ('authProfile', 'openai-api-key'),
                        ('timeout', '9m' if (Path(kwargs['env']['PROSE_CONFIG_DIR']) / 'cli.toml').exists() else '10m')]}})
            elif '--dry-run' in argv:
                self.assertEqual(kwargs['env'].get('OPENAI_API_KEY'), 'provider-free-installation-canary')
                out = json.dumps({'schema': 'openprose.runner-dry-run-report/1', 'wouldStartModel': self.bad_dry_run,
                    'readiness': 'ready', 'billingOwner': 'user-provider', 'blockingError': None,
                    'selection': {'harness': 'agents-sdk', 'adapterId': 'agents-sdk/jsonl', 'transport': 'jsonl', 'runtimeVersion': self.runtime_banner, 'model': 'gpt-6.1-sol'}})
            else:
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
                out = json.dumps({'schema': 'openprose.runner-error/1', 'code': 'HARNESS_NEEDS_AUTH', 'details': {'fallbackAttempted': False}}); code = 10
        else: self.fail('Unexpected process: ' + str(argv))
        return subprocess.CompletedProcess(argv, code, out, '')

    def exercise(self, previous=True):
        with patch.object(rehearsal.subprocess, 'run', side_effect=self.execute):
            return rehearsal.exercise(self.package, self.manifest, self.archives, self.verified,
                                      self.output, 'brew', previous=self.previous if previous else None)

    def test_both_genuine_upgrades_preserve_selection_settings_and_scrub_credentials(self):
        with patch.dict(rehearsal.os.environ, {'OPENAI_API_KEY': 'ambient-secret', 'HTTPS_PROXY': 'ambient-proxy'}):
            receipt = self.exercise()
        self.assertEqual(receipt['upgradeQualification'], 'passed-genuine-upgrade-both-selections')
        self.assertEqual(receipt['modelCalls'], 0)
        self.assertEqual(len(receipt['sdkPayloadChecks']), 6)
        self.assertTrue(all(row['completePayloadVerified'] for row in receipt['sdkPayloadChecks']))
        self.assertTrue(receipt['uninstallPassed'])
        self.assertEqual((self.output / 'user-settings/cli.toml').read_bytes(), b'# explicit upgrade preservation\ntimeout = "9m"\n')
        names = [c['name'] for c in receipt['checks']]
        for selected in ('bun', 'rust'):
            positions = [names.index(selected + suffix) for suffix in ('-unlink-selected-before-upgrade', '-upgrade-inactive',
                '-unlink-upgraded-inactive', '-upgrade-selected', '-restore-selected-link', '-settings-after-uninstall')]
            self.assertEqual(positions, sorted(positions))
        for argv, env in self.calls:
            self.assertNotIn('HTTPS_PROXY', env)
            if '--dry-run' not in argv: self.assertNotIn('OPENAI_API_KEY', env)
        self.assertFalse(self.active.exists() or self.active.is_symlink())
        self.assertNotEqual(self.installed_tap, self.output / 'tap')

    def test_genuine_upgrade_retains_old_kegs_until_guarded_all_version_uninstall(self):
        receipt = self.exercise()
        retained = [(implementation, versions) for implementation, versions in self.retained_before_force
                    if versions == {'0.15.0-rc.3', '0.15.0-rc.4'}]
        self.assertEqual([implementation for implementation, _ in retained], ['bun', 'rust', 'bun', 'rust'])
        self.assertEqual(self.retained_versions, {})
        names = [check['name'] for check in receipt['checks']]
        self.assertIn('bun-remaining-kegs-after-upgrade', names)
        self.assertIn('rust-remaining-kegs-after-upgrade', names)
        for argv, _ in self.calls:
            if argv[:2] == ['brew', 'uninstall']:
                self.assertIn('--force', argv)
                self.assertTrue(all(arg.startswith(rehearsal.TAP + '/prose-') for arg in argv[2:] if arg != '--force'))

    def test_retained_old_keg_after_force_fails_before_second_round(self):
        self.leave_retained_keg = True
        with self.assertRaisesRegex(ValueError, 'kegs survived all-version uninstall'):
            self.exercise()
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())
        # The second round must not disguise retained RC3 as a fresh installation.
        labels = [p.name for p in self.output.glob('rust-install-base-*.log')]
        self.assertEqual(labels, [])

    def test_lingering_helper_after_all_version_uninstall_is_refused(self):
        original_execute = self.execute
        def leave_helper(argv, **kwargs):
            had_old_and_new = any(len(versions) > 1 for versions in self.retained_versions.values())
            result = original_execute(argv, **kwargs)
            if argv[:2] == ['brew', 'uninstall'] and '--force' in argv and had_old_and_new:
                (self.prefix / 'bin/prose-agents-sdk').write_bytes(b'lingering-helper')
            return result
        with patch.object(self, 'execute', side_effect=leave_helper):
            with self.assertRaisesRegex(ValueError, 'commands survived all-version uninstall'):
                self.exercise()
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())

    def test_ordinary_uninstall_retains_old_unlinked_keg_and_reinstall_does_not_link(self):
        self.exercise()
        for version in ('0.15.0-rc.3', '0.15.0-rc.4'):
            (self.prefix / 'Cellar/bun' / version / 'bin').mkdir(parents=True, exist_ok=True)
        self.retained_versions['bun'] = {'0.15.0-rc.3', '0.15.0-rc.4'}
        self.kegs['bun'] = (self.prefix / 'Cellar/bun/0.15.0-rc.4', '0.15.0-rc.4')
        env = {'PROSE_CONFIG_DIR': str(self.output / 'user-settings')}
        self.execute(['brew', 'uninstall', rehearsal.TAP + '/prose-bun'], env=env)
        self.assertEqual(self.retained_versions['bun'], {'0.15.0-rc.3'})
        formula = self.installed_tap / 'Formula/prose-bun.rb'
        formula.write_text(formula.read_text().replace('0.15.0-rc.4', '0.15.0-rc.3'))
        result = self.execute(['brew', 'install', '--build-from-source', rehearsal.TAP + '/prose-bun'], env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn('Already installed, not linked', result.stdout)
        self.assertFalse(self.active.exists() or self.active.is_symlink())

    def test_changed_settings_fail_instead_of_claiming_upgrade_success(self):
        self.corrupt_settings = True
        with self.assertRaisesRegex(ValueError, 'changed explicit user settings'):
            self.exercise()
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())

    def test_model_starting_dry_run_is_refused(self):
        self.bad_dry_run = True
        with self.assertRaisesRegex(ValueError, 'provider-free readiness'):
            self.exercise()

    def assert_runtime_banner_refused(self, banner):
        self.runtime_banner = banner
        with self.assertRaisesRegex(ValueError, 'discover the packaged SDK default'):
            self.exercise()
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())

    def test_complete_ready_selection_rejects_bare_runtime_version(self):
        self.assert_runtime_banner_refused('0.1.0')

    def test_complete_ready_selection_rejects_wrong_runtime_version(self):
        self.assert_runtime_banner_refused('prose-agents-sdk 9.9.9')

    def test_complete_ready_selection_rejects_wrong_helper_identity(self):
        self.assert_runtime_banner_refused('other-helper 0.1.0')

    def test_existing_command_is_not_replaced(self):
        self.active.write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, 'overwrite an existing prose command'):
            self.exercise()
        self.assertEqual(self.active.read_bytes(), b'existing')
        self.assertFalse(any(argv[:2] == ['brew', 'install'] for argv, _ in self.calls))

    def test_existing_tap_is_not_replaced(self):
        self.tapped = True
        with self.assertRaisesRegex(ValueError, 'replace an existing tap'):
            self.exercise()
        self.assertFalse(any(argv[:2] == ['brew', 'install'] for argv, _ in self.calls))

    def test_existing_helper_command_is_not_replaced(self):
        helper = self.prefix / 'bin/prose-agents-sdk'; helper.write_bytes(b'existing-helper')
        with self.assertRaisesRegex(ValueError, 'overwrite an existing SDK helper'):
            self.exercise()
        self.assertEqual(helper.read_bytes(), b'existing-helper')
        self.assertFalse(any(argv[:2] == ['brew', 'install'] for argv, _ in self.calls))

    def test_existing_fully_qualified_prose_keg_is_not_replaced(self):
        self.kegs['bun'] = (self.root, '0.15.0-rc.3')
        with self.assertRaisesRegex(ValueError, 'fresh Homebrew installation'):
            self.exercise()
        self.assertFalse(any(argv[:2] == ['brew', 'install'] for argv, _ in self.calls))

    def test_fresh_only_receipt_does_not_claim_genuine_upgrade(self):
        receipt = self.exercise(previous=False)
        self.assertEqual(receipt['upgradeQualification'], 'not-requested-fresh-install-only')
        self.assertFalse(any(argv[:2] == ['brew', 'upgrade'] for argv, _ in self.calls))

    def test_existing_support_directory_is_not_replaced(self):
        support = self.prefix / 'bin/prose-agents-sdk-runtime'; support.mkdir()
        marker = support / 'caller.txt'; marker.write_bytes(b'caller-owned')
        with self.assertRaisesRegex(ValueError, 'overwrite an existing SDK support directory'):
            self.exercise()
        self.assertEqual(marker.read_bytes(), b'caller-owned')
        self.assertFalse(any(argv[:2] == ['brew', 'install'] for argv, _ in self.calls))

    def test_missing_support_file_fails_before_helper_probe(self):
        original = self.execute
        def remove_support(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ['brew', 'install']:
                helper = self.active.resolve().parent
                name = next(name for name in self.sdk_table['files'] if name.startswith('prose-agents-sdk-runtime/'))
                (helper / name).unlink()
            return result
        with patch.object(self, 'execute', side_effect=remove_support), self.assertRaises(ValueError):
            self.exercise(previous=False)
        self.assertFalse(any(Path(argv[0]).name == 'prose-agents-sdk' for argv, _ in self.calls))
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())

    def test_support_mutation_during_probe_is_refused(self):
        original = self.execute
        def mutate_support(argv, **kwargs):
            result = original(argv, **kwargs)
            if '--dry-run' in argv:
                helper = self.active.resolve().parent
                name = next(name for name in self.sdk_table['files'] if name.startswith('prose-agents-sdk-runtime/'))
                (helper / name).write_bytes(b'mutated')
            return result
        with patch.object(self, 'execute', side_effect=mutate_support), self.assertRaises(ValueError):
            self.exercise(previous=False)
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())

    def test_lingering_support_after_all_version_uninstall_is_refused(self):
        original = self.execute
        def leave_support(argv, **kwargs):
            both = any(len(versions) > 1 for versions in self.retained_versions.values())
            result = original(argv, **kwargs)
            if argv[:2] == ['brew', 'uninstall'] and '--force' in argv and both:
                (self.prefix / 'bin/prose-agents-sdk-runtime').mkdir(exist_ok=True)
            return result
        with patch.object(self, 'execute', side_effect=leave_support), self.assertRaisesRegex(ValueError, 'commands survived all-version uninstall'):
            self.exercise()
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())

    def test_rejected_link_must_preserve_helper_link_identity(self):
        original = self.execute
        def alter_link(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ['brew', 'link'] and result.returncode == 1:
                link = self.prefix / 'bin/prose-agents-sdk'
                link.unlink()
                link.symlink_to(self.kegs['rust'][0] / 'bin/prose-agents-sdk')
            return result
        with patch.object(self, 'execute', side_effect=alter_link), self.assertRaisesRegex(ValueError, 'SDK helper linkage'):
            self.exercise(previous=False)
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())

    def test_rejected_link_must_preserve_complete_selected_payload(self):
        original = self.execute
        def alter_tree(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ['brew', 'link'] and result.returncode == 1:
                name = next(name for name in self.sdk_table['files'] if name.startswith('prose-agents-sdk-runtime/'))
                (self.active.resolve().parent / name).write_bytes(b'changed-by-failed-link')
            return result
        with patch.object(self, 'execute', side_effect=alter_tree), self.assertRaises(ValueError):
            self.exercise(previous=False)
        self.assertFalse((self.output / 'homebrew-rehearsal.json').exists())
