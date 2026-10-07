from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import rehearse_release


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def rebind_rehearsal_evidence(root: Path, name: str, encoded: bytes) -> None:
    (root / name).write_bytes(encoded)
    manifest_path = root / rehearse_release.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    record = next(item for item in manifest["files"] if item["path"] == name)
    record.update({"byteLength": len(encoded), "sha256": sha(encoded)})
    manifest_path.write_bytes(rehearse_release.canonical_json(manifest))
    checksum_names = []
    for line in (root / rehearse_release.CHECKSUM_NAME).read_text("ascii").splitlines():
        _digest, checksum_name = line.split("  ", 1)
        checksum_names.append(checksum_name)
    (root / rehearse_release.CHECKSUM_NAME).write_text(
        "".join(
            f"{sha((root / checksum_name).read_bytes())}  {checksum_name}\n"
            for checksum_name in sorted(checksum_names)
        ),
        "ascii",
    )


RUST_BYTES = b"fake-rust-binary"
BUN_BYTES = b"fake-bun-binary"
LAUNCHER_BYTES = b"#!/usr/bin/env node\n"
PLATFORM_MANIFEST_BYTES = b'{"name":"fake-platform"}\n'
PACKAGE_MANIFEST_BYTES = b'{"fake":"release"}\n'
DEPENDENCY_BYTES = b'{"fake":"dependencies"}\n'
PACKAGE_SUMS_BYTES = b"fake package sums\n"
COMMAND_BYTES = b"fake launcher command\n"
RUST_SHA = sha(RUST_BYTES)
BUN_SHA = sha(BUN_BYTES)
SUMS_SHA = sha(PACKAGE_SUMS_BYTES)
MANIFEST_SHA = sha(PACKAGE_MANIFEST_BYTES)


def reports() -> tuple[dict, dict, dict]:
    build = {
        "schema": "openprose.local-build-report/1",
        "profile": "development",
        "testSeamsEnabled": True,
        "globalStateModified": False,
        "detachedDescendantContainment": "not-enforced",
        "candidates": {
            "rust": {
                "path": "$OPENPROSE_LOCAL_BUILD/candidates/rust/prose",
                "buildSourcePath": "/source/rust/prose",
                "snapshotPath": "$OPENPROSE_LOCAL_BUILD/candidates/rust/prose",
                "snapshotOwnership": "ephemeral-owned-root",
                "byteLength": 10,
                "sha256": RUST_SHA,
                "smoke": "pass",
            },
            "bun": {
                "path": "$OPENPROSE_LOCAL_BUILD/candidates/bun/prose",
                "buildSourcePath": "/source/bun/prose",
                "snapshotPath": "$OPENPROSE_LOCAL_BUILD/candidates/bun/prose",
                "snapshotOwnership": "ephemeral-owned-root",
                "byteLength": 11,
                "sha256": BUN_SHA,
                "smoke": "pass",
            },
        },
        "windowsProcessHost": None,
        "package": {
            "path": "/output/package",
            "mode": "development",
            "purpose": "mock-benchmark",
            "inputBuild": {
                "image": "sentinel-v1",
                "profile": "development",
                "testSeamsEnabled": True,
            },
            "inputs": {
                "rust": {
                    "path": "$OPENPROSE_LOCAL_BUILD/candidates/rust/prose",
                    "buildSourcePath": "/source/rust/prose",
                    "snapshotPath": "$OPENPROSE_LOCAL_BUILD/candidates/rust/prose",
                    "snapshotOwnership": "ephemeral-owned-root",
                    "byteLength": 10,
                    "sha256": RUST_SHA,
                },
                "bun": {
                    "path": "$OPENPROSE_LOCAL_BUILD/candidates/bun/prose",
                    "buildSourcePath": "/source/bun/prose",
                    "snapshotPath": "$OPENPROSE_LOCAL_BUILD/candidates/bun/prose",
                    "snapshotOwnership": "ephemeral-owned-root",
                    "byteLength": 11,
                    "sha256": BUN_SHA,
                },
            },
            "sha256Sums": {"path": "SHA256SUMS", "byteLength": 100, "sha256": SUMS_SHA},
        },
        "install": None,
    }
    limitations = {
        "detachedDescendantContainment": "not-enforced",
        "providerCalls": "none",
        "semanticEvaluation": "not-performed",
        "portabilityEvaluation": "not-performed",
        "releaseEvaluation": "not-performed",
        "ranking": "not-produced",
        "runtimeNetworkIsolation": "not-enforced",
    }
    raw = {
        "schema": "openprose.installed-package-benchmark/1",
        "platform": "test-platform",
        "measurementPlan": {
            "trials": 1,
            "timeoutSeconds": 2,
            "deadlineApplied": True,
            "surfaceOrder": ["direct-rust", "direct-bun", "npm-launcher"],
            "installationMethods": {},
            "npmInstallArgv": [],
            "invocationArgv": [],
            "executablePaths": {},
            "expectedIdentity": {},
        },
        "packageIdentity": {
            "version": "0.1.0",
            "sourceRevision": "development",
            "releaseManifestSha256": MANIFEST_SHA,
            "dependencyEvidenceSha256": sha(DEPENDENCY_BYTES),
            "sha256SumsSha256": SUMS_SHA,
            "rustBinarySha256": RUST_SHA,
            "bunBinarySha256": BUN_SHA,
        },
        "surfaces": {
            "direct-rust": {"binarySha256": RUST_SHA},
            "direct-bun": {"binarySha256": BUN_SHA},
            "npm-launcher": {
                "binarySha256": BUN_SHA,
                "launcherSourceSha256": sha(LAUNCHER_BYTES),
                "launcherCommandIdentity": {
                    "kind": "regular-shim",
                    "sha256": sha(COMMAND_BYTES),
                },
            },
        },
        "artifacts": [],
        "evidence": [
            {
                "path": "dependency-evidence.json",
                "byteLength": len(DEPENDENCY_BYTES),
                "sha256": sha(DEPENDENCY_BYTES),
            },
            {
                "path": "release-manifest.json",
                "byteLength": len(PACKAGE_MANIFEST_BYTES),
                "sha256": MANIFEST_SHA,
            },
            {
                "path": "SHA256SUMS",
                "byteLength": len(PACKAGE_SUMS_BYTES),
                "sha256": SUMS_SHA,
            },
        ],
        "launcherResolution": {
            "platformManifestSha256": sha(PLATFORM_MANIFEST_BYTES),
            "packagedBinarySha256": BUN_SHA,
            "launcherCommand": {
                "kind": "regular-shim",
                "sha256": sha(COMMAND_BYTES),
            },
        },
        "toolchain": {
            "node": {
                "command": "/fake/node",
                "resolvedPath": "/fake/node",
                "sha256": "1" * 64,
                "version": "v26.0.0",
            },
            "npm": {
                "command": "/fake/npm",
                "resolvedPath": "/fake/npm",
                "sha256": "2" * 64,
                "version": "11.12.1",
            },
        },
        "limitations": limitations,
    }
    raw_digest = sha(rehearse_release.canonical_json(raw))
    analysis = {
        "schema": "openprose.installed-package-benchmark-analysis/1",
        "sourceReportSha256": raw_digest,
        "packageIdentity": raw["packageIdentity"],
        "surfaceTimingSummaries": [
            {
                "surface": "direct-bun",
                "count": 1,
                "minimumWallMs": 1,
                "medianWallMs": 1,
                "maximumWallMs": 1,
            },
            {
                "surface": "direct-rust",
                "count": 1,
                "minimumWallMs": 1,
                "medianWallMs": 1,
                "maximumWallMs": 1,
            },
            {
                "surface": "npm-launcher",
                "count": 1,
                "minimumWallMs": 1,
                "medianWallMs": 1,
                "maximumWallMs": 1,
            },
        ],
        "limitations": limitations,
    }
    return build, raw, analysis


