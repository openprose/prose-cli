"""Bind native kernel RC observations to the exact final package bytes."""
import hashlib
import re
from pathlib import PurePosixPath

CHECKS = ('built-bun', 'built-rust', 'installed-bun', 'installed-rust', 'installed-npm')
CHECK_PATHS = tuple('logs/' + name + '.json' for name in CHECKS)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def asset_name(platform, relative, artifacts):
    require(isinstance(relative, str), 'Invalid evidence path')
    path = PurePosixPath(relative)
    require(len(path.parts) == 2 and path.parts[0] in ('package', 'logs')
            and relative == '/'.join(path.parts) and '\\' not in relative
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', path.name)
            and '..' not in path.name, 'Unsafe evidence path')
    if path.parts[0] == 'package':
        return path.name if path.name in artifacts else platform + '-' + path.name
    return platform + '-logs-' + path.name


SDK_NAMES = ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt')
SDK_PROBES = tuple('logs/installed-sdk' + suffix + '-' + runner + '.json'
                   for runner in ('bun', 'rust', 'npm') for suffix in ('', '-tools'))
SDK_IMPORT_TEST = {'schema': 'openprose.sdk-packaged-self-test/1', 'openaiAgents': '0.22.2',
                   'openai': '3.13.0', 'certificates': True, 'modelCalls': 0}
SDK_TOOL_TEST = {'schema': 'openprose.sdk-packaged-tools-self-test/1', 'shellEffects': True,
                 'boundedOutput': True, 'shellCancellation': True, 'mockedPublicRetrieval': True,
                 'incompleteHttpRejected': True, 'modelCalls': 0, 'networkUsed': False}


def validate_sdk_members(manifest, members, prefix, platform):
    """Bind the frozen helper and its source/dependency receipt without executing it."""
    import json
    import publication as pub
    sdk = manifest.get('agentsSdk')
    keys = {'path', 'byteLength', 'sha256', 'receiptSha256', 'noticesSha256', 'python',
            'pyinstaller', 'version', 'discovery', 'selfTest', 'toolSelfTest', 'dependencyLockSha256'}
    require(isinstance(sdk, dict) and set(sdk) == keys, 'Production package lacks closed SDK identity')
    require(sdk['path'] == SDK_NAMES[0] and sdk['python'] == '3.10.20'
            and sdk['pyinstaller'] == '6.22.3' and sdk['version'] == '0.1.0'
            and sdk['discovery'] == 'canonical-cli-sibling'
            and sdk['selfTest'] == SDK_IMPORT_TEST and sdk['toolSelfTest'] == SDK_TOOL_TEST,
            'Packaged SDK policy or tool qualification differs')
    require(all(prefix + name in members for name in SDK_NAMES), 'Missing packaged SDK siblings')
    helper, encoded, notices = (members[prefix + name] for name in SDK_NAMES)
    require(type(sdk['byteLength']) is int and 0 < sdk['byteLength'] == len(helper)
            and hashlib.sha256(helper).hexdigest() == sdk['sha256']
            and hashlib.sha256(encoded).hexdigest() == sdk['receiptSha256']
            and hashlib.sha256(notices).hexdigest() == sdk['noticesSha256'],
            'Packaged SDK sibling bytes differ from manifest')
    require(0 < len(encoded) <= 2 * 1024 * 1024 and 0 < len(notices) <= 8 * 1024 * 1024,
            'Packaged SDK receipt/notices exceed bounds')
    receipt = json.loads(encoded, object_pairs_hook=pub.object_pairs)
    require(isinstance(receipt, dict), 'SDK build receipt must be an object')
    os_name, architecture = platform.split('-')[:2]
    architecture = {'darwin-arm64': 'arm64', 'darwin-x64': 'x86_64', 'linux-arm64-gnu': 'aarch64', 'linux-x64-gnu': 'x86_64'}.get(platform, architecture)
    require(receipt.get('schema') == 'openprose.agents-sdk-build/1'
            and receipt.get('platform') == os_name and receipt.get('architecture') == architecture
            and receipt.get('python') == sdk['python'] and receipt.get('pyinstaller') == sdk['pyinstaller']
            and receipt.get('helper') == {k: sdk[k] for k in ('path', 'byteLength', 'sha256')}
            and receipt.get('selfTest') == SDK_IMPORT_TEST and receipt.get('toolSelfTest') == SDK_TOOL_TEST
            and type(receipt.get('modelCalls')) is int and receipt['modelCalls'] == 0
            and receipt.get('publicationAuthorized') is False,
            'Packaged SDK build receipt identity differs')
    sources = receipt.get('sources')
    require(isinstance(sources, dict) and set(sources) == {
        'harnesses/agents-sdk/run.py', 'harnesses/agents-sdk/requirements-build.txt'}
        and all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for v in sources.values())
        and sources['harnesses/agents-sdk/requirements-build.txt'] == sdk['dependencyLockSha256'],
        'Packaged SDK source/lock binding differs')
    require(receipt.get('notices') == {'path': SDK_NAMES[2], 'byteLength': len(notices),
                                     'sha256': sdk['noticesSha256']}, 'SDK notices receipt differs')
    dependencies = receipt.get('dependencies')
    require(isinstance(dependencies, list) and dependencies, 'SDK dependency receipt is missing')
    seen = set()
    for package in dependencies:
        require(isinstance(package, dict) and set(package) == {'name', 'version', 'wheelSha256'}
                and isinstance(package['name'], str) and package['name'] not in seen
                and isinstance(package['version'], str) and isinstance(package['wheelSha256'], list)
                and package['wheelSha256'] and all(isinstance(h, str) and re.fullmatch(r'[0-9a-f]{64}', h) for h in package['wheelSha256']),
                'SDK dependency receipt is malformed')
        seen.add(package['name'])
    if os_name == 'linux':
        libraries = receipt.get('linuxLibraries', {})
        maximum = libraries.get('requiredGlibcMaximum') if isinstance(libraries, dict) else None
        require(isinstance(maximum, str) and re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', maximum)
                and tuple(map(int, maximum.split('.'))) + (0,) * (3 - len(maximum.split('.'))) <= (2, 34, 0), 'SDK Linux glibc floor differs')
    else:
        require(receipt.get('linuxLibraries') == 'not-applicable', 'SDK platform library receipt differs')
    return sdk


