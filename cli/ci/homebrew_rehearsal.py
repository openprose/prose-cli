#!/usr/bin/env python3
"""Test Homebrew against verified development or native RC bytes; never publish."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from typing import Any

import rehearse_release
import kernel_rc_evidence as custody
import publication as pub

SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}')
SAFE_VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?')
PLATFORMS = {'darwin-arm64', 'darwin-x64', 'linux-arm64-gnu', 'linux-x64-gnu'}
TAP = 'openprose/cli-rehearsal'
PREVIOUS_VERSION = '0.15.0-rc.3'
PREVIOUS_MANIFEST_SHA256 = '571eb285f964ea980d4e3aec6aa574f782b9d51f2a6934d219bc2ae7d8755e4f'


def read_previous_manifest(root: Path) -> dict[str, Any]:
    """Read the unchanged, independently pinned published RC3 manifest."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError('A regular previous release directory is required')
    path = root / 'manifest.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('A regular bounded previous manifest is required')
    if pub.digest(path) != PREVIOUS_MANIFEST_SHA256:
        raise ValueError('Previous published manifest identity mismatch')
    manifest = pub.read_json(path)
    if manifest.get('schema') != 'openprose.cli-distribution/1' or manifest.get('version') != PREVIOUS_VERSION:
        raise ValueError('Expected the pinned published RC3 manifest')
    return manifest


def verify_previous_release(root: Path, platform: str, candidate_version: str) -> dict[str, Any]:
    previous = read_previous_manifest(root)
    match = re.fullmatch(r'0\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc\.(0|[1-9][0-9]*)', candidate_version)
    if match is None or tuple(map(int, match.groups())) <= (15, 0, 3):
        raise ValueError('The candidate must be a newer RC than the genuine previous release')
    if platform not in PLATFORMS:
        raise ValueError('Unsupported previous native platform')
    archives = {}
    binary_hashes = {}
    for item in previous.get('artifacts', []):
        if item.get('kind') != 'standalone' or item.get('platform') != platform:
            continue
        implementation, name = item.get('implementation'), item.get('name')
        if implementation not in {'bun', 'rust'} or implementation in archives:
            raise ValueError('Exactly one previous archive per implementation is required')
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name) or '..' in name:
            raise ValueError('Unsafe previous archive name')
        archive = root / name
        if archive.is_symlink() or not archive.is_file():
            raise ValueError('A regular previous archive is required')
        if type(item.get('size')) is not int or not 0 < item['size'] <= pub.MAX_BYTES or archive.stat().st_size != item['size'] or pub.digest(archive) != item.get('sha256'):
            raise ValueError('Previous archive identity mismatch')
        members = pub.archive_members(archive)
        binaries = [data for path, data in members.items() if Path(path).name == 'prose' and len(Path(path).parts) == 2]
        if len(binaries) != 1:
            raise ValueError('Previous archive requires one prose executable')
        archives[implementation] = dict(item, path=name, byteLength=item['size'])
        binary_hashes[implementation] = hashlib.sha256(binaries[0]).hexdigest()
    if set(archives) != {'bun', 'rust'}:
        raise ValueError('Both previous native implementations are required')
    return {'version': PREVIOUS_VERSION, 'manifestSha256': PREVIOUS_MANIFEST_SHA256,
            'root': root.resolve(), 'archives': archives, 'binaryHashes': binary_hashes}


