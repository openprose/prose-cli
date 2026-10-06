"""Mutation tests for source/check/package custody, using generated npm bytes."""
import hashlib
import json
import unittest
import kernel_rc_evidence as custody
import publication as pub
import test_assemble_kernel_rc as assembly_tests


class EvidenceTests(unittest.TestCase):
    setUp = assembly_tests.AssemblyTests.setUp
    record = assembly_tests.AssemblyTests.record

    def assembled(self):
        import assemble_kernel_rc
        output = self.root / 'assembly'
        plan = assemble_kernel_rc.assemble(self.roots, output, self.evidence)
        hashes = {(runner, platform): hashlib.sha256((runner + platform).encode()).hexdigest()
                  for runner in ('bun', 'rust') for platform in pub.PLATFORMS}
        return plan, output, hashes

    def verify(self, plan, output, hashes):
        return custody.verify_platform_evidence(plan, output, 'darwin-arm64', 'darwin-arm64-build-report.json', hashes)

    def update_bound(self, plan, output, relative, value):
        report_path = output / 'darwin-arm64-build-report.json'
        report = pub.read_json(report_path)
        manifest = pub.read_json(output / 'darwin-arm64-release-manifest.json')
        name = custody.asset_name('darwin-arm64', relative, {a['path'] for a in manifest['artifacts']})
        path = output / name
        path.write_text(json.dumps(value))
        report['evidence'][relative] = {'sha256': pub.digest(path), 'byteLength': path.stat().st_size}
        report_path.write_text(json.dumps(report))
        for name, changed in ((name, path), (report_path.name, report_path)):
            record = next(item for item in plan['artifacts'] if item['name'] == name)
            record.update(sha256=pub.digest(changed), size=changed.stat().st_size)

    def test_all_structured_logs_retained_and_verified(self):
        plan, output, hashes = self.assembled()
        self.verify(plan, output, hashes)
        names = {a['name'] for a in plan['artifacts']}
        for platform in pub.PLATFORMS:
            for check in custody.CHECKS:
                self.assertIn(platform + '-logs-' + check + '.json', names)

    def test_production_manifest_cannot_omit_or_rehash_sdk_identity(self):
        for mutation in ('omit', 'digest'):
            with self.subTest(mutation=mutation):
                plan, output, hashes = self.assembled()
                manifest = pub.read_json(output / 'darwin-arm64-release-manifest.json')
                if mutation == 'omit':
                    manifest.pop('agentsSdk')
                else:
                    manifest['agentsSdk']['sha256'] = '0'*64
                self.update_bound(plan, output, 'package/release-manifest.json', manifest)
                with self.assertRaisesRegex(ValueError, 'SDK'):
                    self.verify(plan, output, hashes)
                import shutil
                shutil.rmtree(output)

    def test_rehashed_installed_sdk_probe_cannot_claim_skipped_tools(self):
        plan, output, hashes = self.assembled()
        self.update_bound(plan, output, 'logs/installed-sdk-tools-bun.json', {})
        with self.assertRaisesRegex(ValueError, 'SDK probe'):
            self.verify(plan, output, hashes)

    def test_rehashed_passing_log_cannot_describe_other_binary(self):
        plan, output, hashes = self.assembled()
        check = pub.read_json(output / 'darwin-arm64-logs-installed-bun.json')
        check['binarySha256'] = '0'*64
        self.update_bound(plan, output, 'logs/installed-bun.json', check)
        with self.assertRaisesRegex(ValueError, 'exact release bytes'):
            self.verify(plan, output, hashes)

    def test_rehashed_checks_reject_fixed_startup_wrong_source_and_test_seams(self):
        plan, output, hashes = self.assembled()
        original = pub.read_json(output / 'darwin-arm64-logs-built-rust.json')
        for mutation in ({'imageSource': 'embedded'}, {'commit': 'f'*40}, {'testSeamsEnabled': True}):
            with self.subTest(mutation=mutation):
                self.update_bound(plan, output, 'logs/built-rust.json', dict(original, **mutation))
                with self.assertRaisesRegex(ValueError, 'exact release bytes'):
                    self.verify(plan, output, hashes)

    def test_missing_retained_evidence_cannot_pass(self):
        plan, output, hashes = self.assembled()
        plan['artifacts'] = [a for a in plan['artifacts'] if a['name'] != 'darwin-arm64-logs-installed-npm.json']
        with self.assertRaisesRegex(ValueError, 'Missing or colliding'):
            self.verify(plan, output, hashes)

    def test_rehashed_manifest_cannot_change_native_platform(self):
        plan, output, hashes = self.assembled()
        manifest = pub.read_json(output / 'darwin-arm64-release-manifest.json')
        manifest['platform'] = 'linux-x64-gnu'
        self.update_bound(plan, output, 'package/release-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'manifest identity'):
            self.verify(plan, output, hashes)

    def test_rehashed_manifest_cannot_authorize_or_enable_test_seams(self):
        plan, output, hashes = self.assembled()
        original = pub.read_json(output / 'darwin-arm64-release-manifest.json')
        for mutation in ({'publicationAuthorized': True}, {'releaseEligible': True},
                         {'buildProfiles': {'bun': {'profile': 'release', 'testSeamsEnabled': True}, 'rust': {'profile': 'release', 'testSeamsEnabled': False}}}):
            with self.subTest(mutation=mutation):
                self.update_bound(plan, output, 'package/release-manifest.json', dict(original, **mutation))
                with self.assertRaisesRegex(ValueError, 'manifest identity'):
                    self.verify(plan, output, hashes)

    def test_evidence_paths_are_closed(self):
        for name in ('../key', '/tmp/key', 'logs/../key', 'logs/nested/key', 'package/./file', 'logs/a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                custody.asset_name('darwin-arm64', name, set())


class LiveEvidenceTests(unittest.TestCase):
    setUp = assembly_tests.AssemblyTests.setUp
    record = assembly_tests.AssemblyTests.record
    live_report = assembly_tests.AssemblyTests.live_report

    def live(self):
        live = self.live_report()
        paths = {(runner, role): self.root/record['path'] for runner, attempt in live['runners'].items() for role, record in attempt['evidence'].items()}
        hashes = {(runner, 'darwin-arm64'): hashlib.sha256((runner+'darwin-arm64').encode()).hexdigest() for runner in ('bun', 'rust')}
        return live, paths, hashes

    def verify(self, live, paths, hashes):
        custody.validate_live_smoke(live, self.source, self.version, hashes, paths)

    def rewrite(self, live, paths, role, value):
        path = paths[('bun', role)]
        path.write_text(json.dumps(value))
        live['runners']['bun']['evidence'][role].update(sha256=pub.digest(path), byteLength=path.stat().st_size)

    def test_retained_kernel_terminal_and_observation_agree(self):
        live, paths, hashes = self.live()
        self.verify(live, paths, hashes)

    def test_rehashed_observation_does_not_override_failure(self):
        live, paths, hashes = self.live()
        observation = pub.read_json(paths[('bun', 'observation')])
        observation['accepted'] = False
        self.rewrite(live, paths, 'observation', observation)
        with self.assertRaisesRegex(ValueError, 'observation did not accept'):
            self.verify(live, paths, hashes)

    def test_rehashed_hello_claim_requires_exact_file_hash(self):
        live, paths, hashes = self.live()
        observation = pub.read_json(paths[('bun', 'observation')])
        observation['after']['hello.txt']['sha256'] = '0'*64
        self.rewrite(live, paths, 'observation', observation)
        with self.assertRaisesRegex(ValueError, 'exact Hello World file'):
            self.verify(live, paths, hashes)

    def test_rehashed_raw_failure_cannot_pass_summary(self):
        live, paths, hashes = self.live()
        event = json.loads(paths[('bun', 'runner')].read_text())
        event['payload']['result']['terminal']['classification'] = 'failure'
        self.rewrite(live, paths, 'runner', event)
        with self.assertRaisesRegex(ValueError, 'Raw runner terminal'):
            self.verify(live, paths, hashes)

    def test_report_cannot_substitute_kernel_identity(self):
        live, paths, hashes = self.live()
        live['runners']['bun']['kernel']['sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'Retained kernel bytes'):
            self.verify(live, paths, hashes)

    def test_rehashed_descriptor_cannot_change_kernel_source(self):
        live, paths, hashes = self.live()
        descriptor = pub.read_json(paths[('bun', 'descriptor')])
        descriptor['source']['commit'] = 'f'*40
        self.rewrite(live, paths, 'descriptor', descriptor)
        with self.assertRaisesRegex(ValueError, 'Kernel descriptor'):
            self.verify(live, paths, hashes)

    def test_native_capture_is_digest_bound_and_required(self):
        live, paths, hashes = self.live()
        paths[('bun', 'native')].write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Live evidence bytes changed'):
            self.verify(live, paths, hashes)
        del live['runners']['bun']['evidence']['native']
        with self.assertRaisesRegex(ValueError, 'Complete retained live evidence'):
            self.verify(live, paths, hashes)

    def test_unbound_live_file_rejected_at_publication(self):
        import assemble_kernel_rc
        live, paths, hashes = self.live()
        live_path = self.root/'live.json'; live_path.write_text(json.dumps(live))
        output = self.root/'complete'
        plan = assemble_kernel_rc.assemble(self.roots, output, self.evidence, live_path)
        plan['artifacts'] = [a for a in plan['artifacts'] if a['name'] != custody.live_asset_name('bun', 'native', live['runners']['bun']['evidence']['native'])]
        with self.assertRaisesRegex(ValueError, 'not bound to the reviewed plan'):
            custody.verify_live_evidence(plan, output, hashes)


if __name__ == '__main__':
    unittest.main()
