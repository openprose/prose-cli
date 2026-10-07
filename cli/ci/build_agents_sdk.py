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
import sdk_native_inventory as native

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'harnesses/agents-sdk/requirements-build.txt'
SOURCE = ROOT / 'harnesses/agents-sdk/run.py'
NAME = 'prose-agents-sdk'
RECEIPT = 'agents-sdk-build.json'
NOTICES = 'AGENTS-SDK-NOTICES.txt'
MAX_BYTES = 256 * 1024 * 1024
# Identity probes must not initialize the provider SDK before the fixed probe deadline.
ENTRY_SOURCE = (
    'import sys\n'
    "if sys.argv[1:] == ['--version']:\n"
    " print('prose-agents-sdk 0.1.0')\n"
    "elif '--packaged-self-test' in sys.argv:\n"
    ' import json, ssl, pathlib, certifi, agents, openai, pydantic, jiter\n'
    ' from importlib.metadata import version\n'
    ' ssl.create_default_context(cafile=certifi.where())\n'
    " print(json.dumps({'schema':'openprose.sdk-packaged-self-test/1','openaiAgents':version('openai-agents'),'openai':version('openai'),'certificates':pathlib.Path(certifi.where()).is_file(),'modelCalls':0},sort_keys=True))\n"
    "elif '--packaged-tool-self-test' in sys.argv:\n"
    ' import sdk_tool_selftest\n'
    ' sdk_tool_selftest.main()\n'
    "elif '--packaged-library-test' in sys.argv:\n"
    ' import json, sdk_native_inventory\n'
    ' print(json.dumps(sdk_native_inventory.inspect_tree(sys._MEIPASS),sort_keys=True))\n'
    'else:\n'
    ' import runpy\n'
    " runpy.run_module('prose_sdk_runtime',run_name='__main__')\n"
)
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
    # PyInstaller may leave this cache unused on Linux; cleanup still owns it.
    cache = output / 'pyinstaller-cache'; cache.mkdir()
    env.update(HOME=str(home), TMPDIR=str(tmp), LANG='C.UTF-8', PYTHONHASHSEED='0',
               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', SOURCE_DATE_EPOCH='0',
               PYINSTALLER_CONFIG_DIR=str(cache))
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


def inspect_archive(helper):
    from PyInstaller.archive.readers import CArchiveReader
    native.read_file(helper)
    archive = CArchiveReader(str(helper)); rows = []; total_bytes = 0
    for name, entry in sorted(archive.toc.items()):
        native.safe_path(name)
        if entry[-1] != 'b': continue
        native.require(type(entry[2]) is int and 0 <= entry[2] <= native.MAX_BYTES, 'Oversized native archive declaration')
        total_bytes += entry[2]
        native.require(total_bytes <= 768 * 1024 * 1024, 'Native archive extraction exceeds bound')
        data = archive.extract(name)
        native.require(len(data) == entry[2], 'Native archive size declaration mismatch')
        native.require(len(data) <= native.MAX_BYTES, 'Oversized frozen archive member')
        if data[:4] == b'\x7fELF': rows.append(native.elf_record(name, data))
        native.require(len(rows) <= native.MAX_FILES, 'Too many native archive members')
    return rows


