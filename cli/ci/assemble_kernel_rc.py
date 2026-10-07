#!/usr/bin/env python3
"""Assemble verified native build outputs; retain qualification as a separate input."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import publication as pub
import kernel_rc_evidence as custody


def assemble(roots, output, evidence, live_smoke=None):
    pub.require(not output.exists(), 'Use a fresh assembly directory')
    pub.require(len(roots) == 4, 'Exactly four native build directories required')
    inventory = {}
    reports = {}
    manifests = {}
    all_binary_hashes = {}
    version = source = diagnostic = policy = None
    for root in roots:
        report = pub.read_json(root / 'build-report.json')
        platform = report.get('platform')
        pub.require(report.get('schema') == 'openprose.kernel-rc-build/1' and platform in pub.PLATFORMS and platform not in reports, 'Invalid or duplicate native build report')
        pub.require(report.get('imageSource') == 'published-on-run' and report.get('testSeamsEnabled') is False and report.get('qualification') == 'offline-install-only' and report.get('publicationAuthorized') is False and report.get('modelCalls') == 0, 'Unexpected build qualification claims')
        wanted = {'built-bun', 'built-rust', 'installed-bun', 'installed-rust', 'installed-npm'}
        checks = report.get('checks', [])
        pub.require(len(checks) == 5 and {c.get('name') for c in checks} == wanted and all(c.get('status') == 'passed' for c in checks), 'Native build/install checks failed or missing')
        for relative, record in report['evidence'].items():
            path = Path(relative)
            pub.require(not path.is_absolute() and '..' not in path.parts and '\\' not in relative, 'Unsafe evidence path')
            item = root / path
            pub.require(item.is_file() and not item.is_symlink() and item.stat().st_size == record['byteLength'] and pub.digest(item) == record['sha256'], 'Build evidence bytes changed')
        package = root / 'package'
        pub.require('package/release-manifest.json' in report['evidence'], 'Package manifest not bound by build report')
        manifest = pub.read_json(package / 'release-manifest.json')
        pub.require(manifest['mode'] == 'kernel-rc' and manifest['platform'] == platform and manifest['imageSource'] == 'published-on-run', 'Wrong package mode/platform')
        if version is None:
            version, source = manifest['version'], manifest['source']['revision']
            diagnostic, policy = manifest['embeddedDiagnosticImage'], manifest['kernelPolicy']
        pub.require(manifest['version'] == version == report['version'] and manifest['source']['revision'] == source == report['sourceRevision'], 'Mixed candidate versions or source commits')
        pub.require(manifest['embeddedDiagnosticImage'] == diagnostic and manifest['kernelPolicy'] == policy, 'Mixed diagnostic image or kernel policy')
        for item in manifest['artifacts']:
            name = item['path']
            pub.require(pub.safe_name(name), 'Unsafe package artifact name')
            actual = package / name
            pub.require('package/' + name in report['evidence'] and actual.is_file() and not actual.is_symlink() and actual.stat().st_size == item['byteLength'] and pub.digest(actual) == item['sha256'], 'Packaged artifact differs from evidence')
            kind = {'standalone-archive': 'standalone', 'npm-meta': 'npm', 'npm-platform': 'npm'}[item['kind']]
            record = {'name': name, 'sha256': item['sha256'], 'size': item['byteLength'], 'kind': kind, 'platform': item['platform'] or 'all', 'implementation': item['implementation']}
            if name in inventory:
                pub.require(item['kind'] == 'npm-meta' and inventory[name][1] == record, 'Artifact collision or inconsistent root npm package')
            else:
                inventory[name] = (actual, record)
        artifact_names = {a['path'] for a in manifest['artifacts']}
        pub.require(set(custody.CHECK_PATHS).issubset(report['evidence']), 'Required structured evidence is missing')
        custody.validate_sdk_archives(manifest, lambda name: pub.read_sdk_archive(package / name, manifest))
        for relative in custody.SDK_PROBES:
            pub.require(relative in report['evidence'], 'Missing installed SDK evidence')
            pub.require(pub.read_json(root / relative) == (custody.SDK_TOOL_TEST if 'sdk-tools-' in relative else custody.SDK_IMPORT_TEST), 'Installed SDK probe differs')
        native_hashes = {}
        launcher_hash = None
        for artifact in manifest['artifacts']:
            if artifact['kind'] == 'npm-meta':
                members = pub.archive_members(package / artifact['path'])
            else:
                table = pub.read_sdk_archive(package / artifact['path'], manifest)
                custody.validate_sdk_archive_table(manifest, table)
                members = {name: data for name, (data, _) in table['files'].items()}
            if artifact['kind'] == 'standalone-archive':
                binaries = [members[custody.sdk_archive_prefix(table) + 'prose']]
                pub.require(len(binaries) == 1, 'Expected one standalone binary')
                native_hashes[(artifact['implementation'], platform)] = hashlib.sha256(binaries[0]).hexdigest()
            elif artifact['kind'] == 'npm-meta':
                pub.require('package/bin/prose.js' in members, 'Missing npm launcher')
                launcher_hash = hashlib.sha256(members['package/bin/prose.js']).hexdigest()
        checks = {name: pub.read_json(root / ('logs/' + name + '.json')) for name in custody.CHECKS}
        custody.validate_native(report, manifest, checks, native_hashes, launcher_hash)
        pub.require(custody.SDK_PAYLOAD_EVIDENCE in report['evidence'], 'Missing complete installed SDK payload evidence')
        custody.validate_installed_sdk_payloads(pub.read_json(root / custody.SDK_PAYLOAD_EVIDENCE), manifest, table)
        custody.validate_sdk_producer_evidence(manifest, table, report['evidence'], lambda relative: (root / relative).read_bytes())
        all_binary_hashes.update(native_hashes)
        # Retain every report-bound input, including the five structured native
        # observations. Publication independently repeats these bindings.
        for relative, bound in report['evidence'].items():
            actual = root / relative
            name = custody.asset_name(platform, relative, artifact_names)
            if relative.startswith('package/') and actual.name in artifact_names:
                continue
            pub.require(pub.safe_name(name) and name not in inventory, 'Evidence collision')
            inventory[name] = (actual, {'name': name, 'sha256': bound['sha256'], 'size': bound['byteLength'], 'kind': 'evidence', 'platform': platform, 'implementation': 'shared'})
        name = platform + '-build-report.json'
        actual = root / 'build-report.json'
        inventory[name] = (actual, {'name': name, 'sha256': pub.digest(actual), 'size': actual.stat().st_size, 'kind': 'evidence', 'platform': platform, 'implementation': 'shared'})
        reports[platform] = {'status': 'pass', 'report': name}
        manifests[platform] = manifest
    output.mkdir(parents=True)
    for name, (path, _) in inventory.items():
        shutil.copyfile(path, output / name)
    # The shipped installation guide names this aggregate checksum file. Keep
    # archive bytes unchanged and bind the checksum file as release evidence.
    checksum_path = output / 'SHA256SUMS'
    archives = sorted((record for _, record in inventory.values()
                       if record['kind'] in ('standalone', 'npm')), key=lambda item: item['name'])
    pub.require(len(archives) == 13 and checksum_path.name not in inventory, 'Expected thirteen install archives')
    checksum_path.write_bytes(''.join(item['sha256'] + '  ' + item['name'] + '\n' for item in archives).encode('ascii'))
    inventory[checksum_path.name] = (checksum_path, {'name': checksum_path.name, 'sha256': pub.digest(checksum_path), 'size': checksum_path.stat().st_size, 'kind': 'evidence', 'platform': 'all', 'implementation': 'shared'})
    live = pub.read_json(live_smoke) if live_smoke else {'status': 'not-run'}
    preflight = {'schema': 'openprose.kernel-rc-release-evidence/1', 'version': version, 'sourceSha': source, 'status': 'pass' if live_smoke else 'incomplete', 'failures': [] if live_smoke else ['Exact-source live smoke remains required'], 'imageSource': 'published-on-run', 'embeddedDiagnosticImage': diagnostic, 'kernelPolicy': policy, 'platforms': reports, 'liveSmoke': live}
    if live_smoke:
        paths = {}
        for runner, attempt in live.get('runners', {}).items():
            for role, record in attempt.get('evidence', {}).items():
                custody.live_asset_name(runner, role, record)
                paths[(runner, role)] = live_smoke.parent / record['path']
        custody.validate_live_smoke(live, source, version, all_binary_hashes, paths)
        for runner, attempt in live['runners'].items():
            for role, record in attempt['evidence'].items():
                name = custody.live_asset_name(runner, role, record)
                pub.require(name not in inventory and pub.safe_name(name), 'Live evidence filename collision')
                actual = paths[(runner, role)]
                shutil.copyfile(actual, output / name)
                inventory[name] = (actual, {'name': name, 'sha256': record['sha256'], 'size': record['byteLength'], 'kind': 'evidence', 'platform': 'darwin-arm64', 'implementation': runner})
    preflight_path = output / 'kernel-rc-release-evidence.json'
    preflight_path.write_text(json.dumps(preflight, indent=2, sort_keys=True) + '\n')
    artifacts = [record for _, record in inventory.values()]
    artifacts.append({'name': preflight_path.name, 'sha256': pub.digest(preflight_path), 'size': preflight_path.stat().st_size, 'kind': 'evidence', 'platform': 'all', 'implementation': 'shared'})
    plan = {'schema': 'openprose.cli-publication/2', 'version': version, 'source': source, 'qualification': {'status': 'kernel-smoke-qualified' if live_smoke else 'development', 'evidence': evidence}, 'artifacts': artifacts, 'preflight': preflight_path.name, 'macos': {}, 'npmProvenance': True, 'signing': 'unsigned-rc'}
    plan_path = output / 'publication-plan.json'
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + '\n')
    if live_smoke:
        pub.load_plan(plan_path)
        pub.verify_local(plan, output)
        custody.verify_live_evidence(plan, output, all_binary_hashes)
    return plan


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots', nargs=4, type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--live-smoke', type=Path)
    args = parser.parse_args()
    result = assemble(args.roots, args.out, args.evidence, args.live_smoke)
    print('Assembly retained; qualification=' + result['qualification']['status'] + '; no publication performed')
