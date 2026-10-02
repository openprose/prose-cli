#!/usr/bin/env python3
"""Provider-free black-box adversary for Rust and Bun installed adapters."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import platform
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from typing import Any

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError as error:  # pragma: no cover
    raise SystemExit(
        "Install cli/shared/requirements-test.txt before running product adversary tests."
    ) from error


HERE = Path(__file__).resolve().parent
CLI = HERE.parents[2]
SHARED = CLI / "shared"
SCHEMAS = SHARED / "schemas"
ORACLE_PATH = SHARED / "capabilities" / "adapters" / "oracle.v1.json"
RECIPES = SHARED / "capabilities" / "adapters" / "recipes"
SCENARIOS = SHARED / "fixtures" / "adapters" / "scenarios"
CASES = CLI / "conformance" / "cases" / "adapters"
ERRORS_PATH = SHARED / "errors" / "taxonomy.v1.json"
IMAGE_ROOT = SHARED / "image" / "echo-v0"
IMAGE_MANIFEST = IMAGE_ROOT / "manifest.json"
LIVE_HARNESS_SOURCE = HERE / "fake_live_harness.py"
INVARIANTS_PATH = HERE / "invariants.v1.json"
RUNNER_METADATA_PATH = (
    SHARED / "fixtures" / "adapters" / "product" / "runner-metadata-env.v1.json"
)

PRODUCT_DEFAULTS = {
    "rust": CLI / "rust" / "target" / "openprose-adapter-fixture" / "debug" / "prose",
    "bun": CLI / "bun" / "dist" / "prose-adapter-fixture",
}
PRODUCT_OVERRIDES = {
    "rust": "OPENPROSE_RUST_BIN",
    "bun": "OPENPROSE_BUN_BIN",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_fixture_products() -> None:
    """Build exact opaque echo inputs without test seams or ambient overrides."""
    allowed = (
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SystemRoot",
        "WINDIR",
        "PATHEXT",
        "RUSTUP_HOME",
        "CARGO_HOME",
        "RUSTUP_TOOLCHAIN",
        "DEVELOPER_DIR",
        "SDKROOT",
        "LANG",
        "LC_ALL",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    with tempfile.TemporaryDirectory(prefix="openprose-adapter-image-") as temporary:
        bundle = Path(temporary) / "echo.bundle.bin"
        checksum = Path(temporary) / "echo.bundle.sha256"
        subprocess.run(
            [
                sys.executable,
                str(SHARED / "image/bundle/image_bundle.py"),
                "build",
                str(IMAGE_ROOT),
                str(bundle),
                "--checksum",
                str(checksum),
            ],
            cwd=CLI.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            check=True,
            timeout=120,
        )
        build_environment = {
            **environment,
            "OPENPROSE_IMAGE_SOURCE_DIR": str(IMAGE_ROOT),
            "OPENPROSE_IMAGE_BUNDLE": str(bundle),
            "OPENPROSE_IMAGE_BUNDLE_CHECKSUM": str(checksum),
        }
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--offline",
                "-p",
                "prose-cli",
                "--bin",
                "prose",
                "--target-dir",
                str(PRODUCT_DEFAULTS["rust"].parents[1]),
            ],
            cwd=CLI / "rust",
            env=build_environment,
            stdin=subprocess.DEVNULL,
            check=True,
            timeout=600,
        )
        subprocess.run(
            [
                "bun",
                "--no-env-file",
                f"--config={CLI / 'bun/config/empty-bunfig.toml'}",
                "run",
                str(CLI / "bun/scripts/image-bundle.ts"),
                "build",
                "--image-dir",
                str(IMAGE_ROOT),
                "--bundle",
                str(bundle),
                "--checksum",
                str(checksum),
                "--outfile",
                str(PRODUCT_DEFAULTS["bun"]),
            ],
            cwd=CLI / "bun",
            env=build_environment,
            stdin=subprocess.DEVNULL,
            check=True,
            timeout=120,
        )


class AdapterProductAdversary(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.oracle = load_json(ORACLE_PATH)
        cls.adapters = {entry["adapterId"]: entry for entry in cls.oracle["adapters"]}
        cls.recipes = {
            recipe["adapterId"]: recipe
            for recipe in (load_json(path) for path in sorted(RECIPES.glob("*.json")))
        }
        # Ordinary products use the current native developer channel; framed
        # sentinel transport remains covered by the separate shared fixture lane.
        cls.recipes["codex/exec-json"] = load_json(
            RECIPES / "codex-exec-json-developer.v1.json"
        )
        cls.scenarios = {
            scenario["adapterId"]: scenario
            for scenario in (
                load_json(path) for path in sorted(SCENARIOS.glob("*.json"))
            )
        }
        cls.cases = {
            cls.case_adapter_id(case): case
            for case in (
                load_json(path)
                for path in sorted(CASES.glob("*-functional-alpha.json"))
            )
        }
        taxonomy = load_json(ERRORS_PATH)
        cls.errors = {entry["code"]: entry for entry in taxonomy["errors"]}
        cls.manifest = load_json(IMAGE_MANIFEST)
        cls.runner_metadata = load_json(RUNNER_METADATA_PATH)
        cls.image_bytes = b"".join(
            (IMAGE_ROOT / entry["path"]).read_bytes()
            for entry in cls.manifest["payload"]
        )
        schemas = {
            path.name: load_json(path) for path in sorted(SCHEMAS.glob("*.schema.json"))
        }
        registry = Registry()
        for schema in schemas.values():
            registry = registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )
        cls.result_validator = jsonschema.Draft202012Validator(
            schemas["runner-result.schema.json"],
            registry=registry,
            format_checker=jsonschema.FormatChecker(),
        )
        cls.dry_run_validator = jsonschema.Draft202012Validator(
            schemas["runner-dry-run-report.schema.json"],
            registry=registry,
            format_checker=jsonschema.FormatChecker(),
        )
        cls.event_validator = jsonschema.Draft202012Validator(
            schemas["normalized-event.schema.json"],
            registry=registry,
            format_checker=jsonschema.FormatChecker(),
        )

        cls.echo_manifest = cls.manifest
        cls.echo_image_bytes = cls.image_bytes
        cls.echo_framing = (
            IMAGE_ROOT / cls.echo_manifest["oneFieldFraming"]["path"]
        ).read_text("utf-8")

        cls.products: dict[str, Path] = {}
        missing = []
        for name, default in PRODUCT_DEFAULTS.items():
            configured = os.environ.get(PRODUCT_OVERRIDES[name])
            path = Path(configured).resolve() if configured else default.resolve()
            if path.is_file():
                cls.products[name] = path
            else:
                missing.append(f"{name}={path}")
        if (
            missing
            and os.environ.get("OPENPROSE_ADVERSARY_ALLOW_MISSING_PRODUCTS") != "1"
        ):
            raise AssertionError(
                "missing required product artifact(s): " + ", ".join(missing)
            )
        cls.missing_products = missing

        expected_ids = {
            "codex/exec-json",
            "claude/print-stream-json",
            "prime/rpc",
            "omp/rpc",
        }
        cohort = load_json(
            SHARED / "capabilities" / "adapters" / "functional-alpha.v1.json"
        )
        cohort_ids = {entry["adapterId"] for entry in cohort["adapters"]}
        if not (
            cohort_ids == set(cls.cases) == expected_ids
            and cohort_ids <= set(cls.adapters)
            and cohort_ids <= set(cls.recipes)
            and cohort_ids <= set(cls.scenarios)
        ):
            raise AssertionError(
                "installed adapter authorities do not match the shared cohort"
            )
        # Additional oracle-only adapters are tested separately. In particular,
        # the SDK fixture must not silently become an installed-admission claim.
        cls.adapters = {
            key: value for key, value in cls.adapters.items() if key in cohort_ids
        }
        cls.recipes = {
            key: value for key, value in cls.recipes.items() if key in cohort_ids
        }
        cls.scenarios = {
            key: value for key, value in cls.scenarios.items() if key in cohort_ids
        }

    def supports_host(self, adapter_id: str) -> bool:
        os_id = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(platform.system(), platform.system().lower())
        arch = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
        candidates = {f"{os_id}-{arch}"}
        if os_id == "linux":
            libc = "musl" if platform.libc_ver()[0] == "musl" else "gnu"
            candidates.add(f"linux-{arch}-{libc}")
            if libc == "gnu":
                candidates.add(f"linux-{arch}-musl")
        return bool(candidates.intersection(self.recipes[adapter_id]["support"]["platforms"]))

    @staticmethod
    def case_adapter_id(case: dict[str, Any]) -> str:
        argv = case["invocation"]["argv"]
        harness = argv[argv.index("--harness") + 1]
        transport = argv[argv.index("--transport") + 1]
        return f"{harness}/{transport}"

    def available_products(self):
        if not self.products:
            self.skipTest(
                "locally built Rust/Bun product artifacts are absent: "
                + ", ".join(self.missing_products)
            )
        return self.products.items()

    def all_credential_names(self) -> set[str]:
        names = set(self.oracle["environmentRules"]["alwaysStrip"])
        for adapter in self.adapters.values():
            for group in adapter["credentialGroups"].values():
                names.update(group)
            names.update(adapter.get("forcedStrips", []))
        names.update(
            {
                "PRIME_AGENT_CODING_AGENT_DIR",
                "PI_CODING_AGENT_DIR",
                "PRIME_AGENT_TELEMETRY",
            }
        )
        return names

    @staticmethod
    def auth_profile(adapter_id: str) -> str:
        return {
            "codex/exec-json": "cached-chatgpt-login",
            "claude/print-stream-json": "claude-subscription",
            "prime/rpc": "openrouter",
            "omp/rpc": "openrouter",
        }[adapter_id]

    @staticmethod
    def configured_argv(adapter_id: str, argv: list[str]) -> list[str]:
        if adapter_id in {"prime/rpc", "omp/rpc"} and "--model" not in argv:
            raise AssertionError(
                "Prime/OMP product cases must carry one explicit model"
            )
        return list(argv)

    def make_environment(
        self,
        temporary: Path,
        trap_directory: Path,
        additions: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        home = temporary / "home"
        config = temporary / "config"
        cache = temporary / "cache"
        scratch = temporary / "tmp"
        for path in (home, config, cache, scratch):
            path.mkdir(parents=True, exist_ok=True)
        environment = {
            "PATH": str(trap_directory),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "TMPDIR": str(scratch),
            "LANG": "C",
            "LC_ALL": "C",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
        if os.name == "nt":  # pragma: no cover - exercised by Windows CI
            for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE"):
                if os.environ.get(name):
                    environment[name] = os.environ[name]
            environment["TEMP"] = str(scratch)
            environment["TMP"] = str(scratch)
        canaries = {
            name: f"OPENPROSE-SECRET-CANARY::{name}::f4471f"
            for name in sorted(self.all_credential_names())
        }
        environment.update(canaries)
        if additions:
            environment.update(additions)
        return environment, canaries

    def run_product(
        self,
        product: Path,
        argv: list[str],
        workspace: Path,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(product), *argv],
            cwd=workspace,
            env=environment,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def assert_no_secrets(
        self, completed: subprocess.CompletedProcess[bytes], canaries: dict[str, str]
    ) -> None:
        combined = completed.stdout + completed.stderr
        for value in canaries.values():
            self.assertNotIn(value.encode(), combined)
        for marker in (
            b"OPENPROSE_SENTINEL_IMAGE_V1",
            b"OPENPROSE_ECHO_IMAGE_V0",
            b"OPENPROSE_ECHO_TERMINAL_V0",
            b"${NOT_INTERPOLATED}",
        ):
            self.assertNotIn(marker, combined)

    def parse_single_json(self, completed: subprocess.CompletedProcess[bytes]) -> Any:
        self.assertTrue(completed.stdout.endswith(b"\n"), completed.stdout)
        self.assertEqual(1, len(completed.stdout.splitlines()), completed.stdout)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                f"stdout is not exactly one JSON value: {error}: {completed.stdout!r}"
            )

    def assert_valid_result(self, result: Any) -> None:
        failures = sorted(
            self.result_validator.iter_errors(result), key=lambda item: list(item.path)
        )
        self.assertEqual(
            [], [f"{list(error.path)}: {error.message}" for error in failures]
        )

    def assert_blocked_result(
        self,
        result: dict[str, Any],
        adapter_id: str,
        expected_error: dict[str, Any],
    ) -> None:
        recipe = self.recipes[adapter_id]
        self.assertEqual(adapter_id, result["adapter"]["id"])
        self.assertEqual(adapter_id.split("/", 1)[1], result["transport"])
        self.assertEqual(recipe["billingOwner"], result["billing"]["owner"])
        self.assertEqual(recipe["auth"]["category"], result["billing"]["authCategory"])
        self.assertEqual("unknown", result["semantic"]["status"])
        self.assertFalse(result["terminal"]["transportCompleted"])
        self.assertFalse(result["terminal"]["terminalEventObserved"])
        self.assertIsNone(result["terminal"]["exitCode"])
        self.assertIsNone(result["terminal"]["signal"])
        self.assertIsNone(result["digests"].get("deliveredImageSha256"))
        self.assertEqual(expected_error["errorCode"], result["error"]["code"])
        self.assertEqual(expected_error["errorAction"], result["error"]["action"])
        self.assertEqual(expected_error["exitCode"], result["runnerExitCode"])
        catalog = self.errors[expected_error["errorCode"]]
        self.assertEqual(catalog["boundary"], result["error"]["boundary"])
        self.assertEqual(catalog["message"], result["error"]["message"])
        self.assertEqual(catalog["retryable"], result["error"]["retryable"])

    def test_missing_installed_harnesses_fail_without_fallback_or_spawn(self) -> None:
        for product_name, product in self.available_products():
            for adapter_id, case in self.cases.items():
                error_code = "HARNESS_UNAVAILABLE" if self.supports_host(adapter_id) else "HARNESS_INCOMPATIBLE"
                for repetition in range(3):
                    with self.subTest(
                        product=product_name,
                        adapter=adapter_id,
                        repetition=repetition,
                    ), tempfile.TemporaryDirectory(
                        prefix="openprose-adversary-"
                    ) as root:
                        temporary = Path(root)
                        workspace = temporary / "workspace with spaces 雪"
                        workspace.mkdir()
                        trap_directory = temporary / "empty-path"
                        trap_directory.mkdir()
                        environment, canaries = self.make_environment(
                            temporary,
                            trap_directory,
                            {"PROSE_AUTH_PROFILE": self.auth_profile(adapter_id)},
                        )
                        completed = self.run_product(
                            product,
                            self.configured_argv(
                                adapter_id, case["invocation"]["argv"]
                            ),
                            workspace,
                            environment,
                        )
                        self.assertEqual(10, completed.returncode)
                        self.assertEqual(b"", completed.stderr)
                        self.assert_no_secrets(completed, canaries)
                        result = self.parse_single_json(completed)
                        self.assert_valid_result(result)
                        self.assert_blocked_result(
                            result,
                            adapter_id,
                            {
                                "errorCode": error_code,
                                "errorAction": self.errors[error_code][
                                    "action"
                                ],
                                "exitCode": 10,
                            },
                        )

    def test_auto_selects_only_declared_baseline_without_fallback(self) -> None:
        for product_name, product in self.available_products():
            for adapter_id, case in self.cases.items():
                if not self.supports_host(adapter_id):
                    continue
                with self.subTest(
                    product=product_name, adapter=adapter_id
                ), tempfile.TemporaryDirectory(prefix="openprose-adversary-") as root:
                    temporary = Path(root)
                    workspace = temporary / "workspace"
                    workspace.mkdir()
                    traps = temporary / "empty-path"
                    traps.mkdir()
                    environment, canaries = self.make_environment(
                        temporary,
                        traps,
                        {"PROSE_AUTH_PROFILE": self.auth_profile(adapter_id)},
                    )
                    argv = list(case["invocation"]["argv"])
                    argv[argv.index("--transport") + 1] = "auto"
                    argv = self.configured_argv(adapter_id, argv)
                    completed = self.run_product(product, argv, workspace, environment)
                    self.assertEqual(10, completed.returncode)
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    result = self.parse_single_json(completed)
                    self.assert_valid_result(result)
                    self.assert_blocked_result(
                        result,
                        adapter_id,
                        {
                            "errorCode": "HARNESS_UNAVAILABLE",
                            "errorAction": self.errors["HARNESS_UNAVAILABLE"]["action"],
                            "exitCode": 10,
                        },
                    )

    def harness_entries(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        entries = report.get("harnesses")
        self.assertIsInstance(entries, list, report)
        return entries

    def assert_listing_entry(
        self,
        entry: dict[str, Any],
        harness: str,
        transport: str,
        recipe: dict[str, Any],
    ) -> None:
        self.assertEqual("installed-process", entry.get("runtime"), entry)
        self.assertEqual(recipe["billingOwner"], entry.get("billingOwner"), entry)
        self.assertEqual(recipe["auth"]["category"], entry.get("authCategory"), entry)
        self.assertIn(transport, entry.get("transports", []), entry)
        self.assertIs(entry.get("testOnly"), False, entry)
        self.assertIs(entry.get("strictWrapperConformant"), False, entry)
        availability = entry.get("availability")
        if isinstance(availability, str):
            self.assertEqual("missing" if self.supports_host(f"{harness}/{transport}") else "incompatible", availability, entry)
        elif "available" in entry:
            self.assertIs(entry["available"], False, entry)

    def test_harness_list_names_every_missing_adapter_honestly(self) -> None:
        for product_name, product in self.available_products():
            with self.subTest(product=product_name), tempfile.TemporaryDirectory(
                prefix="openprose-adversary-"
            ) as root:
                temporary = Path(root)
                workspace = temporary / "workspace"
                workspace.mkdir()
                traps = temporary / "empty-path"
                traps.mkdir()
                environment, canaries = self.make_environment(temporary, traps)
                completed = self.run_product(
                    product,
                    ["cli", "harness", "list", "--json"],
                    workspace,
                    environment,
                )
                self.assertEqual(0, completed.returncode)
                self.assertEqual(b"", completed.stderr)
                self.assert_no_secrets(completed, canaries)
                report = self.parse_single_json(completed)
                self.assertEqual("openprose.harness-list/1", report.get("schema"))
                entries = self.harness_entries(report)
                ids = [entry.get("id") for entry in entries]
                self.assertEqual(len(ids), len(set(ids)), ids)
                for adapter_id, recipe in self.recipes.items():
                    harness, transport = adapter_id.split("/", 1)
                    matches = [entry for entry in entries if entry.get("id") == harness]
                    self.assertEqual(1, len(matches), (harness, entries))
                    self.assert_listing_entry(matches[0], harness, transport, recipe)

    def selected_doctor_facts(
        self, report: dict[str, Any], harness: str
    ) -> dict[str, Any]:
        selected = report.get("harness")
        if isinstance(selected, dict) and selected.get("id") == harness:
            return {
                **selected,
                "transport": report.get("transport"),
                "billingOwner": report.get("billingOwner"),
                "authCategory": report.get("authCategory"),
                "problems": report.get("problems", []),
            }
        entries = report.get("harnesses", [])
        match = next((entry for entry in entries if entry.get("id") == harness), {})
        return {
            **match,
            "transport": report.get("selectedTransport"),
            "billingOwner": report.get("billingOwner", match.get("billingOwner")),
            "authCategory": report.get("authCategory", match.get("authCategory")),
            "problems": report.get("problems")
            or ([match.get("problem")] if match.get("problem") else []),
        }

    def test_doctor_reports_selected_missing_adapter(self) -> None:
        for product_name, product in self.available_products():
            for adapter_id, case in self.cases.items():
                if not self.supports_host(adapter_id):
                    continue
                with self.subTest(
                    product=product_name, adapter=adapter_id
                ), tempfile.TemporaryDirectory(prefix="openprose-adversary-") as root:
                    temporary = Path(root)
                    workspace = temporary / "workspace"
                    workspace.mkdir()
                    traps = temporary / "empty-path"
                    traps.mkdir()
                    environment, canaries = self.make_environment(
                        temporary,
                        traps,
                        {"PROSE_AUTH_PROFILE": self.auth_profile(adapter_id)},
                    )
                    harness, transport = adapter_id.split("/", 1)
                    globals_ = (
                        ["--model", "fixture/model"]
                        if adapter_id in {"prime/rpc", "omp/rpc"}
                        else []
                    )
                    completed = self.run_product(
                        product,
                        [
                            "--harness",
                            harness,
                            "--transport",
                            transport,
                            *globals_,
                            "cli",
                            "doctor",
                            "--json",
                        ],
                        workspace,
                        environment,
                    )
                    self.assertEqual(10, completed.returncode)
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    report = self.parse_single_json(completed)
                    self.assertIn(
                        report.get("schema"),
                        {"openprose.doctor/1", "openprose.doctor-report/1"},
                    )
                    self.assertIs(report.get("ready"), False)
                    facts = self.selected_doctor_facts(report, harness)
                    self.assertEqual(transport, facts.get("transport"), report)
                    self.assertEqual(
                        self.recipes[adapter_id]["billingOwner"],
                        facts.get("billingOwner"),
                        report,
                    )
                    self.assertEqual(
                        self.recipes[adapter_id]["auth"]["category"],
                        facts.get("authCategory"),
                        report,
                    )
                    self.assertTrue(facts.get("problems"), report)
                    problem_codes = {
                        problem if isinstance(problem, str) else problem.get("code")
                        for problem in facts["problems"]
                    }
                    self.assertIn("HARNESS_UNAVAILABLE", problem_codes, report)

    def test_dry_run_reports_selection_billing_and_missing_readiness(self) -> None:
        isolation = {"advisory": "partial", "unsupported": "unsupported"}
        for product_name, product in self.available_products():
            for adapter_id, case in self.cases.items():
                if not self.supports_host(adapter_id):
                    continue
                with self.subTest(
                    product=product_name, adapter=adapter_id
                ), tempfile.TemporaryDirectory(prefix="openprose-adversary-") as root:
                    temporary = Path(root)
                    workspace = temporary / "workspace"
                    workspace.mkdir()
                    traps = temporary / "empty-path"
                    traps.mkdir()
                    environment, canaries = self.make_environment(
                        temporary,
                        traps,
                        {"PROSE_AUTH_PROFILE": self.auth_profile(adapter_id)},
                    )
                    harness, transport = adapter_id.split("/", 1)
                    globals_ = (
                        ["--model", "fixture/model"]
                        if adapter_id in {"prime/rpc", "omp/rpc"}
                        else []
                    )
                    completed = self.run_product(
                        product,
                        [
                            "--harness",
                            harness,
                            "--transport",
                            transport,
                            *globals_,
                            "--output",
                            "json",
                            "--dry-run",
                            "run",
                            "fixture.prose.md",
                        ],
                        workspace,
                        environment,
                    )
                    self.assertEqual(10, completed.returncode)
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    report = self.parse_single_json(completed)
                    failures = sorted(
                        self.dry_run_validator.iter_errors(report),
                        key=lambda item: list(item.path),
                    )
                    self.assertEqual(
                        [],
                        [f"{list(error.path)}: {error.message}" for error in failures],
                    )
                    self.assertIs(report["wouldStartModel"], False)
                    self.assertEqual(harness, report["selection"]["harness"])
                    self.assertEqual(transport, report["selection"]["transport"])
                    self.assertEqual(adapter_id, report["selection"]["adapterId"])
                    recipe = self.recipes[adapter_id]
                    self.assertEqual(
                        recipe["launch"]["instructionPlacement"]["strictness"],
                        report["prompt"]["strictness"],
                    )
                    self.assertEqual(
                        isolation[recipe["isolation"]["guarantee"]], report["isolation"]
                    )
                    self.assertEqual(
                        recipe["auth"]["category"], report["auth"]["category"]
                    )
                    self.assertEqual(recipe["billingOwner"], report["billingOwner"])
                    self.assertEqual("blocked", report["readiness"])
                    self.assertEqual(
                        "HARNESS_UNAVAILABLE", report["blockingError"]["code"]
                    )
                    self.assertEqual(
                        self.errors["HARNESS_UNAVAILABLE"]["action"],
                        report["blockingError"]["action"],
                    )

    def install_live_harnesses(
        self, directory: Path, version_overrides: dict[str, str] | None = None
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        source = LIVE_HARNESS_SOURCE.read_bytes()
        defaults = {
            "codex": "codex-cli 0.149.0-alpha.4.1",
            "claude": "2.1.243 (Claude Code)",
            "prime-agent": "prime-agent 0.7.0",
            "omp": "omp/18.0.9",
        }
        for name, version in (version_overrides or {}).items():
            source = source.replace(
                json.dumps(defaults[name]).encode("utf-8"),
                json.dumps(version).encode("utf-8"),
                1,
            )
        for executable_name in ("codex", "claude", "prime-agent", "omp"):
            executable = directory / executable_name
            executable.write_bytes(source)
            executable.chmod(0o700)
        python_link = directory / "python3"
        python_link.symlink_to(Path(sys.executable).resolve())
        bun = directory / "bun"
        bun.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if sys.argv[1:] != ['--version']:\n"
            "    raise SystemExit(64)\n"
            "print('1.3.14')\n",
            encoding="utf-8",
        )
        bun.chmod(0o700)

    def expected_live_argv(
        self,
        recipe: dict[str, Any],
        observation: dict[str, Any],
        workspace: Path,
        task_bytes: bytes,
        model: str,
    ) -> list[str]:
        tokens = list(recipe["launch"]["argv"])
        for optional in recipe["launch"]["optionalArgv"]:
            self.assertEqual("model-present", optional["when"])
            insertion = len(tokens)
            if optional["placement"] == "before-final-argument":
                insertion -= 1
            elif optional["placement"] == "before-final-pair":
                insertion -= 2
            tokens[insertion:insertion] = optional["argv"]

        observed = observation["argv"]
        self.assertEqual(len(tokens), len(observed), (tokens, observed))
        values = {
            "executable": observed[0],
            "cwd": str(workspace.resolve()),
            "image-utf8": self.echo_image_bytes.decode("utf-8"),
            "image-developer-config": "developer_instructions="
            + json.dumps(
                self.echo_image_bytes.decode("utf-8"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).replace("\x7f", "\\u007f"),
            "task-json": task_bytes.decode("utf-8"),
            "model": model,
        }
        rendered = []
        for index, token in enumerate(tokens):
            if "literal" in token:
                rendered.append(token["literal"])
            elif token["value"] in {
                "image-path",
                "rendered-config-path",
                "task-path",
                "invocation-id",
                "daemon-socket-path",
            }:
                rendered.append(observed[index])
            else:
                rendered.append(values[token["value"]])
        return rendered

    def assert_live_wire(
        self,
        adapter_id: str,
        observation: dict[str, Any],
        task_bytes: bytes,
        invocation_id: str,
    ) -> None:
        stdin = base64.b64decode(observation["stdin"]["base64"])
        self.assertEqual(len(stdin), observation["stdin"]["byteLength"])
        self.assertEqual(sha256(stdin), observation["stdin"]["sha256"])
        if adapter_id == "codex/exec-json":
            self.assertEqual(task_bytes, stdin)
        elif adapter_id in {"prime/rpc", "omp/rpc"}:
            lines = stdin.splitlines()
            if adapter_id == "omp/rpc":
                self.assertEqual(2, len(lines))
                state = json.loads(lines[0])
                self.assertEqual(
                    {
                        "id": f"{invocation_id}.omp.state.1",
                        "type": "get_state",
                    },
                    state,
                )
                frame = json.loads(lines[1])
            else:
                self.assertEqual(1, len(lines))
                frame = json.loads(lines[0])
            self.assertEqual("prompt", frame["type"])
            self.assertEqual(
                (
                    f"{invocation_id}.omp.prompt.1"
                    if adapter_id == "omp/rpc"
                    else invocation_id
                ),
                frame["id"],
            )
            self.assertEqual(task_bytes, frame["message"].encode("utf-8"))
        else:
            self.assertEqual(b"", stdin)

        files = observation["files"]
        if adapter_id in {"claude/print-stream-json", "omp/rpc"}:
            self.assertEqual(
                2 if adapter_id == "omp/rpc" else 1, len(files), observation
            )
            image_file = next(file for file in files if file["flag"] != "--config")
            self.assertEqual(
                self.echo_image_bytes, base64.b64decode(image_file["base64"])
            )
            self.assertEqual(sha256(self.echo_image_bytes), image_file["sha256"])
            if os.name != "nt":
                self.assertEqual("0600", image_file["mode"])
            self.assertFalse(Path(image_file["path"]).exists())
            if adapter_id == "omp/rpc":
                overlay = next(file for file in files if file["flag"] == "--config")
                self.assertEqual(
                    b"retry:\n  enabled: false\ndisabledProviders:\n  - native\n"
                    b"  - omp-plugins\n  - claude\n  - agent-plugins\n"
                    b"  - claude-plugins\n  - codex\n  - gemini\n  - opencode\n"
                    b"  - cursor\n  - windsurf\n  - vscode\n  - mcp-json\n",
                    base64.b64decode(overlay["base64"]),
                )
                self.assertEqual(
                    "47b1a27639ab8d5ecd52abbb5326354419583b7b781ceb66935c822f789d2779",
                    overlay["sha256"],
                )
                if os.name != "nt":
                    self.assertEqual("0600", overlay["mode"])
                self.assertFalse(Path(overlay["path"]).exists())
        else:
            self.assertEqual([], files)
        daemon = observation["daemonSocket"]
        if adapter_id == "prime/rpc":
            self.assertTrue(daemon["absolute"])
            self.assertFalse(daemon["existedAtHarnessStart"])
            if os.name != "nt":
                self.assertEqual("0700", daemon["parentMode"])
            self.assertEqual("prime.sock", Path(daemon["path"]).name)
            self.assertFalse(Path(daemon["path"]).exists())
            self.assertFalse(Path(daemon["path"]).parent.exists())
            self.assertNotIn(".prime-agent", daemon["path"])
        else:
            self.assertIsNone(daemon)

    def normalized_success_projection(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        projected = deepcopy(events)
        for event_value in projected:
            event_value["timestamp"] = "<volatile>"
            event_value["invocationId"] = "<volatile>"
            if event_value["type"] == "runner.started":
                event_value["payload"]["runnerName"] = "<implementation>"
            if event_value["type"] != "runner.completed":
                continue
            result = event_value["payload"]["result"]
            result["invocationId"] = "<volatile>"
            result["runner"]["name"] = "<implementation>"
            result["runner"]["commit"] = "<implementation-build>"
            result["digests"]["invocationSha256"] = "<implementation-volatile>"
            result["digests"]["normalizedEventsSha256"] = "<event-volatile>"
            result["timing"] = {
                "startedAt": "<volatile>",
                "firstEventAt": "<volatile>",
                "cancellationAt": None,
                "terminalAt": "<volatile>",
                "durationMs": "<volatile>",
            }
        return projected

    def test_functional_alpha_live_success_is_cross_product_equivalent(self) -> None:
        if os.name == "nt":  # pragma: no cover - functional alpha is POSIX-only
            self.skipTest(
                "functional-alpha installed-process execution is not admitted on Windows"
            )
        task_argv = [
            "prose",
            "write",
            "Hello world",
            "",
            "snow 雪",
            "--language-owned",
            ";$(touch nope)",
        ]
        task = {
            "schema": self.echo_manifest["taskEnvelope"]["schemaId"],
            "argv": task_argv,
            "interactionMode": "non-interactive",
        }
        task_bytes = canonical_json(task)
        model = "fixture/model:alpha"
        auth_profiles = {
            "codex/exec-json": "cached-chatgpt-login",
            "claude/print-stream-json": "claude-subscription",
            "prime/rpc": "prime-harness-login",
            "omp/rpc": "omp-harness-login",
        }
        adapter_projections: dict[str, dict[str, list[dict[str, Any]]]] = {}

        for adapter_id, recipe in self.recipes.items():
            if not self.supports_host(adapter_id):
                continue
            harness, transport = adapter_id.split("/", 1)
            adapter_projections[adapter_id] = {}
            with tempfile.TemporaryDirectory(prefix="openprose-alpha-parity-") as root:
                temporary = Path(root)
                workspace = temporary / "workspace with spaces 雪"
                workspace.mkdir()
                auth_probe_marker = temporary / "unexpected-auth-probe.txt"
                if adapter_id in {"prime/rpc", "omp/rpc"}:
                    (workspace / ".forbid-harness-login-auth-probe").write_text(
                        str(auth_probe_marker), encoding="utf-8"
                    )
                harness_bin = temporary / "harness-bin"
                self.install_live_harnesses(harness_bin)
                argv = [
                    "--harness",
                    harness,
                    "--transport",
                    transport,
                    "--model",
                    model,
                    "--auth-profile",
                    auth_profiles[adapter_id],
                    "--output",
                    "jsonl",
                    *task_argv[1:],
                ]

                for product_name, product in self.available_products():
                    with self.subTest(product=product_name, adapter=adapter_id):
                        environment, canaries = self.make_environment(
                            temporary,
                            harness_bin,
                        )
                        completed = self.run_product(
                            product, argv, workspace, environment
                        )
                        self.assertEqual(
                            0,
                            completed.returncode,
                            (completed.stdout, completed.stderr),
                        )
                        self.assertFalse(
                            auth_probe_marker.exists(),
                            "harness-login execution must not invoke an installed auth probe",
                        )
                        self.assertEqual(b"", completed.stderr)
                        self.assert_no_secrets(completed, canaries)
                        records = [
                            json.loads(line)
                            for line in completed.stdout.decode("utf-8").splitlines()
                        ]
                        self.assertGreaterEqual(len(records), 5, records)
                        for record in records:
                            failures = sorted(
                                self.event_validator.iter_errors(record),
                                key=lambda item: list(item.path),
                            )
                            self.assertEqual(
                                [],
                                [
                                    f"{list(error.path)}: {error.message}"
                                    for error in failures
                                ],
                            )
                        self.assertEqual("runner.completed", records[-1]["type"])
                        result = records[-1]["payload"]["result"]
                        self.assert_valid_result(result)
                        self.assertEqual(0, result["runnerExitCode"])
                        self.assertEqual(
                            "success", result["terminal"]["classification"]
                        )
                        self.assertEqual("not-applicable", result["semantic"]["status"])
                        self.assertEqual(adapter_id, result["adapter"]["id"])
                        recipe_path = next(
                            path
                            for path in RECIPES.glob("*.json")
                            if load_json(path)["recipeVersion"]
                            == recipe["recipeVersion"]
                        )
                        self.assertEqual(
                            sha256(recipe_path.read_bytes()),
                            result["adapter"]["descriptorDigestSha256"],
                        )
                        base_event_bytes = b"".join(
                            canonical_json(record) + b"\n" for record in records[:-1]
                        )
                        self.assertEqual(
                            sha256(base_event_bytes),
                            result["digests"]["normalizedEventsSha256"],
                        )
                        visible = [
                            record["payload"]["text"]
                            for record in records
                            if record["type"] == "assistant.message"
                        ]
                        expected_argv_text = json.dumps(
                            task_argv,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        expected_visible = (
                            [f"Echoed task argv:\n{expected_argv_text}"]
                            if adapter_id in {"prime/rpc", "omp/rpc"}
                            else ["Echoed task argv:", expected_argv_text]
                        )
                        self.assertEqual(expected_visible, visible)
                        self.assertNotIn(
                            "OPENPROSE_ECHO_TERMINAL_V0",
                            completed.stdout.decode("utf-8"),
                        )

                        observation = load_json(temporary / "observation.json")
                        self.assertEqual(adapter_id, observation["adapterId"])
                        expected_controls = self.oracle["environmentRules"][
                            "adapterOwnedControls"
                        ].get(adapter_id, {})
                        self.assertEqual(
                            expected_controls, observation["adapterControls"]
                        )
                        environment_names = set(observation["environmentNames"])
                        selected_names = set(
                            self.adapters[adapter_id]["credentialGroups"][
                                auth_profiles[adapter_id]
                            ]
                        )
                        if adapter_id in {"prime/rpc", "omp/rpc"}:
                            self.assertEqual(set(), selected_names)
                            self.assertIn("HOME", environment_names)
                            self.assertIsNone(observation["credentialConfig"])
                            self.assertNotIn(
                                "PRIME_AGENT_CODING_AGENT_DIR", environment_names
                            )
                            self.assertNotIn("PI_CODING_AGENT_DIR", environment_names)
                        if adapter_id == "omp/rpc":
                            self.assertNotIn("--profile", observation["argv"])
                        all_credential_names = {
                            name
                            for group in self.adapters[adapter_id][
                                "credentialGroups"
                            ].values()
                            for name in group
                        }
                        self.assertTrue(
                            selected_names.issubset(environment_names),
                            environment_names,
                        )
                        self.assertTrue(
                            (all_credential_names - selected_names).isdisjoint(
                                environment_names
                            ),
                            environment_names,
                        )
                        self.assertTrue(
                            set(
                                self.oracle["environmentRules"]["alwaysStrip"]
                            ).isdisjoint(environment_names),
                            environment_names,
                        )
                        allowed_names = (
                            set(self.oracle["baseEnvironmentAllowlist"])
                            | selected_names
                            | set(expected_controls)
                            | set(self.runner_metadata["names"])
                        )
                        self.assertEqual(
                            set(), environment_names - allowed_names, environment_names
                        )
                        self.assertEqual(
                            self.expected_live_argv(
                                recipe,
                                observation,
                                workspace,
                                task_bytes,
                                model,
                            ),
                            observation["argv"],
                        )
                        self.assert_live_wire(
                            adapter_id,
                            observation,
                            task_bytes,
                            result["invocationId"],
                        )
                        adapter_projections[adapter_id][product_name] = (
                            self.normalized_success_projection(records)
                        )

            with self.subTest(adapter=adapter_id, comparison="normalized-parity"):
                self.assertEqual({"rust", "bun"}, set(adapter_projections[adapter_id]))
                self.assertEqual(
                    adapter_projections[adapter_id]["rust"],
                    adapter_projections[adapter_id]["bun"],
                    adapter_id,
                )

    def test_harness_login_profiles_have_cross_product_readiness_and_error_parity(
        self,
    ) -> None:
        if os.name == "nt":  # pragma: no cover - functional alpha is POSIX-only
            self.skipTest(
                "functional-alpha installed-process execution is not admitted on Windows"
            )
        for adapter_id, auth_profile in {
            "prime/rpc": "prime-harness-login",
            "omp/rpc": "omp-harness-login",
        }.items():
            if not self.supports_host(adapter_id):
                continue
            harness, transport = adapter_id.split("/", 1)
            with tempfile.TemporaryDirectory(prefix="openprose-login-parity-") as root:
                temporary = Path(root)
                workspace = temporary / "workspace"
                workspace.mkdir()
                auth_probe_marker = temporary / "unexpected-auth-probe.txt"
                (workspace / ".forbid-harness-login-auth-probe").write_text(
                    str(auth_probe_marker), encoding="utf-8"
                )
                harness_bin = temporary / "harness-bin"
                self.install_live_harnesses(harness_bin)
                reports: dict[str, dict[str, Any]] = {}
                problems: dict[str, dict[str, Any]] = {}
                for product_name, product in self.available_products():
                    with self.subTest(product=product_name, adapter=adapter_id):
                        environment, canaries = self.make_environment(
                            temporary, harness_bin
                        )
                        completed = self.run_product(
                            product,
                            [
                                "--harness",
                                harness,
                                "--transport",
                                transport,
                                "--model",
                                "fixture/model",
                                "--auth-profile",
                                auth_profile,
                                "--dry-run",
                                "--output",
                                "json",
                                "run",
                                "fixture.prose.md",
                            ],
                            workspace,
                            environment,
                        )
                        self.assertEqual(0, completed.returncode, completed.stdout)
                        self.assertFalse(
                            auth_probe_marker.exists(),
                            "harness-login doctor must retain unknown readiness without probing",
                        )
                        self.assertEqual(b"", completed.stderr)
                        self.assert_no_secrets(completed, canaries)
                        report = self.parse_single_json(completed)
                        self.assertEqual(
                            [],
                            [
                                f"{list(error.path)}: {error.message}"
                                for error in sorted(
                                    self.dry_run_validator.iter_errors(report),
                                    key=lambda item: list(item.path),
                                )
                            ],
                        )
                        self.assertIs(report["wouldStartModel"], False)
                        self.assertEqual(
                            {"category": "harness-managed", "readiness": "unknown"},
                            report["auth"],
                        )
                        self.assertEqual("ready", report["readiness"])
                        self.assertIsNone(report["blockingError"])
                        reports[product_name] = report

                        doctor = self.run_product(
                            product,
                            [
                                "--harness",
                                harness,
                                "--transport",
                                transport,
                                "--model",
                                "fixture/model",
                                "--auth-profile",
                                auth_profile,
                                "--output",
                                "json",
                                "cli",
                                "doctor",
                            ],
                            workspace,
                            environment,
                        )
                        self.assertEqual(0, doctor.returncode, doctor.stdout)
                        self.assertEqual(b"", doctor.stderr)
                        self.assert_no_secrets(doctor, canaries)
                        self.assertEqual([], self.parse_single_json(doctor)["problems"])
                        self.assertEqual(
                            "unknown",
                            self.parse_single_json(doctor)["selectedAuthReadiness"],
                        )
                        self.assertFalse(
                            auth_probe_marker.exists(),
                            "harness-login doctor must not invoke an installed auth probe",
                        )

                        (
                            no_profile_environment,
                            no_profile_canaries,
                        ) = self.make_environment(temporary, harness_bin)
                        missing = self.run_product(
                            product,
                            [
                                "--harness",
                                harness,
                                "--transport",
                                transport,
                                "--model",
                                "fixture/model",
                                "--output",
                                "json",
                                "cli",
                                "doctor",
                            ],
                            workspace,
                            no_profile_environment,
                        )
                        self.assertEqual(2, missing.returncode, missing.stdout)
                        self.assertEqual(b"", missing.stderr)
                        self.assert_no_secrets(missing, no_profile_canaries)
                        problem = self.parse_single_json(missing)["problems"][0]
                        self.assertEqual("CONFIG_INVALID", problem["code"])
                        self.assertEqual(adapter_id, problem["details"]["adapterId"])
                        self.assertIn(
                            auth_profile,
                            problem["details"]["supportedAuthProfiles"],
                        )
                        problems[product_name] = problem

                with self.subTest(adapter=adapter_id, comparison="dry-run-json-parity"):
                    self.assertEqual(reports["rust"], reports["bun"])
                    self.assertEqual(problems["rust"], problems["bun"])

    def test_prime_omp_environment_key_routes_are_fresh_private_and_cross_product_isolated(
        self,
    ) -> None:
        if os.name == "nt":  # pragma: no cover - functional alpha is POSIX-only
            self.skipTest(
                "functional-alpha installed-process execution is not admitted on Windows"
            )
        for adapter_id, config_name, other_config_name in [
            ("prime/rpc", "PRIME_AGENT_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR"),
            ("omp/rpc", "PI_CODING_AGENT_DIR", "PRIME_AGENT_CODING_AGENT_DIR"),
        ]:
            if not self.supports_host(adapter_id):
                continue
            harness, transport = adapter_id.split("/", 1)
            for product_name, product in self.available_products():
                with self.subTest(
                    product=product_name, adapter=adapter_id
                ), tempfile.TemporaryDirectory(
                    prefix="openprose-env-route-parity-"
                ) as root:
                    temporary = Path(root)
                    workspace = temporary / "workspace"
                    workspace.mkdir()
                    auth_probe_marker = temporary / "unexpected-auth-probe.txt"
                    (workspace / ".forbid-harness-login-auth-probe").write_text(
                        str(auth_probe_marker), encoding="utf-8"
                    )
                    harness_bin = temporary / "harness-bin"
                    self.install_live_harnesses(harness_bin)
                    observed_paths: list[str] = []
                    observation_path = temporary / "observation.json"
                    for repetition in range(2):
                        observation_path.unlink(missing_ok=True)
                        environment, canaries = self.make_environment(
                            temporary, harness_bin
                        )
                        completed = self.run_product(
                            product,
                            [
                                "--harness",
                                harness,
                                "--transport",
                                transport,
                                "--model",
                                "fixture/model",
                                "--auth-profile",
                                "openrouter",
                                "--output",
                                "json",
                                "run",
                                "fixture.prose.md",
                            ],
                            workspace,
                            environment,
                        )
                        self.assertEqual(
                            0,
                            completed.returncode,
                            (repetition, completed.stdout, completed.stderr),
                        )
                        self.assertEqual(b"", completed.stderr)
                        self.assert_no_secrets(completed, canaries)
                        self.assertFalse(
                            auth_probe_marker.exists(),
                            "environment-key preflight must not invoke Prime/OMP auth probes",
                        )
                        observation = load_json(observation_path)
                        self.assertEqual(
                            self.oracle["environmentRules"]["adapterOwnedControls"].get(
                                adapter_id, {}
                            ),
                            observation["adapterControls"],
                        )
                        config = observation["credentialConfig"]
                        self.assertEqual(config_name, config["name"])
                        self.assertTrue(config["absolute"])
                        self.assertTrue(config["existsAtHarnessStart"])
                        self.assertEqual("0700", config["mode"])
                        self.assertNotEqual(canaries[config_name], config["path"])
                        self.assertNotIn("OPENPROSE-SECRET-CANARY", config["path"])
                        environment_names = set(observation["environmentNames"])
                        self.assertIn("OPENROUTER_API_KEY", environment_names)
                        self.assertIn(config_name, environment_names)
                        self.assertNotIn(other_config_name, environment_names)
                        if adapter_id == "prime/rpc":
                            self.assertEqual(
                                Path(observation["daemonSocket"]["path"]).parent,
                                Path(config["path"]).parent,
                            )
                        else:
                            self.assertGreater(len(observation["files"]), 0)
                            self.assertEqual(
                                Path(observation["files"][0]["path"]).parent,
                                Path(config["path"]).parent,
                            )
                        self.assertFalse(
                            Path(config["path"]).exists(),
                            "credential config must be removed after complete settlement",
                        )
                        observed_paths.append(config["path"])
                    self.assertEqual(2, len(set(observed_paths)))

    def test_adjacent_harness_versions_fail_with_exact_cross_product_repair_parity(
        self,
    ) -> None:
        if os.name == "nt":  # pragma: no cover - functional alpha is POSIX-only
            self.skipTest(
                "functional-alpha installed-process execution is not admitted on Windows"
            )
        adjacent = {
            "prime/rpc": ("prime-agent", "prime-agent 0.7.1", "prime-harness-login"),
            "omp/rpc": ("omp", "omp/18.0.10", "omp-harness-login"),
            "codex/exec-json": ("codex", "codex-cli 0.149.0-alpha.4.2", None),
            "claude/print-stream-json": ("claude", "2.1.242 (Claude Code)", None),
        }
        for adapter_id, (executable_name, detected, auth_profile) in adjacent.items():
            if not self.supports_host(adapter_id):
                continue
            harness, transport = adapter_id.split("/", 1)
            with tempfile.TemporaryDirectory(
                prefix="openprose-version-parity-"
            ) as root:
                temporary = Path(root)
                workspace = temporary / "workspace"
                workspace.mkdir()
                harness_bin = temporary / "harness-bin"
                self.install_live_harnesses(harness_bin, {executable_name: detected})
                errors: dict[str, dict[str, Any]] = {}
                for product_name, product in self.available_products():
                    environment, canaries = self.make_environment(
                        temporary, harness_bin
                    )
                    argv = ["--harness", harness, "--transport", transport]
                    if auth_profile is not None:
                        argv.extend(
                            [
                                "--model",
                                "fixture/model",
                                "--auth-profile",
                                auth_profile,
                            ]
                        )
                    argv.extend(["--output", "json", "cli", "doctor"])
                    completed = self.run_product(product, argv, workspace, environment)
                    self.assertEqual(10, completed.returncode, completed.stdout)
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    problem = self.parse_single_json(completed)["problems"][0]
                    self.assertEqual("HARNESS_INCOMPATIBLE", problem["code"])
                    self.assertEqual(
                        {
                            "adapterId": adapter_id,
                            "detectedVersion": detected,
                            "admittedVersions": self.recipes[adapter_id]["support"][
                                "admittedVersions"
                            ],
                            "repairCommand": self.recipes[adapter_id]["support"][
                                "repairCommand"
                            ],
                            "fallbackAttempted": False,
                        },
                        problem["details"],
                    )
                    errors[product_name] = problem
                self.assertEqual(errors["rust"], errors["bun"], adapter_id)

    def test_rpc_fixture_rejects_eof_before_terminal(self) -> None:
        if os.name == "nt":  # pragma: no cover - POSIX functional-alpha fixture
            self.skipTest("functional-alpha installed-process execution is POSIX-only")
        with tempfile.TemporaryDirectory(prefix="openprose-early-eof-") as root:
            temporary = Path(root)
            harness_bin = temporary / "harness-bin"
            self.install_live_harnesses(harness_bin)
            task = {
                "schema": self.echo_manifest["taskEnvelope"]["schemaId"],
                "argv": ["prose", "write", "early eof adversary"],
                "interactionMode": "non-interactive",
            }
            state_request = (
                canonical_json(
                    {"id": "early-eof-fixture.omp.state.1", "type": "get_state"}
                )
                + b"\n"
            )
            prompt_request = (
                canonical_json(
                    {
                        "id": "early-eof-fixture.omp.prompt.1",
                        "type": "prompt",
                        "message": canonical_json(task).decode("utf-8"),
                    }
                )
                + b"\n"
            )
            completed = subprocess.run(
                [str(harness_bin / "omp"), "--mode", "rpc"],
                cwd=temporary,
                env={"PATH": str(harness_bin)},
                input=state_request + prompt_request,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(91, completed.returncode)
            self.assertEqual(
                ["ready", "available_commands_update", "response"],
                [json.loads(line)["type"] for line in completed.stdout.splitlines()],
            )
            self.assertIn(b"stdin EOF before agent_end", completed.stderr)

    def test_omp_control_barrier_and_nonterminal_fail_closed_with_product_parity(
        self,
    ) -> None:
        if os.name == "nt":  # pragma: no cover - POSIX functional-alpha fixture
            self.skipTest("functional-alpha installed-process execution is POSIX-only")
        expected = {
            "reordered-lifecycle": ("PROTOCOL_MALFORMED", None),
            "uncorrelated-state": ("PROTOCOL_MALFORMED", None),
            "duplicate-state": ("PROTOCOL_MALFORMED", None),
            "failed-state": ("HARNESS_FAILED", None),
            "nonempty-tools": ("HARNESS_FAILED", None),
            "interactive-ui": ("HARNESS_FAILED", None),
            "nonterminal-settlement": (
                "HARNESS_FAILED",
                "unsupported_nonterminal_settlement",
            ),
        }
        for mode, (code, reason) in expected.items():
            reports: dict[str, dict[str, Any]] = {}
            with tempfile.TemporaryDirectory(prefix="openprose-omp-control-") as root:
                temporary = Path(root)
                workspace = temporary / "workspace"
                workspace.mkdir()
                (workspace / ".omp-control-fixture.json").write_text(
                    json.dumps({"mode": mode}), encoding="utf-8"
                )
                harness_bin = temporary / "harness-bin"
                self.install_live_harnesses(harness_bin)
                argv = [
                    "--harness",
                    "omp",
                    "--transport",
                    "rpc",
                    "--model",
                    "fixture/model",
                    "--auth-profile",
                    "omp-harness-login",
                    "--output",
                    "json",
                    "write",
                    "control adversary",
                ]
                for product_name, product in self.available_products():
                    environment, canaries = self.make_environment(
                        temporary, harness_bin
                    )
                    completed = self.run_product(product, argv, workspace, environment)
                    self.assertEqual(
                        22,
                        completed.returncode,
                        (mode, completed.stdout, completed.stderr),
                    )
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    result = self.parse_single_json(completed)
                    self.assertEqual(
                        code, result["error"]["code"], (mode, product_name, result)
                    )
                    self.assertEqual(
                        False, result["error"]["details"]["fallbackAttempted"]
                    )
                    if reason is not None:
                        self.assertEqual(
                            reason,
                            result["error"]["details"].get("reason"),
                            (mode, product_name, result),
                        )
                    self.assertNotIn(".omp.state.1", completed.stdout.decode("utf-8"))
                    reports[product_name] = result["error"]
                self.assertEqual(reports["rust"], reports["bun"], mode)

        with tempfile.TemporaryDirectory(prefix="openprose-omp-state-canary-") as root:
            temporary = Path(root)
            workspace = temporary / "workspace"
            workspace.mkdir()
            (workspace / ".omp-control-fixture.json").write_text(
                json.dumps({"mode": "candidate-canary"}), encoding="utf-8"
            )
            harness_bin = temporary / "harness-bin"
            self.install_live_harnesses(harness_bin)
            argv = [
                "--harness",
                "omp",
                "--transport",
                "rpc",
                "--model",
                "fixture/model",
                "--auth-profile",
                "omp-harness-login",
                "--output",
                "json",
                "write",
                "state canary",
            ]
            for product_name, product in self.available_products():
                environment, canaries = self.make_environment(temporary, harness_bin)
                completed = self.run_product(product, argv, workspace, environment)
                self.assertEqual(
                    0,
                    completed.returncode,
                    (product_name, completed.stdout, completed.stderr),
                )
                self.assertEqual(b"", completed.stderr)
                self.assert_no_secrets(completed, canaries)
                self.assertNotIn(b"OPENPROSE-CANDIDATE-CANARY", completed.stdout)

    def test_prime_parser_diagnostic_is_exact_safe_and_cross_product_equivalent(
        self,
    ) -> None:
        if not self.supports_host("prime/rpc"):
            self.skipTest("Prime recipe does not admit this host; macOS ARM64 CI qualifies it")
        if os.name == "nt":  # pragma: no cover - Prime is not admitted on Windows
            self.skipTest("Prime parser diagnostics are POSIX-only")
        cases = {
            "malformed-record": (
                "PROTOCOL_MALFORMED",
                "jsonl-framing",
                "record-boundary",
                13,
                3,
                1,
            ),
            "partial-record": (
                "PROTOCOL_TRUNCATED",
                "jsonl-framing",
                "record-boundary",
                13,
                3,
                1,
            ),
            "first-content-record": (
                "PROTOCOL_MALFORMED",
                "prime-lifecycle",
                "await-thinking-or-text-start",
                6,
                0,
                0,
            ),
            "lifecycle-record": (
                "PROTOCOL_MALFORMED",
                "prime-lifecycle",
                "await-text-delta-or-end",
                13,
                3,
                1,
            ),
            "lifecycle-then-malformed": (
                "PROTOCOL_MALFORMED",
                "prime-lifecycle",
                "await-text-delta-or-end",
                13,
                3,
                1,
            ),
        }
        for fault, (
            error_code,
            stage,
            phase,
            accepted_records,
            thinking_deltas,
            text_deltas,
        ) in cases.items():
            diagnostics: dict[str, dict[str, Any]] = {}
            for product_name, product in self.available_products():
                with self.subTest(
                    product=product_name, fault=fault
                ), tempfile.TemporaryDirectory(
                    prefix="openprose-prime-diagnostic-"
                ) as root:
                    temporary = Path(root)
                    workspace = temporary / "workspace"
                    workspace.mkdir()
                    (workspace / ".prime-parser-diagnostic-fixture.json").write_text(
                        json.dumps(
                            {"fault": fault, "acceptedRecords": accepted_records},
                            separators=(",", ":"),
                        ),
                        encoding="utf-8",
                    )
                    harness_bin = temporary / "harness-bin"
                    self.install_live_harnesses(harness_bin)
                    environment, canaries = self.make_environment(
                        temporary, harness_bin
                    )
                    completed = self.run_product(
                        product,
                        [
                            "--harness",
                            "prime",
                            "--transport",
                            "rpc",
                            "--model",
                            "fixture/model",
                            "--auth-profile",
                            "prime-harness-login",
                            "--output",
                            "json",
                            "run",
                            "fixture.prose.md",
                        ],
                        workspace,
                        environment,
                    )
                    self.assertEqual(22, completed.returncode, completed.stdout)
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    self.assertNotIn(
                        b"OPENPROSE-CANDIDATE-CANARY",
                        completed.stdout + completed.stderr,
                    )
                    self.assertNotIn(
                        b"candidateSecret", completed.stdout + completed.stderr
                    )
                    result = self.parse_single_json(completed)
                    self.assert_valid_result(result)
                    self.assertEqual(error_code, result["error"]["code"])
                    diagnostic = result["error"]["details"]["adapterDiagnostic"]
                    self.assertEqual(
                        {
                            "schema": "openprose.adapter-diagnostic/1",
                            "adapterId": "prime/rpc",
                            "stage": stage,
                            "phase": phase,
                            "counters": {
                                "acceptedRecords": accepted_records,
                                "thinkingDeltas": thinking_deltas,
                                "textDeltas": text_deltas,
                                "saturated": False,
                            },
                        },
                        diagnostic,
                    )
                    self.assertNotIn("reason", result["error"]["details"])
                    diagnostics[product_name] = diagnostic

            self.assertEqual(diagnostics["rust"], diagnostics["bun"], f"Prime {fault}")

    def test_native_output_limits_have_safe_cross_product_diagnostics(self) -> None:
        if not self.supports_host("codex/exec-json") or os.name == "nt":
            self.skipTest("Installed Codex fixture requires a recipe-admitted POSIX host")
        for mode in ("capture", "stdout"):
            for product_name, product in self.available_products():
                with self.subTest(mode=mode, product=product_name), tempfile.TemporaryDirectory(
                    prefix="openprose-output-limit-"
                ) as root:
                    temporary = Path(root)
                    workspace = temporary / "workspace"
                    workspace.mkdir()
                    (workspace / ".native-output-limit-fixture.json").write_text(json.dumps({"mode": mode}))
                    harness_bin = temporary / "harness-bin"
                    self.install_live_harnesses(harness_bin)
                    # A one-character locale value would trigger the existing Bun
                    # all-environment redaction policy inside its own replacement text.
                    # Keep that separate policy out of this byte-limit parity control.
                    environment, canaries = self.make_environment(temporary, harness_bin, {
                        "OPENAI_API_KEY": "XY", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
                    })
                    capture = temporary / "native.jsonl"
                    argv = ["--harness", "codex", "--transport", "exec-json",
                            "--auth-profile", "openai-api-key", "--output-contract", "native",
                            "--native-output-bytes", "1048576", "--output", "json"]
                    if mode == "capture":
                        argv += ["--native-log", str(capture)]
                    argv += ["run", "fixture.prose.md"]
                    completed = self.run_product(product, argv, workspace, environment)
                    self.assertEqual(22, completed.returncode, completed.stdout)
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    self.assertNotIn(b"XY", completed.stdout)
                    result = self.parse_single_json(completed)
                    self.assert_valid_result(result)
                    self.assertEqual("HARNESS_FAILED" if mode == "capture" else "PROTOCOL_MALFORMED", result["error"]["code"])
                    diagnostic = result["error"]["details"]["transportDiagnostic"]
                    self.assertEqual("native-capture-limit" if mode == "capture" else "aggregate-stdout-limit", diagnostic["reason"])
                    self.assertEqual(1048576, diagnostic["limitBytes"])
                    self.assertGreater(diagnostic["observedBytes"], diagnostic["limitBytes"])
                    self.assertFalse(diagnostic.get("saturated", False))
                    self.assertFalse(result["error"]["details"]["fallbackAttempted"])
                    pid = int((workspace / "harness.pid").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                    if mode == "capture":
                        retained = capture.read_bytes()
                        self.assertLessEqual(len(retained), 1048576)
                        self.assertNotIn(b"XY", retained)
                        records = [json.loads(line) for line in retained.splitlines()]
                        next_record = {"type": "item.completed", "item": {"id": f"item-{len(records)-2}", "type": "agent_message", "text": "é" + "[REDACTED]" * 2048}}
                        attempted_bytes = len(json.dumps(next_record, ensure_ascii=False, separators=(",", ":")).encode()) + 1
                        self.assertEqual(len(retained) + attempted_bytes, diagnostic["observedBytes"])
                        self.assertNotIn("reason", result["error"]["details"])

    def test_prime_text_only_index_zero_success_is_cross_product_equivalent(
        self,
    ) -> None:
        if not self.supports_host("prime/rpc"):
            self.skipTest("Prime recipe does not admit this host; macOS ARM64 CI qualifies it")
        if os.name == "nt":  # pragma: no cover - Prime is not admitted on Windows
            self.skipTest("Prime text-only RPC lifecycle is POSIX-only")
        projections: dict[str, dict[str, Any]] = {}
        for product_name, product in self.available_products():
            with self.subTest(product=product_name), tempfile.TemporaryDirectory(
                prefix="openprose-prime-text-only-"
            ) as root:
                temporary = Path(root)
                workspace = temporary / "workspace"
                workspace.mkdir()
                (workspace / ".prime-lifecycle-fixture.json").write_text(
                    canonical_json({"mode": "text-only-index-zero"}).decode("utf-8"),
                    encoding="utf-8",
                )
                harness_bin = temporary / "harness-bin"
                self.install_live_harnesses(harness_bin)
                environment, canaries = self.make_environment(temporary, harness_bin)
                completed = self.run_product(
                    product,
                    [
                        "--harness",
                        "prime",
                        "--transport",
                        "rpc",
                        "--model",
                        "fixture/model",
                        "--auth-profile",
                        "prime-harness-login",
                        "--output",
                        "jsonl",
                        "run",
                        "fixture.prose.md",
                    ],
                    workspace,
                    environment,
                )
                self.assertEqual(0, completed.returncode, completed.stdout)
                self.assertEqual(b"", completed.stderr)
                self.assert_no_secrets(completed, canaries)
                records = [
                    json.loads(line)
                    for line in completed.stdout.decode("utf-8").splitlines()
                ]
                for record in records:
                    self.assertEqual(
                        [],
                        [
                            f"{list(error.path)}: {error.message}"
                            for error in self.event_validator.iter_errors(record)
                        ],
                    )
                result = records[-1]["payload"]["result"]
                self.assert_valid_result(result)
                self.assertEqual(0, result["runnerExitCode"])
                self.assertEqual("success", result["terminal"]["classification"])
                normalized_events = self.normalized_success_projection(records)
                normalized_events[-1]["payload"]["result"]["cwd"] = {
                    "path": "<temporary-workspace>",
                    "identitySha256": "<temporary-workspace>",
                }
                projections[product_name] = {
                    "events": normalized_events,
                    "terminal": result["terminal"],
                    "semantic": result["semantic"],
                }
        self.assertEqual(projections["rust"], projections["bun"])

    def test_prime_owned_service_cleanup_precedes_every_run_exit_mode_with_parity(
        self,
    ) -> None:
        if not self.supports_host("prime/rpc"):
            self.skipTest("Prime recipe does not admit this host; macOS ARM64 CI qualifies it")
        if os.name == "nt":  # pragma: no cover - Prime is not admitted on Windows
            self.skipTest("Prime owned Unix service settlement is POSIX-only")
        projections: dict[str, dict[str, dict[str, Any]]] = {}
        for exit_mode in (
            "success",
            "protocol",
            "postprocess",
            "timeout",
            "cancellation",
            "child-failure",
        ):
            projections[exit_mode] = {}
            for product_name, product in self.available_products():
                with self.subTest(
                    product=product_name, exit_mode=exit_mode
                ), tempfile.TemporaryDirectory(
                    prefix="op-prime-owned-", dir="/tmp"
                ) as root:
                    temporary = Path(root)
                    workspace = temporary / "workspace"
                    workspace.mkdir()
                    harness_bin = temporary / "bin"
                    self.install_live_harnesses(harness_bin)
                    ready_path = temporary / "owned-service-ready.txt"
                    external_root = temporary / "external-symlink-target"
                    external_root.mkdir()
                    external_canary = external_root / "must-survive.txt"
                    external_canary_bytes = b"outside the wrapper-owned private root\n"
                    external_canary.write_bytes(external_canary_bytes)
                    (workspace / ".prime-owned-service-fixture.json").write_text(
                        canonical_json(
                            {
                                "hello": "wrong-protocol",
                                "exitMode": exit_mode,
                                "readyPath": str(ready_path),
                            }
                        ).decode("utf-8"),
                        encoding="utf-8",
                    )
                    environment, canaries = self.make_environment(
                        temporary, harness_bin
                    )
                    argv = [
                        "--harness",
                        "prime",
                        "--transport",
                        "rpc",
                        "--model",
                        "fixture/model",
                        "--auth-profile",
                        "prime-harness-login",
                        "--timeout",
                        "500ms" if exit_mode == "timeout" else "3s",
                        "--output",
                        "json",
                        "run",
                        "fixture.prose.md",
                    ]
                    exercises_external_symlink = exit_mode in {
                        "timeout",
                        "cancellation",
                    }
                    if exercises_external_symlink:
                        process = subprocess.Popen(
                            [str(product), *argv],
                            cwd=workspace,
                            env=environment,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        deadline = time.monotonic() + 3
                        while (
                            not ready_path.exists()
                            and process.poll() is None
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                        if not ready_path.exists():
                            process.kill()
                            process.communicate(timeout=3)
                            self.fail("fixture service did not become ready")
                        owned_socket = Path(ready_path.read_text("utf-8"))
                        owned_private_root = owned_socket.parent
                        external_link = owned_private_root / "external-symlink"
                        try:
                            self.assertTrue(
                                owned_private_root.is_dir(),
                                "long-running fixture must expose the owned root before settlement",
                            )
                            external_link.symlink_to(
                                external_root, target_is_directory=True
                            )
                        except BaseException:
                            process.kill()
                            process.communicate(timeout=3)
                            raise
                        if exit_mode == "cancellation":
                            process.send_signal(signal.SIGINT)
                        stdout, stderr = process.communicate(timeout=8)
                        completed = subprocess.CompletedProcess(
                            process.args, process.returncode, stdout, stderr
                        )
                    else:
                        completed = self.run_product(
                            product, argv, workspace, environment
                        )
                        self.assertTrue(
                            ready_path.exists(), "fixture service did not become ready"
                        )
                        owned_socket = Path(ready_path.read_text("utf-8"))
                        owned_private_root = owned_socket.parent
                        external_link = owned_private_root / "external-symlink"
                    self.assertEqual(25, completed.returncode, completed.stdout)
                    self.assertEqual(b"", completed.stderr)
                    self.assert_no_secrets(completed, canaries)
                    result = self.parse_single_json(completed)
                    self.assert_valid_result(result)
                    self.assertEqual("PROCESS_CLEANUP_FAILED", result["error"]["code"])
                    self.assertEqual(
                        {
                            "phase": "owned-service-settlement",
                            "processResource": "owned-prime-harness-service",
                            "adapterId": "prime/rpc",
                            "fallbackAttempted": False,
                        },
                        result["error"]["details"],
                    )
                    self.assertFalse(
                        owned_socket.exists(),
                        "the private Prime socket must not survive product exit",
                    )
                    self.assertFalse(
                        owned_private_root.exists(),
                        "failed service settlement must still scrub the owned private root",
                    )
                    self.assertTrue(
                        external_root.is_dir(),
                        "cleanup must never follow a symlink outside the owned root",
                    )
                    self.assertEqual(
                        external_canary_bytes,
                        external_canary.read_bytes(),
                        "cleanup must preserve bytes beyond the owned-root boundary",
                    )
                    error_evidence = canonical_json(result["error"])
                    for private_path in (
                        owned_socket,
                        owned_private_root,
                        external_link,
                        external_root,
                    ):
                        self.assertNotIn(
                            str(private_path).encode("utf-8"),
                            error_evidence,
                            "cleanup failure evidence must remain pathless",
                        )
                    projections[exit_mode][product_name] = {
                        "error": result["error"],
                        "ownedSocketRemovedBeforeExit": not owned_socket.exists(),
                        "ownedPrivateRootRemovedBeforeExit": not owned_private_root.exists(),
                        "externalSymlinkProtectionExercised": exercises_external_symlink,
                        "externalTargetPreserved": (
                            external_root.is_dir()
                            and external_canary.read_bytes() == external_canary_bytes
                        ),
                        "errorEvidencePathless": all(
                            str(private_path).encode("utf-8") not in error_evidence
                            for private_path in (
                                owned_socket,
                                owned_private_root,
                                external_link,
                                external_root,
                            )
                        ),
                    }

            with self.subTest(exit_mode=exit_mode, comparison="error-json-parity"):
                self.assertEqual(
                    projections[exit_mode]["rust"],
                    projections[exit_mode]["bun"],
                )

    def test_shared_adapter_scenarios_define_the_full_runtime_image(self) -> None:
        full_digest = sha256(self.image_bytes)
        metadata = self.manifest["modelVisibleBytes"]
        self.assertEqual("ordered-raw-concatenation-v1", metadata["serialization"])
        self.assertEqual(len(self.image_bytes), metadata["byteLength"])
        self.assertEqual(full_digest, metadata["sha256"])
        admitted = []
        for adapter_id, scenario in self.scenarios.items():
            file_digests = {entry["sha256"] for entry in scenario["files"]}
            stdin = scenario.get("stdin") or {}
            decoded = {
                value
                for key, value in stdin.items()
                if key.startswith("decodedImageSha256")
            }
            inline_image = (
                adapter_id == "prime/rpc"
                and "{{IMAGE_UTF8}}" in scenario.get("expectedArgv", [])
                and self.recipes[adapter_id]["launch"]["imageDelivery"]
                == {
                    "mechanism": "argv",
                    "field": "--append-system-prompt",
                    "encoding": "utf8",
                }
            )
            if full_digest in file_digests | decoded or inline_image:
                admitted.append(adapter_id)
        self.assertEqual(
            {"codex/exec-json", "claude/print-stream-json", "prime/rpc", "omp/rpc"},
            set(admitted),
            "every shared scenario must transport the full ordered manifest image",
        )

    def test_suite_has_no_literal_provider_launch(self) -> None:
        source = Path(__file__).read_text("utf-8")
        tree = ast.parse(source)
        forbidden = {
            name
            for recipe in self.recipes.values()
            for name in recipe["identity"]["executableNames"]
        }
        literal_launches = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if (
                node.func.attr != "run"
                or not node.args
                or not isinstance(node.args[0], ast.List)
            ):
                continue
            elements = node.args[0].elts
            if (
                elements
                and isinstance(elements[0], ast.Constant)
                and elements[0].value in forbidden
            ):
                literal_launches.append(elements[0].value)
        self.assertEqual([], literal_launches)
        self.assertIn("[str(product), *argv]", source)

    def test_adversarial_manifest_names_real_tests(self) -> None:
        manifest = load_json(INVARIANTS_PATH)
        self.assertEqual(
            "openprose.adapter-product-adversarial-invariants/1", manifest["schema"]
        )
        ids = [check["id"] for check in manifest["checks"]]
        self.assertEqual(len(ids), len(set(ids)))
        for check in manifest["checks"]:
            self.assertTrue(hasattr(self, check["test"]), check)


if __name__ == "__main__":
    if "--build" in sys.argv:
        sys.argv.remove("--build")
        build_fixture_products()
    unittest.main(verbosity=2)
