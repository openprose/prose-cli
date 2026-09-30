import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import publication as p


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = {'schema': 'openprose.cli-publication/1', 'version': '0.15.0-rc.1', 'source': 'a'*40, 'qualification': {'status': 'kernel-smoke-qualified', 'evidence': 'https://github.com/openprose/example-evidence/tree/' + 'b'*40 + '/test'}, 'artifacts': [], 'preflight': 'preflight.json', 'macos': {}, 'npmProvenance': True, 'signing': 'apple-notarized'}
        self.image = {'formatVersion': 1, 'version': 'test', 'sha256': 'c'*64, 'manifestSha256': 'd'*64, 'purpose': 'canonical-language-runtime', 'releaseEligible': True}
        self.add('preflight.json', json.dumps({'schema': 'openprose.release-preflight-report/1', 'status': 'pass', 'failures': [], 'sourceSha': 'a'*40, 'version': self.plan['version'], 'protectedAuthority': {'status': 'pass'}, 'image': {'imageSha256': 'c'*64, 'manifestSha256': 'd'*64, 'version': 'test', 'purpose': 'canonical-language-runtime', 'releaseEligible': True}}).encode())
        for platform in p.PLATFORMS:
            for implementation in ('bun', 'rust'):
                self.tar(implementation + '-' + platform + '.tgz', {'root/prose': (implementation+platform).encode()}, 'standalone', platform, implementation)
            if platform.startswith('darwin'):
                self.add(platform + '-receipt.json', b'{}')
                self.add(platform + '-notarization.zip', b'not a real signature')
                self.plan['macos'][platform] = {'receipt': platform + '-receipt.json', 'zip': platform + '-notarization.zip', 'teamId': 'ABCD123456'}
        for name in p.PACKAGES:
            meta = {'name': name, 'version': self.plan['version'], 'repository': {'url': 'git+https://github.com/' + p.REPOSITORY + '.git'}, 'openproseCohort': {'schema': 'openprose.npm-cohort/1', 'releaseChannel': 'release-candidate', 'releaseEligible': False, 'publicationAuthorized': False, 'version': self.plan['version'], 'sourceRevision': 'a'*40, 'image': self.image, 'purpose': 'canonical-language-runtime', 'admittedPlatforms': sorted(p.PLATFORMS)}}
            members = {}
            platform = 'all'
            if name == p.PACKAGES[-1]:
                meta['optionalDependencies'] = {n: self.plan['version'] for n in p.PACKAGES[:-1]}
            else:
                platform = name.removeprefix('@openprose/prose-cli-')
                meta.update(openproseSourceRevision='a'*40, openproseImage=self.image)
                members['package/bin/prose'] = ('bun'+platform).encode()
            members['package/package.json'] = json.dumps(meta).encode()
            self.tar(name.split('/')[1] + '.tgz', members, 'npm', platform, 'bun')
        self.path = self.root / 'plan.json'
        self.save()

    def add(self, name, data, kind='evidence', platform='all', implementation='shared'):
        (self.root/name).write_bytes(data)
        self.plan['artifacts'].append({'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data), 'kind': kind, 'platform': platform, 'implementation': implementation})

    def tar(self, name, members, kind, platform, implementation):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for member, data in members.items():
                info = tarfile.TarInfo(member)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.add(name, stream.getvalue(), kind, platform, implementation)

    def save(self):
        self.path.write_text(json.dumps(self.plan))

    def test_complete_inventory_validates_but_is_not_signing_evidence(self):
        plan = p.load_plan(self.path)
        packages, hashes = p.verify_local(plan, self.root)
        self.assertEqual(set(packages), set(p.PACKAGES))
        self.assertEqual(len(hashes), 12)
        with self.assertRaisesRegex(ValueError, 'Wrong signing identity'):
            p.verify_macos(plan, self.root, Path('unused'), 'unused', 'unused')

    def test_development_plan_rejected(self):
        self.plan['qualification']['status'] = 'development'
        self.save()
        with self.assertRaisesRegex(ValueError, 'Kernel qualification'):
            p.load_plan(self.path)

    def test_empty_evidence_is_valid_but_empty_packages_are_not(self):
        self.add('empty-build.log', b'')
        self.save()
        p.verify_local(p.load_plan(self.path), self.root)
        next(a for a in self.plan['artifacts'] if a['kind'] == 'standalone')['size'] = 0
        self.save()
        with self.assertRaisesRegex(ValueError, 'Invalid artifact size'):
            p.load_plan(self.path)

    def test_missing_platform_cannot_be_silently_dropped(self):
        self.plan['artifacts'] = [a for a in self.plan['artifacts'] if a['name'] != 'rust-linux-arm64-gnu.tgz']
        self.save()
        with self.assertRaisesRegex(ValueError, 'Both runners'):
            p.load_plan(self.path)

    def test_mutated_archive_fails_before_parsing(self):
        (self.root/'bun-darwin-arm64.tgz').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            p.verify_local(self.plan, self.root)

    def test_private_provenance_override_not_allowed(self):
        self.plan['npmProvenance'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'owner approves'):
            p.load_plan(self.path)

    def test_unsigned_exception_requires_rc_and_no_false_apple_claim(self):
        self.plan['signing'] = 'unsigned-rc'
        self.plan['macos'] = {}
        self.save()
        p.load_plan(self.path)
        self.plan['version'] = '0.15.0'
        self.save()
        with self.assertRaisesRegex(ValueError, 'limited to an explicit RC'):
            p.load_plan(self.path)

    def test_duplicate_json_and_unsafe_names_rejected(self):
        self.path.write_text('{"schema":1,"schema":2}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            p.load_plan(self.path)
        for value in ('../x', '/absolute', '--flag', 'x\nINJECT=y'):
            self.assertFalse(p.safe_name(value))

    def test_links_cannot_escape_archive(self):
        path = self.root/'escape.tgz'
        with tarfile.open(path, 'w:gz') as archive:
            item = tarfile.TarInfo('package/bin/prose')
            item.type = tarfile.SYMTYPE
            item.linkname = '/tmp/elsewhere'
            archive.addfile(item)
        with self.assertRaisesRegex(ValueError, 'Links'):
            p.archive_members(path)

    def test_registry_errors_are_not_missing_packages(self):
        import subprocess
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,'{"error":{"code":"E429"}}','')):
            with self.assertRaisesRegex(ValueError, 'Registry lookup failed'):
                p.registry_integrity('@openprose/prose-cli', '0.15.0')
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,'{"error":{"code":"E404"}}','')):
            self.assertIsNone(p.registry_integrity('@openprose/prose-cli', '0.15.0'))

    def test_publication_forbidden_outside_main_workflow(self):
        with patch.dict(p.os.environ, {}, clear=True), patch.object(p, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'main workflow'):
                p.publish(self.plan, self.root, None, None, None)
            run.assert_not_called()


    def test_complete_partial_recovery_uses_token_only_for_new_platform_names(self):
        self.plan['signing'] = 'unsigned-rc'
        packages = {name: name.split('/')[1] + '.tgz' for name in p.PACKAGES}
        integrities = {name: p.npm_integrity(self.root / path) for name, path in packages.items()}
        published = {p.PACKAGES[0]: integrities[p.PACKAGES[0]]}
        token = 'npm_' + 'x' * 36
        routes = []
        def command(argv):
            self.assertNotIn('NPM_BOOTSTRAP_TOKEN', p.os.environ)
            return '{"visibility":"public"}' if argv[0] == 'gh' else ''
        def publish_package(name, package, tag, route, supplied_token):
            self.assertEqual('rc', tag)
            self.assertEqual(self.root / packages[name], package)
            expected_route = 'oidc' if name == p.PACKAGES[-1] else 'bootstrap-token'
            self.assertEqual(expected_route, route)
            self.assertEqual(None if route == 'oidc' else token, supplied_token)
            published[name] = integrities[name]
            routes.append(name)
        environment = {'GITHUB_REPOSITORY': p.REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
                       'GITHUB_EVENT_NAME': 'workflow_dispatch',
                       'GITHUB_WORKFLOW_REF': p.REPOSITORY + '/.github/workflows/cli-publish.yml@refs/heads/main',
                       'NPM_BOOTSTRAP_TOKEN': token}
        with patch.dict(p.os.environ, environment, clear=True), patch.object(p, 'run', side_effect=command), patch.object(p, 'verify_local', return_value=(packages, {})), patch.object(p, 'registry_integrity', side_effect=lambda name, version: published.get(name)), patch.object(p, 'registry_package_exists', side_effect=lambda name: name == p.PACKAGES[-1] or name in published), patch.object(p, 'publish_package', side_effect=publish_package):
            p.publish(self.plan, self.root, None, None, None, bootstrap=True)
        self.assertEqual(list(p.PACKAGES[1:]), routes)
        receipt = json.loads((self.root / 'publication-receipt.json').read_text())
        self.assertEqual('already-published', receipt['credentialRoutes'][p.PACKAGES[0]])
        self.assertEqual('oidc', receipt['credentialRoutes'][p.PACKAGES[-1]])
        self.assertNotIn(token, json.dumps(receipt))


    def workflow_environment(self):
        return {'GITHUB_REPOSITORY': p.REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
                'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_WORKFLOW_REF': p.REPOSITORY + '/.github/workflows/cli-publish.yml@refs/heads/main'}

    def test_sign_only_checks_evidence_and_signs_without_npm(self):
        with patch.dict(p.os.environ, self.workflow_environment(), clear=True), patch.object(p, 'run', return_value='{"visibility":"public"}') as run, patch.object(p, 'verify_macos') as macos, patch.object(p, 'registry_integrity') as registry, patch.object(p, 'registry_package_exists') as names, patch.object(p, 'npm_command') as npm:
            p.publish(self.plan, self.root, Path('key'), 'id', 'issuer', sign_only=True)
            self.assertEqual(run.call_count, 1 + 2 * len(self.plan['artifacts']))
            macos.assert_called_once()
            registry.assert_not_called()
            names.assert_not_called()
            npm.assert_not_called()
        receipt = json.loads((self.root / 'publication-receipt.json').read_text())
        self.assertEqual(receipt['npmStatus'], 'not-published')
        self.assertEqual(receipt['operation'], 'sign-only')
        self.assertEqual(receipt['npm'], {})
        self.assertFalse(receipt['githubReleasePromoted'])

    def test_sign_only_refuses_bootstrap_and_wrong_workflow(self):
        for env, bootstrap, error in [({}, False, 'main workflow'),
                (self.workflow_environment(), True, 'Sign-only forbids'),
                ({**self.workflow_environment(), 'NPM_BOOTSTRAP_TOKEN': 'npm_' + 'x'*36}, False, 'Sign-only forbids')]:
            with patch.dict(p.os.environ, env, clear=True), patch.object(p, 'run') as run, self.assertRaisesRegex(ValueError, error):
                p.publish(self.plan, self.root, None, None, None, bootstrap=bootstrap, sign_only=True)
            run.assert_not_called()

    def test_sign_only_retains_public_repo_artifact_and_macos_gates(self):
        with patch.dict(p.os.environ, self.workflow_environment(), clear=True), patch.object(p, 'sign_artifacts') as sign:
            with patch.object(p, 'run', return_value='{"visibility":"private"}'), self.assertRaisesRegex(ValueError, 'public source'):
                p.publish(self.plan, self.root, None, None, None, sign_only=True)
            with patch.object(p, 'run', return_value='{"visibility":"public"}'):
                with self.assertRaisesRegex(ValueError, 'Apple credentials'):
                    p.publish(self.plan, self.root, None, None, None, sign_only=True)
                (self.root / 'preflight.json').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'bytes differ'):
                    p.publish(self.plan, self.root, Path('key'), 'id', 'issuer', sign_only=True)
            sign.assert_not_called()

    def test_fetch_allows_published_unsigned_rc_and_ignores_known_bundles(self):
        self.plan['signing'] = 'unsigned-rc'
        assets = [{'name': a['name'], 'size': a['size']} for a in self.plan['artifacts']]
        release = {'isDraft': False, 'isPrerelease': True, 'tagName': 'v' + self.plan['version'], 'assets': assets + [{'name': assets[0]['name'] + '.sigstore.json', 'size': 2000}]}
        def command(argv):
            if argv[1] == 'api': return json.dumps({'sha': self.plan['source']})
            if argv[2] == 'view': return json.dumps(release)
            self.assertEqual(argv[2], 'download')
            self.assertFalse(argv[argv.index('--pattern')+1].endswith('.sigstore.json'))
            return ''
        with patch.object(p, 'run', side_effect=command) as run, patch.object(p, 'verify_local') as verify:
            p.fetch(self.plan, self.root / 'download')
            self.assertEqual(run.call_count, len(assets) + 2)
            verify.assert_called_once()

    def test_fetch_reconstructs_only_exact_empty_evidence(self):
        self.add('empty-build.log', b'')
        release = {'isDraft': True, 'isPrerelease': True, 'tagName': 'v' + self.plan['version'],
                   'assets': [{'name': a['name'], 'size': a['size']} for a in self.plan['artifacts'] if a['size']]}
        calls = []
        def fake(argv):
            calls.append(argv)
            if argv[1] == 'api': return json.dumps({'sha': self.plan['source']})
            if argv[2] == 'view': return json.dumps(release)
            name = argv[argv.index('--pattern') + 1]
            (self.root / 'download' / name).write_bytes((self.root / name).read_bytes())
            return ''
        with patch.object(p, 'run', side_effect=fake):
            p.fetch(self.plan, self.root / 'download')
        self.assertEqual((self.root / 'download/empty-build.log').read_bytes(), b'')
        self.assertFalse(any('empty-build.log' in call for call in calls))
        self.plan['artifacts'][-1]['sha256'] = 'f' * 64
        self.save()
        with self.assertRaisesRegex(ValueError, 'Empty evidence digest mismatch'):
            p.load_plan(self.path)

    def test_fetch_rejects_public_stable_unknown_assets_and_inventory_changes(self):
        self.plan['signing'] = 'unsigned-rc'
        assets = [{'name': a['name'], 'size': a['size']} for a in self.plan['artifacts']]
        release = {'isDraft': False, 'isPrerelease': True, 'tagName': 'v' + self.plan['version'], 'assets': assets}
        variants = [{**release, 'isPrerelease': False},
                    {**release, 'assets': assets + [{'name': 'publication-receipt.json', 'size': 1}]},
                    {**release, 'assets': assets + [{'name': assets[0]['name'] + '.sigstore.json', 'size': 1024*1024+1}]},
                    {**release, 'assets': assets[1:]},
                    {**release, 'assets': [{**assets[0], 'size': assets[0]['size'] + 1}] + assets[1:]},
                    {**release, 'assets': assets + [assets[0]]}]
        for observed in variants:
            with patch.object(p, 'run', side_effect=[json.dumps({'sha': self.plan['source']}), json.dumps(observed)]) as run, self.assertRaises(ValueError):
                p.fetch(self.plan, self.root / 'never-created')
            self.assertEqual(run.call_count, 2)
        self.plan['signing'] = 'apple-notarized'
        with patch.object(p, 'run', side_effect=[json.dumps({'sha': self.plan['source']}), json.dumps(release)]), self.assertRaisesRegex(ValueError, 'unsigned release candidates'):
            p.fetch(self.plan, self.root / 'never-created')


