#!/usr/bin/env python3
"""Hermetic process checks for persistent service selection across fresh invocations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from staging_service import matches

CORPUS = Path(__file__).with_name('service-environment-corpus.json')


def run(command):
    for case in json.loads(CORPUS.read_text())['cases']:
        with tempfile.TemporaryDirectory(prefix='prose-environment-oracle-') as directory:
            root = Path(directory)
            config = root / 'config' / 'openprose' / 'cli.toml'
            config.parent.mkdir(parents=True)
            if 'userConfig' in case:
                config.write_text(case['userConfig'])
            fixture = root / 'service.json'
            fixture.write_text(json.dumps({'credentials': {'production': None, 'staging': None}, 'storeAvailable': True, 'exchanges': []}))
            env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': directory,
                   'XDG_CONFIG_HOME': str(root/'config'), 'TMPDIR': directory,
                   'PROSE_TEST_SERVICE_FIXTURE': str(fixture),
                   'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9',
                   'ALL_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': ''}
            for step in case['steps']:
                result = subprocess.run([*command, *step['argv']], cwd=root, env=env, capture_output=True, timeout=10)
                assert result.returncode == step['exitCode'], (case['id'], result.returncode, result.stderr)
                actual = json.loads(result.stdout)
                matches(actual, step['resultMatches'])
                assert set(actual) == set(step['resultMatches'])
                assert not result.stderr, (case['id'], result.stderr)
            if 'preserve' in case:
                assert config.read_text() == case['preserve']
            print('PASS', case['id'])
    return 0


if __name__ == '__main__':
    args = sys.argv[1:]
    if args[:1] == ['--']:
        args = args[1:]
    if not args:
        raise SystemExit('Supply a test-seam executable after --')
    raise SystemExit(run(args))
