#!/usr/bin/env python3
"""Verify one exact release-mode package set without granting release authority."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


SCHEMA = "openprose.release-package-admission/3"
ERROR_SCHEMA = "openprose.release-package-admission-error/1"
CORPUS_SCHEMA = "openprose.release-package-invariants/1"
HERE = Path(__file__).resolve().parent
CLI = HERE.parent
CORPUS = CLI / "conformance" / "release-package" / "invariants.v1.json"
HELP = CLI / "conformance" / "cases" / "fixtures" / "runner-help.txt"
BENCHMARK = CLI / "benchmarks" / "installed" / "benchmark.py"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
TARGET_PLATFORMS = {
    "linux-x64": "linux-x64-gnu",
    "linux-arm64": "linux-arm64-gnu",
    "darwin-arm": "darwin-arm64",
    "darwin-x64": "darwin-x64",
    "win-x64": "win32-x64",
}
MAX_AUTHORITY_BYTES = 1024 * 1024
ROOT_MARKER = ".openprose-release-package-admission-root"


class AdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code: str, message: str) -> None:
    raise AdmissionError(code, message)


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def strict_object(encoded: bytes, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                fail("INPUT_MALFORMED", f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail("INPUT_MALFORMED", f"{label} is invalid UTF-8 JSON: {error}")
    if not isinstance(value, dict):
        fail("INPUT_MALFORMED", f"{label} must contain an object")
    return value


def exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        fail("INPUT_MALFORMED", f"{label} has unknown or missing fields")


def positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail("INPUT_MALFORMED", f"{label} must be a positive integer")
    return value


def digest_record(encoded: bytes) -> dict[str, Any]:
    return {"byteLength": len(encoded), "sha256": sha256(encoded)}


def load_benchmark() -> Any:
    spec = importlib.util.spec_from_file_location(
        "openprose_release_package_benchmark", BENCHMARK
    )
    if spec is None or spec.loader is None:
        fail("BENCHMARK_UNAVAILABLE", "installed-package verifier cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_input(benchmark: Any, path: Path, label: str) -> bytes:
    try:
        return benchmark.safe_read(path, MAX_AUTHORITY_BYTES)
    except Exception as error:
        fail(getattr(error, "code", "INPUT_UNSAFE"), f"{label} is unsafe: {error}")


def validate_identity_inputs(
    *,
    benchmark: Any,
    profile_path: Path,
    native_path: Path,
    target_id: str,
    version: str,
    source_sha: str,
    control_sha: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile_bytes = read_input(benchmark, profile_path, "profile preflight")
    native_bytes = read_input(benchmark, native_path, "native verification")
    profile = strict_object(profile_bytes, "profile preflight")
    native = strict_object(native_bytes, "native verification")
    exact_keys(
        profile,
        {
            "schema",
            "status",
            "releaseKind",
            "publicationAuthorized",
            "version",
            "sourceSha",
            "checkedOutSha",
            "controlSha",
            "controlRef",
            "productVersions",
            "image",
            "protectedAuthority",
            "gates",
            "failures",
        },
        "profile preflight",
    )
    if (
        profile.get("schema") != "openprose.release-preflight-report/1"
        or profile.get("status") != "pass"
        or profile.get("releaseKind") != "draft-only"
        or profile.get("publicationAuthorized") is not False
        or profile.get("version") != version
        or profile.get("sourceSha") != source_sha
        or profile.get("checkedOutSha") != source_sha
        or profile.get("controlSha") != control_sha
        or profile.get("controlRef") != "refs/heads/main"
        or profile.get("productVersions") != {"rust": version, "bun": version}
        or profile.get("failures") != []
    ):
        fail(
            "AUTHORITY_MISMATCH",
            "profile preflight is not the exact passing draft input",
        )
    protected = profile.get("protectedAuthority")
    if not isinstance(protected, dict) or protected.get("status") != "pass":
        fail("AUTHORITY_MISMATCH", "protected profile authority did not pass")
    image = profile.get("image")
    if not isinstance(image, dict) or set(image) != {
        "bundleSha256",
        "manifestSha256",
        "checksumSha256",
        "imageSha256",
        "version",
        "purpose",
        "releaseEligible",
    }:
        fail("AUTHORITY_MISMATCH", "profile image identity is not closed")
    for key in ("bundleSha256", "manifestSha256", "checksumSha256", "imageSha256"):
        if (
            not isinstance(image.get(key), str)
            or re.fullmatch(r"[0-9a-f]{64}", image[key]) is None
        ):
            fail("AUTHORITY_MISMATCH", f"profile image {key} is invalid")
    if (
        image.get("releaseEligible") is not True
        or image.get("purpose") != "canonical-language-runtime"
    ):
        fail("AUTHORITY_MISMATCH", "profile image is not release eligible")
    gates = profile.get("gates")
    if not isinstance(gates, dict) or set(gates) != {
        "canonicalProfile",
        "releaseEvidence",
    }:
        fail("AUTHORITY_MISMATCH", "profile gates are not closed")
    if any(
        not isinstance(gates[name], dict) or gates[name].get("status") != "pass"
        for name in gates
    ):
        fail("AUTHORITY_MISMATCH", "profile gates did not pass")

    exact_keys(
        native,
        {
            "schema",
            "target",
            "sourceSha",
            "nativeArtifact",
            "nativeManifestSha256",
            "windowsJobObjectReleaseAdmission",
            "windowsProcessHost",
            "containment",
            "reports",
        },
        "native verification",
    )
    if (
        native.get("schema") != "openprose.native-verification/1"
        or native.get("target") != target_id
        or native.get("sourceSha") != source_sha
        or native.get("windowsJobObjectReleaseAdmission") is not False
        or native.get("containment")
        != {
            "strictDescendantContainmentEnforced": False,
            "releaseEligible": False,
            "blocker": "detached-descendant-containment-not-enforced",
        }
    ):
        fail("NATIVE_LINEAGE_MISMATCH", "native verification boundary differs")
    artifact = native.get("nativeArtifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "name",
        "workflowRunId",
        "workflowRunAttempt",
        "files",
    }:
        fail("NATIVE_LINEAGE_MISMATCH", "native artifact identity is not closed")
    if artifact != {
        **artifact,
        "name": f"native-build-{target_id}",
        "workflowRunId": workflow_run_id,
        "workflowRunAttempt": workflow_run_attempt,
    }:
        fail("NATIVE_LINEAGE_MISMATCH", "native artifact workflow identity differs")
    files = artifact.get("files")
    if not isinstance(files, dict):
        fail("NATIVE_LINEAGE_MISMATCH", "native artifact files are malformed")
    suffix = ".exe" if target_id == "win-x64" else ""
    expected_files = {
        "native-manifest.json",
        f"prose-rust{suffix}",
        f"prose-bun{suffix}",
    }
    if target_id == "win-x64":
        expected_files.add("openprose-windows-process-host.exe")
    if set(files) != expected_files:
        fail("NATIVE_LINEAGE_MISMATCH", "native artifact file membership differs")
    for name, raw in files.items():
        if not isinstance(raw, dict) or set(raw) != {"byteLength", "sha256"}:
            fail("NATIVE_LINEAGE_MISMATCH", f"native file record is malformed: {name}")
        positive(raw.get("byteLength"), f"native file length {name}")
        if (
            not isinstance(raw.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) is None
        ):
            fail("NATIVE_LINEAGE_MISMATCH", f"native file digest is malformed: {name}")
    reports = native.get("reports")
    expected_image = {
        "formatVersion": "openprose.skill-runtime-image/1",
        "version": image["version"],
        "sha256": image["imageSha256"],
        "releaseEligible": True,
    }
    expected_reports = {
        name: {
            "runner": {"name": name, "version": version, "commit": source_sha},
            "build": {"profile": "release", "testSeamsEnabled": False},
            "image": expected_image,
        }
        for name in ("rust", "bun")
    }
    if reports != expected_reports:
        fail("NATIVE_LINEAGE_MISMATCH", "native product reports differ")
    native_manifest_sha = native.get("nativeManifestSha256")
    if (
        not isinstance(native_manifest_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", native_manifest_sha) is None
    ):
        fail("NATIVE_LINEAGE_MISMATCH", "native manifest digest is invalid")
    return (
        profile,
        native,
        {
            "profilePreflight": digest_record(profile_bytes),
            "nativeVerification": digest_record(native_bytes),
        },
    )


def load_corpus(
    benchmark: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    encoded = read_input(benchmark, CORPUS, "release invariant corpus")
    value = strict_object(encoded, "release invariant corpus")
    exact_keys(
        value, {"schema", "surfaces", "cases", "claims"}, "release invariant corpus"
    )
    if value.get("schema") != CORPUS_SCHEMA or value.get("claims") != {
        "fullPhase7Corpus": False,
        "languageSemantics": False,
        "programPortability": False,
        "providerCalls": "none",
    }:
        fail("CORPUS_MALFORMED", "release invariant corpus boundary differs")
    if value.get("surfaces") != [
        {"id": "direct-rust", "runner": "rust"},
        {"id": "direct-bun", "runner": "bun"},
        {"id": "npm-launcher", "runner": "bun"},
    ]:
        fail("CORPUS_MALFORMED", "release invariant surface order differs")
    cases = value.get("cases")
    if not isinstance(cases, list) or [
        case.get("id") if isinstance(case, dict) else None for case in cases
    ] != [
        "version",
        "help",
        "config-explain",
        "harness-list",
        "doctor-default",
        "doctor-mock-refused",
        "run-mock-refused",
        "dry-run-mock-refused",
    ]:
        fail("CORPUS_MALFORMED", "release invariant case order differs")
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "id",
            "argv",
            "exitCode",
            "stdout",
        }:
            fail("CORPUS_MALFORMED", "release invariant case shape differs")
        if not isinstance(case["argv"], list) or not all(
            isinstance(item, str) and item for item in case["argv"]
        ):
            fail("CORPUS_MALFORMED", "release invariant argv is invalid")
        output = case["stdout"]
        if not isinstance(output, dict) or output.get("kind") not in {
            "version",
            "exact-file",
            "json",
        }:
            fail("CORPUS_MALFORMED", "release invariant output contract is invalid")
    return cases, digest_record(encoded)


def prepare_root(path: Path) -> Path:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        fail("WORK_ROOT_UNSAFE", "work root must not already exist")
    try:
        parent = requested.parent.resolve(strict=True)
        metadata = parent.lstat()
    except OSError as error:
        fail("WORK_ROOT_UNSAFE", f"work-root parent is unavailable: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        fail("WORK_ROOT_UNSAFE", "work-root parent must be a real directory")
    root = parent / requested.name
    try:
        root.mkdir(mode=0o700)
        with (root / ROOT_MARKER).open("xb") as marker:
            marker.write(b"owned release package admission root\n")
    except OSError as error:
        fail("WORK_ROOT_UNSAFE", f"cannot create work root: {error}")
    return root


def snapshot_package(
    benchmark: Any, context: Mapping[str, Any], destination: Path
) -> dict[str, Any]:
    destination.mkdir(mode=0o700)
    for name in sorted(context["encoded"]):
        target = destination / name
        with target.open("xb") as output:
            output.write(context["encoded"][name])
        target.chmod(0o400)
    sums = benchmark.safe_read(
        context["root"] / "SHA256SUMS", benchmark.MAX_EVIDENCE_BYTES
    )
    with (destination / "SHA256SUMS").open("xb") as output:
        output.write(sums)
    return benchmark.verify_package_output(
        destination,
        expected_platform=context["platform"],
        purpose="release-invariants",
    )


def expected_projection(
    case_id: str,
    value: dict[str, Any] | None,
    *,
    runner: str,
    version: str,
    source_sha: str,
    image: Mapping[str, Any],
) -> Any:
    if case_id == "version":
        return {"kind": "version", "runner": runner, "version": version}
    if case_id == "help":
        return {
            "kind": "help",
            "byteLength": 7486,
            "sha256": "d58587215fa0a5182433a81763c3a70370a92f7926c83f0b4187069025656096",
        }
    assert value is not None
    if value.get("schema") == "openprose.configuration-explanation/1":
        values = value.get("values")
        if not isinstance(values, dict):
            fail("INVARIANT_FAILED", "configuration values are malformed")
        projection = {
            "schema": value.get("schema"),
            "harness": values.get("harness", {}).get("value"),
            "transport": values.get("transport", {}).get("value"),
            "output": values.get("output", {}).get("value"),
            "color": values.get("color", {}).get("value"),
        }
        if projection != {
            "schema": "openprose.configuration-explanation/1",
            "harness": "openprose",
            "transport": "auto",
            "output": "json",
            "color": False,
        }:
            fail("INVARIANT_FAILED", "configuration projection differs")
        return projection
    if value.get("schema") == "openprose.harness-list/1":
        harnesses = value.get("harnesses")
        if not isinstance(harnesses, list):
            fail("INVARIANT_FAILED", "harness inventory is malformed")
        matches = [
            item
            for item in harnesses
            if isinstance(item, dict) and item.get("id") == "mock"
        ]
        if len(matches) != 1:
            fail("INVARIANT_FAILED", "harness inventory lacks one mock record")
        mock_record = matches[0]
        projection = {
            "schema": value.get("schema"),
            "selected": value.get("selected"),
            "availability": mock_record.get("availability"),
            "detectedVersion": mock_record.get("detectedVersion"),
            "strictWrapperConformant": mock_record.get("strictWrapperConformant"),
            "testOnly": mock_record.get("testOnly"),
            "admissionBlock": mock_record.get("admissionBlock"),
        }
        if projection != {
            "schema": "openprose.harness-list/1",
            "selected": "openprose",
            "availability": "unavailable",
            "detectedVersion": None,
            "strictWrapperConformant": False,
            "testOnly": True,
            "admissionBlock": "test-seams-disabled",
        }:
            fail("INVARIANT_FAILED", "release mock inventory projection differs")
        return projection
    if value.get("schema") == "openprose.doctor-report/1":
        expected_runner = {"name": runner, "version": version, "commit": source_sha}
        expected_image = {
            key: image[key]
            for key in ("formatVersion", "version", "sha256", "releaseEligible")
        }
        if (
            value.get("runner") != expected_runner
            or value.get("build") != {"profile": "release", "testSeamsEnabled": False}
            or value.get("image") != expected_image
            or value.get("ready") is not False
        ):
            fail("INVARIANT_FAILED", "doctor build identity differs")
        expected_harness = "mock" if case_id == "doctor-mock-refused" else "openprose"
        expected_error = (
            "HARNESS_UNAVAILABLE"
            if expected_harness == "mock"
            else "HOSTED_UNAVAILABLE"
        )
        problems = value.get("problems")
        if (
            value.get("selectedHarness") != expected_harness
            or not isinstance(problems, list)
            or not problems
            or problems[0].get("code") != expected_error
        ):
            fail("INVARIANT_FAILED", "doctor refusal projection differs")
        if expected_harness == "mock" and (
            value.get("selectedTransport") != "deterministic"
            or problems[0].get("details")
            != {
                "harness": "mock",
                "admissionStatus": "blocked",
                "admissionBlock": "test-seams-disabled",
                "fallbackAttempted": False,
            }
        ):
            fail(
                "INVARIANT_FAILED", "mock doctor transport or fallback evidence differs"
            )
        return {
            "schema": value["schema"],
            "runner": expected_runner,
            "ready": False,
            "harness": expected_harness,
            "error": expected_error,
            "build": value["build"],
            "image": value["image"],
        }
    if value.get("schema") == "openprose.runner-result/1":
        if value.get("runner") != {
            "name": runner,
            "version": version,
            "commit": source_sha,
        }:
            fail("INVARIANT_FAILED", "mock refusal runner identity differs")
        adapter = value.get("adapter")
        terminal = value.get("terminal")
        semantic = value.get("semantic")
        error = value.get("error")
        if (
            not isinstance(adapter, dict)
            or adapter
            != {
                "id": "mock/unavailable",
                "harnessVersion": None,
                "descriptorDigestSha256": "599d3baf7b7aee1f97855dd4ca13e780c4de0aff410ad5860000631c0ac22fa4",
            }
            or not isinstance(terminal, dict)
            or terminal.get("classification") != "runner-error"
            or terminal.get("transportCompleted") is not False
            or terminal.get("terminalEventObserved") is not False
            or terminal.get("exitCode") is not None
            or terminal.get("signal") is not None
            or not isinstance(semantic, dict)
            or semantic.get("status") != "unknown"
            or semantic.get("terminalEnvelopeDigestSha256") is not None
            or not isinstance(error, dict)
            or error.get("code") != "HARNESS_UNAVAILABLE"
            or error.get("exitCode") != 10
            or error.get("details")
            != {
                "harness": "mock",
                "admissionStatus": "blocked",
                "admissionBlock": "test-seams-disabled",
                "fallbackAttempted": False,
            }
            or value.get("transport") != "deterministic"
            or value.get("runnerExitCode") != 10
        ):
            fail("INVARIANT_FAILED", "mock refusal terminal projection differs")
        return {
            "schema": value["schema"],
            "runner": {"name": runner, "version": version, "commit": source_sha},
            "adapter": adapter,
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
                "details": error["details"],
            },
            "runnerExitCode": 10,
        }
    fail("INVARIANT_FAILED", f"unsupported invariant output schema for {case_id}")


def run_cases(
    benchmark: Any,
    installed: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    *,
    version: str,
    source_sha: str,
    deadline: float,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    environment = dict(installed["runEnvironment"])
    canary = installed["install"] / "spawn-canary.py"
    spawn_marker = installed["install"] / "must-not-spawn"
    observation = installed["install"] / "must-not-observe.json"
    canary.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"Path({str(spawn_marker)!r}).write_bytes(b'spawned\\n')\n",
        "utf-8",
    )
    canary.chmod(0o500)
    environment.update(
        {
            "OPENPROSE_CONFORMANCE_FAKE_HARNESS": str(canary),
            "OPENPROSE_CONFORMANCE_FAKE_SCENARIO": "success",
            "OPENPROSE_CONFORMANCE_FAKE_OBSERVATION": str(observation),
        }
    )
    image = installed["release"]["image"]
    (installed["workspace"] / ".admission-workspace-marker").write_bytes(
        b"release invariant workspace custody\n"
    )
    workspace_identity = benchmark.capture_installed_tree(installed["workspace"])
    records: list[dict[str, Any]] = []
    bun_outputs: dict[str, bytes] = {}
    projections: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = case["id"]
        observations = []
        projections[case_id] = {}
        for surface in ("direct-rust", "direct-bun", "npm-launcher"):
            benchmark.remaining_before(deadline, f"before {surface} {case_id}")
            target = installed["surfaces"][surface]
            outcome = benchmark.run_process_before_deadline(
                [str(target["executable"]), *case["argv"]],
                installed["workspace"],
                environment,
                timeout_seconds,
                f"release invariant {case_id} on {surface}",
                deadline,
            )
            if outcome["exitCode"] != case["exitCode"] or outcome["stderr"]:
                fail("INVARIANT_FAILED", f"{case_id} failed on {surface}")
            contract = case["stdout"]
            parsed: dict[str, Any] | None = None
            if contract["kind"] == "version":
                expected = f"prose {version} ({target['runner']})\n".encode()
                if outcome["stdout"] != expected:
                    fail("INVARIANT_FAILED", f"version differs on {surface}")
            elif contract["kind"] == "exact-file":
                expected = benchmark.safe_read(HELP, benchmark.MAX_EVIDENCE_BYTES)
                if (
                    len(expected) != contract["byteLength"]
                    or sha256(expected) != contract["sha256"]
                    or outcome["stdout"] != expected
                ):
                    fail("INVARIANT_FAILED", f"help differs on {surface}")
            else:
                parsed = strict_object(outcome["stdout"], f"{surface} {case_id} output")
                if parsed.get("schema") != contract["schema"]:
                    fail(
                        "INVARIANT_FAILED",
                        f"output schema differs on {surface} {case_id}",
                    )
            projections[case_id][surface] = expected_projection(
                case_id,
                parsed,
                runner=target["runner"],
                version=version,
                source_sha=source_sha,
                image=image,
            )
            observations.append(
                {
                    "surface": surface,
                    "exitCode": outcome["exitCode"],
                    "stdout": digest_record(outcome["stdout"]),
                    "stderr": digest_record(outcome["stderr"]),
                    "settlement": outcome["settlement"],
                    "settlementAuthority": outcome["settlementAuthority"],
                    "projection": projections[case_id][surface],
                }
            )
            if surface == "direct-bun":
                bun_outputs[case_id] = outcome["stdout"]
            elif (
                surface == "npm-launcher" and outcome["stdout"] != bun_outputs[case_id]
            ):
                fail("PARITY_FAILED", f"npm and Bun output differ for {case_id}")
        rust_projection = normalized_projection_for_parity(
            projections[case_id]["direct-rust"]
        )
        bun_projection = normalized_projection_for_parity(
            projections[case_id]["direct-bun"]
        )
        if rust_projection != bun_projection:
            fail(
                "PARITY_FAILED",
                f"Rust and Bun invariant projections differ for {case_id}",
            )
        records.append(
            {"id": case_id, "argv": list(case["argv"]), "observations": observations}
        )
    benchmark.verify_installed_tree(installed["workspace"], workspace_identity)
    if spawn_marker.exists() or observation.exists():
        fail("UNEXPECTED_SPAWN", "release invariant reached a compiled-out test seam")
    if benchmark.live_owned_probe_descriptions():
        fail("PROCESS_UNSETTLED", "release invariant left an owned process active")
    return records


def normalized_projection_for_parity(projection: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(projection))
    runner = normalized.get("runner")
    if isinstance(runner, dict):
        runner["name"] = "<runner>"
    elif isinstance(runner, str):
        normalized["runner"] = "<runner>"
    return normalized


def package_records(
    benchmark: Any, context: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    sums = benchmark.safe_read(
        context["root"] / "SHA256SUMS", benchmark.MAX_EVIDENCE_BYTES
    )
    files = [
        {"path": name, "byteLength": len(value), "sha256": sha256(value)}
        for name, value in sorted(context["encoded"].items())
    ] + [{"path": "SHA256SUMS", **digest_record(sums)}]
    files.sort(key=lambda item: item["path"])
    return (
        files,
        digest_record(sums),
        digest_record(context["encoded"]["release-manifest.json"]),
    )


def native_lineage(
    native: Mapping[str, Any], payloads: Mapping[str, Any], target_id: str
) -> dict[str, Any]:
    suffix = ".exe" if target_id == "win-x64" else ""
    files = native["nativeArtifact"]["files"]
    products = {}
    for implementation in ("rust", "bun"):
        path = f"prose-{implementation}{suffix}"
        packaged = payloads["extracted"][implementation]["binarySha256"]
        if files[path]["sha256"] != packaged:
            fail(
                "NATIVE_LINEAGE_MISMATCH",
                f"packaged {implementation} binary differs from native artifact",
            )
        products[implementation] = {
            "nativePath": path,
            "nativeSha256": files[path]["sha256"],
            "packagedBinarySha256": packaged,
        }
    windows = native["windowsProcessHost"]
    if target_id == "win-x64":
        if windows != payloads["release"]["windowsProcessHost"]:
            fail(
                "NATIVE_LINEAGE_MISMATCH",
                "packaged Windows sidecar differs from native verification",
            )
    elif (
        windows is not None
        or payloads["release"]["windowsProcessHost"] != "not-applicable"
    ):
        fail("NATIVE_LINEAGE_MISMATCH", "non-Windows lineage contains a sidecar")
    return {
        "nativeArtifact": {
            "name": native["nativeArtifact"]["name"],
            "workflowRunId": native["nativeArtifact"]["workflowRunId"],
            "workflowRunAttempt": native["nativeArtifact"]["workflowRunAttempt"],
        },
        "nativeManifestSha256": native["nativeManifestSha256"],
        "files": files,
        "products": products,
        "windowsProcessHost": windows if windows is not None else "not-applicable",
    }


def surface_records(
    benchmark: Any, payloads: Mapping[str, Any], installed: Mapping[str, Any] | None
) -> dict[str, Any]:
    result = {}
    installs = (
        {}
        if installed is None
        else {item["surface"]: item for item in installed["installations"]}
    )
    for surface, runner in (
        ("direct-rust", "rust"),
        ("direct-bun", "bun"),
        ("npm-launcher", "bun"),
    ):
        implementation = "rust" if surface == "direct-rust" else "bun"
        if surface == "npm-launcher":
            npm = payloads["npmPackage"]
            artifact_sha = payloads["context"]["artifacts"][npm["platformName"]][
                "sha256"
            ]
            meta_sha = payloads["context"]["artifacts"][npm["metaName"]]["sha256"]
            launcher_sha = sha256(npm["launcher"])
        else:
            artifact_sha = payloads["extracted"][implementation]["artifact"]["sha256"]
            meta_sha = None
            launcher_sha = None
        tree_sha = None
        command_identity = None
        if installed is not None:
            tree_sha = installs[surface]["treeIdentity"]["digestSha256"]
            if surface == "npm-launcher":
                command_identity = installed["commandIdentity"]
        result[surface] = {
            "runner": runner,
            "binarySha256": payloads["extracted"][implementation]["binarySha256"],
            "artifactSha256": artifact_sha,
            "metaArtifactSha256": meta_sha,
            "installationTreeSha256": tree_sha,
            "launcherSourceSha256": launcher_sha,
            "launcherCommandIdentity": command_identity,
            "execution": "blocked-before-execution"
            if installed is None
            else "verified-posix",
        }
    return result


def execution_toolchain(
    benchmark: Any, installed: Mapping[str, Any] | None
) -> dict[str, Any]:
    if installed is None:
        return {
            "authority": "not-observed-no-candidate-execution",
            "node": None,
            "npm": None,
        }
    records = {name: installed[f"{name}Tool"] for name in ("node", "npm")}
    for name, record in records.items():
        encoded = benchmark.safe_read(
            Path(record["resolvedPath"]), benchmark.MAX_TOOL_BYTES
        )
        if sha256(encoded) != record["sha256"]:
            fail(
                "TOOL_MUTATED",
                f"{name} executable bytes changed after package execution",
            )
    return {
        "authority": "reporter-observed-and-finally-reauthenticated-executable-bytes",
        **records,
    }


def run_admission(
    *,
    packages: Path,
    work_root: Path,
    out: Path,
    target_id: str,
    version: str,
    source_sha: str,
    control_sha: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    profile_preflight: Path,
    native_verification: Path,
    timeout_seconds: float,
    deadline_monotonic: float,
    benchmark_module: Any | None = None,
) -> dict[str, Any]:
    if target_id not in TARGET_PLATFORMS:
        fail("ARGUMENT_INVALID", "target-id is unsupported")
    if (
        SEMVER.fullmatch(version) is None
        or FULL_SHA.fullmatch(source_sha) is None
        or FULL_SHA.fullmatch(control_sha) is None
    ):
        fail("ARGUMENT_INVALID", "version/source/control identity is malformed")
    positive(workflow_run_id, "workflow-run-id")
    positive(workflow_run_attempt, "workflow-run-attempt")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
        fail("ARGUMENT_INVALID", "timeout-seconds must be finite and in (0,120]")
    if not math.isfinite(deadline_monotonic) or deadline_monotonic <= time.monotonic():
        fail("ARGUMENT_INVALID", "deadline must be a finite future monotonic time")
    if os.path.lexists(out):
        fail("OUTPUT_UNSAFE", "output must not already exist")
    benchmark = benchmark_module or load_benchmark()
    profile, native, authority_inputs = validate_identity_inputs(
        benchmark=benchmark,
        profile_path=profile_preflight,
        native_path=native_verification,
        target_id=target_id,
        version=version,
        source_sha=source_sha,
        control_sha=control_sha,
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
    )
    cases, corpus_identity = load_corpus(benchmark)
    platform_value = TARGET_PLATFORMS[target_id]
    try:
        source_context = benchmark.verify_package_output(
            packages, expected_platform=platform_value, purpose="release-invariants"
        )
    except Exception as error:
        fail(
            getattr(error, "code", "PACKAGE_MALFORMED"),
            f"package verification failed: {error}",
        )
    release = source_context["release"]
    if release["version"] != version or release["source"]["revision"] != source_sha:
        fail(
            "IDENTITY_DIVERGENCE",
            "package version/source differs from requested identity",
        )
    expected_image = {
        "formatVersion": "openprose.skill-runtime-image/1",
        "version": profile["image"]["version"],
        "sha256": profile["image"]["imageSha256"],
        "manifestSha256": profile["image"]["manifestSha256"],
        "purpose": profile["image"]["purpose"],
        "releaseEligible": True,
    }
    if release["image"] != expected_image:
        fail("AUTHORITY_MISMATCH", "package image differs from profile preflight")
    gates = profile["gates"]
    package_gates = release["externalGates"]
    if (
        package_gates.get("authorityValidatedByPackager") is not False
        or package_gates["canonicalProfile"].get("sha256")
        != gates["canonicalProfile"].get("sha256")
        or package_gates["releaseEvidence"].get("sha256")
        != gates["releaseEvidence"].get("sha256")
    ):
        fail(
            "AUTHORITY_MISMATCH", "package external gate digests differ from preflight"
        )
    root = prepare_root(work_root)
    snapshot_context = snapshot_package(
        benchmark, source_context, root / "package-snapshot"
    )
    payloads = benchmark.validate_package_payloads(snapshot_context)
    payloads["context"] = snapshot_context
    lineage = native_lineage(native, payloads, target_id)
    installed = None
    case_records: list[dict[str, Any]] = []
    installations: list[dict[str, Any]] = []
    if target_id != "win-x64":
        if benchmark.current_platform_id() != platform_value:
            fail(
                "PLATFORM_UNSUPPORTED",
                "candidate execution requires the matching native host",
            )
        installed = benchmark.install_verified_package_set(
            snapshot_context,
            root / "install",
            timeout_seconds=timeout_seconds,
            deadline_monotonic=deadline_monotonic,
        )
        case_records = run_cases(
            benchmark,
            installed,
            cases,
            version=version,
            source_sha=source_sha,
            deadline=deadline_monotonic,
            timeout_seconds=timeout_seconds,
        )
        for record in installed["installations"]:
            installations.append(
                {
                    "surface": record["surface"],
                    "method": record["method"],
                    "installedByteCount": record["installedByteCount"],
                    "treeSha256": record["treeIdentity"]["digestSha256"],
                }
            )
            if record["surface"] == "npm-launcher":
                command, meta_root, _, _ = benchmark.npm_layout(
                    installed["npmPrefix"], platform_value
                )
                links = (
                    {command: meta_root / "bin" / "prose.js"}
                    if command.is_symlink()
                    else {}
                )
                benchmark.verify_installed_tree(
                    installed["npmPrefix"], record["treeIdentity"], links
                )
            else:
                benchmark.verify_installed_tree(
                    installed["extracted"][record["surface"].removeprefix("direct-")][
                        "destination"
                    ],
                    record["treeIdentity"],
                )
    benchmark.assert_package_output_unchanged(source_context)
    benchmark.assert_package_output_unchanged(snapshot_context)
    files, sums_record, manifest_record = package_records(benchmark, snapshot_context)
    status = (
        "blocked-before-execution"
        if target_id == "win-x64"
        else "passed-provider-free-release-invariants"
    )
    report = {
        "schema": SCHEMA,
        "status": status,
        "targetId": target_id,
        "version": version,
        "sourceSha": source_sha,
        "controlSha": control_sha,
        "workflowRun": {"id": workflow_run_id, "attempt": workflow_run_attempt},
        "authorityInputs": authority_inputs,
        "corpus": corpus_identity,
        "package": {
            "platform": platform_value,
            "mode": "release",
            "sha256Sums": sums_record,
            "releaseManifest": manifest_record,
            "files": files,
            "image": release["image"],
            "buildProfiles": release["buildProfiles"],
            "nativeLineage": lineage,
        },
        "installations": installations,
        "executionToolchain": execution_toolchain(benchmark, installed),
        "surfaces": surface_records(benchmark, payloads, installed),
        "cases": case_records,
        "claims": {
            "providerCalls": "not-observed",
            "semanticEvaluation": False,
            "programPortabilityEvaluation": False,
            "releaseEligible": False,
            "publicationAuthorized": False,
            "rankingProduced": False,
            "strictDescendantContainment": False,
            "runtimeNetworkIsolation": False,
            "candidateExecution": "blocked-before-execution"
            if target_id == "win-x64"
            else "performed-posix-invariants",
        },
    }
    publish_exclusive(out, canonical_json(report))
    return report


def publish_exclusive(path: Path, encoded: bytes) -> None:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        fail("OUTPUT_UNSAFE", "output must not already exist")
    try:
        parent = requested.parent.resolve(strict=True)
        metadata = parent.lstat()
    except OSError as error:
        fail("OUTPUT_UNSAFE", f"output parent is unavailable: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        fail("OUTPUT_UNSAFE", "output parent must be a real directory")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".release-package-admission-", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, requested, follow_symlinks=False)
        if requested.read_bytes() != encoded:
            fail("OUTPUT_UNSAFE", "published report bytes differ")
    except FileExistsError:
        fail("OUTPUT_UNSAFE", "output was created concurrently")
    except OSError as error:
        fail("OUTPUT_UNSAFE", f"cannot publish report atomically: {error}")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ClosedParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        fail("ARGUMENT_INVALID", message)


def parser() -> argparse.ArgumentParser:
    result = ClosedParser(description=__doc__)
    result.add_argument("--packages", type=Path, required=True)
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--out", type=Path, required=True)
    result.add_argument("--target-id", choices=tuple(TARGET_PLATFORMS), required=True)
    result.add_argument("--version", required=True)
    result.add_argument("--source-sha", required=True)
    result.add_argument("--control-sha", required=True)
    result.add_argument("--workflow-run-id", type=int, required=True)
    result.add_argument("--workflow-run-attempt", type=int, required=True)
    result.add_argument("--profile-preflight", type=Path, required=True)
    result.add_argument("--native-verification", type=Path, required=True)
    result.add_argument("--timeout-seconds", type=float, default=10.0)
    result.add_argument("--budget-seconds", type=float, default=300.0)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if (
            not math.isfinite(args.budget_seconds)
            or not 5 <= args.budget_seconds <= 1800
        ):
            fail("ARGUMENT_INVALID", "budget-seconds must be finite and in [5,1800]")
        report = run_admission(
            packages=args.packages,
            work_root=args.work_root,
            out=args.out,
            target_id=args.target_id,
            version=args.version,
            source_sha=args.source_sha,
            control_sha=args.control_sha,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            profile_preflight=args.profile_preflight,
            native_verification=args.native_verification,
            timeout_seconds=args.timeout_seconds,
            deadline_monotonic=time.monotonic() + args.budget_seconds,
        )
        sys.stdout.buffer.write(canonical_json(report))
        return 0
    except AdmissionError as error:
        sys.stderr.buffer.write(
            canonical_json(
                {"schema": ERROR_SCHEMA, "code": error.code, "message": error.message}
            )
        )
        return 1
    except Exception:
        sys.stderr.buffer.write(
            canonical_json(
                {
                    "schema": ERROR_SCHEMA,
                    "code": "INTERNAL_ERROR",
                    "message": "release package admission failed at a closed internal boundary",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
