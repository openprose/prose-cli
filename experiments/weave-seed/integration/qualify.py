#!/usr/bin/env python3
"""Run explicit offline seed checks and write a local qualification record."""
import argparse
import datetime
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bun', default=shutil.which('bun'))
    parser.add_argument('--cargo', default=shutil.which('cargo'))
    parser.add_argument('--output', help='New local JSON report; existing files are not replaced')
    args = parser.parse_args()
    if not args.bun or not args.cargo:
        parser.error('Bun and Cargo are required; supply --bun and --cargo if not on PATH')
    bun, cargo = str(Path(args.bun).resolve()), str(Path(args.cargo).absolute())
    seed = Path(__file__).resolve().parent.parent
    repository = seed.parent.parent
    prefix = 'experiments/weave-seed/'
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'CARGO_HOME', 'RUSTUP_HOME') if k in os.environ}
    env['WEAVE_CARGO'] = cargo
    # Qualification also runs against an inventoried copied bundle. Do not leave
    # Python import caches beside its source files.
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    rows = []
    def check(name, command, timeout=180):
        start = time.monotonic()
        try:
            result = subprocess.run(command, cwd=repository, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            output, code = result.stdout, result.returncode
        except subprocess.TimeoutExpired:
            output, code = 'Qualification timeout; child outcome requires inspection.', -1
        # Test output only: no provider keys or parent credential environment are supplied.
        row = {'name': name, 'command': command, 'exitCode': code,
               'elapsedSeconds': round(time.monotonic()-start, 3), 'output': output[-65536:]}
        rows.append(row)
        print(('PASS' if code == 0 else 'FAIL') + ' ' + name, flush=True)
        return code == 0
    def snapshot():
        files = {}
        for path in sorted(seed.rglob('*')):
            relative = path.relative_to(seed)
            if not path.is_file() or path.is_symlink() or any(part.startswith('.') or part in ('target', 'node_modules', '__pycache__', 'dist', 'build', 'results', 'receipts') for part in relative.parts):
                continue
            if path.suffix in ('.mjs', '.js', '.py', '.rs', '.json', '.toml', '.lock', '.md', '.tsv'):
                files[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    source, dirty = None, None
    try:
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True, capture_output=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=repository, text=True, capture_output=True)
        if revision.returncode == status.returncode == 0:
            source, dirty = revision.stdout.strip(), bool(status.stdout)
    except FileNotFoundError:
        pass
    source_digest = snapshot()
    with tempfile.TemporaryDirectory(prefix='weave-qualification-') as temporary:
        target = Path(temporary)/'target'
        env['CARGO_TARGET_DIR'] = str(target)
        check('Bun version', [bun, '--version'])
        check('Cargo version', [cargo, '--version'])
        for name in ('rust', 'rust-host', 'rust-binding', 'rust-local'):
            check(name+' tests', [cargo, 'test', '--offline', '--locked', '--manifest-path', prefix+name+'/Cargo.toml'])
        for name in ('rust-host', 'rust-binding', 'rust-local'):
            check(name+' binary', [cargo, 'build', '--offline', '--locked', '--manifest-path', prefix+name+'/Cargo.toml'])
        for path in ('bun/conformance.mjs', 'bun/host.test.mjs', 'integration/binding.test.mjs',
                     'integration/config.test.mjs', 'integration/process.test.mjs', 'integration/run.test.mjs', 'local/coordinator.test.mjs', 'local/settlement.test.mjs',
                     'integration/sdk.test.mjs', 'integration/native-actor/actor.test.mjs'):
            check(path, [bun, '--no-env-file', prefix+path])
        for path in ('providers/jev.test.mjs', 'providers/evaluation/provider.test.mjs', 'local/check.test.mjs', 'getting-started/walkthrough.test.mjs', 'getting-started/configure.test.mjs', 'getting-started/host-binding.test.mjs'):
            check(path, [bun, 'test', '--no-env-file', prefix+path])
        check('Bun/Rust observer parity', [bun, '--no-env-file', prefix+'rust-binding/conformance.mjs', str(target/'debug/weave-file-binding-experiment')])
        check('Bun/Rust checkpoint interchange', [bun, '--no-env-file', prefix+'integration/interchange.test.mjs', str(target/'debug/weave-local-host-experiment')])
        for path in ('rust-local/conformance.mjs', 'rust-local/check-parity.mjs', 'integration/local-parity.test.mjs', 'integration/config-parity.test.mjs', 'integration/full-loop.test.mjs', 'integration/evaluation-parity.test.mjs', 'integration/settlement-parity.test.mjs'):
            check(path, [bun, '--no-env-file', prefix+path, str(target/'debug/weave-rust-local')])
        env['WEAVE_RUST_LOCAL'] = str(target/'debug/weave-rust-local')
        check('Generated Bun/Rust configuration loop', [bun, 'test', '--no-env-file', prefix+'getting-started/generated-loop.test.mjs'])
        check('Independent copied Rust SDK', [sys.executable, prefix+'rust-local/consumer-check.py', '--cargo', cargo])
        check('Fixed kernel image staging', [sys.executable, '-m', 'unittest', 'discover', '-s', prefix+'getting-started', '-p', 'test_stage_kernel_image.py', '-q'])
        check('Private bundle tooling', [sys.executable, '-m', 'unittest', 'discover', '-s', prefix+'distribution', '-p', 'test_pack.py', '-q'])
        check('Private installation', [sys.executable, '-m', 'unittest', 'discover', '-s', prefix+'distribution', '-p', 'test_install.py', '-q'])
    unchanged = source_digest == snapshot()
    report = {'schema': 'openprose.local-qualification/1', 'finishedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'sourceCommit': source, 'workingTreeChanged': dirty, 'sourceDigest': source_digest, 'sourceUnchangedDuringChecks': unchanged, 'providerCalls': 0,
              'scope': 'Offline source-package and local process qualification; not installed release or semantic model qualification.',
              'passed': unchanged and all(r['exitCode'] == 0 for r in rows), 'checks': rows}
    if args.output:
        with open(args.output, 'x', encoding='utf8') as handle:
            json.dump(report, handle, indent=2)
            handle.write('\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'checks'}))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    sys.exit(main())
