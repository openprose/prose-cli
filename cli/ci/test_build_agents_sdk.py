import importlib.util
import os
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import build_agents_sdk as sdk


class BuildSdkTests(unittest.TestCase):
    def test_generated_exact_version_probe_avoids_all_sdk_and_runtime_imports(self):
        # Execute the bytes actually bundled by the builder, with heavy imports fatal.
        wrapper = """import builtins, sys
original_import = builtins.__import__
def lightweight_import(name, *args, **kwargs):
    if name != 'sys':
        raise RuntimeError('Unexpected version-probe import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = lightweight_import
sys.argv = ['prose-agents-sdk', '--version']
""" + "exec(compile(" + repr(sdk.ENTRY_SOURCE) + ", 'sdk-entry.py', 'exec'))\n"
        result = subprocess.run([sys.executable, '-I', '-c', wrapper],
                                capture_output=True, timeout=5,
                                env={'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'prose-agents-sdk 0.1.0\n')
        self.assertEqual(result.stderr, b'')

    def test_generated_nonversion_and_mixed_argv_keep_runtime_dispatch(self):
        for arguments in ([], ['--cwd', '/work', '--prompt', 'task'],
                          ['--version', '--model', 'chosen'], ['--version', '--version']):
            with self.subTest(arguments=arguments):
                wrapper = """import json, sys, types
runtime_dispatch = types.ModuleType('runpy')
def run_module(name, run_name):
    print(json.dumps({'module': name, 'runName': run_name, 'argv': sys.argv[1:]}))
runtime_dispatch.run_module = run_module
sys.modules['runpy'] = runtime_dispatch
""" + 'sys.argv = ' + repr(['prose-agents-sdk', *arguments]) + '\n'
                wrapper += "exec(compile(" + repr(sdk.ENTRY_SOURCE) + ", 'sdk-entry.py', 'exec'))\n"
                result = subprocess.run([sys.executable, '-I', '-c', wrapper],
                                        capture_output=True, timeout=5,
                                        env={'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1'})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 {'module': 'prose_sdk_runtime', 'runName': '__main__', 'argv': arguments})
                self.assertEqual(result.stderr, b'')

    def test_generated_packaged_tool_selftest_keeps_precedence_for_mixed_version_argv(self):
        for arguments in (['--packaged-tool-self-test'], ['--version', '--packaged-tool-self-test']):
            with self.subTest(arguments=arguments):
                wrapper = """import sys, types
selftest = types.ModuleType('sdk_tool_selftest')
selftest.main = lambda: print('tool-self-test-dispatched')
sys.modules['sdk_tool_selftest'] = selftest
""" + 'sys.argv = ' + repr(['prose-agents-sdk', *arguments]) + '\n'
                wrapper += "exec(compile(" + repr(sdk.ENTRY_SOURCE) + ", 'sdk-entry.py', 'exec'))\n"
                result = subprocess.run([sys.executable, '-I', '-c', wrapper],
                                        capture_output=True, timeout=5,
                                        env={'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1'})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b'tool-self-test-dispatched\n')
                self.assertEqual(result.stderr, b'')

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

    def test_build_environment_owns_fresh_cache_even_when_freezer_never_uses_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ambient_cache = root / 'ambient-cache'; ambient_cache.mkdir()
            marker = ambient_cache / 'preserve'; marker.write_bytes(b'ambient')
            for name in ('first', 'second'):
                output = root / name; output.mkdir()
                env = sdk.environment(output, {'PATH': '/usr/bin:/bin',
                    'PYINSTALLER_CONFIG_DIR': str(ambient_cache), 'OPENAI_API_KEY': 'fixture-secret'})
                cache = output / 'pyinstaller-cache'
                self.assertEqual(str(cache), env['PYINSTALLER_CONFIG_DIR'])
                self.assertTrue(cache.is_dir())
                self.assertFalse(cache.is_symlink())
                self.assertEqual([], list(cache.iterdir()))
                self.assertNotIn('OPENAI_API_KEY', env)
                # The production cleanup remains strict even if PyInstaller wrote nothing.
                sdk.shutil.rmtree(cache)
                self.assertFalse(cache.exists())
                with self.assertRaises(FileNotFoundError):
                    sdk.shutil.rmtree(cache)
            self.assertEqual(b'ambient', marker.read_bytes())

    def test_build_environment_refuses_preexisting_cache_directory_or_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            foreign = root / 'foreign'; foreign.mkdir()
            marker = foreign / 'preserve'; marker.write_bytes(b'foreign')
            for kind in ('directory', 'symlink'):
                output = root / kind; output.mkdir()
                cache = output / 'pyinstaller-cache'
                if kind == 'directory': cache.mkdir()
                else: cache.symlink_to(foreign, target_is_directory=True)
                with self.subTest(kind=kind), self.assertRaises(FileExistsError):
                    sdk.environment(output, {'PATH': '/usr/bin:/bin'})
            self.assertEqual(b'foreign', marker.read_bytes())

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

    def test_linux_hooks_are_paired_platform_scoped_and_required_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            for host, supplier, origin, reason in (('darwin', Path('/fixture'), Path('/origin'), 'Linux-only'),
                    ('linux', None, None, 'requires explicit'), ('linux', Path('/fixture'), None, 'supplied together')):
                with self.subTest(host=host, reason=reason), mock.patch.object(sdk.sys, 'platform', host), \
                        mock.patch.object(sdk.platform, 'python_version', return_value='3.10.20'), \
                        mock.patch.object(sdk.platform, 'machine', return_value='x86_64'):
                    output = Path(temp) / 'out'
                    with self.assertRaisesRegex(ValueError, reason):
                        sdk.build(output, linux_libgcc=supplier, linux_native_origin=origin)
                    self.assertFalse(output.exists())

    def test_linux_build_binds_toc_archive_extracted_bytes_and_source_snapshot(self):
        from test_sdk_native_inventory import input_fixture
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value, origin_path, supplier, source = input_fixture(root)
            for relative in sdk.native.SOURCE_PATHS:
                (source / relative).write_bytes((sdk.ROOT / relative).read_bytes())
            value['sourceSnapshot']['sources'] = {name: sdk.digest(source / name) for name in sdk.native.SOURCE_PATHS}
            origin_path.write_text(json.dumps(value))
            rows = [sdk.native.elf_record('libgcc_s.so.1', supplier.read_bytes())]
            report = {'schema': 'openprose.sdk-packaged-libraries/1', 'elfCount': 1,
                      'requiredGlibcMaximum': '2.17', 'libraries': rows, 'modelCalls': 0}
            executed = []
            def simulated_run(command, *, env, cwd, log, timeout=900):
                executed.append((command, timeout))
                self.assertNotIn('OPENAI_API_KEY', env)
                if '-m' in command:
                    self.assertIn('--add-binary', command)
                    index = command.index('--add-binary')
                    self.assertEqual(command[index + 1], str(supplier) + sdk.os.pathsep + '.')
                    self.assertEqual((cwd / 'sdk_native_inventory.py').read_bytes(), Path(sdk.native.__file__).read_bytes())
                    (cwd / 'dist').mkdir(); (cwd / 'dist' / sdk.NAME).write_bytes(b'nonexecuted fixture helper')
                    work = cwd / 'work' / sdk.NAME; work.mkdir(parents=True)
                    (work / 'Analysis-00.toc').write_text(repr(([], [('libgcc_s.so.1', str(supplier), 'BINARY')])))
                    log.write_text('simulated freeze')
                elif '--packaged-self-test' in command:
                    log.write_text(json.dumps({'schema':'openprose.sdk-packaged-self-test/1','openaiAgents':'0.22.2','openai':'3.13.0','certificates':True,'modelCalls':0}))
                elif '--packaged-tool-self-test' in command:
                    log.write_text(json.dumps({'schema':'openprose.sdk-packaged-tools-self-test/1','shellEffects':True,'boundedOutput':True,'shellCancellation':True,'mockedPublicRetrieval':True,'incompleteHttpRejected':True,'modelCalls':0,'networkUsed':False}))
                elif '--version' in command: log.write_text('prose-agents-sdk 0.1.0\n')
                elif '--packaged-library-test' in command: log.write_text(json.dumps(report))
                else: self.fail('Unexpected simulated command')
            with mock.patch.object(sdk.sys, 'platform', 'linux'), \
                    mock.patch.object(sdk.platform, 'machine', return_value='x86_64'), \
                    mock.patch.object(sdk.platform, 'python_version', return_value='3.10.20'), \
                    mock.patch.object(sdk, 'ROOT', source), mock.patch.object(sdk, 'SOURCE', source / 'harnesses/agents-sdk/run.py'), \
                    mock.patch.object(sdk, 'LOCK', source / 'harnesses/agents-sdk/requirements-build.txt'), \
                    mock.patch.object(sdk, 'lock_packages', return_value=[]), \
                    mock.patch.object(sdk, 'installed_inventory', return_value=b'fixture notices'), \
                    mock.patch.object(sdk.metadata, 'version', return_value='6.22.3'), \
                    mock.patch.object(sdk, 'inspect_archive', return_value=rows), \
                    mock.patch.object(sdk, 'run', side_effect=simulated_run), \
                    mock.patch.object(sdk.shutil, 'disk_usage', return_value=mock.Mock(free=1024**3)):
                output = root / 'out'
                receipt = sdk.build(output, linux_libgcc=supplier, linux_native_origin=origin_path)
                self.assertEqual(receipt['linuxBuildInputSha256'], sdk.digest(origin_path))
                self.assertEqual(receipt['linuxBuildSourceSnapshot'], value['sourceSnapshot'])
                self.assertTrue(receipt['nativeDependencies']['symbolClosureVerified'])
                self.assertEqual(receipt['nativeDependencies']['libraries'][0]['origin'], 'supplier')
                self.assertIn(b'GCC RUNTIME LIBRARY EXCEPTION', (output / sdk.NOTICES).read_bytes())
                self.assertIn(b'gcc-8.5.0-fixture.src.rpm', (output / sdk.NOTICES).read_bytes())
                self.assertFalse((output / 'work').exists())
                self.assertEqual([timeout for command, timeout in executed if '--version' in command], [5])
                report['libraries'] = []
                with self.assertRaisesRegex(ValueError, 'membership differs'):
                    sdk.build(root / 'mismatch', linux_libgcc=supplier, linux_native_origin=origin_path)
                self.assertFalse((root / 'mismatch' / sdk.RECEIPT).exists())
