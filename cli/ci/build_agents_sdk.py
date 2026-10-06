#!/usr/bin/env python3
"""Freeze the package-owned SDK helper without provider calls or ambient credentials."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'harnesses/agents-sdk/requirements-build.txt'
SOURCE = ROOT / 'harnesses/agents-sdk/run.py'
NAME = 'prose-agents-sdk'
RECEIPT = 'agents-sdk-build.json'
NOTICES = 'AGENTS-SDK-NOTICES.txt'
MAX_BYTES = 256 * 1024 * 1024
TOOL_SELF_TEST_SOURCE = r'''
import asyncio
import json
from pathlib import Path
import socket
import tempfile
from unittest.mock import AsyncMock, patch
import prose_sdk_runtime as runtime

class Writer:
    def write(self, data):
        assert data.startswith(b'GET /fixture HTTP/1.1\r\n')
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass

async def check():
    with tempfile.TemporaryDirectory(prefix='sdk-tool-self-test-') as directory:
        root = Path(directory)
        env = {'PATH': '/usr/bin:/bin'}
        result = await runtime.shell('printf fixture > effect; cat effect', directory, 2, env)
        assert result['exit_code'] == 0 and result['stdout'] == 'fixture'
        assert (root / 'effect').read_text() == 'fixture'
        result = await runtime.shell('head -c 100000 /dev/zero', directory, 2, env, output_limit=4096)
        assert len(result['stdout']) == 4096 and result['stdout_truncated']
        task = asyncio.create_task(runtime.shell('(sleep .2; touch late) & wait', directory, 2, env))
        await asyncio.sleep(.04)
        task.cancel()
        try:
            await task
            raise AssertionError('shell cancellation did not cancel')
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(.25)
        assert not (root / 'late').exists()
    reader = asyncio.StreamReader()
    reader.feed_data(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\nContent-Type: text/plain\r\n\r\nfixture')
    reader.feed_eof()
    loop = asyncio.get_running_loop()
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=addresses)), patch.object(asyncio, 'open_connection', AsyncMock(return_value=(reader, Writer()))):
        retrieved = await runtime.retrieve_public('https://example.invalid/fixture', 2)
    assert retrieved['status'] == 200 and retrieved['content'] == 'fixture' and not retrieved['truncated']
    truncated = asyncio.StreamReader()
    truncated.feed_data(b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nshort')
    truncated.feed_eof()
    with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=addresses)), patch.object(asyncio, 'open_connection', AsyncMock(return_value=(truncated, Writer()))):
        try:
            await runtime.retrieve_public('https://example.invalid/fixture', 2)
            raise AssertionError('truncated HTTP content was accepted')
        except (ValueError, asyncio.IncompleteReadError):
            pass
    return {'schema': 'openprose.sdk-packaged-tools-self-test/1', 'shellEffects': True,
            'boundedOutput': True, 'shellCancellation': True, 'mockedPublicRetrieval': True,
            'incompleteHttpRejected': True, 'modelCalls': 0, 'networkUsed': False}

def main():
    print(json.dumps(asyncio.run(check()), sort_keys=True))
'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock_packages(path=LOCK):
    rows = path.read_text('utf-8').replace('\\\n', '').splitlines()
    result = []
    for row in rows:
        row = row.strip()
        if not row or row.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)', row)
        require(match is not None, 'SDK lock must contain only exact hash-locked requirements')
        name, version, hashes = match.groups()
        result.append({'name': name.lower().replace('_', '-'), 'version': version,
                       'wheelSha256': sorted(set(re.findall(r'sha256:([0-9a-f]{64})', hashes)))})
    require(len(result) > 0 and len({r['name'] for r in result}) == len(result), 'Empty or duplicate SDK lock')
    return result


def environment(output, ambient):
    env = {k: ambient[k] for k in ('PATH', 'DEVELOPER_DIR', 'SDKROOT', 'SYSTEMROOT') if k in ambient}
    home = output / 'home'; home.mkdir()
    tmp = output / 'tmp'; tmp.mkdir()
    env.update(HOME=str(home), TMPDIR=str(tmp), LANG='C.UTF-8', PYTHONHASHSEED='0',
               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', SOURCE_DATE_EPOCH='0',
               PYINSTALLER_CONFIG_DIR=str(output / 'pyinstaller-cache'))
    return env


def installed_inventory(packages):
    notices = ['OpenProse packaged Agents SDK helper: third-party notices',
               'Python ' + platform.python_version() + ' (PSF License); https://docs.python.org/3/license.html', '']
    import builtins
    builtins.license._Printer__setup()
    notices += builtins.license._Printer__lines
    for package in packages:
        dist = metadata.distribution(package['name'])
        require(dist.version == package['version'], 'Installed SDK distribution differs from lock: ' + package['name'])
        notices += [package['name'] + ' ' + package['version'],
                    dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'License: see included distribution license files']
        for entry in sorted(dist.files or [], key=str):
            if ('license' in str(entry).lower() or 'copying' in str(entry).lower()) and '.dist-info/' in str(entry):
                path = dist.locate_file(entry)
                if path.is_file() and path.stat().st_size <= 1024 * 1024:
                    notices += ['--- ' + str(entry) + ' ---', path.read_text('utf-8', errors='replace')]
        notices += ['']
    return '\n'.join(notices).encode('utf-8')


def run(command, *, env, cwd, log, timeout=900):
    with log.open('wb') as stream:
        completed = subprocess.run([str(x) for x in command], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    require(completed.returncode == 0, 'SDK build command failed; inspect ' + str(log))


def build(output, *, epoch=0, codesign_identity=None):
    require(platform.python_version() == '3.10.20', 'SDK release builder requires Python 3.10.20')
    require(sys.platform in ('darwin', 'linux') and platform.machine() in ('arm64', 'aarch64', 'x86_64'), 'SDK builder requires native supported POSIX host')
    if codesign_identity is not None:
        require(sys.platform == 'darwin' and re.fullmatch(r'Developer ID Application: [^\r\n]+ \([A-Z0-9]{10}\)', codesign_identity), 'SDK signing requires a macOS Developer ID Application identity')
    require(not output.exists() and not output.is_symlink(), 'SDK output must be fresh')
    require(epoch >= 0, 'Source date epoch must be non-negative')
    output.parent.mkdir(parents=True, exist_ok=True)
    require(shutil.disk_usage(output.parent).free >= 512 * 1024 * 1024,
            'SDK freeze requires at least 512 MiB free scratch space')
    packages = lock_packages()
    notices = installed_inventory(packages)
    output.mkdir(parents=True)
    env = environment(output, os.environ); env['SOURCE_DATE_EPOCH'] = str(epoch)
    (output / NOTICES).write_bytes(notices)
    (output / 'sdk_tool_selftest.py').write_text(TOOL_SELF_TEST_SOURCE)
    # A frozen module cannot run python -c; this explicit self-test entrypoint is bundled
    # separately and exercises imported SDK modules/certificate resources without API calls.
    entry = output / 'sdk-entry.py'
    entry.write_text("import sys\nif '--packaged-self-test' in sys.argv:\n"
                     " import json, ssl, pathlib, certifi, agents, openai, pydantic, jiter\n"
                     " from importlib.metadata import version\n"
                     " ssl.create_default_context(cafile=certifi.where())\n"
                     " print(json.dumps({'schema':'openprose.sdk-packaged-self-test/1','openaiAgents':version('openai-agents'),'openai':version('openai'),'certificates':pathlib.Path(certifi.where()).is_file(),'modelCalls':0},sort_keys=True))\n"
                     "elif '--packaged-tool-self-test' in sys.argv:\n"
                     " import sdk_tool_selftest\n sdk_tool_selftest.main()\n"
                     "elif '--packaged-library-test' in sys.argv:\n"
                     " import json, pathlib, re\n"
                     " versions=[]; count=0\n"
                     " for path in pathlib.Path(sys._MEIPASS).rglob('*'):\n"
                     "  if path.is_file() and not path.is_symlink() and path.stat().st_size<=268435456:\n"
                     "   with path.open('rb') as stream:\n"
                     "    if stream.read(4)!=b'\\x7fELF': continue\n"
                     "    data=stream.read()\n"
                     "   count+=1\n"
                     "   versions.extend(tuple(int(x) for x in v.split(b'.')) for v in re.findall(rb'GLIBC_([0-9]+\\.[0-9]+(?:\\.[0-9]+)?)',data))\n"
                     " print(json.dumps({'schema':'openprose.sdk-packaged-libraries/1','elfCount':count,'requiredGlibcMaximum':'.'.join(str(x) for x in max(versions)) if versions else None,'modelCalls':0},sort_keys=True))\n"
                     "else:\n import runpy\n runpy.run_module('prose_sdk_runtime',run_name='__main__')\n")
    # Snapshot the concurrently maintained runtime before freezing. Receipt binds exact bytes.
    source_copy = output / 'prose_sdk_runtime.py'; source_copy.write_bytes(SOURCE.read_bytes())
    source_sha = digest(source_copy)
    args = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--onefile', '--noupx',
            '--name', NAME, '--distpath', output / 'dist', '--workpath', output / 'work',
            '--specpath', output, '--paths', output, '--hidden-import', 'prose_sdk_runtime',
            '--collect-all', 'agents', '--collect-all', 'openai', '--collect-all', 'certifi',
            '--copy-metadata', 'openai-agents', '--copy-metadata', 'openai',
            '--add-data', str(output / NOTICES) + os.pathsep + '.', entry]
    if codesign_identity is not None:
        args.extend(['--codesign-identity', codesign_identity])
    for package in packages:
        args.extend(['--copy-metadata', package['name']])
    run(args, env=env, cwd=output, log=output / 'freeze.log')
    helper = output / NAME; shutil.copyfile(output / 'dist' / NAME, helper); helper.chmod(0o755)
    require(0 < helper.stat().st_size <= MAX_BYTES, 'Frozen SDK helper exceeds size limit')
    # Disposable build caches duplicate the frozen payload and need not accompany receipts.
    for directory in ('dist', 'work', 'pyinstaller-cache'):
        shutil.rmtree(output / directory)
    run([helper, '--packaged-self-test'], env=env, cwd=output, log=output / 'self-test.json', timeout=30)
    self_test = json.loads((output / 'self-test.json').read_text())
    require(self_test == {'schema': 'openprose.sdk-packaged-self-test/1', 'openaiAgents': '0.22.2', 'openai': '3.13.0', 'certificates': True, 'modelCalls': 0}, 'Frozen SDK self-test mismatch')
    run([helper, '--packaged-tool-self-test'], env=env, cwd=output, log=output / 'tool-self-test.json', timeout=30)
    tool_self_test = json.loads((output / 'tool-self-test.json').read_text())
    require(tool_self_test == {'schema': 'openprose.sdk-packaged-tools-self-test/1', 'shellEffects': True,
                              'boundedOutput': True, 'shellCancellation': True, 'mockedPublicRetrieval': True,
                              'incompleteHttpRejected': True, 'modelCalls': 0, 'networkUsed': False}, 'Frozen SDK tool self-test mismatch')
    run([helper, '--version'], env=env, cwd=output, log=output / 'version.txt', timeout=5)
    require((output / 'version.txt').read_text().strip() == 'prose-agents-sdk 0.1.0', 'Frozen SDK version mismatch')
    signing = 'not-applicable'
    linux_libraries = 'not-applicable'
    if sys.platform == 'linux':
        run([helper, '--packaged-library-test'], env=env, cwd=output, log=output / 'libraries.json', timeout=30)
        linux_libraries = json.loads((output / 'libraries.json').read_text())
        require(linux_libraries.get('elfCount', 0) > 0 and linux_libraries.get('requiredGlibcMaximum') is not None,
                'Missing frozen Linux library inspection')
        maximum = tuple(int(x) for x in linux_libraries['requiredGlibcMaximum'].split('.'))
        require(maximum <= (2, 34), 'Frozen SDK libraries require glibc newer than 2.34')
    if sys.platform == 'darwin':
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', helper], env=env, cwd=output, log=output / 'codesign.log', timeout=30)
        signing = 'developer-id-embedded' if codesign_identity is not None else 'ad-hoc-integrity-only'
    receipt = {'schema': 'openprose.agents-sdk-build/1', 'helper': {'path': NAME, 'sha256': digest(helper), 'byteLength': helper.stat().st_size},
               'python': platform.python_version(), 'pyinstaller': metadata.version('pyinstaller'),
               'platform': sys.platform, 'architecture': platform.machine(), 'sourceDateEpoch': epoch,
               'sources': {'harnesses/agents-sdk/run.py': source_sha, 'harnesses/agents-sdk/requirements-build.txt': digest(LOCK)},
               'dependencies': packages, 'notices': {'path': NOTICES, 'sha256': digest(output / NOTICES), 'byteLength': len(notices)},
               'selfTest': self_test, 'signing': signing, 'modelCalls': 0,
               'embeddedSigning': {'identity': codesign_identity, 'verification': 'pyinstaller-inner-binaries-and-frozen-self-tests'} if codesign_identity is not None else 'ad-hoc-integrity-only',
               'toolSelfTest': tool_self_test,
               'linuxLibraries': linux_libraries, 'pythonExecutableSha256': digest(Path(sys.executable).resolve()),
               'authority': 'local-build-and-imports-only', 'publicationAuthorized': False}
    (output / RECEIPT).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--codesign-identity', help='macOS Developer ID identity for every embedded binary; requires prepared signing keychain')
    parser.add_argument('--source-date-epoch', type=int, default=0)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.out.absolute(), epoch=args.source_date_epoch, codesign_identity=args.codesign_identity), sort_keys=True))
    except (ValueError, OSError, subprocess.SubprocessError, metadata.PackageNotFoundError) as error:
        parser.exit(2, str(error) + '\n')
