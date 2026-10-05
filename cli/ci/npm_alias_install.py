"""Install original npm alias tarballs offline after loopback-only cache priming."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import unquote, urlsplit
import publication


def install(meta, platform, prefix, *, env, cwd, command, log):
    members = publication.archive_members(platform)
    manifest = json.loads(members['package/package.json'])
    name, version = manifest['name'], manifest['version']
    publication.require(name == '@openprose/prose-cli' and manifest.get('os') and manifest.get('cpu'), 'Expected exact same-name platform package')
    payload = platform.read_bytes()
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
    return {'registryScope': 'loopback-only', 'cacheRequests': requests,
            'installation': 'offline-original-root-tarball', 'platformVersion': version,
            'platformSha256': hashlib.sha256(payload).hexdigest()}
