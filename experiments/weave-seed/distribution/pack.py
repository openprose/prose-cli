#!/usr/bin/env python3
"""Build an unpublished local review bundle; never install or publish anything."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

SEED = Path(__file__).resolve().parent.parent
REPOSITORY = SEED.parents[1]
DIRECTORIES = {'bun', 'local', 'integration', 'providers', 'getting-started', 'rust', 'rust-host', 'rust-binding', 'rust-local', 'fixtures', 'examples', 'distribution'}
TOP_FILES = {'README.md', 'HOST.md', 'SPEC.md', 'SDK.md', 'CONTRIBUTING.md', 'FEEDBACK.md', 'package.json', 'qualify.py'}
SUFFIXES = {'.mjs', '.js', '.ts', '.rs', '.md', '.json', '.toml', '.lock', '.tsv', '.py'}
EXCLUDED = {'target', 'node_modules', 'results', 'receipts', 'dist', 'build', '__pycache__'}
# Exact supporting files for the copied seed's existing links and Python quick start.
# No recursive CLI/harness code selection and no generated evidence or credentials.
SUPPORTING_FILES = (
    'docs/weave-v1-readiness.md', 'docs/kernel-startup.md',
    'cli/protocol/decisions/weave-host.md',
    'cli/shared/tests/weave_host_process.py',
    'cli/shared/tests/weave_host_fixture.py',
    'cli/shared/fixtures/weave-host-v1.json',
    'docs/api-credentials.md', 'docs/native-profiles.md',
    'cli/AGENTS.md', 'cli/CONTRIBUTING.md',
    'experiments/weave/README.md', 'experiments/weave/CASES.md',
    'experiments/weave/demo.py', 'experiments/weave/engine.py',
    'experiments/weave/evidence.py', 'experiments/weave/store.py',
    'experiments/weave/test_checkpoint_validation.py',
    'experiments/weave/test_engine.py', 'experiments/weave/test_evidence.py',
    'experiments/weave/test_recovery.py', 'experiments/weave/test_store.py',
)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def source_files(seed=SEED):
    selected = []
    for directory, children, names in os.walk(seed, followlinks=False):
        parent = Path(directory)
        children[:] = sorted(name for name in children if name not in EXCLUDED and not name.startswith('.') and not (parent / name).is_symlink() and (parent != seed or name in DIRECTORIES))
        for name in sorted(names):
            path = parent / name
            if path.is_symlink() or name.startswith('.') or not path.is_file():
                continue
            relative = path.relative_to(seed)
            if (parent == seed and name in TOP_FILES) or (parent != seed and path.suffix in SUFFIXES):
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError('source file exceeds private bundle bound')
                selected.append(relative)
    return sorted(selected)

def copy_sources(destination, seed=SEED):
    for relative in source_files(seed):
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed / relative, output)

def copy_supporting_files(destination, repository=REPOSITORY):
    """Copy only named reference docs and the deterministic Python reference."""
    for relative in SUPPORTING_FILES:
        source = repository / relative
        if any((repository / Path(*Path(relative).parts[:i])).is_symlink()
               for i in range(1, len(Path(relative).parts) + 1)):
            raise ValueError('supporting source must not be a symlink')
        if not source.is_file() or source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('required supporting source unavailable or oversized')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

def run(argv, *, cwd=None, timeout=300, environment=None):
    return subprocess.run([str(a) for a in argv], cwd=cwd, env=environment, check=True,
                          capture_output=True, text=True, timeout=timeout).stdout

def provenance(repository=REPOSITORY):
    # Read-only Git diagnostics. Do not store a diff or source contents in metadata.
    try:
        commit = run(['git', '-C', repository, 'rev-parse', 'HEAD']).strip()
        status = run(['git', '-C', repository, 'status', '--porcelain=v1', '--untracked-files=all'])
        return {'commit': commit, 'dirty': bool(status), 'statusPorcelain': status}
    except (OSError, subprocess.SubprocessError):
        return {'commit': None, 'dirty': None, 'statusPorcelain': None}

def inventory(root):
    return {str(p.relative_to(root)): {'sha256': sha(p), 'bytes': p.stat().st_size}
            for p in sorted(root.rglob('*')) if p.is_file() and p != root / 'manifest.json'}

def verify(root):
    expected = json.loads((root / 'manifest.json').read_text())['files']
    actual = inventory(root)
    if actual != expected:
        raise ValueError('bundle file identity mismatch')
    return True

def smoke(bundle, bun):
    """Copy away from build root, generate paths from the copy, execute both sidecars."""
    with tempfile.TemporaryDirectory(prefix='weave-bundle-smoke-') as temp:
        moved = Path(temp) / 'relocated'
        shutil.copytree(bundle, moved)
        results = []
        for name in ('weave-bun', 'weave-rust'):
            binary = moved / 'bin' / name
            run([binary, '--help'], environment={})
            root = Path(temp) / name
            created = json.loads(run([bun, '--no-env-file', moved / 'source/experiments/weave-seed/getting-started/create.mjs', root], environment={}))
            config = Path(created['config'])
            config_data = json.loads(config.read_text())
            for role in ('assessor', 'actor'):
                assert str(moved / 'source') in config_data[role][2]
                assert str(SEED) not in config_data[role][2]
            check = json.loads(run([binary, 'check', config], environment={}))
            assert check['status'] == 'configured' and check['providerVerified'] is False
            status = json.loads(run([binary, 'status', config], environment={}))
            assert status['checkpoint'] is None and not (root / 'host').exists()
            first = json.loads(run([binary, 'step', config], environment={}))
            second = json.loads(run([binary, 'step', config], environment={}))
            assert first['status'] == 'satisfied' and first['attempts'] == 1
            assert second['status'] == 'reused' and second['attempts'] == 1
            assert (root / 'source.txt').read_bytes() == (root / 'report.txt').read_bytes()
            results.append({'binary': name, 'help': True, 'check': check['status'], 'initialStatus': 'absent', 'step': first, 'repeat': second, 'sourceMapping': 'relocated bundle only', 'childEnvironment': 'empty', 'synthetic': True})
        return results

README = '''# Private local review bundle

For a stable private installation, follow
source/experiments/weave-seed/distribution/INSTALL.md using the reviewed manifest digest.
Install before generating subject configuration; upgrades use a separate destination.

UNPUBLISHED review candidate, not a public release or installed Prose command.
Current build/smoke qualification is macOS arm64 only. Check manifest.json for
actual platform, executable/source hashes, source commit and dirty status.

bin/weave-bun and bin/weave-rust are standalone local coordinator sidecars.
They need no Bun process for their own help/check/status/step/serve operations;
your explicitly selected assessor/actor executables still need their runtimes.
The copied synthetic fixtures, Jev/native actor adapters and configuration helper
require an explicitly installed Bun. Native acting additionally requires a
separate actual Prose CLI and admitted Agents SDK harness. No login service,
public package install, provider calls, sentinel image, PATH edit or network
publication is included. source/ contains allowlisted seed source, the deterministic
Python reference, selected supporting documentation and LICENSE. Native CLI
implementation and harness installation sources are not included.

Start with bin/weave-bun --help or bin/weave-rust --help. To create an offline
example with paths referring to THIS copy, run:

  /absolute/bun --no-env-file /absolute/bundle/source/experiments/weave-seed/getting-started/create.mjs /absolute/new-subject

Then run the included binaries directly (replace the absolute placeholders):

  /absolute/bundle/bin/weave-bun check /absolute/new-subject/config.json
  /absolute/bundle/bin/weave-bun step /absolute/new-subject/config.json
  /absolute/bundle/bin/weave-rust step /absolute/new-subject/config.json
  /absolute/bundle/bin/weave-rust serve /absolute/new-subject/config.json --poll-ms 250 --max-steps 3

The first step repairs once; the second reuses the fresh result across runtimes.
The generated subject README also shows the equivalent Bun source commands.
Both entry points use the same configuration and checkpoint.
For commands in the copied source guides, the repository root is bundle/source:

  cd /absolute/bundle/source

Those guides use paths starting experiments/weave-seed/ or experiments/weave/.
The standalone binaries remain one directory above, at ../bin/weave-*.
Do not use example paths generated before relocating the bundle; generate a
fresh subject afterwards. See source/experiments/weave-seed/getting-started/README.md and
source/experiments/weave-seed/getting-started/BYOK.md for source/runtime requirements and explicit user
credentials. No model or network call is made by the synthetic fixture.

The embedded coordinator code is built from the copied sources. SHA256 inventory
is integrity evidence, not independent authentication. manifest.json is unsigned.
Private review recipients must compare its hash through their trusted channel.
No production, all-platform, sandbox, distributed ownership or semantic correctness
claim follows from the included synthetic smoke. Both sidecars retain conservative
pending-effect recovery; never delete state/locks to bypass uncertain effects.
'''

def pack(bun, cargo, output):
    for executable in (bun, cargo):
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('explicit absolute executable required')
    if not output.is_absolute():
        raise ValueError('absolute fresh output directory required')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('current review bundle qualification supports macOS arm64 only')
    output.mkdir(mode=0o700)  # Exclusive claim. Never reuse or clean someone else's directory.
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'CARGO_HOME', 'RUSTUP_HOME', 'TMPDIR', 'LANG', 'SDKROOT') if key in os.environ}
    try:
        source = output / 'source/experiments/weave-seed'; source.mkdir(parents=True)
        copy_sources(source)
        copy_supporting_files(output / 'source')
        shutil.copyfile(REPOSITORY / 'LICENSE', output / 'LICENSE')
        shutil.copyfile(REPOSITORY / 'LICENSE', output / 'source/LICENSE')
        (output / 'README.md').write_text(README)
        binaries = output / 'bin'; binaries.mkdir()
        bun_version = run([bun, '--version'], environment=environment).strip()
        cargo_version = run([cargo, '--version'], environment=environment).strip()
        run([bun, '--no-env-file', 'build', '--compile', '--outfile', binaries / 'weave-bun', source / 'local/run.mjs'], cwd=source, environment=environment)
        with tempfile.TemporaryDirectory(prefix='weave-rust-build-') as temp:
            build_env = {**environment, 'CARGO_TARGET_DIR': temp}
            run([cargo, 'build', '--offline', '--locked', '--release', '--bin', 'weave-rust-local', '--manifest-path', source / 'rust-local/Cargo.toml'], cwd=source, environment=build_env)
            shutil.copy2(Path(temp) / 'release/weave-rust-local', binaries / 'weave-rust')
        results = smoke(output, bun)
        (output / 'smoke.json').write_text(json.dumps(results, indent=2) + '\n')
        manifest = {'schema': 'openprose.private-local-bundle/1', 'published': False, 'qualification': 'synthetic macOS arm64 only', 'platform': {'system': platform.system(), 'machine': platform.machine()}, 'tools': {'bunVersion': bun_version, 'cargoVersion': cargo_version}, 'source': provenance(), 'files': inventory(output)}
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        verify(output)
        return {'directory': str(output), 'manifestSha256': sha(output / 'manifest.json'), 'artifacts': {name: sha(binaries / name) for name in ('weave-bun', 'weave-rust')}, 'smoke': results}
    except BaseException:
        # Keep partial output for diagnosis; subsequent attempts require a fresh directory.
        raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bun', type=Path, required=True)
    parser.add_argument('--cargo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(pack(args.bun, args.cargo, args.output), indent=2))
    except Exception as error:
        parser.exit(1, f'Private bundle failed: {type(error).__name__}; partial fresh output retained.\n')
