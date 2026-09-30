import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import assemble_kernel_rc as a
import package_local as package
import publication as p
import kernel_rc_evidence as custody


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roots = []
        self.source = 'a' * 40
        self.version = '0.15.0-rc.1'
        image, manifest_sha = package.read_image_manifest(package.CLI / 'shared/image/echo-v0/manifest.json')
        self.image = package.image_identity(image, manifest_sha)
        for platform in p.PLATFORMS:
            root = self.root / platform
            output = root / 'package'
            output.mkdir(parents=True)
            self.roots.append(root)
            artifacts = []
            for implementation in ('bun', 'rust'):
                name = implementation + '-' + platform + '.tgz'
                data = (implementation + platform).encode()
                with tarfile.open(output / name, 'w:gz') as archive:
                    member = tarfile.TarInfo('root/prose')
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                artifacts.append(self.record(output / name, implementation, 'standalone-archive', platform))
            runtime = {'minimumGlibc': '2.34', 'requiredGlibcMaximum': {'rust': '2.34', 'bun': '2.34'}, 'executionEvidence': 'ubuntu-22.04-only'} if platform.startswith('linux-') else 'not-applicable'
            meta, native = package.npm_packages(output, ('bun' + platform).encode(), self.version, platform, 0, self.source, self.image, runtime, 'kernel-rc', package.HELLO_EXAMPLE.read_bytes(), publication_platforms='posix-four')
            artifacts += [self.record(meta, 'bun', 'npm-meta', None), self.record(native, 'bun', 'npm-platform', platform)]
            manifest = {'schema': 'openprose.local-release-manifest/1', 'mode': 'kernel-rc', 'platform': platform, 'version': self.version, 'source': {'revision': self.source, 'verification': 'matched-product-doctor'}, 'releaseEligible': False, 'publicationAuthorized': False, 'buildProfiles': {r: {'profile': 'release', 'testSeamsEnabled': False} for r in ('bun', 'rust')}, 'imageSource': 'published-on-run', 'embeddedDiagnosticImage': {k:v for k,v in self.image.items() if k != 'releaseEligible'}, 'kernelPolicy': package.PUBLISHED_KERNEL_POLICY, 'artifacts': artifacts}
            (output / 'release-manifest.json').write_text(json.dumps(manifest))
            logs = root / 'logs'; logs.mkdir()
            with tarfile.open(meta) as archive:
                launcher_hash = hashlib.sha256(archive.extractfile('package/bin/prose.js').read()).hexdigest()
            for name in custody.CHECKS:
                runner = 'rust' if name.endswith('-rust') else 'bun'
                check = {'schema': 'openprose.published-release-check/1', 'status': 'passed-offline-release-check', 'runner': runner, 'commit': self.source, 'version': self.version, 'binarySha256': launcher_hash if name == 'installed-npm' else hashlib.sha256((runner+platform).encode()).hexdigest(), 'imageSource': 'published-on-run', 'testSeamsEnabled': False, 'modelCalls': 0, 'networkCalls': 0}
                if name == 'installed-npm':
                    check['nodeInterpreterSha256'] = 'd'*64
                (logs / (name + '.json')).write_text(json.dumps(check))
            evidence = {str(f.relative_to(root)): {'sha256': p.digest(f), 'byteLength': f.stat().st_size} for directory in (output, logs) for f in directory.iterdir()}
            report = {'schema': 'openprose.kernel-rc-build/1', 'platform': platform, 'version': self.version, 'sourceRevision': self.source, 'imageSource': 'published-on-run', 'testSeamsEnabled': False, 'qualification': 'offline-install-only', 'publicationAuthorized': False, 'modelCalls': 0, 'kernelFetches': 0, 'checks': [{'name': n, 'status': 'passed'} for n in ('built-bun','built-rust','installed-bun','installed-rust','installed-npm')], 'evidence': evidence}
            (root / 'build-report.json').write_text(json.dumps(report))
        self.evidence = 'https://github.com/openprose/example-evidence/tree/' + 'b'*40 + '/test'

    def record(self, path, implementation, kind, platform):
        return {'path': path.name, 'sha256': p.digest(path), 'byteLength': path.stat().st_size, 'implementation': implementation, 'kind': kind, 'platform': platform}

    def live_report(self):
        release = 'fixture-rc.1'
        kernel_bytes = b'# Kernel fixture\n'
        kernel_sha = hashlib.sha256(kernel_bytes).hexdigest()
        aggregate = hashlib.sha256(b'payload/kernel.md\0' + str(len(kernel_bytes)).encode() + b'\0' + kernel_bytes + b'\0').hexdigest()
        inventory = json.dumps({'README.md': {'mode': '100644', 'sha256': kernel_sha}}).encode()
        descriptor = {'identity': 'openprose/core', 'release': release, 'source': {'commit': 'e'*40}, 'exports': {'entry': 'README.md'}, 'inventory': 'releases/'+release+'/core/inventory.json', 'inventory_sha256': hashlib.sha256(inventory).hexdigest()}
        live = {'schema': 'openprose.kernel-rc-live-smoke/1', 'sourceSha': self.source, 'version': self.version, 'status': 'pass', 'platform': 'darwin-arm64', 'runners': {}}
        for runner in ('bun', 'rust'):
            observation = {'exit_code': 0, 'outer_watchdog_triggered': False, 'accepted': True, 'hello_exact': True, 'changed_original_files': [], 'new_files': ['hello.txt'], 'validation_failures': [], 'after': {'hello.txt': {'type': 'file', 'links': 1, 'sha256': hashlib.sha256(b'Hello World\n').hexdigest()}}}
            kernel = {'version': 'kernel-'+release, 'sha256': aggregate, 'entrypoint': 'https://pkg.prose.md/kernel.md', 'resolvedUrl': 'https://pkg.prose.md/releases/'+release+'/core/README.md', 'sourceRevision': 'e'*40, 'kernelSha256': kernel_sha}
            result = {'schema': 'openprose.runner-result/1', 'runner': {'name': runner, 'commit': self.source, 'version': self.version}, 'runnerExitCode': 0, 'terminal': {'classification': 'success', 'transportCompleted': True, 'terminalEventObserved': True}, 'languageImage': {'formatVersion': 'openprose.skill-runtime-image/1', 'version': kernel['version'], 'sha256': aggregate}, 'digests': {'deliveredImageSha256': kernel_sha}}
            values = {'observation': json.dumps(observation).encode(), 'native': b'{"fixture":true}\n', 'runner': json.dumps({'schema': 'openprose.normalized-event/1', 'type': 'runner.completed', 'payload': {'result': result}}).encode()+b'\n', 'readiness': b'{}', 'selection': b'{}', 'kernel': kernel_bytes, 'inventory': inventory, 'descriptor': json.dumps(descriptor).encode()}
            records = {}
            for role, data in values.items():
                name = runner+'-'+role+'.json'
                path = self.root/name
                path.write_bytes(data)
                records[role] = {'path': name, 'sha256': p.digest(path), 'byteLength': len(data)}
            live['runners'][runner] = {'accepted': True, 'helloExact': True, 'binarySha256': hashlib.sha256((runner+'darwin-arm64').encode()).hexdigest(), 'kernel': kernel, 'evidence': records}
        return live

    def test_generated_packages_assemble_without_claiming_live_qualification(self):
        plan = a.assemble(self.roots, self.root/'assembly', self.evidence)
        self.assertEqual(plan['qualification']['status'], 'development')
        self.assertEqual(len([x for x in plan['artifacts'] if x['kind'] == 'npm']), 5)
        with self.assertRaisesRegex(ValueError, 'Kernel qualification'):
            p.load_plan(self.root/'assembly/publication-plan.json')

    def test_aggregate_checksums_bind_only_all_install_archives(self):
        output = self.root / 'checksums'
        plan = a.assemble(self.roots, output, self.evidence)
        archives = sorted((item for item in plan['artifacts'] if item['kind'] in ('standalone', 'npm')), key=lambda item: item['name'])
        expected = ''.join(p.digest(output / item['name']) + '  ' + item['name'] + '\n' for item in archives).encode('ascii')
        self.assertEqual(len(archives), 13)
        self.assertEqual((output / 'SHA256SUMS').read_bytes(), expected)
        record = next(item for item in plan['artifacts'] if item['name'] == 'SHA256SUMS')
        self.assertEqual(record, {'name': 'SHA256SUMS', 'sha256': hashlib.sha256(expected).hexdigest(), 'size': len(expected), 'kind': 'evidence', 'platform': 'all', 'implementation': 'shared'})
        reversed_output = self.root / 'reversed-checksums'
        a.assemble(list(reversed(self.roots)), reversed_output, self.evidence)
        self.assertEqual((reversed_output / 'SHA256SUMS').read_bytes(), expected)

    def test_damaged_generated_package_refused(self):
        next((self.roots[0]/'package').glob('*.tgz')).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'evidence bytes changed'):
            a.assemble(self.roots, self.root/'assembly', self.evidence)
        self.assertFalse((self.root/'assembly').exists())

    def test_duplicate_platform_refused(self):
        with self.assertRaisesRegex(ValueError, 'duplicate native'):
            a.assemble([self.roots[0]]*4, self.root/'assembly', self.evidence)

    def test_live_evidence_must_bind_exact_both_binary_bytes(self):
        live = self.live_report()
        path = self.root/'live.json'
        path.write_text(json.dumps(live))
        plan = a.assemble(self.roots, self.root/'assembly', self.evidence, path)
        self.assertEqual(plan['qualification']['status'], 'kernel-smoke-qualified')
        _, hashes = p.verify_local(plan, self.root/'assembly')
        for platform in p.PLATFORMS:
            custody.verify_platform_evidence(plan, self.root/'assembly', platform, platform+'-build-report.json', hashes)
        live['runners']['rust']['binarySha256'] = '0'*64
        path.write_text(json.dumps(live))
        with self.assertRaisesRegex(ValueError, 'differs from release bytes'):
            a.assemble(self.roots, self.root/'changed-live', self.evidence, path)


if __name__ == '__main__':
    unittest.main()
