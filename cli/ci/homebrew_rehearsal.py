#!/usr/bin/env python3
"""Test Homebrew against verified development rehearsal bytes; never publish."""
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

SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}')
SAFE_VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?')
PLATFORMS = {'darwin-arm64', 'darwin-x64', 'linux-arm64-gnu', 'linux-x64-gnu'}
TAP = 'openprose/cli-rehearsal'


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
        # Homebrew strips the package root and installs only the verified executable.
        with tarfile.open(archive, 'r:gz') as contents:
            members = contents.getmembers()
            for member in members:
                parts = Path(member.name).parts
                if member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):
                    raise ValueError('Unsafe archive member')
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
        '  def install', '    bin.install "prose"', '  end', '',
        '  test do', f'    assert_equal "prose {version} ({implementation})", shell_output("#{{bin}}/prose --version").strip',
        '  end', 'end', '',
    ])


def run(rehearsal: Path, output: Path, brew: str) -> dict[str, Any]:
    # Reuse exact-byte/conformance custody before executing anything from the archive.
    verified = rehearse_release.verify_rehearsal(rehearsal)
    manifest, archives = select_archives(rehearsal.resolve() / 'package')
    output.mkdir(exist_ok=False)
    output = output.resolve()
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'DEVELOPER_DIR', 'SDKROOT') if key in os.environ}
    env.update(HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1', HOMEBREW_NO_INSTALL_CLEANUP='1',
               HOMEBREW_CACHE=str(output / 'cache'), HOMEBREW_LOGS=str(output / 'logs'),
               HOMEBREW_TEMP=str(output / 'tmp'), XDG_CONFIG_HOME=str(output / 'trust'))
    for name in ('cache', 'logs', 'tmp', 'trust'):
        (output / name).mkdir()
    checks = []

    def command(label: str, argv: list[str], *, expected_failure: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=180)
        (output / f'{label}.log').write_text(result.stdout + result.stderr)
        checks.append({'name': label, 'exitCode': result.returncode, 'expectedFailure': expected_failure})
        if (result.returncode != 0) != expected_failure:
            raise ValueError(f'Homebrew check failed: {label}; inspect its retained log')
        return result

    prefix = Path(command('prefix', [brew, '--prefix']).stdout.strip())
    if TAP in command('existing-taps', [brew, 'tap']).stdout.splitlines():
        raise ValueError('Rehearsal refuses to replace an existing tap')
    installed = command('existing-installations', [brew, 'list', '--formula', '--versions']).stdout
    if any(line.split()[0] in {'prose-bun', 'prose-rust'} for line in installed.splitlines() if line.split()):
        raise ValueError('Rehearsal requires a fresh Homebrew installation without Prose packages')
    # Keep an existing command unchanged; CI runners should have no prefix/bin/prose.
    active = prefix / 'bin/prose'
    if active.exists() or active.is_symlink():
        raise ValueError('Rehearsal refuses to overwrite an existing prose command')
    tap = output / 'tap'
    (tap / 'Formula').mkdir(parents=True)
    for implementation, item in archives.items():
        (tap / 'Formula' / f'prose-{implementation}.rb').write_text(
            formula(manifest['version'], implementation, rehearsal.resolve() / 'package' / item['path'], item['sha256']))
    command('tap-init', ['git', '-C', str(tap), 'init', '-q'])
    command('tap-add', ['git', '-C', str(tap), 'add', 'Formula'])
    command('tap-commit', ['git', '-C', str(tap), '-c', 'user.name=Rehearsal', '-c', 'user.email=rehearsal@localhost', 'commit', '-qm', 'Local non-publishing formula rehearsal'])
    command('tap', [brew, 'tap', TAP, str(tap)])
    completed = False
    try:
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
            command(f'unlink-{implementation}', [brew, 'unlink', name])
        command('link-bun', [brew, 'link', f'{TAP}/prose-bun'])
        original = hashlib.sha256(active.read_bytes()).hexdigest()
        conflict = command('reject-overwrite', [brew, 'link', f'{TAP}/prose-rust'], expected_failure=True)
        if 'Could not symlink' not in conflict.stdout + conflict.stderr:
            raise ValueError('The rejected link did not report a symlink collision')
        if hashlib.sha256(active.read_bytes()).hexdigest() != original:
            raise ValueError('A failed link changed the active executable')
        command('switch-unlink-bun', [brew, 'unlink', f'{TAP}/prose-bun'])
        command('switch-link-rust', [brew, 'link', f'{TAP}/prose-rust'])
        if hashlib.sha256(active.read_bytes()).hexdigest() != verified['packageIdentity']['rustBinarySha256']:
            raise ValueError('Rust link switch did not select the verified executable')
        completed = True
    finally:
        # Remove only packages under the newly owned rehearsal tap.
        for implementation in ('bun', 'rust'):
            cleaned = subprocess.run([brew, 'uninstall', f'{TAP}/prose-{implementation}'], env=env, text=True, capture_output=True, timeout=180)
            (output / f'cleanup-{implementation}.log').write_text(cleaned.stdout + cleaned.stderr)
        untapped = subprocess.run([brew, 'untap', TAP], env=env, text=True, capture_output=True, timeout=180)
        (output / 'cleanup-tap.log').write_text(untapped.stdout + untapped.stderr)
    if TAP in command('remaining-taps', [brew, 'tap']).stdout.splitlines():
        raise ValueError('The rehearsal tap did not uninstall cleanly')
    remaining = command('remaining-installations', [brew, 'list', '--formula', '--versions']).stdout
    if any(line.split()[0] in {'prose-bun', 'prose-rust'} for line in remaining.splitlines() if line.split()):
        raise ValueError('The rehearsal packages did not uninstall cleanly')
    if active.exists() or active.is_symlink():
        raise ValueError('The rehearsal command did not uninstall cleanly')
    receipt = {'schema': 'openprose.homebrew-rehearsal/1', 'status': 'passed-development-packaging-check' if completed else 'failed',
               'version': manifest['version'], 'platform': manifest['platform'], 'packageIdentity': verified['packageIdentity'],
               'rehearsalManifestSha256': verified['manifestSha256'], 'checks': checks, 'modelCalls': 0,
               'publicationAuthorized': False, 'releaseQualification': False, 'uninstallPassed': True}
    (output / 'homebrew-rehearsal.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rehearsal', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--brew', default='brew')
    args = parser.parse_args()
    print(json.dumps(run(args.rehearsal, args.output, args.brew), indent=2))
