"""Hermetic custody and consumer-environment checks for installation qualification."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qualify_installed_sdk as installed


def archive(path, members, table=None):
    with tarfile.open(path, 'w:gz') as target:
        for name, data in members.items():
            data, mode = data if isinstance(data, tuple) else (data, 0o755)
            member = tarfile.TarInfo(name); member.size = len(data); member.mode = mode
            target.addfile(member, io.BytesIO(data))
        for name, mode in (table or {}).get('directories', {}).items():
            member = tarfile.TarInfo(name); member.type = tarfile.DIRTYPE; member.mode = mode; target.addfile(member)
        for name, text in (table or {}).get('symlinks', {}).items():
            member = tarfile.TarInfo(name); member.type = tarfile.SYMTYPE; member.mode = 0o777; member.linkname = text; target.addfile(member)


def sdk_archive(path, extra=None, prefix='payload/'):
    from test_kernel_rc_evidence import sdk_fixture
    sdk, scoped = sdk_fixture('darwin-x64')
    table = {kind: {prefix + name: value for name, value in rows.items()} for kind, rows in scoped.items()}
    table['files'].update(extra or {prefix + 'prose': (b'cli', 0o755)})
    archive(path, table['files'], table)
    return {'agentsSdk': sdk, 'platform': 'darwin-x64'}, table


def sdk_archive_with_resource_names(path):
    manifest, table = sdk_archive(path)
    payload = json.loads(table['files']['payload/agents-sdk-build.json'][0])
    for name in ('prose', 'agents-sdk-build.json'):
        relative = 'prose-agents-sdk-runtime/' + name
        data = b'support resource ' + name.encode()
        table['files']['payload/' + relative] = (data, 0o644)
        payload['payload']['entries'].append({'path': relative, 'type': 'file', 'mode': 0o644,
            'byteLength': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        payload['payload']['totalRegularBytes'] += len(data)
    payload['payload']['entries'].sort(key=lambda row: row['path'])
    encoded = json.dumps(payload, sort_keys=True).encode()
    table['files']['payload/agents-sdk-build.json'] = (encoded, 0o644)
    manifest['agentsSdk']['receiptSha256'] = hashlib.sha256(encoded).hexdigest()
    archive(path, table['files'], table)
    return manifest, table


class InstallationQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.node = self.root / 'node'; self.node.write_bytes(b'node-fixture'); self.node.chmod(0o755)
        self.npm = self.root / 'npm-cli.js'; self.npm.write_bytes(b'npm-fixture')

    def qualification(self):
        return installed.Qualification(self.root / 'output', self.node, self.npm)

    def test_nonfresh_output_and_symlink_inputs_rejected(self):
        output = self.root / 'output'; output.mkdir()
        with self.assertRaisesRegex(ValueError, 'fresh'):
            installed.Qualification(output, self.node, self.npm)
        linked = self.root / 'linked'; linked.symlink_to(self.node)
        with self.assertRaisesRegex(ValueError, 'regular'):
            installed.Qualification(self.root / 'new', linked, self.npm)

    def test_consumer_environment_has_no_provider_python_checkout_or_ambient_settings(self):
        q = self.qualification(); root = q.output / 'consumer'; root.mkdir()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'PROSE_HARNESS': 'codex', 'PROSE_CONFIG_DIR': '/user-settings', 'PYTHONPATH': '/checkout', 'npm_config_userconfig': '/user-npmrc'}, clear=False):
            native = q.environment(root)
            npm = q.environment(root, npm=True, explicit_config=True)
        self.assertEqual(native['PATH'], '')
        self.assertTrue((root / '.git').is_dir())
        self.assertEqual(npm['PATH'], str(q.tools))
        for env in (native, npm):
            self.assertNotIn('OPENAI_API_KEY', env); self.assertNotIn('PROSE_HARNESS', env); self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('HTTPS_PROXY', env); self.assertTrue(Path(env['HOME']).is_relative_to(q.output))
        self.assertEqual(npm['PROSE_CONFIG_DIR'], str(root / 'preferences'))
        self.assertEqual(sorted(p.name for p in q.tools.iterdir()), ['node'])
        self.assertEqual(npm['npm_config_offline'], 'true')
        self.assertEqual(npm['npm_config_script_shell'], '/bin/sh')
        self.assertEqual(Path(npm['npm_config_userconfig']).read_bytes(), b'')

    def test_exact_rc_order_and_invalid_versions(self):
        self.assertGreater(installed.rc_version('0.15.0-rc.4'), installed.rc_version('0.15.0-rc.3'))
        self.assertLess(installed.rc_version('0.15.0-rc.2'), installed.rc_version('0.15.0-rc.3'))
        for bad in ('0.15.0', '0.15.0-rc.04', 'latest', '0.15.0-rc.3-dev'):
            with self.assertRaises(ValueError): installed.rc_version(bad)

    def test_npx_preserves_exact_priming_registry_cache_key_and_remains_offline(self):
        env = {'npm_config_registry': 'http://127.0.0.1:9', 'npm_config_offline': 'false', 'HOME': '/isolated'}
        actual = installed.offline_registry_environment(env, {'registryUrl': 'http://127.0.0.1:45321'})
        self.assertEqual(actual, {'npm_config_registry': 'http://127.0.0.1:45321', 'npm_config_offline': 'true', 'HOME': '/isolated'})
        self.assertEqual(env['npm_config_registry'], 'http://127.0.0.1:9')
        for registry in (None, 'https://registry.npmjs.org', 'http://localhost:1234', 'http://127.0.0.1:0', 'http://127.0.0.1:65536'):
            with self.assertRaisesRegex(ValueError, 'loopback'):
                installed.offline_registry_environment(env, {'registryUrl': registry})

    def test_standalone_extracts_bound_siblings_and_relocation_preserves_bytes(self):
        path = self.root / 'package.tgz'
        manifest, _ = sdk_archive(path)
        receipt = installed.extract_standalone(path, self.root / 'install', sdk=True, manifest=manifest)
        (self.root / 'install').rename(self.root / 'relocated')
        self.assertEqual(set(receipt), {'prose', *installed.SDK_NAMES, 'supportTree'})
        for name, record in receipt.items():
            if name == "supportTree": continue
            self.assertEqual(installed.pub.digest(self.root / 'relocated' / name), record['sha256'])
        with self.assertRaisesRegex(ValueError, 'fresh'):
            installed.extract_standalone(path, self.root / 'relocated', sdk=True, manifest=manifest)

    def test_archive_link_and_missing_helper_rejected_before_execution(self):
        path = self.root / 'bad.tgz'
        with tarfile.open(path, 'w:gz') as target:
            member = tarfile.TarInfo('payload/prose'); member.type = tarfile.SYMTYPE; member.linkname = '/user/bin/prose'; target.addfile(member)
        from test_kernel_rc_evidence import sdk_fixture
        manifest = {'platform': 'darwin-x64', 'agentsSdk': sdk_fixture('darwin-x64')[0]}
        with self.assertRaises(ValueError):
            installed.extract_standalone(path, self.root / 'install', sdk=True, manifest=manifest)
        archive(path, {'payload/prose': b'cli'})
        with self.assertRaisesRegex(ValueError, 'SDK'):
            installed.extract_standalone(path, self.root / 'install', sdk=True, manifest=manifest)
        self.assertFalse((self.root / 'install').exists())

    def test_artifact_digest_and_regular_file_binding(self):
        path = self.root / 'native.tgz'; path.write_bytes(b'original')
        record = {'path': path.name, 'byteLength': 8, 'sha256': installed.pub.digest(path)}
        self.assertEqual(installed.artifact(self.root, record), path)
        path.write_bytes(b'modified')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            installed.artifact(self.root, record)

    def test_npm_original_alias_cohort_and_native_binding(self):
        source = '1' * 40; version = '0.15.0-rc.4'; platform = 'darwin-x64'; binary = b'compiled-bun'
        payload_version = installed.package_local.npm_payload_version(version, platform)
        cohort = {'sourceRevision': source, 'version': version}
        meta = {'name': '@openprose/prose-cli', 'version': version, 'openproseCohort': cohort, 'optionalDependencies': {'@openprose/prose-cli-' + platform: 'npm:@openprose/prose-cli@' + payload_version}}
        native = {'name': '@openprose/prose-cli', 'version': payload_version, 'openproseCohort': cohort}
        archive(self.root / 'meta.tgz', {'package/package.json': json.dumps(meta).encode(), 'package/bin/prose.js': b'launcher'})
        sdk_manifest, native_table = sdk_archive(self.root / 'native.tgz', {'package/package.json': (json.dumps(native).encode(), 0o644), 'package/bin/prose': (binary, 0o755)}, prefix='package/bin/')
        manifest = {'source': {'revision': source}, 'version': version, 'artifacts': [{'kind': kind, 'platform': target, 'path': path.name, 'byteLength': path.stat().st_size, 'sha256': installed.pub.digest(path)} for kind, target, path in [('npm-meta', None, self.root / 'meta.tgz'), ('npm-platform', platform, self.root / 'native.tgz')]]}
        manifest['agentsSdk'] = sdk_manifest['agentsSdk']; manifest['platform'] = 'darwin-x64'
        self.assertEqual(installed.npm_inputs(self.root, manifest, platform, expected_binary=hashlib.sha256(binary).hexdigest()), (self.root / 'meta.tgz', self.root / 'native.tgz'))
        with self.assertRaisesRegex(ValueError, 'differs from verified'):
            installed.npm_inputs(self.root, manifest, platform, expected_binary='0' * 64)
        native['scripts'] = {'postinstall': 'touch bad'}
        native_table['files']['package/package.json'] = (json.dumps(native).encode(), 0o644)
        archive(self.root / 'native.tgz', native_table['files'], native_table)
        manifest['artifacts'][1].update(byteLength=(self.root / 'native.tgz').stat().st_size, sha256=installed.pub.digest(self.root / 'native.tgz'))
        with self.assertRaisesRegex(ValueError, 'lifecycle'):
            installed.npm_inputs(self.root, manifest, platform)

    def test_execute_uses_explicit_node_and_checksum_bound_receipt(self):
        q = self.qualification(); root = q.output / 'consumer'; root.mkdir(); env = q.environment(root, npm=True)
        with patch.object(installed.package_local, 'run_bounded', return_value=subprocess.CompletedProcess([], 0, b'ok\n', b'')) as run:
            q.execute(['npm', '--version'], cwd=root, env=env, label='npm-version')
        self.assertEqual(run.call_args.args[0][:2], [str(self.node), str(self.npm)])
        self.assertEqual(run.call_args.kwargs['cwd'], root)
        evidence = q.output / q.commands[0]['path']
        self.assertEqual(installed.pub.digest(evidence), q.commands[0]['sha256'])
        self.assertEqual(json.loads(evidence.read_text())['stdout'], 'ok\n')

    def test_failed_command_keeps_receipt_and_does_not_claim_pass(self):
        q = self.qualification(); env = {'PATH': ''}
        with patch.object(installed.package_local, 'run_bounded', return_value=subprocess.CompletedProcess([], 7, b'', b'failure')):
            with self.assertRaisesRegex(ValueError, 'Unexpected exit'):
                q.execute(['/fixture/prose'], cwd=q.output, env=env, label='failed')
        self.assertEqual(q.checks, []); self.assertEqual(len(q.commands), 1)

    def test_defaults_reject_stale_identity_and_persisted_builtin_settings(self):
        q = self.qualification(); root = q.output / 'consumer'; root.mkdir(); env = q.environment(root)
        with patch.object(q, 'identity', return_value={'selectedHarness': 'openprose', 'selectedTransport': 'hosted'}):
            with self.assertRaisesRegex(ValueError, 'installed default'):
                q.defaults(['/fixture'], 'bun', '0.15.0-rc.4', '1' * 40, root, env, 'stale')
        self.assertEqual(q.checks, [])

    def consumer_responses(self, q, root, env, *, persist=False, source='1' * 40):
        doctor = {'runner': {'name': 'bun', 'version': '0.15.0-rc.4', 'commit': source}, 'build': {'profile': 'release', 'testSeamsEnabled': False}, 'selectedHarness': 'agents-sdk', 'selectedTransport': 'jsonl', 'problems': [{'code': 'HARNESS_NEEDS_AUTH', 'action': installed.SETUP_ACTION, 'details': {'adapterId': 'agents-sdk/jsonl', 'authProfile': 'openai-api-key', 'fallbackAttempted': False}}]}
        def run(argv, **kwargs):
            self.assertEqual(kwargs['cwd'], root)
            if kwargs['environment'].get('OPENAI_API_KEY') == installed.AUTH_CANARY:
                self.assertIn('--dry-run', argv)
                self.assertEqual(kwargs['environment'], dict(env, OPENAI_API_KEY=installed.AUTH_CANARY))
            else: self.assertEqual(kwargs['environment'], env)
            self.assertEqual(argv[0], '/installed/prose')
            if '--version' in argv: return subprocess.CompletedProcess(argv, 0, b'prose 0.15.0-rc.4 (bun)\n', b'')
            if 'doctor' in argv: value, code = doctor, 10
            elif 'explain' in argv:
                value, code = {'values': {k: {'value': v, 'source': {'kind': 'default'}} for k, v in {'harness': 'agents-sdk', 'model': 'gpt-6.1-sol', 'authProfile': 'openai-api-key'}.items()}}, 0
                if persist:
                    path = Path(env['HOME']) / '.prose/cli.toml'; path.parent.mkdir(); path.write_text('harness="agents-sdk"\n')
            elif '--dry-run' in argv:
                if kwargs['environment'].get('OPENAI_API_KEY') == installed.AUTH_CANARY:
                    value, code = {'schema': 'openprose.runner-dry-run-report/1', 'wouldStartModel': False, 'readiness': 'ready', 'blockingError': None, 'billingOwner': 'user-provider', 'selection': {'harness': 'agents-sdk', 'adapterId': 'agents-sdk/jsonl', 'transport': 'jsonl', 'runtimeVersion': 'prose-agents-sdk 0.1.0', 'model': 'gpt-6.1-sol'}}, 0
                else: value, code = {'schema': 'openprose.runner-dry-run-report/1', 'blockingError': {'code': 'HARNESS_NEEDS_AUTH'}}, 10
            else: value, code = {'schema': 'openprose.runner-error/1', 'code': 'HARNESS_NEEDS_AUTH', 'action': installed.SETUP_ACTION}, 10
            return subprocess.CompletedProcess(argv, code, json.dumps(value).encode() + b'\n', b'')
        return run

    def test_actual_consumer_probe_matrix_checks_identity_defaults_and_zero_persistence(self):
        q = self.qualification(); root = q.output / 'consumer'; root.mkdir(); env = q.environment(root)
        with patch.object(installed.package_local, 'run_bounded', side_effect=self.consumer_responses(q, root, env)) as run:
            q.defaults(['/installed/prose'], 'bun', '0.15.0-rc.4', '1' * 40, root, env, 'fresh')
        self.assertEqual(run.call_count, 6)
        self.assertEqual([x['label'] for x in q.commands], ['fresh-version', 'fresh-doctor', 'fresh-explain', 'fresh-missing-key', 'fresh-dry-run', 'fresh-helper-discovery'])
        self.assertEqual(len(q.checks), 1)
        self.assertFalse((Path(env['HOME']) / '.prose/cli.toml').exists())

    def test_readonly_explain_cannot_silently_persist_builtin_preferences(self):
        q = self.qualification(); root = q.output / 'consumer'; root.mkdir(); env = q.environment(root)
        with patch.object(installed.package_local, 'run_bounded', side_effect=self.consumer_responses(q, root, env, persist=True)):
            with self.assertRaisesRegex(ValueError, 'persisted settings'):
                q.defaults(['/installed/prose'], 'bun', '0.15.0-rc.4', '1' * 40, root, env, 'fresh')
        self.assertEqual(q.checks, [])

    def test_actual_consumer_probe_rejects_wrong_embedded_source(self):
        q = self.qualification(); root = q.output / 'consumer'; root.mkdir(); env = q.environment(root)
        with patch.object(installed.package_local, 'run_bounded', side_effect=self.consumer_responses(q, root, env, source='2' * 40)):
            with self.assertRaisesRegex(ValueError, 'source identity'):
                q.defaults(['/installed/prose'], 'bun', '0.15.0-rc.4', '1' * 40, root, env, 'fresh')
        self.assertEqual(q.checks, [])

    def test_outer_helper_discovery_requires_no_inference_and_exact_packaged_runtime(self):
        valid_selection = {'harness': 'agents-sdk', 'adapterId': 'agents-sdk/jsonl', 'transport': 'jsonl',
                           'runtimeVersion': 'prose-agents-sdk 0.1.0', 'model': 'gpt-6.1-sol'}
        mutations = [{'wouldStartModel': True}, {'readiness': 'blocked'},
                     {'blockingError': {'code': 'HARNESS_UNAVAILABLE'}}]
        mutations.extend({'selection': dict(valid_selection, runtimeVersion=banner)} for banner in
                         ('0.1.0', 'prose-agents-sdk 9.9.9', 'other-helper 0.1.0'))
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                q = installed.Qualification(self.root / ('output-' + str(index)), self.node, self.npm)
                root = q.output / 'consumer'; root.mkdir(); env = q.environment(root)
                normal = self.consumer_responses(q, root, env)
                def run(argv, **kwargs):
                    result = normal(argv, **kwargs)
                    if kwargs['environment'].get('OPENAI_API_KEY') == installed.AUTH_CANARY:
                        value = json.loads(result.stdout); value.update(mutation)
                        result = subprocess.CompletedProcess(argv, result.returncode, json.dumps(value).encode(), b'')
                    return result
                with patch.object(installed.package_local, 'run_bounded', side_effect=run):
                    with self.assertRaisesRegex(ValueError, 'Synthetic-key|packaged SDK sibling'):
                        q.defaults(['/installed/prose'], 'bun', '0.15.0-rc.4', '1' * 40, root, env, 'fresh')
                self.assertEqual(q.checks, [])

    def test_saved_preferences_must_survive_byte_exactly(self):
        q = self.qualification(); root = q.output / 'consumer'; root.mkdir(); env = q.environment(root)
        path = root / 'preferences.toml'; path.write_bytes(b'# original\ntimeout="1m"\n')
        saved = (path, path.read_bytes()); path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Upgrade changed'):
            q.preferences(['/fixture'], root, env, 'upgrade', saved)

    def test_candidate_custody_failure_prevents_output_and_execution(self):
        with patch.object(installed.custody, 'verify_kernel_rc', side_effect=ValueError('custody rejected')) as verifier:
            with self.assertRaisesRegex(ValueError, 'custody rejected'):
                installed.qualify(self.root / 'candidate', self.root / 'previous', self.root / 'output', '1' * 40, '0.15.0-rc.4', self.node, self.npm)
        verifier.assert_called_once(); self.assertFalse((self.root / 'output').exists())

    def test_support_resource_named_prose_does_not_select_cli(self):
        manifest, _ = sdk_archive_with_resource_names(self.root / 'complete.tgz')
        directory = self.root / 'installed'
        installed.extract_standalone(self.root / 'complete.tgz', directory, sdk=True, manifest=manifest)
        self.assertEqual((directory / 'prose').read_bytes(), b'cli')
        self.assertEqual((directory / 'prose-agents-sdk-runtime/prose').read_bytes(), b'support resource prose')

    def test_complete_tree_relocation_and_aliases_are_preserved(self):
        manifest, _ = sdk_archive(self.root / 'complete.tgz')
        directory = self.root / 'installed'
        installed.extract_standalone(self.root / 'complete.tgz', directory, sdk=True, manifest=manifest)
        before = installed.installed_sdk_identity(directory, manifest['agentsSdk'])
        directory.rename(self.root / 'relocated-complete')
        self.assertEqual(before, installed.installed_sdk_identity(self.root / 'relocated-complete', manifest['agentsSdk']))
        framework = self.root / 'relocated-complete/prose-agents-sdk-runtime/Python.framework'
        self.assertTrue((framework / 'Versions/Current').is_symlink())
        self.assertEqual(os.readlink(framework / 'Versions/Current'), '3.10')
        (framework / 'Versions/3.10/Python').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            installed.installed_sdk_identity(self.root / 'relocated-complete', manifest['agentsSdk'])

    def test_missing_support_refused_before_any_helper_probe(self):
        manifest, _ = sdk_archive(self.root / 'complete.tgz')
        q = self.qualification(); directory = q.output / 'installed'
        installed.extract_standalone(self.root / 'complete.tgz', directory, sdk=True, manifest=manifest)
        import shutil
        shutil.rmtree(directory / 'prose-agents-sdk-runtime')
        with patch.object(q, 'execute') as execute:
            with self.assertRaises(ValueError):
                q.payload(directory, installed.pub.digest(directory / 'prose'), manifest['agentsSdk'])
            execute.assert_not_called()

    def test_support_mutation_during_mocked_tool_probe_cannot_be_recorded(self):
        manifest, _ = sdk_archive(self.root / 'complete.tgz')
        q = self.qualification(); directory = q.output / 'installed'
        installed.extract_standalone(self.root / 'complete.tgz', directory, sdk=True, manifest=manifest)
        def mocked(argv, **kwargs):
            if argv[-1] == '--packaged-tool-self-test':
                (directory / 'prose-agents-sdk-runtime/empty-data').write_bytes(b'mutated')
                expected = installed.kernel_rc_evidence.SDK_TOOL_TEST
            else: expected = installed.kernel_rc_evidence.SDK_IMPORT_TEST
            return {'stdout': json.dumps(expected), 'stderr': ''}
        with patch.object(q, 'execute', side_effect=mocked):
            with self.assertRaises(ValueError):
                q.payload(directory, installed.pub.digest(directory / 'prose'), manifest['agentsSdk'])
        self.assertEqual(q.payloads, [])

    def test_receipt_dictionary_is_once_bound_and_missing_unreferenced_poison_refused(self):
        import copy
        manifest, _ = sdk_archive(self.root / 'complete.tgz')
        q = self.qualification(); directory = q.output / 'installed'
        installed.extract_standalone(self.root / 'complete.tgz', directory, sdk=True, manifest=manifest)
        def mocked(argv, **kwargs):
            expected = installed.kernel_rc_evidence.SDK_TOOL_TEST if argv[-1] == '--packaged-tool-self-test' else installed.kernel_rc_evidence.SDK_IMPORT_TEST
            return {'stdout': json.dumps(expected), 'stderr': ''}
        with patch.object(q, 'execute', side_effect=mocked):
            q.payload(directory, installed.pub.digest(directory / 'prose'), manifest['agentsSdk'])
        installed.validate_payload_evidence(q.payloads, q.sdk_evidence, 'darwin-x64')
        with self.assertRaisesRegex(ValueError, 'architecture differs'):
            installed.validate_payload_evidence(q.payloads, q.sdk_evidence, 'darwin-arm64')
        self.assertEqual(len(q.sdk_evidence), 1)
        self.assertNotIn('payload', q.payloads[0]['members']['supportTree'])
        for poison in ('missing', 'unreferenced', 'support-count', 'receipt-bytes', 'record-extra'):
            records, evidence = copy.deepcopy(q.payloads), dict(q.sdk_evidence)
            if poison == 'missing': evidence.clear()
            elif poison == 'unreferenced': evidence['0' * 64] = next(iter(evidence.values()))
            elif poison == 'support-count': records[0]['before']['supportTree']['entryCount'] = True
            elif poison == 'receipt-bytes': evidence[next(iter(evidence))] += ' '
            else: records[0]['unknown'] = 'not-accepted'
            with self.subTest(poison=poison), self.assertRaises(ValueError):
                installed.validate_payload_evidence(records, evidence, 'darwin-x64')
        digest = next(iter(q.sdk_evidence))
        raw = q.sdk_evidence[digest].replace('{', '{"schema":"duplicate",', 1)
        rebound = hashlib.sha256(raw.encode()).hexdigest()
        records = copy.deepcopy(q.payloads)
        for identity in (records[0]['before'], records[0]['after'], records[0]['members']):
            identity['supportTree']['receiptSha256'] = rebound
            identity['agents-sdk-build.json'] = {'sha256': rebound, 'byteLength': len(raw.encode())}
        with self.assertRaises(ValueError):
            installed.validate_payload_evidence(records, {rebound: raw}, 'darwin-x64')


if __name__ == '__main__':
    unittest.main()
