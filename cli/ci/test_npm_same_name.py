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
            self.assertEqual(payload['version'], cohort['version']+'-'+platform)
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
                version=plan['version']+('-'+alias.removeprefix('@openprose/prose-cli-') if alias!=pub.PACKAGES[-1] else '')
                self.assertEqual((name,route,token),('@openprose/prose-cli','oidc',None))
                self.assertEqual(tag,'rc' if alias==pub.PACKAGES[-1] else 'platform-'+alias.removeprefix('@openprose/prose-cli-'))
                order.append(alias);published[version]=pub.npm_integrity(tarball)
            environment={'GITHUB_REPOSITORY':pub.REPOSITORY,'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_WORKFLOW_REF':pub.REPOSITORY+'/.github/workflows/cli-publish.yml@refs/heads/main'}
            with patch.dict(os.environ,environment,clear=True),patch.object(pub,'run',return_value='{"visibility":"public"}'),patch.object(pub,'verify_local',return_value=(packages,{})),patch.object(pub,'registry_package_exists',return_value=True),patch.object(pub,'registry_integrity',side_effect=lambda name,version:published.get(version)),patch.object(pub,'sign_artifacts'),patch.object(pub,'publish_package',side_effect=publish):
                pub.publish(plan,root,None,None,None)
            self.assertEqual(order,list(pub.PACKAGES))
            self.assertEqual(json.loads((root/'publication-receipt.json').read_text())['schema'],'openprose.cli-publication-receipt/2')
