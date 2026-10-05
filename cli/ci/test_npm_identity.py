"""Verify npm identities and explicit canonical-kernel RC platform selection."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import package_local as package


class IdentityTests(unittest.TestCase):
    def test_names_launcher_bindings_and_self_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            image={'formatVersion':'1','version':'test','sha256':'a'*64,'manifestSha256':'b'*64,'purpose':'transport-test','releaseEligible':False}
            meta,platform=package.npm_packages(root,b'fixture binary','0.1.0','darwin-arm64',0,'c'*40,image,'not-applicable','development',b'hello',package_name='@openprose/prose')
            self.assertEqual(meta.name,'openprose-prose-0.1.0.tgz')
            with tarfile.open(meta) as archive:
                manifest=json.load(archive.extractfile('package/package.json'))
                launcher=archive.extractfile('package/bin/prose.js').read()
                readme=archive.extractfile('package/README.md').read()
            self.assertEqual(manifest['name'],'@openprose/prose')
            self.assertEqual(manifest['openproseLauncher']['sha256'],hashlib.sha256(launcher).hexdigest())
            self.assertIn(b'path.basename(metaRoot) !== "prose"',launcher)
            self.assertNotIn(b'@openprose/prose-cli',launcher+readme)
            self.assertNotIn(b'openprose-prose-cli-',readme)
            self.assertTrue(all(k.startswith('@openprose/prose-') for k in manifest['optionalDependencies']))
            with tarfile.open(platform) as archive:
                p=json.load(archive.extractfile('package/package.json'))
                self.assertEqual(p['name'],'@openprose/prose-darwin-arm64')
                self.assertEqual(p['openproseBinarySha256'],hashlib.sha256(b'fixture binary').hexdigest())

    def test_new_identity_installs_offline_and_preserves_exit_status(self):
        npm = shutil.which("npm")
        node = shutil.which("node")
        if npm is None or node is None:
            self.skipTest("Node and npm are required for the installation test")
        platform = package.current_platform_id()
        if platform.startswith("win32"):
            self.skipTest("Windows distribution is not admitted")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_source, image_hash = package.read_image_manifest(package.SENTINEL_IMAGE_MANIFEST)
            image = package.image_identity(image_source, image_hash)
            binary = ("#!" + sys.executable + "\nimport sys\nprint('identity fixture')\nraise SystemExit(37 if '--fail' in sys.argv else 0)\n").encode()
            runtime = {"minimumGlibc": "2.34", "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"}, "executionEvidence": "ubuntu-22.04-only"} if platform.startswith("linux") else "not-applicable"
            meta, native = package.npm_packages(root, binary, "0.1.0", platform, 0, "c"*40, image, runtime, "development", b"hello", package_name="@openprose/prose")
            home = root / "home"; home.mkdir()
            prefix = root / "prefix"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "npm_config_cache": str(root / "cache"), "npm_config_userconfig": str(root / "empty-npmrc"), "npm_config_registry": "http://127.0.0.1:9"}
            installed = subprocess.run([npm, "install", "--global", "--prefix", str(prefix), "--ignore-scripts", "--offline", "--no-audit", "--no-fund", str(meta), str(native)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            launcher = prefix / "bin/prose"
            result = subprocess.run([str(launcher), "--fail"], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertEqual(result.stdout, "identity fixture\n")

    def test_unapproved_names_and_release_modes_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name,mode in [('@other/prose','development'),('@openprose/prose','alpha'),('@openprose/prose','release')]:
                with self.assertRaises(package.PackageError):
                    package.npm_packages(Path(tmp),b'x','0.1.0','darwin-arm64',0,'c'*40,{},'not-applicable',mode,b'',package_name=name)
            self.assertEqual(list(Path(tmp).iterdir()),[])


class KernelReleaseCandidateTests(unittest.TestCase):
    def image(self):
        return {'formatVersion': '1', 'version': 'core-rc', 'sha256': 'a'*64,
                'manifestSha256': 'b'*64, 'purpose': 'canonical-language-runtime',
                'releaseEligible': True}

    def test_four_platform_meta_packages_are_identical_and_non_authorizing(self):
        digests = []
        with tempfile.TemporaryDirectory() as temporary:
            for platform in package.POSIX_PUBLICATION_PLATFORMS:
                root = Path(temporary) / platform
                root.mkdir()
                runtime = {'minimumGlibc': '2.34', 'requiredGlibcMaximum': {'rust': '2.34', 'bun': '2.34'}, 'executionEvidence': 'ubuntu-22.04-only'} if platform.startswith('linux-') else 'not-applicable'
                meta, native = package.npm_packages(
                    root, b'fixture-' + platform.encode(), '0.15.0-rc.1', platform,
                    123, 'c'*40, self.image(), runtime, 'release', b'hello',
                    publication_platforms='posix-four')
                digests.append(hashlib.sha256(meta.read_bytes()).hexdigest())
                with tarfile.open(meta) as archive:
                    manifest = json.load(archive.extractfile('package/package.json'))
                    readme = archive.extractfile('package/README.md').read()
                    launcher = archive.extractfile('package/bin/prose.js').read()
                self.assertEqual(manifest['name'], '@openprose/prose-cli')
                self.assertEqual(manifest['repository']['url'], 'git+https://github.com/openprose/prose-cli.git')
                self.assertEqual(manifest['openproseCohort']['admittedPlatforms'], list(package.POSIX_PUBLICATION_PLATFORMS))
                self.assertEqual(manifest['optionalDependencies'], {'@openprose/prose-cli-' + name: '0.15.0-rc.1' for name in package.POSIX_PUBLICATION_PLATFORMS})
                self.assertIs(manifest['openproseCohort']['releaseEligible'], False)
                self.assertIs(manifest['openproseCohort']['publicationAuthorized'], False)
                self.assertEqual(manifest['openproseCohort']['releaseChannel'], 'release-candidate')
                self.assertEqual(manifest['openproseLauncher']['sha256'], hashlib.sha256(launcher).hexdigest())
                self.assertNotIn(b'win32-x64', readme)
                self.assertIn(b'Windows is not included', readme)
                with tarfile.open(native) as archive:
                    native_manifest = json.load(archive.extractfile('package/package.json'))
                self.assertEqual(native_manifest['repository']['url'], manifest['repository']['url'])
                self.assertEqual(native_manifest['openproseCohort'], manifest['openproseCohort'])
        self.assertEqual(len(set(digests)), 1)

    def test_posix_route_does_not_change_legacy_cohorts(self):
        common = dict(version='0.15.0-rc.1', source_revision='c'*40, image=self.image())
        self.assertEqual(package.npm_cohort(mode='release', **common)['admittedPlatforms'], sorted(package.PLATFORMS))
        self.assertEqual(package.npm_cohort(mode='alpha', **common)['admittedPlatforms'], sorted(package.ALPHA_HARNESS_SUPPORT))

    def test_posix_route_rejects_unqualified_inputs(self):
        baseline = dict(mode='release', version='0.15.0-rc.1', source_revision='c'*40,
                        image=self.image(), publication_platforms='posix-four')
        for changes in ({'mode': 'development'}, {'mode': 'alpha'}, {'version': '0.15.0'},
                        {'version': '0.15.0-dev.1'}, {'version': '0.15.0-rc.01'},
                        {'source_revision': 'main'}, {'publication_platforms': 'windows'},
                        {'image': dict(self.image(), releaseEligible=False)},
                        {'image': dict(self.image(), purpose='sentinel-transport-test')},
                        {'image': dict(self.image(), purpose='functional-alpha-placeholder')}):
            with self.subTest(changes=changes), self.assertRaises(package.PackageError):
                package.npm_cohort(**dict(baseline, **changes))

    def test_posix_route_rejects_windows_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(package.PackageError):
                package.npm_packages(root, b'fixture', '0.15.0-rc.1', 'win32-x64',
                                     0, 'c'*40, self.image(), 'not-applicable',
                                     'release', b'hello', publication_platforms='posix-four')
            self.assertEqual(list(root.iterdir()), [])


class PublishedKernelCandidateTests(unittest.TestCase):
    def diagnostic(self):
        image, digest = package.read_image_manifest(package.DIAGNOSTIC_IMAGE_MANIFEST)
        return image, package.image_identity(image, digest)

    def test_schema_three_separates_diagnostic_from_runtime_policy(self):
        _, image = self.diagnostic()
        hashes = []
        with tempfile.TemporaryDirectory() as temporary:
            for platform in package.POSIX_PUBLICATION_PLATFORMS:
                root = Path(temporary) / platform
                root.mkdir()
                runtime = {'minimumGlibc': '2.34', 'requiredGlibcMaximum': {'rust': '2.34', 'bun': '2.34'}, 'executionEvidence': 'ubuntu-22.04-only'} if platform.startswith('linux-') else 'not-applicable'
                meta, native = package.npm_packages(root, b'fixture', '0.15.0-rc.1', platform,
                    0, 'c'*40, image, runtime, 'kernel-rc', b'hello', publication_platforms='posix-four')
                hashes.append(hashlib.sha256(meta.read_bytes()).hexdigest())
                with tarfile.open(meta) as archive:
                    manifest = json.load(archive.extractfile('package/package.json'))
                    readme = archive.extractfile('package/README.md').read()
                cohort = manifest['openproseCohort']
                self.assertEqual(cohort['schema'], 'openprose.npm-cohort/3')
                self.assertEqual(cohort['imageSource'], 'published-on-run')
                self.assertEqual(cohort['purpose'], 'published-kernel-loader')
                self.assertEqual(cohort['kernelPolicy'], package.PUBLISHED_KERNEL_POLICY)
                self.assertNotIn('image', cohort)
                self.assertNotIn('releaseEligible', cohort['embeddedDiagnosticImage'])
                self.assertFalse(cohort['releaseEligible'])
                self.assertFalse(cohort['publicationAuthorized'])
                self.assertIn(b'unsigned release candidate', readme)
                self.assertIn(b'latest published kernel', readme)
                with tarfile.open(native) as archive:
                    native_manifest = json.load(archive.extractfile('package/package.json'))
                self.assertNotIn('openproseImage', native_manifest)
                self.assertEqual(native_manifest['openproseEmbeddedDiagnosticImage'], cohort['embeddedDiagnosticImage'])
                self.assertEqual(native_manifest['openproseKernelPolicy'], cohort['kernelPolicy'])
        self.assertEqual(len(set(hashes)), 1)

    def test_kernel_candidate_rejects_changed_fixture_and_missing_platform_policy(self):
        _, image = self.diagnostic()
        args = dict(mode='kernel-rc', version='0.15.0-rc.1', source_revision='c'*40, image=image, publication_platforms='posix-four')
        for changes in ({'publication_platforms': None}, {'version': '0.15.0'},
                        {'image': dict(image, sha256='0'*64)},
                        {'image': dict(image, purpose='canonical-language-runtime')}):
            with self.subTest(changes=changes), self.assertRaises(package.PackageError):
                package.npm_cohort(**dict(args, **changes))

    def test_doctor_must_prove_published_startup_release_profile_and_no_test_seams(self):
        image, _ = self.diagnostic()
        report = {'schema': 'openprose.doctor-report/1',
                  'image': {'formatVersion': image['imageFormatVersion'], 'version': image['imageVersion'], 'sha256': image['aggregateSha256']['sha256'], 'releaseEligible': image['releaseEligible']},
                  'runner': {'name': 'bun', 'version': '0.15.0-rc.1', 'commit': 'c'*40},
                  'build': {'profile': 'release', 'testSeamsEnabled': False},
                  'imageSource': 'published-on-run'}
        version = subprocess.CompletedProcess([], 0, b'prose 0.15.0-rc.1 (bun)\n', b'')
        def check(value):
            doctor = subprocess.CompletedProcess([], 10, json.dumps(value).encode(), b'')
            with mock.patch.object(package, 'run_bounded', side_effect=[version, doctor]):
                return package.verify_product(Path('/fixture'), 'bun', '0.15.0-rc.1', 'c'*40, image, 'kernel-rc', test_seams_enabled=False)
        self.assertEqual(check(report), report['build'])
        for changes in ({'imageSource': 'embedded'}, {'build': {'profile': 'development', 'testSeamsEnabled': False}}, {'build': {'profile': 'release', 'testSeamsEnabled': True}}):
            with self.subTest(changes=changes), self.assertRaises(package.PackageError):
                check(dict(report, **changes))

    def test_schema_three_launcher_runs_offline_and_rejects_runtime_policy_drift(self):
        npm = shutil.which('npm')
        if npm is None or shutil.which('node') is None:
            self.skipTest('Node and npm required')
        platform = package.current_platform_id()
        if platform not in package.POSIX_PUBLICATION_PLATFORMS:
            self.skipTest('Four POSIX publication platforms only')
        _, image = self.diagnostic()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = ('#!' + sys.executable + "\nprint('published-on-run doctor fixture')\n").encode()
            runtime = {'minimumGlibc': '2.34', 'requiredGlibcMaximum': {'rust': '2.34', 'bun': '2.34'}, 'executionEvidence': 'ubuntu-22.04-only'} if platform.startswith('linux-') else 'not-applicable'
            meta, native = package.npm_packages(root, binary, '0.15.0-rc.1', platform,
                0, 'c'*40, image, runtime, 'kernel-rc', b'hello', publication_platforms='posix-four')
            home = root / 'home'; home.mkdir()
            prefix = root / 'prefix'
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'npm_config_cache': str(root/'cache'), 'npm_config_userconfig': str(root/'empty-npmrc'), 'npm_config_registry': 'http://127.0.0.1:9'}
            import build_kernel_rc
            import npm_alias_install
            npm_alias_install.install(meta, native, prefix, env=env, cwd=root,
                                      command=build_kernel_rc.command, log=root/'npm-install.log')
            launcher = prefix / 'bin/prose'
            ran = subprocess.run([str(launcher), 'cli', 'doctor'], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(ran.stdout, 'published-on-run doctor fixture\n')
            native_manifest_path = next(prefix.rglob('prose-cli-'+platform+'/package.json'))
            native_manifest = json.loads(native_manifest_path.read_text())
            native_manifest['openproseKernelPolicy']['resolution'] = 'fixed-image'
            native_manifest_path.write_text(json.dumps(native_manifest))
            rejected = subprocess.run([str(launcher), 'cli', 'doctor'], env=env, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(rejected.stdout, '')
            self.assertIn('cohort', rejected.stderr)


if __name__=='__main__': unittest.main()