class BootstrapTests(unittest.TestCase):
    token = 'npm_' + 'x' * 36

    def test_name_level_absence_requires_explicit_authorization_and_secret(self):
        existing = dict.fromkeys(p.PACKAGES)
        names = dict.fromkeys(p.PACKAGES, True)
        names[p.PACKAGES[0]] = False
        for enabled, token in [(False, self.token), (True, '')]:
            with self.assertRaisesRegex(ValueError, 'explicit bootstrap'):
                p.publication_routes(existing, names, enabled, token)
        routes = p.publication_routes(existing, names, True, self.token)
        self.assertEqual('bootstrap-token', routes[p.PACKAGES[0]])
        self.assertEqual('oidc', routes[p.PACKAGES[-1]])
        self.assertEqual('oidc', routes[p.PACKAGES[1]])

    def test_root_cannot_be_bootstrapped_and_recovery_skips_existing_bytes(self):
        existing = dict.fromkeys(p.PACKAGES)
        names = dict.fromkeys(p.PACKAGES, True)
        names[p.PACKAGES[-1]] = False
        with self.assertRaisesRegex(ValueError, 'root package'):
            p.publication_routes(existing, names, True, self.token)
        names[p.PACKAGES[-1]] = True
        existing[p.PACKAGES[0]] = 'verified-integrity'
        routes = p.publication_routes(existing, names, False, '')
        self.assertEqual('already-published', routes[p.PACKAGES[0]])
        self.assertTrue(all(route == 'oidc' for name, route in routes.items() if name != p.PACKAGES[0]))

    def test_registry_absence_is_name_not_version_and_other_errors_fail_closed(self):
        import subprocess
        for code in ['E401', 'E403', 'E429']:
            with patch.object(p, 'npm_read', return_value=subprocess.CompletedProcess([], 1, json.dumps({'error': {'code': code}}), '')):
                with self.assertRaisesRegex(ValueError, 'Package-name lookup failed'):
                    p.registry_package_exists(p.PACKAGES[0])
        with patch.object(p, 'npm_read', return_value=subprocess.CompletedProcess([], 1, '{"error":{"code":"E404"}}', '')) as read:
            self.assertFalse(p.registry_package_exists(p.PACKAGES[0]))
            self.assertEqual(['view', p.PACKAGES[0], 'name', '--json', '--registry=https://registry.npmjs.org'], read.call_args.args[0])

    def test_bootstrap_uses_private_configuration_and_preserves_provenance_oidc(self):
        import subprocess
        paths = []
        def execute(argv, **kwargs):
            env = kwargs['env']
            self.assertNotIn(self.token, argv)
            for key in ['NPM_BOOTSTRAP_TOKEN', 'NPM_TOKEN', 'NODE_AUTH_TOKEN', 'npm_config_registry']:
                self.assertNotIn(key, env)
            self.assertEqual('oidc-request-token', env['ACTIONS_ID_TOKEN_REQUEST_TOKEN'])
            self.assertEqual('oidc-request-url', env['ACTIONS_ID_TOKEN_REQUEST_URL'])
            userconfig = Path(env['NPM_CONFIG_USERCONFIG'])
            paths.append(userconfig)
            self.assertEqual(0o600, userconfig.stat().st_mode & 0o777)
            self.assertEqual(0o700, userconfig.parent.stat().st_mode & 0o777)
            self.assertEqual('//registry.npmjs.org/:_authToken=' + self.token + '\n', userconfig.read_text())
            self.assertEqual(kwargs['cwd'], userconfig.parent)
            self.assertIn('--provenance', argv)
            self.assertIn('--ignore-scripts', argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        ambient = {'NPM_BOOTSTRAP_TOKEN': self.token, 'NPM_TOKEN': 'hostile', 'NODE_AUTH_TOKEN': 'hostile', 'npm_config_registry': 'https://bad.invalid', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'oidc-request-token', 'ACTIONS_ID_TOKEN_REQUEST_URL': 'oidc-request-url'}
        with patch.dict(p.os.environ, ambient, clear=True), patch.object(p, 'registry_package_exists', return_value=False), patch.object(p.subprocess, 'run', side_effect=execute):
            p.publish_package(p.PACKAGES[0], Path('/tmp/qualified.tgz'), 'rc', 'bootstrap-token', self.token)
        self.assertTrue(paths)
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_oidc_error_is_not_retried_with_token_and_cleanup_is_guaranteed(self):
        import subprocess
        paths = []
        def execute(argv, **kwargs):
            path = Path(kwargs['env']['NPM_CONFIG_USERCONFIG'])
            paths.append(path)
            self.assertEqual('', path.read_text())
            self.assertNotIn('NPM_BOOTSTRAP_TOKEN', kwargs['env'])
            return subprocess.CompletedProcess(argv, 1, '', 'auth failed')
        with patch.dict(p.os.environ, {'NPM_BOOTSTRAP_TOKEN': self.token}), patch.object(p.subprocess, 'run', side_effect=execute) as call:
            with self.assertRaisesRegex(ValueError, 'no fallback'):
                p.publish_package(p.PACKAGES[-1], Path('/tmp/root.tgz'), 'rc', 'oidc', self.token)
            self.assertEqual(1, call.call_count)
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_bootstrap_cannot_publish_root_or_race_with_existing_name(self):
        with patch.object(p, 'npm_command') as call:
            with self.assertRaisesRegex(ValueError, 'limited to absent'):
                p.publish_package(p.PACKAGES[-1], Path('/tmp/root.tgz'), 'rc', 'bootstrap-token', self.token)
            with patch.object(p, 'registry_package_exists', return_value=True):
                with self.assertRaisesRegex(ValueError, 'now exists'):
                    p.publish_package(p.PACKAGES[0], Path('/tmp/platform.tgz'), 'rc', 'bootstrap-token', self.token)
            call.assert_not_called()

    def test_auth_config_is_removed_after_subprocess_exception(self):
        paths = []
        def execute(argv, **kwargs):
            paths.append(Path(kwargs['env']['NPM_CONFIG_USERCONFIG']))
            raise OSError('process failed')
        with patch.object(p.subprocess, 'run', side_effect=execute):
            with self.assertRaises(OSError):
                p.npm_command(['publish', '/tmp/package.tgz', '--provenance'], self.token)
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_secret_cannot_inject_npm_config(self):
        with patch.object(p.subprocess, 'run') as call:
            with self.assertRaisesRegex(ValueError, 'credential format'):
                p.npm_command(['publish', '/tmp/package.tgz'], self.token + '\nregistry=evil')
            call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
