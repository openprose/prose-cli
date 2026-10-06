from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_package_admission as ADMISSION


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_TEST_PATH = ROOT / "cli" / "benchmarks" / "installed" / "test_benchmark.py"
SPEC = importlib.util.spec_from_file_location(
    "openprose_benchmark_test_fixtures", BENCHMARK_TEST_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load installed benchmark fixtures")
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)

VERSION = "0.1.0"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
CONTROL_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", "utf-8")


def release_binary(runner: str, image: dict[str, object]) -> bytes:
    help_bytes = (ROOT / "cli/conformance/cases/fixtures/runner-help.txt").read_bytes()
    runner_identity = {"name": runner, "version": VERSION, "commit": SOURCE_SHA}
    doctor_image = {
        key: image[key]
        for key in ("formatVersion", "version", "sha256", "releaseEligible")
    }
    config = {
        "schema": "openprose.configuration-explanation/1",
        "values": {
            "harness": {"value": "openprose"},
            "transport": {"value": "auto"},
            "output": {"value": "json"},
            "color": {"value": False},
        },
    }
    inventory = {
        "schema": "openprose.harness-list/1",
        "selected": "openprose",
        "harnesses": [
            {
                "id": "mock",
                "availability": "unavailable",
                "detectedVersion": None,
                "strictWrapperConformant": False,
                "testOnly": True,
                "admissionBlock": "test-seams-disabled",
            }
        ],
    }
    doctor_base = {
        "schema": "openprose.doctor-report/1",
        "runner": runner_identity,
        "build": {"profile": "release", "testSeamsEnabled": False},
        "image": doctor_image,
        "ready": False,
    }
    default_doctor = {
        **doctor_base,
        "selectedHarness": "openprose",
        "problems": [{"code": "HOSTED_UNAVAILABLE", "exitCode": 10}],
    }
    mock_doctor = {
        **doctor_base,
        "selectedHarness": "mock",
        "selectedTransport": "deterministic",
        "problems": [
            {
                "code": "HARNESS_UNAVAILABLE",
                "exitCode": 10,
                "details": {
                    "harness": "mock",
                    "admissionStatus": "blocked",
                    "admissionBlock": "test-seams-disabled",
                    "fallbackAttempted": False,
                },
            }
        ],
    }
    refused = {
        "schema": "openprose.runner-result/1",
        "runner": runner_identity,
        "adapter": {
            "id": "mock/unavailable",
            "harnessVersion": None,
            "descriptorDigestSha256": "599d3baf7b7aee1f97855dd4ca13e780c4de0aff410ad5860000631c0ac22fa4",
        },
        "transport": "deterministic",
        "terminal": {
            "classification": "runner-error",
            "transportCompleted": False,
            "terminalEventObserved": False,
            "exitCode": None,
            "signal": None,
        },
        "semantic": {"status": "unknown", "terminalEnvelopeDigestSha256": None},
        "error": {
            "code": "HARNESS_UNAVAILABLE",
            "exitCode": 10,
            "details": {
                "harness": "mock",
                "admissionStatus": "blocked",
                "admissionBlock": "test-seams-disabled",
                "fallbackAttempted": False,
            },
        },
        "runnerExitCode": 10,
    }
    return (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"help_bytes = {help_bytes!r}\n"
        f"values = { {'config': config, 'list': inventory, 'default': default_doctor, 'mock': mock_doctor, 'refused': refused}!r}\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        f" print('prose {VERSION} ({runner})')\n"
        "elif args == ['--help']:\n"
        " sys.stdout.buffer.write(help_bytes)\n"
        "elif args == ['--output', 'json', 'cli', 'config', 'explain']:\n"
        " print(json.dumps(values['config'], sort_keys=True, separators=(',', ':')))\n"
        "elif args == ['--output', 'json', 'cli', 'harness', 'list']:\n"
        " print(json.dumps(values['list'], sort_keys=True, separators=(',', ':')))\n"
        "elif args == ['--output', 'json', 'cli', 'doctor']:\n"
        " print(json.dumps(values['default'], sort_keys=True, separators=(',', ':'))); raise SystemExit(10)\n"
        "elif args == ['--harness', 'mock', '--output', 'json', 'cli', 'doctor']:\n"
        " print(json.dumps(values['mock'], sort_keys=True, separators=(',', ':'))); raise SystemExit(10)\n"
        'elif args in (["--harness", "mock", "--output", "json", "run", "fixture.prose.md"], ["--harness", "mock", "--output", "json", "--dry-run", "run", "fixture.prose.md"]):\n'
        " print(json.dumps(values['refused'], sort_keys=True, separators=(',', ':'))); raise SystemExit(10)\n"
        "else:\n"
        " raise SystemExit(2)\n"
    ).encode()


