import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import build_kernel_rc as rc


class KernelRCBuildTests(unittest.TestCase):
    def archive(self, path, members):
        with tarfile.open(path, 'w:gz') as archive:
            for name, kind in members:
                item = tarfile.TarInfo(name); item.mode = 0o755
                if kind == 'link':
                    item.type = tarfile.SYMTYPE; item.linkname = '/tmp/escape'
                    archive.addfile(item)
                else:
                    data = b'#!/bin/sh\nexit 0\n'; item.size = len(data)
                    archive.addfile(item, io.BytesIO(data))

    def test_extraction_refuses_traversal_links_and_duplicates_without_writes(self):
        for members in ([('../prose', 'file')], [('bin/prose', 'link')],
                        [('one/prose', 'file'), ('two/prose', 'file')],
                        [('bin/prose', 'file'), ('bin/prose', 'file')]):
            with self.subTest(members=members), tempfile.TemporaryDirectory() as d:
                root = Path(d); archive = root / 'source.tgz'; self.archive(archive, members)
                with self.assertRaises(ValueError):
                    rc.extract_binary(archive, root / 'install/prose')
                self.assertFalse((root / 'install').exists())

    def test_regular_archive_extracts_only_binary(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); archive = root / 'source.tgz'
            self.archive(archive, [('release/prose', 'file'), ('release/README', 'file')])
            executable = rc.extract_binary(archive, root / 'install/prose')
            self.assertTrue(os.access(executable, os.X_OK))
            self.assertEqual(list(executable.parent.iterdir()), [executable])

    def test_credentials_and_image_overrides_are_not_inherited(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = rc.environment(root, {'PATH': '/bin', 'HOME': '/original',
                                      'OPENAI_API_KEY': 'not-a-real-key',
                                      'NODE_AUTH_TOKEN': 'not-a-real-token',
                                      'OPENPROSE_IMAGE_SOURCE_DIR': '/sentinel',
                                      'BUN_OPTIONS': '--preload=untrusted', 'RUSTFLAGS': 'untrusted'})
            self.assertFalse(set(env) & {'OPENAI_API_KEY', 'NODE_AUTH_TOKEN', 'OPENPROSE_IMAGE_SOURCE_DIR', 'BUN_OPTIONS', 'RUSTFLAGS'})
            self.assertEqual(env['HOME'], str(root / 'home'))
            self.assertEqual(env['CARGO_NET_OFFLINE'], 'true')
            self.assertEqual(env['npm_config_offline'], 'true')

    def test_non_rc_version_fails_before_git_or_output(self):
        for version in ('1.0.0', '1.0.0-rc.01', '1.0.0-rc.1;echo bad', '01.0.0-rc.1'):
            with tempfile.TemporaryDirectory() as d, patch.object(rc.subprocess, 'check_output') as git:
                output = Path(d) / 'out'
                with self.assertRaisesRegex(ValueError, 'exact'):
                    rc.build(version, output)
                git.assert_not_called(); self.assertFalse(output.exists())

    def test_dirty_checkout_fails_before_build_or_output(self):
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / 'out'
            with patch.object(rc.subprocess, 'check_output', side_effect=['a' * 40, b' M source.py']), patch.object(rc, 'command') as command:
                with self.assertRaisesRegex(ValueError, 'clean'):
                    rc.build('0.15.0-rc.1', output)
                command.assert_not_called()
                self.assertFalse(output.exists())

    def test_existing_output_is_never_reused(self):
        with tempfile.TemporaryDirectory() as d, patch.object(rc.subprocess, 'check_output') as git:
            output = Path(d)
            marker = output / 'previous-result'; marker.write_text('preserved')
            with self.assertRaisesRegex(ValueError, 'fresh'):
                rc.build('0.15.0-rc.1', output)
            git.assert_not_called()
            self.assertEqual(marker.read_text(), 'preserved')

    def test_tool_symlink_resolves_to_direct_executable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); actual = root / 'target-readelf'; actual.write_text('#!/bin/sh\nexit 0\n'); actual.chmod(0o755)
            alias = root / 'readelf'; alias.symlink_to(actual.name)
            self.assertEqual(rc.executable_tool('readelf', {'PATH': d}), actual.resolve())

    def test_macos_ad_hoc_signature_is_verified_before_packaging(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); binary = root / 'prose-rust'
            with patch.object(rc, 'command') as command:
                rc.prepare_macos_binary(binary, {}, root)
                self.assertEqual(command.call_args_list[0].args[0], ['/usr/bin/codesign', '--force', '--sign', '-', binary])
                self.assertEqual(command.call_args_list[1].args[0], ['/usr/bin/codesign', '--verify', '--deep', '--strict', binary])
            with patch.object(rc, 'command', side_effect=ValueError('sign failed')) as command:
                with self.assertRaisesRegex(ValueError, 'sign failed'):
                    rc.prepare_macos_binary(binary, {}, root)
                self.assertEqual(command.call_count, 1)

    def test_tampered_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); artifacts = []
            for index, (kind, impl) in enumerate([('npm-meta', 'bun'), ('npm-platform', 'bun'), ('standalone-archive', 'bun'), ('standalone-archive', 'rust')]):
                path = root / str(index); path.write_bytes(b'original')
                artifacts.append({'path': path.name, 'kind': kind, 'implementation': impl,
                                  'byteLength': path.stat().st_size, 'sha256': rc.digest(path)})
            (root / 'release-manifest.json').write_text(json.dumps({'mode': 'kernel-rc', 'artifacts': artifacts}))
            rc.verified_artifacts(root)
            (root / '0').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'bytes changed'):
                rc.verified_artifacts(root)


if __name__ == '__main__':
    unittest.main()

class KernelRCSdkExtractionTests(unittest.TestCase):
    archive = KernelRCBuildTests.archive
    def test_production_extraction_requires_sdk_siblings_before_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); archive = root / 'source.tgz'
            self.archive(archive, [('release/prose', 'file')])
            with self.assertRaisesRegex(ValueError, 'SDK'):
                rc.extract_binary(archive, root / 'install/prose', require_sdk=True)
            self.assertFalse((root / 'install').exists())

    def test_production_extracts_closed_sdk_payload(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); archive = root / 'source.tgz'
            names = ('prose', 'prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt')
            self.archive(archive, [('release/' + name, 'file') for name in names])
            executable = rc.extract_binary(archive, root / 'install/prose', require_sdk=True)
            self.assertEqual({p.name for p in executable.parent.iterdir()}, set(names))
            self.assertTrue(os.access(executable.parent / 'prose-agents-sdk', os.X_OK))