class FakeBenchmark:
    def __init__(self, raw: dict, analysis: dict) -> None:
        self.raw = raw
        self.analysis = analysis
        self.calls: list[tuple] = []
        self.tree_digests: dict[str, str] = {}

    def run_benchmark(
        self, package, install, *, trials, timeout_seconds, deadline_monotonic=None
    ):
        self.calls.append(
            (package, install, trials, timeout_seconds, deadline_monotonic)
        )
        self.raw["measurementPlan"].update(
            {
                "trials": trials,
                "timeoutSeconds": timeout_seconds,
                "deadlineApplied": deadline_monotonic is not None,
            }
        )
        package.mkdir()
        (package / "release-manifest.json").write_bytes(PACKAGE_MANIFEST_BYTES)
        (package / "dependency-evidence.json").write_bytes(DEPENDENCY_BYTES)
        (package / "SHA256SUMS").write_bytes(PACKAGE_SUMS_BYTES)
        install.mkdir()
        version = self.raw["packageIdentity"]["version"]
        platform_value = self.raw["platform"]
        for implementation, encoded in (("rust", RUST_BYTES), ("bun", BUN_BYTES)):
            binary = (
                install
                / f"{implementation}-standalone"
                / f"openprose-prose-cli-{implementation}-{version}-{platform_value}"
                / "prose"
            )
            binary.parent.mkdir(parents=True)
            binary.write_bytes(encoded)
            binary.chmod(0o755)
        command, meta_root, platform_root, _ = self.npm_layout(
            install / "npm-prefix", platform_value
        )
        (meta_root / "bin").mkdir(parents=True)
        (platform_root / "bin").mkdir(parents=True)
        (meta_root / "bin" / "prose.js").write_bytes(LAUNCHER_BYTES)
        (meta_root / "package.json").write_bytes(b'{"name":"@openprose/prose-cli","version":"0.1.0"}\n')
        (platform_root / "package.json").write_bytes(PLATFORM_MANIFEST_BYTES)
        (platform_root / "bin" / "prose").write_bytes(BUN_BYTES)
        command.parent.mkdir(parents=True)
        command.write_bytes(COMMAND_BYTES)
        command.chmod(0o755)
        node = install / "toolchain" / "node"
        node.parent.mkdir()
        node.write_bytes(b"fake exact node interpreter")
        node.chmod(0o755)
        node_digest = sha(node.read_bytes())
        self.raw["toolchain"]["node"] = {
            "command": str(node),
            "resolvedPath": str(node),
            "sha256": node_digest,
            "version": "v26.0.0",
        }
        self.raw["measurementPlan"]["executablePaths"] = {
            "direct-rust": "$INSTALL_ROOT/"
            + str(
                binary_path(install, "rust", version, platform_value).relative_to(
                    install
                )
            ),
            "direct-bun": "$INSTALL_ROOT/"
            + str(
                binary_path(install, "bun", version, platform_value).relative_to(
                    install
                )
            ),
            "npm-launcher": "$INSTALL_ROOT/" + str(command.relative_to(install)),
        }
        closure=(meta_root/'package.json',meta_root/'bin/prose.js',platform_root/'package.json',platform_root/'bin/prose')
        self.raw['installations']=[{'surface':'npm-launcher','treeIdentity':{'entries':[{'path':str(path.relative_to(install/'npm-prefix')),'type':'regular','byteLength':path.stat().st_size,'sha256':sha(path.read_bytes())} for path in closure]}}]
        self.tree_digests = self._current_tree_digests(install)
        return copy.deepcopy(self.raw)

    def analyse_report(self, raw):
        self.assert_raw = raw
        result = copy.deepcopy(self.analysis)
        result["sourceReportSha256"] = sha(rehearse_release.canonical_json(raw))
        result["packageIdentity"] = copy.deepcopy(raw["packageIdentity"])
        return result

    @staticmethod
    def render_json(value):
        return rehearse_release.canonical_json(value)

    @staticmethod
    def json_no_duplicates(encoded, _label):
        return json.loads(encoded)

    def verify_package_output(self, package, expected_platform=None):
        if (package / "release-manifest.json").read_bytes() != PACKAGE_MANIFEST_BYTES:
            raise rehearse_release.RehearsalError("fake package manifest differs")
        if (package / "SHA256SUMS").read_bytes() != PACKAGE_SUMS_BYTES:
            raise rehearse_release.RehearsalError("fake package sums differ")
        if (package / "dependency-evidence.json").read_bytes() != DEPENDENCY_BYTES:
            raise rehearse_release.RehearsalError("fake dependency evidence differs")
        return {
            "platform": expected_platform,
            "release": {
                "version": self.raw["packageIdentity"]["version"],
                "source": {"revision": self.raw["packageIdentity"]["sourceRevision"]},
            },
            "artifacts": {},
            "encoded": {
                "release-manifest.json": PACKAGE_MANIFEST_BYTES,
                "dependency-evidence.json": DEPENDENCY_BYTES,
            },
            "checksums": {
                "release-manifest.json": MANIFEST_SHA,
                "dependency-evidence.json": sha(DEPENDENCY_BYTES),
            },
            "evidence": copy.deepcopy(self.raw["evidence"]),
        }

    @staticmethod
    def safe_read(path, _maximum):
        return path.read_bytes()

    @staticmethod
    def sha256_bytes(value):
        return sha(value)

    @staticmethod
    def npm_layout(prefix, _platform):
        modules = prefix / "lib" / "node_modules"
        return (
            prefix / "bin" / "prose",
            modules / "@openprose" / "prose-cli",
            modules / "@openprose" / "prose-cli-test-platform",
            modules,
        )

    @staticmethod
    def launcher_command_identity(command, _launcher_source):
        return {"kind": "regular-shim", "sha256": sha(command.read_bytes())}

    @staticmethod
    def _tree_digest(root: Path) -> str:
        records = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                records.append(
                    (path.relative_to(root).as_posix(), sha(path.read_bytes()))
                )
        return sha(rehearse_release.canonical_json(records))

    def _current_tree_digests(self, install: Path) -> dict[str, str]:
        return {
            "direct-rust": self._tree_digest(install / "rust-standalone"),
            "direct-bun": self._tree_digest(install / "bun-standalone"),
            "npm-launcher": self._tree_digest(install / "npm-prefix"),
        }

    def verify_retained_install_trees(self, install, _raw):
        observed = self._current_tree_digests(install)
        if observed != self.tree_digests:
            raise rehearse_release.RehearsalError("fake installed tree mutated")
        return observed


