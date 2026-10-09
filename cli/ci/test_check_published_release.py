import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import check_published_release as subject


class PublishedReleaseTests(unittest.TestCase):
    def check_fixture(self, mutation=None, use_node=False, failed_args=None, failed_code=21, failed_stderr=b''):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'prose'
            binary.write_bytes(b'fixture executable')
            binary.chmod(0o700)
            node = Path(directory) / 'node' if use_node else None
            if node:
                node.write_bytes(b'fixture node interpreter')
                node.chmod(0o700)
            contract = json.loads(subject.CONTRACT.read_text())
            calls = []
            def execute(argv, cwd, env):
                self.assertNotIn('OPENAI_API_KEY', env)
                self.assertEqual(directory, str(binary.parent))
                if node:
                    self.assertEqual(str(node.resolve()), argv[0])
                args = argv[2:] if node else argv[1:]
                calls.append(args)
                if args == failed_args:
                    return subprocess.CompletedProcess(argv, failed_code, b'private output body', failed_stderr)
                if args == ['--version']:
                    return subprocess.CompletedProcess(argv, 0, b'prose 0.15.0-rc.1 (bun)\n', b'')
                if args[-1] == 'doctor':
                    value = {**contract['doctor'], 'runner': {'name': 'bun', 'version': '0.15.0-rc.1', 'commit': 'a' * 40}}
                    if mutation: mutation(value)
                    code = 10
                elif args[-1] == 'list':
                    value, code = {'harnesses': [{'id': 'mock', **contract['mock']}]}, 0
                else:
                    value, code = {'error': {'code': 'HARNESS_UNAVAILABLE'}}, 10
                return subprocess.CompletedProcess(argv, code, json.dumps(value).encode(), b'')
            report = subject.check(binary, 'bun', 'a' * 40, '0.15.0-rc.1', execute, node=node)
            self.assertEqual(4, len(calls))
            self.assertEqual('published-on-run', report['imageSource'])
            return report

    def test_published_release_offline_contract(self):
        self.assertEqual(0, self.check_fixture()['modelCalls'])

    def test_explicit_node_is_used_and_bound_for_npm_launcher(self):
        self.assertEqual(64, len(self.check_fixture(use_node=True)['nodeInterpreterSha256']))

    def test_fixed_image_cannot_claim_published_startup(self):
        with self.assertRaises(ValueError):
            self.check_fixture(lambda value: value.pop('imageSource'))

    def test_development_or_test_seams_fail_closed(self):
        for build in [{'profile': 'development', 'testSeamsEnabled': False}, {'profile': 'release', 'testSeamsEnabled': True}, {'profile': 'release', 'testSeamsEnabled': 0}]:
            with self.assertRaises(ValueError):
                self.check_fixture(lambda value: value.update(build=build))

    def test_source_mismatch_fails_closed(self):
        with self.assertRaises(ValueError):
            self.check_fixture(lambda value: value['runner'].update(commit='b' * 40))

    def test_each_failed_offline_probe_has_safe_measured_identity(self):
        probes = [
            ('version', ['--version']),
            ('doctor', ['--output=json', 'cli', 'doctor']),
            ('harness-list', ['--output=json', 'cli', 'harness', 'list']),
            ('mock-run', ['--harness=mock', '--output=json', 'run', 'hello']),
        ]
        for name, args in probes:
            for use_node in (False, True):
                with self.subTest(probe=name, node=use_node):
                    with self.assertRaises(ValueError) as error:
                        self.check_fixture(use_node=use_node, failed_args=args)
                    self.assertEqual(f'offline release probe {name} failed its exit/stderr contract '
                                     '(exit 21; stderr present: False)', str(error.exception))

    def test_expected_exit_with_stderr_still_fails_without_disclosing_bodies(self):
        with self.assertRaises(ValueError) as error:
            self.check_fixture(failed_args=['--output=json', 'cli', 'doctor'], failed_code=10,
                               failed_stderr=b'private stderr credential body')
        self.assertEqual('offline release probe doctor failed its exit/stderr contract '
                         '(exit 10; stderr present: True)', str(error.exception))
