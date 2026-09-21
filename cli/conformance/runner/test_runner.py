#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location(
    "openprose_conformance_runner", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class ContractRegistryTest(unittest.TestCase):
    def test_all_corpus_output_contracts_are_registered(self):
        contracts = runner.ContractRegistry()
        for path in runner.CASES.rglob("*.json"):
            case = json.loads(path.read_text("utf-8"))
            for stream in ("stdout", "stderr"):
                rule = case.get("expected", {}).get(stream, {})
                if "schema" in rule:
                    self.assertIn(rule["schema"], contracts.by_contract, str(path))
        # The regression must validate the actual schema, not merely recognize it.
        valid = {"schema": "openprose.service-account/1", "environment": "staging",
                 "operation": "status", "authenticated": False,
                 "credentialSource": "none", "problem": None}
        self.assertEqual([], contracts.errors(valid["schema"], valid))
        self.assertTrue(contracts.errors(valid["schema"], {**valid, "authenticated": "false"}))

    def test_discovers_new_contracts_and_fails_closed_on_unknown_or_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                      "$id": "https://example.test/future.schema.json", "type": "object",
                      "required": ["schema"],
                      "properties": {"schema": {"const": "openprose.future-output/1"}}}
            (root / "future.schema.json").write_text(json.dumps(schema))
            with patch.object(runner, "SCHEMAS", root):
                contracts = runner.ContractRegistry()
                self.assertEqual([], contracts.errors("openprose.future-output/1", {"schema": "openprose.future-output/1"}))
                self.assertTrue(contracts.errors("openprose.unknown/1", {}))
                duplicate = {**schema, "$id": "https://example.test/duplicate.schema.json"}
                (root / "duplicate.schema.json").write_text(json.dumps(duplicate))
                with self.assertRaisesRegex(ValueError, "Duplicate contract discriminator"):
                    runner.ContractRegistry()