def sdk_context(manifest: dict[str, Any], verified: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Keep the explicit development placeholder distinct from a real SDK."""
    if 'agentsSdk' in manifest:
        try:
            sdk = rehearse_release._sdk_release_identity(verified or {}, manifest)
        except rehearse_release.RehearsalError as error:
            raise ValueError(str(error)) from error
    else:
        if manifest.get('mode') in ('release', 'kernel-rc'):
            raise ValueError('Production release lacks an SDK identity')
        sdk = None
    if sdk is None:
        for context in (manifest, verified or {}):
            if any(name in context for name in ('sdkBuild', 'sdkEvidence', 'sdkSourceCustody')):
                raise ValueError('SDK-absent Homebrew context has SDK evidence')
    return sdk


def select_archives(package: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if package.is_symlink() or not package.is_dir():
        raise ValueError('A regular verified package directory is required')
    path = package / 'release-manifest.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('A regular package manifest is required')
    manifest = json.loads(path.read_text())
    if manifest.get('schema') != 'openprose.local-release-manifest/1':
        raise ValueError('Unsupported package manifest')
    if not isinstance(manifest.get('version'), str) or not SAFE_VERSION.fullmatch(manifest['version']):
        raise ValueError('Unsafe formula version')
    if manifest.get('platform') not in PLATFORMS:
        raise ValueError('Unsupported native platform')
    sdk = sdk_context(manifest)
    selected = {}
    for item in manifest.get('artifacts', []):
        if item.get('kind') != 'standalone-archive':
            continue
        implementation = item.get('implementation')
        name = item.get('path')
        if implementation not in {'bun', 'rust'} or implementation in selected:
            raise ValueError('Exactly one native archive per implementation is required')
        if item.get('platform') != manifest['platform']:
            raise ValueError('Mixed native archive platforms')
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name) or '..' in name:
            raise ValueError('Unsafe archive name')
        archive = package / name
        if archive.is_symlink() or not archive.is_file():
            raise ValueError('A regular native archive is required')
        if type(item.get('byteLength')) is not int or not 0 < item['byteLength'] <= 512 * 1024 * 1024:
            raise ValueError('Invalid archive byte limit')
        if archive.stat().st_size != item['byteLength'] or hashlib.sha256(archive.read_bytes()).hexdigest() != item.get('sha256'):
            raise ValueError('Native archive identity mismatch')
        if sdk is not None:
            table = pub.read_sdk_archive(archive, manifest)
            custody.validate_sdk_archive_table(manifest, table)
            cli_path = custody.sdk_archive_prefix(table) + 'prose'
            if cli_path not in table['files'] or len(Path(cli_path).parts) != 2:
                raise ValueError('Archive requires one prose executable under one package root')
        else:
            with tarfile.open(archive, 'r:gz') as contents:
                members = contents.getmembers()
                for member in members:
                    parts = Path(member.name).parts
                    if member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):
                        raise ValueError('Unsafe archive member')
                    if any(part in {'prose-agents-sdk', 'prose-agents-sdk-runtime',
                                    'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'} for part in parts):
                        raise ValueError('SDK-absent archive contains SDK members')
                executables = [m for m in members if Path(m.name).name == 'prose' and m.isfile()]
                if len(executables) != 1 or len(Path(executables[0].name).parts) != 2:
                    raise ValueError('Archive requires one prose executable under one package root')
        selected[implementation] = item
    if set(selected) != {'bun', 'rust'}:
        raise ValueError('Both native implementations are required')
    return manifest, selected


def formula(version: str, implementation: str, archive: Path, sha256: str) -> str:
    return '\n'.join([
        f'class Prose{implementation.capitalize()} < Formula',
        '  desc "Development-only Prose packaging rehearsal"',
        '  homepage "https://prose.md"', f'  version "{version}"', '  license "MIT"',
        f'  url "{archive.as_uri()}"', f'  sha256 "{sha256}"', '',
        '  preserve_rpath',
        '  skip_clean "bin/prose", "bin/prose-agents-sdk", "bin/prose-agents-sdk-runtime"', '',
        '  def install', '    bin.install "prose"',
        '    bin.install "prose-agents-sdk" if File.exist?("prose-agents-sdk")',
        '    bin.install "prose-agents-sdk-runtime" if File.directory?("prose-agents-sdk-runtime")',
        '    pkgshare.install "agents-sdk-build.json", "AGENTS-SDK-NOTICES.txt" if File.exist?("agents-sdk-build.json")',
        '  end', '',
        '  test do', f'    assert_equal "prose {version} ({implementation})", shell_output("#{{bin}}/prose --version").strip',
        '  end', 'end', '',
    ])


def verify_kernel_rc(root: Path, expected_source: str, expected_version: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Check offline build custody, without executing or qualifying the candidate."""
    if not isinstance(expected_source, str) or not re.fullmatch(r'[0-9a-f]{40}', expected_source):
        raise ValueError('An exact 40-hex expected source is required')
    if not isinstance(expected_version, str) or not re.fullmatch(r'0\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc\.(0|[1-9][0-9]*)', expected_version):
        raise ValueError('An explicit expected 0.x RC version is required')
    if root.is_symlink() or not root.is_dir():
        raise ValueError('A regular native RC root is required')
    root = root.resolve()
    for name in ('package', 'logs'):
        if (root / name).is_symlink() or not (root / name).is_dir():
            raise ValueError('Regular native RC evidence directories are required')
    report = pub.read_json(root / 'build-report.json')
    custody.require(report.get('sourceRevision') == expected_source and report.get('version') == expected_version,
                    'Native report differs from expected source/version')
    pub.read_json(root / 'package/release-manifest.json')  # Reject duplicate JSON keys before selection.
    manifest, archives = select_archives(root / 'package')
    artifacts = manifest['artifacts']
    names = {item['path'] for item in artifacts}
    custody.require(len(names) == len(artifacts), 'Duplicate native artifact paths')
    evidence = report.get('evidence', {})
    custody.require(isinstance(evidence, dict) and set(custody.CHECK_PATHS).union({'package/release-manifest.json'}).issubset(evidence),
                    'Required native evidence is missing')
    for relative, record in evidence.items():
        custody.asset_name(manifest['platform'], relative, names)
        path = root / relative
        custody.require(isinstance(record, dict) and set(record) == {'sha256', 'byteLength'}
                        and type(record['byteLength']) is int and 0 <= record['byteLength'] <= pub.MAX_BYTES
                        and isinstance(record['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
                        and path.is_file() and not path.is_symlink()
                        and path.stat().st_size == record['byteLength'] and pub.digest(path) == record['sha256'],
                        'Native evidence identity mismatch: ' + relative)
    custody.validate_producer_command_logs(report, manifest, lambda relative: custody.read_command_log_bytes(root / relative))
    for item in artifacts:
        name = item['path']
        custody.require(isinstance(name, str) and SAFE_NAME.fullmatch(name) and '..' not in name,
                        'Unsafe native artifact path')
        record = evidence.get('package/' + name, {})
        custody.require(record.get('sha256') == item.get('sha256') and record.get('byteLength') == item.get('byteLength')
                        and type(item.get('byteLength')) is int and item['byteLength'] > 0,
                        'Native artifact is not bound to report evidence')
    hashes = {}
    for implementation, item in archives.items():
        if 'agentsSdk' in manifest:
            table = pub.read_sdk_archive(root / 'package' / item['path'], manifest)
            custody.validate_sdk_archive_table(manifest, table)
            members = {name: data for name, (data, _) in table['files'].items()}
        else:
            members = pub.archive_members(root / 'package' / item['path'])
        binaries = ([members[custody.sdk_archive_prefix(table) + 'prose']] if 'agentsSdk' in manifest
                    else [data for name, data in members.items() if Path(name).name == 'prose'])
        custody.require(len(binaries) == 1, 'A unique native binary is required')
        hashes[(implementation, manifest['platform'])] = hashlib.sha256(binaries[0]).hexdigest()
    platform_artifact = next((a for a in artifacts if a.get('kind') == 'npm-platform'), None)
    custody.require(platform_artifact is not None, 'Missing npm native artifact')
    if 'agentsSdk' in manifest:
        npm_table = pub.read_sdk_archive(root / 'package' / platform_artifact['path'], manifest)
        custody.validate_sdk_archive_table(manifest, npm_table)
        npm_members = {name: data for name, (data, _) in npm_table['files'].items()}
    else:
        npm_members = pub.archive_members(root / 'package' / platform_artifact['path'])
    custody.require('package/bin/prose' in npm_members
                    and hashlib.sha256(npm_members['package/bin/prose']).hexdigest() == hashes[('bun', manifest['platform'])],
                    'npm native binary differs from standalone archive')
    meta = next((a for a in artifacts if a.get('kind') == 'npm-meta'), None)
    custody.require(meta is not None, 'Missing npm launcher artifact')
    members = pub.archive_members(root / 'package' / meta['path'])
    custody.require('package/bin/prose.js' in members, 'Missing npm launcher')
    launcher_hash = hashlib.sha256(members['package/bin/prose.js']).hexdigest()
    checks = {name: pub.read_json(root / 'logs' / (name + '.json')) for name in custody.CHECKS}
    custody.validate_native(report, manifest, checks, hashes, launcher_hash)
    if 'agentsSdk' in manifest:
        custody.require(custody.SDK_PAYLOAD_EVIDENCE in evidence, 'Missing complete installed SDK payload evidence')
        custody.validate_installed_sdk_payloads(pub.read_json(root / custody.SDK_PAYLOAD_EVIDENCE, max_bytes=16*1024*1024), manifest, npm_table)
        custody.validate_sdk_producer_evidence(manifest, npm_table, evidence, lambda relative: (root / relative).read_bytes())
    custody.validate_linux_runtime_evidence(report, manifest, npm_table if 'agentsSdk' in manifest else None,
        lambda relative: custody.read_command_log_bytes(root / relative),
        expected_sources=custody.read_linux_runtime_sources(Path(__file__).resolve().parents[2])
            if manifest['platform'].startswith('linux-') else None)
    # Original build trees retain these files; uploaded artifact trees retain the
    # built probes instead. Both bind to the exact packaged executable hashes.
    binaries = root / 'binaries'
    if binaries.exists() or binaries.is_symlink():
        custody.require(binaries.is_dir() and not binaries.is_symlink(), 'Invalid compiled binary directory')
        for implementation in ('bun', 'rust'):
            path = binaries / ('prose-' + implementation)
            custody.require(path.is_file() and not path.is_symlink()
                            and pub.digest(path) == hashes[(implementation, manifest['platform'])],
                            'Compiled binary differs from native archive')
    verified = {'packageIdentity': {implementation + 'BinarySha256': hashes[(implementation, manifest['platform'])]
                                  for implementation in ('bun', 'rust')},
                'manifestSha256': pub.digest(root / 'package/release-manifest.json'),
                'source': expected_source, 'custodyKind': 'kernel-rc-native',
                'nativeReportSha256': pub.digest(root / 'build-report.json'),
                'archiveIdentities': [{key: item[key] for key in ('kind', 'implementation', 'platform', 'path', 'sha256', 'byteLength')}
                                      for item in artifacts]}
    if 'agentsSdk' in manifest:
        verified['sdkBuild'] = json.loads(npm_table['files'][custody.sdk_archive_prefix(npm_table) + 'agents-sdk-build.json'][0])
    return verified, manifest, archives


def run(rehearsal: Path, output: Path, brew: str) -> dict[str, Any]:
    verified = rehearse_release.verify_rehearsal(rehearsal)
    verified = dict(verified, custodyKind='development-rehearsal')
    manifest, archives = select_archives(rehearsal.resolve() / 'package')
    return exercise(rehearsal.resolve() / 'package', manifest, archives, verified, output, brew)


def run_kernel_rc(root: Path, output: Path, brew: str, expected_source: str, expected_version: str,
                  previous_release: Path | None = None) -> dict[str, Any]:
    verified, manifest, archives = verify_kernel_rc(root, expected_source, expected_version)
    previous = verify_previous_release(previous_release, manifest['platform'], manifest['version']) if previous_release else None
    if previous and not isinstance(manifest.get('agentsSdk'), dict):
        raise ValueError('Genuine SDK upgrade qualification requires a packaged SDK candidate')
    return exercise(root.resolve() / 'package', manifest, archives, verified, output, brew, previous=previous)


def qualify_installed_sdk(active: Path, manifest: dict[str, Any], verified: dict[str, Any],
                          command: Any, label: str, *, expected_timeout: str = '10m') -> dict[str, Any]:
    """Probe the real installed default; synthetic-key dry runs never infer."""
    helper = active.resolve(strict=True).parent / 'prose-agents-sdk'
    sdk = manifest.get('agentsSdk')
    if not isinstance(sdk, dict) or not helper.is_file() or helper.is_symlink() or pub.digest(helper) != sdk.get('sha256'):
        raise ValueError('Homebrew SDK helper differs from the verified package')
    payload_view = None
    build = verified.get('sdkBuild')
    if manifest['platform'].startswith('darwin'):
        import sdk_native_inventory as native
        if not isinstance(build, dict) or not isinstance(build.get('payload'), dict):
            raise ValueError('Homebrew Mac SDK requires a verified complete payload')
        payload_view = native.read_macos_payload(helper.parent, build['payload'], build['architecture'])
    command(label + '-sdk-imports', [str(helper), '--packaged-self-test'])
    command(label + '-sdk-tools', [str(helper), '--packaged-tool-self-test'])
    def report(suffix: str, argv: list[str], code: int = 0, additions: dict[str, str] | None = None) -> dict[str, Any]:
        result = command(label + suffix, [str(active), *argv], expected_code=code, additions=additions)
        if result.stderr:
            raise ValueError('Installed SDK probe emitted unexpected diagnostics')
        return json.loads(result.stdout)
    explanation = report('-default-explain', ['cli', 'config', 'explain', '--json', '--', 'run', 'input.prose.md'])
    if explanation.get('schema') != 'openprose.configuration-explanation/1' or explanation.get('diagnostics'):
        raise ValueError('Installed default explanation failed')
    for key, value in (('harness', 'agents-sdk'), ('model', 'gpt-6.1-sol'), ('authProfile', 'openai-api-key')):
        if explanation.get('values', {}).get(key, {}).get('value') != value:
            raise ValueError('Installed SDK default selection differs')
    if explanation.get('values', {}).get('timeout', {}).get('value') != expected_timeout:
        raise ValueError('Installed SDK explanation did not inherit the expected user settings')
    missing = report('-missing-key', ['--output', 'json', 'run', 'input.prose.md'], 10)
    if missing.get('schema') != 'openprose.runner-error/1' or missing.get('code') != 'HARNESS_NEEDS_AUTH' or missing.get('details', {}).get('fallbackAttempted') is not False:
        raise ValueError('Installed missing-key preflight did not fail safely')
    # This is an inert canary, never an account credential. Only --dry-run gets it.
    dry = report('-default-dry-run', ['--output', 'json', '--dry-run', 'run', 'input.prose.md'],
                 additions={'OPENAI_API_KEY': 'provider-free-installation-canary'})
    if dry.get('schema') != 'openprose.runner-dry-run-report/1' or dry.get('wouldStartModel') is not False or dry.get('readiness') != 'ready':
        raise ValueError('Installed SDK dry run did not establish provider-free readiness')
    selection = dry.get('selection', {})
    if any(selection.get(key) != value for key, value in (('harness', 'agents-sdk'), ('adapterId', 'agents-sdk/jsonl'),
                                                          ('transport', 'jsonl'), ('runtimeVersion', 'prose-agents-sdk 0.1.0'), ('model', 'gpt-6.1-sol'))):
        raise ValueError('Installed command did not discover the packaged SDK default')
    if dry.get('billingOwner') != 'user-provider' or dry.get('blockingError') is not None:
        raise ValueError('Installed SDK dry run changed billing or reported a blocker')
    proof = {'stage': label, 'helperSha256': sdk['sha256'], 'receiptSha256': sdk['receiptSha256']}
    if payload_view is not None:
        if native.read_macos_payload(helper.parent, build['payload'], build['architecture']) != payload_view:
            raise ValueError('Homebrew SDK support tree changed during qualification')
        proof.update(layout=build['payload']['layout'], supportEntries=len(build['payload']['entries']),
                     totalRegularBytes=build['payload']['totalRegularBytes'], completePayloadVerified=True)
    return proof


def exercise(package: Path, manifest: dict[str, Any], archives: dict[str, Any], verified: dict[str, Any],
             output: Path, brew: str, *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    sdk = sdk_context(manifest, verified)
    output.mkdir(exist_ok=False)
    output = output.resolve()
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'DEVELOPER_DIR', 'SDKROOT') if key in os.environ}
    env.update(HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1', HOMEBREW_NO_INSTALL_CLEANUP='1',
               HOMEBREW_CACHE=str(output / 'cache'), HOMEBREW_LOGS=str(output / 'logs'),
               HOMEBREW_TEMP=str(output / 'tmp'), XDG_CONFIG_HOME=str(output / 'trust'),
               PROSE_CONFIG_DIR=str(output / 'user-settings'))
    for name in ('cache', 'logs', 'tmp', 'trust', 'user-settings'):
        (output / name).mkdir()
    checks = []
    sdk_payload_checks = []

    def command(label: str, argv: list[str], *, expected_failure: bool = False,
                expected_code: int = 0, additions: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(argv, env=dict(env, **(additions or {})), cwd=output,
                                text=True, capture_output=True, timeout=180)
        (output / f'{label}.log').write_text(result.stdout + result.stderr)
        checks.append({'name': label, 'exitCode': result.returncode, 'expectedFailure': expected_failure})
        if (expected_failure and result.returncode == 0) or (not expected_failure and result.returncode != expected_code):
            raise ValueError(f'Homebrew check failed: {label}; inspect its retained log')
        return result

    prefix = Path(command('prefix', [brew, '--prefix']).stdout.strip())
    if TAP in command('existing-taps', [brew, 'tap']).stdout.splitlines():
        raise ValueError('Rehearsal refuses to replace an existing tap')
    installed = command('existing-installations', [brew, 'list', '--formula', '--versions']).stdout
    if any(line.split()[0].split('/')[-1] in {'prose-bun', 'prose-rust'} for line in installed.splitlines() if line.split()):
        raise ValueError('Rehearsal requires a fresh Homebrew installation without Prose packages')
    # Keep an existing command unchanged; CI runners should have no prefix/bin/prose.
    active = prefix / 'bin/prose'
    if active.exists() or active.is_symlink():
        raise ValueError('Rehearsal refuses to overwrite an existing prose command')
    active_helper = prefix / 'bin/prose-agents-sdk'
    if sdk is not None and (active_helper.exists() or active_helper.is_symlink()):
        raise ValueError('Rehearsal refuses to overwrite an existing SDK helper command')
    active_support = prefix / 'bin/prose-agents-sdk-runtime'
    if sdk is not None and (active_support.exists() or active_support.is_symlink()):
        raise ValueError('Rehearsal refuses to overwrite an existing SDK support directory')
    tap = output / 'tap'
    (tap / 'Formula').mkdir(parents=True)
    for implementation, item in archives.items():
        (tap / 'Formula' / f'prose-{implementation}.rb').write_text(
            formula(manifest['version'], implementation, package / item['path'], item['sha256']))
    command('tap-init', ['git', '-C', str(tap), 'init', '-q'])
    command('tap-add', ['git', '-C', str(tap), 'add', 'Formula'])
    command('tap-commit', ['git', '-C', str(tap), '-c', 'user.name=Rehearsal', '-c', 'user.email=rehearsal@localhost', 'commit', '-qm', 'Local non-publishing formula rehearsal'])
    command('tap', [brew, 'tap', TAP, str(tap)])
    completed = False
    try:
        # A local tap is cloned by Homebrew. Upgrade the owned installed checkout,
        # not the separate source repository used when initially creating it.
        installed_tap = Path(command('installed-tap-repository', [brew, '--repo', TAP]).stdout.strip())
        if installed_tap.is_symlink() or not installed_tap.is_dir():
            raise ValueError('Homebrew did not create a regular owned tap checkout')
        for implementation in ('bun', 'rust'):
            installed_formula = installed_tap / 'Formula' / f'prose-{implementation}.rb'
            if installed_formula.is_symlink() or not installed_formula.is_file() or installed_formula.read_bytes() != (tap / 'Formula' / installed_formula.name).read_bytes():
                raise ValueError('Homebrew installed tap differs from the newly owned formulas')
        for implementation in ('bun', 'rust'):
            name = f'{TAP}/prose-{implementation}'
            command(f'install-{implementation}', [brew, 'install', '--build-from-source', name])
            command(f'test-{implementation}', [brew, 'test', name])
            result = command(f'version-{implementation}', [str(active), '--version'])
            if result.stdout.strip() != f'prose {manifest["version"]} ({implementation})':
                raise ValueError('Homebrew selected a different implementation or version')
            digest = hashlib.sha256(active.read_bytes()).hexdigest()
            if digest != verified['packageIdentity'][f'{implementation}BinarySha256']:
                raise ValueError('Homebrew executable differs from the verified candidate')
            if sdk is not None:
                sdk_payload_checks.append(qualify_installed_sdk(active, manifest, verified, command, 'fresh-' + implementation))
            command(f'unlink-{implementation}', [brew, 'unlink', name])
        command('link-bun', [brew, 'link', f'{TAP}/prose-bun'])
        original = hashlib.sha256(active.read_bytes()).hexdigest()
        def sdk_link_identity():
            if sdk is None:
                return None
            helper = active.resolve(strict=True).parent / 'prose-agents-sdk'
            if active_helper.resolve(strict=True) != helper or pub.digest(helper) != manifest['agentsSdk']['sha256']:
                raise ValueError('Active Homebrew SDK helper linkage differs from selected keg')
            identity = {'links': {}}
            for name, path in (('prose', active), ('helper', active_helper), ('support', active_support)):
                if not (path.exists() or path.is_symlink()):
                    identity['links'][name] = None
                else:
                    metadata = path.lstat()
                    identity['links'][name] = {'device': metadata.st_dev, 'inode': metadata.st_ino,
                        'mode': metadata.st_mode, 'mtimeNs': metadata.st_mtime_ns,
                        'target': os.readlink(path) if path.is_symlink() else None}
            if manifest['platform'].startswith('darwin'):
                import sdk_native_inventory as native
                build = verified['sdkBuild']
                native.read_macos_payload(helper.parent, build['payload'], build['architecture'])
                identity['payloadSha256'] = hashlib.sha256(json.dumps(build['payload'], sort_keys=True).encode()).hexdigest()
            return identity
        sdk_conflict_before = sdk_link_identity()
        conflict = command('reject-overwrite', [brew, 'link', f'{TAP}/prose-rust'], expected_failure=True)
        if 'Could not symlink' not in conflict.stdout + conflict.stderr:
            raise ValueError('The rejected link did not report a symlink collision')
        if hashlib.sha256(active.read_bytes()).hexdigest() != original:
            raise ValueError('A failed link changed the active executable')
        if sdk_link_identity() != sdk_conflict_before:
            raise ValueError('A failed link changed the active SDK linkage or payload')
        command('switch-unlink-bun', [brew, 'unlink', f'{TAP}/prose-bun'])
        command('switch-link-rust', [brew, 'link', f'{TAP}/prose-rust'])
        if hashlib.sha256(active.read_bytes()).hexdigest() != verified['packageIdentity']['rustBinarySha256']:
            raise ValueError('Rust link switch did not select the verified executable')
        if previous is not None:
            # Initial admission established exclusive ownership of both formulas.
            # Upgrade retains old kegs; ordinary uninstall removes only one version.
            def require_empty_owned_installation(label: str) -> None:
                installed = command(label, [brew, 'list', '--formula', '--versions']).stdout
                if any(line.split()[0].split('/')[-1] in {'prose-bun', 'prose-rust'} for line in installed.splitlines() if line.split()):
                    raise ValueError('Owned rehearsal kegs survived all-version uninstall')
                if any(path.exists() or path.is_symlink() for path in (active, active_helper, active_support)):
                    raise ValueError('Owned rehearsal commands survived all-version uninstall')
            command('uninstall-fresh-before-upgrade', [brew, 'uninstall', '--force', f'{TAP}/prose-bun', f'{TAP}/prose-rust'])
            require_empty_owned_installation('remaining-kegs-before-upgrades')
            settings = output / 'user-settings/cli.toml'
            settings_bytes = b'# explicit upgrade preservation\ntimeout = "9m"\n'
            settings.write_bytes(settings_bytes)
            def preserve(label: str) -> None:
                if settings.is_symlink() or not settings.is_file() or settings.read_bytes() != settings_bytes:
                    raise ValueError('Homebrew changed explicit user settings: ' + label)
                checks.append({'name': label, 'exitCode': 0, 'sha256': hashlib.sha256(settings_bytes).hexdigest()})
            for selected in ('bun', 'rust'):
                for implementation, item in previous['archives'].items():
                    (installed_tap / 'Formula' / f'prose-{implementation}.rb').write_text(
                        formula(previous['version'], implementation, previous['root'] / item['path'], item['sha256']))
                command(selected + '-install-base-bun', [brew, 'install', '--build-from-source', f'{TAP}/prose-bun'])
                command(selected + '-unlink-base-bun', [brew, 'unlink', f'{TAP}/prose-bun'])
                command(selected + '-install-base-rust', [brew, 'install', '--build-from-source', f'{TAP}/prose-rust'])
                if selected == 'bun':
                    command(selected + '-unlink-base-rust', [brew, 'unlink', f'{TAP}/prose-rust'])
                command(selected + '-link-base-selected', [brew, 'link', f'{TAP}/prose-{selected}'])
                if pub.digest(active) != previous['binaryHashes'][selected]:
                    raise ValueError('Selected prior executable differs from genuine published bytes')
                base_banner = command(selected + '-base-version', [str(active), '--version']).stdout.strip()
                if base_banner != f'prose {previous["version"]} ({selected})':
                    raise ValueError('The upgrade did not start from the genuine previous version')
                preserve(selected + '-settings-before-upgrade')
                for implementation, item in archives.items():
                    (installed_tap / 'Formula' / f'prose-{implementation}.rb').write_text(
                        formula(manifest['version'], implementation, package / item['path'], item['sha256']))
                other = 'rust' if selected == 'bun' else 'bun'
                command(selected + '-unlink-selected-before-upgrade', [brew, 'unlink', f'{TAP}/prose-{selected}'])
                command(selected + '-upgrade-inactive', [brew, 'upgrade', f'{TAP}/prose-{other}'])
                command(selected + '-test-upgraded-inactive', [brew, 'test', f'{TAP}/prose-{other}'])
                if pub.digest(active) != verified['packageIdentity'][other + 'BinarySha256']:
                    raise ValueError('Inactive implementation did not upgrade to exact candidate bytes')
                sdk_payload_checks.append(qualify_installed_sdk(active, manifest, verified, command, selected + '-upgraded-inactive', expected_timeout='9m'))
                preserve(selected + '-settings-after-inactive-upgrade')
                command(selected + '-unlink-upgraded-inactive', [brew, 'unlink', f'{TAP}/prose-{other}'])
                command(selected + '-upgrade-selected', [brew, 'upgrade', f'{TAP}/prose-{selected}'])
                command(selected + '-restore-selected-link', [brew, 'link', f'{TAP}/prose-{selected}'])
                command(selected + '-test-upgraded-selected', [brew, 'test', f'{TAP}/prose-{selected}'])
                selected_prefix = Path(command(selected + '-selected-prefix', [brew, '--prefix', f'{TAP}/prose-{selected}']).stdout.strip())
                if active.resolve() != (selected_prefix / 'bin/prose').resolve() or pub.digest(active) != verified['packageIdentity'][selected + 'BinarySha256']:
                    raise ValueError('Upgrade changed the selected implementation or candidate identity')
                sdk_payload_checks.append(qualify_installed_sdk(active, manifest, verified, command, selected + '-upgraded-selected', expected_timeout='9m'))
                preserve(selected + '-settings-after-selected-upgrade')
                command(selected + '-uninstall-upgraded', [brew, 'uninstall', '--force', f'{TAP}/prose-bun', f'{TAP}/prose-rust'])
                require_empty_owned_installation(selected + '-remaining-kegs-after-upgrade')
                preserve(selected + '-settings-after-uninstall')
                if active.exists() or active.is_symlink():
                    raise ValueError('Upgraded command survived uninstall')
        completed = True
    finally:
        # Remove only packages under the newly owned rehearsal tap.
        for implementation in ('bun', 'rust'):
            cleaned = subprocess.run([brew, 'uninstall', '--force', f'{TAP}/prose-{implementation}'], env=env, text=True, capture_output=True, timeout=180)
            (output / f'cleanup-{implementation}.log').write_text(cleaned.stdout + cleaned.stderr)
        untapped = subprocess.run([brew, 'untap', TAP], env=env, text=True, capture_output=True, timeout=180)
        (output / 'cleanup-tap.log').write_text(untapped.stdout + untapped.stderr)
    if TAP in command('remaining-taps', [brew, 'tap']).stdout.splitlines():
        raise ValueError('The rehearsal tap did not uninstall cleanly')
    remaining = command('remaining-installations', [brew, 'list', '--formula', '--versions']).stdout
    if any(line.split()[0].split('/')[-1] in {'prose-bun', 'prose-rust'} for line in remaining.splitlines() if line.split()):
        raise ValueError('The rehearsal packages did not uninstall cleanly')
    if active.exists() or active.is_symlink():
        raise ValueError('The rehearsal command did not uninstall cleanly')
    if sdk is not None and (active_helper.exists() or active_helper.is_symlink()):
        raise ValueError('The rehearsal SDK helper command did not uninstall cleanly')
    if sdk is not None and (active_support.exists() or active_support.is_symlink()):
        raise ValueError('The rehearsal SDK support directory did not uninstall cleanly')
    if previous:
        preserve('settings-after-final-cleanup')
    receipt = {'schema': 'openprose.homebrew-rehearsal/1', 'status': ('passed-native-rc-packaging-check' if verified['custodyKind'] == 'kernel-rc-native' else 'passed-development-packaging-check') if completed else 'failed',
               'version': manifest['version'], 'platform': manifest['platform'], 'packageIdentity': verified['packageIdentity'],
               'source': verified.get('source', verified['packageIdentity'].get('sourceRevision', manifest.get('source', {}).get('revision'))), 'custodyKind': verified['custodyKind'],
               'rehearsalManifestSha256': verified['manifestSha256'], 'checks': checks, 'modelCalls': 0,
               'publicationAuthorized': False, 'releaseQualification': False, 'uninstallPassed': True}
    if sdk_payload_checks:
        receipt['sdkPayloadChecks'] = sdk_payload_checks
        receipt['sdkLinkConflictPreserved'] = True
    receipt['upgradeQualification'] = 'passed-genuine-upgrade-both-selections' if previous else 'not-requested-fresh-install-only'
    receipt['kernelRetrieval'] = 'published-kernel-may-be-acquired-by-provider-free-dry-run' if sdk is not None else 'not-measured'
    if previous:
        receipt['previousRelease'] = {'version': previous['version'], 'manifestSha256': previous['manifestSha256'],
                                     'archives': list(previous['archives'].values())}
    if 'nativeReportSha256' in verified:
        receipt['nativeReportSha256'] = verified['nativeReportSha256']
        receipt['archiveIdentities'] = verified['archiveIdentities']
    (output / 'homebrew-rehearsal.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--rehearsal', type=Path)
    inputs.add_argument('--kernel-rc', type=Path)
    parser.add_argument('--expected-source')
    parser.add_argument('--expected-version')
    parser.add_argument('--previous-release', type=Path, help='Untouched pinned RC3 manifest.json and current-platform archives; enables genuine upgrades')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--brew', default='brew')
    args = parser.parse_args()
    if args.kernel_rc:
        if not args.expected_source or not args.expected_version:
            parser.error('--kernel-rc requires --expected-source and --expected-version')
        result = run_kernel_rc(args.kernel_rc, args.output, args.brew, args.expected_source, args.expected_version, args.previous_release)
    else:
        if args.expected_source or args.expected_version or args.previous_release:
            parser.error('Expected source/version and previous release are only supported with --kernel-rc')
        result = run(args.rehearsal, args.output, args.brew)
    print(json.dumps(result, indent=2))
