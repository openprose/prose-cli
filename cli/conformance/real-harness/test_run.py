#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("openprose_real_harness", HERE / "run.py")
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class RealHarnessRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = runner.read_json(HERE / "policy.v1.json")
        self.matrix = runner.read_json(HERE / "matrix.v1.json")
        self.fake = HERE / "fake_prime_agent.py"

    def test_frozen_policy_and_matrix_are_non_admitting_and_budgeted(self) -> None:
        runner.validate_frozen_inputs(self.policy, self.matrix)
        self.assertEqual(
            1.0,
            sum(route["plannedCostCeilingUsd"] for route in self.matrix["routes"]),
        )
        self.assertFalse(self.matrix["claims"]["strictWrapperAdmission"])
        self.assertEqual("unknown", self.matrix["claims"]["semanticConformance"])
        self.assertFalse(self.matrix["claims"]["releaseEligible"])

    def test_run_parser_has_no_machine_specific_live_input_defaults(self) -> None:
        run = next(
            action
            for action in runner.parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["run"]
        actions = {action.dest: action for action in run._actions}
        self.assertTrue(actions["executable"].required)
        self.assertIsNone(actions["executable"].default)
        self.assertIsNone(actions["env_file"].default)
        source = (HERE / "run.py").read_text("utf-8")
        self.assertNotIn("/" + "Users/sl/", source)

    def test_openrouter_run_refuses_ambient_credentials_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".env").write_text(
                "OPENROUTER_API_KEY=ambient-file-secret\n", "utf-8"
            )
            args = argparse.Namespace(
                i_understand_this_spends_money=True,
                accept_research_only_incompatible_version=True,
                policy=HERE / "policy.v1.json",
                matrix=HERE / "matrix.v1.json",
                executable=self.fake.resolve(),
                env_file=None,
                output_dir=root / "evidence",
                route=["openrouter-qwen"],
            )
            with mock.patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "ambient-process-secret"},
            ), mock.patch.object(
                runner,
                "observe_version",
                side_effect=AssertionError("must fail before a process starts"),
            ):
                with self.assertRaisesRegex(
                    runner.ConfigurationError, "explicit --env-file"
                ):
                    runner.command_run(args)
            self.assertFalse(args.output_dir.exists())
        with self.assertRaisesRegex(runner.ConfigurationError, "absolute"):
            runner.parse_dotenv_selected(Path(".env"), ["OPENROUTER_API_KEY"])

    def test_prime_hosted_run_uses_explicit_executable_without_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "evidence"
            args = argparse.Namespace(
                i_understand_this_spends_money=True,
                accept_research_only_incompatible_version=True,
                policy=HERE / "policy.v1.json",
                matrix=HERE / "matrix.v1.json",
                executable=self.fake.resolve(),
                env_file=None,
                output_dir=output,
                route=["prime-openai"],
            )
            self.assertEqual(0, runner.command_run(args))
            evidence = runner.read_json(output / "prime-openai.evidence.json")
        serialized = json.dumps(evidence)
        self.assertNotIn(str(self.fake.resolve()), serialized)
        self.assertNotIn("/" + "Users/sl/", serialized)

    def test_budget_overflow_is_rejected_before_a_process_can_run(self) -> None:
        matrix = deepcopy(self.matrix)
        matrix["routes"][0]["plannedCostCeilingUsd"] = 0.251
        with self.assertRaisesRegex(runner.ConfigurationError, "exceed"):
            runner.validate_frozen_inputs(self.policy, matrix)

    def test_provider_free_canary_uses_disposable_cwd_and_every_isolation_flag(
        self,
    ) -> None:
        route = deepcopy(self.matrix["routes"][0])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            observation_path = root / "fake-observation.json"
            env_file = root / ".env"
            env_file.write_text("UNRELATED_SECRET=must-not-pass\n", "utf-8")
            evidence = runner.run_route(
                executable=self.fake,
                executable_sha256=runner.executable_digest(self.fake),
                observed_version="0.7.0",
                policy=self.policy,
                matrix=self.matrix,
                route=route,
                env_file=env_file,
                extra_environment={
                    "OPENPROSE_REAL_HARNESS_TEST_OBSERVATION": str(observation_path)
                },
            )
            observed = json.loads(observation_path.read_text("utf-8"))
        argv = observed["argv"]
        for flag in (
            "--no-session",
            "--no-tools",
            "--no-builtin-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ):
            self.assertIn(flag, argv)
        self.assertIn("--provider", argv)
        self.assertIn("--model", argv)
        self.assertEqual("[REDACTED_PROMPT]", argv[-1])
        self.assertFalse(Path(observed["cwd"]).exists())
        self.assertEqual("route-canary-pass", evidence["observation"]["classification"])
        self.assertEqual(1, evidence["observation"]["assistantTerminalAttempts"])
        self.assertTrue(evidence["observation"]["frozenRetryPolicySatisfied"])
        self.assertAlmostEqual(0.0001, evidence["observation"]["cost"]["totalUsd"])
        self.assertTrue(evidence["claims"]["exploratoryRouteCanary"])
        self.assertFalse(evidence["claims"]["baseTransportObservation"])
        self.assertFalse(evidence["claims"]["strictWrapperAdmission"])
        self.assertEqual("unknown", evidence["claims"]["semanticConformance"])
        self.assertNotIn("must-not-pass", json.dumps(evidence))

    def test_failure_is_sanitized_and_retained_as_evidence(self) -> None:
        route = deepcopy(self.matrix["routes"][0])
        policy = deepcopy(self.policy)
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            env_file.write_text("IGNORED=x\n", "utf-8")
            evidence = runner.run_route(
                executable=self.fake,
                executable_sha256=runner.executable_digest(self.fake),
                observed_version="0.7.0",
                policy=policy,
                matrix=self.matrix,
                route=route,
                env_file=env_file,
                extra_environment={"FAKE_PRIME_MODE": "fail"},
            )
        self.assertEqual("route-unavailable", evidence["observation"]["classification"])
        self.assertIn("[REDACTED_SECRET]", evidence["process"]["sanitizedStderr"])
        self.assertNotIn("super-secret-value", json.dumps(evidence))
        runner.validate_evidence(evidence)

    def test_timeout_is_hard_and_classified(self) -> None:
        route = deepcopy(self.matrix["routes"][0])
        policy = deepcopy(self.policy)
        policy["limits"]["timeoutSecondsPerTrial"] = 1
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            env_file.write_text("IGNORED=x\n", "utf-8")
            evidence = runner.run_route(
                executable=self.fake,
                executable_sha256=runner.executable_digest(self.fake),
                observed_version="0.7.0",
                policy=policy,
                matrix=self.matrix,
                route=route,
                env_file=env_file,
                extra_environment={"FAKE_PRIME_MODE": "timeout"},
            )
        self.assertEqual("timeout", evidence["observation"]["classification"])
        self.assertTrue(evidence["process"]["timedOut"])
        self.assertIsNone(evidence["process"]["exitCode"])
        self.assertLess(evidence["process"]["durationMs"], 5000)
        self.assertTrue(evidence["process"]["settlement"]["leaderReaped"])
        self.assertTrue(evidence["process"]["settlement"]["streamsClosed"])
        self.assertFalse(
            evidence["process"]["settlement"]["detachedDescendantsContained"]
        )

    @unittest.skipIf(os.name == "nt", "POSIX process-group oracle")
    def test_dual_stream_overflow_is_bounded_and_settles_original_group(self) -> None:
        program = textwrap.dedent(
            """
            import os
            from pathlib import Path
            import sys
            import time

            child = os.fork()
            if child == 0:
                while True:
                    time.sleep(1)
            Path(sys.argv[1]).write_text(
                f"{os.getpid()} {child} {os.getpgrp()}", encoding="utf-8"
            )
            block = b"x" * 4096
            while True:
                os.write(1, block)
                os.write(2, block)
            """
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            identities = root / "identities"
            observation = runner.run_bounded(
                [sys.executable, "-c", program, str(identities)],
                root,
                runner.isolated_environment({}),
                5,
                maximum_stream_bytes=1024,
            )
            published = [int(value) for value in identities.read_text().split()]
        self.assertTrue(observation.output_limit_exceeded)
        self.assertLessEqual(len(observation.stdout), 1024)
        self.assertLessEqual(len(observation.stderr), 1024)
        self.assertGreater(
            observation.stdout_bytes_observed + observation.stderr_bytes_observed,
            1024,
        )
        self.assertGreater(observation.stdout_bytes_observed, 0)
        self.assertGreater(observation.stderr_bytes_observed, 0)
        self.assertTrue(observation.leader_reaped)
        self.assertTrue(observation.streams_closed)
        self.assertTrue(observation.original_process_group_empty)
        self.assertFalse(observation.detached_descendants_contained)
        self.assertTrue(self._wait_for_group_absent(published[2]))

    @unittest.skipIf(os.name == "nt", "POSIX process-group oracle")
    def test_timeout_settles_child_and_grandchild_in_original_group(self) -> None:
        program = textwrap.dedent(
            """
            import os
            from pathlib import Path
            import sys
            import time

            child = os.fork()
            if child == 0:
                while True:
                    time.sleep(1)
            Path(sys.argv[1]).write_text(
                f"{os.getpid()} {child} {os.getpgrp()}", encoding="utf-8"
            )
            while True:
                time.sleep(1)
            """
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            identities = root / "identities"
            observation = runner.run_bounded(
                [sys.executable, "-c", program, str(identities)],
                root,
                runner.isolated_environment({}),
                1,
                maximum_stream_bytes=1024,
            )
            published = [int(value) for value in identities.read_text().split()]
        self.assertTrue(observation.timed_out)
        self.assertTrue(observation.original_process_group_empty)
        self.assertTrue(self._wait_for_group_absent(published[2]))

    @unittest.skipIf(os.name == "nt", "POSIX process-group oracle")
    def test_base_exception_still_settles_validated_process_group(self) -> None:
        program = textwrap.dedent(
            """
            import os
            from pathlib import Path
            import sys
            import time

            child = os.fork()
            if child == 0:
                while True:
                    time.sleep(1)
            Path(sys.argv[1]).write_text(
                f"{os.getpid()} {child} {os.getpgrp()}", encoding="utf-8"
            )
            while True:
                time.sleep(1)
            """
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            identities = root / "identities"

            def interrupt_after_publish(process: object, timeout: float) -> int:
                del process, timeout
                deadline = time.monotonic() + 3
                while not identities.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not identities.exists():
                    self.fail("fixture did not publish process identities")
                raise KeyboardInterrupt

            with mock.patch.object(
                runner, "wait_for_process", side_effect=interrupt_after_publish
            ):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_bounded(
                        [sys.executable, "-c", program, str(identities)],
                        root,
                        runner.isolated_environment({}),
                        5,
                        maximum_stream_bytes=1024,
                    )
            published = [int(value) for value in identities.read_text().split()]
            self.assertTrue(self._wait_for_group_absent(published[2]))

    def test_output_limit_is_explicit_sanitized_non_admitting_evidence(self) -> None:
        route = deepcopy(self.matrix["routes"][0])
        policy = deepcopy(self.policy)
        policy["limits"]["maximumPersistedStreamBytes"] = 128
        observation = runner.ProcessObservation(
            exit_code=-9,
            timed_out=False,
            output_limit_exceeded=True,
            duration_ms=3,
            stdout=(b"api_key=super-secret-value " + b"x" * 104)[:128],
            stderr=(b"Bearer secret-token-value " + b"y" * 102)[:128],
            stdout_bytes_observed=4096,
            stderr_bytes_observed=4096,
            stopped_after_first_assistant_failure=False,
            leader_reaped=True,
            streams_closed=True,
            original_process_group_empty=True,
            detached_descendants_contained=False,
        )
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            env_file.write_text("IGNORED=x\n", "utf-8")
            with mock.patch.object(runner, "run_bounded", return_value=observation):
                evidence = runner.run_route(
                    executable=self.fake,
                    executable_sha256=runner.executable_digest(self.fake),
                    observed_version="0.7.0",
                    policy=policy,
                    matrix=self.matrix,
                    route=route,
                    env_file=env_file,
                )
        process = evidence["process"]
        self.assertEqual("output-limit", evidence["observation"]["classification"])
        self.assertEqual(
            {
                "limitBytesPerStream": 128,
                "enforcedLive": True,
                "outputLimitExceeded": True,
                "stdoutBytesObserved": 4096,
                "stderrBytesObserved": 4096,
            },
            process["capture"],
        )
        self.assertNotIn("super-secret-value", json.dumps(evidence))
        self.assertNotIn("secret-token-value", json.dumps(evidence))
        self.assertLessEqual(len(process["sanitizedStdout"].encode()), 128)
        self.assertLessEqual(len(process["sanitizedStderr"].encode()), 128)
        self.assertFalse(process["settlement"]["strictContainmentClaimed"])
        runner.validate_evidence(evidence)

    def test_retained_pre_enforcement_evidence_migrates_honestly(self) -> None:
        path = HERE / "evidence/current/openrouter-deepseek.evidence.json"
        retained = runner.enrich_retained_evidence(
            runner.read_json(path), self.policy, self.matrix
        )
        self.assertEqual(
            {
                "limitBytesPerStream": 8192,
                "enforcedLive": False,
                "outputLimitExceeded": None,
                "stdoutBytesObserved": retained["process"]["stdoutBytes"],
                "stderrBytesObserved": retained["process"]["stderrBytes"],
            },
            retained["process"]["capture"],
        )
        self.assertIsNone(
            retained["process"]["settlement"]["originalProcessGroupEmpty"]
        )
        self.assertFalse(
            retained["process"]["settlement"]["detachedDescendantsContained"]
        )
        runner.validate_evidence(retained)

    def test_evidence_rejects_strict_containment_or_inconsistent_capture(self) -> None:
        route = deepcopy(self.matrix["routes"][0])
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            env_file.write_text("IGNORED=x\n", "utf-8")
            evidence = runner.run_route(
                executable=self.fake,
                executable_sha256=runner.executable_digest(self.fake),
                observed_version="0.7.0",
                policy=self.policy,
                matrix=self.matrix,
                route=route,
                env_file=env_file,
            )
        tampered = deepcopy(evidence)
        tampered["process"]["settlement"]["strictContainmentClaimed"] = True
        with self.assertRaisesRegex(runner.ConfigurationError, "strict containment"):
            runner.validate_evidence(tampered)
        tampered = deepcopy(evidence)
        tampered["process"]["capture"]["stdoutBytesObserved"] = 0
        with self.assertRaisesRegex(runner.ConfigurationError, "observed byte"):
            runner.validate_evidence(tampered)

    def test_only_route_declared_credentials_enter_child_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            env_file.write_text(
                "OPENROUTER_API_KEY='wanted-secret'\n"
                "OPENAI_API_KEY=unrelated-secret\n"
                "ANTHROPIC_API_KEY=also-unrelated\n",
                "utf-8",
            )
            selected = runner.parse_dotenv_selected(env_file, ["OPENROUTER_API_KEY"])
        self.assertEqual({"OPENROUTER_API_KEY": "wanted-secret"}, selected)
        environment = runner.isolated_environment(selected)
        self.assertEqual("wanted-secret", environment["OPENROUTER_API_KEY"])
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_prompt_and_secrets_are_removed_before_bounding(self) -> None:
        prompt = runner.build_prompt("OPCANARY_TEST_123")
        executable = Path("/private/operator/bin/prime-agent")
        env_file = Path("/private/operator/credentials.env")
        raw = (
            prompt
            + runner.SYSTEM_INSTRUCTION
            + " Bearer secret-token-value "
            + str(executable)
            + str(env_file)
            + "x" * 200
        ).encode()
        sanitized = runner.bounded_sanitize(
            raw,
            prompt=prompt,
            secrets=["secret-token-value"],
            maximum_bytes=100,
            workspace=Path("/tmp/disposable"),
            private_paths=(executable, env_file),
        )
        self.assertNotIn(prompt, sanitized)
        self.assertNotIn(runner.SYSTEM_INSTRUCTION, sanitized)
        self.assertNotIn("secret-token-value", sanitized)
        self.assertNotIn("/private/operator", sanitized)
        self.assertIn("TRUNCATED", sanitized)
        self.assertLessEqual(len(sanitized.encode()), 100)

    def test_report_generation_is_deterministic_and_non_admitting(self) -> None:
        sample = {
            "schema": runner.SCHEMA,
            "matrixId": "m",
            "matrixSha256": "4" * 64,
            "policyId": "p",
            "policySha256": "5" * 64,
            "route": {"id": "z", "provider": "p", "model": "m", "trial": 1},
            "harness": {
                "name": "prime-agent",
                "executableSha256": "0" * 64,
                "observedVersion": "0.7.0",
                "stableRecipeVersion": "0.8.1",
                "stableRecipeCompatible": False,
            },
            "taskSha256": "1" * 64,
            "process": {
                "exitCode": 0,
                "timedOut": False,
                "stoppedAfterFirstAssistantFailure": False,
                "durationMs": 9,
                "stdoutBytes": 1,
                "stderrBytes": 0,
                "stdoutSha256": "2" * 64,
                "stderrSha256": "3" * 64,
                "sanitizedStdout": "x",
                "sanitizedStderr": "",
                "capture": {
                    "limitBytesPerStream": 8192,
                    "enforcedLive": True,
                    "outputLimitExceeded": False,
                    "stdoutBytesObserved": 1,
                    "stderrBytesObserved": 0,
                },
                "settlement": {
                    "scope": "original-posix-process-group",
                    "leaderReaped": True,
                    "streamsClosed": True,
                    "originalProcessGroupEmpty": True,
                    "detachedDescendantsContained": False,
                    "strictContainmentClaimed": False,
                },
            },
            "observation": {
                "classification": "route-canary-pass",
                "jsonEventsParsed": 1,
                "assistantExactMatch": True,
                "assistantText": "x",
                "usage": None,
                "cost": None,
                "assistantTerminalAttempts": 1,
                "harnessInternalRetryObserved": False,
                "frozenRetryPolicySatisfied": True,
            },
            "claims": {
                "exploratoryRouteCanary": True,
                "baseTransportObservation": False,
                "strictWrapperAdmission": False,
                "semanticConformance": "unknown",
                "proseComplete": "unknown",
                "releaseEligible": False,
            },
        }
        runner.validate_evidence(sample)
        first = runner.report_value([sample])
        second = runner.report_value([deepcopy(sample)])
        self.assertEqual(runner.canonical_json(first), runner.canonical_json(second))
        markdown = runner.report_markdown(first)
        self.assertIn("Semantic status: **unknown**", markdown)
        self.assertIn("Release eligible: **no**", markdown)

        tampered = deepcopy(sample)
        tampered["claims"]["baseTransportObservation"] = True
        with self.assertRaisesRegex(runner.ConfigurationError, "adapter transport"):
            runner.validate_evidence(tampered)

    def test_real_run_requires_both_explicit_acknowledgements(self) -> None:
        args = argparse.Namespace(i_understand_this_spends_money=False)
        with self.assertRaisesRegex(runner.ConfigurationError, "opt-in"):
            runner.command_run(args)

    def test_harness_internal_retry_is_stopped_after_first_failure(self) -> None:
        route = deepcopy(self.matrix["routes"][0])
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            env_file.write_text("IGNORED=x\n", "utf-8")
            evidence = runner.run_route(
                executable=self.fake,
                executable_sha256=runner.executable_digest(self.fake),
                observed_version="0.7.0",
                policy=self.policy,
                matrix=self.matrix,
                route=route,
                env_file=env_file,
                extra_environment={"FAKE_PRIME_MODE": "internal-retry"},
            )
        self.assertTrue(evidence["process"]["stoppedAfterFirstAssistantFailure"])
        self.assertEqual(1, evidence["observation"]["assistantTerminalAttempts"])
        self.assertTrue(evidence["observation"]["frozenRetryPolicySatisfied"])
        self.assertEqual("route-unavailable", evidence["observation"]["classification"])

    @staticmethod
    def _wait_for_group_absent(process_group: int) -> bool:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.02)
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