def validate_sdk_archives(manifest, archive_reader):
    """All three platform payloads must contain the identical bound helper."""
    platform = manifest['platform']
    observed = []
    for artifact in manifest['artifacts']:
        if artifact['kind'] not in ('standalone-archive', 'npm-platform'):
            continue
        members = archive_reader(artifact['path'])
        cli_names = [name for name in members if name.endswith('/prose') or name.endswith('/prose.exe')]
        require(len(cli_names) == 1, 'Expected one packaged CLI for SDK sibling binding')
        prefix = cli_names[0].rsplit('/', 1)[0] + '/'
        observed.append(validate_sdk_members(manifest, members, prefix, platform))
    require(len(observed) == 3, 'Three platform SDK payloads are required')
    return manifest['agentsSdk']


def validate_native(report, manifest, checks, final_hashes, launcher_hash):
    platform = report.get('platform')
    source, version = report.get('sourceRevision'), report.get('version')
    require(report.get('schema') == 'openprose.kernel-rc-build/1'
            and report.get('imageSource') == 'published-on-run'
            and report.get('testSeamsEnabled') is False
            and report.get('publicationAuthorized') is False
            and report.get('qualification') == 'offline-install-only'
            and type(report.get('modelCalls')) is int and report['modelCalls'] == 0
            and type(report.get('kernelFetches')) is int and report['kernelFetches'] == 0,
            'Invalid native build claims')
    labels = report.get('checks', [])
    require(len(labels) == 5 and {c.get('name') for c in labels} == set(CHECKS)
            and all(c.get('status') == 'passed' for c in labels), 'Missing native check labels')
    require(manifest.get('schema') == 'openprose.local-release-manifest/1'
            and manifest.get('mode') == 'kernel-rc' and manifest.get('platform') == platform
            and manifest.get('version') == version
            and manifest.get('source') == {'revision': source, 'verification': 'matched-product-doctor'}
            and manifest.get('imageSource') == 'published-on-run'
            and manifest.get('releaseEligible') is False
            and manifest.get('publicationAuthorized') is False
            and manifest.get('buildProfiles') == {
                name: {'profile': 'release', 'testSeamsEnabled': False} for name in ('bun', 'rust')},
            'Native manifest identity or release profile mismatch')
    artifacts = manifest.get('artifacts', [])
    require(len(artifacts) == 4
            and sorted((a.get('kind'), a.get('implementation')) for a in artifacts) ==
            [('npm-meta', 'bun'), ('npm-platform', 'bun'), ('standalone-archive', 'bun'), ('standalone-archive', 'rust')]
            and all(a.get('platform') == (None if a['kind'] == 'npm-meta' else platform) for a in artifacts),
            'Native package artifact coverage/platform mismatch')
    require(set(checks) == set(CHECKS), 'Five retained structured checks are required')
    for name, check in checks.items():
        runner = 'rust' if name.endswith('-rust') else 'bun'
        expected_hash = launcher_hash if name == 'installed-npm' else final_hashes[(runner, platform)]
        require(check.get('schema') == 'openprose.published-release-check/1'
                and check.get('status') == 'passed-offline-release-check'
                and check.get('runner') == runner and check.get('commit') == source
                and check.get('version') == version and check.get('binarySha256') == expected_hash
                and check.get('imageSource') == 'published-on-run'
                and check.get('testSeamsEnabled') is False
                and type(check.get('modelCalls')) is int and check['modelCalls'] == 0
                and type(check.get('networkCalls')) is int and check['networkCalls'] == 0,
                'Structured native check does not bind exact release bytes: ' + name)
        if name == 'installed-npm':
            require(re.fullmatch(r'[0-9a-f]{64}', check.get('nodeInterpreterSha256', '')),
                    'Installed npm check lacks Node interpreter identity')


