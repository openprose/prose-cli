#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import unittest

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError as error:  # pragma: no cover - actionable bootstrap failure
    raise SystemExit(
        "Install the pinned test dependency: "
        "python3 -m pip install -r cli/shared/requirements-test.txt"
    ) from error


SHARED = Path(__file__).resolve().parents[1]
CLI = SHARED.parent
SCHEMAS = SHARED / "schemas"
FIXTURES = SHARED / "fixtures"
SENTINEL = SHARED / "image" / "sentinel-v1"
CASES = CLI / "conformance" / "cases"


def load_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def canonical_json(value) -> bytes:
    """Reference JCS encoding for the I-JSON fixture subset (no floats)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ContractsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            path.name: load_json(path)
            for path in sorted(SCHEMAS.glob("*.schema.json"))
        }
        cls.registry = Registry()
        for schema in cls.schemas.values():
            cls.registry = cls.registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )

    def validator(self, name: str):
        schema = self.schemas[name]
        return jsonschema.Draft202012Validator(
            schema,
            registry=self.registry,
            format_checker=jsonschema.FormatChecker(),
        )

    def assert_valid(self, schema_name: str, instance) -> None:
        errors = sorted(self.validator(schema_name).iter_errors(instance), key=lambda error: list(error.path))
        self.assertEqual([], [f"{list(error.path)}: {error.message}" for error in errors])

    def test_registry_reports_bind_operation_and_failure(self):
        # `cli package ...` prints the service-operation/1 envelope; its result
        # is a receipt, a page of receipts or a withdrawal.
        receipt = load_json(FIXTURES / "registry/directory.receipt.json")
        for result in (receipt, {"packages": [receipt], "nextCursor": None}, {"receipt": receipt, "withdrawn": True}):
            self.assert_valid("package-operation.schema.json", result)
        base = {"schema": "openprose.service-operation/1", "operation": "package.publish",
                "interaction": "registry.gateway", "result": receipt, "problem": None}
        self.assert_valid("service-operation.schema.json", base)
        taxonomy = load_json(SHARED / "errors/taxonomy.v1.json")
        records = taxonomy["errors"]
        failure = {"schema": "openprose.runner-error/1", **next(item for item in records if item["code"] == "SERVICE_UNAVAILABLE")}
        self.assert_valid("service-operation.schema.json", {**base, "result": None, "problem": failure})
        for changes in ({"result": None}, {"problem": failure}, {"token": "secret"}, {"environment": "production"}):
            self.assertTrue(list(self.validator("service-operation.schema.json").iter_errors({**base, **changes})))
        for bad in ({**receipt, "token": "secret"}, {"packages": [receipt]}, {"receipt": receipt, "withdrawn": False}):
            self.assertTrue(list(self.validator("package-operation.schema.json").iter_errors(bad)))
        self.assertEqual(receipt["reference"]["sha256"], sha256((FIXTURES / "registry/directory.canonical.json").read_bytes()))

    def test_kernel_startup_instruction_recipes_and_provider_routes(self):
        fixture = load_json(FIXTURES / "adapters/kernel-startup.json")
        recipes = SHARED / "capabilities/adapters/recipes"
        for mode in ("developer", "base"):
            self.assert_valid("adapter-admission-recipe.schema.json", load_json(recipes / f"codex-exec-json-{mode}.v1.json"))
        oracle = load_json(SHARED / "capabilities/adapters/oracle.v1.json")
        admissions = {a["adapterId"].split("/")[0]: a for a in oracle["adapters"]}
        for harness, routes in fixture["credentialRoutes"].items():
            for route in routes:
                self.assertIn(route, admissions[harness]["credentialGroups"])
        self.assertEqual(fixture["failurePolicy"], "no-echo-or-provider-fallback")

    def test_safe_transport_diagnostics(self):
        validator = self.validator("transport-diagnostic.schema.json")
        for fixture in load_json(FIXTURES / "transport-diagnostics.json"):
            value = {"schema": "openprose.transport-diagnostic/1", "reason": fixture["reason"]}
            for key in ("observedBytes", "limitBytes"):
                if key in fixture:
                    value[key] = fixture[key]
            self.assertEqual(list(validator.iter_errors(value)), [])
        for changes in ({"reason": "raw secret"}, {"observedBytes": -1}, {"observedBytes": 4294967296}, {"payload": "secret"}):
            value = {"schema": "openprose.transport-diagnostic/1", "reason": "invalid-json", **changes}
            self.assertTrue(list(validator.iter_errors(value)))

    def test_native_sdk_limits_and_failures_are_closed(self):
        fixture=json.loads((SHARED / 'fixtures/adapters/sdk-native-limits.json').read_text())
        config=json.loads((SHARED / 'fixtures/operations/configuration-explanation.json').read_text())
        config['values']['nativeMaxTurns']={'value':'40','source':{'kind':'flag','location':'--native-max-turns'}}
        config['values']['nativeTimeout']={'value':'5m','source':{'kind':'flag','location':'--native-timeout'}}
        self.assert_valid('configuration-explanation.schema.json',config)
        self.assert_valid('native-limits.schema.json',fixture['defaults'])
        self.assert_valid('native-limits.schema.json',fixture['override']['limits'])
        for case in fixture['errorCases']:
            self.assert_valid('native-failure.schema.json',{'kind':case['kind'],'limits':fixture['defaults'],'elapsedSeconds':1.5})
        for bad in [{'kind':'arbitrary'}, {'kind':'execution','message':'secret'}, {'kind':'timeout','elapsedSeconds':-1}]:
            self.assertTrue(list(self.validator('native-failure.schema.json').iter_errors(bad)))

    def test_optional_reporting_fields_are_named_and_closed(self):
        fixture=json.loads((SHARED / 'fixtures/config/optional-reporting.json').read_text())
        config=json.loads((SHARED / 'fixtures/operations/configuration-explanation.json').read_text())
        for case in fixture['cases']:
            for key in fixture['defaultsOmitted']:
                config['values'][key]={'value':case[key],'source':{'kind':'flag','location':'test'}}
            self.assert_valid('configuration-explanation.schema.json',config)
        config['values']['permissionMode']['value']='arbitrary'
        self.assertTrue(list(self.validator('configuration-explanation.schema.json').iter_errors(config)))

    def test_every_schema_is_valid_draft_2020_12_and_has_unique_id(self) -> None:
        self.assertGreaterEqual(len(self.schemas), 10)
        ids = []
        for name, schema in self.schemas.items():
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema", name)
            jsonschema.Draft202012Validator.check_schema(schema)
            ids.append(schema["$id"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_normative_transport_examples_validate(self) -> None:
        self.assert_valid("runner-invocation.schema.json", load_json(FIXTURES / "transport" / "runner-invocation.json"))
        self.assert_valid("launch-adapter.schema.json", load_json(FIXTURES / "transport" / "mock-adapter.json"))
        self.assert_valid("launch-adapter.schema.json", load_json(FIXTURES / "transport" / "deterministic-mock-adapter.json"))
        self.assert_valid("runner-result.schema.json", load_json(FIXTURES / "transport" / "runner-result-success.json"))
        self.assert_valid("runner-result.schema.json", load_json(FIXTURES / "transport" / "runner-result-hosted-unavailable.json"))
        self.assert_valid("normalized-event.schema.json", load_json(FIXTURES / "transport" / "runner-completed-event.json"))
        for line in (FIXTURES / "transport" / "normalized-events-preterminal.jsonl").read_text("utf-8").splitlines():
            self.assert_valid("normalized-event.schema.json", json.loads(line))

    def test_human_safe_scalar_fixture_is_closed_and_covers_terminal_controls(self) -> None:
        fixture = load_json(FIXTURES / "human" / "human-safe-scalars.json")
        self.assertEqual(fixture["schema"], "openprose.human-safe-scalars/1")
        self.assertEqual(
            fixture["escapeNotation"],
            {
                "backslash": r"\\",
                "lineFeed": r"\n",
                "carriageReturn": r"\r",
                "tab": r"\t",
                "backspace": r"\b",
                "formFeed": r"\f",
                "otherControlTemplate": r"\u{XXXX}",
            },
        )
        self.assertEqual(
            fixture["unsafeCodePointRanges"],
            [
                {"start": 0, "end": 31},
                {"start": 127, "end": 159},
                {"start": 0x2028, "end": 0x2029},
            ],
        )
        cases = fixture["cases"]
        self.assertEqual(
            [case["id"] for case in cases],
            [
                "printable",
                "literal-backslashes",
                "named-controls",
                "other-line-breaking-controls",
            ],
        )
        self.assertEqual(len(cases), len({case["id"] for case in cases}))
        hostile_input = "".join(case["input"] for case in cases)
        for code_point in (0, 7, 8, 9, 10, 11, 12, 13, 27, 31, 127, 133, 159, 0x2028, 0x2029):
            self.assertIn(chr(code_point), hostile_input)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(set(case), {"id", "input", "rendered"})
                self.assertFalse(
                    any(
                        ord(character) <= 31
                        or 127 <= ord(character) <= 159
                        or ord(character) in (0x2028, 0x2029)
                        for character in case["rendered"]
                    )
                )

        multiline_cases = fixture["multilineCases"]
        self.assertEqual(
            [case["id"] for case in multiline_cases],
            ["ordinary-multiline-prose", "hostile-terminal-sequence"],
        )
        self.assertEqual(multiline_cases[0]["input"], multiline_cases[0]["rendered"])
        self.assertIn("\\\\path", multiline_cases[1]["rendered"])
        for case in multiline_cases:
            with self.subTest(multiline_case=case["id"]):
                self.assertEqual(set(case), {"id", "input", "rendered"})
                self.assertEqual(case["input"].count("\n"), case["rendered"].count("\n"))
                self.assertFalse(
                    any(
                        (ord(character) <= 31 and character != "\n")
                        or 127 <= ord(character) <= 159
                        or ord(character) in (0x2028, 0x2029)
                        for character in case["rendered"]
                    )
                )

    def test_flat_toml_configuration_corpus_is_closed_and_redacted(self) -> None:
        fixture = load_json(FIXTURES / "config" / "flat-toml-v1.json")
        self.assertEqual(fixture["schema"], "openprose.config-flat-toml-corpus/1")
        self.assertEqual(
            fixture["knownKeys"],
            [
                "harness",
                "transport",
                "model",
                "timeout",
                "output",
                "color",
                "verbose",
                "auth_profile",
            ],
        )
        self.assertEqual(
            fixture["basicStringEscapes"],
            [r'\"', r"\\", r"\b", r"\t", r"\n", r"\f", r"\r", r"\uXXXX", r"\UXXXXXXXX"],
        )
        cases = fixture["accepted"] + fixture["rejected"] + fixture["binaryRejected"]
        self.assertEqual(len(cases), len({case["id"] for case in cases}))
        self.assertGreaterEqual(len(fixture["accepted"]), 4)
        self.assertGreaterEqual(len(fixture["rejected"]), 20)
        for case in fixture["accepted"]:
            with self.subTest(accepted=case["id"]):
                self.assertEqual(set(case), {"id", "source", "values"})
                self.assertIsInstance(case["values"], dict)
        for case in fixture["rejected"]:
            with self.subTest(rejected=case["id"]):
                self.assertIn(set(case), [
                    {"id", "source", "line", "reason"},
                    {"id", "source", "line", "reason", "forbidden"},
                ])
                self.assertGreaterEqual(case["line"], 1)
                self.assertLessEqual(case["line"], len(case["source"].splitlines()))
                if forbidden := case.get("forbidden"):
                    self.assertIn(forbidden, case["source"])
                    self.assertNotIn(forbidden, case["reason"])
        for case in fixture["binaryRejected"]:
            with self.subTest(binary_rejected=case["id"]):
                self.assertEqual(set(case), {"id", "sourceHex", "line", "reason"})
                source = bytes.fromhex(case["sourceHex"])
                with self.assertRaises(UnicodeDecodeError):
                    source.decode("utf-8")
                self.assertGreaterEqual(case["line"], 1)
                self.assertEqual(case["reason"], "Configuration file is not valid UTF-8.")

    def test_policy_shape_examples_validate(self) -> None:
        examples = FIXTURES / "schema-examples"
        pairs = {
            "adapter-admission-recipe.json": "adapter-admission-recipe.schema.json",
            "release-profile.json": "release-profile.schema.json",
            "benchmark-policy.json": "benchmark-policy.schema.json",
            "dry-run-report.json": "runner-dry-run-report.schema.json",
        }
        for fixture, schema in pairs.items():
            with self.subTest(fixture=fixture):
                self.assert_valid(schema, load_json(examples / fixture))

    def test_machine_operation_examples_validate_and_share_inventory(self) -> None:
        operations = FIXTURES / "operations"
        pairs = {
            "configuration-explanation.json": "configuration-explanation.schema.json",
            "harness-list.json": "harness-list.schema.json",
            "doctor-report.json": "doctor-report.schema.json",
            "account-status-unavailable.json": "account-status.schema.json",
            "harness-selection.json": "harness-selection.schema.json",
            "prime-cleanup.json": "prime-cleanup.schema.json",
        }
        examples = {}
        for fixture, schema in pairs.items():
            with self.subTest(fixture=fixture):
                examples[fixture] = load_json(operations / fixture)
                self.assert_valid(schema, examples[fixture])
        self.assertEqual(
            examples["harness-list.json"]["harnesses"],
            examples["doctor-report.json"]["harnesses"],
        )
        self.assertEqual(
            examples["doctor-report.json"]["runner"],
            {"name": "rust", "version": "0.1.0-fixture", "commit": "fixture-commit"},
        )
        self.assertEqual(
            examples["configuration-explanation.json"],
            examples["doctor-report.json"]["configuration"],
        )
        self.assertEqual(
            examples["doctor-report.json"]["problems"][0]["exitCode"], 10
        )
        self.assertEqual(
            examples["doctor-report.json"]["build"],
            {"profile": "development", "testSeamsEnabled": True},
        )
        omp = next(
            harness
            for harness in examples["harness-list.json"]["harnesses"]
            if harness["id"] == "omp"
        )
        self.assertEqual(
            omp["runtimePrerequisites"],
            [
                {
                    "runtime": "bun",
                    "versionRange": ">=1.3.14",
                    "detectedVersion": None,
                    "availability": "missing",
                    "repairCommand": "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
                }
            ],
        )
        self.assertEqual(
            examples["account-status-unavailable.json"]["problem"]["exitCode"],
            10,
        )
        hosted_error = {
            record["code"]: record
            for record in load_json(SHARED / "errors" / "taxonomy.v1.json")["errors"]
        }["HOSTED_UNAVAILABLE"]
        self.assertEqual(
            hosted_error["action"],
            "To use the hosted service, run `cli run submit FILE --preview`; running "
            "programs on this machine needs a local harness (`cli harness list`).",
        )
        self.assertEqual(
            examples["account-status-unavailable.json"]["problem"]["action"],
            hosted_error["action"],
        )
        self.assertEqual(
            examples["doctor-report.json"]["problems"][0]["action"],
            hosted_error["action"],
        )

    def test_release_profile_mock_unavailability_is_a_closed_harness_status(self) -> None:
        release_inventory = deepcopy(
            load_json(FIXTURES / "operations" / "harness-list.json")
        )
        mock = next(
            harness
            for harness in release_inventory["harnesses"]
            if harness["id"] == "mock"
        )
        mock.update(
            {
                "availability": "unavailable",
                "detectedVersion": None,
                "strictWrapperConformant": False,
                "admissionBlock": "test-seams-disabled",
            }
        )
        self.assert_valid("harness-list.schema.json", release_inventory)

    def test_runtime_prerequisite_available_range_is_closed_and_bounded(self) -> None:
        for version in (
            "1.3.14",
            "1.3.1000000",
            "1.4.0",
            "2.0.0",
            "1000000.1000000.1000000",
        ):
            inventory = deepcopy(load_json(FIXTURES / "operations" / "harness-list.json"))
            omp = next(item for item in inventory["harnesses"] if item["id"] == "omp")
            omp["runtimePrerequisites"][0].update(
                availability="available", detectedVersion=version
            )
            self.assert_valid("harness-list.schema.json", inventory)

        for version in ("1.3.13", "1000001.0.0", "01.3.14", "1.3.14-beta.1"):
            inventory = deepcopy(load_json(FIXTURES / "operations" / "harness-list.json"))
            omp = next(item for item in inventory["harnesses"] if item["id"] == "omp")
            omp["runtimePrerequisites"][0].update(
                availability="available", detectedVersion=version
            )
            self.assertTrue(
                list(self.validator("harness-list.schema.json").iter_errors(inventory)),
                version,
            )

    def test_machine_operation_contracts_are_closed(self) -> None:
        operations = FIXTURES / "operations"
        doctor = load_json(operations / "doctor-report.json")
        doctor["implementationSpecificMeaning"] = True
        self.assertTrue(
            list(self.validator("doctor-report.schema.json").iter_errors(doctor))
        )
        mismatched_build = load_json(operations / "doctor-report.json")
        mismatched_build["build"] = {
            "profile": "release",
            "testSeamsEnabled": True,
        }
        self.assertTrue(
            list(
                self.validator("doctor-report.schema.json").iter_errors(
                    mismatched_build
                )
            )
        )
        config = load_json(operations / "configuration-explanation.json")
        config["values"]["secretToken"] = "forbidden"
        self.assertTrue(
            list(
                self.validator("configuration-explanation.schema.json").iter_errors(
                    config
                )
            )
        )
        inventory = load_json(operations / "harness-list.json")
        omp = next(item for item in inventory["harnesses"] if item["id"] == "omp")
        for mutation in (
            lambda value: value.pop("runtimePrerequisites"),
            lambda value: value.update(runtimePrerequisites=[]),
            lambda value: value["runtimePrerequisites"][0].update(runtime="node"),
            lambda value: value["runtimePrerequisites"][0].update(
                versionRange=">=1.3.13"
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                availability="available", detectedVersion=None
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                availability="incompatible", detectedVersion="1.3.14-beta..1"
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                availability="available", detectedVersion="1.3.14-beta.1"
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                availability="available", detectedVersion="1.3.13"
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                availability="available", detectedVersion="1000001.3.14"
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                availability="incompatible", detectedVersion="1.1000001.14-beta.1"
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                availability="incompatible", detectedVersion="01.3.14"
            ),
            lambda value: value["runtimePrerequisites"][0].update(
                repairCommand="npm install bun@latest"
            ),
            lambda value: value["runtimePrerequisites"][0].update(path="/tmp/bun"),
        ):
            drifted = deepcopy(inventory)
            drifted_omp = next(
                item for item in drifted["harnesses"] if item["id"] == "omp"
            )
            mutation(drifted_omp)
            self.assertTrue(
                list(
                    self.validator("harness-list.schema.json").iter_errors(
                        drifted
                    )
                )
            )
        runtime_error = {
            "schema": "openprose.runner-error/1",
            "code": "HARNESS_INCOMPATIBLE",
            "boundary": "adapter",
            "message": "The selected harness version is incompatible with this adapter.",
            "action": "Run the exact Repair command reported with this error, then retry.",
            "exitCode": 10,
            "retryable": False,
            "details": {
                "adapterId": "omp/rpc",
                "fallbackAttempted": False,
                "runtimePrerequisite": omp["runtimePrerequisites"][0],
            },
        }
        self.assert_valid("runner-error.schema.json", runtime_error)
        runtime_error["details"]["runtimePrerequisite"]["rawOutput"] = "secret"
        self.assertTrue(
            list(
                self.validator("runner-error.schema.json").iter_errors(
                    runtime_error
                )
            )
        )

    def test_policy_safety_invariants_fail_closed(self) -> None:
        examples = FIXTURES / "schema-examples"
        admission = load_json(examples / "adapter-admission-recipe.json")
        admission["launch"]["shell"] = True
        self.assertTrue(list(self.validator("adapter-admission-recipe.schema.json").iter_errors(admission)))
        admission = load_json(examples / "adapter-admission-recipe.json")
        admission["support"].pop("admittedVersions", None)
        self.assertTrue(list(self.validator("adapter-admission-recipe.schema.json").iter_errors(admission)))
        admission = load_json(examples / "adapter-admission-recipe.json")
        admission["support"]["admittedVersions"] = ["1.0.0", "1.0.0"]
        self.assertTrue(list(self.validator("adapter-admission-recipe.schema.json").iter_errors(admission)))
        admission = load_json(examples / "adapter-admission-recipe.json")
        admission["support"]["repairCommand"] = "npm install fake@latest\nrm -rf nope"
        self.assertTrue(list(self.validator("adapter-admission-recipe.schema.json").iter_errors(admission)))
        admission = load_json(examples / "adapter-admission-recipe.json")
        admission["launch"]["environmentControls"] = {
            "PRIME_AGENT_TELEMETRY": "0"
        }
        self.assertTrue(list(self.validator("adapter-admission-recipe.schema.json").iter_errors(admission)))
        omp_recipe = load_json(SHARED / "capabilities" / "adapters" / "recipes" / "omp-rpc.v1.json")
        self.assert_valid("adapter-admission-recipe.schema.json", omp_recipe)
        expected_omp_runtime = [
            {
                "runtime": "bun",
                "versionRange": ">=1.3.14",
                "repairCommand": "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
            }
        ]
        self.assertEqual(
            omp_recipe["support"]["runtimePrerequisites"], expected_omp_runtime
        )
        functional_alpha = load_json(
            SHARED / "capabilities" / "adapters" / "functional-alpha.v1.json"
        )
        omp_alpha = next(
            item
            for item in functional_alpha["adapters"]
            if item["adapterId"] == "omp/rpc"
        )
        self.assertEqual(omp_alpha["runtimePrerequisites"], expected_omp_runtime)
        self.assertEqual(
            omp_recipe["support"]["repairCommand"],
            expected_omp_runtime[0]["repairCommand"],
        )
        self.assertEqual(
            omp_alpha["repairCommand"], expected_omp_runtime[0]["repairCommand"]
        )
        for mutation in (
            lambda value: value["support"].pop("runtimePrerequisites"),
            lambda value: value["support"].update(runtimePrerequisites=[]),
            lambda value: value["support"].update(
                repairCommand="npm install --global @oh-my-pi/pi-coding-agent@18.0.9"
            ),
            lambda value: value["support"]["runtimePrerequisites"][0].update(
                runtime="node"
            ),
            lambda value: value["support"]["runtimePrerequisites"][0].update(
                versionRange=">=1.3.13"
            ),
            lambda value: value["support"]["runtimePrerequisites"][0].update(
                repairCommand="npm install --global @oh-my-pi/pi-coding-agent@18.0.9"
            ),
            lambda value: value["support"]["runtimePrerequisites"][0].update(
                repairCommand="npm install bun@1.3.14\nwhoami"
            ),
            lambda value: value["support"]["runtimePrerequisites"][0].update(
                executable="bun"
            ),
        ):
            drifted = deepcopy(omp_recipe)
            mutation(drifted)
            self.assertTrue(
                list(
                    self.validator(
                        "adapter-admission-recipe.schema.json"
                    ).iter_errors(drifted)
                )
            )
        for mutation in (
            lambda value: value["launch"]["controls"]["ownedConfigOverlay"].update(bytes="retry:\n  enabled: true\n"),
            lambda value: value["launch"]["controls"]["ownedConfigOverlay"].update(mode="0644"),
            lambda value: value["launch"]["controls"]["protocolPrelude"].update(requiredStateDataArrayLength=1),
        ):
            drifted = deepcopy(omp_recipe)
            mutation(drifted)
            self.assertTrue(drifted != omp_recipe)
        benchmark = load_json(examples / "benchmark-policy.json")
        benchmark["noOutlierDrop"] = False
        self.assertTrue(list(self.validator("benchmark-policy.schema.json").iter_errors(benchmark)))
        release = load_json(examples / "release-profile.json")
        release["image"]["purpose"] = "sentinel-transport-test"
        self.assertTrue(list(self.validator("release-profile.schema.json").iter_errors(release)))

    def test_closed_contracts_reject_unknown_fields(self) -> None:
        result = load_json(FIXTURES / "transport" / "runner-result-success.json")
        result["languageMeaning"] = "must never exist"
        self.assertTrue(list(self.validator("runner-result.schema.json").iter_errors(result)))
        invocation = load_json(FIXTURES / "transport" / "runner-invocation.json")
        invocation["semanticRouting"] = "forbidden"
        self.assertTrue(list(self.validator("runner-invocation.schema.json").iter_errors(invocation)))

    def test_success_result_cannot_carry_runner_error(self) -> None:
        result = load_json(FIXTURES / "transport" / "runner-result-success.json")
        taxonomy = load_json(SHARED / "errors" / "taxonomy.v1.json")["errors"][0]
        result["error"] = {"schema": "openprose.runner-error/1", **taxonomy}
        self.assertTrue(list(self.validator("runner-result.schema.json").iter_errors(result)))

    def test_prime_adapter_diagnostic_is_closed_and_bounded(self) -> None:
        diagnostic = {
            "schema": "openprose.adapter-diagnostic/1",
            "adapterId": "prime/rpc",
            "stage": "prime-lifecycle",
            "phase": "await-text-delta-or-end",
            "counters": {
                "acceptedRecords": 12,
                "thinkingDeltas": 3,
                "textDeltas": 1,
                "saturated": False,
            },
        }
        self.assert_valid("adapter-diagnostic.schema.json", diagnostic)
        first_content = deepcopy(diagnostic)
        first_content["phase"] = "await-thinking-or-text-start"
        self.assert_valid("adapter-diagnostic.schema.json", first_content)
        stale_first_content = deepcopy(diagnostic)
        stale_first_content["phase"] = "await-thinking-start"
        self.assertTrue(
            list(
                self.validator("adapter-diagnostic.schema.json").iter_errors(
                    stale_first_content
                )
            )
        )
        error = {
            "schema": "openprose.runner-error/1",
            "code": "PROTOCOL_MALFORMED",
            "boundary": "protocol",
            "message": "The harness protocol is malformed.",
            "action": "Retry after checking the exact harness version.",
            "exitCode": 22,
            "retryable": False,
            "details": {"adapterDiagnostic": diagnostic},
        }
        self.assert_valid("runner-error.schema.json", error)

        mutations = []
        for path, value in (
            (("adapterId",), "omp/rpc"),
            (("stage",), "candidate-stage"),
            (("phase",), "candidate-phase"),
            (("counters", "acceptedRecords"), -1),
            (("counters", "thinkingDeltas"), 4294967296),
            (("counters", "textDeltas"), "1"),
        ):
            mutated = deepcopy(diagnostic)
            target = mutated
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            mutations.append(mutated)
        extra = deepcopy(diagnostic)
        extra["candidateValue"] = "must-not-be-admitted"
        mutations.append(extra)
        mismatched_stage = deepcopy(diagnostic)
        mismatched_stage.update({"stage": "jsonl-framing", "phase": "await-agent-end"})
        mutations.append(mismatched_stage)
        for mutated in mutations:
            with self.subTest(mutated=mutated):
                self.assertTrue(
                    list(
                        self.validator("adapter-diagnostic.schema.json").iter_errors(
                            mutated
                        )
                    )
                )

    def test_sentinel_manifest_hashes_order_and_external_artifacts(self) -> None:
        manifest = load_json(SENTINEL / "manifest.json")
        self.assert_valid("skill-runtime-image-manifest.schema.json", manifest)
        self.assertEqual(manifest["purpose"], "sentinel-transport-test")
        self.assertIs(manifest["releaseEligible"], False)
        paths = [entry["path"] for entry in manifest["payload"]]
        self.assertEqual(paths, ["payload/00-sentinel.md", "payload/10-byte-canary.md"])
        self.assertEqual(len(paths), len(set(paths)))
        aggregate = hashlib.sha256()
        model_visible = bytearray()
        for entry in manifest["payload"]:
            path = entry["path"]
            pure = PurePosixPath(path)
            self.assertNotIn("..", pure.parts)
            self.assertFalse(pure.is_absolute())
            data = (SENTINEL / path).read_bytes()
            data.decode("utf-8")
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", data)
            self.assertEqual(entry["byteLength"], len(data))
            self.assertEqual(entry["sha256"], sha256(data))
            model_visible.extend(data)
            aggregate.update(path.encode("utf-8"))
            aggregate.update(b"\0")
            aggregate.update(str(len(data)).encode("ascii"))
            aggregate.update(b"\0")
            aggregate.update(data)
            aggregate.update(b"\0")
        self.assertEqual(manifest["aggregateSha256"]["algorithm"], "sha256-path-length-nul-v1")
        self.assertEqual(manifest["aggregateSha256"]["sha256"], aggregate.hexdigest())
        self.assertEqual(
            manifest["modelVisibleBytes"],
            {
                "serialization": "ordered-raw-concatenation-v1",
                "byteLength": len(model_visible),
                "sha256": sha256(bytes(model_visible)),
            },
        )
        for name in ("taskEnvelope", "terminalEnvelope", "oneFieldFraming"):
            artifact = manifest[name]
            self.assertEqual(artifact["sha256"], sha256((SENTINEL / artifact["path"]).read_bytes()))
        placements = manifest["instructionPlacements"]
        self.assertEqual([item["id"] for item in placements], ["developer", "system-append", "user-prefix-framed"])
        self.assertEqual([item["strictness"] for item in placements], ["strict", "strict", "degraded"])

    def test_sentinel_can_never_be_marked_release_eligible(self) -> None:
        manifest = load_json(SENTINEL / "manifest.json")
        manifest["releaseEligible"] = True
        self.assertTrue(list(self.validator("skill-runtime-image-manifest.schema.json").iter_errors(manifest)))
        manifest = load_json(SENTINEL / "manifest.json")
        manifest["payload"][0]["path"] = "payload/../outside.md"
        self.assertTrue(list(self.validator("skill-runtime-image-manifest.schema.json").iter_errors(manifest)))

    def test_sentinel_external_schemas_validate_their_fixtures(self) -> None:
        task_schema = load_json(SENTINEL / "contracts" / "task-envelope.schema.json")
        terminal_schema = load_json(SENTINEL / "contracts" / "terminal-envelope.schema.json")
        jsonschema.Draft202012Validator.check_schema(task_schema)
        jsonschema.Draft202012Validator.check_schema(terminal_schema)
        jsonschema.Draft202012Validator(task_schema).validate(load_json(FIXTURES / "transport" / "sentinel-task.json"))
        leaked_task = load_json(FIXTURES / "transport" / "sentinel-task.json")
        leaked_task["harness"] = "must-stay-out-of-band"
        self.assertTrue(list(jsonschema.Draft202012Validator(task_schema).iter_errors(leaked_task)))
        jsonschema.Draft202012Validator(terminal_schema).validate(
            {
                "schema": "openprose.sentinel-terminal-envelope/1",
                "semanticStatus": "not-applicable",
                "marker": "OPENPROSE_SENTINEL_TERMINAL_V1",
            }
        )
        semantic_claim = {
            "schema": "openprose.sentinel-terminal-envelope/1",
            "semanticStatus": "success",
            "marker": "OPENPROSE_SENTINEL_TERMINAL_V1",
        }
        self.assertTrue(
            list(jsonschema.Draft202012Validator(terminal_schema).iter_errors(semantic_claim))
        )

    def test_transport_fixture_digests_are_internally_consistent(self) -> None:
        task = load_json(FIXTURES / "transport" / "sentinel-task.json")
        invocation = load_json(FIXTURES / "transport" / "runner-invocation.json")
        adapter = load_json(FIXTURES / "transport" / "mock-adapter.json")
        result = load_json(FIXTURES / "transport" / "runner-result-success.json")
        self.assertEqual(invocation["task"], task)
        self.assertEqual(invocation["taskDigestSha256"], sha256(canonical_json(task)))
        self.assertEqual(result["digests"]["taskSha256"], sha256(canonical_json(task)))
        self.assertEqual(result["digests"]["invocationSha256"], sha256(canonical_json(invocation)))
        self.assertEqual(result["adapter"]["descriptorDigestSha256"], sha256(canonical_json(adapter)))
        self.assertEqual(result["cwd"]["identitySha256"], sha256(result["cwd"]["path"].encode("utf-8")))
        terminal = {
            "schema": "openprose.sentinel-terminal-envelope/1",
            "semanticStatus": "not-applicable",
            "marker": "OPENPROSE_SENTINEL_TERMINAL_V1",
        }
        self.assertEqual(result["semantic"]["terminalEnvelopeDigestSha256"], sha256(canonical_json(terminal)))
        event_bytes = b""
        sequences = []
        for line in (FIXTURES / "transport" / "normalized-events-preterminal.jsonl").read_text("utf-8").splitlines():
            event = json.loads(line)
            event_bytes += canonical_json(event) + b"\n"
            sequences.append(event["sequence"])
        self.assertEqual(sequences, list(range(len(sequences))))
        self.assertEqual(result["digests"]["normalizedEventsSha256"], sha256(event_bytes))
        terminal_event = load_json(FIXTURES / "transport" / "runner-completed-event.json")
        self.assertEqual(terminal_event["sequence"], len(sequences))
        self.assertEqual(terminal_event["payload"]["result"], result)
        mismatched = deepcopy(terminal_event)
        mismatched["type"] = "runner.failed"
        self.assertTrue(list(self.validator("normalized-event.schema.json").iter_errors(mismatched)))

    def test_error_taxonomy_is_complete_unique_and_schema_valid(self) -> None:
        taxonomy = load_json(SHARED / "errors" / "taxonomy.v1.json")
        self.assertEqual(taxonomy["schema"], "openprose.runner-error-taxonomy/1")
        expected_codes = {
            "CONFIG_INVALID", "INVOCATION_INVALID", "HARNESS_UNAVAILABLE",
            "HARNESS_INCOMPATIBLE",
            "HARNESS_NEEDS_AUTH", "TRANSPORT_UNSUPPORTED",
            "PROMPT_CHANNEL_UNSUPPORTED", "IMAGE_INVALID", "IMAGE_TOO_LARGE",
            "RECURSIVE_INVOCATION", "STARTUP_TIMEOUT", "PROTOCOL_MALFORMED",
            "PROTOCOL_TRUNCATED", "HARNESS_FAILED", "SEMANTIC_STATUS_UNKNOWN",
            "CANCELLED", "PROCESS_CLEANUP_FAILED", "HOSTED_UNAVAILABLE",
            "HOSTED_AUTH_REQUIRED", "HOSTED_QUOTA_EXCEEDED", "INTERNAL_ERROR",
            "SERVICE_UNAVAILABLE", "SERVICE_AUTH_REQUIRED", "SERVICE_PROTOCOL_INVALID",
            "CREDENTIAL_STORE_UNAVAILABLE", "DEVICE_AUTH_FAILED", "DEVICE_AUTH_EXPIRED",
            # Hosted service operations (additive).
            "CONFIRMATION_REQUIRED", "SERVICE_REQUEST_REJECTED", "SERVICE_RESOURCE_NOT_FOUND",
            "SERVICE_FEATURE_DISABLED", "SERVICE_BALANCE_INSUFFICIENT", "SERVICE_PREMIUM_MODEL_LOCKED", "SERVICE_ACCOUNT_SUSPENDED",
            "SERVICE_WRITE_CONFLICT", "GITHUB_LINK_REQUIRED", "SERVICE_RESPONSE_TOO_LARGE",
            "SERVICE_WATCH_DEADLINE", "HOSTED_RUN_FAILED", "RUN_SUBMISSION_AMBIGUOUS",
            "HOSTED_RUN_DETACHED", "HOSTED_RUN_CANCELLED", "EXAMPLE_NOT_VIEWABLE",
        }
        records = taxonomy["errors"]
        self.assertEqual({record["code"] for record in records}, expected_codes)
        self.assertEqual(len(records), len(expected_codes))
        for record in records:
            self.assert_valid("runner-error.schema.json", {"schema": "openprose.runner-error/1", **record})
            self.assertNotIn("prose cli", record["action"])
            self.assertNotIn("$PROSE", record["action"])
        cleanup = next(record for record in records if record["code"] == "PROCESS_CLEANUP_FAILED")
        self.assertIn("`cli doctor` runner operation", cleanup["action"])
        configuration = next(
            record for record in records if record["code"] == "CONFIG_INVALID"
        )
        invocation = next(
            record for record in records if record["code"] == "INVOCATION_INVALID"
        )
        self.assertEqual("configuration", configuration["boundary"])
        self.assertEqual("invocation", invocation["boundary"])
        self.assertNotEqual(configuration["action"], invocation["action"])
        for record, wrong_boundary in (
            (configuration, "invocation"),
            (invocation, "configuration"),
        ):
            mismatched = {
                "schema": "openprose.runner-error/1",
                **record,
                "boundary": wrong_boundary,
            }
            self.assertTrue(
                list(self.validator("runner-error.schema.json").iter_errors(mismatched))
            )
        harness_auth = next(
            record for record in records if record["code"] == "HARNESS_NEEDS_AUTH"
        )
        self.assertIn("for Claude, run `claude auth login`", harness_auth["action"])
        self.assertNotIn("enter `/login`", harness_auth["action"])

    def test_all_case_manifests_validate_and_error_actions_are_frozen(self) -> None:
        case_schema = load_json(CASES / "case-manifest.schema.json")
        jsonschema.Draft202012Validator.check_schema(case_schema)
        hosted_case_schema = load_json(CASES / "hosted" / "hosted-case-manifest.schema.json")
        jsonschema.Draft202012Validator.check_schema(hosted_case_schema)
        validators = {
            "openprose.runner-case/1": jsonschema.Draft202012Validator(
                case_schema, format_checker=jsonschema.FormatChecker()
            ),
            "openprose.hosted-oracle-case/1": jsonschema.Draft202012Validator(
                hosted_case_schema, format_checker=jsonschema.FormatChecker()
            ),
        }
        taxonomy = {
            record["code"]: record
            for record in load_json(SHARED / "errors" / "taxonomy.v1.json")["errors"]
        }
        cases = []
        for path in sorted(CASES.rglob("*.json")):
            case = load_json(path)
            validator = validators.get(case.get("schema"))
            if validator is None:
                continue
            errors = sorted(validator.iter_errors(case), key=lambda error: list(error.path))
            self.assertEqual([], [f"{path}: {list(error.path)}: {error.message}" for error in errors])
            cases.append(case)
            if case["schema"] == "openprose.runner-case/1":
                stdout = case["expected"]["stdout"]
                if stdout["kind"] == "exact-fixture":
                    fixture = CLI.parent / stdout["fixture"]
                    self.assertTrue(fixture.is_file(), fixture)
                if "errorCode" in case["expected"]:
                    frozen = taxonomy[case["expected"]["errorCode"]]
                    self.assertEqual(case["expected"]["exitCode"], frozen["exitCode"])
                    self.assertEqual(case["expected"]["errorAction"], frozen["action"])
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue({"core.initial-help", "core.opaque-argv", "core.mock-success", "core.openprose-hosted-unavailable"}.issubset(ids))

    def test_adversarial_scenario_catalog_covers_fake_harness_contract(self) -> None:
        catalog = load_json(CLI / "conformance" / "adversarial" / "transport" / "fake-scenario-expectations.json")
        expected = {
            "success", "malformed", "truncated", "eof-without-terminal",
            "nonzero", "terminal-nonzero", "delay", "stderr", "fragmented",
            "crlf", "duplicate-terminal", "reordered", "descendant",
        }
        records = catalog["scenarios"]
        self.assertEqual({record["scenario"] for record in records}, expected)
        self.assertEqual(len(records), len(expected))
        for record in records:
            if "errorCode" in record:
                self.assertIn(
                    record["errorCode"],
                    {item["code"] for item in load_json(SHARED / "errors" / "taxonomy.v1.json")["errors"]},
                )

    def test_transport_limits_forbid_silent_drop(self) -> None:
        limits = load_json(SHARED / "capabilities" / "transport-limits.v1.json")
        self.assertEqual(limits["schema"], "openprose.transport-limits/1")
        self.assertIs(limits["silentDropPermitted"], False)
        self.assertLess(limits["maxQueuedRecords"], limits["maxRecordBytes"])
        self.assertLess(limits["maxRecordBytes"], limits["maxAggregateStdoutBytes"])
        argv = limits["argv"]
        self.assertEqual(argv["maxItems"], 64)
        self.assertLess(argv["posix"]["maxArgumentBytes"], argv["posix"]["maxTotalBytes"])
        self.assertLess(argv["windows"]["maxArgumentUtf16CodeUnits"], argv["windows"]["maxTotalChargedUtf16CodeUnits"])

    def test_adapter_credential_requirements_are_closed_and_complete(self) -> None:
        oracle = load_json(SHARED / "capabilities" / "adapters" / "oracle.v1.json")
        for adapter in oracle["adapters"]:
            groups = adapter["credentialGroups"]
            requirements = adapter["credentialRequirements"]
            self.assertEqual(set(requirements), set(groups), adapter["adapterId"])
            for group, requirement in requirements.items():
                self.assertIn(requirement["kind"], {"probe-owned", "any-nonempty"})
                if requirement["kind"] == "probe-owned":
                    self.assertEqual(set(requirement), {"kind"})
                    continue
                self.assertEqual(set(requirement), {"kind", "alternatives"})
                self.assertTrue(requirement["alternatives"])
                for alternative in requirement["alternatives"]:
                    self.assertTrue(alternative)
                    self.assertTrue(set(alternative).issubset(groups[group]))
        for adapter in oracle["adapters"]:
            if adapter["adapterId"] not in {"prime/rpc", "omp/rpc"}:
                continue
            requirements = adapter["credentialRequirements"]
            self.assertEqual(
                requirements["aws-bedrock"]["alternatives"],
                [["AWS_PROFILE"], ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]],
            )
            self.assertEqual(
                requirements["google"]["alternatives"],
                [["GEMINI_API_KEY"], ["GOOGLE_APPLICATION_CREDENTIALS"]],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
