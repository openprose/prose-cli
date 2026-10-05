#!/usr/bin/env python3
"""Build and install an unsigned RC with published-kernel startup, without model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
RC = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc\.(0|[1-9][0-9]*)')
MAX_BYTES = 512 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def environment(output, ambient):
    """Keep tool caches, but exclude credentials, user npm config and image overrides."""
    env = {k: ambient[k] for k in ('PATH', 'DEVELOPER_DIR', 'SDKROOT') if k in ambient}
    home = output / 'home'; home.mkdir()
    temporary = output / 'tmp'; temporary.mkdir()
    original_home = Path(ambient.get('HOME', str(home)))
    env.update(HOME=str(home), TMPDIR=str(temporary), LANG='C.UTF-8',
               CARGO_HOME=ambient.get('CARGO_HOME', str(original_home / '.cargo')),
               RUSTUP_HOME=ambient.get('RUSTUP_HOME', str(original_home / '.rustup')),
               CARGO_NET_OFFLINE='true', npm_config_offline='true',
               npm_config_registry='http://127.0.0.1:9',
               npm_config_userconfig=str(output / 'empty-npmrc'),
               npm_config_cache=str(output / 'npm-cache'),
               BUN_CONFIG_NO_CLEAR_TERMINAL='1')
    (output / 'empty-npmrc').write_text('')
    return env


def command(args, *, env, cwd, log, timeout=1200):
    with log.open('wb') as stream:
        result = subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    require(result.returncode == 0, 'Command failed; inspect ' + str(log))


def executable_tool(name, env):
    selected = shutil.which(name, path=env.get('PATH'))
    require(selected, name + ' required')
    resolved = Path(selected).resolve(strict=True)
    require(resolved.is_file() and os.access(resolved, os.X_OK), name + ' must resolve to an executable file')
    return resolved


def prepare_macos_binary(binary, env, logs):
    # Rust's Intel linker need not emit the ad-hoc signature that ARM64 gets.
    # This supplies Mach-O integrity only, without a trusted signing identity.
    command(['/usr/bin/codesign', '--force', '--sign', '-', binary], env=env,
            cwd=ROOT, log=logs / 'rust-ad-hoc-sign.log', timeout=60)
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', binary], env=env,
            cwd=ROOT, log=logs / 'rust-ad-hoc-verify.log', timeout=60)


def extract_binary(archive, output):
    """Extract only one regular executable; reject unsafe archive metadata first."""
    with tarfile.open(archive) as source:
        members = source.getmembers()
        require(len(members) <= 100, 'Too many archive members')
        names = set(); binaries = []; total = 0
        for member in members:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and '..' not in name.parts and '\\' not in member.name,
                    'Unsafe archive path')
            require(member.name not in names and member.isfile(), 'Unsupported archive member')
            names.add(member.name); total += member.size
            require(0 <= member.size <= MAX_BYTES and total <= MAX_BYTES, 'Archive exceeds size limit')
            if name.name == 'prose':
                binaries.append(member)
        require(len(binaries) == 1 and binaries[0].mode & 0o111, 'Missing unique executable')
        output.parent.mkdir(parents=True, exist_ok=False)
        with source.extractfile(binaries[0]) as src, output.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        output.chmod(0o755)
    return output


def verified_artifacts(package):
    manifest = json.loads((package / 'release-manifest.json').read_text())
    require(manifest.get('mode') == 'kernel-rc', 'Wrong package mode')
    artifacts = manifest['artifacts']
    require(len(artifacts) == 4, 'Expected two standalone and two npm artifacts')
    names = set()
    for item in artifacts:
        name = item['path']
        require(isinstance(name, str) and Path(name).name == name and name not in ('.', '..')
                and name not in names, 'Unsafe or duplicate artifact path')
        names.add(name); path = package / name
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MAX_BYTES,
                'Invalid artifact file')
        require(path.stat().st_size == item['byteLength'] and digest(path) == item['sha256'],
                'Artifact bytes changed')
    require(sorted((a['kind'], a['implementation']) for a in artifacts) ==
            [('npm-meta', 'bun'), ('npm-platform', 'bun'), ('standalone-archive', 'bun'), ('standalone-archive', 'rust')],
            'Incomplete implementation coverage')
    return manifest


def build(version, output):
    require(RC.fullmatch(version) is not None, 'Use an exact X.Y.Z-rc.N version')
    require(not output.exists() and not output.is_symlink(), 'Output must be fresh')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(re.fullmatch('[0-9a-f]{40}', revision) is not None, 'Invalid source revision')
    require(not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT),
            'Tracked source must be clean')
    epoch = subprocess.check_output(['git', 'show', '-s', '--format=%ct', 'HEAD'], cwd=ROOT, text=True).strip()
    output.mkdir(parents=True); logs = output / 'logs'; logs.mkdir()
    env = environment(output, os.environ)
    env.update(OPENPROSE_BUILD_COMMIT=revision, OPENPROSE_BUILD_VERSION=version,
               OPENPROSE_REQUIRE_RELEASE_IMAGE='1', CARGO_TARGET_DIR=str(output / 'cargo-target'))
    binaries = output / 'binaries'; binaries.mkdir()
    command(['bun', '--no-env-file', 'scripts/image-bundle.ts', 'build', '--require-release-eligible',
             '--outfile', binaries / 'prose-bun'], env=env, cwd=ROOT / 'cli/bun', log=logs / 'build-bun.log')
    command(['cargo', 'build', '--manifest-path', ROOT / 'cli/rust/Cargo.toml', '--release', '--locked',
             '--offline', '--bin', 'prose'], env=env, cwd=ROOT, log=logs / 'build-rust.log')
    shutil.copy2(output / 'cargo-target/release/prose', binaries / 'prose-rust')
    if sys.platform == 'darwin':
        prepare_macos_binary(binaries / 'prose-rust', env, logs)
    def check(binary, runner, label, node=None):
        command([sys.executable, ROOT / 'cli/ci/check_published_release.py', '--binary', binary,
                 '--runner', runner, '--commit', revision, '--version', version,
                 *(['--node', node] if node else [])], env=env, cwd=ROOT,
                log=logs / (label + '.json'), timeout=90)
    for runner in ('bun', 'rust'):
        check(binaries / ('prose-' + runner), runner, 'built-' + runner)
    package = output / 'package'
    args = [sys.executable, ROOT / 'cli/ci/package_local.py', '--mode', 'kernel-rc', '--publication-platforms',
            'posix-four', '--version', version, '--source-revision', revision, '--source-date-epoch', epoch,
            '--rust-binary', binaries / 'prose-rust', '--bun-binary', binaries / 'prose-bun',
            '--image-manifest', ROOT / 'cli/shared/image/echo-v0/manifest.json', '--out', package]
    if sys.platform.startswith('linux'):
        args += ['--readelf', executable_tool('readelf', env)]
    command(args, env=env, cwd=ROOT, log=logs / 'package.log')
    manifest = verified_artifacts(package)
    for item in manifest['artifacts']:
        if item['kind'] == 'standalone-archive':
            runner = item['implementation']
            binary = extract_binary(package / item['path'], output / 'installed' / runner / 'prose')
            check(binary, runner, 'installed-' + runner)
    prefix = output / 'npm-prefix'
    from npm_alias_install import install as install_alias
    meta = next(package / a['path'] for a in manifest['artifacts'] if a['kind'] == 'npm-meta')
    platform_package = next(package / a['path'] for a in manifest['artifacts'] if a['kind'] == 'npm-platform')
    alias_install = install_alias(meta, platform_package, prefix, env=env, cwd=output,
                                  command=command, log=logs / 'npm-install.log')
    (logs / 'npm-alias-install.json').write_text(json.dumps(alias_install, sort_keys=True) + '\n')
    node = shutil.which('node', path=env.get('PATH')); require(node, 'Node is required for npm launcher')
    check(prefix / 'bin/prose', 'bun', 'installed-npm', node)
    report = {'schema': 'openprose.kernel-rc-build/1', 'version': version, 'sourceRevision': revision,
              'platform': manifest['platform'], 'imageSource': 'published-on-run', 'testSeamsEnabled': False,
              'signing': 'unsigned', 'modelCalls': 0, 'kernelFetches': 0,
              'networkIsolation': 'not-enforced', 'qualification': 'offline-install-only',
              'publicationAuthorized': False, 'checks': [{'name': name, 'status': 'passed'} for name in ['built-bun', 'built-rust', 'installed-bun', 'installed-rust', 'installed-npm']],
              'evidence': {str(p.relative_to(output)): {'sha256': digest(p), 'byteLength': p.stat().st_size} for directory in (logs, package)
                           for p in sorted(directory.rglob('*')) if p.is_file()}}
    (output / 'build-report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.version, args.out.absolute()), sort_keys=True))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(2, str(error) + '\n')
