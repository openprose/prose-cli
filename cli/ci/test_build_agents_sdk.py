import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import build_agents_sdk as sdk


class BuildSdkTests(unittest.TestCase):
    def test_lock_is_closed_and_contains_all_runtime_and_build_requirements(self):
        packages = sdk.lock_packages()
        self.assertEqual(len(packages), 46)
        versions = {p['name']: p['version'] for p in packages}
        self.assertEqual(versions['openai-agents'], '0.22.2')
        self.assertEqual(versions['openai'], '3.13.0')
        self.assertEqual(versions['pyinstaller'], '6.22.3')
        self.assertEqual(versions['cryptography'], '48.0.1')
        self.assertTrue(all(p['wheelSha256'] for p in packages))

    def test_unpinned_hashless_duplicate_and_extra_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'lock'
            for text in ('openai>=3\n', 'openai==3.13.0\n', '--index-url https://example.invalid\n',
                         'openai==3.13.0 --hash=sha256:' + 'a' * 64 + '\n' + 'openai==3.13.0 --hash=sha256:' + 'b' * 64):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    sdk.lock_packages(path)

    def test_build_environment_excludes_credentials_configuration_and_runtime_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            env = sdk.environment(Path(temp), {'PATH': '/usr/bin:/bin', 'OPENAI_API_KEY': 'fixture-secret',
                                  'PYTHONPATH': '/untrusted', 'LD_LIBRARY_PATH': '/untrusted', 'HOME': '/real-home'})
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('LD_LIBRARY_PATH', env)
            self.assertEqual(env['HOME'], str(Path(temp) / 'home'))

    def test_release_builder_rejects_wrong_python_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'out'
            with mock.patch.object(sdk.platform, 'python_version', return_value='3.11.0'):
                with self.assertRaisesRegex(ValueError, 'Python 3.10.20'):
                    sdk.build(path)
            self.assertFalse(path.exists())

    def test_installed_dependency_mismatch_prevents_freezing(self):
        with mock.patch.object(sdk.metadata, 'distribution', return_value=mock.Mock(version='0.0.0')):
            with self.assertRaisesRegex(ValueError, 'differs from lock'):
                sdk.installed_inventory([{'name': 'openai', 'version': '3.13.0'}])

    def test_sdk_signing_rejects_non_developer_id_before_freezing(self):
        with tempfile.TemporaryDirectory() as temp:
            for identity in ('-', 'Apple Development: example', 'Developer ID Application: bad\n(ABCDE12345)'):
                with self.assertRaisesRegex(ValueError, 'Developer ID'):
                    sdk.build(Path(temp) / 'out', codesign_identity=identity)
                self.assertFalse((Path(temp) / 'out').exists())