def replace_release_binaries(packages: Path, platform_value: str) -> None:
    release_path = packages / "release-manifest.json"
    release = json.loads(release_path.read_text("utf-8"))
    binaries = {
        name: release_binary(name, release["image"]) for name in ("rust", "bun")
    }
    executable = "prose.exe" if platform_value.startswith("win32-") else "prose"
    for implementation in ("rust", "bun"):
        archive = (
            packages
            / f"openprose-prose-cli-{implementation}-{VERSION}-{platform_value}.tar.gz"
        )

        def mutate(members, implementation=implementation):
            for index, (name, data, mode) in enumerate(members):
                if name.endswith(f"/{executable}"):
                    members[index] = (name, binaries[implementation], mode)

        FIXTURES.rewrite_tar(archive, mutate)
    platform_package = packages / f"openprose-prose-cli-{platform_value}-{VERSION}.tgz"

    def mutate_platform(members):
        for index, (name, data, mode) in enumerate(members):
            if name == f"package/bin/{executable}":
                members[index] = (name, binaries["bun"], mode)
            elif name == "package/package.json":
                manifest = json.loads(data)
                manifest["openproseBinaryByteLength"] = len(binaries["bun"])
                manifest["openproseBinarySha256"] = hashlib.sha256(
                    binaries["bun"]
                ).hexdigest()
                members[index] = (name, FIXTURES.canonical(manifest), mode)

    FIXTURES.rewrite_tar(platform_package, mutate_platform)
    FIXTURES.refresh_evidence(packages)


