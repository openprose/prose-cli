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
        self.assertIn((self.package / 'bun.tar.gz').as_uri(), rendered)
        self.assertNotIn('pkg.prose.md', rendered)

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