def build(output, *, epoch=0, codesign_identity=None, linux_libgcc=None, linux_native_origin=None):
    require(platform.python_version() == '3.10.20', 'SDK release builder requires Python 3.10.20')
    require(sys.platform in ('darwin', 'linux') and platform.machine() in ('arm64', 'aarch64', 'x86_64'), 'SDK builder requires native supported POSIX host')
    if codesign_identity is not None:
        require(sys.platform == 'darwin' and re.fullmatch(r'Developer ID Application: [^\r\n]+ \([A-Z0-9]{10}\)', codesign_identity), 'SDK signing requires a macOS Developer ID Application identity')
    require((linux_libgcc is None) == (linux_native_origin is None), 'Linux native hooks must be supplied together')
    require(sys.platform == 'linux' or linux_libgcc is None, 'Linux native hooks are Linux-only')
    require(sys.platform != 'linux' or linux_libgcc is not None, 'Linux build requires explicit libgcc supplier and native origin')
    native_input = None
    if linux_libgcc is not None:
        target = 'linux-arm64-gnu' if platform.machine() == 'aarch64' else 'linux-x64-gnu'
        native_input, native_input_sha, supplier_texts = native.validate_input(linux_native_origin, linux_libgcc, ROOT, target)
        supplier_row = native.elf_record('libgcc_s.so.1', native.read_file(linux_libgcc))
        require(supplier_row['machine'] == (183 if target == 'linux-arm64-gnu' else 62), 'Supplier machine differs from target')
    require(not output.exists() and not output.is_symlink(), 'SDK output must be fresh')
    require(epoch >= 0, 'Source date epoch must be non-negative')
    output.parent.mkdir(parents=True, exist_ok=True)
    require(shutil.disk_usage(output.parent).free >= 512 * 1024 * 1024,
            'SDK freeze requires at least 512 MiB free scratch space')
    packages = lock_packages()
    notices = installed_inventory(packages)
    if native_input is not None:
        origin = native_input['libgcc']
        supplier_header = json.dumps({'package': origin['package'], 'library': origin['library'], 'image': native_input['images']['supplier']}, sort_keys=True)
        python_origin, _ = native.python_origin(native_input)
        texts = ['Actual libgcc supplier provenance: ' + supplier_header, *supplier_texts,
                 'Full Python native provenance: ' + json.dumps(python_origin, sort_keys=True)]
        python_root = Path(native_input['pythonDistribution']['root'])
        texts += [native.read_file(python_root / row['path'], 1024 * 1024).decode('utf-8') for row in python_origin['licenses']]
        notices += ('\n' + '\n'.join(texts) + '\n').encode('utf-8')
    output.mkdir(parents=True)
    env = environment(output, os.environ); env['SOURCE_DATE_EPOCH'] = str(epoch)
    (output / NOTICES).write_bytes(notices)
    (output / 'sdk_tool_selftest.py').write_text(TOOL_SELF_TEST_SOURCE)
    # A frozen module cannot run python -c; this explicit self-test entrypoint is bundled
    # separately and exercises imported SDK modules/certificate resources without API calls.
    entry = output / 'sdk-entry.py'
    entry.write_text(ENTRY_SOURCE)
    # Snapshot the concurrently maintained runtime before freezing. Receipt binds exact bytes.
    source_copy = output / 'prose_sdk_runtime.py'; source_copy.write_bytes(SOURCE.read_bytes())
    source_sha = digest(source_copy)
    native_source = Path(native.__file__)
    (output / 'sdk_native_inventory.py').write_bytes(native.read_file(native_source))
    if native_input is not None:
        require(source_sha == native_input['sourceSnapshot']['sources']['harnesses/agents-sdk/run.py'] and
                digest(output / 'sdk_native_inventory.py') == native_input['sourceSnapshot']['sources']['cli/ci/sdk_native_inventory.py'],
                'Frozen source copies differ from bound source snapshot')
    args = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--onefile', '--noupx',
            '--name', NAME, '--distpath', output / 'dist', '--workpath', output / 'work',
            '--specpath', output, '--paths', output, '--hidden-import', 'prose_sdk_runtime',
            '--collect-all', 'agents', '--collect-all', 'openai', '--collect-all', 'certifi',
            '--copy-metadata', 'openai-agents', '--copy-metadata', 'openai',
            '--add-data', str(output / NOTICES) + os.pathsep + '.', entry]
    args.extend(['--hidden-import', 'sdk_native_inventory'])
    if native_input is not None:
        args.extend(['--add-binary', str(linux_libgcc) + os.pathsep + '.'])
    if codesign_identity is not None:
        args.extend(['--codesign-identity', codesign_identity])
    for package in packages:
        args.extend(['--copy-metadata', package['name']])
    run(args, env=env, cwd=output, log=output / 'freeze.log')
    helper = output / NAME; shutil.copyfile(output / 'dist' / NAME, helper); helper.chmod(0o755)
    require(0 < helper.stat().st_size <= MAX_BYTES, 'Frozen SDK helper exceeds size limit')
    native_dependencies = None
    if native_input is not None:
        toc_sha = native.verify_analysis_toc(output / 'work' / NAME / 'Analysis-00.toc', linux_libgcc)
        archive_rows = inspect_archive(helper)
        native.verify_closure(archive_rows, target)
        origin_rows, origins = native.assign_origins(archive_rows, native_input, packages, metadata.distribution)
        native_dependencies = {'libraries': origin_rows, 'symbolClosureVerified': True,
            'libgccSelection': {'path': 'libgcc_s.so.1', 'sha256': supplier_row['sha256'],
                               'byteLength': supplier_row['byteLength'], 'analysisTocSha256': toc_sha},
            'origins': origins}
        native.validate_native_dependencies(native_dependencies, target)
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
        require(linux_libraries.get('libraries') == archive_rows, 'Frozen extracted native membership differs from archive')
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
    if native_input is not None:
        final_input, final_sha, _ = native.validate_input(linux_native_origin, linux_libgcc, ROOT, target)
        final_rows, final_origins = native.assign_origins(archive_rows, final_input, packages, metadata.distribution)
        require(final_rows == origin_rows and final_origins == origins, 'Native origin/license bytes changed during build')
        require(final_input == native_input and final_sha == native_input_sha, 'Native input changed during build')
        receipt.update(linuxBuildInputSha256=native_input_sha,
                       linuxBuildSourceSnapshot=native_input['sourceSnapshot'], nativeDependencies=native_dependencies)
        native.validate_linux_receipt(receipt)
    (output / RECEIPT).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--codesign-identity', help='macOS Developer ID identity for every embedded binary; requires prepared signing keychain')
    parser.add_argument('--source-date-epoch', type=int, default=0)
    parser.add_argument('--linux-libgcc', type=Path, help='Digest-verified same-architecture libgcc supplier')
    parser.add_argument('--linux-native-origin', type=Path, help='Owned Linux native-input provenance record')
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.out.absolute(), epoch=args.source_date_epoch, codesign_identity=args.codesign_identity,
                               linux_libgcc=args.linux_libgcc, linux_native_origin=args.linux_native_origin), sort_keys=True))
    except (ValueError, OSError, subprocess.SubprocessError, metadata.PackageNotFoundError) as error:
        parser.exit(2, str(error) + '\n')
