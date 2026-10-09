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
    def installed_records(self, platform):
        from test_kernel_rc_evidence import sdk_fixture
        sdk, table = sdk_fixture(platform)
        table = {kind: {'release/' + name: value for name, value in rows.items()}
                 for kind, rows in table.items()}
        table['files']['release/prose'] = (b'nonexecuted CLI fixture', 0o755)
        prefix = rc.sdk_custody.sdk_archive_prefix(table)
        identity = {name: {'sha256': hashlib.sha256(table['files'][prefix + name][0]).hexdigest(),
                           'byteLength': len(table['files'][prefix + name][0])}
                    for name in rc.sdk_custody.SDK_NAMES}
        if platform.startswith('darwin'):
            payload = json.loads(table['files'][prefix + 'agents-sdk-build.json'][0])['payload']
            identity['supportTree'] = {'payload': payload,
                'sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'byteLength': payload['totalRegularBytes'], 'entryCount': len(payload['entries'])}
        return {'platform': platform, 'agentsSdk': sdk}, table, [
            {'surface': surface, 'before': copy.deepcopy(identity), 'after': copy.deepcopy(identity)}
            for surface in ('installed-rust', 'installed-bun', 'installed-npm')]

    def test_actual_producer_writer_canonicalizes_shuffled_surfaces_for_custody(self):
        for platform in ('darwin-arm64', 'darwin-x64'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                manifest, table, records = self.installed_records(platform)
                with self.assertRaisesRegex(ValueError, 'surface identity differs'):
                    rc.sdk_custody.validate_installed_sdk_payloads(records, manifest, table)
                path = Path(directory) / 'installed-sdk-payloads.json'
                rc.write_installed_sdk_payloads(path, records)
                recorded = json.loads(path.read_bytes())
                self.assertEqual([r['surface'] for r in recorded],
                                 ['installed-bun', 'installed-rust', 'installed-npm'])
                self.assertEqual(rc.sdk_custody.validate_installed_sdk_payloads(recorded, manifest, table), recorded)
                self.assertEqual({r['surface']: r for r in records}, {r['surface']: r for r in recorded})

    def test_producer_refuses_duplicate_missing_unknown_or_changed_surfaces_before_write(self):
        _, _, original = self.installed_records('darwin-arm64')
        poisons = [original[:2], [original[0], original[0], original[2]]]
        unknown = copy.deepcopy(original); unknown[0]['surface'] = 'installed-other'; poisons.append(unknown)
        changed = copy.deepcopy(original); changed[0]['after']['prose-agents-sdk']['byteLength'] += 1; poisons.append(changed)
        for records in poisons:
            with self.subTest(records=records), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'installed-sdk-payloads.json'
                with self.assertRaises(ValueError):
                    rc.write_installed_sdk_payloads(path, records)
                self.assertFalse(path.exists())

    def command_log_fixture(self, output, platform='darwin-arm64'):
        (output / 'logs').mkdir()
        names = (rc.sdk_custody.COMMAND_LOG_MAC_MEMBERS if platform.startswith('darwin')
                 else rc.sdk_custody.COMMAND_LOG_LINUX_MEMBERS)
        data = {name: (b'' if i % 2 == 0 else b'opaque\x00\xff\n' + name.encode())
                for i, name in enumerate(names)}
        data['logs/unknown-future.log'] = b'keep unknown producer output'
        data['logs/installed-sdk-bun.json'] = b'{"structured":"not bundled"}'
        evidence = {}
        for name, encoded in reversed(list(data.items())):
            (output / name).write_bytes(encoded)
            evidence[name] = {'sha256': hashlib.sha256(encoded).hexdigest(), 'byteLength': len(encoded)}
        report = {'schema': 'openprose.kernel-rc-build/1', 'platform': platform,
                  'sourceRevision': 'a' * 40, 'version': '0.15.0-rc.4', 'evidence': evidence}
        manifest = {'schema': 'openprose.local-release-manifest/1', 'mode': 'kernel-rc',
                    'platform': platform, 'version': report['version'],
                    'source': {'revision': report['sourceRevision'], 'verification': 'matched-product-doctor'}}
        return report, manifest, data

    def test_actual_opaque_writer_preserves_shuffled_raw_zero_unknown_and_structured_bytes(self):
        for platform in rc.sdk_custody.COMMAND_LOG_PLATFORMS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                output = Path(directory).resolve()
                report, manifest, originals = self.command_log_fixture(output, platform)
                original_report = copy.deepcopy(report)
                metadata = {name: ((output / name).stat().st_mode, (output / name).stat().st_mtime_ns)
                            for name in originals}
                evidence = rc.producer_command_log_evidence(output, report, manifest)
                self.assertEqual(report, original_report)
                decoded = rc.sdk_custody.validate_producer_command_logs({**report, 'evidence': evidence}, manifest,
                    lambda name: rc.sdk_custody.read_command_log_bytes(output / name))
                self.assertEqual(decoded, {name: data for name, data in originals.items()
                                          if name not in ('logs/unknown-future.log', 'logs/installed-sdk-bun.json')})
                self.assertEqual(set(evidence), {'logs/unknown-future.log', 'logs/installed-sdk-bun.json',
                                               rc.sdk_custody.COMMAND_LOG_EVIDENCE})
                for name, data in originals.items():
                    self.assertEqual((output / name).read_bytes(), data)
                    self.assertEqual(((output / name).stat().st_mode, (output / name).stat().st_mtime_ns), metadata[name])
                bundle = json.loads((output / rc.sdk_custody.COMMAND_LOG_EVIDENCE).read_bytes())
                for row in bundle['members']:
                    if row['byteLength'] == 0:
                        self.assertEqual(row['base64'], '')
                        self.assertEqual(row['sha256'], hashlib.sha256(b'').hexdigest())

    def test_opaque_writer_refuses_stale_evidence_changed_metadata_and_existing_output(self):
        for poison in ('stale-evidence', 'changed-mode', 'existing-output'):
            with self.subTest(poison=poison), tempfile.TemporaryDirectory() as directory:
                output = Path(directory).resolve(); report, manifest, _ = self.command_log_fixture(output)
                name = rc.sdk_custody.COMMAND_LOG_MAC_MEMBERS[0]
                target = output / rc.sdk_custody.COMMAND_LOG_EVIDENCE
                if poison == 'stale-evidence':
                    (output / name).write_bytes(b'changed after original evidence capture')
                if poison == 'existing-output': target.write_bytes(b'preserve existing bundle')
                reader = rc.sdk_custody.read_command_log_bytes
                def changed(path):
                    data = reader(path)
                    if path == output / name: path.chmod(0o755)
                    return data
                with patch.object(rc.sdk_custody, 'read_command_log_bytes',
                                  side_effect=changed if poison == 'changed-mode' else reader):
                    with self.assertRaises(ValueError):
                        rc.producer_command_log_evidence(output, report, manifest)
                if poison == 'existing-output': self.assertEqual(target.read_bytes(), b'preserve existing bundle')
                else: self.assertFalse(target.exists())

    def test_opaque_writer_rejects_oversized_encoded_bundle_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve(); report, manifest, _ = self.command_log_fixture(output)
            # Exactly16MiB of base64 alone, so required JSON metadata exceeds
            # the real aggregate bound even though the raw file is admissible.
            name = rc.sdk_custody.COMMAND_LOG_MAC_MEMBERS[0]
            data = b'x' * (12 * 1024 * 1024)
            (output / name).write_bytes(data)
            report['evidence'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'byteLength': len(data)}
            for other in rc.sdk_custody.COMMAND_LOG_MAC_MEMBERS[1:]:
                (output / other).write_bytes(b'')
                report['evidence'][other] = {'sha256': hashlib.sha256(b'').hexdigest(), 'byteLength': 0}
            with self.assertRaisesRegex(ValueError, 'exceeds evidence bound'):
                rc.producer_command_log_evidence(output, report, manifest)
            self.assertFalse((output / rc.sdk_custody.COMMAND_LOG_EVIDENCE).exists())

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


class KernelRCLinuxRuntimeProducerTests(unittest.TestCase):
    def fixture(self, output, platform='linux-x64-gnu', *, large_helper=False):
        from test_kernel_rc_evidence import sdk_fixture, linux_runtime_fixture
        sdk, table = sdk_fixture(platform)
        table = {kind: {'release/' + name: value for name, value in rows.items()}
                 for kind, rows in table.items()}
        table['files']['release/prose'] = (b'nonexecuted CLI fixture', 0o755)
        if large_helper:
            helper = table['files']['release/prose-agents-sdk'][0]
            helper += b'\0' * (16 * 1024 * 1024 + 1 - len(helper))
            receipt = json.loads(table['files']['release/agents-sdk-build.json'][0])
            receipt['helper'].update(sha256=hashlib.sha256(helper).hexdigest(), byteLength=len(helper))
            encoded = json.dumps(receipt, sort_keys=True).encode()
            table['files']['release/prose-agents-sdk'] = (helper, 0o755)
            table['files']['release/agents-sdk-build.json'] = (encoded, 0o644)
            sdk.update(sha256=receipt['helper']['sha256'], byteLength=len(helper),
                       receiptSha256=hashlib.sha256(encoded).hexdigest())
        report = {'schema': 'openprose.kernel-rc-build/1', 'platform': platform,
                  'sourceRevision': 'a' * 40, 'version': '0.15.0-rc.4', 'evidence': {}}
        manifest = {'schema': 'openprose.local-release-manifest/1', 'mode': 'kernel-rc',
                    'platform': platform, 'version': report['version'], 'agentsSdk': sdk,
                    'source': {'revision': report['sourceRevision'], 'verification': 'matched-product-doctor'}}
        (output / 'logs').mkdir()
        if platform.startswith('darwin'):
            return report, manifest, table, {}
        originals = linux_runtime_fixture(report, manifest, table, rc.ROOT)
        runtime = output / 'agents-sdk-runtime'
        for name in ('job/home', 'job/docker-config', 'payload'):
            (runtime / name).mkdir(parents=True, exist_ok=True)
        for name, data in originals.items():
            if not name.startswith('sources/') and name != rc.sdk_custody.LINUX_RUNTIME_EVIDENCE:
                (runtime / name).write_bytes(data)
        for name in rc.sdk_custody.SDK_NAMES:
            path = runtime / 'payload' / name
            path.write_bytes(table['files']['release/' + name][0])
            path.chmod(0o555 if name == 'prose-agents-sdk' else 0o444)
        (output / 'logs/unknown.log').write_bytes(b'preserve other evidence')
        report['evidence']['logs/unknown.log'] = {
            'sha256': hashlib.sha256(b'preserve other evidence').hexdigest(), 'byteLength': 23}
        return report, manifest, table, originals

    def test_actual_runtime_packet_writer_preserves_all_originals_and_replays_both_architectures(self):
        for platform in ('linux-x64-gnu', 'linux-arm64-gnu'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                output = Path(directory).resolve(); report, manifest, table, originals = self.fixture(output, platform)
                before_report = copy.deepcopy(report)
                evidence = rc.producer_linux_runtime_evidence(output, report, manifest, table, source_root=rc.ROOT)
                self.assertEqual(report, before_report)
                self.assertEqual(set(evidence), {'logs/unknown.log', rc.sdk_custody.LINUX_RUNTIME_EVIDENCE})
                packet_path = output / rc.sdk_custody.LINUX_RUNTIME_EVIDENCE
                runtime_report = rc.sdk_custody.validate_linux_runtime_evidence({**report, 'evidence': evidence},
                    manifest, table, lambda name: rc.sdk_custody.read_command_log_bytes(output / name),
                    expected_sources=rc.sdk_custody.read_linux_runtime_sources(rc.ROOT))
                self.assertEqual(runtime_report, json.loads(originals['runtime-report.json']))
                packet = json.loads(packet_path.read_bytes())
                self.assertEqual(len(packet['members']), 17)
                self.assertFalse(any(row['path'].startswith('payload/') for row in packet['members']))
                for name, data in originals.items():
                    if name.startswith('sources/'):
                        self.assertEqual((rc.ROOT / name[len('sources/'):]).read_bytes(), data)
                    elif name != rc.sdk_custody.LINUX_RUNTIME_EVIDENCE:
                        self.assertEqual((output / 'agents-sdk-runtime' / name).read_bytes(), data)
                for name in rc.sdk_custody.SDK_NAMES:
                    self.assertEqual((output / 'agents-sdk-runtime/payload' / name).read_bytes(),
                                     table['files']['release/' + name][0])

    def test_runtime_writer_refuses_extra_directory_payload_file_alias_or_changed_copied_trio(self):
        for poison in ('extra-job-file', 'extra-directory', 'extra-payload', 'payload-alias', 'changed-trio', 'wrong-mode'):
            with self.subTest(poison=poison), tempfile.TemporaryDirectory() as directory:
                output = Path(directory).resolve(); report, manifest, table, _ = self.fixture(output)
                runtime = output / 'agents-sdk-runtime'; helper = runtime / 'payload/prose-agents-sdk'
                if poison == 'extra-job-file': (runtime / 'job/unknown.log').write_bytes(b'not silently discarded')
                elif poison == 'extra-directory': (runtime / 'job/home/unknown').mkdir()
                elif poison == 'extra-payload': (runtime / 'payload/unknown').write_bytes(b'not silently discarded')
                elif poison == 'payload-alias':
                    helper.unlink(); helper.symlink_to(output / 'absent')
                elif poison == 'changed-trio':
                    helper.chmod(0o755); helper.write_bytes(b'changed copied bytes'); helper.chmod(0o555)
                elif poison == 'wrong-mode': helper.chmod(0o755)
                with self.assertRaises(ValueError):
                    rc.producer_linux_runtime_evidence(output, report, manifest, table, source_root=rc.ROOT)
                self.assertFalse((output / rc.sdk_custody.LINUX_RUNTIME_EVIDENCE).exists())

    def test_runtime_writer_requires_trusted_source_bytes_and_fresh_packet(self):
        for poison in ('changed-source', 'existing-packet'):
            with self.subTest(poison=poison), tempfile.TemporaryDirectory() as directory:
                output = Path(directory).resolve(); report, manifest, table, originals = self.fixture(output)
                source_root = rc.ROOT
                target = output / rc.sdk_custody.LINUX_RUNTIME_EVIDENCE
                if poison == 'existing-packet': target.write_bytes(b'preserve prior packet')
                else:
                    source_root = output / 'source-fixture'; source_root.mkdir()
                    for name in rc.sdk_custody.LINUX_RUNTIME_SOURCE_PATHS:
                        path = source_root / name; path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(originals['sources/' + name])
                    changed = source_root / 'cli/ci/build_agents_sdk_linux.py'
                    changed.write_bytes(changed.read_bytes() + b'\n# changed trusted source fixture\n')
                with self.assertRaises(ValueError):
                    rc.producer_linux_runtime_evidence(output, report, manifest, table, source_root=source_root)
                if poison == 'existing-packet': self.assertEqual(target.read_bytes(), b'preserve prior packet')
                else: self.assertFalse(target.exists())

    def test_runtime_packet_cap_does_not_limit_copied_helper_to_sixteen_mebibytes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve(); report, manifest, table, _ = self.fixture(output, large_helper=True)
            evidence = rc.producer_linux_runtime_evidence(output, report, manifest, table, source_root=rc.ROOT)
            self.assertGreater((output / 'agents-sdk-runtime/payload/prose-agents-sdk').stat().st_size, 16 * 1024 * 1024)
            self.assertLess(evidence[rc.sdk_custody.LINUX_RUNTIME_EVIDENCE]['byteLength'], 16 * 1024 * 1024)

    def test_runtime_writer_refuses_oversized_packet_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve(); report, manifest, table, _ = self.fixture(output)
            (output / 'agents-sdk-runtime/job/pull-runtime.log').write_bytes(b'x' * (12 * 1024 * 1024))
            with self.assertRaisesRegex(ValueError, 'exceeds evidence bound'):
                rc.producer_linux_runtime_evidence(output, report, manifest, table, source_root=rc.ROOT)
            self.assertFalse((output / rc.sdk_custody.LINUX_RUNTIME_EVIDENCE).exists())

    def test_mac_absence_guard_performs_no_linux_source_lookup_or_runtime_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve(); report, manifest, table, _ = self.fixture(output, 'darwin-arm64')
            with patch.object(rc.sdk_custody, 'read_linux_runtime_sources') as sources, \
                    patch.object(rc.sdk_custody, 'read_command_log_bytes') as reader:
                self.assertEqual(rc.producer_linux_runtime_evidence(output, report, manifest, table,
                    source_root=output / 'absent'), {})
                report['evidence'][rc.sdk_custody.LINUX_RUNTIME_EVIDENCE] = {'sha256': 'a' * 64, 'byteLength': 1}
                with self.assertRaisesRegex(ValueError, 'Mac report contains'):
                    rc.producer_linux_runtime_evidence(output, report, manifest, table, source_root=output / 'absent')
                sources.assert_not_called(); reader.assert_not_called()


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
