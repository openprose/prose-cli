#!/usr/bin/env python3
"""Validate a reviewed signed release plan; never build or change package bytes."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile
import tarfile
from package_local import npm_payload_version

REPOSITORY = 'openprose/prose-cli'
PLATFORMS = ('darwin-arm64', 'darwin-x64', 'linux-arm64-gnu', 'linux-x64-gnu')
PACKAGES = tuple('@openprose/prose-cli-' + p for p in PLATFORMS) + ('@openprose/prose-cli',)
IDENTITY = 'https://github.com/' + REPOSITORY + '/.github/workflows/cli-publish.yml@refs/heads/main'
MAX_BYTES = 512 * 1024 * 1024


def require(ok, message):
    if not ok:
        raise ValueError(message)


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def read_json(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size < 1024 * 1024, 'Invalid JSON file')
    return json.loads(path.read_text(), object_pairs_hook=object_pairs)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    return isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', name) and '..' not in name


def load_plan(path):
    plan = read_json(path)
    require(set(plan) == {'schema', 'version', 'source', 'qualification', 'artifacts', 'preflight', 'macos', 'npmProvenance', 'signing'}, 'Invalid plan fields')
    require(plan['schema'] in ('openprose.cli-publication/1', 'openprose.cli-publication/2'), 'Unsupported publication plan')
    require(re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:rc|dev)\.(?:0|[1-9][0-9]*))?', plan['version']), 'Invalid version')
    require(re.fullmatch(r'[0-9a-f]{40}', plan['source']), 'Invalid source')
    q = plan['qualification']
    require(set(q) == {'status', 'evidence'} and q['status'] == 'kernel-smoke-qualified', 'Kernel qualification required')
    require(re.fullmatch(r'https://github.com/openprose/[a-z0-9-]+/(?:blob|tree)/[0-9a-f]{40}/[^\s?#]+', q['evidence']), 'Immutable qualification evidence required')
    require(plan['npmProvenance'] is True, 'npm provenance is mandatory until the owner approves a different policy')
    require(isinstance(plan['artifacts'], list) and 1 <= len(plan['artifacts']) <= 128, 'Invalid artifact inventory')
    names = set()
    for item in plan['artifacts']:
        require(set(item) == {'name', 'sha256', 'size', 'kind', 'platform', 'implementation'}, 'Invalid artifact fields')
        require(safe_name(item['name']) and item['name'] not in names, 'Unsafe or duplicate artifact name')
        names.add(item['name'])
        require(re.fullmatch(r'[0-9a-f]{64}', item['sha256']), 'Invalid artifact digest')
        require(type(item['size']) is int and (0 if item['kind'] == 'evidence' else 1) <= item['size'] <= MAX_BYTES, 'Invalid artifact size')
        require(item['kind'] in ('standalone', 'npm', 'evidence'), 'Invalid artifact kind')
        require(item['size'] != 0 or item['sha256'] == hashlib.sha256(b'').hexdigest(), 'Empty evidence digest mismatch')
        require(item['platform'] in (*PLATFORMS, 'all'), 'Invalid platform')
        require(item['implementation'] in ('bun', 'rust', 'shared'), 'Invalid implementation')
    require(sum(a['size'] for a in plan['artifacts']) <= 2 * 1024**3, 'Artifact set exceeds budget')
    standalone = [(a['implementation'], a['platform']) for a in plan['artifacts'] if a['kind'] == 'standalone']
    expected = {(i, p) for i in ('bun', 'rust') for p in PLATFORMS}
    require(len(standalone) == len(expected) and set(standalone) == expected, 'Both runners on all four platforms required')
    require(plan['preflight'] in names, 'Protected release preflight required')
    require(plan['signing'] in ('unsigned-rc', 'apple-notarized'), 'Explicit signing policy required')
    if plan['signing'] == 'unsigned-rc':
        require('-rc.' in plan['version'] and plan['macos'] == {}, 'Unsigned publication is limited to an explicit RC')
    else:
        require(set(plan['macos']) == {'darwin-arm64', 'darwin-x64'}, 'Both macOS signing records required')
    for record in plan['macos'].values():
        require(set(record) == {'receipt', 'zip', 'teamId'}, 'Invalid signing reference')
        require(record['receipt'] in names and record['zip'] in names and re.fullmatch(r'[A-Z0-9]{10}', record['teamId']), 'Invalid signing reference')
    return plan


def archive_members(path):
    """Read bounded regular members without filesystem extraction or execution."""
    result = {}
    with tarfile.open(path, 'r:gz') as archive:
        total = 0
        for index, member in enumerate(archive):
            require(index < 128, 'Too many archive members')
            require(not member.name.startswith('/') and '..' not in Path(member.name).parts and '\\' not in member.name, 'Unsafe archive path')
            require(member.isdir() or member.isfile(), 'Links and special members are forbidden')
            if member.isdir():
                continue
            require(member.name not in result and 0 <= member.size <= MAX_BYTES, 'Invalid archive member')
            total += member.size
            require(total <= MAX_BYTES, 'Expanded archive exceeds budget')
            result[member.name] = archive.extractfile(member).read()
    return result


def verify_local(plan, root):
    for item in plan['artifacts']:
        p = root / item['name']
        require(p.is_file() and not p.is_symlink() and p.stat().st_size == item['size'] and digest(p) == item['sha256'], 'Artifact bytes differ from reviewed plan: ' + item['name'])
    preflight = read_json(root / plan['preflight'])
    moving = preflight.get('schema') == 'openprose.kernel-rc-release-evidence/1'
    require(plan['signing'] != 'unsigned-rc' or moving, 'Unsigned RC must preserve latest-kernel startup')
    require(preflight.get('status') == 'pass' and preflight.get('failures') == [], 'Release qualification must pass')
    require(preflight.get('sourceSha') == plan['source'] and preflight.get('version') == plan['version'], 'Preflight does not bind the candidate')
    if moving:
        require(any(a['name'] == 'SHA256SUMS' and a['kind'] == 'evidence' for a in plan['artifacts']), 'Aggregate install checksums required')
        sums = ''.join(a['sha256'] + '  ' + a['name'] + '\n' for a in sorted(plan['artifacts'], key=lambda a: a['name']) if a['kind'] in ('standalone', 'npm')).encode()
        require((root / 'SHA256SUMS').read_bytes() == sums, 'Aggregate install checksums do not match reviewed archives')
        require(preflight.get('imageSource') == 'published-on-run' and set(preflight.get('platforms', {})) == set(PLATFORMS), 'Latest-kernel platform qualification required')
        expected_image = preflight.get('embeddedDiagnosticImage', {})
        require(set(expected_image) == {'formatVersion', 'version', 'sha256', 'manifestSha256', 'purpose'} and expected_image['purpose'] == 'functional-alpha-placeholder', 'Invalid diagnostic image binding')
        require(preflight.get('kernelPolicy') == {'schema': 'openprose.published-kernel-policy/1', 'resolution': 'latest-published-on-run', 'entrypoint': 'https://pkg.prose.md/kernel.md', 'pinning': 'per-run'}, 'Unexpected kernel acquisition policy')
        require(preflight.get('liveSmoke', {}).get('schema') == 'openprose.kernel-rc-live-smoke/1' and preflight['liveSmoke'].get('version') == plan['version'] and preflight['liveSmoke'].get('sourceSha') == plan['source'] and preflight['liveSmoke'].get('status') == 'pass', 'Exact-source live smoke evidence required')
        for platform, record in preflight['platforms'].items():
            require(record.get('status') == 'pass' and safe_name(record.get('report')), 'Missing platform evidence')
            require(any(a['name'] == record['report'] and a['kind'] == 'evidence' for a in plan['artifacts']), 'Platform report is not bound to the plan')
            build = read_json(root / record['report'])
            checks = build.get('checks', [])
            require(build.get('schema') == 'openprose.kernel-rc-build/1' and build.get('sourceRevision') == plan['source'] and build.get('version') == plan['version'] and build.get('platform') == platform and build.get('imageSource') == 'published-on-run' and build.get('testSeamsEnabled') is False, 'Platform evidence identity mismatch')
            require(len(checks) == 5 and {c.get('name') for c in checks} == {'built-bun','built-rust','installed-bun','installed-rust','installed-npm'} and all(c.get('status') == 'passed' for c in checks), 'Incomplete native install qualification')
    else:
        require(preflight.get('schema') == 'openprose.release-preflight-report/1' and preflight.get('protectedAuthority', {}).get('status') == 'pass', 'Existing protected release preflight must pass')
        image = preflight.get('image', {})
        require(image.get('releaseEligible') is True and image.get('purpose') == 'canonical-language-runtime' and re.fullmatch(r'[0-9a-f]{64}', image.get('imageSha256', '')) and re.fullmatch(r'[0-9a-f]{64}', image.get('manifestSha256', '')), 'Protected preflight must qualify a canonical image')
        expected_image = {'formatVersion': 1, 'version': image.get('version'), 'sha256': image['imageSha256'], 'manifestSha256': image['manifestSha256'], 'purpose': image['purpose'], 'releaseEligible': True}
    same_name = plan['schema'] == 'openprose.cli-publication/2'
    require(not same_name or moving, 'Same-name npm layout requires published-kernel qualification')
    packages = {}
    binary_hashes = {}
    for item in plan['artifacts']:
        if item['kind'] not in ('npm', 'standalone'):
            continue
        members = archive_members(root / item['name'])
        if item['kind'] == 'standalone':
            binaries = [b for n, b in members.items() if n.endswith('/prose')]
            require(len(binaries) == 1, 'Expected one standalone binary')
            binary_hashes[(item['implementation'], item['platform'])] = hashlib.sha256(binaries[0]).hexdigest()
            continue
        require('package/package.json' in members, 'Missing npm manifest')
        meta = json.loads(members['package/package.json'], object_pairs_hook=object_pairs)
        alias = '@openprose/prose-cli' if item['platform'] == 'all' else '@openprose/prose-cli-' + item['platform']
        name = meta.get('name')
        expected_name = '@openprose/prose-cli' if same_name else alias
        expected_version = npm_payload_version(plan['version'], item['platform']) if same_name and item['platform'] != 'all' else plan['version']
        require(alias in PACKAGES and alias not in packages and name == expected_name and meta.get('version') == expected_version, 'Wrong npm package identity')
        require(meta.get('repository', {}).get('url') == 'git+https://github.com/' + REPOSITORY + '.git', 'npm repository must match OIDC publisher')
        require(not meta.get('scripts'), 'Package lifecycle scripts are forbidden')
        config = meta.get('publishConfig', {})
        require(set(config) <= {'access', 'provenance'} and config.get('access', 'public') == 'public' and config.get('provenance', True) is True, 'Unsafe npm publish configuration')
        cohort = meta.get('openproseCohort', {})
        require(cohort.get('version') == plan['version'] and cohort.get('sourceRevision') == plan['source'] and cohort.get('admittedPlatforms') == sorted(PLATFORMS), 'Package cohort does not match qualified source/platforms')
        if moving:
            require(cohort.get('schema') == ('openprose.npm-cohort/3' if same_name else 'openprose.npm-cohort/2') and cohort.get('releaseChannel') == 'kernel-release-candidate' and cohort.get('purpose') == 'published-kernel-loader' and cohort.get('imageSource') == 'published-on-run' and cohort.get('embeddedDiagnosticImage') == expected_image and cohort.get('kernelPolicy') == preflight['kernelPolicy'], 'Package does not match latest-kernel qualification')
        else:
            require(cohort.get('schema') == 'openprose.npm-cohort/1' and cohort.get('releaseChannel') == 'release-candidate' and cohort.get('image') == expected_image and cohort.get('purpose') == 'canonical-language-runtime', 'Package does not match canonical-image qualification')
        require(cohort.get('releaseEligible') is False and cohort.get('publicationAuthorized') is False, 'Local packaging must not grant publication authority')
        if alias == PACKAGES[-1]:
            expected_dependencies = {n: ('npm:@openprose/prose-cli@' + npm_payload_version(plan['version'], n.removeprefix('@openprose/prose-cli-')) if same_name else plan['version']) for n in PACKAGES[:-1]}
            require(meta.get('optionalDependencies') == expected_dependencies, 'Incomplete platform cohort')
        else:
            if same_name:
                platform = item['platform']
                os_name, cpu = platform.split('-')[:2]
                require(meta.get('os') == [os_name] and meta.get('cpu') == [cpu]
                        and meta.get('openprosePlatform') == platform
                        and (meta.get('libc') == ['glibc'] if os_name == 'linux' else 'libc' not in meta), 'Wrong npm platform selectors')
            require(meta.get('openproseSourceRevision') == plan['source'], 'npm source mismatch')
            if moving:
                require('openproseImage' not in meta and meta.get('openproseEmbeddedDiagnosticImage') == expected_image and meta.get('openproseKernelPolicy') == preflight['kernelPolicy'] and meta.get('openproseImageSource') == 'published-on-run', 'Wrong kernel-loader package metadata')
            else:
                require(meta.get('openproseImage') == expected_image, 'Sentinel or unqualified image')
            require('package/bin/prose' in members, 'Missing npm binary')
            binary_hashes[('npm', alias.removeprefix('@openprose/prose-cli-'))] = hashlib.sha256(members['package/bin/prose']).hexdigest()
        packages[alias] = item['name']
    require(set(packages) == set(PACKAGES), 'All five npm packages required')
    for p in PLATFORMS:
        require(binary_hashes[('npm', p)] == binary_hashes[('bun', p)], 'npm and standalone Bun bytes differ')
    if moving:
        from kernel_rc_evidence import verify_platform_evidence, verify_live_evidence
        for platform, record in preflight['platforms'].items():
            verify_platform_evidence(plan, root, platform, record['report'], binary_hashes)
        verify_live_evidence(plan, root, binary_hashes)
        for implementation in ('bun', 'rust'):
            observation = preflight['liveSmoke'].get('runners', {}).get(implementation, {})
            require(observation.get('accepted') is True and observation.get('helloExact') is True and observation.get('binarySha256') == binary_hashes[(implementation, 'darwin-arm64')], 'Live smoke does not bind exact release binary')
    return packages, binary_hashes


def run(argv):
    environment = {k: v for k, v in os.environ.items() if k != 'NPM_BOOTSTRAP_TOKEN'}
    completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, env=environment)
    require(completed.returncode == 0, 'External verification/publication command failed: ' + Path(argv[0]).name)
    return completed.stdout


def fetch(plan, root):
    require(not root.exists(), 'Use a fresh artifact directory')
    ref = json.loads(run(['gh', 'api', 'repos/' + REPOSITORY + '/commits/v' + plan['version']]))
    require(ref['sha'] == plan['source'], 'Release tag does not match reviewed source')
    release = json.loads(run(['gh', 'release', 'view', 'v' + plan['version'], '--repo', REPOSITORY, '--json', 'isDraft,isPrerelease,tagName,assets']))
    require(release.get('tagName') == 'v' + plan['version'], 'Wrong release tag')
    require(release['isDraft'] is True or (plan['signing'] == 'unsigned-rc' and '-rc.' in plan['version'] and release.get('isPrerelease') is True), 'Only drafts or published unsigned release candidates are accepted')
    expected = {a['name']: a['size'] for a in plan['artifacts'] if a['size'] > 0}
    observed = {a['name']: a['size'] for a in release['assets']}
    require(len(release['assets']) == len(observed), 'Duplicate release asset names')
    require(all(observed.get(name) == size for name, size in expected.items()), 'Release inventory differs from reviewed plan')
    extras = {name: size for name, size in observed.items() if name not in expected}
    require(all(name in {a['name'] + '.sigstore.json' for a in plan['artifacts']} and type(size) is int and 0 < size <= 1024 * 1024 for name, size in extras.items()), 'Unrecognized release asset or oversized signature bundle')
    root.mkdir(parents=True)
    for item in plan['artifacts']:
        if item['size'] == 0:
            require(item['kind'] == 'evidence' and item['sha256'] == hashlib.sha256(b'').hexdigest(), 'Only verified empty evidence can be reconstructed')
            (root / item['name']).write_bytes(b'')
            continue
        run(['gh', 'release', 'download', 'v' + plan['version'], '--repo', REPOSITORY, '--pattern', item['name'], '--dir', str(root)])
    verify_local(plan, root)


def verify_macos(plan, root, key, key_id, issuer):
    _, binaries = verify_local(plan, root)
    for platform, ref in plan['macos'].items():
        receipt = read_json(root / ref['receipt'])
        require(receipt.get('schema') == 'openprose.macos-signing/1' and receipt.get('teamId') == ref['teamId'], 'Wrong signing identity')
        require(receipt.get('notarization', {}).get('sha256') == digest(root / ref['zip']), 'Wrong notarization ZIP')
        for implementation in ('bun', 'rust'):
            require(receipt.get('binaries', {}).get(implementation, {}).get('signedSha256') == binaries[(implementation, platform)], 'Signed binary differs from packaged binary')
        with tempfile.TemporaryDirectory(prefix='prose-signature-check-') as temporary:
            directory = Path(temporary)
            (directory / 'receipt.json').write_bytes((root / ref['receipt']).read_bytes())
            (directory / 'notarization.zip').write_bytes((root / ref['zip']).read_bytes())
            with zipfile.ZipFile(directory / 'notarization.zip') as archive:
                require(set(archive.namelist()) == {'prose-bun', 'prose-rust'} and len(archive.infolist()) == 2, 'Unexpected notarization archive')
                for name in ('prose-bun', 'prose-rust'):
                    require(0 < archive.getinfo(name).file_size <= MAX_BYTES, 'Oversized signed executable')
                    (directory / name).write_bytes(archive.read(name))
                    (directory / name).chmod(0o755)
            run(['python3', str(Path(__file__).with_name('sign_macos.py')), '--verify-existing', str(directory), '--team-id', ref['teamId'], '--identity', receipt['identity'], '--notary-key', str(key), '--notary-key-id', key_id, '--notary-issuer', issuer])


def npm_integrity(path):
    return 'sha512-' + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()


def npm_environment(directory):
    # Retain CI OIDC variables for registry auth and mandatory provenance, but
    # exclude token aliases and every ambient npm configuration override.
    environment = {k: v for k, v in os.environ.items()
                   if k not in {'NPM_BOOTSTRAP_TOKEN', 'NPM_TOKEN', 'NODE_AUTH_TOKEN'}
                   and not k.lower().startswith('npm_config_')}
    environment.update(HOME=str(directory), NPM_CONFIG_USERCONFIG=str(directory / 'user.npmrc'),
                       NPM_CONFIG_GLOBALCONFIG=str(directory / 'global.npmrc'),
                       NPM_CONFIG_CACHE=str(directory / 'cache'))
    return environment


def npm_command(arguments, token=None):
    with tempfile.TemporaryDirectory(prefix='prose-npm-auth-') as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        userconfig = directory / 'user.npmrc'
        with os.fdopen(os.open(userconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            if token is not None:
                require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_=-]{20,8192}', token), 'Invalid bootstrap credential format')
                stream.write('//registry.npmjs.org/:_authToken=' + token + '\n')
        (directory / 'global.npmrc').write_text('')
        return subprocess.run(['npm', *arguments], cwd=directory, env=npm_environment(directory),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)


def npm_read(arguments):
    return npm_command(arguments)


def registry_package_exists(package):
    result = npm_read(['view', package, 'name', '--json', '--registry=https://registry.npmjs.org'])
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise ValueError('Package-name lookup failed; no bootstrap attempted')
    if result.returncode:
        require(isinstance(value, dict) and value.get('error', {}).get('code') == 'E404',
                'Package-name lookup failed; no bootstrap attempted')
        return False
    require(value == package, 'Package-name lookup returned an unexpected identity')
    return True


def publication_routes(existing, names_exist, bootstrap, token):
    routes = {}
    for name in PACKAGES:
        if existing[name] is not None:
            routes[name] = 'already-published'
        elif names_exist[name]:
            routes[name] = 'oidc'
        else:
            require(name in PACKAGES[:-1], 'The root package must already exist; bootstrap cannot authorize it')
            require(bootstrap and token, 'An absent platform package requires explicit bootstrap and NPM_BOOTSTRAP_TOKEN')
            routes[name] = 'bootstrap-token'
    return routes


def publish_package(name, package, tag, route, token):
    require(route in {'oidc', 'bootstrap-token'}, 'Invalid registry credential route')
    if route == 'bootstrap-token':
        require(name in PACKAGES[:-1] and token, 'Bootstrap is limited to absent platform packages')
        require(not registry_package_exists(name), 'Package now exists; bootstrap refused without credential fallback')
    result = npm_command(['publish', str(package.resolve()), '--access=public', '--ignore-scripts',
                          '--provenance', '--tag=' + tag, '--registry=https://registry.npmjs.org'],
                         token if route == 'bootstrap-token' else None)
    # Never retry an authentication error using a different credential route.
    require(result.returncode == 0, 'npm publication failed on the selected credential route; no fallback attempted')


def registry_integrity(package, version):
    result = npm_read(['view', package + '@' + version, 'dist.integrity', '--json', '--registry=https://registry.npmjs.org'])
    if result.returncode:
        try:
            error = json.loads(result.stdout)
        except ValueError:
            raise ValueError('Registry lookup failed; no publication attempted')
        require(error.get('error', {}).get('code') == 'E404', 'Registry lookup failed')
        return None
    return json.loads(result.stdout)


def sign_artifacts(plan, root):
    signatures = root / 'signatures'
    signatures.mkdir(exist_ok=False)
    for item in plan['artifacts']:
        blob = root / item['name']
        bundle = signatures / (item['name'] + '.sigstore.json')
        run(['cosign', 'sign-blob', '--yes', '--bundle', str(bundle), str(blob)])
        run(['cosign', 'verify-blob', '--bundle', str(bundle), '--certificate-identity', IDENTITY, '--certificate-oidc-issuer', 'https://token.actions.githubusercontent.com', str(blob)])


def publish(plan, root, key, key_id, issuer, bootstrap=False, sign_only=False):
    require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY and os.environ.get('GITHUB_REF') == 'refs/heads/main' and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'Publication requires the main workflow')
    require(os.environ.get('GITHUB_WORKFLOW_REF') == REPOSITORY + '/.github/workflows/cli-publish.yml@refs/heads/main', 'Wrong publisher workflow')
    require(not os.environ.get('NODE_AUTH_TOKEN') and not os.environ.get('NPM_TOKEN'), 'Only OIDC publication is allowed')
    bootstrap_token = os.environ.pop('NPM_BOOTSTRAP_TOKEN', '')
    require(not sign_only or (not bootstrap and not bootstrap_token), 'Sign-only forbids npm bootstrap authorization and credentials')
    require(bootstrap or not bootstrap_token, 'Bootstrap credential requires explicit bootstrap authorization')
    require(not bootstrap_token or re.fullmatch(r'[A-Za-z0-9_=-]{20,8192}', bootstrap_token), 'Invalid bootstrap credential format')
    repo = json.loads(run(['gh', 'api', 'repos/' + REPOSITORY]))
    require(repo.get('visibility') == 'public', 'npm provenance requires public source; owner decision pending')
    packages, _ = verify_local(plan, root)
    if plan['signing'] == 'apple-notarized':
        require(key and key_id and issuer, 'Apple credentials required for signed publication')
        verify_macos(plan, root, key, key_id, issuer)
    if sign_only:
        sign_artifacts(plan, root)
        (root / 'publication-receipt.json').write_text(json.dumps({'schema': 'openprose.cli-publication-receipt/1', 'operation': 'sign-only', 'version': plan['version'], 'source': plan['source'], 'npmStatus': 'not-published', 'npm': {}, 'credentialRoutes': {}, 'githubReleasePromoted': False, 'signing': plan['signing']}, indent=2) + '\n')
        return
    # Preflight every package before any registry mutation; existing exact bytes
    # support recovery after a partial publication, never version replacement.
    if plan['schema'] == 'openprose.cli-publication/2':
        require(not bootstrap and not bootstrap_token, 'Same-name npm publication forbids bootstrap credentials')
        require(registry_package_exists('@openprose/prose-cli'), 'Existing root npm identity is required')
        versions = {alias: npm_payload_version(plan['version'], alias.removeprefix('@openprose/prose-cli-')) if alias != PACKAGES[-1] else plan['version'] for alias in PACKAGES}
        existing = {alias: registry_integrity('@openprose/prose-cli', version) for alias, version in versions.items()}
        for alias, integrity in existing.items():
            require(integrity is None or integrity == npm_integrity(root / packages[alias]), 'Version already exists with different bytes')
        sign_artifacts(plan, root)
        root_tag = 'rc' if '-rc.' in plan['version'] else 'dev' if '-dev.' in plan['version'] else 'latest'
        receipts = {}
        for alias in PACKAGES:
            package = root / packages[alias]
            require(digest(package) == next(a['sha256'] for a in plan['artifacts'] if a['name'] == package.name), 'Package changed before publication')
            tag = root_tag if alias == PACKAGES[-1] else 'platform-' + alias.removeprefix('@openprose/prose-cli-')
            if existing[alias] is None:
                publish_package('@openprose/prose-cli', package, tag, 'oidc', None)
            require(registry_integrity('@openprose/prose-cli', versions[alias]) == npm_integrity(package), 'Published integrity mismatch; stop and inspect')
            receipts[alias] = {'name': '@openprose/prose-cli', 'version': versions[alias], 'artifact': package.name, 'tag': tag, 'credentialRoute': 'oidc'}
        (root / 'publication-receipt.json').write_text(json.dumps({'schema': 'openprose.cli-publication-receipt/2', 'version': plan['version'], 'source': plan['source'], 'npm': receipts, 'tag': root_tag, 'githubReleasePromoted': False, 'signing': plan['signing']}, indent=2) + '\n')
        return
    existing = {name: registry_integrity(name, plan['version']) for name in PACKAGES}
    for name, integrity in existing.items():
        require(integrity is None or integrity == npm_integrity(root / packages[name]), 'Version already exists with different bytes')
    names_exist = {name: registry_package_exists(name) for name in PACKAGES}
    routes = publication_routes(existing, names_exist, bootstrap, bootstrap_token)
    tag = 'rc' if '-rc.' in plan['version'] else 'dev' if '-dev.' in plan['version'] else 'latest'
    sign_artifacts(plan, root)
    for name in PACKAGES:
        package = root / packages[name]
        require(digest(package) == next(a['sha256'] for a in plan['artifacts'] if a['name'] == package.name), 'Package changed before publication')
        if existing[name] is None:
            publish_package(name, package, tag, routes[name], bootstrap_token if routes[name] == 'bootstrap-token' else None)
        require(registry_integrity(name, plan['version']) == npm_integrity(package), 'Published integrity mismatch; stop and inspect')
    (root / 'publication-receipt.json').write_text(json.dumps({'schema': 'openprose.cli-publication-receipt/1', 'version': plan['version'], 'source': plan['source'], 'npm': packages, 'credentialRoutes': routes, 'tag': tag, 'githubReleasePromoted': False, 'signing': plan['signing']}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('verify', 'fetch', 'publish', 'sign-only'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--bootstrap-platform-packages', action='store_true', help='One-time first publication of absent platform package names only')
    parser.add_argument('--notary-key', type=Path)
    parser.add_argument('--notary-key-id')
    parser.add_argument('--notary-issuer')
    args = parser.parse_args()
    plan = load_plan(args.plan)
    if args.operation == 'fetch':
        fetch(plan, args.artifacts)
    elif args.operation in ('publish', 'sign-only'):
        publish(plan, args.artifacts, args.notary_key, args.notary_key_id, args.notary_issuer, bootstrap=args.bootstrap_platform_packages, sign_only=args.operation == 'sign-only')
    else:
        verify_local(plan, args.artifacts)
    print('Reviewed artifact inventory verified; signing and publication remain separate gates.')


if __name__ == '__main__':
    main()
