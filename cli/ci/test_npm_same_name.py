from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import build_kernel_rc as build
import npm_alias_install
import package_local as pack
import publication as pub


class SameNameNpmTests(unittest.TestCase):
    def cohort(self):
        manifest, digest = pack.read_image_manifest(pack.DIAGNOSTIC_IMAGE_MANIFEST)
        image = pack.image_identity(manifest, digest)
        return pack.npm_cohort(mode='kernel-rc', version='0.15.0-rc.2', source_revision='a'*40,
                               image=image, publication_platforms='posix-four'), image

    def payload(self, cohort, image, platform, binary):
        runtime = {'minimumGlibc': '2.34', 'requiredGlibcMaximum': {'bun': '2.34', 'rust': '2.34'}, 'executionEvidence': 'ubuntu-22.04-only'}
        return pack.npm_platform_manifest(cohort['version'], platform, binary, 'a'*40, image, cohort, runtime if platform.startswith('linux-') else 'not-applicable')

    def test_payload_identity_and_exact_aliases(self):
        cohort, image = self.cohort()
        root = pack.npm_meta_manifest(cohort['version'], cohort, b'launcher')
        self.assertEqual(cohort['schema'], 'openprose.npm-cohort/3')
        for platform in pub.PLATFORMS:
            payload = self.payload(cohort,image,platform,b'binary')
            self.assertEqual(payload['name'], root['name'])
            self.assertEqual(payload['version'], '0.15.0-0.rc.2-'+platform)
            self.assertEqual(root['optionalDependencies']['@openprose/prose-cli-'+platform],
                             'npm:@openprose/prose-cli@'+payload['version'])
            self.assertNotIn('scripts', payload)

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'), 'npm and Node required')
    def test_original_tarballs_install_offline_with_alias_and_reject_tampering(self):
        cohort, image = self.cohort()
        platform = pack.current_platform_id()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = build.environment(root, {'PATH': os.environ['PATH'], 'HOME': directory})
            binary = b'#!/bin/sh\nprintf "alias-install-ok\\n"\n'
            launcher = (pack.ROOT/'cli/bun/npm/bin/prose.js').read_text().replace('__OPENPROSE_COHORT__', json.dumps(cohort, sort_keys=True,separators=(',',':'))).encode()
            meta = pack.npm_meta_manifest(cohort['version'], cohort, launcher)
            payload = self.payload(cohort,image,platform,binary)
            meta_tar, payload_tar = root/'root.tgz', root/'payload.tgz'
            pack.tar_gz(meta_tar, [('package/package.json',json.dumps(meta).encode(),0o644),('package/bin/prose.js',launcher,0o755)],0)
            pack.tar_gz(payload_tar, [('package/package.json',json.dumps(payload).encode(),0o644),('package/bin/prose',binary,0o755)],0)
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (meta_tar,payload_tar)}
            receipt = npm_alias_install.install(meta_tar,payload_tar,root/'prefix',env=env,cwd=root,command=build.command,log=root/'install.log')
            executable = root/'prefix/bin/prose'
            run = subprocess.run([shutil.which('node'), str(executable)],env=env,capture_output=True)
            self.assertEqual(run.returncode,0,run.stderr.decode())
            self.assertEqual(run.stdout,b'alias-install-ok\n')
            self.assertEqual(receipt['installation'],'offline-original-root-tarball')
            aliases=list((root/'prefix').rglob('prose-cli-'+platform+'/package.json'))
            self.assertEqual(len(aliases),1)
            installed=aliases[0]
            self.assertEqual(json.loads(installed.read_text())['name'],'@openprose/prose-cli')
            self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
            altered=json.loads(installed.read_text());altered['version']=cohort['version'];installed.write_text(json.dumps(altered))
            rejected=subprocess.run([shutil.which('node'),str(executable)],env=env,capture_output=True)
            self.assertNotEqual(rejected.returncode,0)
            self.assertIn(b'requires exactly',rejected.stderr)

    def test_oidc_publish_payloads_first_with_separate_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);packages={a:a.split('/')[1]+'.tgz' for a in pub.PACKAGES};artifacts=[]
            for alias,filename in packages.items():
                (root/filename).write_bytes(alias.encode());artifacts.append({'name':filename,'sha256':pub.digest(root/filename)})
            plan={'schema':'openprose.cli-publication/2','version':'0.15.0-rc.2','source':'a'*40,'signing':'unsigned-rc','artifacts':artifacts}
            published={};order=[]
            def publish(name,tarball,tag,route,token):
                alias=next(a for a,f in packages.items() if f==tarball.name)
                version='0.15.0-0.rc.2-'+alias.removeprefix('@openprose/prose-cli-') if alias!=pub.PACKAGES[-1] else plan['version']
                self.assertEqual((name,route,token),('@openprose/prose-cli','oidc',None))
                self.assertEqual(tag,'rc' if alias==pub.PACKAGES[-1] else 'platform-'+alias.removeprefix('@openprose/prose-cli-'))
                order.append(alias);published[version]=pub.npm_integrity(tarball)
            environment={'GITHUB_REPOSITORY':pub.REPOSITORY,'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_WORKFLOW_REF':pub.REPOSITORY+'/.github/workflows/cli-publish.yml@refs/heads/main'}
            with patch.dict(os.environ,environment,clear=True),patch.object(pub,'run',return_value='{"visibility":"public"}'),patch.object(pub,'verify_local',return_value=(packages,{})),patch.object(pub,'registry_package_exists',return_value=True),patch.object(pub,'registry_integrity',side_effect=lambda name,version:published.get(version)),patch.object(pub,'sign_artifacts'),patch.object(pub,'publish_package',side_effect=publish):
                pub.publish(plan,root,None,None,None)
            self.assertEqual(order,list(pub.PACKAGES))
            self.assertEqual(json.loads((root/'publication-receipt.json').read_text())['schema'],'openprose.cli-publication-receipt/2')

    def test_partial_resume_and_conflicting_versions_preflight_before_mutation(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                packages = {alias: alias.split('/')[1] + '.tgz' for alias in pub.PACKAGES}
                artifacts = []
                for alias, filename in packages.items():
                    (root / filename).write_bytes(alias.encode())
                    artifacts.append({'name': filename, 'sha256': pub.digest(root / filename)})
                plan = {'schema': 'openprose.cli-publication/2', 'version': '0.15.0-rc.2',
                        'source': 'a' * 40, 'signing': 'unsigned-rc', 'artifacts': artifacts}
                versions = {alias: '0.15.0-0.rc.2-' + alias.removeprefix('@openprose/prose-cli-')
                            if alias != pub.PACKAGES[-1] else plan['version'] for alias in pub.PACKAGES}
                first = pub.PACKAGES[0]
                published = {versions[first]: pub.npm_integrity(root / packages[first])}
                if conflict:
                    published[versions[pub.PACKAGES[-1]]] = 'sha512-conflicting-bytes'
                order = []
                def publish(name, tarball, tag, route, token):
                    alias = next(a for a, filename in packages.items() if filename == tarball.name)
                    order.append(alias)
                    published[versions[alias]] = pub.npm_integrity(tarball)
                environment = {'GITHUB_REPOSITORY': pub.REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
                               'GITHUB_EVENT_NAME': 'workflow_dispatch',
                               'GITHUB_WORKFLOW_REF': pub.REPOSITORY + '/.github/workflows/cli-publish.yml@refs/heads/main'}
                with ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, environment, clear=True))
                    stack.enter_context(patch.object(pub, 'run', return_value='{"visibility":"public"}'))
                    stack.enter_context(patch.object(pub, 'verify_local', return_value=(packages, {})))
                    stack.enter_context(patch.object(pub, 'registry_package_exists', return_value=True))
                    stack.enter_context(patch.object(pub, 'registry_integrity', side_effect=lambda name, version: published.get(version)))
                    sign = stack.enter_context(patch.object(pub, 'sign_artifacts'))
                    stack.enter_context(patch.object(pub, 'publish_package', side_effect=publish))
                    if conflict:
                        with self.assertRaisesRegex(ValueError, 'different bytes'):
                            pub.publish(plan, root, None, None, None)
                        sign.assert_not_called()
                        self.assertEqual(order, [])
                        self.assertFalse((root / 'publication-receipt.json').exists())
                    else:
                        pub.publish(plan, root, None, None, None)
                        self.assertEqual(order, list(pub.PACKAGES[1:]))
                        self.assertEqual(order[-1], pub.PACKAGES[-1])

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'), 'npm and Node required')
    def test_npm_ranges_select_launcher_instead_of_payload_versions(self):
        npm_root = Path(shutil.which('npm')).resolve().parents[1]
        semver = npm_root / 'node_modules/semver'
        cases = []
        for version in ('0.15.0-rc.2', '0.15.0-dev.2', '0.15.0'):
            payloads = [pack.npm_payload_version(version, platform) for platform in pub.PLATFORMS]
            cases.append({'root': version, 'payloads': payloads})
        script = r"""
const semver = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
const results = cases.map(c => ({
  selected: semver.maxSatisfying([c.root, ...c.payloads], '^' + c.root),
  payloadsBelowRoot: c.payloads.every(p => semver.lt(p, c.root)),
}));
process.stdout.write(JSON.stringify(results));
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(semver), json.dumps(cases)],
                                capture_output=True, check=True, text=True)
        for case, observed in zip(cases, json.loads(result.stdout)):
            self.assertEqual(observed, {'selected': case['root'], 'payloadsBelowRoot': True})


class RegistryVisibilityTests(unittest.TestCase):
    def test_delayed_visibility_polls_reads_without_republication(self):
        versions = {'arm': '1.0.0-arm', 'x64': '1.0.0-x64'}
        expected = {'arm': 'sha512-arm', 'x64': 'sha512-x64'}
        with patch.object(pub, 'registry_integrity', side_effect=[None, 'sha512-x64', 'sha512-arm']) as read, patch.object(pub.time, 'monotonic', return_value=0), patch.object(pub.time, 'sleep') as sleep, patch.object(pub, 'publish_package') as publish:
            pub.await_registry_integrities(versions, expected)
        self.assertEqual(read.call_count, 3)
        sleep.assert_called_once_with(30)
        publish.assert_not_called()

    def test_conflicting_bytes_stop_without_wait_or_publish(self):
        with patch.object(pub, 'registry_integrity', return_value='sha512-other'), patch.object(pub.time, 'sleep') as sleep, patch.object(pub, 'publish_package') as publish:
            with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                pub.await_registry_integrities({'arm':'1.0.0-arm'}, {'arm':'sha512-arm'})
        sleep.assert_not_called();publish.assert_not_called()

    def test_absence_has_finite_deadline_without_republication(self):
        with patch.object(pub, 'registry_integrity', return_value=None), patch.object(pub.time, 'monotonic', side_effect=[0, 0, 21]), patch.object(pub.time, 'sleep') as sleep, patch.object(pub, 'publish_package') as publish:
            with self.assertRaisesRegex(ValueError, 'reconcile before resuming'):
                pub.await_registry_integrities({'arm':'1.0.0-arm'}, {'arm':'sha512-arm'}, timeout=20)
        sleep.assert_called_once_with(20);publish.assert_not_called()

    def test_root_waits_for_all_payloads_and_stays_absent_on_timeout(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root=Path(directory); packages={a:a.split('/')[1]+'.tgz' for a in pub.PACKAGES}
                artifacts=[]
                for alias, filename in packages.items():
                    (root/filename).write_bytes(alias.encode()); artifacts.append({'name':filename,'sha256':pub.digest(root/filename)})
                plan={'schema':'openprose.cli-publication/2','version':'0.15.0-rc.2','source':'a'*40,'signing':'unsigned-rc','artifacts':artifacts}
                versions={a:pack.npm_payload_version(plan['version'],a.removeprefix('@openprose/prose-cli-')) if a!=pub.PACKAGES[-1] else plan['version'] for a in pub.PACKAGES}
                accepted={};visible={};order=[]
                def publish(name, package, tag, route, token):
                    alias=next(a for a,f in packages.items() if f==package.name)
                    order.append(alias)
                    if alias==pub.PACKAGES[-1]:
                        self.assertEqual(set(visible),{versions[a] for a in pub.PACKAGES[:-1]})
                        visible[versions[alias]]=pub.npm_integrity(package)
                    else:accepted[versions[alias]]=pub.npm_integrity(package)
                environment={'GITHUB_REPOSITORY':pub.REPOSITORY,'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_WORKFLOW_REF':pub.REPOSITORY+'/.github/workflows/cli-publish.yml@refs/heads/main'}
                stack.enter_context(patch.dict(os.environ,environment,clear=True))
                stack.enter_context(patch.object(pub,'run',return_value='{"visibility":"public"}'))
                stack.enter_context(patch.object(pub,'verify_local',return_value=(packages,{})))
                stack.enter_context(patch.object(pub,'registry_package_exists',return_value=True))
                stack.enter_context(patch.object(pub,'registry_integrity',side_effect=lambda name,v:visible.get(v)))
                stack.enter_context(patch.object(pub,'sign_artifacts'))
                stack.enter_context(patch.object(pub,'publish_package',side_effect=publish))
                stack.enter_context(patch.object(pub.time,'sleep',side_effect=lambda seconds:visible.update(accepted)))
                stack.enter_context(patch.object(pub.time,'monotonic',side_effect=[0,1201] if timeout else lambda:0))
                if timeout:
                    with self.assertRaisesRegex(ValueError,'reconcile before resuming'):pub.publish(plan,root,None,None,None)
                    self.assertEqual(order,list(pub.PACKAGES[:-1]));self.assertFalse((root/'publication-receipt.json').exists())
                else:
                    pub.publish(plan,root,None,None,None)
                    self.assertEqual(order,list(pub.PACKAGES));self.assertTrue((root/'publication-receipt.json').exists())
