from __future__ import annotations
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import fetch_previous_release as fetcher


def digest(value): return hashlib.sha256(value).hexdigest()

def fixture():
    objects={}
    artifacts=[]
    for platform in fetcher.PLATFORMS:
        for kind,implementation in [('standalone','rust'),('standalone','bun'),('npm','bun')]:
            name=f'{kind}-{implementation}-{platform}.tgz'
            body=name.encode()
            objects[fetcher.BASE_URL+name]=body
            artifacts.append(dict(kind=kind,implementation=implementation,platform=platform,name=name,size=len(body),sha256=digest(body)))
    name='npm-bun-all.tgz'; body=b'meta';objects[fetcher.BASE_URL+name]=body
    artifacts.append(dict(kind='npm',implementation='bun',platform='all',name=name,size=len(body),sha256=digest(body)))
    manifest=dict(schema='openprose.cli-distribution/1',version=fetcher.VERSION,source=fetcher.SOURCE,artifacts=artifacts)
    return manifest,objects

class FetchPreviousReleaseTest(unittest.TestCase):
    def run_fetch(self, root, manifest=None, mutate=None, platform='darwin-arm64'):
        default,objects=fixture();manifest=manifest or default
        encoded=(json.dumps(manifest,sort_keys=True)+'\n').encode()
        objects[fetcher.BASE_URL+'manifest.json']=encoded
        calls=[]
        def download(url,sink,maximum):
            calls.append(url)
            if mutate: mutate(url,sink,objects)
            sink.write(objects[url])
        with patch.object(fetcher,'MANIFEST_SHA256',digest(encoded)):
            receipt=fetcher.fetch(platform,root,download)
        return receipt,calls,encoded

    def test_all_four_platforms_select_exact_bytes_and_fixed_same_origin(self):
        for platform in fetcher.PLATFORMS:
            with self.subTest(platform=platform),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()/'fresh';receipt,calls,manifest=self.run_fetch(root,platform=platform)
                self.assertEqual(5,len(calls));self.assertEqual(4,len(receipt['artifacts']))
                self.assertEqual(manifest,(root/'manifest.json').read_bytes())
                self.assertEqual(platform,receipt['platform'])
                self.assertEqual(digest(manifest),receipt['manifestSha256'])
                self.assertTrue(all(url.startswith(fetcher.BASE_URL) for url in calls))
                self.assertTrue(all(platform in row['name'] or row['name']=='npm-bun-all.tgz' for row in receipt['artifacts']))
                self.assertEqual(receipt,json.loads((root/'download-receipt.json').read_text()))
                for row in receipt['artifacts']:
                    data=(root/row['name']).read_bytes()
                    self.assertEqual(row['sha256'],digest(data));self.assertEqual(row['byteLength'],len(data))

    def test_fixed_manifest_hash_and_metadata_required(self):
        manifest,_=fixture();encoded=json.dumps(manifest).encode()
        with self.assertRaisesRegex(ValueError,'identity'):fetcher.decode_manifest(encoded)
        for key,value in [('schema','wrong'),('version','0.15.0-rc.4'),('source','0'*40)]:
            changed=copy.deepcopy(manifest);changed[key]=value;data=json.dumps(changed).encode()
            with patch.object(fetcher,'MANIFEST_SHA256',digest(data)),self.assertRaisesRegex(ValueError,'fixed published'):fetcher.decode_manifest(data)
        for body in (b'not JSON',b'[]'):
            with patch.object(fetcher,'MANIFEST_SHA256',digest(body)),self.assertRaises(ValueError):fetcher.decode_manifest(body)

    def test_missing_duplicate_and_cross_platform_selection_rejected(self):
        manifest,_=fixture()
        for change in ('missing','duplicate','wrong-platform'):
            altered=copy.deepcopy(manifest)
            if change=='missing':altered['artifacts'].pop(0)
            elif change=='duplicate':altered['artifacts'].append(altered['artifacts'][0])
            else:altered['artifacts'][0]['platform']='linux-x64-musl'
            with self.subTest(change=change),self.assertRaises(ValueError):fetcher.select_artifacts(altered,'darwin-arm64')
        with self.assertRaises(ValueError):fetcher.select_artifacts(manifest,'linux-x64-musl')

    def test_unsafe_names_sizes_digests_and_classifications_rejected(self):
        original,_=fixture()
        changes=[('name','../escape'),('name','https://evil.test/a'),('name','manifest.json'),('name','download-receipt.json'),('name','a?query'),('size',True),('size',0),('size',fetcher.MAX_ARTIFACT+1),('sha256','bad'),('platform',[])]
        for key,value in changes:
            changed=copy.deepcopy(original);changed['artifacts'][0][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):fetcher.select_artifacts(changed,'darwin-arm64')

    def test_output_must_be_fresh_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'existing';root.mkdir();(root/'keep').write_bytes(b'keep')
            with self.assertRaisesRegex(ValueError,'fresh'):self.run_fetch(root)
            self.assertEqual(b'keep',(root/'keep').read_bytes())
            link=Path(tmp).resolve()/'link';link.symlink_to(root,target_is_directory=True)
            with self.assertRaises(ValueError):self.run_fetch(link)
            with self.assertRaisesRegex(ValueError,'symlinks'):self.run_fetch(link/'child')
            self.assertFalse((root/'child').exists())

    def test_wrong_artifact_bytes_and_short_download_fail_without_receipt(self):
        for body in (b'wrong',b''):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()/'fresh'
                def corrupt(url,sink,objects):
                    if url.endswith('standalone-rust-darwin-arm64.tgz'):objects[url]=body
                with self.assertRaises(ValueError):self.run_fetch(root,mutate=corrupt)
                self.assertFalse((root/'download-receipt.json').exists())

    def test_bounded_download_enforces_length_even_without_content_length(self):
        stream=io.BytesIO();writer=fetcher.BoundedWriter(stream,4);writer.write(b'ab')
        with self.assertRaises(ValueError):writer.write(b'cde')
        self.assertEqual(b'ab',stream.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fresh'
            def excessive(url,sink,objects):
                if url.endswith('.tgz'):sink.write(b'x'*1024)
            with self.assertRaises(ValueError):self.run_fetch(root,mutate=excessive)
            self.assertFalse((root/'download-receipt.json').exists())

    def test_later_download_cannot_tamper_with_prior_verified_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fresh'
            def tamper(url,sink,objects):
                if url.endswith('npm-bun-darwin-arm64.tgz'):(root/'standalone-rust-darwin-arm64.tgz').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError,'retained'):self.run_fetch(root,mutate=tamper)
            self.assertFalse((root/'download-receipt.json').exists())

    def test_precreated_symlink_cannot_redirect_artifact_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fresh';victim=Path(tmp).resolve()/'victim';victim.write_bytes(b'keep')
            def inject(url,sink,objects):
                if url.endswith('manifest.json'):(root/'standalone-rust-darwin-arm64.tgz').symlink_to(victim)
            with self.assertRaises(FileExistsError):self.run_fetch(root,mutate=inject)
            self.assertEqual(b'keep',victim.read_bytes())

    def test_curl_is_fixed_https_without_redirects_or_ambient_credentials(self):
        class Process:
            def __init__(self):
                self.stdout=io.BytesIO(b'bytes');self.returncode=0
            def wait(self,timeout):return self.returncode
            def poll(self):return self.returncode
        stream=io.BytesIO()
        with patch.object(fetcher.subprocess,'Popen',return_value=Process()) as popen:
            fetcher.curl_download(fetcher.BASE_URL+'manifest.json',fetcher.BoundedWriter(stream,5),5)
        args=popen.call_args.args[0];options=popen.call_args.kwargs
        self.assertEqual('/usr/bin/curl',args[0]);self.assertEqual('--disable',args[1])
        self.assertIn('--max-time',args);self.assertIn('--max-filesize',args)
        self.assertEqual('=https',args[args.index('--proto')+1])
        self.assertNotIn('--location',args);self.assertNotIn('-L',args)
        self.assertEqual({'PATH':'/usr/bin:/bin','LC_ALL':'C'},options['env'])
        self.assertEqual(fetcher.subprocess.DEVNULL,options['stderr'])
        self.assertTrue(options['start_new_session'])
        self.assertEqual(b'bytes',stream.getvalue())

    def test_cli_download_errors_do_not_leak_provider_or_local_diagnostics(self):
        with patch.object(fetcher,'fetch',side_effect=ValueError('secret-body /private/path')),patch('sys.stderr',new_callable=io.StringIO) as err:
            self.assertEqual(1,fetcher.main(['--platform','darwin-arm64','--out','fresh']))
            self.assertNotIn('secret-body',err.getvalue());self.assertNotIn('/private/path',err.getvalue())

if __name__=='__main__':unittest.main()