def authority_inputs(
    root: Path, benchmark, packages: Path, target_id: str
) -> tuple[Path, Path]:
    platform_value = ADMISSION.TARGET_PLATFORMS[target_id]
    context = benchmark.verify_package_output(
        packages, expected_platform=platform_value, purpose="release-invariants"
    )
    payloads = benchmark.validate_package_payloads(context)
    release = context["release"]
    profile = {
        "schema": "openprose.release-preflight-report/1",
        "status": "pass",
        "releaseKind": "draft-only",
        "publicationAuthorized": False,
        "version": VERSION,
        "sourceSha": SOURCE_SHA,
        "checkedOutSha": SOURCE_SHA,
        "controlSha": CONTROL_SHA,
        "controlRef": "refs/heads/main",
        "productVersions": {"rust": VERSION, "bun": VERSION},
        "image": {
            "bundleSha256": "1" * 64,
            "manifestSha256": release["image"]["manifestSha256"],
            "checksumSha256": "2" * 64,
            "imageSha256": release["image"]["sha256"],
            "version": release["image"]["version"],
            "purpose": "canonical-language-runtime",
            "releaseEligible": True,
        },
        "protectedAuthority": {"status": "pass"},
        "gates": {
            "canonicalProfile": {
                "status": "pass",
                "sha256": release["externalGates"]["canonicalProfile"]["sha256"],
            },
            "releaseEvidence": {
                "status": "pass",
                "sha256": release["externalGates"]["releaseEvidence"]["sha256"],
            },
        },
        "failures": [],
    }
    suffix = ".exe" if target_id == "win-x64" else ""
    files = {
        "native-manifest.json": {"byteLength": 10, "sha256": "3" * 64},
        f"prose-rust{suffix}": {
            "byteLength": len(payloads["extracted"]["rust"]["binaryBytes"]),
            "sha256": payloads["extracted"]["rust"]["binarySha256"],
        },
        f"prose-bun{suffix}": {
            "byteLength": len(payloads["extracted"]["bun"]["binaryBytes"]),
            "sha256": payloads["extracted"]["bun"]["binarySha256"],
        },
    }
    sdk_archive = packages / f'openprose-prose-cli-{release["platform"]}-{release["version"]}.tgz'
    with tarfile.open(sdk_archive) as archive:
        for name in ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'):
            data = archive.extractfile('package/bin/' + name).read()
            files[name] = {'byteLength': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    windows = None
    if target_id == "win-x64":
        windows = release["windowsProcessHost"]
        files[windows["path"]] = {
            "byteLength": windows["byteLength"],
            "sha256": windows["sha256"],
        }
    expected_image = {
        key: release["image"][key]
        for key in ("formatVersion", "version", "sha256", "releaseEligible")
    }
    native = {
        "schema": "openprose.native-verification/1",
        "target": target_id,
        "sourceSha": SOURCE_SHA,
        "nativeArtifact": {
            "name": f"native-build-{target_id}",
            "workflowRunId": 123,
            "workflowRunAttempt": 2,
            "files": files,
        },
        "nativeManifestSha256": "3" * 64,
        "windowsJobObjectReleaseAdmission": False,
        "windowsProcessHost": windows,
        "containment": {
            "strictDescendantContainmentEnforced": False,
            "releaseEligible": False,
            "blocker": "detached-descendant-containment-not-enforced",
        },
        "reports": {
            name: {
                "runner": {"name": name, "version": VERSION, "commit": SOURCE_SHA},
                "build": {"profile": "release", "testSeamsEnabled": False},
                "image": expected_image,
            }
            for name in ("rust", "bun")
        },
    }
    profile_path = root / "profile.json"
    native_path = root / "native.json"
    write_json(profile_path, profile)
    write_json(native_path, native)
    return profile_path, native_path


class ReleasePackageAdmissionTests(unittest.TestCase):
    def test_execution_toolchain_is_finally_reauthenticated(self) -> None:
        node = b"n" * (33 * 1024 * 1024)
        npm = b"npm-tool"
        paths: dict[Path, bytes | int] = {
            Path("/tools/node"): node,
            Path("/tools/npm"): npm,
        }

        class Benchmark:
            MAX_TOOL_BYTES = 256 * 1024 * 1024

            @staticmethod
            def safe_read(path: Path, maximum: int) -> bytes:
                self.assertEqual(maximum, Benchmark.MAX_TOOL_BYTES)
                value = paths[path]
                if isinstance(value, int):
                    if value > maximum:
                        raise ValueError("tool exceeds the closed byte limit")
                    raise AssertionError("integer fixture must represent an oversize file")
                return value

        installed = {
            "nodeTool": {
                "command": "/tools/node",
                "resolvedPath": "/tools/node",
                "sha256": hashlib.sha256(node).hexdigest(),
            },
            "npmTool": {
                "command": "/tools/npm",
                "resolvedPath": "/tools/npm",
                "sha256": hashlib.sha256(npm).hexdigest(),
            },
        }
        report = ADMISSION.execution_toolchain(Benchmark(), installed)
        self.assertEqual(
            report["authority"],
            "reporter-observed-and-finally-reauthenticated-executable-bytes",
        )
        paths[Path("/tools/npm")] = b"mutated"
        with self.assertRaisesRegex(ADMISSION.AdmissionError, "npm executable bytes"):
            ADMISSION.execution_toolchain(Benchmark(), installed)
        paths[Path("/tools/npm")] = npm
        paths[Path("/tools/node")] = Benchmark.MAX_TOOL_BYTES + 1
        with self.assertRaisesRegex(ValueError, "closed byte limit"):
            ADMISSION.execution_toolchain(Benchmark(), installed)

    def test_cli_failure_envelope_is_closed_canonical_and_bounded(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(Path(ADMISSION.__file__)), "--not-a-real-option"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertGreater(len(completed.stderr), 0)
        self.assertLessEqual(len(completed.stderr), 65536)
        report = json.loads(completed.stderr)
        self.assertEqual(set(report), {"schema", "code", "message"})
        self.assertEqual(
            report["schema"], "openprose.release-package-admission-error/1"
        )
        self.assertEqual(report["code"], "ARGUMENT_INVALID")
        self.assertIsInstance(report["message"], str)
        self.assertGreater(len(report["message"]), 0)
        self.assertLessEqual(len(report["message"]), 1000)
        self.assertEqual(completed.stderr, ADMISSION.canonical_json(report))

    def test_windows_package_is_fully_static_and_blocked_before_candidate_spawn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            packages = FIXTURES.make_release_package_output(
                fixture_root, platform_value="win32-x64"
            )
            benchmark = ADMISSION.load_benchmark()
            profile, native = authority_inputs(root, benchmark, packages, "win-x64")
            with mock.patch.object(
                benchmark,
                "install_verified_package_set",
                side_effect=AssertionError("Windows must not install or spawn"),
            ):
                report = ADMISSION.run_admission(
                    packages=packages,
                    work_root=root / "work",
                    out=root / "release-package-admission.json",
                    target_id="win-x64",
                    version=VERSION,
                    source_sha=SOURCE_SHA,
                    control_sha=CONTROL_SHA,
                    workflow_run_id=123,
                    workflow_run_attempt=2,
                    profile_preflight=profile,
                    native_verification=native,
                    timeout_seconds=2,
                    deadline_monotonic=time.monotonic() + 20,
                    benchmark_module=benchmark,
                )
            self.assertEqual(report["status"], "blocked-before-execution")
            self.assertEqual(report["schema"], "openprose.release-package-admission/3")
            self.assertEqual(
                set(report),
                {
                    "schema",
                    "status",
                    "targetId",
                    "version",
                    "sourceSha",
                    "controlSha",
                    "workflowRun",
                    "authorityInputs",
                    "corpus",
                    "package",
                    "installations",
                    "executionToolchain",
                    "surfaces",
                    "cases",
                    "claims",
                },
            )
            self.assertEqual(
                report["executionToolchain"],
                {
                    "authority": "not-observed-no-candidate-execution",
                    "node": None,
                    "npm": None,
                },
            )
            self.assertEqual(
                set(report["package"]),
                {
                    "platform",
                    "mode",
                    "sha256Sums",
                    "releaseManifest",
                    "files",
                    "image",
                    "buildProfiles",
                    "nativeLineage",
                },
            )
            self.assertEqual(
                [item["path"] for item in report["package"]["files"]],
                sorted(path.name for path in packages.iterdir()),
            )
            self.assertEqual(
                report["corpus"],
                ADMISSION.digest_record(ADMISSION.CORPUS.read_bytes()),
            )
            self.assertEqual(report["claims"]["providerCalls"], "not-observed")
            self.assertEqual(report["installations"], [])
            self.assertEqual(report["cases"], [])
            self.assertEqual(
                report["claims"]["candidateExecution"], "blocked-before-execution"
            )
            self.assertEqual(
                set(report["surfaces"]), {"direct-rust", "direct-bun", "npm-launcher"}
            )
            self.assertEqual(
                (root / "release-package-admission.json").read_bytes(),
                ADMISSION.canonical_json(report),
            )

    @unittest.skipIf(os.name == "nt", "POSIX package execution requires process groups")
    def test_release_package_installs_and_runs_all_three_surfaces(self) -> None:
        if not all(shutil.which(tool) for tool in ("node", "npm")):
            self.skipTest("Node and npm are required")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            packages = FIXTURES.make_release_package_output(fixture_root)
            replace_release_binaries(packages, FIXTURES.PLATFORM)
            benchmark = ADMISSION.load_benchmark()
            profile, native = authority_inputs(
                root,
                benchmark,
                packages,
                {
                    "linux-x64-gnu": "linux-x64",
                    "linux-arm64-gnu": "linux-arm64",
                    "darwin-arm64": "darwin-arm",
                    "darwin-x64": "darwin-x64",
                }[FIXTURES.PLATFORM],
            )
            target_id = {
                "linux-x64-gnu": "linux-x64",
                "linux-arm64-gnu": "linux-arm64",
                "darwin-arm64": "darwin-arm",
                "darwin-x64": "darwin-x64",
            }[FIXTURES.PLATFORM]
            report = ADMISSION.run_admission(
                packages=packages,
                work_root=root / "work",
                out=root / "release-package-admission.json",
                target_id=target_id,
                version=VERSION,
                source_sha=SOURCE_SHA,
                control_sha=CONTROL_SHA,
                workflow_run_id=123,
                workflow_run_attempt=2,
                profile_preflight=profile,
                native_verification=native,
                timeout_seconds=5,
                deadline_monotonic=time.monotonic() + 60,
                benchmark_module=benchmark,
            )
            self.assertEqual(
                report["status"], "passed-provider-free-release-invariants"
            )
            self.assertEqual(len(report["installations"]), 3)
            toolchain = report["executionToolchain"]
            self.assertEqual(
                toolchain["authority"],
                "reporter-observed-and-finally-reauthenticated-executable-bytes",
            )
            for name in ("node", "npm"):
                self.assertEqual(
                    set(toolchain[name]), {"command", "resolvedPath", "sha256"}
                )
                self.assertEqual(
                    toolchain[name]["command"], toolchain[name]["resolvedPath"]
                )
                self.assertRegex(toolchain[name]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(report["cases"]), 8)
            self.assertTrue(
                all(len(case["observations"]) == 3 for case in report["cases"])
            )
            self.assertTrue(
                all(
                    isinstance(observation["projection"], dict)
                    for case in report["cases"]
                    for observation in case["observations"]
                )
            )
            by_id = {case["id"]: case for case in report["cases"]}
            version_projection = by_id["version"]["observations"][0]["projection"]
            self.assertEqual(
                version_projection,
                {"kind": "version", "runner": "rust", "version": VERSION},
            )
            refusal_projection = by_id["run-mock-refused"]["observations"][0][
                "projection"
            ]
            self.assertEqual(refusal_projection["transport"], "deterministic")
            self.assertFalse(
                refusal_projection["error"]["details"]["fallbackAttempted"]
            )
            self.assertEqual(
                refusal_projection["terminal"],
                {
                    "classification": "runner-error",
                    "transportCompleted": False,
                    "terminalEventObserved": False,
                    "exitCode": None,
                    "signal": None,
                },
            )
            self.assertFalse(report["claims"]["releaseEligible"])
            self.assertEqual(benchmark.live_owned_probe_descriptions(), [])

    def test_authority_mismatch_and_existing_output_fail_without_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            packages = FIXTURES.make_release_package_output(
                fixture_root, platform_value="win32-x64"
            )
            benchmark = ADMISSION.load_benchmark()
            profile, native = authority_inputs(root, benchmark, packages, "win-x64")
            value = json.loads(profile.read_text("utf-8"))
            value["sourceSha"] = "f" * 40
            write_json(profile, value)
            out = root / "report.json"
            with self.assertRaises(ADMISSION.AdmissionError):
                ADMISSION.run_admission(
                    packages=packages,
                    work_root=root / "must-not-exist",
                    out=out,
                    target_id="win-x64",
                    version=VERSION,
                    source_sha=SOURCE_SHA,
                    control_sha=CONTROL_SHA,
                    workflow_run_id=123,
                    workflow_run_attempt=2,
                    profile_preflight=profile,
                    native_verification=native,
                    timeout_seconds=2,
                    deadline_monotonic=time.monotonic() + 10,
                    benchmark_module=benchmark,
                )
            self.assertFalse(out.exists())
            self.assertFalse((root / "must-not-exist").exists())
            occupied = root / "occupied-work"
            occupied.mkdir()
            marker = occupied / "preserve"
            marker.write_bytes(b"user data\n")
            with self.assertRaises(ADMISSION.AdmissionError):
                ADMISSION.prepare_root(occupied)
            self.assertEqual(marker.read_bytes(), b"user data\n")
            out.write_bytes(b"preserve\n")
            with self.assertRaises(ADMISSION.AdmissionError):
                ADMISSION.publish_exclusive(out, b"replacement\n")
            self.assertEqual(out.read_bytes(), b"preserve\n")

    def test_functional_alpha_placeholder_cannot_enter_full_release_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            packages = FIXTURES.make_release_package_output(
                fixture_root, platform_value="win32-x64"
            )
            benchmark = ADMISSION.load_benchmark()
            profile, native = authority_inputs(root, benchmark, packages, "win-x64")
            value = json.loads(profile.read_text("utf-8"))
            value["image"]["purpose"] = "functional-alpha-placeholder"
            write_json(profile, value)
            out = root / "report.json"
            with self.assertRaises(ADMISSION.AdmissionError):
                ADMISSION.run_admission(
                    packages=packages,
                    work_root=root / "must-not-exist",
                    out=out,
                    target_id="win-x64",
                    version=VERSION,
                    source_sha=SOURCE_SHA,
                    control_sha=CONTROL_SHA,
                    workflow_run_id=123,
                    workflow_run_attempt=2,
                    profile_preflight=profile,
                    native_verification=native,
                    timeout_seconds=2,
                    deadline_monotonic=time.monotonic() + 10,
                    benchmark_module=benchmark,
                )
            self.assertFalse(out.exists())
            self.assertFalse((root / "must-not-exist").exists())

    def test_stale_package_bytes_are_rejected_before_work_or_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            packages = FIXTURES.make_release_package_output(
                fixture_root, platform_value="win32-x64"
            )
            benchmark = ADMISSION.load_benchmark()
            profile, native = authority_inputs(root, benchmark, packages, "win-x64")
            artifact = next(packages.glob("*rust*.tar.gz"))
            artifact.write_bytes(artifact.read_bytes() + b"stale")
            with self.assertRaises(ADMISSION.AdmissionError):
                ADMISSION.run_admission(
                    packages=packages,
                    work_root=root / "work",
                    out=root / "report.json",
                    target_id="win-x64",
                    version=VERSION,
                    source_sha=SOURCE_SHA,
                    control_sha=CONTROL_SHA,
                    workflow_run_id=123,
                    workflow_run_attempt=2,
                    profile_preflight=profile,
                    native_verification=native,
                    timeout_seconds=2,
                    deadline_monotonic=time.monotonic() + 10,
                    benchmark_module=benchmark,
                )
            self.assertFalse((root / "work").exists())
            self.assertFalse((root / "report.json").exists())

    def test_corpus_is_closed_and_unknown_fields_are_rejected(self) -> None:
        benchmark = ADMISSION.load_benchmark()
        cases, identity = ADMISSION.load_corpus(benchmark)
        self.assertEqual(len(cases), 8)
        self.assertEqual(
            identity, ADMISSION.digest_record(ADMISSION.CORPUS.read_bytes())
        )
        help_bytes = (
            ROOT / "cli/conformance/cases/fixtures/runner-help.txt"
        ).read_bytes()
        help_identity = ADMISSION.digest_record(help_bytes)
        help_case = next(case for case in cases if case["id"] == "help")
        self.assertEqual(help_case["stdout"], {
            "kind": "exact-file",
            "path": "../cases/fixtures/runner-help.txt",
            **help_identity,
        })
        self.assertEqual(
            ADMISSION.expected_projection(
                "help",
                None,
                runner="rust",
                version=VERSION,
                source_sha=SOURCE_SHA,
                image={},
            ),
            {"kind": "help", **help_identity},
        )
        with tempfile.TemporaryDirectory() as temporary:
            mutated = Path(temporary) / "invariants.json"
            value = json.loads(ADMISSION.CORPUS.read_text("utf-8"))
            value["unexpected"] = True
            write_json(mutated, value)
            with mock.patch.object(ADMISSION, "CORPUS", mutated):
                with self.assertRaises(ADMISSION.AdmissionError):
                    ADMISSION.load_corpus(benchmark)

            reformatted = Path(temporary) / "reformatted.json"
            reformatted.write_text(
                json.dumps(json.loads(ADMISSION.CORPUS.read_text("utf-8"))), "utf-8"
            )
            with mock.patch.object(ADMISSION, "CORPUS", reformatted):
                _, drifted_identity = ADMISSION.load_corpus(benchmark)
            self.assertNotEqual(identity, drifted_identity)

    def test_mock_refusal_projection_rejects_transport_and_fallback_drift(self) -> None:
        image = {
            "formatVersion": "openprose.skill-runtime-image/1",
            "version": "canonical-v1",
            "sha256": "a" * 64,
            "releaseEligible": True,
        }
        details = {
            "harness": "mock",
            "admissionStatus": "blocked",
            "admissionBlock": "test-seams-disabled",
            "fallbackAttempted": False,
        }
        refused = {
            "schema": "openprose.runner-result/1",
            "runner": {"name": "rust", "version": VERSION, "commit": SOURCE_SHA},
            "adapter": {
                "id": "mock/unavailable",
                "harnessVersion": None,
                "descriptorDigestSha256": "599d3baf7b7aee1f97855dd4ca13e780c4de0aff410ad5860000631c0ac22fa4",
            },
            "transport": "deterministic",
            "terminal": {
                "classification": "runner-error",
                "transportCompleted": False,
                "terminalEventObserved": False,
                "exitCode": None,
                "signal": None,
            },
            "semantic": {
                "status": "unknown",
                "terminalEnvelopeDigestSha256": None,
            },
            "error": {
                "code": "HARNESS_UNAVAILABLE",
                "exitCode": 10,
                "details": details,
            },
            "runnerExitCode": 10,
        }
        ADMISSION.expected_projection(
            "run-mock-refused",
            refused,
            runner="rust",
            version=VERSION,
            source_sha=SOURCE_SHA,
            image=image,
        )
        mutations = []
        wrong_transport = json.loads(json.dumps(refused))
        wrong_transport["transport"] = "unavailable"
        mutations.append(wrong_transport)
        missing_fallback = json.loads(json.dumps(refused))
        del missing_fallback["error"]["details"]["fallbackAttempted"]
        mutations.append(missing_fallback)
        true_fallback = json.loads(json.dumps(refused))
        true_fallback["error"]["details"]["fallbackAttempted"] = True
        mutations.append(true_fallback)
        wrong_descriptor = json.loads(json.dumps(refused))
        wrong_descriptor["adapter"]["descriptorDigestSha256"] = "0" * 64
        mutations.append(wrong_descriptor)
        wrong_terminal = json.loads(json.dumps(refused))
        wrong_terminal["terminal"]["classification"] = "failure"
        mutations.append(wrong_terminal)
        for value in mutations:
            with self.assertRaises(ADMISSION.AdmissionError):
                ADMISSION.expected_projection(
                    "run-mock-refused",
                    value,
                    runner="rust",
                    version=VERSION,
                    source_sha=SOURCE_SHA,
                    image=image,
                )

        doctor = {
            "schema": "openprose.doctor-report/1",
            "runner": {"name": "rust", "version": VERSION, "commit": SOURCE_SHA},
            "build": {"profile": "release", "testSeamsEnabled": False},
            "image": image,
            "ready": False,
            "selectedHarness": "mock",
            "selectedTransport": "deterministic",
            "problems": [
                {"code": "HARNESS_UNAVAILABLE", "exitCode": 10, "details": details}
            ],
        }
        ADMISSION.expected_projection(
            "doctor-mock-refused",
            doctor,
            runner="rust",
            version=VERSION,
            source_sha=SOURCE_SHA,
            image=image,
        )
        doctor["selectedTransport"] = "unavailable"
        with self.assertRaises(ADMISSION.AdmissionError):
            ADMISSION.expected_projection(
                "doctor-mock-refused",
                doctor,
                runner="rust",
                version=VERSION,
                source_sha=SOURCE_SHA,
                image=image,
            )


if __name__ == "__main__":
    unittest.main()