class RunnerUnitTest(unittest.TestCase):
    def test_host_oracle_keeps_admitted_expectations_and_requires_rejection(self):
        for path in runner.case_paths(7, set()):
            case = json.loads(path.read_text("utf-8"))
            original = json.loads(json.dumps(case))
            self.assertEqual(case["expected"], runner.expected_for_host(case, "darwin", "arm64"))
            for os_name, arch in [("linux", "aarch64"), ("linux", "x86_64"), ("darwin", "x86_64")]:
                expected = runner.expected_for_host(case, os_name, arch)
                adapter = case.get("controls", {}).get("installedAdapter", {}).get("adapterId")
                rejected = adapter in {"claude/print-stream-json", "prime/rpc"} or (
                    adapter == "omp/rpc" and (os_name, arch) != ("linux", "x86_64"))
                if rejected:
                    self.assertEqual(10, expected["exitCode"])
                    self.assertFalse(expected["startedHarness"])
                    self.assertNotIn("forwardedTask", expected)
                    self.assertIn("HARNESS_INCOMPATIBLE", json.dumps(expected))
                else:
                    self.assertEqual(case["expected"], expected)
            self.assertEqual(original, case)

    def test_host_oracle_rejects_unsupported_host_without_skipping_case(self):
        case = json.loads((runner.CASES / "adapters/claude-functional-alpha.json").read_text())
        wanted = runner.expected_for_host(case, "linux", "aarch64")
        self.assertEqual("arm64", wanted["resultMatches"]["error"]["details"]["hostArchitecture"])
        self.assertTrue(runner.deep_subset({"terminal": {"classification": "success"}}, wanted["resultMatches"]))
        self.assertEqual(48, len(list(runner.case_paths(7, set()))))

    def test_hosted_transport_and_missing_selection_cases_freeze_dx_precedence(
        self,
    ) -> None:
        transport_cases = {
            "core/openprose-invalid-transport-run.json": {
                "harness": "openprose",
                "requested": "unsupported",
                "supported": ["hosted"],
            },
            "operations/openprose-invalid-transport-doctor.json": {
                "harness": "openprose",
                "requested": "unsupported",
                "supported": ["hosted"],
            },
            "core/codex-invalid-transport-run.json": {
                "harness": "codex",
                "requested": "rpc",
                "supported": ["exec-json"],
            },
            "operations/prime-invalid-transport-doctor.json": {
                "harness": "prime",
                "requested": "exec-json",
                "supported": ["rpc"],
            },
        }
        for relative, details in transport_cases.items():
            case = json.loads((runner.CASES / relative).read_text("utf-8"))
            expected = case["expected"]
            self.assertEqual(20, expected["exitCode"])
            self.assertEqual(
                "openprose.runner-error/1", expected["stdout"]["schema"]
            )
            self.assertEqual(
                details,
                expected["resultMatches"]["details"],
            )

        missing = json.loads(
            (
                runner.CASES
                / "operations/harness-use-prime-missing-bundle.json"
            ).read_text("utf-8")
        )["expected"]["resultMatches"]
        self.assertEqual("invocation", missing["boundary"])
        self.assertEqual(
            ["--model", "--auth-profile"], missing["details"]["requiredOptions"]
        )
        self.assertIn("credential route", missing["details"]["reason"])
        self.assertNotIn("billing", missing["details"]["reason"])

    def test_duplicate_output_cases_retain_the_first_machine_channel(self) -> None:
        json_case = json.loads(
            (runner.CASES / "core/duplicate-output-json-invalid.json").read_text(
                "utf-8"
            )
        )["expected"]
        self.assertEqual("json", json_case["stdout"]["kind"])
        self.assertEqual("INVOCATION_INVALID", json_case["resultMatches"]["code"])
        self.assertEqual("invocation", json_case["resultMatches"]["boundary"])

        jsonl_case = json.loads(
            (runner.CASES / "core/duplicate-output-jsonl-invalid.json").read_text(
                "utf-8"
            )
        )["expected"]
        self.assertEqual("jsonl", jsonl_case["stdout"]["kind"])
        self.assertEqual(["runner.failed"], jsonl_case["stdout"]["eventTypes"])
        self.assertEqual("INVOCATION_INVALID", jsonl_case["errorCode"])

    def test_invocation_and_configuration_cases_freeze_distinct_recovery_boundaries(
        self,
    ) -> None:
        invocation = json.loads(
            (
                runner.CASES / "operations/invocation-invalid-runner-command.json"
            ).read_text("utf-8")
        )
        configuration = json.loads(
            (
                runner.CASES / "operations/config-invalid-harness-environment.json"
            ).read_text("utf-8")
        )
        expected = invocation["expected"]
        self.assertEqual(2, expected["exitCode"])
        self.assertEqual("INVOCATION_INVALID", expected["resultMatches"]["code"])
        self.assertEqual("invocation", expected["resultMatches"]["boundary"])
        self.assertEqual(
            "Runner invocation is invalid.",
            expected["resultMatches"]["message"],
        )
        self.assertEqual(
            "Review the runner syntax with the --help option, place global options "
            "before cli, and retry the command.",
            expected["resultMatches"]["action"],
        )
        self.assertEqual(
            "configuration",
            configuration["expected"]["resultMatches"]["boundary"],
        )

    def test_installed_harness_fixture_emits_the_active_image_terminal_contract(
        self,
    ) -> None:
        spec = importlib.util.spec_from_file_location(
            "openprose_installed_harness_fixture",
            runner.INSTALLED_ADAPTER_HARNESS,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        task = {"argv": ["prose", "write", "Hello world"]}

        sentinel = json.loads(
            fixture.terminal_for_image([], b"OPENPROSE_SENTINEL_IMAGE_V1", [], task)
        )
        self.assertEqual(
            {
                "schema": "openprose.sentinel-terminal-envelope/1",
                "semanticStatus": "not-applicable",
                "marker": "OPENPROSE_SENTINEL_TERMINAL_V1",
            },
            sentinel,
        )

        echo = json.loads(
            fixture.terminal_for_image([], b"OPENPROSE_ECHO_IMAGE_V0", [], task)
        )
        self.assertEqual("openprose.echo-terminal/1", echo["schema"])
        self.assertEqual(task, echo["task"])

    def test_installed_adapter_control_installs_only_the_selected_frozen_harness(
        self,
    ) -> None:
        case = {
            "controls": {"installedAdapter": {"adapterId": "omp/rpc"}},
            "invocation": {
                "argv": ["--version"],
                "cwd": "{{WORKSPACE}}",
                "environment": {},
            },
        }
        product = runner.Product("fixture", Path(sys.executable))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment_root = root / "environment"
            workspace = root / "workspace"
            captured: dict[str, object] = {}
            original = runner.run_owned_process

            def record(argv, *, cwd, environment, timeout_seconds):
                captured.update(
                    argv=list(argv),
                    cwd=cwd,
                    environment=dict(environment),
                    timeout_seconds=timeout_seconds,
                )
                return runner.OwnedProcessResult(0, b"", b"", False, True)

            runner.run_owned_process = record
            try:
                runner.execute(product, case, environment_root, workspace)
            finally:
                runner.run_owned_process = original

            path = Path(captured["environment"]["PATH"])
            self.assertEqual(
                ["bun", "omp", "python3"],
                sorted(item.name for item in path.iterdir()),
            )
            self.assertEqual(
                runner.INSTALLED_ADAPTER_HARNESS.read_bytes(),
                (path / "omp").read_bytes(),
            )
            self.assertEqual(
                runner.BUN_RUNTIME_FIXTURE,
                (path / "bun").read_bytes(),
            )
            self.assertEqual(
                "fixture-provider-free-openrouter-key",
                captured["environment"]["OPENROUTER_API_KEY"],
            )

    def test_installed_adapter_wrong_stream_control_installs_only_the_probe_fixture(
        self,
    ) -> None:
        case = {
            "controls": {
                "installedAdapter": {
                    "adapterId": "codex/exec-json",
                    "versionProbeScenario": "wrong-stream-success",
                }
            },
            "invocation": {
                "argv": ["--version"],
                "cwd": "{{WORKSPACE}}",
                "environment": {},
            },
        }
        product = runner.Product("fixture", Path(sys.executable))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment_root = root / "environment"
            workspace = root / "workspace"
            captured: dict[str, object] = {}
            original = runner.run_owned_process

            def record(argv, *, cwd, environment, timeout_seconds):
                captured.update(
                    argv=list(argv),
                    cwd=cwd,
                    environment=dict(environment),
                    timeout_seconds=timeout_seconds,
                )
                return runner.OwnedProcessResult(0, b"", b"", False, True)

            runner.run_owned_process = record
            try:
                runner.execute(product, case, environment_root, workspace)
            finally:
                runner.run_owned_process = original

            path = Path(captured["environment"]["PATH"])
            self.assertEqual(
                ["codex", "python3"],
                sorted(item.name for item in path.iterdir()),
            )
            self.assertEqual(
                runner.WRONG_STREAM_VERSION_HARNESS,
                (path / "codex").read_bytes(),
            )
            self.assertNotEqual(
                runner.INSTALLED_ADAPTER_HARNESS.read_bytes(),
                (path / "codex").read_bytes(),
            )

    def test_installed_adapter_and_fake_process_controls_are_mutually_exclusive(
        self,
    ) -> None:
        case = {
            "controls": {
                "fakeHarness": {"scenario": "success"},
                "installedAdapter": {"adapterId": "codex/exec-json"},
            },
            "invocation": {
                "argv": ["--version"],
                "cwd": "{{WORKSPACE}}",
                "environment": {},
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "cannot select both"):
                runner.execute(
                    runner.Product("fixture", Path(sys.executable)),
                    case,
                    root / "environment",
                    root / "workspace",
                )

    def test_build_products_uses_explicit_sentinel_test_profile(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        original_run = runner.subprocess.run
        original_key = os.environ.get("OPENAI_API_KEY")
        original_override = os.environ.get("OPENPROSE_IMAGE_SOURCE_DIR")

        def record(argv, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        runner.subprocess.run = record
        os.environ["OPENAI_API_KEY"] = "must-not-enter-build"
        os.environ["OPENPROSE_IMAGE_SOURCE_DIR"] = "/hostile/ambient/image"
        try:
            runner.build_products()
        finally:
            runner.subprocess.run = original_run
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key
            if original_override is None:
                os.environ.pop("OPENPROSE_IMAGE_SOURCE_DIR", None)
            else:
                os.environ["OPENPROSE_IMAGE_SOURCE_DIR"] = original_override

        self.assertEqual(len(calls), 3)
        generator, rust, bun = calls
        self.assertIn("image_bundle.py", " ".join(generator[0]))
        self.assertEqual(rust[0][0], "cargo")
        self.assertEqual(bun[0][0], "bun")
        self.assertEqual(
            rust[0][rust[0].index("--features") + 1],
            "prose-cli/test-seams",
        )
        self.assertIn("--test-seams", bun[0])
        self.assertEqual(bun[0][bun[0].index("--image-dir") + 1], str(runner.SENTINEL))
        for _argv, options in calls:
            environment = options["env"]
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotEqual(
                environment.get("OPENPROSE_IMAGE_SOURCE_DIR"),
                "/hostile/ambient/image",
            )
        for _argv, options in (rust, bun):
            environment = options["env"]
            self.assertEqual(
                environment["OPENPROSE_IMAGE_SOURCE_DIR"], str(runner.SENTINEL)
            )
            self.assertTrue(
                environment["OPENPROSE_IMAGE_BUNDLE"].endswith("sentinel.bundle.bin")
            )

    def test_candidate_identity_rejects_oversized_bytes_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            candidate = Path(raw) / "oversized"
            with candidate.open("wb") as output:
                output.truncate(runner.MAX_CANDIDATE_BYTES + 1)
            candidate.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "candidate byte limit"):
                runner.capture_candidate_identity(
                    runner.Product("oversized", candidate, "rust")
                )

    def test_all_exact_dx_fixtures_validate_the_declared_machine_contract(self) -> None:
        contracts = runner.ContractRegistry()
        fixture_root = runner.CLI / "shared/fixtures/dx"
        for fixture in sorted(fixture_root.glob("*.json")):
            with self.subTest(fixture=fixture.name):
                value = json.loads(fixture.read_text("utf-8"))
                self.assertEqual([], contracts.errors(value["schema"], value))

    def test_exact_json_fixture_freezes_order_redaction_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            fixture = runner.CLI / "shared/fixtures/dx/dry-run-mock.json"
            expected = json.loads(fixture.read_text("utf-8"))
            actual = json.loads(
                json.dumps(expected).replace("{{WORKSPACE}}", str(workspace))
            )
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {
                    "expected": {
                        "exitCode": 0,
                        "stdout": {
                            "kind": "json",
                            "schema": "openprose.runner-dry-run-report/1",
                        },
                        "stderr": {"kind": "empty"},
                        "resultFixture": "cli/shared/fixtures/dx/dry-run-mock.json",
                    }
                },
                0,
                json.dumps(actual).encode(),
                b"",
                workspace=workspace,
            )
            self.assertEqual(
                [], runner.validate_output(observation, runner.ContractRegistry())
            )
            actual["configuration"][-1]["redacted"] = False
            observation.stdout = json.dumps(actual).encode()
            failures = runner.validate_output(observation, runner.ContractRegistry())
            self.assertTrue(
                any("exact JSON fixture" in failure for failure in failures)
            )

    def test_dry_run_blocking_error_is_the_error_validation_surface(self) -> None:
        fixture = runner.load_exact_result_fixture(
            "cli/shared/fixtures/dx/dry-run-default-hosted.json"
        )
        action = fixture["blockingError"]["action"]
        observation = runner.Observation(
            runner.Product("fixture", Path(sys.executable)),
            {
                "expected": {
                    "exitCode": 10,
                    "stdout": {
                        "kind": "json",
                        "schema": "openprose.runner-dry-run-report/1",
                    },
                    "stderr": {"kind": "empty"},
                    "errorCode": "HOSTED_UNAVAILABLE",
                    "errorAction": action,
                }
            },
            10,
            json.dumps(fixture).encode(),
            b"",
        )
        self.assertEqual(
            [], runner.validate_output(observation, runner.ContractRegistry())
        )
        fixture["blockingError"]["action"] = "wrong"
        observation.stdout = json.dumps(fixture).encode()
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertTrue(any(action in failure for failure in failures))

    def test_started_harness_expectation_is_checked_from_machine_evidence(self) -> None:
        fixture = runner.load_exact_result_fixture(
            "cli/shared/fixtures/dx/dry-run-mock.json"
        )
        observation = runner.Observation(
            runner.Product("fixture", Path(sys.executable)),
            {
                "expected": {
                    "exitCode": 0,
                    "stdout": {
                        "kind": "json",
                        "schema": "openprose.runner-dry-run-report/1",
                    },
                    "stderr": {"kind": "empty"},
                    "startedHarness": False,
                }
            },
            0,
            json.dumps(fixture).encode(),
            b"",
        )
        self.assertEqual(
            [], runner.validate_output(observation, runner.ContractRegistry())
        )
        observation.case["expected"]["startedHarness"] = True
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertIn("harness start: expected True, observed False", failures)

    def test_file_effects_are_bounded_to_workspace_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            selection = workspace / "config/openprose/cli.toml"
            selection.parent.mkdir(parents=True)
            selection.write_text('harness = "claude"\n', encoding="utf-8")
            value = {
                "schema": "openprose.harness-selection/1",
                "harness": "claude",
                "scope": "user",
                "path": str(selection),
                "changed": True,
            }
            expected = {
                "exitCode": 0,
                "stdout": {
                    "kind": "json",
                    "schema": "openprose.harness-selection/1",
                },
                "stderr": {"kind": "empty"},
                "fileEffects": [
                    {
                        "path": "{{WORKSPACE}}/config/openprose/cli.toml",
                        "utf8": 'harness = "claude"\n',
                    }
                ],
            }
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {"expected": expected},
                0,
                json.dumps(value).encode(),
                b"",
                workspace=workspace,
            )
            self.assertEqual(
                [], runner.validate_output(observation, runner.ContractRegistry())
            )
            selection.write_text('harness = "codex"\n', encoding="utf-8")
            failures = runner.validate_output(observation, runner.ContractRegistry())
            self.assertTrue(any("file effect" in failure for failure in failures))
            expected["fileEffects"][0]["path"] = "{{WORKSPACE}}/../escape"
            failures = runner.validate_output(observation, runner.ContractRegistry())
            self.assertTrue(
                any("leaves product workspace" in failure for failure in failures)
            )

    def test_result_subset_normalizes_the_product_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            result = {
                "schema": "openprose.harness-selection/1",
                "harness": "claude",
                "scope": "user",
                "path": str(workspace / "config/openprose/cli.toml"),
                "changed": True,
            }
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {
                    "expected": {
                        "exitCode": 0,
                        "stdout": {
                            "kind": "json",
                            "schema": "openprose.harness-selection/1",
                        },
                        "stderr": {"kind": "empty"},
                        "resultMatches": {
                            "path": "{{WORKSPACE}}/config/openprose/cli.toml"
                        },
                    }
                },
                0,
                json.dumps(result).encode(),
                b"",
                workspace=workspace,
            )
            self.assertEqual(
                [], runner.validate_output(observation, runner.ContractRegistry())
            )

    def test_jsonl_terminal_payload_is_the_result_validation_surface(self) -> None:
        error = {
            "schema": "openprose.runner-error/1",
            "code": "HOSTED_UNAVAILABLE",
            "boundary": "hosted-service",
            "exitCode": 10,
            "retryable": False,
            "message": "OpenProse-billed execution is not available in this build.",
            "action": (
                "Select an available BYO harness with the `cli harness use <id>` "
                "runner operation, then invoke the `cli doctor` runner operation."
            ),
            "details": {"billingOwner": "openprose", "fallbackSelected": False},
        }
        event = {
            "schema": "openprose.normalized-event/1",
            "sequence": 0,
            "timestamp": "2025-01-01T00:00:00Z",
            "invocationId": "fixture-invocation-0001",
            "type": "runner.failed",
            "payload": {"kind": "runner.failed", "error": error},
        }
        observation = runner.Observation(
            runner.Product("fixture", Path(sys.executable)),
            {
                "expected": {
                    "exitCode": 10,
                    "stdout": {
                        "kind": "jsonl",
                        "eventTypes": ["runner.failed"],
                        "terminalType": "runner.failed",
                    },
                    "stderr": {"kind": "empty"},
                    "errorCode": "HOSTED_UNAVAILABLE",
                    "errorAction": error["action"],
                }
            },
            10,
            (json.dumps(event) + "\n").encode(),
            b"",
        )
        self.assertEqual(
            [], runner.validate_output(observation, runner.ContractRegistry())
        )
        event["payload"]["error"]["code"] = "CANCELLED"
        observation.stdout = (json.dumps(event) + "\n").encode()
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertTrue(any("HOSTED_UNAVAILABLE" in failure for failure in failures))

    def test_jsonl_difference_normalization_removes_only_declared_event_volatility(
        self,
    ) -> None:
        left = [
            {
                "schema": "openprose.normalized-event/1",
                "sequence": 0,
                "timestamp": "left",
                "invocationId": "left-id",
                "type": "runner.started",
                "payload": {
                    "kind": "runner.started",
                    "runnerName": "rust",
                    "runnerVersion": "1",
                },
            }
        ]
        right = [
            {
                "schema": "openprose.normalized-event/1",
                "sequence": 0,
                "timestamp": "right",
                "invocationId": "right-id",
                "type": "runner.started",
                "payload": {
                    "kind": "runner.started",
                    "runnerName": "bun",
                    "runnerVersion": "1",
                },
            }
        ]
        self.assertEqual(
            runner.normalized_for_difference(left),
            runner.normalized_for_difference(right),
        )

    def test_candidate_specs_preserve_surface_label_and_expected_runner(self) -> None:
        products = runner.parse_candidate_specs(
            [
                ["rust-installed", "rust", "/tmp/rust/prose"],
                ["bun-installed", "bun", "/tmp/bun/prose"],
                ["npm-launcher", "bun", "/tmp/npm/prose"],
            ]
        )
        self.assertEqual(
            [
                ("rust-installed", "rust", Path("/tmp/rust/prose")),
                ("bun-installed", "bun", Path("/tmp/bun/prose")),
                ("npm-launcher", "bun", Path("/tmp/npm/prose")),
            ],
            [
                (item.name, item.expected_runner_name, item.executable)
                for item in products
            ],
        )
        with self.assertRaisesRegex(ValueError, "duplicate candidate label"):
            runner.parse_candidate_specs(
                [["same", "rust", "/tmp/a"], ["same", "bun", "/tmp/b"]]
            )
        with_interpreter = runner.attach_candidate_interpreters(
            products, [["npm-launcher", "/usr/bin/node"]]
        )
        self.assertEqual(Path("/usr/bin/node"), with_interpreter[2].interpreter)
        with self.assertRaisesRegex(ValueError, "duplicate candidate interpreter"):
            runner.attach_candidate_interpreters(
                products,
                [
                    ["npm-launcher", "/usr/bin/node"],
                    ["npm-launcher", "/opt/node"],
                ],
            )

    @unittest.skipUnless(os.name == "posix", "symlink custody fixture requires POSIX")
    def test_symlink_candidate_binds_link_and_resolved_target_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "launcher.py"
            target.write_bytes(b"#!/usr/bin/env python3\nprint('ok')\n")
            target.chmod(0o755)
            leaf = root / "prose"
            leaf.symlink_to(target.name)
            product = runner.Product("npm-launcher", leaf, "bun")
            identity = runner.capture_candidate_identity(product)
            self.assertEqual("symlink", identity.leaf_kind)
            self.assertEqual(
                runner.sha256(target.name.encode("utf-8")),
                identity.leaf_sha256,
            )
            self.assertEqual(runner.sha256(target.read_bytes()), identity.target_sha256)
            self.assertFalse(identity.leaf_and_target_same_bytes)
            record = runner.candidate_report_record(product, identity)
            self.assertNotIn(str(root), json.dumps(record))
            self.assertIsNone(runner.candidate_identity_difference(product, identity))

    def test_tampered_candidate_bytes_fail_custody(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "prose"
            executable.write_bytes(b"#!/usr/bin/env python3\nprint('one')\n")
            executable.chmod(0o755)
            product = runner.Product("rust-installed", executable, "rust")
            identity = runner.capture_candidate_identity(product)
            executable.write_bytes(b"#!/usr/bin/env python3\nprint('two')\n")
            difference = runner.candidate_identity_difference(product, identity)
            self.assertEqual("resolved target bytes changed", difference)

    def test_machine_report_is_closed_canonical_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "prose"
            executable.write_bytes(b"fixture executable")
            executable.chmod(0o755)
            product = runner.Product("rust-installed", executable, "rust")
            identity = runner.capture_candidate_identity(product)
            report = runner.make_report(
                phase=7,
                case_ids=["a.case", "b.case"],
                candidates=[(product, identity)],
                candidate_passed=1,
                candidate_failed=1,
                differential_passed=0,
                differential_failed=0,
                failures=[
                    {
                        "caseId": "b.case",
                        "candidateLabels": ["rust-installed"],
                        "code": "OUTPUT_VALIDATION_FAILED",
                        "detail": "bounded detail",
                    }
                ],
            )
            encoded = runner.render_report(report)
            self.assertEqual(encoded, runner.render_report(report))
            self.assertTrue(encoded.endswith(b"\n"))
            self.assertEqual(
                encoded,
                runner.canonical_json(json.loads(encoded)) + b"\n",
            )
            self.assertEqual(
                {
                    "schema",
                    "phase",
                    "caseIds",
                    "candidates",
                    "validations",
                    "failures",
                    "claims",
                },
                set(report),
            )
            self.assertEqual(2, report["validations"]["total"])
            self.assertEqual("fail", report["validations"]["status"])
            self.assertFalse(report["claims"]["semanticConformance"])
            self.assertFalse(report["claims"]["releaseAdmission"])
            self.assertFalse(report["claims"]["detachedDescendantContainment"])
            self.assertNotIn(str(Path(temporary)), encoded.decode("utf-8"))
            self.assertEqual([], runner.validate_report(report))
            changed = json.loads(encoded)
            changed["validations"]["candidateCases"]["unknown"] = 1
            self.assertTrue(runner.validate_report(changed))
            malformed = json.loads(encoded)
            malformed["validations"]["candidateCases"] = None
            malformed["caseIds"] = 7
            malformed["candidates"][0]["executableLeaf"] = None
            self.assertTrue(runner.validate_report(malformed))
            overclaim = json.loads(encoded)
            overclaim["claims"]["detachedDescendantContainment"] = True
            self.assertTrue(
                any(
                    "overstates" in failure
                    for failure in runner.validate_report(overclaim)
                )
            )

            destination = Path(temporary) / "exclusive-report.json"
            runner.write_report(destination, report)
            original = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                runner.write_report(destination, report)
            self.assertEqual(original, destination.read_bytes())

    def test_report_option_runs_exact_candidates_without_building(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            help_bytes = (runner.CASES / "fixtures/runner-help.txt").read_bytes()
            executable = root / "fake-prose"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.stdout.buffer.write({help_bytes!r})\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            reports = [root / "first.json", root / "second.json"]
            outputs: list[str] = []
            for report_path in reports:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    status = runner.main(
                        [
                            "--phase",
                            "7",
                            "--case",
                            "core.initial-help",
                            "--candidate",
                            "fixture",
                            "rust",
                            str(executable),
                            "--candidate-interpreter",
                            "fixture",
                            sys.executable,
                            "--report-json",
                            str(report_path),
                        ]
                    )
                self.assertEqual(0, status, stderr.getvalue())
                outputs.append(stdout.getvalue())
            self.assertEqual(reports[0].read_bytes(), reports[1].read_bytes())
            self.assertEqual(outputs[0], outputs[1])
            report = json.loads(reports[0].read_bytes())
            interpreter = report["candidates"][0]["interpreter"]
            self.assertIsNotNone(interpreter)
            self.assertEqual(
                {
                    "mode": "resolved-original-path",
                    "preAndPostByteCustody": True,
                    "ownedSnapshot": False,
                    "relocatableClosureCaptured": False,
                    "execBoundaryToctouProtection": "not-enforced",
                },
                interpreter["execution"],
            )
            overclaim = json.loads(reports[0].read_bytes())
            overclaim["candidates"][0]["interpreter"]["execution"][
                "execBoundaryToctouProtection"
            ] = "enforced"
            self.assertTrue(
                any(
                    "overstates interpreter custody" in failure
                    for failure in runner.validate_report(overclaim)
                )
            )

    def test_report_rejects_candidate_attempt_to_mutate_owned_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            help_bytes = (runner.CASES / "fixtures/runner-help.txt").read_bytes()
            executable = root / "mutating-prose"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"sys.stdout.buffer.write({help_bytes!r})\n"
                "with Path(__file__).open('ab') as changed:\n"
                "    changed.write(b'# mutation\\n')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            source_bytes = executable.read_bytes()
            report_path = root / "report.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                status = runner.main(
                    [
                        "--phase",
                        "7",
                        "--case",
                        "core.initial-help",
                        "--candidate",
                        "fixture",
                        "rust",
                        str(executable),
                        "--report-json",
                        str(report_path),
                    ]
                )
            self.assertEqual(1, status)
            report = json.loads(report_path.read_bytes())
            self.assertEqual("fail", report["validations"]["status"])
            self.assertEqual(1, report["validations"]["candidateCases"]["failed"])
            self.assertEqual(
                ["OUTPUT_VALIDATION_FAILED"],
                [failure["code"] for failure in report["failures"]],
            )
            self.assertEqual(source_bytes, executable.read_bytes())

    def test_owned_snapshot_executes_captured_bytes_after_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_text(
                "#!/usr/bin/env python3\nprint('captured')\n", encoding="utf-8"
            )
            source.chmod(0o755)
            product = runner.Product("fixture", source, "rust", Path(sys.executable))
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            source.write_text(
                "#!/usr/bin/env python3\nprint('changed')\n", encoding="utf-8"
            )
            result = runner.run_owned_process(
                snapshot.execution_argv([]),
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(0, result.exit_code)
            self.assertEqual(b"captured\n", result.stdout)
            self.assertIsNone(runner.snapshot_identity_difference(snapshot, identity))

    @unittest.skipUnless(
        os.name == "posix", "relative interpreter fixture requires POSIX"
    )
    def test_interpreter_executes_in_original_location_with_relative_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            interpreter = runtime_root / "bin" / "fake-node"
            sibling = runtime_root / "lib" / "closure-marker"
            interpreter.parent.mkdir(parents=True)
            sibling.parent.mkdir(parents=True)
            sibling.write_text("closure-present\n", encoding="utf-8")
            interpreter.write_text(
                "#!/bin/sh\n"
                'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                'test "$(cat "$root/lib/closure-marker")" = closure-present || exit 91\n'
                'exec "$@"\n',
                encoding="utf-8",
            )
            interpreter.chmod(0o755)
            candidate = root / "launcher"
            candidate.write_text("#!/bin/sh\nprintf 'portable\\n'\n", encoding="utf-8")
            candidate.chmod(0o755)
            product = runner.Product("fixture", candidate, "bun", interpreter)
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            self.assertEqual(interpreter.resolve(), snapshot.execution_interpreter)
            self.assertFalse((root / "owned-snapshot" / "interpreter").exists())
            result = runner.run_owned_process(
                snapshot.execution_argv([]),
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(0, result.exit_code, result.stderr)
            self.assertEqual(b"portable\n", result.stdout)
            self.assertIsNone(runner.snapshot_identity_difference(snapshot, identity))

    @unittest.skipUnless(shutil.which("node"), "Node is required for CommonJS fixture")
    def test_node_executes_snapshot_bytes_with_original_package_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "node_modules" / "@openprose" / "prose-cli"
            launcher = package / "bin" / "prose.js"
            launcher.parent.mkdir(parents=True)
            (package / "closure-marker").write_text(
                "package-context\n", encoding="utf-8"
            )
            launcher.write_text(
                "const fs=require('node:fs'); const path=require('node:path');\n"
                "process.stdout.write(fs.readFileSync(path.resolve(__dirname,'..','closure-marker'),'utf8'));\n",
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            node = Path(shutil.which("node") or "")
            product = runner.Product("npm-launcher", launcher, "bun", node)
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            result = runner.run_owned_process(
                snapshot.execution_argv(["opaque", "--flag"]),
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(0, result.exit_code, result.stderr)
            self.assertEqual(b"package-context\n", result.stdout)

    def test_owned_snapshot_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            source.chmod(0o755)
            product = runner.Product("fixture", source, "rust")
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            assert snapshot.execution_executable is not None
            snapshot.execution_executable.chmod(0o755)
            snapshot.execution_executable.write_bytes(b"#!/bin/sh\nexit 1\n")
            self.assertEqual(
                "owned executable snapshot bytes changed",
                runner.snapshot_identity_difference(snapshot, identity),
            )

    def test_owned_snapshot_refuses_source_bytes_changed_after_identity_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            source.chmod(0o755)
            product = runner.Product("fixture", source, "rust")
            identity = runner.capture_candidate_identity(product)
            source.write_bytes(b"#!/bin/sh\nexit 1\n")
            with self.assertRaisesRegex(ValueError, "changed before snapshot"):
                runner.snapshot_product(product, identity, root / "owned-snapshot")

    def test_failure_detail_redacts_paths_tokens_and_bounds_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary)
            product = runner.Product(
                "rust-installed", suite / "owned/bin/prose", "rust"
            )
            detail = (
                f"workspace={suite}/case token=super-secret "
                f"candidate={product.executable} " + ("x" * 5000)
            )
            sanitized = runner.sanitize_failure_detail(detail, suite, [product])
            self.assertNotIn(str(suite), sanitized)
            self.assertNotIn("super-secret", sanitized)
            self.assertLessEqual(
                len(sanitized.encode("utf-8")), runner.MAX_FAILURE_BYTES
            )

    def test_candidate_expected_runner_identity_is_checked_before_differential(
        self,
    ) -> None:
        value = json.loads(
            (
                runner.CLI / "shared/fixtures/transport/runner-result-success.json"
            ).read_text("utf-8")
        )
        value["runner"]["name"] = "rust"
        observation = runner.Observation(
            runner.Product("npm-launcher", Path(sys.executable), "bun"),
            {
                "expected": {
                    "exitCode": 0,
                    "stdout": {
                        "kind": "json",
                        "schema": "openprose.runner-result/1",
                    },
                    "stderr": {"kind": "empty"},
                }
            },
            0,
            json.dumps(value).encode(),
            b"",
        )
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertIn("runner identity: expected 'bun', got 'rust'", failures)

    def test_process_groups_cannot_claim_detached_descendant_authority(self) -> None:
        self.assertFalse(runner.timeout_cleanup_has_descendant_authority("posix"))
        self.assertFalse(runner.timeout_cleanup_has_descendant_authority("nt"))

    def test_configuration_sourced_cwd_is_not_treated_as_runner_result_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            value = json.loads(
                (
                    runner.CLI
                    / "shared/fixtures/operations/configuration-explanation.json"
                ).read_text("utf-8")
            )
            encoded = json.dumps(value).replace("/workspace", str(workspace)).encode()
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {
                    "expected": {
                        "exitCode": 0,
                        "stdout": {
                            "kind": "json",
                            "schema": "openprose.configuration-explanation/1",
                        },
                        "stderr": {"kind": "empty"},
                    }
                },
                0,
                encoded,
                b"",
                workspace=workspace,
            )
            failures = runner.validate_output(
                observation,
                runner.ContractRegistry(),
            )
            self.assertEqual([], failures)

    def test_product_roots_use_distinct_workspaces_and_environment_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case_root = Path(temporary)
            rust_environment, rust_workspace = runner.product_roots(case_root, "rust")
            bun_environment, bun_workspace = runner.product_roots(case_root, "bun")
            self.assertNotEqual(rust_workspace, bun_workspace)
            self.assertNotEqual(rust_environment, bun_environment)
            rust_workspace.mkdir(parents=True)
            bun_workspace.mkdir(parents=True)
            (rust_workspace / "product-state").write_text("rust", encoding="utf-8")
            self.assertFalse((bun_workspace / "product-state").exists())

    def test_execute_forwards_only_each_products_own_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case_root = Path(temporary)
            executable = case_root / "fake-product"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,pathlib\n"
                "pathlib.Path('state').write_text(os.environ['PRODUCT'])\n"
                "print(json.dumps({'cwd':os.getcwd(),'forwarded':os.environ['FORWARDED']}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            case = {
                "controls": {},
                "invocation": {
                    "argv": [],
                    "cwd": "{{WORKSPACE}}",
                    "environment": {
                        "FORWARDED": "{{WORKSPACE}}/nested",
                        "PRODUCT": "fixture",
                    },
                },
                "expected": {},
            }
            observed = {}
            for name in ("rust", "bun"):
                environment_root, workspace = runner.product_roots(case_root, name)
                workspace.mkdir(parents=True)
                observation = runner.execute(
                    runner.Product(name, executable),
                    case,
                    environment_root,
                    workspace,
                )
                self.assertTrue(observation.process_settled)
                self.assertEqual(observation.exit_code, 0)
                observed[name] = json.loads(observation.stdout)
            self.assertNotEqual(observed["rust"]["cwd"], observed["bun"]["cwd"])
            for name in ("rust", "bun"):
                _, workspace = runner.product_roots(case_root, name)
                self.assertEqual(observed[name]["cwd"], str(workspace.resolve()))
                self.assertEqual(
                    observed[name]["forwarded"], str(workspace.resolve() / "nested")
                )
                self.assertEqual((workspace / "state").read_text("utf-8"), "fixture")

    def test_difference_normalization_verifies_and_rewrites_owned_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            value = {
                "cwd": {
                    "path": str(workspace),
                    "identitySha256": runner.sha256(str(workspace).encode("utf-8")),
                },
                "nested": {"path": str(workspace / "home"), "unrelated": "/elsewhere"},
            }
            normalized = runner.normalized_for_difference(value, workspace)
            self.assertEqual(normalized["cwd"]["path"], "{{WORKSPACE}}")
            self.assertEqual(
                normalized["cwd"]["identitySha256"], "{{WORKSPACE_IDENTITY_SHA256}}"
            )
            self.assertEqual(normalized["nested"]["path"], "{{WORKSPACE}}/home")
            self.assertEqual(normalized["nested"]["unrelated"], "/elsewhere")

    @unittest.skipUnless(
        os.name == "posix", "owned process-group oracle requires POSIX"
    )
    def test_owned_timeout_kills_descendants_and_settles_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "image"
            task = root / "task"
            identities = root / "identities.json"
            image.write_bytes(b"image")
            task.write_bytes(b"task")
            result = runner.run_owned_process(
                [
                    sys.executable,
                    str(runner.FAKE_HARNESS),
                    "run",
                    "--scenario",
                    "descendant",
                    "--image-file",
                    str(image),
                    "--task-file",
                    str(task),
                    "--descendant-pid-file",
                    str(identities),
                ],
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(result.settled)
            self.assertEqual(result.exit_code, 124)
            self.assertTrue(identities.is_file())
            published = json.loads(identities.read_text("utf-8"))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and any(
                runner.pid_exists(published[name])
                for name in ("childPid", "grandchildPid")
            ):
                time.sleep(0.01)
            self.assertFalse(runner.pid_exists(published["childPid"]))
            self.assertFalse(runner.pid_exists(published["grandchildPid"]))

    @unittest.skipUnless(
        os.name == "posix", "owned process-group oracle requires POSIX"
    )
    def test_zero_exit_with_live_owned_descendant_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / "child.json"
            script = (
                "import json,subprocess,sys\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "open(sys.argv[1],'w').write(json.dumps({'pid':child.pid}))\n"
            )
            result = runner.run_owned_process(
                [sys.executable, "-c", script, str(identity)],
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(result.exit_code, 124)
            self.assertFalse(result.timed_out)
            self.assertFalse(result.settled)
            child_pid = json.loads(identity.read_text("utf-8"))["pid"]
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and runner.pid_exists(child_pid):
                time.sleep(0.01)
            self.assertFalse(runner.pid_exists(child_pid))

    def test_owned_process_capture_is_bounded_while_pipes_are_fully_drained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload_bytes = runner.MAX_CAPTURE_BYTES * 2 + 17
            script = (
                "import os,sys\n"
                "remaining=int(sys.argv[1])\n"
                "chunk=b'x'*65536\n"
                "while remaining:\n"
                " part=chunk[:remaining]\n"
                " os.write(1,part); os.write(2,part); remaining-=len(part)\n"
            )
            result = runner.run_owned_process(
                [sys.executable, "-c", script, str(payload_bytes)],
                cwd=Path(temporary),
                environment={"PATH": os.defpath},
                timeout_seconds=10.0,
            )
            self.assertEqual(0, result.exit_code)
            self.assertTrue(result.settled)
            self.assertLessEqual(len(result.stdout), runner.MAX_CAPTURE_BYTES)
            self.assertLessEqual(len(result.stderr), runner.MAX_CAPTURE_BYTES)
            self.assertTrue(result.stdout_truncated)
            self.assertTrue(result.stderr_truncated)
            self.assertTrue(result.stdout.endswith(runner.CAPTURE_TRUNCATION_MARKER))
            self.assertTrue(result.stderr.endswith(runner.CAPTURE_TRUNCATION_MARKER))

    @unittest.skipUnless(os.name == "posix", "signal cleanup oracle requires POSIX")
    def test_base_exception_cleans_owned_process_group_before_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / "pid"
            script = (
                "import os,pathlib,signal,sys,time\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "time.sleep(60)\n"
            )

            def interrupt_after_spawn() -> None:
                deadline = time.monotonic() + 5.0
                while not identity.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if identity.exists():
                    os.kill(os.getpid(), signal.SIGINT)

            interrupter = threading.Thread(target=interrupt_after_spawn)
            interrupter.start()
            with self.assertRaises(KeyboardInterrupt):
                runner.run_owned_process(
                    [sys.executable, "-c", script, str(identity)],
                    cwd=root,
                    environment={"PATH": os.defpath},
                    timeout_seconds=30.0,
                )
            interrupter.join(timeout=2.0)
            self.assertFalse(interrupter.is_alive())
            child_pid = int(identity.read_text("utf-8"))
            self.assertTrue(
                runner._wait_until(lambda: not runner.pid_exists(child_pid), 2.0)
            )

    def test_deep_subset_reports_nested_drift(self) -> None:
        self.assertEqual(
            [], runner.deep_subset({"a": {"b": 1}, "extra": 2}, {"a": {"b": 1}})
        )
        self.assertEqual(
            ["$.a.b: expected 2, got 1"],
            runner.deep_subset({"a": {"b": 1}}, {"a": {"b": 2}}),
        )

    def test_difference_normalization_keeps_adapter_claims(self) -> None:
        value = {
            "invocationId": "volatile",
            "runner": {"name": "rust", "version": "1", "commit": "x"},
            "timing": {"startedAt": "now", "durationMs": 1},
            "digests": {
                "invocationSha256": "derived",
                "normalizedEventsSha256": "derived",
                "taskSha256": "stable",
            },
            "adapter": {"id": "stable"},
        }
        normalized = runner.normalized_for_difference(value)
        self.assertNotIn("invocationId", normalized)
        self.assertNotIn("name", normalized["runner"])
        self.assertEqual({"taskSha256": "stable"}, normalized["digests"])
        self.assertEqual({"id": "stable"}, normalized["adapter"])

    def test_task_digest_is_shell_neutral(self) -> None:
        task = {
            "schema": "openprose.task-envelope/1",
            "argv": ["prose", "run", "space here", ";$(nope)", "雪"],
            "interactionMode": "non-interactive",
        }
        self.assertEqual(
            runner.sha256(runner.canonical_json(task)),
            runner.sha256(runner.canonical_json(task)),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
