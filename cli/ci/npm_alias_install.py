"""Install original npm alias tarballs offline after loopback-only cache priming."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from urllib.parse import unquote, urlsplit
import publication


def platform_payload(platform, *, sdk_manifest=None):
    """Read SDK publications only with their explicit, exact artifact authority."""
    if sdk_manifest is None:
        members = publication.archive_members(platform)
        return json.loads(members['package/package.json']), platform.read_bytes()
    publication.require(isinstance(sdk_manifest, dict) and isinstance(sdk_manifest.get('agentsSdk'), dict),
                        'Explicit SDK publication manifest required')
    artifacts = sdk_manifest.get('artifacts')
    publication.require(isinstance(artifacts, list), 'SDK publication artifact list required')
    matches = [row for row in artifacts if isinstance(row, dict) and row.get('kind') == 'npm-platform']
    publication.require(len(matches) == 1 and matches[0].get('path') == platform.name,
                        'SDK npm artifact filename differs from publication')
    artifact = matches[0]
    publication.require(platform.is_file() and not platform.is_symlink()
                        and 0 < platform.stat().st_size <= publication.MAX_BYTES, 'Regular bounded SDK npm archive required')
    before = platform.stat()
    descriptor = os.open(platform, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        publication.require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), 'SDK npm archive changed before read')
        payload = source.read(publication.MAX_BYTES + 1)
    after = platform.stat()
    identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
    publication.require(not platform.is_symlink() and len(payload) == before.st_size
                        and identity(before) == identity(opened) == identity(after), 'SDK npm archive changed during read')
    publication.require(type(artifact.get('byteLength')) is int and artifact['byteLength'] == len(payload)
                        and artifact.get('sha256') == hashlib.sha256(payload).hexdigest(), 'SDK npm artifact digest or length differs')
    table = publication.decode_sdk_archive(payload, sdk_manifest, label=platform.name)
    # The full typed SDK tree is authenticated before projecting npm metadata.
    manifest = json.loads(table['files']['package/package.json'][0], object_pairs_hook=publication.object_pairs)
    return manifest, payload


def install(meta, platform, prefix, *, env, cwd, command, log, sdk_manifest=None):
    manifest, payload = platform_payload(platform, sdk_manifest=sdk_manifest)
    name, version = manifest['name'], manifest['version']
    publication.require(name == '@openprose/prose-cli' and manifest.get('os') and manifest.get('cpu'), 'Expected exact same-name platform package')
    requests = []
    class Registry(BaseHTTPRequestHandler):
        def do_GET(self):
            route = unquote(urlsplit(self.path).path)
            requests.append(route)
            if route == '/@openprose/prose-cli':
                record = dict(manifest)
                record['dist'] = {'tarball': registry + '/payload.tgz',
                                  'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(payload).digest()).decode(),
                                  'shasum': hashlib.sha1(payload).hexdigest()}
                content = json.dumps({'name': name, 'versions': {version: record}, 'dist-tags': {}}).encode()
                content_type = 'application/json'
            elif route == '/payload.tgz':
                content, content_type = payload, 'application/octet-stream'
            else:
                self.send_error(404); return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers(); self.wfile.write(content)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
    registry = 'http://127.0.0.1:' + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cache_env = dict(env, npm_config_registry=registry, npm_config_offline='false', npm_config_update_notifier='false')
    try:
        command(['npm', 'cache', 'add', name + '@' + version, '--ignore-scripts'],
                env=cache_env, cwd=cwd, log=log.with_name(log.stem + '-cache.log'), timeout=120)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    publication.require('/payload.tgz' in requests and all(p in ('/@openprose/prose-cli', '/payload.tgz', '/npm') for p in requests), 'Unexpected cache registry request')
    offline_env = dict(cache_env, npm_config_offline='true')
    command(['npm', 'install', '--global', '--prefix', prefix, '--offline', '--ignore-scripts', '--no-audit',
             '--no-fund', meta], env=offline_env, cwd=cwd, log=log, timeout=120)
    return {'registryScope': 'loopback-only', 'registryUrl': registry, 'cacheRequests': requests,
            'installation': 'offline-original-root-tarball', 'platformVersion': version,
            'platformSha256': hashlib.sha256(payload).hexdigest()}
