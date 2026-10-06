"""Provider-free controls for the explicit native observation, not support claims."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

PATH = Path(__file__).with_name("codex_compatibility.py")
SPEC = importlib.util.spec_from_file_location("codex_compatibility", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def outcome(**kwargs):
    values = dict(stdout=b"", stderr=b"", exit_code=0, timed_out=False,
                  settled=True, stdout_truncated=False, stderr_truncated=False)
    values.update(kwargs)
    return SimpleNamespace(**values)


class NativeObservationTests(unittest.TestCase):
    def test_descriptions_and_prefixes_do_not_establish_capabilities(self):
        outcomes = [outcome(stdout=b"codex-cli 0.999.0-alpha.1\n"), outcome(stdout=b"Description mentions --json\n  --json-future\n -s, --sandbox read-only\n", stderr=b"")]
        with patch.object(MODULE, "run_owned_process", side_effect=outcomes) as execute:
            result = MODULE.observe(PATH)
        self.assertIn("--json", result["missingOptions"])
        self.assertNotIn("--sandbox", result["missingOptions"])
        self.assertEqual(result["qualification"], "unqualified")
        self.assertEqual(result["providerCallsAttempted"], 0)
        for call in execute.call_args_list:
            self.assertNotIn("HOME", call.kwargs["environment"])
            self.assertNotIn("OPENAI_API_KEY", call.kwargs["environment"])
            self.assertEqual(call.kwargs["timeout_seconds"], 5)

    def test_invalid_identity_refuses_before_capability_observation(self):
        with patch.object(MODULE, "run_owned_process", return_value=outcome(stdout=b"codex-cli broken")) as execute:
            with self.assertRaises(ValueError):
                MODULE.observe(PATH)
        self.assertEqual(execute.call_count, 1)

    def test_failed_unsettled_and_truncated_probes_are_rejected(self):
        for values in ({"exit_code": 2}, {"timed_out": True}, {"settled": False},
                       {"stdout_truncated": True}, {"stderr_truncated": True}):
            with self.subTest(values=values), patch.object(MODULE, "run_owned_process", return_value=outcome(**values)):
                with self.assertRaises(ValueError):
                    MODULE.observe(PATH)

    def test_real_capture_limit_and_process_group_cleanup(self):
        import sys
        import os
        result = MODULE.run_owned_process([sys.executable, "-c", "print('x' * 65536)"],
                                          cwd=PATH.parent, environment={}, timeout_seconds=5,
                                          max_capture_bytes=32768)
        self.assertTrue(result.stdout_truncated)
        self.assertLessEqual(len(result.stdout), 32768)
        self.assertTrue(result.settled)
        if os.name == "posix":
            result = MODULE.run_owned_process([sys.executable, "-c", "import os,time,sys; os.fork(); sys.stderr.write('x' * 65536); sys.stderr.flush(); time.sleep(10)"],
                                              cwd=PATH.parent, environment={}, timeout_seconds=0.1,
                                              max_capture_bytes=32768)
            self.assertTrue(result.timed_out)
            self.assertTrue(result.settled)
            self.assertLessEqual(len(result.stderr), 32768)


if __name__ == "__main__":
    unittest.main()
