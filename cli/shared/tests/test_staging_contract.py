"""Validate shared staging examples independently of either product."""
import json
from pathlib import Path
import unittest
import test_contracts

CLI = Path(__file__).resolve().parents[2]


class StagingContractTest(unittest.TestCase):
    setUpClass = classmethod(test_contracts.ContractsTest.setUpClass.__func__)
    validator = test_contracts.ContractsTest.validator
    assert_valid = test_contracts.ContractsTest.assert_valid
    def test_staging_corpus(self):
        corpus = json.loads((CLI/'conformance/runner/staging-service-corpus.json').read_text())
        taxonomy = {e['code']: e for e in json.loads((CLI/'shared/errors/taxonomy.v1.json').read_text())['errors']}
        for case in corpus['cases']:
            result = case['resultMatches'].copy()
            if result['problem']:
                result['problem'] = {'schema': 'openprose.runner-error/1', **taxonomy[result['problem']['code']]}
            schema = 'organization-list' if result['schema'] == 'openprose.organization-list/1' else 'service-account'
            self.assert_valid(schema+'.schema.json', result)
            result['api_key'] = 'must be rejected'
            self.assertTrue(list(self.validator(schema+'.schema.json').iter_errors(result)))
        self.assertEqual(len(corpus['cases']), len({c['id'] for c in corpus['cases']}))


if __name__ == '__main__':
    unittest.main()
