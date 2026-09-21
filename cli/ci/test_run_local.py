from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "run_local.py"


def load_module():
    spec = importlib.util.spec_from_file_location("openprose_run_local", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class LocalAdmissionTest(unittest.TestCase):
    def test_default_plan_covers_every_provider_free_authority_in_order(self) -> None:
        runner = load_module()
        plan = runner.plan(runner.gates(), selected=(), quick=False)
        self.assertEqual(
            [gate.name for gate in plan],
            [
                "local-runner-tests",
                "architecture-tests",
                "release-preflight-tests",
                "release-notes",
                "alpha-promotion",
                "post-public-verification",
                "release-reproducibility",
                "workflow-policy",
                "dependency-contract",
                "dependency-evidence",
                "shared-contracts",
                "image-bundle",
                "fake-harness",
                "fake-hosted-service",
                "adapter-oracle",
                "windows-resolution-oracle",
                "real-harness-contract",
                "functional-alpha-live-contract",
                "direct-skill-contract",
                "architecture-boundary",
                "windows-host-static",
                "windows-host-format",
                "windows-host-clippy",
                "windows-host-tests",
                "rust-format",
                "rust-clippy",
                "rust-tests",
                "package-lifecycle",
                "rust-build",
                "bun-typecheck",
                "bun-tests",
                "bun-build",
                "adapter-product-adversary",
                "differential-conformance",
                "staging-service-corpus",
                "staging-service-rust-build",
                "staging-service-rust",
                "staging-service-bun-build",
                "staging-service-bun",
                "service-environment-rust",
                "service-environment-bun",
                "registry-service-rust",
                "registry-service-bun",
                "conformance-host",
                "package-local",
                "alpha-package-admission",
                "installed-package-benchmark",
                "release-package-admission",
                "release-rehearsal-contract",
                "release-rehearsal-real",
                "benchmark-contract",
            ],
        )
        flattened = [part for gate in plan for part in gate.argv]
        differential = next(
            gate for gate in plan if gate.name == "differential-conformance"
        )
        self.assertEqual(differential.argv[-2:], ("--phase", "7"))
        self.assertIn("--build", differential.argv)
        for name in ("rust-clippy", "rust-tests"):
            rust_gate = next(gate for gate in plan if gate.name == name)
            self.assertEqual(
                rust_gate.argv[rust_gate.argv.index("--features") + 1],
                "prose-cli/test-seams",
            )
        self.assertIn("cli/conformance/real-harness", " ".join(flattened))
        self.assertIn("test_*.py", flattened)
        self.assertIn("direct-skill-real", " ".join(flattened))
        self.assertIn("release_preflight", " ".join(flattened))
        self.assertIn("check_workflows", " ".join(flattened))
        self.assertTrue(all(gate.provider_free for gate in plan))
        self.assertTrue(
            all(
                0 < gate.timeout_seconds <= runner.MAX_GATE_TIMEOUT_SECONDS
                for gate in plan
            )
        )
        heavy = {gate.name: gate.timeout_seconds for gate in plan if not gate.quick}
        self.assertTrue(heavy)
        self.assertTrue(
            all(
                timeout > runner.DEFAULT_GATE_TIMEOUT_SECONDS
                for timeout in heavy.values()
            )
        )

    def test_quick_and_explicit_selection_are_closed(self) -> None:
        runner = load_module()
        quick = runner.plan(runner.gates(), selected=(), quick=True)
        self.assertNotIn("benchmark-contract", [gate.name for gate in quick])
        self.assertNotIn("package-local", [gate.name for gate in quick])
        self.assertIn("alpha-package-admission", [gate.name for gate in quick])
        self.assertNotIn("package-lifecycle", [gate.name for gate in quick])
        self.assertIn("installed-package-benchmark", [gate.name for gate in quick])
        self.assertIn("release-package-admission", [gate.name for gate in quick])
        self.assertIn("release-rehearsal-contract", [gate.name for gate in quick])
        self.assertIn("alpha-promotion", [gate.name for gate in quick])
        self.assertIn("post-public-verification", [gate.name for gate in quick])
        self.assertNotIn("release-rehearsal-real", [gate.name for gate in quick])
        self.assertNotIn("windows-host-tests", [gate.name for gate in quick])
        chosen = runner.plan(
            runner.gates(), selected=("rust-tests", "shared-contracts"), quick=False
        )
        self.assertEqual(
            [gate.name for gate in chosen], ["shared-contracts", "rust-tests"]
        )
        with self.assertRaisesRegex(ValueError, "unknown gate"):
            runner.plan(runner.gates(), selected=("does-not-exist",), quick=False)

    def test_alpha_promotion_gate_is_listed_quick_provider_free_and_exact(self) -> None:
        runner = load_module()
        all_gates = runner.gates()
        gate = next(gate for gate in all_gates if gate.name == "alpha-promotion")
        self.assertEqual(
            gate.argv,
            (
                sys.executable,
                "cli/ci/test_promote_alpha_release.py",
                "-v",
            ),
        )
        self.assertEqual(gate.cwd, runner.REPOSITORY_ROOT)
        self.assertTrue(gate.provider_free)
        self.assertTrue(gate.quick)
        self.assertIn(
            "alpha-promotion",
            [gate.name for gate in runner.plan(all_gates, selected=(), quick=False)],
        )
        self.assertIn(
            "alpha-promotion",
            [gate.name for gate in runner.plan(all_gates, selected=(), quick=True)],
        )
        self.assertEqual(
            runner.plan(all_gates, selected=("alpha-promotion",), quick=False),
            (gate,),
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(runner.main(["--list"]), 0)
        self.assertEqual(stdout.getvalue().splitlines().count("alpha-promotion"), 1)

    def test_post_public_gate_is_listed_quick_provider_free_and_exact(self) -> None:
        runner = load_module()
        all_gates = runner.gates()
        gate = next(
            gate for gate in all_gates if gate.name == "post-public-verification"
        )
        self.assertEqual(
            gate.argv,
            (
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/ci",
                "-p",
                "test_*public_alpha*.py",
                "-v",
            ),
        )
        self.assertEqual(gate.cwd, runner.REPOSITORY_ROOT)
        self.assertTrue(gate.provider_free)
        self.assertTrue(gate.quick)
        self.assertIn(
            "post-public-verification",
            [gate.name for gate in runner.plan(all_gates, selected=(), quick=False)],
        )
        self.assertIn(
            "post-public-verification",
            [gate.name for gate in runner.plan(all_gates, selected=(), quick=True)],
        )
        self.assertEqual(
            runner.plan(all_gates, selected=("post-public-verification",), quick=False),
            (gate,),
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(runner.main(["--list"]), 0)
        self.assertEqual(
            stdout.getvalue().splitlines().count("post-public-verification"), 1
        )

    def test_gate_timeouts_reject_unbounded_or_ambiguous_values(self) -> None:
        runner = load_module()
        for timeout in (False, 0, -1, float("nan"), float("inf"), 3_601):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                ValueError, "gate timeout"
            ):
                runner.Gate(
                    "invalid-timeout",
                    Path.cwd(),
                    (sys.executable, "-c", "pass"),
                    timeout_seconds=timeout,
                )

    def test_child_environment_removes_credentials_and_disables_bytecode(self) -> None:
        runner = load_module()
        source = {
            "PATH": "/safe/bin",
            "HOME": "/safe/home",
            "OPENAI_API_KEY": "secret-openai",
            "ANTHROPIC_API_KEY": "secret-anthropic",
            "OPENROUTER_API_KEY": "secret-router",
            "OPENPROSE_TOKEN": "secret-prose",
            "openai_api_key": "secret-lowercase-openai",
            "OpenProse_Token": "secret-mixed-prose",
            "aws_secret_access_key": "secret-lowercase-aws",
            "node_options": "--require=/untrusted/lowercase-hook.js",
            "AWS_SECRET_ACCESS_KEY": "secret-aws",
            "RUSTFLAGS": "--cfg weaken_admission",
            "PYTHONPATH": "/untrusted/imports",
            "NODE_OPTIONS": "--require=/untrusted/hook.js",
            "UNRELATED": "kept",
        }
        sanitized = runner.provider_free_environment(source)
        self.assertEqual(sanitized["PATH"], "/safe/bin")
        self.assertEqual(sanitized["UNRELATED"], "kept")
        self.assertEqual(sanitized["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertFalse(any("secret" in value for value in sanitized.values()))
        for name in runner.SECRET_ENVIRONMENT_NAMES:
            self.assertNotIn(name, sanitized)
        for name in runner.BUILD_OVERRIDE_NAMES:
            self.assertNotIn(name, sanitized)

    def test_execution_stops_at_first_failure_and_records_no_false_success(
        self,
    ) -> None:
        runner = load_module()
        attempted: list[str] = []

        def execute(gate, _environment):
            attempted.append(gate.name)
            return 7 if gate.name == "image-bundle" else 0

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = runner.run(
                runner.plan(runner.gates(), selected=(), quick=False),
                environment={},
                execute=execute,
            )
        self.assertEqual(result, 7)
        self.assertIn("FAILED: image-bundle (exit 7)", stderr.getvalue())
        self.assertEqual(
            attempted,
            [
                "local-runner-tests",
                "architecture-tests",
                "release-preflight-tests",
                "release-notes",
                "alpha-promotion",
                "post-public-verification",
                "release-reproducibility",
                "workflow-policy",
                "dependency-contract",
                "dependency-evidence",
                "shared-contracts",
                "image-bundle",
            ],
        )

    def test_streams_progress_but_bounds_each_child_output_channel(self) -> None:
        runner = load_module()
        size = runner.MAX_CAPTURE_BYTES * 4
        program = (
            "import sys; "
            f"sys.stdout.write('stdout-start\\n' + 'o' * {size} + 'STDOUT-LATE-ERROR\\n'); sys.stdout.flush(); "
            f"sys.stderr.write('stderr-start\\n' + 'e' * {size} + 'STDERR-LATE-ERROR\\n'); sys.stderr.flush()"
        )
        gate = runner.Gate(
            "bounded-output",
            Path.cwd(),
            (sys.executable, "-c", program),
            timeout_seconds=10,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = runner.run((gate,), environment=os.environ)
        self.assertEqual(result, 0)
        self.assertIn("stdout-start", stdout.getvalue())
        self.assertIn("stderr-start", stderr.getvalue())
        self.assertIn("stdout omitted", stdout.getvalue())
        self.assertIn("stderr omitted", stderr.getvalue())
        self.assertIn("STDOUT-LATE-ERROR", stdout.getvalue())
        self.assertIn("STDERR-LATE-ERROR", stderr.getvalue())
        self.assertLess(len(stdout.getvalue()), runner.MAX_CAPTURE_BYTES + 2048)
        self.assertLess(len(stderr.getvalue()), runner.MAX_CAPTURE_BYTES + 2048)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process-group authority")
    def test_absolute_timeout_kills_and_reaps_the_owned_child_and_grandchild(
        self,
    ) -> None:
        runner = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence = root / "pids.json"
            child = (
                "import json, os, pathlib, subprocess, sys, time; "
                "grandchild=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({'child':os.getpid(),'grandchild':grandchild.pid})); "
                "time.sleep(60)"
            )
            gate = runner.Gate(
                "timeout-tree",
                root,
                (sys.executable, "-c", child, str(evidence)),
                timeout_seconds=0.5,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = runner.run((gate,), environment=os.environ)
            self.assertEqual(result, runner.TIMEOUT_EXIT_CODE)
            self.assertIn("absolute timeout", stderr.getvalue())
            pids = json.loads(evidence.read_text())
            self.assertTrue(
                wait_until(
                    lambda: not pid_exists(pids["child"])
                    and not pid_exists(pids["grandchild"])
                ),
                pids,
            )

    @unittest.skipUnless(os.name == "posix", "requires POSIX process-group authority")
    def test_nominal_success_with_a_live_descendant_fails_and_cleans_up(self) -> None:
        runner = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence = root / "pid.txt"
            child = (
                "import pathlib, subprocess, sys; "
                "grandchild=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "pathlib.Path(sys.argv[1]).write_text(str(grandchild.pid))"
            )
            gate = runner.Gate(
                "leaked-descendant",
                root,
                (sys.executable, "-c", child, str(evidence)),
                timeout_seconds=10,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = runner.run((gate,), environment=os.environ)
            self.assertEqual(result, runner.UNSETTLED_EXIT_CODE)
            self.assertIn("did not settle", stderr.getvalue())
            grandchild = int(evidence.read_text())
            self.assertTrue(wait_until(lambda: not pid_exists(grandchild)), grandchild)

    @unittest.skipUnless(
        os.name == "posix", "requires POSIX signal and process-group authority"
    )
    def test_interrupt_signals_clean_the_owned_tree_before_returning(self) -> None:
        for delivered_signal, expected_exit in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(
                signal=delivered_signal
            ), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                evidence = root / "pids.json"
                child = (
                    "import json, os, pathlib, subprocess, sys, time; "
                    "grandchild=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                    "pathlib.Path(sys.argv[1]).write_text(json.dumps({'child':os.getpid(),'grandchild':grandchild.pid})); "
                    "time.sleep(60)"
                )
                driver = (
                    "import importlib.util, os, pathlib, sys; "
                    f"path=pathlib.Path({str(MODULE_PATH)!r}); "
                    "spec=importlib.util.spec_from_file_location('interrupt_run_local', path); "
                    "module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module); "
                    f"gate=module.Gate('interrupt-tree', pathlib.Path({str(root)!r}), "
                    f"(sys.executable, '-c', {child!r}, {str(evidence)!r}), timeout_seconds=30); "
                    "raise SystemExit(module.run((gate,), environment=os.environ))"
                )
                process = subprocess.Popen(
                    [sys.executable, "-c", driver],
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(lambda: process.poll() is None and process.kill())
                self.assertTrue(
                    wait_until(evidence.exists), "child did not publish identities"
                )
                process.send_signal(delivered_signal)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, expected_exit, (stdout, stderr))
                self.assertIn("INTERRUPTED: interrupt-tree", stderr)
                pids = json.loads(evidence.read_text())
                self.assertTrue(
                    wait_until(
                        lambda: not pid_exists(pids["child"])
                        and not pid_exists(pids["grandchild"])
                    ),
                    pids,
                )

    def test_windows_forced_cleanup_cannot_claim_descendant_settlement(self) -> None:
        runner = load_module()
        self.assertFalse(runner.descendant_authority("nt"))
        failure = runner.forced_exit_failure(
            gate_name="fixture",
            reason="absolute timeout",
            platform_name="nt",
            direct_child_reaped=True,
            process_group_gone=True,
        )
        self.assertEqual(failure.exit_code, runner.UNSETTLED_EXIT_CODE)
        self.assertIn("cannot guarantee descendant cleanup", str(failure))

    def test_windows_refuses_before_spawning_without_job_object_authority(self) -> None:
        runner = load_module()
        gate = runner.Gate(
            "windows-pre-spawn-refusal",
            Path.cwd(),
            (sys.executable, "-c", "raise SystemExit('must not run')"),
        )
        with mock.patch.object(runner.os, "name", "nt"), mock.patch.object(
            runner.subprocess, "Popen"
        ) as spawn, self.assertRaisesRegex(
            runner.GateExecutionFailure, "Job Object"
        ) as captured:
            runner.execute_gate(gate, {})
        self.assertEqual(captured.exception.exit_code, runner.UNSETTLED_EXIT_CODE)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