def verify_platform_evidence(plan, root, platform, report_name, binary_hashes):
    # Runtime import keeps the validator usable from publication.py's CLI without
    # a module initialization cycle. These helpers never execute package content.
    import publication as pub
    inventory = {a['name']: a for a in plan['artifacts']}
    require(report_name in inventory and inventory[report_name]['kind'] == 'evidence', 'Native report is not in reviewed inventory')
    report_path = root / report_name
    require(report_path.stat().st_size == inventory[report_name]['size'] and pub.digest(report_path) == inventory[report_name]['sha256'], 'Native report bytes differ from reviewed inventory')
    report = pub.read_json(report_path)
    require(report.get('platform') == platform and report.get('sourceRevision') == plan['source']
            and report.get('version') == plan['version'], 'Native report is for a different candidate')
    manifest_name = platform + '-release-manifest.json'
    require(manifest_name in inventory, 'Native manifest is not retained')
    manifest = pub.read_json(root / manifest_name)
    preflight = pub.read_json(root / plan['preflight'])
    require(manifest.get('embeddedDiagnosticImage') == preflight.get('embeddedDiagnosticImage') and manifest.get('kernelPolicy') == preflight.get('kernelPolicy'), 'Native manifest kernel policy differs from qualification')
    artifact_names = {a['path'] for a in manifest.get('artifacts', [])}
    evidence = report.get('evidence', {})
    require(isinstance(evidence, dict) and set(CHECK_PATHS + SDK_PROBES).union({'package/release-manifest.json'}).issubset(evidence), 'Required structured evidence is missing')
    seen = set()
    for relative, record in evidence.items():
        name = asset_name(platform, relative, artifact_names)
        require(name not in seen and name in inventory, 'Missing or colliding retained evidence')
        seen.add(name)
        item = inventory[name]
        require(set(record) == {'sha256', 'byteLength'} and record['sha256'] == item['sha256']
                and record['byteLength'] == item['size'], 'Retained evidence differs from build report')
        path = root / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == record['byteLength']
                and pub.digest(path) == record['sha256'], 'Retained evidence bytes changed')
    for artifact in manifest.get('artifacts', []):
        name = artifact['path']
        require('package/' + name in evidence and name in inventory, 'Package artifact lacks native evidence')
        item = inventory[name]
        kind = {'standalone-archive': 'standalone', 'npm-meta': 'npm', 'npm-platform': 'npm'}.get(artifact['kind'])
        require(item['sha256'] == artifact['sha256'] and item['size'] == artifact['byteLength']
                and item['kind'] == kind and item['implementation'] == artifact['implementation']
                and item['platform'] == (artifact['platform'] or 'all'), 'Native artifact differs from final reviewed inventory')
    require(manifest.get('platform') == platform, 'Native manifest identity mismatch')
    validate_sdk_archives(manifest, lambda name: pub.archive_members(root / name))
    for relative in SDK_PROBES:
        probe = pub.read_json(root / asset_name(platform, relative, artifact_names))
        require(probe == (SDK_TOOL_TEST if 'sdk-tools-' in relative else SDK_IMPORT_TEST),
                'Installed SDK probe differs: ' + relative)
    meta = next((a for a in manifest['artifacts'] if a['kind'] == 'npm-meta'), None)
    require(meta is not None, 'Missing root npm package')
    members = pub.archive_members(root / meta['path'])
    require('package/bin/prose.js' in members, 'Missing npm launcher')
    launcher_hash = hashlib.sha256(members['package/bin/prose.js']).hexdigest()
    checks = {name: pub.read_json(root / asset_name(platform, 'logs/' + name + '.json', artifact_names)) for name in CHECKS}
    validate_native(report, manifest, checks, binary_hashes, launcher_hash)
    return manifest


