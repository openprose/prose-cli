"""Archive admission for the non-publishing native Homebrew rehearsal."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
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
        self.assertIn((self.package / 'bun.tar.gz').as_uri(), rendered)
        self.assertNotIn('pkg.prose.md', rendered)

    def test_sdk_archive_identity_is_required_before_homebrew_mutation(self):
        sdk = [('prose-agents-sdk', b'helper', 'sha256'), ('agents-sdk-build.json', b'{}', 'receiptSha256'),
               ('AGENTS-SDK-NOTICES.txt', b'notices', 'noticesSha256')]
        self.manifest['agentsSdk'] = {key: hashlib.sha256(data).hexdigest() for _, data, key in sdk}
        with self.assertRaisesRegex(ValueError, 'SDK'):
            self.select()
        for item in self.manifest['artifacts']:
            path = self.package / item['path']
            with tarfile.open(path, 'w:gz') as archive:
                for name, data in [('prose', item['implementation'].encode()), *[(n, d) for n, d, _ in sdk]]:
                    member = tarfile.TarInfo('package/' + name); member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            item.update(byteLength=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.select()
        self.manifest['agentsSdk']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SDK member differs'):
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