def binary_path(
    install: Path, implementation: str, version: str, platform_value: str
) -> Path:
    return (
        install
        / f"{implementation}-standalone"
        / f"openprose-prose-cli-{implementation}-{version}-{platform_value}"
        / "prose"
    )


def fake_conformance(mutate_after_write=None):
    runner = rehearse_release.load_conformance_runner()

    def execute(argv, _cwd, _environment):
        phase = int(argv[argv.index("--phase") + 1])
        report_path = Path(argv[argv.index("--report-json") + 1])
        candidates = []
        index = 0
        while index < len(argv):
            if argv[index] == "--candidate":
                candidates.append(argv[index + 1 : index + 4])
                index += 4
            else:
                index += 1
        interpreters = []
        index = 0
        while index < len(argv):
            if argv[index] == "--candidate-interpreter":
                interpreters.append(argv[index + 1 : index + 3])
                index += 3
            else:
                index += 1
        products = runner.attach_candidate_interpreters(
            runner.parse_candidate_specs(candidates), interpreters
        )
        identities = [
            (product, runner.capture_candidate_identity(product))
            for product in products
        ]
        case_ids = sorted(
            json.loads(path.read_text("utf-8"))["id"]
            for path in runner.case_paths(phase, set())
        )
        report = runner.make_report(
            phase=phase,
            case_ids=case_ids,
            candidates=identities,
            candidate_passed=len(case_ids) * len(products),
            candidate_failed=0,
            differential_passed=len(case_ids) * (len(products) - 1),
            differential_failed=0,
            failures=[],
        )
        report_path.write_bytes(runner.render_report(report))
        if mutate_after_write is not None:
            mutate_after_write(products)
        return subprocess.CompletedProcess(argv, 0, b"PASS\n", b"")

    return runner, execute


def conformance_kwargs(mutate_after_write=None):
    runner, executor = fake_conformance(mutate_after_write)
    return {"conformance_module": runner, "conformance_executor": executor}


