import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from distribution_plan import create_plan


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = 'https://github.com/openprose/example-evidence/tree/'+'b'*40+'/case'
        self.manifest = {'schema':'openprose.local-release-manifest/1','version':'0.15.0-dev.1','source':{'revision':'a'*40},'platform':'darwin-arm64','artifacts':[]}
        for impl in ['bun','rust']:
            data = impl.encode()
            name = impl + '.tar.gz'
            (self.root/name).write_bytes(data)
            self.manifest['artifacts'].append({'path':name,'kind':'standalone-archive','implementation':impl,'platform':'darwin-arm64','byteLength':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        for name in ['sbom.cdx.json','provenance.json','dependency-evidence.json','SHA256SUMS']:
            (self.root/name).write_text('fixture evidence\n')
        self.save()

    def save(self):
        (self.root/'release-manifest.json').write_text(json.dumps(self.manifest))

    def test_bound_bytes_and_no_automatic_qualification(self):
        result = create_plan(self.root,self.evidence)
        self.assertEqual(result['qualification']['status'],'development')
        self.assertEqual(len(result['artifacts']),7)
        for item in result['artifacts']:
            self.assertEqual(item['sha256'],hashlib.sha256((self.root/item['name']).read_bytes()).hexdigest())

    def test_corrupt_missing_and_symlink(self):
        path=self.root/'bun.tar.gz'
        path.write_text('corrupt')
        with self.assertRaises(ValueError): create_plan(self.root,self.evidence)
        path.unlink()
        with self.assertRaises(ValueError): create_plan(self.root,self.evidence)
        path.symlink_to(self.root/'rust.tar.gz')
        with self.assertRaises(ValueError): create_plan(self.root,self.evidence)

    def test_rejects_renaming_alpha_and_missing_implementation(self):
        self.manifest['version']='0.15.0-alpha.1'; self.save()
        with self.assertRaises(ValueError): create_plan(self.root,self.evidence)
        self.manifest['version']='0.15.0-rc.1'; self.manifest['artifacts'].pop(); self.save()
        with self.assertRaises(ValueError): create_plan(self.root,self.evidence)

    def test_shared_contract_fixture(self):
        path=Path(__file__).resolve().parents[1]/'shared/fixtures/distribution/plan.json'
        fixture=json.loads(path.read_text())
        self.assertEqual(set(create_plan(self.root,self.evidence)),set(fixture))


if __name__=='__main__': unittest.main()
