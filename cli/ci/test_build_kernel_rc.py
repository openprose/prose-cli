import io
import copy
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import build_kernel_rc as rc
import npm_alias_install


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



class NpmAliasSdkArchiveTests(unittest.TestCase):
    def fixture(self, path):
        from test_qualify_installed_sdk import sdk_archive, archive
        manifest, table = sdk_archive(path, prefix='package/bin/')
        encoded = table['files']['package/bin/agents-sdk-build.json'][0]
        receipt = json.loads(encoded)
        for index in range(140):
            name = 'prose-agents-sdk-runtime/resource-' + str(index).zfill(3)
            data = b'nonexecuted support fixture'
            table['files']['package/bin/' + name] = (data, 0o644)
            receipt['payload']['entries'].append({'path': name, 'type': 'file', 'mode': 0o644,
                'byteLength': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            receipt['payload']['totalRegularBytes'] += len(data)
        receipt['payload']['entries'].sort(key=lambda row: row['path'])
        encoded = json.dumps(receipt, sort_keys=True).encode()
        table['files']['package/bin/agents-sdk-build.json'] = (encoded, 0o644)
        manifest['agentsSdk']['receiptSha256'] = hashlib.sha256(encoded).hexdigest()
        metadata = {'name': '@openprose/prose-cli', 'version': '0.15.0-rc.4', 'os': ['darwin'], 'cpu': ['x64']}
        table['files']['package/package.json'] = (json.dumps(metadata).encode(), 0o644)
        archive(path, table['files'], table)
        manifest['artifacts'] = [{'path': path.name, 'kind': 'npm-platform',
            'byteLength': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        return manifest, table, metadata

    def test_explicit_sdk_authority_accepts_complete_tree_above_generic_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'sdk-platform.tgz'; manifest, table, metadata = self.fixture(path)
            self.assertGreater(sum(len(rows) for rows in table.values()), 128)
            observed, payload = npm_alias_install.platform_payload(path, sdk_manifest=manifest)
            self.assertEqual(observed, metadata)
            self.assertEqual(payload, path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'Too many archive members'):
                npm_alias_install.platform_payload(path)

    def test_rehashed_support_or_artifact_authority_poison_fails_before_server_or_commands(self):
        from test_qualify_installed_sdk import archive
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); path = root / 'sdk-platform.tgz'
            for poison in ('filename', 'digest', 'length', 'bool-length', 'duplicate-artifact', 'missing-identity', 'support'):
                manifest, table, _ = self.fixture(path)
                if poison == 'filename': manifest['artifacts'][0]['path'] = 'other.tgz'
                elif poison == 'digest': manifest['artifacts'][0]['sha256'] = '0' * 64
                elif poison == 'length': manifest['artifacts'][0]['byteLength'] += 1
                elif poison == 'bool-length': manifest['artifacts'][0]['byteLength'] = True
                elif poison == 'duplicate-artifact': manifest['artifacts'].append(copy.deepcopy(manifest['artifacts'][0]))
                elif poison == 'missing-identity': manifest.pop('agentsSdk')
                else:
                    table['files']['package/bin/prose-agents-sdk-runtime/resource-000'] = (b'poison', 0o644)
                    archive(path, table['files'], table)
                    manifest['artifacts'][0].update(byteLength=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                with self.subTest(poison=poison), patch.object(npm_alias_install, 'ThreadingHTTPServer') as server:
                    command = unittest.mock.Mock(side_effect=AssertionError('No npm call'))
                    with self.assertRaises(ValueError):
                        npm_alias_install.install(root / 'meta.tgz', path, root / 'prefix', env={}, cwd=root,
                                                  command=command, log=root / 'npm.log', sdk_manifest=manifest)
                    server.assert_not_called(); command.assert_not_called()
                    self.assertFalse((root / 'prefix').exists())

    def test_generic_prior_archive_retains_regular_only_policy(self):
        from test_qualify_installed_sdk import archive
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'prior.tgz'
            metadata = {'name': '@openprose/prose-cli', 'version': '0.15.0-rc.3'}
            files = {'package/package.json': (json.dumps(metadata).encode(), 0o644)}
            archive(path, files)
            self.assertEqual(npm_alias_install.platform_payload(path, sdk_manifest=None)[0], metadata)
            archive(path, files, {'symlinks': {'package/alias': 'package.json'}})
            with self.assertRaisesRegex(ValueError, 'Links and special members'):
                npm_alias_install.platform_payload(path, sdk_manifest=None)


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
            from test_qualify_installed_sdk import sdk_archive
            manifest, _ = sdk_archive(archive, prefix='release/')
            names = ('prose', 'prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt', 'prose-agents-sdk-runtime')
            executable = rc.extract_binary(archive, root.resolve() / 'install/prose', require_sdk=True, manifest=manifest)
            self.assertEqual({p.name for p in executable.parent.iterdir()}, set(names))
            self.assertTrue(os.access(executable.parent / 'prose-agents-sdk', os.X_OK))

    def test_mutated_support_is_rejected_before_installation(self):
        from test_qualify_installed_sdk import sdk_archive, archive as write_archive
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); path = root / 'source.tgz'
            manifest, table = sdk_archive(path, prefix='release/')
            table['files']['release/prose-agents-sdk-runtime/empty-data'] = (b'poison', 0o644)
            write_archive(path, table['files'], table)
            with self.assertRaises(ValueError):
                rc.extract_binary(path, root / 'install/prose', require_sdk=True, manifest=manifest)
            self.assertFalse((root / 'install').exists())


class CompleteSdkProducerEvidenceTests(unittest.TestCase):
    def test_support_resource_names_cannot_select_cli_or_receipt(self):
        from test_qualify_installed_sdk import sdk_archive_with_resource_names
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); path = root / 'source.tgz'
            manifest, _ = sdk_archive_with_resource_names(path)
            rc.extract_binary(path, root / 'installed/prose', require_sdk=True, manifest=manifest)
            self.assertEqual((root / 'installed/prose').read_bytes(), b'cli')
            self.assertEqual((root / 'installed/prose-agents-sdk-runtime/agents-sdk-build.json').read_bytes(), b'support resource agents-sdk-build.json')

    def test_producer_bundle_matches_shared_custody_and_retains_actual_mocked_outputs(self):
        import hashlib
        import kernel_rc_evidence as custody
        from test_kernel_rc_evidence import sdk_fixture
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); sdk_dir = root / 'agents-sdk'; sdk_dir.mkdir()
            sdk, scoped = sdk_fixture('darwin-x64')
            receipt = json.loads(scoped['files']['agents-sdk-build.json'][0]); payload = receipt['payload']
            collect = b'nonexecuted producer declaration fixture'
            payload['collectTocSha256'] = hashlib.sha256(collect).hexdigest()
            (sdk_dir / 'collect.toc').write_bytes(collect)
            encoded = json.dumps(receipt, sort_keys=True).encode(); scoped['files']['agents-sdk-build.json'] = (encoded, 0o644)
            sdk['receiptSha256'] = hashlib.sha256(encoded).hexdigest(); (sdk_dir / 'agents-sdk-build.json').write_bytes(encoded)
            checks = [('codesign-' + str(i).zfill(4) + '.log', path, 'strict') for i, path in enumerate(payload['codeSignaturePaths'])]
            checks.append(('codesign.log', 'prose-agents-sdk', 'strict-deep'))
            for name, target, verification in checks:
                data = b'fixture diagnostic'; raw_name = name + '.stderr'; (sdk_dir / raw_name).write_bytes(data)
                proof = {'schema': 'openprose.sdk-code-signature-check/1', 'phase': 'code-signature', 'path': target,
                    'verification': verification, 'exitCode': 0, 'timeoutSeconds': 30, 'success': True,
                    'outputComplete': True, 'timedOut': False, 'outputLimitExceeded': False, 'stdoutBytes': 0, 'stderrBytes': len(data),
                    'rawOutput': {'stderr': {'path': raw_name, 'sha256': hashlib.sha256(data).hexdigest(), 'byteLength': len(data)}}}
                (sdk_dir / name).write_text(json.dumps(proof))
            evidence = rc.sdk_producer_evidence(root, sdk_dir)
            table = {kind: {'source/' + n: v for n, v in rows.items()} for kind, rows in scoped.items()}
            table['files']['source/prose'] = (b'fixture CLI', 0o755)
            manifest = {'platform': 'darwin-x64', 'agentsSdk': sdk}
            bundle = custody.validate_sdk_producer_evidence(manifest, table, evidence, lambda name: (root / name).read_bytes())
            self.assertEqual(len(bundle['checks']), len(checks)); self.assertEqual(len(bundle['rawOutputs']), len(checks))
            self.assertTrue(all((sdk_dir / name).is_file() for name, _, _ in checks))
            broken = dict(evidence); broken['logs/sdk-code-signatures.json'] = dict(broken['logs/sdk-code-signatures.json'], sha256='0'*64)
            with self.assertRaises(ValueError): custody.validate_sdk_producer_evidence(manifest, table, broken, lambda name: (root / name).read_bytes())

    def test_collect_and_silent_signature_proofs_are_retained_and_poison_refused(self):
        import hashlib
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); sdk = root / 'agents-sdk'; sdk.mkdir()
            (sdk / 'collect.toc').write_bytes(b'fixture producer TOC')
            payload = {'collectTocSha256': rc.digest(sdk / 'collect.toc'), 'codeSignaturePaths': ['prose-agents-sdk']}
            (sdk / 'agents-sdk-build.json').write_text(json.dumps({'platform': 'darwin', 'architecture': 'x86_64', 'payload': payload}))
            for name, verification in (('codesign-0000.log', 'strict'), ('codesign.log', 'strict-deep')):
                proof = {'schema': 'openprose.sdk-code-signature-check/1', 'phase': 'code-signature', 'path': 'prose-agents-sdk',
                    'verification': verification, 'exitCode': 0, 'timeoutSeconds': 30, 'success': True,
                    'outputComplete': True, 'timedOut': False, 'outputLimitExceeded': False, 'rawOutput': {}, 'stdoutBytes': 0, 'stderrBytes': 0}
                (sdk / name).write_text(json.dumps(proof))
            evidence = rc.sdk_producer_evidence(root, sdk)
            self.assertEqual(set(evidence), {'logs/sdk-collect.toc', 'logs/sdk-code-signatures.json'})
            for poison in ('collect', 'empty', 'failure', 'raw-path'):
                with self.subTest(poison=poison):
                    original = (sdk / 'codesign.log').read_bytes(); toc = (sdk / 'collect.toc').read_bytes()
                    if poison == 'collect': (sdk / 'collect.toc').write_bytes(b'changed')
                    elif poison == 'empty': (sdk / 'codesign.log').write_bytes(b'')
                    else:
                        proof = json.loads(original)
                        if poison == 'failure': proof['success'] = False
                        else: proof['rawOutput'] = {'stderr': {'path': '../escape', 'sha256': '0'*64, 'byteLength': 1}}
                        (sdk / 'codesign.log').write_text(json.dumps(proof))
                    with self.assertRaises(ValueError): rc.sdk_producer_evidence(root, sdk)
                    (sdk / 'codesign.log').write_bytes(original); (sdk / 'collect.toc').write_bytes(toc)