class ReleaseRehearsalTests(unittest.TestCase):
    def test_npm_context_is_bound_to_exact_benchmark_inventory_and_all_surfaces(self):
        for mutation in ('none','missing','duplicate','native-cross','launcher-cross','platform-cross','file-change','symlink'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); _build,raw,analysis=reports(); benchmark=FakeBenchmark(raw,analysis)
                install=root/'install'; observed=benchmark.run_benchmark(root/'package',install,trials=1,timeout_seconds=1,deadline_monotonic=time.monotonic()+10)
                original=rehearse_release._npm_conformance_context(install,observed)
                self.assertEqual({'prefix','platform','files'},set(original))
                self.assertEqual({'launcher','native','metaManifest','platformManifest'},set(original['files']))
                changed=copy.deepcopy(observed)
                if mutation=='missing': changed['installations'][0]['treeIdentity']['entries'].pop()
                elif mutation=='duplicate': changed['installations'].append(copy.deepcopy(changed['installations'][0]))
                elif mutation=='native-cross': changed['surfaces']['direct-bun']['binarySha256']='f'*64
                elif mutation=='launcher-cross': changed['surfaces']['npm-launcher']['launcherSourceSha256']='f'*64
                elif mutation=='platform-cross': changed['launcherResolution']['platformManifestSha256']='f'*64
                elif mutation in ('file-change','symlink'):
                    path=Path(original['prefix'])/original['files']['metaManifest']['path']
                    if mutation=='file-change': path.write_bytes(b'tampered')
                    else:
                        foreign=path.with_name('foreign'); path.rename(foreign); path.symlink_to(foreign)
                if mutation=='none': self.assertEqual(original,rehearse_release._npm_conformance_context(install,changed))
                else:
                    with self.assertRaises(rehearse_release.RehearsalError): rehearse_release._npm_conformance_context(install,changed)

    def test_npm_context_checks_custody_after_failed_conformance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); _build,raw,analysis=reports(); benchmark=FakeBenchmark(raw,analysis)
            install=root/'install'; observed=benchmark.run_benchmark(root/'package',install,trials=1,timeout_seconds=1,deadline_monotonic=time.monotonic()+10)
            runner,_execute=fake_conformance()
            context=rehearse_release._npm_conformance_context(install,observed)
            def failing_executor(argv,cwd,environment):
                index=argv.index('--candidate-npm-context')
                self.assertEqual('npm-launcher',argv[index+1]); self.assertEqual(context,json.loads(argv[index+2]))
                (Path(context['prefix'])/context['files']['native']['path']).write_bytes(b'tampered')
                return subprocess.CompletedProcess(argv,1,b'',b'failed fixture')
            with self.assertRaisesRegex(rehearse_release.RehearsalError,'benchmark custody'):
                rehearse_release._run_mechanical_conformance(runner,observed,install,root/'report.json',{},time.monotonic()+10,failing_executor)

    def test_conformance_inputs_require_a_versioned_exact_node_identity(self) -> None:
        _build, raw, analysis = reports()
        benchmark = FakeBenchmark(raw, analysis)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "install"
            observed = benchmark.run_benchmark(
                root / "package",
                install,
                trials=1,
                timeout_seconds=1,
                deadline_monotonic=time.monotonic() + 10,
            )
            candidates, node, digest = rehearse_release._conformance_inputs(
                install, observed
            )
            self.assertEqual(
                [item[0] for item in candidates],
                [
                    "direct-rust",
                    "direct-bun",
                    "npm-launcher",
                ],
            )
            self.assertEqual(digest, sha(node.read_bytes()))
            for mutation in ("missing", "malformed"):
                changed = copy.deepcopy(observed)
                if mutation == "missing":
                    del changed["toolchain"]["node"]["version"]
                else:
                    changed["toolchain"]["node"]["version"] = "26.0.0"
                with self.subTest(mutation=mutation), self.assertRaisesRegex(
                    rehearse_release.RehearsalError,
                    "Node tool identity has an unsupported shape"
                    if mutation == "missing"
                    else "Node tool version is malformed",
                ):
                    rehearse_release._conformance_inputs(install, changed)

    def test_cross_bindings_accept_exact_three_surface_custody(self) -> None:
        build, raw, analysis = reports()
        bindings = rehearse_release.validate_cross_bindings(build, raw, analysis)
        self.assertEqual(RUST_SHA, bindings["rustCandidateSha256"])
        self.assertEqual(BUN_SHA, bindings["bunCandidateSha256"])
        self.assertTrue(bindings["threeInstalledSurfacesBound"])

    def test_cross_bindings_reject_every_important_identity_divergence(self) -> None:
        build, raw, analysis = reports()
        mutations = []
        changed = copy.deepcopy(raw)
        changed["surfaces"]["npm-launcher"]["binarySha256"] = "9" * 64
        mutations.append((build, changed, analysis, "Bun bytes diverge"))
        changed = copy.deepcopy(raw)
        changed["packageIdentity"]["sha256SumsSha256"] = "9" * 64
        changed_analysis = copy.deepcopy(analysis)
        changed_analysis["packageIdentity"] = changed["packageIdentity"]
        changed_analysis["sourceReportSha256"] = sha(
            rehearse_release.canonical_json(changed)
        )
        mutations.append((build, changed, changed_analysis, "package differs"))
        changed_analysis = copy.deepcopy(analysis)
        changed_analysis["sourceReportSha256"] = "9" * 64
        mutations.append((build, raw, changed_analysis, "not bound"))
        changed = copy.deepcopy(raw)
        changed["limitations"]["ranking"] = "winner-produced"
        changed_analysis = copy.deepcopy(analysis)
        changed_analysis["sourceReportSha256"] = sha(
            rehearse_release.canonical_json(changed)
        )
        changed_analysis["limitations"] = changed["limitations"]
        mutations.append((build, changed, changed_analysis, "limitations"))
        for candidate, raw_value, analysis_value, message in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(
                rehearse_release.RehearsalError, message
            ):
                rehearse_release.validate_cross_bindings(
                    candidate, raw_value, analysis_value
                )

    def test_rehearsal_writes_closed_checksum_bound_nonrelease_evidence(self) -> None:
        build, raw, analysis = reports()
        benchmark = FakeBenchmark(raw, analysis)
        calls = []

        def builder(**kwargs):
            calls.append(kwargs)
            return copy.deepcopy(build)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rehearsal"
            summary = rehearse_release.rehearse(
                output,
                trials=1,
                timeout_seconds=2,
                budget_seconds=60,
                ambient={"OPENAI_API_KEY": "must-not-enter-evidence"},
                builder=builder,
                benchmark_module=benchmark,
                **conformance_kwargs(),
            )
            self.assertFalse(summary["claims"]["releaseEligible"])
            self.assertFalse(summary["claims"]["publicationAuthorized"])
            self.assertFalse(summary["claims"]["rankingProduced"])
            self.assertNotIn("providerCallsMade", summary["claims"])
            self.assertTrue(summary["claims"]["deterministicMockSelected"])
            self.assertEqual(
                "not-performed", summary["claims"]["providerCallMonitoring"]
            )
            self.assertEqual("not-enforced", summary["claims"]["networkIsolation"])
            self.assertEqual(1, summary["policy"]["trials"])
            self.assertIn("elapsedSeconds", summary["timing"])
            self.assertIn("generator", summary)
            self.assertEqual(
                {"cargo", "bun", "python"},
                set(summary["generator"]["buildTools"]),
            )
            self.assertEqual(
                raw["toolchain"], summary["generator"]["benchmarkToolchain"]
            )
            self.assertEqual("both", calls[0]["selection"])
            self.assertTrue(calls[0]["smoke"])
            self.assertIsNone(calls[0]["install_dir"])
            self.assertEqual(calls[0]["package_purpose"], "mock-benchmark")
            self.assertIn("executor", calls[0])
            self.assertNotIn("OPENAI_API_KEY", calls[0]["ambient"])
            self.assertNotEqual(os.environ.get("HOME"), calls[0]["ambient"]["HOME"])
            self.assertEqual("http://127.0.0.1:9", calls[0]["ambient"]["HTTPS_PROXY"])
            self.assertEqual(1, benchmark.calls[0][2])
            self.assertIsInstance(benchmark.calls[0][4], float)
            mechanical = summary["bindings"]["mechanicalConformance"]
            self.assertEqual(7, mechanical["phase"])
            self.assertEqual(100, len(mechanical["caseIds"]))
            self.assertEqual(300, mechanical["candidateCaseValidations"])
            self.assertEqual(200, mechanical["differentialValidations"])
            self.assertEqual(500, mechanical["totalValidations"])
            self.assertEqual(
                rehearse_release.CONFORMANCE_CASES, len(mechanical["caseIds"])
            )
            self.assertEqual(
                rehearse_release.CONFORMANCE_CANDIDATE_VALIDATIONS,
                mechanical["candidateCaseValidations"],
            )
            self.assertEqual(
                rehearse_release.CONFORMANCE_DIFFERENTIAL_VALIDATIONS,
                mechanical["differentialValidations"],
            )
            self.assertEqual(
                rehearse_release.CONFORMANCE_TOTAL_VALIDATIONS,
                mechanical["totalValidations"],
            )
            self.assertFalse(mechanical["semanticConformance"])
            self.assertFalse(mechanical["releaseAdmission"])
            manifest = json.loads((output / "rehearsal-manifest.json").read_bytes())
            self.assertFalse(manifest["releaseEligible"])
            self.assertEqual(
                set(rehearse_release.EVIDENCE_FILES),
                {item["path"] for item in manifest["files"]},
            )
            sums = (output / "REHEARSAL-SHA256SUMS").read_text("ascii").splitlines()
            self.assertEqual(
                sorted(line.split("  ", 1)[1] for line in sums),
                [line.split("  ", 1)[1] for line in sums],
            )
            self.assertEqual(
                {*rehearse_release.EVIDENCE_FILES, "rehearsal-manifest.json"},
                {line.split("  ", 1)[1] for line in sums},
            )
            for line in sums:
                expected, name = line.split("  ", 1)
                self.assertEqual(expected, sha((output / name).read_bytes()))
            self.assertNotIn(
                b"must-not-enter-evidence",
                b"".join(
                    (output / name).read_bytes()
                    for name in rehearse_release.EVIDENCE_FILES
                ),
            )

    def test_forged_conformance_counts_claims_digests_cases_and_bytes_fail(
        self,
    ) -> None:
        build, raw, analysis = reports()
        mutations = {
            "counts": lambda value: value["validations"].__setitem__("total", 129),
            "claims": lambda value: value["claims"].__setitem__(
                "semanticConformance", True
            ),
            "release-claim": lambda value: value["claims"].__setitem__(
                "releaseAdmission", True
            ),
            "digest": lambda value: value["candidates"][0][
                "resolvedTarget"
            ].__setitem__("sha256", "9" * 64),
            "case-list": lambda value: value["caseIds"].__setitem__(0, "forged.case"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "forged"
                benchmark = FakeBenchmark(copy.deepcopy(raw), copy.deepcopy(analysis))
                runner, executor = fake_conformance()
                rehearse_release.rehearse(
                    output,
                    trials=1,
                    budget_seconds=60,
                    builder=lambda **_kwargs: copy.deepcopy(build),
                    benchmark_module=benchmark,
                    conformance_module=runner,
                    conformance_executor=executor,
                )
                report_path = output / "installed-mechanical-conformance.json"
                report = json.loads(report_path.read_bytes())
                mutate(report)
                rebind_rehearsal_evidence(
                    output, report_path.name, rehearse_release.canonical_json(report)
                )
                with self.assertRaises(rehearse_release.RehearsalError):
                    rehearse_release.verify_rehearsal(
                        output,
                        benchmark_module=benchmark,
                        conformance_module=runner,
                    )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "noncanonical"
            benchmark = FakeBenchmark(copy.deepcopy(raw), copy.deepcopy(analysis))
            runner, executor = fake_conformance()
            rehearse_release.rehearse(
                output,
                trials=1,
                budget_seconds=60,
                builder=lambda **_kwargs: copy.deepcopy(build),
                benchmark_module=benchmark,
                conformance_module=runner,
                conformance_executor=executor,
            )
            path = output / "installed-mechanical-conformance.json"
            rebind_rehearsal_evidence(output, path.name, path.read_bytes() + b" \n")
            with self.assertRaises(rehearse_release.RehearsalError):
                rehearse_release.verify_rehearsal(
                    output, benchmark_module=benchmark, conformance_module=runner
                )

    def test_post_report_candidate_and_interpreter_mutation_fail_before_evidence(
        self,
    ) -> None:
        build, raw, analysis = reports()

        def mutate_candidate(products):
            products[0].executable.write_bytes(b"changed after report")

        def mutate_interpreter(products):
            assert products[2].interpreter is not None
            products[2].interpreter.write_bytes(b"changed exact node")

        for name, mutation in (
            ("candidate", mutate_candidate),
            ("interpreter", mutate_interpreter),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / name
                benchmark = FakeBenchmark(copy.deepcopy(raw), copy.deepcopy(analysis))
                with self.assertRaises(rehearse_release.RehearsalError):
                    rehearse_release.rehearse(
                        output,
                        trials=1,
                        budget_seconds=60,
                        builder=lambda **_kwargs: copy.deepcopy(build),
                        benchmark_module=benchmark,
                        **conformance_kwargs(mutation),
                    )
                self.assertFalse(
                    (output / "installed-mechanical-conformance.json").exists()
                )

    def test_secret_bearing_builder_report_fails_before_evidence_write(self) -> None:
        build, raw, analysis = reports()
        benchmark = FakeBenchmark(raw, analysis)
        secret = "top-secret-provider-token"

        def builder(**_kwargs):
            leaked = copy.deepcopy(build)
            leaked["candidates"]["rust"]["buildSourcePath"] = secret
            return leaked

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "secret-leak"
            with self.assertRaisesRegex(rehearse_release.RehearsalError, "secret"):
                rehearse_release.rehearse(
                    output,
                    trials=1,
                    budget_seconds=60,
                    ambient={
                        "OPENAI_API_KEY": secret,
                        "PATH": os.environ.get("PATH", ""),
                    },
                    builder=builder,
                    benchmark_module=benchmark,
                    **conformance_kwargs(),
                )
            self.assertFalse(
                any(
                    (output / name).exists() for name in rehearse_release.EVIDENCE_FILES
                )
            )

    def test_one_deadline_caps_builder_and_benchmark_and_exhaustion_fails(self) -> None:
        build, raw, analysis = reports()
        benchmark = FakeBenchmark(raw, analysis)
        executor_calls = []

        def builder(**kwargs):
            executor_calls.append(kwargs["executor"])
            kwargs["executor"](("tool",), Path.cwd(), {})
            return copy.deepcopy(build)

        with tempfile.TemporaryDirectory() as directory:
            before = time.monotonic()
            with patch.object(
                rehearse_release.build_local,
                "execute_bounded",
                return_value=object(),
            ) as bounded:
                rehearse_release.rehearse(
                    Path(directory) / "deadline",
                    trials=1,
                    budget_seconds=60,
                    builder=builder,
                    benchmark_module=benchmark,
                    **conformance_kwargs(),
                )
                self.assertGreater(bounded.call_args.kwargs["timeout_seconds"], 0)
                self.assertLessEqual(bounded.call_args.kwargs["timeout_seconds"], 60)
                self.assertGreaterEqual(benchmark.calls[0][4], before + 59)
                self.assertLessEqual(benchmark.calls[0][4], time.monotonic() + 60)

        class Clock:
            value = 100.0

            def __call__(self):
                return self.value

        clock = Clock()

        def exhausting_builder(**_kwargs):
            clock.value = 161.0
            return copy.deepcopy(build)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            rehearse_release.time, "monotonic", clock
        ):
            with self.assertRaisesRegex(rehearse_release.RehearsalError, "deadline"):
                rehearse_release.rehearse(
                    Path(directory) / "expired",
                    trials=1,
                    budget_seconds=60,
                    builder=exhausting_builder,
                    benchmark_module=benchmark,
                    **conformance_kwargs(),
                )

    def test_verify_roundtrip_and_post_write_tamper_fail_closed(self) -> None:
        build, raw, analysis = reports()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "verify"
            benchmark = FakeBenchmark(raw, analysis)
            runner, executor = fake_conformance()
            rehearse_release.rehearse(
                output,
                trials=1,
                budget_seconds=60,
                builder=lambda **_kwargs: copy.deepcopy(build),
                benchmark_module=benchmark,
                conformance_module=runner,
                conformance_executor=executor,
            )
            verified = rehearse_release.verify_rehearsal(
                output, benchmark_module=benchmark, conformance_module=runner
            )
            self.assertEqual("verified-local-development-rehearsal", verified["status"])
            (output / "unexpected").write_bytes(b"extra")
            with self.assertRaises(rehearse_release.RehearsalError):
                rehearse_release.verify_rehearsal(
                    output, benchmark_module=benchmark, conformance_module=runner
                )

    def test_output_is_canonical_owned_and_rejects_unsafe_or_replaced_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real"
            real.mkdir()
            alias = base / "alias"
            alias.symlink_to(real, target_is_directory=True)
            owned = rehearse_release._owned_output(alias / "safe-output")
            self.assertEqual(real.resolve() / "safe-output", owned.path)
            rehearse_release._verify_root(owned)
            with self.assertRaisesRegex(rehearse_release.RehearsalError, "basename"):
                rehearse_release._owned_output(real / ".hidden")
            moved = real / "moved-output"
            owned.path.rename(moved)
            owned.path.mkdir()
            with self.assertRaisesRegex(rehearse_release.RehearsalError, "ownership"):
                rehearse_release._verify_root(owned)

    def test_verify_rejects_evidence_symlink_duplicate_json_and_current_byte_tamper(
        self,
    ) -> None:
        build, raw, analysis = reports()
        mutations = (
            "evidence-symlink",
            "duplicate-json",
            "package",
            "installed-binary",
            "installed-ancestor-symlink",
            "launcher",
        )
        for mutation in mutations:
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "verify-tamper"
                benchmark = FakeBenchmark(raw, analysis)
                rehearse_release.rehearse(
                    output,
                    trials=1,
                    budget_seconds=60,
                    builder=lambda **_kwargs: copy.deepcopy(build),
                    benchmark_module=benchmark,
                    **conformance_kwargs(),
                )
                if mutation == "evidence-symlink":
                    target = output / "installed-package-analysis.json"
                    target.unlink()
                    target.symlink_to("installed-package-raw.json")
                elif mutation == "duplicate-json":
                    target = output / "build-report.json"
                    original = target.read_bytes()
                    duplicate = b'{"schema":"duplicate",' + original[1:]
                    rebind_rehearsal_evidence(output, target.name, duplicate)
                elif mutation == "package":
                    (output / "package" / "release-manifest.json").write_bytes(
                        b"tampered"
                    )
                elif mutation == "installed-binary":
                    binary = next(
                        (output / "installed" / "rust-standalone").rglob("prose")
                    )
                    binary.write_bytes(b"tampered")
                elif mutation == "installed-ancestor-symlink":
                    standalone = output / "installed" / "rust-standalone"
                    moved = output / "installed" / "rust-standalone-owned-bytes"
                    standalone.rename(moved)
                    standalone.symlink_to(moved.name, target_is_directory=True)
                else:
                    command, _meta, _platform, _modules = benchmark.npm_layout(
                        output / "installed" / "npm-prefix", "test-platform"
                    )
                    command.write_bytes(b"tampered")
                with self.assertRaises(rehearse_release.RehearsalError):
                    rehearse_release.verify_rehearsal(
                        output,
                        benchmark_module=benchmark,
                        conformance_module=rehearse_release.load_conformance_runner(),
                    )

    def test_recorded_generator_digest_is_portable_but_reports_current_match(
        self,
    ) -> None:
        build, raw, analysis = reports()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "portable-provenance"
            benchmark = FakeBenchmark(raw, analysis)
            runner, executor = fake_conformance()
            rehearse_release.rehearse(
                output,
                trials=1,
                timeout_seconds=2,
                budget_seconds=60,
                builder=lambda **_kwargs: copy.deepcopy(build),
                benchmark_module=benchmark,
                conformance_module=runner,
                conformance_executor=executor,
            )
            summary = json.loads((output / "rehearsal-summary.json").read_bytes())
            summary["generator"]["rehearsalSourceSha256"] = "f" * 64
            rebind_rehearsal_evidence(
                output,
                "rehearsal-summary.json",
                rehearse_release.canonical_json(summary),
            )
            verified = rehearse_release.verify_rehearsal(
                output, benchmark_module=benchmark, conformance_module=runner
            )
            self.assertFalse(verified["matchesCurrentVerifier"])

    def test_historical_report_remains_verifiable_when_current_corpus_drifts(
        self,
    ) -> None:
        build, raw, analysis = reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "historical"
            benchmark = FakeBenchmark(raw, analysis)
            runner, executor = fake_conformance()
            rehearse_release.rehearse(
                output,
                trials=1,
                budget_seconds=60,
                builder=lambda **_kwargs: copy.deepcopy(build),
                benchmark_module=benchmark,
                conformance_module=runner,
                conformance_executor=executor,
            )
            changed_paths = []
            cases = root / "future-corpus"
            cases.mkdir()
            for ordinal in range(27):
                path = cases / f"{ordinal:02d}.json"
                path.write_text(
                    json.dumps({"id": f"future.case.{ordinal:02d}"}), "utf-8"
                )
                changed_paths.append(path)
            with patch.object(runner, "case_paths", return_value=changed_paths):
                verified = rehearse_release.verify_rehearsal(
                    output,
                    benchmark_module=benchmark,
                    conformance_module=runner,
                )
            self.assertFalse(verified["mechanicalConformanceCurrentCorpusMatch"])

    def test_prior_31_case_rehearsal_remains_verifiable_but_not_current(
        self,
    ) -> None:
        build, raw, analysis = reports()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "historical-31-case"
            benchmark = FakeBenchmark(raw, analysis)
            runner, executor = fake_conformance()
            historical_paths = list(runner.case_paths(7, set()))[:31]
            self.assertEqual(31, len(historical_paths))
            with (
                patch.object(runner, "case_paths", return_value=historical_paths),
                patch.object(rehearse_release, "CONFORMANCE_CASES", 31),
                patch.object(rehearse_release, "CONFORMANCE_CANDIDATE_VALIDATIONS", 93),
                patch.object(
                    rehearse_release, "CONFORMANCE_DIFFERENTIAL_VALIDATIONS", 62
                ),
                patch.object(rehearse_release, "CONFORMANCE_TOTAL_VALIDATIONS", 155),
            ):
                rehearse_release.rehearse(
                    output,
                    trials=1,
                    budget_seconds=60,
                    builder=lambda **_kwargs: copy.deepcopy(build),
                    benchmark_module=benchmark,
                    conformance_module=runner,
                    conformance_executor=executor,
                )

            summary = json.loads((output / "rehearsal-summary.json").read_bytes())
            historical = summary["bindings"]["mechanicalConformance"]
            self.assertEqual(31, len(historical["caseIds"]))
            self.assertEqual(93, historical["candidateCaseValidations"])
            self.assertEqual(62, historical["differentialValidations"])
            self.assertEqual(155, historical["totalValidations"])
            verified = rehearse_release.verify_rehearsal(
                output,
                benchmark_module=benchmark,
                conformance_module=runner,
            )
            self.assertFalse(verified["mechanicalConformanceCurrentCorpusMatch"])

            report_path = output / "installed-mechanical-conformance.json"
            report = json.loads(report_path.read_bytes())
            report["validations"]["candidateCases"]["total"] = 92
            rebind_rehearsal_evidence(
                output, report_path.name, rehearse_release.canonical_json(report)
            )
            with self.assertRaisesRegex(
                rehearse_release.RehearsalError, "mechanical conformance report"
            ):
                rehearse_release.verify_rehearsal(
                    output,
                    benchmark_module=benchmark,
                    conformance_module=runner,
                )

    def test_refuses_existing_output_and_windows_before_builder(self) -> None:
        build, raw, analysis = reports()
        benchmark = FakeBenchmark(raw, analysis)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "exists"
            output.mkdir()
            with self.assertRaisesRegex(
                rehearse_release.RehearsalError, "must not already exist"
            ):
                rehearse_release.rehearse(
                    output,
                    budget_seconds=60,
                    benchmark_module=benchmark,
                    **conformance_kwargs(),
                )
            untouched = Path(directory) / "windows"
            with patch.object(
                rehearse_release.os, "name", "nt"
            ), self.assertRaisesRegex(rehearse_release.RehearsalError, "Job Object"):
                rehearse_release.rehearse(
                    untouched,
                    budget_seconds=60,
                    benchmark_module=benchmark,
                )
            self.assertFalse(untouched.exists())

    def test_bounds_are_closed(self) -> None:
        _build, raw, analysis = reports()
        benchmark = FakeBenchmark(raw, analysis)
        with tempfile.TemporaryDirectory() as directory:
            for suffix, kwargs in (
                ("trials", {"trials": 0}),
                ("trials-bool", {"trials": True}),
                ("timeout", {"timeout_seconds": float("nan")}),
                ("timeout-zero", {"timeout_seconds": 0}),
                ("timeout-bool", {"timeout_seconds": False}),
                ("budget", {"budget_seconds": 59}),
                ("budget-zero", {"budget_seconds": 0}),
                ("budget-bool", {"budget_seconds": True}),
            ):
                with self.subTest(suffix=suffix), self.assertRaises(
                    rehearse_release.RehearsalError
                ):
                    rehearse_release.rehearse(
                        Path(directory) / suffix,
                        benchmark_module=benchmark,
                        **conformance_kwargs(),
                        **kwargs,
                    )


class CompleteSdkConformanceCustodyTests(unittest.TestCase):
    def fixture(self, root, platform='darwin-x64'):
        from test_kernel_rc_evidence import sdk_fixture
        import kernel_rc_evidence as custody
        module = rehearse_release.load_installed_benchmark(); module.sdk_modules()
        sdk, scoped = sdk_fixture(platform)
        manifest = {'platform': platform, 'agentsSdk': sdk}
        raw = {'platform': platform, 'installations': [], 'measurementPlan': {'executablePaths': {'direct-rust': '$INSTALL_ROOT/rust-standalone/release/prose', 'direct-bun': '$INSTALL_ROOT/bun-standalone/release/prose'}}}
        evidence = {sdk['receiptSha256']: scoped['files']['agents-sdk-build.json'][0].decode()}
        if platform.startswith('darwin'): raw['sdkEvidence'] = evidence
        for label, tree_root, prefix in (
                ('direct-rust', root / 'rust-standalone', 'release'),
                ('direct-bun', root / 'bun-standalone', 'release'),
                ('npm-launcher', root / 'npm-prefix', 'lib/node_modules/@openprose/prose-cli-' + platform + '/bin')):
            directory = tree_root / prefix; directory.mkdir(parents=True)
            table = {kind: {'source/' + n: v for n, v in rows.items()} for kind, rows in scoped.items()}
            table['files']['source/prose'] = (b'nonexecuted CLI fixture', 0o755)
            custody.materialize_sdk_members(manifest, table, directory)
            (directory / 'prose').write_bytes(b'nonexecuted CLI fixture'); (directory / 'prose').chmod(0o755)
            context = {'receiptSha256': sdk['receiptSha256'], 'prefix': prefix, 'encoded': evidence[sdk['receiptSha256']]} if platform.startswith('darwin') else None
            tree = module.capture_installed_tree(tree_root, sdk_context=context)
            raw['installations'].append({'surface': label, 'treeIdentity': tree})
        return module, manifest, raw

    def test_three_complete_source_trees_authenticate_on_both_platforms(self):
        for platform in ('darwin-x64', 'linux-x64-gnu'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve(); module, release, raw = self.fixture(root, platform)
                contexts = rehearse_release._sdk_conformance_contexts(root, raw, release, module)
                self.assertEqual(set(contexts), {'direct-rust', 'direct-bun', 'npm-launcher'})
                self.assertTrue(all(c['agentsSdk'] == release['agentsSdk'] for c in contexts.values()))
                expected = rehearse_release._sdk_report_binding(raw, release)
                rehearse_release._validate_sdk_conformance_binding({'sdkSourceCustody': expected}, raw, release)
                with self.assertRaises(rehearse_release.RehearsalError): rehearse_release._validate_sdk_conformance_binding({}, raw, release)
                bad = json.loads(json.dumps(expected)); bad['candidateReferences']['direct-rust'] = '0' * 64
                with self.assertRaises(rehearse_release.RehearsalError): rehearse_release._validate_sdk_conformance_binding({'sdkSourceCustody': bad}, raw, release)
                altered = json.loads(json.dumps(release)); altered['agentsSdk']['sha256'] = '0' * 64
                with self.assertRaises(ValueError): rehearse_release._sdk_conformance_contexts(root, raw, altered, module)

    def test_omitted_release_and_support_changes_refuse_instead_of_fixture_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); module, release, raw = self.fixture(root)
            with self.assertRaises(rehearse_release.RehearsalError): rehearse_release._sdk_conformance_contexts(root, raw, None, module)
            (root / 'rust-standalone/release/prose-agents-sdk-runtime/empty-data').write_bytes(b'changed')
            with self.assertRaises(Exception): rehearse_release._sdk_conformance_contexts(root, raw, release, module)

    def test_source_rechecked_after_mocked_conformance_before_report_admission(self):
        import time
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); module, release, raw = self.fixture(root)
            labels = [('direct-rust', 'rust', root / 'rust-standalone/prose'), ('direct-bun', 'bun', root / 'bun-standalone/prose'), ('npm-launcher', 'bun', root / 'npm-prefix/bin/prose')]
            def execute(argv, cwd, env):
                self.assertEqual(argv.count('--candidate-sdk-source'), 3)
                (root / 'bun-standalone/release/prose-agents-sdk-runtime/empty-data').write_bytes(b'changed')
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            with patch.object(rehearse_release, '_conformance_inputs', return_value=(labels, Path('/nonexecuted/node'), '0'*64)), patch.object(rehearse_release, '_npm_conformance_context', return_value={}):
                with self.assertRaises(Exception): rehearse_release._run_mechanical_conformance(type('Runner', (), {'__file__': __file__}), raw, root, root / 'report.json', {}, time.monotonic() + 30, execute, release=release, benchmark_module=module)
                self.assertFalse((root / 'report.json').exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
