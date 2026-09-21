#!/usr/bin/env python3
"""Provider-free process oracle for explicit staging account commands.

Usage: python3 staging_service.py -- /absolute/path/to/prose
       python3 staging_service.py -- bun /absolute/path/to/src/cli.ts
Each invocation receives fresh HOME, a closed HTTP transcript, and no credentials.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CORPUS = Path(__file__).with_name('staging-service-corpus.json')


def matches(actual, expected, path='$'):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError(f'{path}: expected object')
        for key, value in expected.items():
            if key not in actual:
                raise AssertionError(f'{path}.{key}: missing')
            matches(actual[key], value, f'{path}.{key}')
    elif actual != expected:
        raise AssertionError(f'{path}: differs from corpus')


def validate_corpus():
    corpus = json.loads(CORPUS.read_text())
    cli = CORPUS.parents[2]
    taxonomy = {e['code']: e for e in json.loads((cli/'shared/errors/taxonomy.v1.json').read_text())['errors']}
    ids = set()
    for case in corpus['cases']:
        assert case['id'] not in ids
        ids.add(case['id'])
        assert case['argv'][:2] == ['--service-environment', 'staging']
        result = case['resultMatches']
        assert result['environment'] == 'staging'
        if result['problem']:
            assert taxonomy[result['problem']['code']]['exitCode'] == case['exitCode']
        else:
            assert case['exitCode'] == 0
        for exchange in case['fixture'].get('exchanges', []):
            assert (exchange['method'], exchange['path']) in {
                ('GET', '/organizations'), ('POST', '/auth/device'), ('POST', '/auth/device/poll')}
            assert isinstance(exchange['status'], int)
    print(f'Validated {len(ids)} staging corpus cases (standard-library structural checks).')
    return 0


def run(command):
    failures = []
    for case in json.loads(CORPUS.read_text())['cases']:
        with tempfile.TemporaryDirectory(prefix='prose-staging-oracle-') as directory:
            root = Path(directory)
            fixture = root / 'service.json'
            fixture.write_text(json.dumps(case['fixture']))
            environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                           'HOME': directory, 'XDG_CONFIG_HOME': str(root/'config'),
                           'XDG_CACHE_HOME': str(root/'cache'), 'TMPDIR': directory,
                           'PROSE_TEST_SERVICE_FIXTURE': str(fixture),
                           'HTTP_PROXY': 'http://127.0.0.1:9',
                           'HTTPS_PROXY': 'http://127.0.0.1:9',
                           'ALL_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': ''}
            environment.update(case.get("environment", {}))
            try:
                observed = subprocess.run([*command, *case['argv']], cwd=root,
                                          env=environment, capture_output=True, timeout=10)
                if observed.returncode != case['exitCode']:
                    raise AssertionError(f'exit {observed.returncode}, expected {case["exitCode"]}')
                result = json.loads(observed.stdout)
                matches(result, case['resultMatches'])
                for secret in (b'rr_test_11111111111111111111111111111111', b'rr_test_22222222222222222222222222222222',
                               b'rr_test_33333333333333333333333333333333', b'fixture-device-secret', b'fixture-secret-must-not-escape'):
                    if secret in observed.stdout + observed.stderr:
                        raise AssertionError('secret appeared in process output')
                if case['id'] == 'login-user-code-equals-device-code' and b'ABCD-EFGH' in observed.stdout + observed.stderr:
                    raise AssertionError('device secret appeared in output')
                if result.get('problem') is None and set(result) != set(case['resultMatches']):
                    raise AssertionError('unexpected result fields')
                print(f'PASS {case["id"]}')
            except (AssertionError, ValueError, subprocess.TimeoutExpired) as error:
                failures.append(case['id'])
                print(f'FAIL {case["id"]}: {error}')
    return 1 if failures else 0


if __name__ == '__main__':
    command = sys.argv[1:]
    if command[:1] == ['--']:
        command = command[1:]
    if command == ['--validate']:
        raise SystemExit(validate_corpus())
    if not command:
        raise SystemExit('Provide a test-seam product command after --')
    raise SystemExit(run(command))