LIVE_ROLES = {'observation', 'native', 'runner', 'readiness', 'selection', 'kernel', 'descriptor', 'inventory'}
LIVE_MAX_BYTES = 64 * 1024 * 1024


def live_asset_name(runner, role, record):
    require(runner in ('bun', 'rust') and role in LIVE_ROLES, 'Invalid live evidence role')
    require(isinstance(record, dict) and set(record) == {'path', 'sha256', 'byteLength'}, 'Invalid live evidence record')
    path = record['path']
    require(isinstance(path, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', path)
            and '..' not in path, 'Live evidence paths must be flat filenames')
    require(re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
            and type(record['byteLength']) is int and 0 < record['byteLength'] <= LIVE_MAX_BYTES,
            'Invalid live evidence digest or size')
    return 'live-' + runner + '-' + role + '-' + path


def validate_live_smoke(live, source, version, binary_hashes, evidence_paths):
    """Verify retained runner terminal, observation and immutable kernel bytes.

    This verifies the report's custody and consistency, not provider semantics.
    Native captures remain opaque evidence whose exact bytes are retained.
    """
    import json
    from urllib.parse import urlsplit
    import publication as pub
    require(live.get('schema') == 'openprose.kernel-rc-live-smoke/1'
            and live.get('sourceSha') == source and live.get('version') == version
            and live.get('status') == 'pass' and live.get('platform') == 'darwin-arm64'
            and set(live.get('runners', {})) == {'bun', 'rust'}, 'Invalid exact-source live smoke report')
    for runner, attempt in live['runners'].items():
        require(attempt.get('accepted') is True and attempt.get('helloExact') is True
                and attempt.get('binarySha256') == binary_hashes[(runner, 'darwin-arm64')],
                'Live smoke binary differs from release bytes')
        records = attempt.get('evidence', {})
        require(set(records) == LIVE_ROLES, 'Complete retained live evidence is required')
        paths = {}
        for role, record in records.items():
            live_asset_name(runner, role, record)
            path = evidence_paths[(runner, role)]
            require(path.is_file() and not path.is_symlink()
                    and path.stat().st_size == record['byteLength'] and pub.digest(path) == record['sha256'],
                    'Live evidence bytes changed: ' + role)
            paths[role] = path
        observation = pub.read_json(paths['observation'])
        require(observation.get('accepted') is True and observation.get('hello_exact') is True
                and observation.get('exit_code') == 0 and observation.get('outer_watchdog_triggered') is False
                and observation.get('validation_failures') == [] and observation.get('changed_original_files') == []
                and observation.get('new_files') == ['hello.txt'], 'Live observation did not accept an unchanged exact Hello World run')
        after = observation.get('after', {}).get('hello.txt', {})
        require(after.get('type') == 'file' and after.get('sha256') == hashlib.sha256(b'Hello World\n').hexdigest()
                and after.get('links') == 1, 'Live observation does not bind the exact Hello World file')
        raw = paths['runner'].read_text()
        require(len(raw.splitlines()) <= 100000, 'Runner event count exceeds limit')
        events = [json.loads(line, object_pairs_hook=pub.object_pairs) for line in raw.splitlines()]
        require(events and all(e.get('schema') == 'openprose.normalized-event/1' for e in events), 'Invalid retained runner events')
        terminal_events = [e for e in events if e.get('type') in ('runner.completed', 'runner.failed', 'runner.cancelled')]
        require(len(terminal_events) == 1 and terminal_events[0] is events[-1]
                and terminal_events[0].get('type') == 'runner.completed', 'Runner did not have exactly one final completion')
        result = terminal_events[0].get('payload', {}).get('result', {})
        require(result.get('schema') == 'openprose.runner-result/1'
                and result.get('runner') == {'name': runner, 'version': version, 'commit': source}
                and result.get('runnerExitCode') == 0
                and result.get('terminal', {}).get('classification') == 'success'
                and result['terminal'].get('transportCompleted') is True
                and result['terminal'].get('terminalEventObserved') is True,
                'Raw runner terminal does not accept this release candidate')
        kernel = attempt.get('kernel', {})
        require(set(kernel) == {'version', 'sha256', 'entrypoint', 'resolvedUrl', 'sourceRevision', 'kernelSha256'}
                and kernel['entrypoint'] == 'https://pkg.prose.md/kernel.md', 'Invalid resolved kernel identity')
        url = urlsplit(kernel['resolvedUrl'])
        match = re.fullmatch(r'/releases/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/core/README\.md', url.path)
        require(url.scheme == 'https' and url.netloc == 'pkg.prose.md' and not url.query and not url.fragment
                and match is not None, 'Kernel must resolve to the immutable package endpoint')
        release = match.group(1)
        descriptor = pub.read_json(paths['descriptor'])
        require(descriptor.get('identity') == 'openprose/core' and descriptor.get('release') == release
                and descriptor.get('source', {}).get('commit') == kernel['sourceRevision']
                and re.fullmatch(r'[0-9a-f]{40}', kernel['sourceRevision'])
                and descriptor.get('exports', {}).get('entry') == 'README.md'
                and descriptor.get('inventory') == 'releases/' + release + '/core/inventory.json'
                and descriptor.get('inventory_sha256') == pub.digest(paths['inventory']),
                'Kernel descriptor does not bind the resolved release')
        inventory = pub.read_json(paths['inventory'])
        content = paths['kernel'].read_bytes()
        kernel_hash = hashlib.sha256(content).hexdigest()
        aggregate = hashlib.sha256(b'payload/kernel.md\0' + str(len(content)).encode() + b'\0' + content + b'\0').hexdigest()
        require(inventory.get('README.md', {}).get('mode') == '100644'
                and inventory['README.md'].get('sha256') == kernel_hash == kernel['kernelSha256']
                and kernel['sha256'] == aggregate and kernel['version'] == 'kernel-' + release,
                'Retained kernel bytes do not match the resolved image')
        require(result.get('languageImage') == {'formatVersion': 'openprose.skill-runtime-image/1', 'version': kernel['version'], 'sha256': aggregate}
                and result.get('digests', {}).get('deliveredImageSha256') == kernel_hash,
                'Runner did not execute the retained published kernel')


def verify_live_evidence(plan, root, binary_hashes):
    """Repeat live custody verification at publication without making API calls."""
    import publication as pub
    live = pub.read_json(root / plan['preflight'])['liveSmoke']
    inventory = {a['name']: a for a in plan['artifacts']}
    paths = {}
    for runner, attempt in live.get('runners', {}).items():
        for role, record in attempt.get('evidence', {}).items():
            name = live_asset_name(runner, role, record)
            require(name in inventory and inventory[name]['kind'] == 'evidence'
                    and inventory[name]['sha256'] == record['sha256'] and inventory[name]['size'] == record['byteLength'],
                    'Live evidence is not bound to the reviewed plan')
            paths[(runner, role)] = root / name
    validate_live_smoke(live, plan['source'], plan['version'], binary_hashes, paths)