class NativeSdkRoutingTests(unittest.TestCase):
    def test_linux_requires_pin_and_never_uses_host_python(self):
        with tempfile.TemporaryDirectory() as d, patch.object(rc.sys, 'platform', 'linux'), patch.object(rc, 'command') as command:
            root = Path(d)
            with self.assertRaisesRegex(ValueError, 'pinned full Python'):
                rc.build_sdk(root, epoch='0', env={}, logs=root, agents_sdk_python=Path('/ambient/python'))
            command.assert_not_called()
            self.assertFalse((root / 'agents-sdk').exists())

    def test_native_linux_uses_driver_and_exact_frozen_trio(self):
        import build_agents_sdk_linux as driver
        import verify_agents_sdk_linux as verifier
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def frozen(source, stage, target, archive, *, epoch):
                self.assertEqual((source, target, archive, epoch), (rc.ROOT, 'linux-arm64-gnu', root / 'pinned.tar.zst', 123))
                (stage / 'frozen').mkdir(parents=True)
                for name in ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'):
                    (stage / 'frozen' / name).write_bytes(name.encode())
            with patch.object(rc.sys, 'platform', 'linux'), patch.object(rc.platform, 'machine', return_value='aarch64'), patch.object(driver, 'build', side_effect=frozen), patch.object(verifier, 'verify') as verify, patch.object(rc, 'command') as command:
                actual = rc.build_sdk(root, epoch='123', env={}, logs=root, linux_python_archive=root / 'pinned.tar.zst')
                command.assert_not_called()
                verify.assert_called_once_with(root / 'agents-sdk-native/frozen', root / 'agents-sdk-runtime', 'linux-arm64-gnu')
            self.assertEqual(len(list(actual.iterdir())), 3)
            for p in actual.iterdir():
                self.assertEqual(p.read_bytes(), p.name.encode())

    def test_failed_clean_runtime_never_admits_sdk_payload(self):
        import build_agents_sdk_linux as driver
        import verify_agents_sdk_linux as verifier
        with tempfile.TemporaryDirectory() as d, patch.object(rc.sys, 'platform', 'linux'), patch.object(rc.platform, 'machine', return_value='x86_64'), patch.object(driver, 'build'), patch.object(verifier, 'verify', side_effect=ValueError('runtime rejected')), patch.object(rc, 'command') as command:
            root = Path(d)
            with self.assertRaisesRegex(ValueError, 'runtime rejected'):
                rc.build_sdk(root, epoch='0', env={}, logs=root, linux_python_archive=root / 'pinned')
            command.assert_not_called(); self.assertFalse((root / 'agents-sdk').exists())

    def test_macos_keeps_separate_pinned_interpreter(self):
        with tempfile.TemporaryDirectory() as d, patch.object(rc.sys, 'platform', 'darwin'), patch.object(rc, 'command') as command:
            root = Path(d)
            rc.build_sdk(root, epoch='123', env={'PATH':'/bin'}, logs=root, agents_sdk_python=Path('/selected/python'))
            self.assertEqual(command.call_args.args[0][0], Path('/selected/python'))
            self.assertEqual(command.call_args.args[0][-1], '123')


if __name__ == '__main__':
    unittest.main()
