#!/usr/bin/env python3
"""Qualify original installed RC bytes in isolated consumer roots; never publish."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

import homebrew_rehearsal as custody
import kernel_rc_evidence
import npm_alias_install
import package_local
import publication as pub
import sdk_native_inventory as sdk_inventory

PREVIOUS_MANIFEST_SHA256 = '571eb285f964ea980d4e3aec6aa574f782b9d51f2a6934d219bc2ae7d8755e4f'
SETUP_ACTION = 'Set OPENAI_API_KEY to an OpenAI API key in the process environment, then retry. No model request was sent.'
SDK_NAMES = ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt')
AUTH_CANARY = 'provider-free-installation-canary'


def offline_registry_environment(env: dict[str, str], installation: dict) -> dict[str, str]:
    """npm keys cached metadata by registry URL, including its loopback port."""
    registry = installation.get('registryUrl')
    pub.require(isinstance(registry, str) and re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', registry) and int(registry.rsplit(':', 1)[1]) <= 65535, 'Exact loopback cache registry URL is required')
    return dict(env, npm_config_registry=registry, npm_config_offline='true')


def regular(path: Path) -> None:
    pub.require(path.is_file() and not path.is_symlink(), 'Expected regular file: ' + str(path))


def rc_version(version: str) -> tuple[int, ...]:
    pub.require(isinstance(version, str) and re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc\.(0|[1-9][0-9]*)', version), 'Exact RC version required')
    return tuple(int(x) for x in re.split(r'\.|-rc\.', version))


def artifact(root: Path, record: dict, *, previous=False) -> Path:
    name = record['name' if previous else 'path']
    pub.require(pub.safe_name(name), 'Unsafe artifact name')
    path = root / name
    regular(path)
    length = record['size' if previous else 'byteLength']
    pub.require(type(length) is int and 0 < length <= pub.MAX_BYTES and path.stat().st_size == length and pub.digest(path) == record['sha256'], 'Artifact bytes differ: ' + name)
    return path


def npm_inputs(root: Path, manifest: dict, platform: str, *, previous=False, expected_binary=None) -> tuple[Path, Path]:
    kinds = ('npm', 'npm') if previous else ('npm-meta', 'npm-platform')
    platforms = ('all', platform) if previous else (None, platform)
    selected = []
    for kind, target in zip(kinds, platforms):
        records = [a for a in manifest['artifacts'] if a['kind'] == kind and a.get('platform') == target]
        pub.require(len(records) == 1, 'Exactly one npm artifact per role is required')
        selected.append(artifact(root, records[0], previous=previous))
    meta = pub.archive_members(selected[0])
    native = (pub.archive_members(selected[1]) if previous else
              {name: value[0] for name, value in pub.read_sdk_archive(selected[1], manifest)['files'].items()})
    for members in (meta, native):
        pub.require('package/package.json' in members, 'Missing npm package metadata')
    root_package, platform_package = [json.loads(m['package/package.json'], object_pairs_hook=pub.object_pairs) for m in (meta, native)]
    version = manifest['version']
    source = manifest['source'] if previous else manifest['source']['revision']
    pub.require(root_package.get('name') == platform_package.get('name') == '@openprose/prose-cli' and root_package.get('version') == version and platform_package.get('version') == package_local.npm_payload_version(version, platform), 'Original same-name npm identities differ')
    for record in (root_package, platform_package):
        pub.require(not record.get('scripts') and record.get('openproseCohort', {}).get('version') == version and record['openproseCohort'].get('sourceRevision') == source, 'npm source/cohort or lifecycle differs')
    alias = '@openprose/prose-cli-' + platform
    pub.require(root_package.get('optionalDependencies', {}).get(alias) == 'npm:@openprose/prose-cli@' + platform_package['version'], 'npm platform alias differs')
    pub.require('package/bin/prose.js' in meta and 'package/bin/prose' in native, 'Missing npm launcher/native bytes')
    if expected_binary is not None:
        pub.require(hashlib.sha256(native['package/bin/prose']).hexdigest() == expected_binary, 'npm native payload differs from verified standalone')
    return tuple(selected)


def extract_standalone(archive: Path, directory: Path, *, sdk: bool, manifest=None) -> dict:
    """Extract only verified regular payloads, never archive metadata or links."""
    pub.require(not directory.exists() and not directory.is_symlink(), 'Installation must be fresh')
    table = None
    if sdk:
        pub.require(isinstance(manifest, dict), 'SDK installation requires bound production manifest')
        table = pub.read_sdk_archive(archive, manifest)
        kernel_rc_evidence.validate_sdk_archive_table(manifest, table)
        members = {name: value[0] for name, value in table['files'].items()}
    else:
        # Explicit published RC3 extraction retains its original regular-only policy.
        members = pub.archive_members(archive)
    if sdk:
        prefix = kernel_rc_evidence.sdk_archive_prefix(table)
    else:
        executables = [name for name in members if Path(name).name == 'prose']
        pub.require(len(executables) == 1 and len(Path(executables[0]).parts) == 2, 'Unique package-root CLI required')
        prefix = executables[0].rsplit('/', 1)[0] + '/'
    names = ('prose',)
    pub.require(all(prefix + name in members for name in names), 'Required executable siblings missing')
    directory.mkdir(parents=True)
    receipt = {}
    for name in names:
        data = members[prefix + name]
        path = directory / name
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o755 if name in ('prose', 'prose-agents-sdk') else 0o644)
        receipt[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'byteLength': len(data)}
    if sdk:
        kernel_rc_evidence.materialize_sdk_members(manifest, table, directory)
        receipt.update(installed_sdk_identity(directory, manifest['agentsSdk']))
    return receipt


def installed_sdk_identity(directory: Path, sdk_record: dict) -> dict:
    """Authenticate the full installed Mac tree; Linux keeps its frozen trio."""
    identities = {}
    for name, key in zip(SDK_NAMES, ('sha256', 'receiptSha256', 'noticesSha256')):
        path = directory / name
        regular(path)
        pub.require(pub.digest(path) == sdk_record[key], 'Installed SDK sibling bytes differ')
        identities[name] = {'sha256': sdk_record[key], 'byteLength': path.stat().st_size}
    receipt_path = directory / SDK_NAMES[1]
    pub.require(0 < receipt_path.stat().st_size <= 2 * 1024 * 1024, 'Installed SDK receipt exceeds bound')
    receipt = json.loads(receipt_path.read_bytes(), object_pairs_hook=pub.object_pairs)
    pub.require(isinstance(receipt, dict), 'Installed SDK receipt must be an object')
    if receipt.get('platform') == 'darwin':
        payload = receipt.get('payload')
        sdk_inventory.read_macos_payload(directory, payload, receipt.get('architecture'))
        identities['supportTree'] = {'payload': payload,
            'sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'byteLength': payload['totalRegularBytes'], 'entryCount': len(payload['entries'])}
    return identities


def validate_payload_evidence(records, evidence, platform):
    """Validate captured receipt references; this does not re-execute native proof."""
    pub.require(isinstance(records, list) and 0 < len(records) <= 32, 'Installed payload record bound differs')
    mac = isinstance(platform, str) and platform.startswith('darwin-')
    pub.require(isinstance(evidence, dict) and (len(evidence) == 1 if mac else not evidence), 'SDK evidence dictionary differs')
    receipts = {}
    for digest, encoded in evidence.items():
        pub.require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
                    and isinstance(encoded, str), 'SDK evidence key/value differs')
        raw = encoded.encode('utf-8')
        pub.require(0 < len(raw) <= 2 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == digest,
                    'SDK evidence receipt digest/length differs')
        receipt = json.loads(raw, object_pairs_hook=pub.object_pairs)
        pub.require(isinstance(receipt, dict) and receipt.get('schema') == 'openprose.agents-sdk-build/1'
                    and receipt.get('platform') == 'darwin', 'SDK evidence platform differs')
        pub.require(receipt.get('architecture') == {'darwin-arm64': 'arm64', 'darwin-x64': 'x86_64'}.get(platform), 'SDK evidence architecture differs from qualification platform')
        sdk_inventory.validate_macos_payload_structure(receipt['payload'], receipt['architecture'])
        receipts[digest] = receipt
    referenced = set()
    for record in records:
        pub.require(isinstance(record, dict) and set(record) == {'directory', 'members', 'before', 'after'}
                    and record['before'] == record['after'], 'Installed payload record shape/equality differs')
        pub.require(isinstance(record['directory'], str) and record['directory']
                    and not Path(record['directory']).is_absolute() and '..' not in Path(record['directory']).parts,
                    'Installed payload directory differs')
        identity = record['before']
        pub.require(isinstance(identity, dict) and set(identity) == set(SDK_NAMES) | ({'supportTree'} if mac else set()),
                    'Installed SDK identity shape differs')
        pub.require(isinstance(record['members'], dict) and set(record['members']) == set(identity) | {'prose'}
                    and all(record['members'][key] == value for key, value in identity.items()),
                    'Installed member identities differ')
        for name in ('prose', *SDK_NAMES):
            row = record['members'][name]
            pub.require(isinstance(row, dict) and set(row) == {'sha256', 'byteLength'}
                        and isinstance(row['sha256'], str) and re.fullmatch('[0-9a-f]{64}', row['sha256'])
                        and type(row['byteLength']) is int and 0 < row['byteLength'] <= 256 * 1024 * 1024,
                        'Installed sibling identity differs')
        if mac:
            tree = identity['supportTree']
            pub.require(isinstance(tree, dict) and set(tree) == {'sha256', 'byteLength', 'entryCount', 'receiptSha256'},
                        'Installed support reference shape differs')
            digest = tree['receiptSha256']; pub.require(isinstance(digest, str) and digest in receipts, 'Missing SDK receipt reference')
            referenced.add(digest); receipt = receipts[digest]; payload = receipt['payload']
            expected = {'sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                        'byteLength': payload['totalRegularBytes'], 'entryCount': len(payload['entries']), 'receiptSha256': digest}
            pub.require(tree == expected and all(type(tree[key]) is int for key in ('byteLength', 'entryCount')),
                        'Installed support identity differs from receipt')
            pub.require(identity['agents-sdk-build.json'] == {'sha256': digest, 'byteLength': len(evidence[digest].encode())}
                        and identity['prose-agents-sdk'] == {key: receipt['helper'][key] for key in ('sha256', 'byteLength')}
                        and identity['AGENTS-SDK-NOTICES.txt'] == {key: receipt['notices'][key] for key in ('sha256', 'byteLength')},
                        'Installed SDK sibling receipt bindings differ')
    pub.require(referenced == set(evidence), 'Unreferenced SDK evidence')
    return records


class Qualification:
    def __init__(self, output: Path, node: Path, npm: Path):
        pub.require(not output.exists() and not output.is_symlink(), 'Output must be fresh')
        regular(node); regular(npm)
        pub.require(os.access(node, os.X_OK), 'Node must be executable')
        self.output, self.node, self.npm = output.absolute(), node, npm
        output.mkdir(parents=True)
        self.logs = output / 'logs'; self.logs.mkdir()
        self.tools = output / 'node-only'; self.tools.mkdir()
        (self.tools / 'node').symlink_to(node)
        self.commands = []; self.checks = []; self.payloads = []; self.sdk_evidence = {}

    def environment(self, root: Path, *, npm=False, explicit_config=False) -> dict[str, str]:
        # A boundary marker stops project configuration discovery at this
        # isolated consumer root even when evidence lives beneath a checkout.
        # It contains no Git history or source files and never invokes Git.
        boundary = root / '.git'; boundary.mkdir(exist_ok=True)
        home = root / 'home'; home.mkdir(parents=True, exist_ok=True)
        temporary = root / 'tmp'; temporary.mkdir(exist_ok=True)
        env = {'HOME': str(home), 'TMPDIR': str(temporary), 'LANG': 'C', 'LC_ALL': 'C', 'PATH': str(self.tools) if npm else ''}
        if explicit_config:
            config = root / 'preferences'; config.mkdir(exist_ok=True)
            env['PROSE_CONFIG_DIR'] = str(config)
        if npm:
            userconfig = root / 'user.npmrc'; userconfig.write_text('')
            globalconfig = root / 'global.npmrc'; globalconfig.write_text('')
            env.update(npm_config_userconfig=str(userconfig), npm_config_globalconfig=str(globalconfig), npm_config_cache=str(root / 'npm-cache'), npm_config_registry='http://127.0.0.1:9', npm_config_offline='true', npm_config_update_notifier='false', npm_config_script_shell='/bin/sh')
        return env

    def execute(self, argv, *, env, cwd, label, expected=0, timeout=120):
        args = [str(x) for x in argv]
        if args[0] == 'npm':
            args = [str(self.node), str(self.npm), *args[1:]]
        result = package_local.run_bounded(args, cwd=cwd, environment=env, timeout_seconds=timeout, label=label)
        record = {'argv': args, 'exitCode': result.returncode, 'stdout': result.stdout.decode('utf-8', errors='strict'), 'stderr': result.stderr.decode('utf-8', errors='strict')}
        path = self.logs / (str(len(self.commands)).zfill(3) + '-' + label + '.json')
        path.write_text(json.dumps(record, sort_keys=True) + '\n')
        self.commands.append({'label': label, 'path': str(path.relative_to(self.output)), 'sha256': pub.digest(path), 'byteLength': path.stat().st_size})
        pub.require(result.returncode == expected, 'Unexpected exit for ' + label + '; inspect ' + str(path))
        return record

    def npm_command(self, args, *, env, cwd, log, timeout=120):
        return self.execute(args, env=env, cwd=cwd, label=log.stem, timeout=timeout)

    def json_command(self, command, args, root, env, label, expected=0):
        result = self.execute([*command, *args], cwd=root, env=env, label=label, expected=expected)
        pub.require(result['stderr'] == '', 'Unexpected diagnostic stderr for ' + label)
        return json.loads(result['stdout'], object_pairs_hook=pub.object_pairs)

    def identity(self, command, runner, version, source, root, env, label):
        banner = self.execute([*command, '--version'], cwd=root, env=env, label=label + '-version')
        pub.require(banner['stdout'].strip() == f'prose {version} ({runner})' and not banner['stderr'], 'Installed version/runner mismatch')
        report = self.json_command(command, ['--output=json', 'cli', 'doctor'], root, env, label + '-doctor', 10)
        pub.require(report.get('runner') == {'name': runner, 'version': version, 'commit': source}, 'Installed source identity mismatch')
        pub.require(report.get('build', {}).get('profile') == 'release' and report['build'].get('testSeamsEnabled') is False, 'Production release without seams required')
        return report

    def defaults(self, command, runner, version, source, root, env, label):
        doctor = self.identity(command, runner, version, source, root, env, label)
        pub.require(doctor.get('selectedHarness') == 'agents-sdk' and doctor.get('selectedTransport') == 'jsonl', 'SDK must be the installed default')
        auth = doctor.get('problems', [])
        pub.require(len(auth) == 1 and auth[0].get('code') == 'HARNESS_NEEDS_AUTH' and auth[0].get('action') == SETUP_ACTION and auth[0].get('details') == {'adapterId': 'agents-sdk/jsonl', 'authProfile': 'openai-api-key', 'fallbackAttempted': False}, 'Missing-key doctor differs')
        explain = self.json_command(command, ['cli', 'config', 'explain', '--json', '--', 'run', 'missing.prose.md'], root, env, label + '-explain')
        for key, value in {'harness': 'agents-sdk', 'model': 'gpt-6.1-sol', 'authProfile': 'openai-api-key'}.items():
            pub.require(explain['values'][key]['value'] == value and explain['values'][key]['source']['kind'] == 'default', 'Defaults must inherit without writing overrides')
        path = Path(env.get('PROSE_CONFIG_DIR', str(Path(env['HOME']) / '.prose'))) / 'cli.toml'
        pub.require(not path.exists() and not path.is_symlink(), 'Read-only diagnostics persisted settings')
        missing = self.json_command(command, ['--output=json', 'run', 'missing.prose.md'], root, env, label + '-missing-key', 10)
        pub.require(missing.get('schema') == 'openprose.runner-error/1' and missing.get('code') == 'HARNESS_NEEDS_AUTH' and missing.get('action') == SETUP_ACTION, 'Missing-key run must fail before inference/image acquisition')
        dry = self.json_command(command, ['--output=json', '--dry-run', 'run', 'missing.prose.md'], root, env, label + '-dry-run', 10)
        pub.require(dry.get('schema') == 'openprose.runner-dry-run-report/1' and dry.get('blockingError', {}).get('code') == 'HARNESS_NEEDS_AUTH', 'Installed default dry-run must diagnose absent key')
        ready = self.json_command(command, ['--output=json', '--dry-run', 'run', 'missing.prose.md'], root, dict(env, OPENAI_API_KEY=AUTH_CANARY), label + '-helper-discovery')
        selection = ready.get('selection', {})
        pub.require(ready.get('schema') == 'openprose.runner-dry-run-report/1' and ready.get('wouldStartModel') is False and ready.get('readiness') == 'ready' and ready.get('blockingError') is None and ready.get('billingOwner') == 'user-provider', 'Synthetic-key dry-run must be ready without inference')
        pub.require(all(selection.get(key) == value for key, value in {'harness': 'agents-sdk', 'adapterId': 'agents-sdk/jsonl', 'transport': 'jsonl', 'runtimeVersion': 'prose-agents-sdk 0.1.0', 'model': 'gpt-6.1-sol'}.items()), 'Installed CLI did not discover its packaged SDK sibling')
        pub.require(not path.exists(), 'Dry-run persisted settings')
        self.checks.append({'name': label, 'status': 'pass', 'modelCalls': 0, 'settingsPath': str(path)})

    def save_preferences(self, command, root, env, label):
        path = Path(env.get('PROSE_CONFIG_DIR', str(Path(env['HOME']) / '.prose'))) / 'cli.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'# retained preference\ntimeout = "1m"\n')
        self.json_command(command, ['--output=json', 'cli', 'harness', 'use', 'claude', '--model', 'claude-sonnet-4-6', '--auth-profile', 'anthropic-api-key'], root, env, label + '-save')
        data = path.read_bytes()
        pub.require(b'# retained preference' in data and b'timeout = "1m"' in data, 'Preference writer lost unrelated settings')
        return path, data

    def preferences(self, command, root, env, label, saved):
        path, data = saved
        pub.require(path.read_bytes() == data, 'Upgrade changed explicit preferences')
        report = self.json_command(command, ['--output=json', 'cli', 'config', 'explain'], root, env, label + '-saved-explain')
        for key, value in {'harness': 'claude', 'model': 'claude-sonnet-4-6', 'authProfile': 'anthropic-api-key', 'timeout': '1m'}.items():
            pub.require(report['values'][key]['value'] == value and report['values'][key]['source']['kind'] == 'user-config', 'Saved alternative or override lost')
        override = self.json_command(command, ['cli', 'config', 'explain', '--json', '--', '--harness', 'agents-sdk', '--model', 'gpt-6.1-sol', '--auth-profile', 'openai-api-key', 'run', 'missing.prose.md'], root, env, label + '-explicit-override')
        pub.require(override['values']['harness']['value'] == 'agents-sdk' and override['values']['harness']['source']['kind'] == 'flag' and path.read_bytes() == data, 'Explicit override persisted or failed')

    def payload(self, directory, expected_binary, sdk_record):
        regular(directory / 'prose')
        pub.require(pub.digest(directory / 'prose') == expected_binary, 'Installed CLI bytes differ')
        before = installed_sdk_identity(directory, sdk_record)
        for argument, expected in (('--packaged-self-test', kernel_rc_evidence.SDK_IMPORT_TEST), ('--packaged-tool-self-test', kernel_rc_evidence.SDK_TOOL_TEST)):
            env = {'PATH': '', 'HOME': str(self.output), 'TMPDIR': str(self.output), 'LANG': 'C', 'LC_ALL': 'C'}
            record = self.execute([directory / 'prose-agents-sdk', argument], env=env, cwd=self.output, label=directory.parent.name + '-' + argument.lstrip('-'), timeout=45)
            pub.require(not record['stderr'] and json.loads(record['stdout']) == expected, 'Installed SDK self-test differs')
        after = installed_sdk_identity(directory, sdk_record)
        pub.require(before == after and pub.digest(directory / 'prose') == expected_binary,
                    'Installed bytes changed during probes')
        if 'supportTree' in after:
            encoded = (directory / 'agents-sdk-build.json').read_text('utf-8')
            digest = sdk_record['receiptSha256']
            pub.require(digest not in self.sdk_evidence or self.sdk_evidence[digest] == encoded, 'Installed SDK receipt identity conflicts')
            self.sdk_evidence[digest] = encoded
            for identity in (before, after):
                identity['supportTree'] = {key: value for key, value in identity['supportTree'].items() if key != 'payload'}
                identity['supportTree']['receiptSha256'] = digest
        identities = {'prose': {'sha256': expected_binary, 'byteLength': (directory / 'prose').stat().st_size}, **after}
        self.payloads.append({'directory': str(directory.relative_to(self.output)), 'members': identities,
                              'before': before, 'after': after})



def qualify(candidate_root, previous_release, output, source, version, node, npm):
    """All writes and installations are beneath fresh output; prior bytes stay original."""
    verified, manifest, archives = custody.verify_kernel_rc(candidate_root, source, version)
    kernel_rc_evidence.validate_sdk_archives(manifest, lambda name: pub.read_sdk_archive(candidate_root / 'package' / name, manifest))
    previous = custody.verify_previous_release(previous_release, manifest['platform'], version)
    previous_plan = custody.read_previous_manifest(previous_release)
    pub.require(pub.digest(previous_release / 'manifest.json') == PREVIOUS_MANIFEST_SHA256, 'Previous manifest is not the pinned published release')
    pub.require(rc_version(version) > rc_version(previous_plan['version']), 'A genuine version upgrade is required')
    pub.require(manifest['platform'] == package_local.current_platform_id(), 'Candidate must match the native host')
    candidate_npm = npm_inputs(candidate_root / 'package', manifest, manifest['platform'], expected_binary=verified['packageIdentity']['bunBinarySha256'])
    previous_npm = npm_inputs(previous_release, previous_plan, manifest['platform'], previous=True, expected_binary=previous['binaryHashes']['bun'])
    q = Qualification(output, node, npm)
    inputs = [candidate_root / 'package' / a['path'] for a in manifest['artifacts']] + [candidate_root / 'package/release-manifest.json', candidate_root / 'build-report.json', previous_release / 'manifest.json', *previous_npm, node, npm]
    inputs += [previous_release / a['name'] for a in previous['archives'].values()]
    input_hashes = {str(path): pub.digest(path) for path in inputs}
    for runner in ('bun', 'rust'):
        root = output / ('standalone-' + runner); root.mkdir()
        env = q.environment(root, explicit_config=runner == 'rust')
        fresh = root / 'fresh'
        extract_standalone(candidate_root / 'package' / archives[runner]['path'], fresh, sdk=True, manifest=manifest)
        q.payload(fresh, verified['packageIdentity'][runner + 'BinarySha256'], manifest['agentsSdk'])
        q.defaults([fresh / 'prose'], runner, version, source, root, env, runner + '-fresh')
        relocated = root / 'relocated'; fresh.rename(relocated)
        q.payload(relocated, verified['packageIdentity'][runner + 'BinarySha256'], manifest['agentsSdk'])
        q.defaults([relocated / 'prose'], runner, version, source, root, env, runner + '-relocated')
        saved = q.save_preferences([relocated / 'prose'], root, env, runner)
        upgrade = root / 'upgrade'
        extract_standalone(previous_release / previous['archives'][runner]['name'], upgrade, sdk=False)
        pub.require(pub.digest(upgrade / 'prose') == previous['binaryHashes'][runner], 'Installed previous bytes differ')
        q.identity([upgrade / 'prose'], runner, previous_plan['version'], previous_plan['source'], root, env, runner + '-previous')
        shutil.rmtree(upgrade)
        extract_standalone(candidate_root / 'package' / archives[runner]['path'], upgrade, sdk=True, manifest=manifest)
        q.payload(upgrade, verified['packageIdentity'][runner + 'BinarySha256'], manifest['agentsSdk'])
        q.identity([upgrade / 'prose'], runner, version, source, root, env, runner + '-upgraded')
        q.preferences([upgrade / 'prose'], root, env, runner + '-upgraded', saved)
        q.payload(upgrade, verified['packageIdentity'][runner + 'BinarySha256'], manifest['agentsSdk'])
        shutil.rmtree(upgrade); shutil.rmtree(relocated)
        pub.require(saved[0].read_bytes() == saved[1], 'Standalone uninstall removed user settings')
        q.checks.append({'name': runner + '-upgrade-uninstall', 'status': 'pass', 'settingsSha256': hashlib.sha256(saved[1]).hexdigest()})
    root = output / 'npm'; root.mkdir()
    env = q.environment(root, npm=True, explicit_config=True)
    prefix = root / 'prefix'
    def install(packages, label, sdk_manifest):
        return npm_alias_install.install(*packages, prefix, env=env, cwd=root, command=q.npm_command, log=q.logs / (label + '.log'), sdk_manifest=sdk_manifest)
    installs = [install(candidate_npm, 'npm-fresh', manifest)]
    launcher = prefix / 'bin/prose'
    def npm_payload():
        native = list((prefix / 'lib/node_modules').rglob('prose-agents-sdk'))
        pub.require(len(native) == 1, 'Exactly one installed npm SDK helper is required')
        q.payload(native[0].parent, verified['packageIdentity']['bunBinarySha256'], manifest['agentsSdk'])
        meta = pub.archive_members(candidate_npm[0])
        pub.require(pub.digest(launcher.resolve(strict=True)) == hashlib.sha256(meta['package/bin/prose.js']).hexdigest(), 'Installed npm launcher differs')
    npm_payload()
    q.defaults([node, launcher], 'bun', version, source, root, env, 'npm-fresh')
    saved = q.save_preferences([node, launcher], root, env, 'npm')
    q.execute(['npm', 'uninstall', '--global', '--prefix', prefix, '--offline', '--ignore-scripts', '--no-audit', '--no-fund', '@openprose/prose-cli'], env=env, cwd=root, label='npm-remove-fresh')
    installs.append(install(previous_npm, 'npm-previous', None))
    q.identity([node, launcher], 'bun', previous_plan['version'], previous_plan['source'], root, env, 'npm-previous')
    installs.append(install(candidate_npm, 'npm-upgrade', manifest))
    npm_payload()
    q.identity([node, launcher], 'bun', version, source, root, env, 'npm-upgraded')
    q.preferences([node, launcher], root, env, 'npm-upgraded', saved)
    npm_payload()
    q.execute(['npm', 'uninstall', '--global', '--prefix', prefix, '--offline', '--ignore-scripts', '--no-audit', '--no-fund', '@openprose/prose-cli'], env=env, cwd=root, label='npm-uninstall')
    pub.require(saved[0].read_bytes() == saved[1] and not launcher.exists(), 'npm uninstall changed settings or left launcher')
    q.checks.append({'name': 'npm-genuine-upgrade-uninstall', 'status': 'pass', 'settingsSha256': hashlib.sha256(saved[1]).hexdigest()})
    root = output / 'npx'; root.mkdir()
    env = q.environment(root, npm=True)
    # Populate only the exact platform package using the established offline
    # installer; npm exec itself installs the original root tarball in _npx.
    install_record = npm_alias_install.install(*candidate_npm, root / 'cache-primer-prefix', env=env, cwd=root, command=q.npm_command, log=q.logs / 'npx-prime.log', sdk_manifest=manifest)
    env = offline_registry_environment(env, install_record)
    shutil.rmtree(root / 'cache-primer-prefix')
    command = [node, npm, 'exec', '--offline', '--yes', '--ignore-scripts', '--no-audit', '--no-fund', '--package', candidate_npm[0], '--', 'prose']
    q.defaults(command, 'bun', version, source, root, env, 'npx-cache')
    cache_launchers = list((root / 'npm-cache' / '_npx').rglob('@openprose/prose-cli/bin/prose.js'))
    pub.require(len(cache_launchers) == 1, 'Actual npx cache launcher is required')
    cache_launcher = cache_launchers[0]
    meta = pub.archive_members(candidate_npm[0])
    pub.require(pub.digest(cache_launcher) == hashlib.sha256(meta['package/bin/prose.js']).hexdigest(), 'npx launcher bytes differ')
    helpers = list((root / 'npm-cache' / '_npx').rglob('prose-agents-sdk'))
    pub.require(len(helpers) == 1, 'Actual npx cache SDK helper required')
    q.payload(helpers[0].parent, verified['packageIdentity']['bunBinarySha256'], manifest['agentsSdk'])
    saved = q.save_preferences(command, root, env, 'npx')
    q.preferences(command, root, env, 'npx-saved', saved)
    q.payload(helpers[0].parent, verified['packageIdentity']['bunBinarySha256'], manifest['agentsSdk'])
    shutil.rmtree(root / 'npm-cache' / '_npx')
    pub.require(saved[0].read_bytes() == saved[1], 'npx cache removal changed settings')
    q.checks.append({'name': 'npx-cache-removal-settings', 'status': 'pass', 'settingsSha256': hashlib.sha256(saved[1]).hexdigest()})
    for path, digest in input_hashes.items():
        pub.require(pub.digest(Path(path)) == digest, 'Original qualification input changed')
    report = {'schema': 'openprose.installed-sdk-qualification/1', 'status': 'pass', 'sourceRevision': source, 'version': version, 'platform': manifest['platform'], 'candidate': verified, 'previous': {'version': previous_plan['version'], 'source': previous_plan['source'], 'manifestSha256': PREVIOUS_MANIFEST_SHA256}, 'inputSha256': input_hashes, 'node': {'path': str(node), 'sha256': pub.digest(node)}, 'npm': {'path': str(npm), 'sha256': pub.digest(npm)}, 'checks': q.checks, 'commands': q.commands, 'installedPayloads': q.payloads, 'npmInstallations': [*installs, install_record], 'modelCalls': 0, 'networkScope': 'loopback npm cache priming and public published-kernel retrieval only; no provider credentials', 'networkIsolation': 'not-enforced', 'publicationAuthorized': False, 'globalUserStateModified': False}
    validate_payload_evidence(q.payloads, q.sdk_evidence, manifest['platform'])
    if q.sdk_evidence:
        report['sdkEvidence'] = q.sdk_evidence
    encoded = json.dumps(report, indent=2, sort_keys=True).encode('utf-8') + b'\n'
    pub.require(len(encoded) <= 16 * 1024 * 1024, 'Installed qualification report exceeds evidence bound')
    (output / 'qualification.json').write_bytes(encoded)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-root', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--previous-release', type=Path, required=True)
    parser.add_argument('--previous-manifest-sha256', choices=[PREVIOUS_MANIFEST_SHA256], default=PREVIOUS_MANIFEST_SHA256)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--npm', type=Path, required=True, help='Exact npm-cli.js file, invoked by explicit Node')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = qualify(args.candidate_root.absolute(), args.previous_release.absolute(), args.out.absolute(), args.source, args.version, args.node.resolve(strict=True), args.npm.resolve(strict=True))
        print(json.dumps({'status': report['status'], 'report': str(args.out.absolute() / 'qualification.json')}, sort_keys=True))
    except (ValueError, OSError, KeyError, package_local.PackageError) as error:
        parser.exit(2, str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
