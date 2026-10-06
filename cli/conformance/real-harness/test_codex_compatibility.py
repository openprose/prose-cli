"""Provider-free controls for the explicit native observation, not support claims."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import json
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
            # Reap the controlled child ourselves: orphan/zombie reaping by init
            # is host-dependent and must not be mistaken for a capture failure.
            script = """
import json, os, signal, sys, time
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    time.sleep(60)
    os._exit(0)
def terminate(signum, frame):
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(child, 0)
    sys.exit(0)
signal.signal(signal.SIGTERM, terminate)
with open(sys.argv[1], 'w') as output:
    json.dump({'parent': os.getpid(), 'child': child}, output)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
sys.stderr.write('x' * 65536)
sys.stderr.flush()
time.sleep(60)
"""
            with tempfile.TemporaryDirectory() as temporary:
                identities = Path(temporary) / "identities.json"
                result = MODULE.run_owned_process(
                    [sys.executable, "-c", script, str(identities)],
                    cwd=PATH.parent, environment={}, timeout_seconds=2,
                    max_capture_bytes=32768)
                self.assertTrue(result.timed_out)
                self.assertTrue(result.settled)
                self.assertTrue(result.stderr_truncated)
                self.assertLessEqual(len(result.stderr), 32768)
                self.assertTrue(identities.is_file())
                for pid in json.loads(identities.read_text()).values():
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
