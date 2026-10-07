#!/usr/bin/env python3
"""Fetch fixed, checksum-pinned RC3 inputs for native upgrade qualification."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
from typing import BinaryIO, Callable

VERSION = '0.15.0-rc.3'
SOURCE = '1941a34c3503f9a1417aebea1bc19ecada91e58c'
MANIFEST_SHA256 = '571eb285f964ea980d4e3aec6aa574f782b9d51f2a6934d219bc2ae7d8755e4f'
BASE_URL = 'https://pkg.prose.md/cli/releases/' + VERSION + '/'
PLATFORMS = ('darwin-arm64', 'darwin-x64', 'linux-arm64-gnu', 'linux-x64-gnu')
MAX_MANIFEST = 1024 * 1024
MAX_ARTIFACT = 256 * 1024 * 1024
SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}')
SHA256 = re.compile(r'[0-9a-f]{64}')

class BoundedWriter:
    def __init__(self, stream: BinaryIO, maximum: int):
        self.stream, self.maximum, self.count = stream, maximum, 0
    def write(self, data: bytes) -> int:
        if self.count + len(data) > self.maximum:
            raise ValueError('Previous release download exceeded its byte bound')
        count = self.stream.write(data)
        if count != len(data):
            raise ValueError('Previous release download write failed')
        self.count += count
        return count

def curl_download(url: str, sink: BoundedWriter, maximum: int) -> None:
    # No curlrc, redirects, ambient credentials/proxies, shell, or diagnostic bodies.
    process = subprocess.Popen([
        '/usr/bin/curl', '--disable', '--fail', '--silent', '--proto', '=https',
        '--tlsv1.2', '--connect-timeout', '20', '--max-time', '120',
        '--max-filesize', str(maximum), url,
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
       env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, start_new_session=True)
    try:
        assert process.stdout is not None
        while True:
            block = process.stdout.read(65536)
            if not block:
                break
            sink.write(block)
        if process.wait(timeout=5) != 0:
            raise ValueError('Previous release download failed')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()

def decode_manifest(data: bytes) -> dict:
    if not data or len(data) > MAX_MANIFEST or hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
        raise ValueError('Previous published manifest identity mismatch')
    try:
        manifest = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Previous published manifest is invalid') from None
    if not isinstance(manifest, dict) or manifest.get('schema') != 'openprose.cli-distribution/1' or manifest.get('version') != VERSION or manifest.get('source') != SOURCE:
        raise ValueError('Expected the fixed published RC3 manifest')
    return manifest

def select_artifacts(manifest: dict, platform: str) -> list[dict]:
    if platform not in PLATFORMS:
        raise ValueError('Unsupported previous native platform')
    artifacts = manifest.get('artifacts')
    if not isinstance(artifacts, list):
        raise ValueError('Previous manifest artifact inventory is invalid')
    wanted = {('standalone','rust',platform), ('standalone','bun',platform),
              ('npm','bun',platform), ('npm','bun','all')}
    selected: dict[tuple, dict] = {}
    names: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError('Previous manifest artifact record is invalid')
        identity = (item.get('kind'),item.get('implementation'),item.get('platform'))
        if not all(isinstance(value,str) for value in identity):
            raise ValueError('Previous manifest artifact classification is invalid')
        if identity not in wanted:
            continue
        name, digest, size = item.get('name'),item.get('sha256'),item.get('size')
        if identity in selected or not isinstance(name,str) or not SAFE_NAME.fullmatch(name) or '..' in name or name in {'manifest.json','download-receipt.json'} or name in names:
            raise ValueError('Previous selected artifact name or selection is invalid')
        if not isinstance(digest,str) or not SHA256.fullmatch(digest) or type(size) is not int or not 0 < size <= MAX_ARTIFACT:
            raise ValueError('Previous selected artifact identity is invalid')
        selected[identity] = item
        names.add(name)
    if set(selected) != wanted:
        raise ValueError('Exactly four previous platform and meta artifacts are required')
    return [selected[key] for key in [('standalone','rust',platform),('standalone','bun',platform),('npm','bun','all'),('npm','bun',platform)]]

def fetch(platform: str, out: Path, downloader: Callable = curl_download) -> dict:
    if platform not in PLATFORMS:
        raise ValueError('Unsupported previous native platform')
    out = out.absolute()
    for parent in (out.parent,*out.parent.parents):
        if parent.is_symlink():
            raise ValueError('Previous release destination parents must not be symlinks')
    if not out.parent.is_dir():
        raise ValueError('Previous release destination parent must exist')
    try:
        out.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError('Previous release destination must be fresh') from None
    directory = os.open(out,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    identity = os.fstat(directory)
    def custody() -> None:
        current = out.lstat()
        if out.is_symlink() or (current.st_dev,current.st_ino)!=(identity.st_dev,identity.st_ino):
            raise ValueError('Previous release destination custody changed')
    def write(name: str, maximum: int, url: str | None = None, content: bytes | None = None) -> bytes:
        custody()
        fd = os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'w+b') as stream:
            sink=BoundedWriter(stream,maximum)
            if content is not None:
                sink.write(content)
            else:
                try:
                    downloader(url,sink,maximum)
                except Exception:
                    raise ValueError('Previous release download failed or exceeded its byte bound') from None
            stream.flush()
            os.fsync(stream.fileno())
            stat=os.fstat(stream.fileno())
            named=os.stat(name,dir_fd=directory,follow_symlinks=False)
            if (stat.st_dev,stat.st_ino,stat.st_size)!=(named.st_dev,named.st_ino,named.st_size):
                raise ValueError('Previous release file custody changed')
            stream.seek(0)
            data=stream.read(maximum+1)
            if len(data)!=sink.count or len(data)>maximum:
                raise ValueError('Previous release file size changed')
        custody()
        return data
    try:
        data=write('manifest.json',MAX_MANIFEST,BASE_URL+'manifest.json')
        write_manifest_bytes=data
        manifest=decode_manifest(data)
        rows=[]
        for item in select_artifacts(manifest,platform):
            data=write(item['name'],item['size'],BASE_URL+item['name'])
            digest=hashlib.sha256(data).hexdigest()
            if len(data)!=item['size'] or digest!=item['sha256']:
                raise ValueError('Previous artifact byte identity mismatch')
            rows.append({'name':item['name'],'sha256':digest,'byteLength':len(data)})
        # Recheck the whole retained inventory after the last download. A later
        # download must not silently replace an earlier verified input.
        for row in [{'name':'manifest.json','sha256':MANIFEST_SHA256,'byteLength':len(write_manifest_bytes)},*rows]:
            fd=os.open(row['name'],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
            with os.fdopen(fd,'rb') as stream:
                before=os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size!=row['byteLength']:
                    raise ValueError('Previous retained input custody mismatch')
                digest=hashlib.sha256()
                for block in iter(lambda:stream.read(65536),b''):
                    digest.update(block)
                after=os.fstat(stream.fileno())
                named=os.stat(row['name'],dir_fd=directory,follow_symlinks=False)
                if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or (named.st_dev,named.st_ino)!=(after.st_dev,after.st_ino) or digest.hexdigest()!=row['sha256']:
                    raise ValueError('Previous retained input identity mismatch')
        custody()
        receipt={'schema':'openprose.prior-release-qualification-inputs/1','version':VERSION,
                 'source':SOURCE,'platform':platform,'manifestSha256':MANIFEST_SHA256,'artifacts':rows}
        write('download-receipt.json',MAX_MANIFEST,content=(json.dumps(receipt,sort_keys=True,indent=2)+'\n').encode())
        return receipt
    finally:
        os.close(directory)

def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform',choices=PLATFORMS,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        fetch(args.platform,args.out)
    except (ValueError,OSError,subprocess.SubprocessError):
        print('Previous RC3 input fetch failed; use a fresh output directory and verify fixed release availability.',file=sys.stderr)
        return 1
    print('Pinned previous RC3 platform inputs verified.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
