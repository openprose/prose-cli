#!/usr/bin/env python3
"""Provider-free admission of a compiled release that resolves the latest kernel."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import build_local

CONTRACT = Path(__file__).resolve().parents[1] / 'shared/fixtures/build/published-release.json'


def subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(actual[k], v) for k, v in expected.items())
    return type(actual) is type(expected) and actual == expected


def check(binary: Path, runner: str, commit: str, version: str, execute=None, *, node: Path | None = None):
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('binary must be an executable regular file')
    if runner not in {'bun', 'rust'} or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('runner and exact source commit are required')
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?', version):
        raise ValueError('exact release version is required')
    if node is not None:
        node = node.resolve(strict=True)
        if runner != 'bun' or not node.is_file() or not os.access(node, os.X_OK):
            raise ValueError('an explicit executable Node interpreter is supported only for the Bun npm launcher')
    interpreter_digest = hashlib.sha256(node.read_bytes()).hexdigest() if node else None
    before = hashlib.sha256(binary.read_bytes()).hexdigest()
    contract = json.loads(CONTRACT.read_text())
    with tempfile.TemporaryDirectory(prefix='openprose-release-check-') as directory:
        root = Path(directory)
        env = {'PATH': os.defpath, 'HOME': directory, 'XDG_CONFIG_HOME': str(root / 'config'),
               'TMPDIR': directory, 'LANG': 'C', 'LC_ALL': 'C',
               'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9',
               'ALL_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': ''}
        def run(probe, args, expected_code):
            call = execute or (lambda argv, cwd, environment: build_local.execute_bounded(argv, cwd, environment, timeout_seconds=15))
            result = call(([str(node)] if node else []) + [str(binary), *args], root, env)
            if result.returncode != expected_code or result.stderr:
                raise ValueError(f'offline release probe {probe} failed its exit/stderr contract '
                                 f'(exit {result.returncode}; stderr present: {bool(result.stderr)})')
            return result.stdout.decode('utf-8')
        if run('version', ['--version'], 0).strip() != f'prose {version} ({runner})':
            raise ValueError('version banner does not match requested release')
        doctor = json.loads(run('doctor', ['--output=json', 'cli', 'doctor'], 10))
        if not subset(doctor, contract['doctor']) or not subset(doctor.get('runner'), {'name': runner, 'commit': commit, 'version': version}):
            raise ValueError('release doctor does not attest published startup without test seams')
        inventory = json.loads(run('harness-list', ['--output=json', 'cli', 'harness', 'list'], 0))
        mocks = [item for item in inventory['harnesses'] if item['id'] == 'mock']
        if len(mocks) != 1 or not subset(mocks[0], contract['mock']):
            raise ValueError('mock execution must be unavailable in release binaries')
        result = json.loads(run('mock-run', ['--harness=mock', '--output=json', 'run', 'hello'], contract['mockRunExitCode']))
        if result.get('error', result).get('code') != contract['mockRunErrorCode']:
            raise ValueError('mock request did not fail closed')
    if hashlib.sha256(binary.read_bytes()).hexdigest() != before:
        raise ValueError('binary changed during release probes')
    if node is not None and hashlib.sha256(node.read_bytes()).hexdigest() != interpreter_digest:
        raise ValueError('Node interpreter changed during release probes')
    return {'schema': 'openprose.published-release-check/1', 'status': 'passed-offline-release-check',
            'runner': runner, 'commit': commit, 'version': version, 'binarySha256': before,
            'imageSource': 'published-on-run', 'testSeamsEnabled': False, 'modelCalls': 0, 'networkCalls': 0,
            **({'nodeInterpreterSha256': interpreter_digest} if node else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--node', type=Path, help='Explicit Node executable for an installed Bun npm launcher')
    parser.add_argument('--runner', required=True, choices=['bun', 'rust'])
    parser.add_argument('--commit', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    try:
        report = check(args.binary, args.runner, args.commit, args.version, node=args.node)
    except (ValueError, OSError, KeyError, build_local.LocalBuildError) as error:
        print(json.dumps({'schema': 'openprose.published-release-check-error/1', 'message': str(error)}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
