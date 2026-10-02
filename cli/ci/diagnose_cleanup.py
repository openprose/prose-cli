#!/usr/bin/env python3
"""Bounded private-build investigation; not release or source qualification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = Path('cli/rust/crates/prose-process-supervisor/src/supervisor.rs')
SOURCE_SHA256 = 'a2653ca0f44287ae768b1ab3bd63bb26f8e4004fdfb47ba576410feba1ecddda'
MARKER = '        if cleanup.is_err() || !readers_settled || !stdin_settled {\n'
DIAGNOSTIC = '''            eprintln!(
                "OPENPROSE_CLEANUP_DIAGNOSTIC group_ok={} stdout={:?} stderr={:?} stdin={:?}",
                cleanup.is_ok(), stdout_settlement, stderr_settlement,
                stdin_settlement.as_ref().map(|s| s.as_ref().map(|(r, forced)| (r.is_ok(), *forced)))
            );
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_logged(argv, cwd, environment, log_path, timeout):
    """Bound a diagnostic and terminate only its own process group on timeout."""
    with log_path.open('wb') as log:
        child = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return child.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            child.wait(timeout=5)
            return child.returncode, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=50)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 50:
        parser.error('repetitions must be between 1 and 50')
    args.output.mkdir(parents=True, exist_ok=False)
    if sha(ROOT / SUPERVISOR) != SOURCE_SHA256:
        raise ValueError('supervisor input differs from the inspected source')
    record = {'purpose': 'Private diagnostic build only; assertions and product failure semantics unchanged.',
              'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'supervisor_before_sha256': SOURCE_SHA256, 'provider_calls': 0,
              'repetition_limit': args.repetitions, 'attempts': []}
    environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'RUSTUP_HOME', 'CARGO_HOME',
                   'RUSTUP_TOOLCHAIN', 'LANG', 'LC_ALL') if k in os.environ}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    with tempfile.TemporaryDirectory(prefix='openprose-cleanup-diagnostic-') as temporary:
        temporary = Path(temporary)
        archive = temporary / 'source.tar'
        source = temporary / 'source'
        source.mkdir()
        with archive.open('wb') as f:
            subprocess.run(['git', 'archive', 'HEAD'], cwd=ROOT, stdout=f, check=True, timeout=60)
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter='data')
        path = source / SUPERVISOR
        assert sha(path) == SOURCE_SHA256
        original = path.read_text()
        assert original.count(MARKER) == 1
        path.write_text(original.replace(MARKER, MARKER + DIAGNOSTIC))
        record['supervisor_instrumented_sha256'] = sha(path)
        record['instrumentation'] = DIAGNOSTIC
        if args.prepare_only:
            record['state'] = 'prepared-only; no build or product execution'
        else:
            dependencies = ROOT / 'cli/bun/node_modules'
            if not dependencies.is_dir():
                raise ValueError('prepare locked Bun dependencies before diagnosis')
            shutil.copytree(dependencies, source / 'cli/bun/node_modules', symlinks=False)
            fixture = source / 'cli/conformance/adversarial/adapter-products/test_adapter_products.py'
            # The existing builder disables network and creates the ordinary
            # opaque-image fixture products, with no runtime test seam enabled.
            build_code, build_timeout = run_logged([sys.executable, '-c',
                'import importlib.util,sys; s=importlib.util.spec_from_file_location("fixture",sys.argv[1]); '
                'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.build_fixture_products()',
                str(fixture)], source, environment, args.output / 'build.log', 900)
            record['build_returncode'] = build_code
            record['build_timed_out'] = build_timeout
            if build_code != 0 or build_timeout:
                record['state'] = 'build-failed'
            else:
                test = 'AdapterProductAdversary.test_omp_control_barrier_and_nonterminal_fail_closed_with_product_parity'
                record['state'] = 'bounded-repetitions-passed'
                for attempt in range(1, args.repetitions + 1):
                    started = time.monotonic()
                    name = f'attempt-{attempt:03}.log'
                    code, timed_out = run_logged([sys.executable, str(fixture), test], source, environment,
                                                 args.output / name, 60)
                    record['attempts'].append({'attempt': attempt, 'returncode': code, 'timed_out': timed_out,
                                               'seconds': time.monotonic() - started, 'log': name,
                                               'sha256': sha(args.output / name)})
                    if code != 0 or timed_out:
                        record['state'] = 'failure-observed'
                        break
        assert sha(ROOT / SUPERVISOR) == SOURCE_SHA256
    (args.output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'state': record['state'], 'attempts': len(record['attempts']), 'provider_calls': 0}))
    return 0 if args.prepare_only or record['state'] == 'bounded-repetitions-passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
