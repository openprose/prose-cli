#!/usr/bin/env python3
"""Black-box and differential admission runner for installed `prose` artifacts."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError as error:  # pragma: no cover
    raise SystemExit(
        "Install the pinned contract test dependencies from "
        "cli/shared/requirements-test.txt"
    ) from error


CLI = Path(__file__).resolve().parents[2]
ROOT = CLI.parent
CASES = CLI / "conformance" / "cases"
SCHEMAS = CLI / "shared" / "schemas"
SENTINEL = CLI / "shared" / "image" / "sentinel-v1"
FAKE_HARNESS = CLI / "conformance" / "fake-harness" / "fake_harness.py"
INSTALLED_ADAPTER_HARNESS = (
    CLI / "conformance" / "adversarial" / "adapter-products" / "fake_live_harness.py"
)
BUN_RUNTIME_FIXTURE = (
    b"#!/usr/bin/env python3\n"
    b"import sys\n"
    b"if sys.argv[1:] == ['--version']:\n"
    b"    print('1.3.14')\n"
    b"    raise SystemExit(0)\n"
    b"raise SystemExit(64)\n"
)
INSTALLED_ADAPTER_EXECUTABLES = {
    "codex/exec-json": "codex",
    "claude/print-stream-json": "claude",
    "prime/rpc": "prime-agent",
    "omp/rpc": "omp",
}
WRONG_STREAM_VERSION_HARNESS = b"""#!/usr/bin/env python3
import pathlib
import sys

VERSIONS = {
    "codex": "codex-cli 0.149.0-alpha.4.1",
    "claude": "2.1.243 (Claude Code)",
    "prime-agent": "prime-agent 0.7.0",
    "omp": "omp/18.0.9",
}

executable = pathlib.Path(sys.argv[0]).name
if sys.argv[1:] != ["--version"] or executable not in VERSIONS:
    raise SystemExit(88)
stream = sys.stdout if executable == "prime-agent" else sys.stderr
print(VERSIONS[executable], file=stream)
print("wrong-stream-private-output", file=stream)
"""
DEFAULT_RUST = CLI / "rust" / "target" / "debug" / "prose"
DEFAULT_BUN = CLI / "bun" / "dist" / "prose"
MAX_FAILURE_BYTES = 1024
MAX_CANDIDATE_BYTES = 256 * 1024 * 1024
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
CAPTURE_TRUNCATION_MARKER = b"\n...[output truncated by conformance runner]\n"
NODE_COMMONJS_SNAPSHOT_BOOTSTRAP = """
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const [snapshot, sourceFilename, ...opaqueArgv] = process.argv.slice(1);
if (!snapshot || !sourceFilename) throw new Error("missing conformance snapshot context");
process.argv = [process.execPath, sourceFilename, ...opaqueArgv];
const candidate = new Module(sourceFilename, module);
candidate.filename = sourceFilename;
candidate.paths = Module._nodeModulePaths(path.dirname(sourceFilename));
candidate._compile(fs.readFileSync(snapshot, "utf8"), sourceFilename);
""".strip()


@dataclass(frozen=True)
class Product:
    name: str
    executable: Path
    runner_name: str | None = None
    interpreter: Path | None = None
    execution_executable: Path | None = None
    execution_interpreter: Path | None = None
    execution_source_context: Path | None = None

    @property
    def expected_runner_name(self) -> str:
        return self.runner_name or self.name

    def execution_argv(self, arguments: list[str]) -> list[str]:
        executable = self.execution_executable or self.executable
        interpreter = self.execution_interpreter or self.interpreter
        if interpreter is not None and self.execution_source_context is not None:
            return [
                str(interpreter),
                "-e",
                NODE_COMMONJS_SNAPSHOT_BOOTSTRAP,
                "--",
                str(executable),
                str(self.execution_source_context),
                *arguments,
            ]
        return [
            *(
                [str(interpreter), str(executable)]
                if interpreter is not None
                else [str(executable)]
            ),
            *arguments,
        ]


@dataclass(frozen=True)
class CandidateIdentity:
    leaf_kind: str
    leaf_byte_length: int
    leaf_sha256: str
    target_byte_length: int
    target_sha256: str
    leaf_and_target_same_bytes: bool
    interpreter: CandidateIdentity | None = None


@dataclass
class Observation:
    product: Product
    case: dict[str, Any]
    exit_code: int
    stdout: bytes
    stderr: bytes
    parsed: Any = None
    fake_observation: Path | None = None
    workspace: Path | None = None
    process_settled: bool = True
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class OwnedProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    settled: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("resolved executable target is not a regular file")
        if before.st_size <= 0:
            raise ValueError("resolved executable target is empty")
        if before.st_size > MAX_CANDIDATE_BYTES:
            raise ValueError(
                "resolved executable target exceeds the candidate byte limit"
            )
        digest = hashlib.sha256()
        length = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity or length != after.st_size:
            raise ValueError("resolved executable target changed while hashing")
        return length, digest.hexdigest()
    finally:
        os.close(descriptor)


def _absolute_leaf(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _capture_executable_identity(path: Path) -> CandidateIdentity:
    leaf = _absolute_leaf(path)
    leaf_status = leaf.lstat()
    if stat.S_ISLNK(leaf_status.st_mode):
        link_bytes = os.fsencode(os.readlink(leaf))
        target = leaf.resolve(strict=True)
        target_length, target_digest = _digest_regular_file(target)
        if not os.access(leaf, os.X_OK):
            raise ValueError("resolved executable target is not executable")
        return CandidateIdentity(
            leaf_kind="symlink",
            leaf_byte_length=len(link_bytes),
            leaf_sha256=sha256(link_bytes),
            target_byte_length=target_length,
            target_sha256=target_digest,
            leaf_and_target_same_bytes=False,
        )
    if not stat.S_ISREG(leaf_status.st_mode):
        raise ValueError("executable leaf is not a regular file or symlink")
    length, digest = _digest_regular_file(leaf)
    if not os.access(leaf, os.X_OK):
        raise ValueError("executable leaf is not executable")
    return CandidateIdentity(
        leaf_kind="regular-file",
        leaf_byte_length=length,
        leaf_sha256=digest,
        target_byte_length=length,
        target_sha256=digest,
        leaf_and_target_same_bytes=True,
    )


def capture_candidate_identity(product: Product) -> CandidateIdentity:
    executable = _capture_executable_identity(product.executable)
    interpreter = (
        _capture_executable_identity(product.interpreter)
        if product.interpreter is not None
        else None
    )
    return CandidateIdentity(
        leaf_kind=executable.leaf_kind,
        leaf_byte_length=executable.leaf_byte_length,
        leaf_sha256=executable.leaf_sha256,
        target_byte_length=executable.target_byte_length,
        target_sha256=executable.target_sha256,
        leaf_and_target_same_bytes=executable.leaf_and_target_same_bytes,
        interpreter=interpreter,
    )


def _copy_target_to_owned_snapshot(
    source: Path,
    destination: Path,
    *,
    expected_length: int,
    expected_sha256: str,
) -> None:
    target = _absolute_leaf(source).resolve(strict=True)
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(target, source_flags)
    destination_descriptor: int | None = None
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("snapshot source is not a regular file")
        if before.st_size != expected_length:
            raise ValueError("candidate target bytes changed before snapshot")
        destination.parent.mkdir(parents=True, exist_ok=False)
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o500,
        )
        digest = hashlib.sha256()
        length = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:  # pragma: no cover - defensive OS contract guard
                    raise OSError("snapshot write made no progress")
                view = view[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity:
            raise ValueError("candidate target changed while snapshotting")
        if length != expected_length or digest.hexdigest() != expected_sha256:
            raise ValueError("candidate target bytes changed before snapshot")
    except BaseException:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)
    destination.chmod(0o500)


def _snapshot_suffix(path: Path) -> str:
    try:
        return _absolute_leaf(path).resolve(strict=True).suffix
    except OSError:
        return path.suffix


def _uses_node_commonjs_source_context(
    executable: Path, interpreter: Path | None
) -> bool:
    if interpreter is None:
        return False
    resolved_executable = _absolute_leaf(executable).resolve(strict=True)
    resolved_interpreter = _absolute_leaf(interpreter).resolve(strict=True)
    return resolved_executable.suffix.lower() in {
        ".js",
        ".cjs",
    } and resolved_interpreter.name.lower() in {"node", "node.exe"}


def snapshot_product(
    product: Product, identity: CandidateIdentity, snapshot_root: Path
) -> Product:
    executable = snapshot_root / f"candidate{_snapshot_suffix(product.executable)}"
    _copy_target_to_owned_snapshot(
        product.executable,
        executable,
        expected_length=identity.target_byte_length,
        expected_sha256=identity.target_sha256,
    )
    interpreter: Path | None = None
    if product.interpreter is not None:
        if identity.interpreter is None:  # pragma: no cover - dataclass invariant
            raise ValueError("interpreter identity is unavailable")
        # Interpreters such as Homebrew Node may resolve a dynamic-library or
        # resource closure relative to their installed executable. Copying only
        # the executable changes that meaning. Execute its resolved installed
        # path and bind its exact bytes before and after each invocation instead.
        interpreter = _absolute_leaf(product.interpreter).resolve(strict=True)
        interpreter_difference = _external_interpreter_difference(
            interpreter,
            expected_length=identity.interpreter.target_byte_length,
            expected_sha256=identity.interpreter.target_sha256,
        )
        if interpreter_difference is not None:
            raise ValueError(interpreter_difference)
    snapshot_root.chmod(0o500)
    source_context = (
        _absolute_leaf(product.executable).resolve(strict=True)
        if _uses_node_commonjs_source_context(product.executable, product.interpreter)
        else None
    )
    return Product(
        product.name,
        product.executable,
        product.runner_name,
        product.interpreter,
        executable,
        interpreter,
        source_context,
    )


def _snapshot_target_difference(
    path: Path | None,
    *,
    expected_length: int,
    expected_sha256: str,
    label: str,
) -> str | None:
    if path is None:
        return f"owned {label} snapshot is unavailable"
    try:
        length, digest = _digest_regular_file(path)
    except (OSError, ValueError) as error:
        return f"owned {label} snapshot became unavailable: {error}"
    if length != expected_length or digest != expected_sha256:
        return f"owned {label} snapshot bytes changed"
    if not os.access(path, os.X_OK):
        return f"owned {label} snapshot is not executable"
    return None


def _external_interpreter_difference(
    path: Path | None,
    *,
    expected_length: int,
    expected_sha256: str,
) -> str | None:
    if path is None:
        return "external interpreter execution path is unavailable"
    try:
        length, digest = _digest_regular_file(path)
    except (OSError, ValueError) as error:
        return f"external interpreter execution path became unavailable: {error}"
    if length != expected_length or digest != expected_sha256:
        return "external interpreter execution path bytes changed"
    if not os.access(path, os.X_OK):
        return "external interpreter execution path is not executable"
    return None


def snapshot_identity_difference(
    product: Product, expected: CandidateIdentity
) -> str | None:
    difference = _snapshot_target_difference(
        product.execution_executable,
        expected_length=expected.target_byte_length,
        expected_sha256=expected.target_sha256,
        label="executable",
    )
    if difference is not None:
        return difference
    if expected.interpreter is None:
        if product.execution_interpreter is not None:
            return "owned interpreter snapshot is unexpected"
        return None
    return _external_interpreter_difference(
        product.execution_interpreter,
        expected_length=expected.interpreter.target_byte_length,
        expected_sha256=expected.interpreter.target_sha256,
    )


def _executable_identity_difference(
    actual: CandidateIdentity, expected: CandidateIdentity, prefix: str = ""
) -> str | None:
    if (
        actual.target_byte_length != expected.target_byte_length
        or actual.target_sha256 != expected.target_sha256
    ):
        return f"{prefix}resolved target bytes changed"
    if actual.leaf_kind != expected.leaf_kind:
        return f"{prefix}executable leaf kind changed"
    if (
        actual.leaf_byte_length != expected.leaf_byte_length
        or actual.leaf_sha256 != expected.leaf_sha256
    ):
        return f"{prefix}executable leaf bytes changed"
    if actual.leaf_and_target_same_bytes != expected.leaf_and_target_same_bytes:
        return f"{prefix}executable leaf/target relationship changed"
    return None


def candidate_identity_difference(
    product: Product, expected: CandidateIdentity
) -> str | None:
    try:
        actual = capture_candidate_identity(product)
    except (OSError, ValueError) as error:
        return f"candidate identity became unavailable: {error}"
    difference = _executable_identity_difference(actual, expected)
    if difference is not None:
        return difference
    if (actual.interpreter is None) != (expected.interpreter is None):
        return "interpreter presence changed"
    if actual.interpreter is not None and expected.interpreter is not None:
        return _executable_identity_difference(
            actual.interpreter, expected.interpreter, "interpreter "
        )
    return None


def executable_report_record(identity: CandidateIdentity) -> dict[str, Any]:
    leaf_domain = (
        "file-bytes" if identity.leaf_kind == "regular-file" else "symlink-target-bytes"
    )
    return {
        "executableLeaf": {
            "kind": identity.leaf_kind,
            "digestDomain": leaf_domain,
            "byteLength": identity.leaf_byte_length,
            "sha256": identity.leaf_sha256,
        },
        "resolvedTarget": {
            "kind": "regular-file",
            "digestDomain": "file-bytes",
            "byteLength": identity.target_byte_length,
            "sha256": identity.target_sha256,
        },
        "leafAndTargetSameBytes": identity.leaf_and_target_same_bytes,
    }


def interpreter_report_record(identity: CandidateIdentity) -> dict[str, Any]:
    return {
        **executable_report_record(identity),
        "execution": {
            "mode": "resolved-original-path",
            "preAndPostByteCustody": True,
            "ownedSnapshot": False,
            "relocatableClosureCaptured": False,
            "execBoundaryToctouProtection": "not-enforced",
        },
    }


def candidate_report_record(
    product: Product, identity: CandidateIdentity
) -> dict[str, Any]:
    return {
        "label": product.name,
        "expectedRunnerIdentity": product.expected_runner_name,
        **executable_report_record(identity),
        "interpreter": (
            interpreter_report_record(identity.interpreter)
            if identity.interpreter is not None
            else None
        ),
    }


def sanitize_failure_detail(
    detail: str, suite_root: Path, products: list[Product]
) -> str:
    sanitized = detail
    suite_paths = {
        str(suite_root),
        str(suite_root.absolute()),
        str(suite_root.resolve()),
    }
    for suite_path in sorted(suite_paths, key=len, reverse=True):
        sanitized = sanitized.replace(suite_path, "{{SUITE_ROOT}}")
    for product in products:
        replacements: dict[str, str] = {}
        for kind, configured in (
            ("CANDIDATE", product.executable),
            ("INTERPRETER", product.interpreter),
        ):
            if configured is None:
                continue
            leaf = _absolute_leaf(configured)
            replacements[str(leaf)] = f"{{{{{kind}:{product.name}}}}}"
            try:
                replacements[
                    str(leaf.resolve(strict=True))
                ] = f"{{{{{kind}_TARGET:{product.name}}}}}"
            except OSError:
                pass
        for raw, replacement in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            sanitized = sanitized.replace(raw, replacement)
    sanitized = re.sub(
        r"(?i)\b(authorization|api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=<redacted>",
        sanitized,
    )
    sanitized = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <redacted>", sanitized)
    sanitized = " ".join(sanitized.split())
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= MAX_FAILURE_BYTES:
        return sanitized
    suffix = b"...[truncated]"
    shortened = encoded[: MAX_FAILURE_BYTES - len(suffix)]
    while True:
        try:
            return shortened.decode("utf-8") + suffix.decode("ascii")
        except UnicodeDecodeError:
            shortened = shortened[:-1]


def make_report(
    *,
    phase: int,
    case_ids: list[str],
    candidates: list[tuple[Product, CandidateIdentity]],
    candidate_passed: int,
    candidate_failed: int,
    differential_passed: int,
    differential_failed: int,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_total = candidate_passed + candidate_failed
    differential_total = differential_passed + differential_failed
    total = candidate_total + differential_total
    failed = candidate_failed + differential_failed
    return {
        "schema": "openprose.mechanical-conformance-report/1",
        "phase": phase,
        "caseIds": list(case_ids),
        "candidates": [
            candidate_report_record(product, identity)
            for product, identity in candidates
        ],
        "validations": {
            "candidateCases": {
                "total": candidate_total,
                "passed": candidate_passed,
                "failed": candidate_failed,
            },
            "differential": {
                "total": differential_total,
                "passed": differential_passed,
                "failed": differential_failed,
            },
            "total": total,
            "passed": total - failed,
            "failed": failed,
            "status": "pass" if failed == 0 else "fail",
        },
        "failures": list(failures),
        "claims": {
            "scope": "selected-provider-free-mechanical-corpus-only",
            "detachedDescendantContainment": False,
            "semanticConformance": False,
            "releaseAdmission": False,
        },
    }


def _exact_keys(value: Any, expected: set[str], path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path} must be an object"]
    if set(value) != expected:
        return [
            f"{path} has unknown or missing fields: "
            f"expected {sorted(expected)!r}, got {sorted(value)!r}"
        ]
    return []


def _valid_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_executable_report(value: Any, path: str) -> list[str]:
    failures = _exact_keys(
        value,
        {"executableLeaf", "resolvedTarget", "leafAndTargetSameBytes"},
        path,
    )
    if failures:
        return failures
    leaf = value["executableLeaf"]
    leaf_failures = _exact_keys(
        leaf,
        {"kind", "digestDomain", "byteLength", "sha256"},
        f"{path}.executableLeaf",
    )
    failures.extend(leaf_failures)
    target = value["resolvedTarget"]
    target_failures = _exact_keys(
        target,
        {"kind", "digestDomain", "byteLength", "sha256"},
        f"{path}.resolvedTarget",
    )
    failures.extend(target_failures)
    if not leaf_failures:
        leaf_kind = leaf["kind"]
        expected_domain = (
            {
                "regular-file": "file-bytes",
                "symlink": "symlink-target-bytes",
            }.get(leaf_kind)
            if isinstance(leaf_kind, str)
            else None
        )
        if leaf["digestDomain"] != expected_domain:
            failures.append(f"{path}.executableLeaf domain is inconsistent")
        if not _valid_count(leaf["byteLength"]) or leaf["byteLength"] <= 0:
            failures.append(f"{path}.executableLeaf byteLength is invalid")
        if (
            not isinstance(leaf["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", leaf["sha256"]) is None
        ):
            failures.append(f"{path}.executableLeaf sha256 is invalid")
    if not target_failures:
        if target["kind"] != "regular-file" or target["digestDomain"] != "file-bytes":
            failures.append(f"{path}.resolvedTarget kind/domain is inconsistent")
        if not _valid_count(target["byteLength"]) or target["byteLength"] <= 0:
            failures.append(f"{path}.resolvedTarget byteLength is invalid")
        if (
            not isinstance(target["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", target["sha256"]) is None
        ):
            failures.append(f"{path}.resolvedTarget sha256 is invalid")
    same = value["leafAndTargetSameBytes"]
    if not isinstance(same, bool):
        failures.append(f"{path}.leafAndTargetSameBytes must be boolean")
    elif not leaf_failures and not target_failures:
        exact_same = (
            leaf["kind"] == "regular-file"
            and leaf["byteLength"] == target["byteLength"]
            and leaf["sha256"] == target["sha256"]
        )
        if same is not exact_same:
            failures.append(f"{path}.leafAndTargetSameBytes is inconsistent")
    return failures


def _validate_interpreter_report(value: Any, path: str) -> list[str]:
    failures = _exact_keys(
        value,
        {
            "executableLeaf",
            "resolvedTarget",
            "leafAndTargetSameBytes",
            "execution",
        },
        path,
    )
    if failures:
        return failures
    failures.extend(
        _validate_executable_report(
            {
                "executableLeaf": value["executableLeaf"],
                "resolvedTarget": value["resolvedTarget"],
                "leafAndTargetSameBytes": value["leafAndTargetSameBytes"],
            },
            path,
        )
    )
    execution = value["execution"]
    execution_failures = _exact_keys(
        execution,
        {
            "mode",
            "preAndPostByteCustody",
            "ownedSnapshot",
            "relocatableClosureCaptured",
            "execBoundaryToctouProtection",
        },
        f"{path}.execution",
    )
    failures.extend(execution_failures)
    if not execution_failures and execution != {
        "mode": "resolved-original-path",
        "preAndPostByteCustody": True,
        "ownedSnapshot": False,
        "relocatableClosureCaptured": False,
        "execBoundaryToctouProtection": "not-enforced",
    }:
        failures.append(f"{path}.execution overstates interpreter custody")
    return failures


def validate_report(report: Any) -> list[str]:
    failures = _exact_keys(
        report,
        {
            "schema",
            "phase",
            "caseIds",
            "candidates",
            "validations",
            "failures",
            "claims",
        },
        "$",
    )
    if failures:
        return failures
    if report["schema"] != "openprose.mechanical-conformance-report/1":
        failures.append("$.schema is unsupported")
    phase = report["phase"]
    if not isinstance(phase, int) or isinstance(phase, bool) or not 0 <= phase <= 7:
        failures.append("$.phase must be an integer from 0 through 7")
    case_ids = report["caseIds"]
    if (
        not isinstance(case_ids, list)
        or not case_ids
        or any(not isinstance(item, str) or not item for item in case_ids)
        or len(set(case_ids)) != len(case_ids)
        or case_ids != sorted(case_ids)
    ):
        failures.append("$.caseIds must be a nonempty sorted unique string array")
    valid_case_ids = (
        set(case_ids)
        if isinstance(case_ids, list)
        and all(isinstance(item, str) for item in case_ids)
        else set()
    )

    candidates = report["candidates"]
    labels: list[str] = []
    if not isinstance(candidates, list) or not candidates:
        failures.append("$.candidates must be a nonempty array")
        candidates = []
    for index, candidate in enumerate(candidates):
        path = f"$.candidates[{index}]"
        candidate_failures = _exact_keys(
            candidate,
            {
                "label",
                "expectedRunnerIdentity",
                "executableLeaf",
                "resolvedTarget",
                "leafAndTargetSameBytes",
                "interpreter",
            },
            path,
        )
        failures.extend(candidate_failures)
        if candidate_failures:
            continue
        label = candidate["label"]
        if not isinstance(label, str) or CANDIDATE_LABEL.fullmatch(label) is None:
            failures.append(f"{path}.label is invalid")
        else:
            labels.append(label)
        if not isinstance(candidate["expectedRunnerIdentity"], str) or candidate[
            "expectedRunnerIdentity"
        ] not in {"rust", "bun"}:
            failures.append(f"{path}.expectedRunnerIdentity is unsupported")
        failures.extend(
            _validate_executable_report(
                {
                    "executableLeaf": candidate["executableLeaf"],
                    "resolvedTarget": candidate["resolvedTarget"],
                    "leafAndTargetSameBytes": candidate["leafAndTargetSameBytes"],
                },
                path,
            )
        )
        interpreter = candidate["interpreter"]
        if interpreter is not None:
            failures.extend(
                _validate_interpreter_report(interpreter, f"{path}.interpreter")
            )
    if len(set(labels)) != len(labels):
        failures.append("$.candidates contains duplicate labels")

    validations = report["validations"]
    validation_failures = _exact_keys(
        validations,
        {
            "candidateCases",
            "differential",
            "total",
            "passed",
            "failed",
            "status",
        },
        "$.validations",
    )
    failures.extend(validation_failures)
    if not validation_failures:
        groups_valid = True
        for key in ("candidateCases", "differential"):
            group = validations[key]
            group_failures = _exact_keys(
                group, {"total", "passed", "failed"}, f"$.validations.{key}"
            )
            failures.extend(group_failures)
            if not group_failures:
                if any(not _valid_count(group[name]) for name in group):
                    failures.append(f"$.validations.{key} counts are invalid")
                    groups_valid = False
                elif group["passed"] + group["failed"] != group["total"]:
                    failures.append(f"$.validations.{key} counts are inconsistent")
                    groups_valid = False
            else:
                groups_valid = False
        scalar_names = ("total", "passed", "failed")
        scalars_valid = not any(
            not _valid_count(validations[name]) for name in scalar_names
        )
        if not scalars_valid:
            failures.append("$.validations aggregate counts are invalid")
        elif groups_valid and (
            validations["passed"] + validations["failed"] != validations["total"]
            or validations["total"]
            != validations["candidateCases"]["total"]
            + validations["differential"]["total"]
            or validations["passed"]
            != validations["candidateCases"]["passed"]
            + validations["differential"]["passed"]
            or validations["failed"]
            != validations["candidateCases"]["failed"]
            + validations["differential"]["failed"]
        ):
            failures.append("$.validations aggregate counts are inconsistent")
        if groups_valid and isinstance(case_ids, list):
            expected_candidate_total = len(case_ids) * len(candidates)
            expected_differential_total = len(case_ids) * max(0, len(candidates) - 1)
            if validations["candidateCases"]["total"] != expected_candidate_total:
                failures.append(
                    "$.validations.candidateCases total does not bind the corpus"
                )
            if validations["differential"]["total"] != expected_differential_total:
                failures.append(
                    "$.validations.differential total does not bind the corpus"
                )
        if scalars_valid:
            expected_status = "pass" if validations["failed"] == 0 else "fail"
            if validations["status"] != expected_status:
                failures.append("$.validations.status is inconsistent")

    failure_records = report["failures"]
    if not isinstance(failure_records, list):
        failures.append("$.failures must be an array")
        failure_records = []
    allowed_codes = {
        "CANDIDATE_BYTES_CHANGED",
        "OUTPUT_VALIDATION_FAILED",
        "DIFFERENTIAL_UNAVAILABLE",
        "DIFFERENTIAL_DRIFT",
    }
    for index, record in enumerate(failure_records):
        path = f"$.failures[{index}]"
        record_failures = _exact_keys(
            record, {"caseId", "candidateLabels", "code", "detail"}, path
        )
        failures.extend(record_failures)
        if record_failures:
            continue
        if (
            not isinstance(record["caseId"], str)
            or record["caseId"] not in valid_case_ids
        ):
            failures.append(f"{path}.caseId is outside the selected corpus")
        record_labels = record["candidateLabels"]
        if (
            not isinstance(record_labels, list)
            or not record_labels
            or any(label not in labels for label in record_labels)
        ):
            failures.append(f"{path}.candidateLabels are invalid")
        if not isinstance(record["code"], str) or record["code"] not in allowed_codes:
            failures.append(f"{path}.code is unsupported")
        detail = record["detail"]
        if (
            not isinstance(detail, str)
            or not detail
            or len(detail.encode("utf-8")) > MAX_FAILURE_BYTES
        ):
            failures.append(f"{path}.detail is invalid")
    if not validation_failures:
        if bool(failure_records) is not (validations["failed"] > 0):
            failures.append(
                "$.failures presence is inconsistent with failed validations"
            )
        if len(failure_records) != validations["failed"]:
            failures.append(
                "$.failures must contain exactly one bounded record per failed validation"
            )

    claims = report["claims"]
    claim_failures = _exact_keys(
        claims,
        {
            "scope",
            "detachedDescendantContainment",
            "semanticConformance",
            "releaseAdmission",
        },
        "$.claims",
    )
    failures.extend(claim_failures)
    if not claim_failures and claims != {
        "scope": "selected-provider-free-mechanical-corpus-only",
        "detachedDescendantContainment": False,
        "semanticConformance": False,
        "releaseAdmission": False,
    }:
        failures.append("$.claims overstates or changes the report scope")
    return failures


def render_report(report: dict[str, Any]) -> bytes:
    failures = validate_report(report)
    if failures:
        raise ValueError(
            "invalid mechanical conformance report: " + "; ".join(failures)
        )
    return canonical_json(report) + b"\n"


def write_report(path: Path, report: dict[str, Any]) -> None:
    encoded = render_report(report)
    with path.open("xb") as destination:
        destination.write(encoded)


def product_roots(case_root: Path, product_name: str) -> tuple[Path, Path]:
    product_root = case_root / "products" / product_name
    return product_root / "environment", product_root / "workspace"


def pid_exists(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def timeout_cleanup_has_descendant_authority(platform_name: str) -> bool:
    # A POSIX process group does not contain descendants that call setsid(2),
    # and CREATE_NEW_PROCESS_GROUP is not a Windows Job Object. Neither is
    # authority over every possible descendant.
    return False


def _wait_until(predicate: Any, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _terminate_owned_boundary(process: subprocess.Popen[bytes]) -> bool:
    if os.name == "posix":
        process_group_id = process.pid
        authority_error = False
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            authority_error = True
        deadline = time.monotonic() + 0.25
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not authority_error and _process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                authority_error = True
        if authority_error and process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            return False
        group_gone = _wait_until(
            lambda: not _process_group_exists(process_group_id), 2.0
        )
        return not authority_error and group_gone and process.poll() is not None

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                return False
    # CREATE_NEW_PROCESS_GROUP is not Job Object authority. Even if the direct
    # child exited, a Windows timeout cannot claim descendant settlement.
    return False


class _BoundedPipeReader:
    def __init__(self, pipe: Any) -> None:
        self.pipe = pipe
        self.retained = bytearray()
        self.truncated = False
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _drain(self) -> None:
        retained_limit = MAX_CAPTURE_BYTES - len(CAPTURE_TRUNCATION_MARKER)
        try:
            while True:
                chunk = self.pipe.read(64 * 1024)
                if not chunk:
                    break
                available = retained_limit - len(self.retained)
                if available > 0:
                    self.retained.extend(chunk[:available])
                if len(chunk) > max(0, available):
                    self.truncated = True
        except (OSError, ValueError) as error:
            self.error = error
        finally:
            try:
                self.pipe.close()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        try:
            os.close(self.pipe.fileno())
        except (OSError, ValueError):
            pass

    def value(self) -> bytes:
        encoded = bytes(self.retained)
        if self.truncated:
            encoded += CAPTURE_TRUNCATION_MARKER
        return encoded


def _finish_pipe_readers(
    readers: tuple[_BoundedPipeReader, _BoundedPipeReader],
    timeout_seconds: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    for reader in readers:
        reader.thread.join(max(0.0, deadline - time.monotonic()))
    alive = [reader for reader in readers if reader.thread.is_alive()]
    for reader in alive:
        reader.close()
    for reader in alive:
        reader.thread.join(0.25)
    return all(
        not reader.thread.is_alive() and reader.error is None for reader in readers
    )


def _append_bounded(stream: bytes, suffix: bytes) -> bytes:
    if len(suffix) >= MAX_CAPTURE_BYTES:
        return suffix[-MAX_CAPTURE_BYTES:]
    return stream[: MAX_CAPTURE_BYTES - len(suffix)] + suffix


def run_owned_process(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
) -> OwnedProcessResult:
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            start_new_session=os.name == "posix",
            creationflags=creation_flags,
        )
    except OSError as error:
        return OwnedProcessResult(
            126,
            b"",
            f"conformance runner spawn failed: {error}\n".encode(
                "utf-8", errors="replace"
            ),
            False,
            True,
        )
    assert process.stdout is not None and process.stderr is not None
    stdout_reader = _BoundedPipeReader(process.stdout)
    stderr_reader = _BoundedPipeReader(process.stderr)
    readers = (stdout_reader, stderr_reader)
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        boundary_settled = _terminate_owned_boundary(process)
        readers_settled = _finish_pipe_readers(readers)
        return OwnedProcessResult(
            124,
            stdout_reader.value(),
            _append_bounded(stderr_reader.value(), b"conformance runner timeout\n"),
            True,
            boundary_settled and readers_settled,
            stdout_reader.truncated,
            stderr_reader.truncated,
        )
    except BaseException:
        _terminate_owned_boundary(process)
        _finish_pipe_readers(readers)
        raise

    leaked_owned_group = os.name == "posix" and _process_group_exists(process.pid)
    if leaked_owned_group:
        _terminate_owned_boundary(process)
        _finish_pipe_readers(readers)
        return OwnedProcessResult(
            124,
            stdout_reader.value(),
            _append_bounded(
                stderr_reader.value(),
                b"conformance runner detected an unsettled process group\n",
            ),
            False,
            False,
            stdout_reader.truncated,
            stderr_reader.truncated,
        )
    readers_settled = _finish_pipe_readers(readers)
    return OwnedProcessResult(
        process.returncode,
        stdout_reader.value(),
        stderr_reader.value(),
        False,
        process.poll() is not None and readers_settled,
        stdout_reader.truncated,
        stderr_reader.truncated,
    )


class ContractRegistry:
    def __init__(self) -> None:
        self.schemas = {
            path.name: json.loads(path.read_text("utf-8"))
            for path in sorted(SCHEMAS.glob("*.schema.json"))
        }
        self.by_contract: dict[str, str] = {}
        for name, schema in self.schemas.items():
            contract = schema.get("properties", {}).get("schema", {}).get("const")
            # Helper schemas have no output-envelope discriminator.
            if contract is None:
                continue
            if not isinstance(contract, str) or not contract:
                raise ValueError(f"Invalid contract discriminator in {name}")
            if contract in self.by_contract:
                raise ValueError(f"Duplicate contract discriminator: {contract}")
            self.by_contract[contract] = name
        registry = Registry()
        for schema in self.schemas.values():
            registry = registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )
        self.registry = registry

    def errors(self, contract: str, instance: Any) -> list[str]:
        schema_name = self.by_contract.get(contract)
        if schema_name is None:
            return [f"Unknown output contract: {contract}"]
        validator = jsonschema.Draft202012Validator(
            self.schemas[schema_name],
            registry=self.registry,
            format_checker=jsonschema.FormatChecker(),
        )
        return [
            f"{list(error.path)}: {error.message}"
            for error in sorted(
                validator.iter_errors(instance), key=lambda item: list(item.path)
            )
        ]


def build_products() -> None:
    clean = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("PROSE_", "OPENPROSE_"))
        and not name.upper().endswith(
            ("_API_KEY", "_PASSWORD", "_SECRET", "_TOKEN", "_CREDENTIALS")
        )
    }
    clean["OPENPROSE_BUILD_COMMIT"] = "development"
    generator = CLI / "shared/image/bundle/image_bundle.py"
    with tempfile.TemporaryDirectory(prefix="openprose-conformance-image-") as raw:
        root = Path(raw)
        bundle = root / "sentinel.bundle.bin"
        checksum = root / "sentinel.bundle.sha256"
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "build",
                str(SENTINEL),
                str(bundle),
                "--checksum",
                str(checksum),
            ],
            cwd=ROOT,
            env=clean,
            stdin=subprocess.DEVNULL,
            check=True,
        )
        build_environment = {
            **clean,
            "OPENPROSE_IMAGE_SOURCE_DIR": str(SENTINEL),
            "OPENPROSE_IMAGE_BUNDLE": str(bundle),
            "OPENPROSE_IMAGE_BUNDLE_CHECKSUM": str(checksum),
        }
        subprocess.run(
            [
                "cargo",
                "build",
                "--workspace",
                "--locked",
                "--offline",
                "--features",
                "prose-cli/test-seams",
            ],
            cwd=CLI / "rust",
            env=build_environment,
            stdin=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            [
                "bun",
                "--no-env-file",
                f"--config={CLI / 'bun/config/empty-bunfig.toml'}",
                "run",
                str(CLI / "bun/scripts/image-bundle.ts"),
                "build",
                "--test-seams",
                "--image-dir",
                str(SENTINEL),
                "--bundle",
                str(bundle),
                "--checksum",
                str(checksum),
            ],
            cwd=ROOT,
            env=build_environment,
            stdin=subprocess.DEVNULL,
            check=True,
        )


def case_paths(phase: int, selected: set[str]) -> Iterable[Path]:
    for path in sorted(CASES.glob("**/*.json")):
        if path.name.endswith("schema.json"):
            continue
        case = json.loads(path.read_text("utf-8"))
        if case.get("schema") != "openprose.runner-case/1":
            continue
        if case["phase"] <= phase and (not selected or case["id"] in selected):
            yield path


CANDIDATE_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def parse_candidate_specs(specs: list[list[str]]) -> list[Product]:
    products: list[Product] = []
    labels: set[str] = set()
    for spec in specs:
        if len(spec) != 3:
            raise ValueError("candidate requires LABEL RUNNER PATH")
        label, runner_name, raw_path = spec
        if CANDIDATE_LABEL.fullmatch(label) is None:
            raise ValueError(f"invalid candidate label: {label!r}")
        if label in labels:
            raise ValueError(f"duplicate candidate label: {label}")
        if runner_name not in {"rust", "bun"}:
            raise ValueError(
                f"candidate {label} has unsupported runner identity {runner_name!r}"
            )
        labels.add(label)
        products.append(Product(label, Path(raw_path), runner_name))
    return products


def attach_candidate_interpreters(
    products: list[Product], specs: list[list[str]]
) -> list[Product]:
    interpreters: dict[str, Path] = {}
    known = {product.name for product in products}
    for spec in specs:
        if len(spec) != 2:
            raise ValueError("candidate interpreter requires LABEL PATH")
        label, raw_path = spec
        if label not in known:
            raise ValueError(f"candidate interpreter has unknown label: {label}")
        if label in interpreters:
            raise ValueError(f"duplicate candidate interpreter label: {label}")
        interpreters[label] = Path(raw_path)
    return [
        Product(
            product.name,
            product.executable,
            product.runner_name,
            interpreters.get(product.name),
        )
        for product in products
    ]


def hermetic_environment(root: Path, additions: dict[str, str]) -> dict[str, str]:
    empty_path = root / "empty-path"
    home = root / "home"
    config = root / "config"
    cache = root / "cache"
    temporary = root / "tmp"
    for path in (empty_path, home, config, cache, temporary):
        path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        python_link = empty_path / "python3"
        if not python_link.exists():
            python_link.symlink_to(Path(sys.executable).resolve())
    environment = {
        "PATH": str(empty_path),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "LANG": "C",
        "LC_ALL": "C",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }
    environment.update(additions)
    return environment


def execute(
    product: Product,
    case: dict[str, Any],
    environment_root: Path,
    workspace: Path,
) -> Observation:
    canonical_workspace = workspace.resolve()
    cwd_text = case["invocation"]["cwd"].replace(
        "{{WORKSPACE}}", str(canonical_workspace)
    )
    cwd = Path(cwd_text)
    cwd.mkdir(parents=True, exist_ok=True)
    additions = {
        key: value.replace("{{WORKSPACE}}", str(canonical_workspace))
        for key, value in case["invocation"]["environment"].items()
    }
    fake = case["controls"].get("fakeHarness")
    installed_adapter = case["controls"].get("installedAdapter")
    if fake is not None and installed_adapter is not None:
        raise ValueError("a case cannot select both fakeHarness and installedAdapter")
    observation_path: Path | None = None
    if fake is not None:
        observation_path = environment_root / "fake-observation.json"
        descendant_path = environment_root / "descendants.json"
        additions.update(
            {
                "OPENPROSE_CONFORMANCE_FAKE_HARNESS": str(FAKE_HARNESS),
                "OPENPROSE_CONFORMANCE_FAKE_SCENARIO": fake["scenario"],
                "OPENPROSE_CONFORMANCE_FAKE_OBSERVATION": str(observation_path),
                "OPENPROSE_CONFORMANCE_DESCENDANT_IDENTITIES": str(descendant_path),
            }
        )
        if "delayMs" in fake:
            additions["OPENPROSE_CONFORMANCE_FAKE_DELAY_MS"] = str(fake["delayMs"])
        if "cancelAfterMs" in fake:
            additions["OPENPROSE_CONFORMANCE_CANCEL_AFTER_MS"] = str(
                fake["cancelAfterMs"]
            )
    if installed_adapter is not None:
        adapter_id = installed_adapter["adapterId"]
        executable_name = INSTALLED_ADAPTER_EXECUTABLES[adapter_id]
        harness_bin = environment_root / "installed-adapter-bin"
        harness_bin.mkdir(parents=True, exist_ok=False)
        executable = harness_bin / executable_name
        if installed_adapter.get("versionProbeScenario") == "wrong-stream-success":
            executable.write_bytes(WRONG_STREAM_VERSION_HARNESS)
        else:
            shutil.copyfile(INSTALLED_ADAPTER_HARNESS, executable)
        executable.chmod(0o700)
        python_link = harness_bin / "python3"
        python_link.symlink_to(Path(sys.executable).resolve())
        if adapter_id == "omp/rpc":
            bun_runtime = harness_bin / "bun"
            bun_runtime.write_bytes(BUN_RUNTIME_FIXTURE)
            bun_runtime.chmod(0o700)
        additions["PATH"] = str(harness_bin)
        if adapter_id in {"prime/rpc", "omp/rpc"}:
            additions["OPENROUTER_API_KEY"] = "fixture-provider-free-openrouter-key"
    environment = hermetic_environment(environment_root, additions)
    for name in case["controls"].get("omitEnvironment", []):
        environment.pop(name, None)
    completed = run_owned_process(
        product.execution_argv(case["invocation"]["argv"]),
        cwd=cwd,
        environment=environment,
        timeout_seconds=15,
    )
    return Observation(
        product,
        case,
        completed.exit_code,
        completed.stdout,
        completed.stderr,
        fake_observation=observation_path,
        workspace=canonical_workspace,
        process_settled=completed.settled,
        stdout_truncated=completed.stdout_truncated,
        stderr_truncated=completed.stderr_truncated,
    )


def deep_subset(actual: Any, expected: Any, path: str = "$") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object subset, got {type(actual).__name__}"]
        failures: list[str] = []
        for key, value in expected.items():
            if key not in actual:
                failures.append(f"{path}.{key}: missing")
            else:
                failures.extend(deep_subset(actual[key], value, f"{path}.{key}"))
        return failures
    if isinstance(expected, list):
        if actual != expected:
            return [f"{path}: expected {expected!r}, got {actual!r}"]
        return []
    if actual != expected:
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return []


def first_exact_difference(actual: Any, expected: Any, path: str = "$") -> str | None:
    if type(actual) is not type(expected):
        return (
            f"{path}: expected {type(expected).__name__}, "
            f"got {type(actual).__name__}"
        )
    if isinstance(expected, dict):
        actual_keys = set(actual)
        expected_keys = set(expected)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            return f"{path}: missing keys {missing!r}; extra keys {extra!r}"
        for key in expected:
            difference = first_exact_difference(
                actual[key], expected[key], f"{path}.{key}"
            )
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(actual) != len(expected):
            return f"{path}: expected {len(expected)} items, got {len(actual)}"
        for index, expected_item in enumerate(expected):
            difference = first_exact_difference(
                actual[index], expected_item, f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return None
    if actual != expected:
        return f"{path}: expected {expected!r}, got {actual!r}"
    return None


def load_exact_result_fixture(relative: str) -> Any:
    base = (CLI / "shared/fixtures/dx").resolve()
    path = (ROOT / relative).resolve()
    try:
        path.relative_to(base)
    except ValueError as error:
        raise ValueError("exact JSON fixture leaves shared DX fixture root") from error
    if not path.is_file():
        raise ValueError(f"exact JSON fixture is not a regular file: {relative}")
    return json.loads(path.read_text("utf-8"))


def observed_harness_started(observation: Observation) -> bool:
    if (
        observation.fake_observation is not None
        and observation.fake_observation.is_file()
    ):
        return True
    parsed = observation.parsed
    if isinstance(parsed, list):
        return any(record.get("type") == "harness.started" for record in parsed)
    if isinstance(parsed, dict) and parsed.get("schema") == "openprose.runner-result/1":
        terminal = parsed.get("terminal")
        return isinstance(terminal, dict) and terminal.get("transportCompleted") is True
    return False


def expected_for_host(case: dict[str, Any], host_os: str | None = None,
                      host_arch: str | None = None) -> dict[str, Any]:
    """Apply the independently frozen host oracle; never infer success from products."""
    expected = deepcopy(case["expected"])
    oracle = json.loads((CLI / "conformance/fixtures/adapter-host-expectations.json").read_text("utf-8"))
    adapter = oracle["cases"].get(case.get("id"))
    if adapter is None:
        return expected
    os_name = host_os or sys.platform
    arch = host_arch or platform.machine()
    os_name = {"macos": "darwin", "windows": "win32"}.get(os_name, os_name)
    arch = {"aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(arch, arch)
    if f"{os_name}-{arch}" in oracle["admittedHosts"][adapter]:
        return expected
    error = {
        "schema": "openprose.runner-error/1", "code": "HARNESS_INCOMPATIBLE",
        "boundary": "adapter", "message": "The selected harness version is incompatible with this adapter.",
        "action": "Run the exact Repair command reported with this error, then retry.",
        "exitCode": 10, "retryable": False,
        "details": {"adapterId": adapter, "hostPlatform": os_name,
                    "hostArchitecture": arch, "supportedPlatforms": oracle["supportedPlatforms"][adapter],
                    "fallbackAttempted": False},
    }
    expected.update(exitCode=10, startedHarness=False)
    expected.pop("forwardedTask", None)
    if case["id"] == "dx.dry-run-prime":
        dry_run = load_exact_result_fixture(expected.pop("resultFixture"))
        dry_run["selection"]["runtimeVersion"] = None
        dry_run.update(readiness="blocked", blockingError=error)
        expected["resultMatches"] = dry_run
    elif "version-probe" in case["id"]:
        expected["resultMatches"]["problems"] = [error]
    else:
        expected["resultMatches"] = {
            "adapter": {"id": adapter}, "runnerExitCode": 10,
            "terminal": {"classification": "runner-error", "transportCompleted": False,
                         "terminalEventObserved": False, "exitCode": None, "signal": None},
            "semantic": {"status": "unknown"}, "error": error,
        }
    return expected


def validate_output(observation: Observation, contracts: ContractRegistry) -> list[str]:
    expected = expected_for_host(observation.case)
    failures: list[str] = []
    if not observation.process_settled:
        failures.append(
            "direct process, original process group, or output readers did not settle"
        )
    if observation.stdout_truncated:
        failures.append(f"stdout exceeded the {MAX_CAPTURE_BYTES}-byte capture limit")
    if observation.stderr_truncated:
        failures.append(f"stderr exceeded the {MAX_CAPTURE_BYTES}-byte capture limit")
    if observation.exit_code != expected["exitCode"]:
        failures.append(
            f"exit: expected {expected['exitCode']}, got {observation.exit_code}"
        )
    for stream_name in ("stdout", "stderr"):
        stream = getattr(observation, stream_name)
        rule = expected[stream_name]
        kind = rule["kind"]
        if kind == "empty" and stream:
            failures.append(f"{stream_name}: expected empty, got {stream!r}")
        elif kind == "exact-fixture":
            wanted = (ROOT / rule["fixture"]).read_bytes()
            if stream != wanted:
                failures.append(
                    f"{stream_name}: differs from {rule['fixture']} "
                    f"(wanted {sha256(wanted)}, got {sha256(stream)})"
                )
        elif kind == "contains":
            text = stream.decode("utf-8", errors="replace")
            for needle in rule["contains"]:
                if needle not in text:
                    failures.append(f"{stream_name}: missing {needle!r}")
        elif kind == "json":
            try:
                observation.parsed = json.loads(stream)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                failures.append(f"stdout: invalid single JSON result: {error}")
                continue
            failures.extend(contracts.errors(rule["schema"], observation.parsed))
        elif kind == "jsonl":
            try:
                records = [json.loads(line) for line in stream.splitlines()]
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                failures.append(f"stdout: invalid JSONL: {error}")
                continue
            observation.parsed = records
            for record in records:
                failures.extend(
                    contracts.errors("openprose.normalized-event/1", record)
                )
            actual_types = [record.get("type") for record in records]
            if actual_types != rule["eventTypes"]:
                failures.append(
                    f"stdout event types: expected {rule['eventTypes']!r}, got {actual_types!r}"
                )
            if not records or records[-1].get("type") != rule["terminalType"]:
                failures.append(f"stdout: terminal event is not {rule['terminalType']}")
            if any(
                record.get("type") in {"runner.completed", "runner.failed"}
                for record in records[:-1]
            ):
                failures.append("stdout: terminal event was not exactly once and last")

    if "startedHarness" in expected:
        observed_started = observed_harness_started(observation)
        if observed_started is not expected["startedHarness"]:
            failures.append(
                "harness start: expected "
                f"{expected['startedHarness']}, observed {observed_started}"
            )

    result = observation.parsed
    if isinstance(result, list):
        payload = result[-1].get("payload") if result else None
        if not isinstance(payload, dict):
            result = None
        elif result[-1].get("type") == "runner.completed":
            result = payload.get("result")
        elif result[-1].get("type") == "runner.failed":
            result = {"error": payload.get("error")}
        else:
            result = None
    if isinstance(result, dict):
        schema = result.get("schema")
        if schema in {"openprose.runner-result/1", "openprose.doctor-report/1"}:
            runner_record = result.get("runner")
            observed_runner = (
                runner_record.get("name") if isinstance(runner_record, dict) else None
            )
            if observed_runner != observation.product.expected_runner_name:
                failures.append(
                    "runner identity: expected "
                    f"{observation.product.expected_runner_name!r}, got {observed_runner!r}"
                )
        if (
            result.get("schema") == "openprose.runner-result/1"
            and observation.workspace is not None
            and isinstance(result.get("cwd"), dict)
        ):
            expected_workspace = str(observation.workspace)
            actual_path = result["cwd"].get("path")
            actual_identity = result["cwd"].get("identitySha256")
            if actual_path != expected_workspace:
                failures.append(
                    f"cwd path: expected product workspace {expected_workspace!r}, "
                    f"got {actual_path!r}"
                )
            expected_identity = sha256(expected_workspace.encode("utf-8"))
            if actual_identity != expected_identity:
                failures.append(
                    f"cwd identity: expected {expected_identity}, got {actual_identity}"
                )
        if "resultMatches" in expected:
            subset_actual = result
            if observation.workspace is not None:
                subset_actual = _normalize_workspace_strings(
                    result, str(observation.workspace.resolve())
                )
            failures.extend(deep_subset(subset_actual, expected["resultMatches"]))
        if "resultFixture" in expected:
            try:
                wanted = load_exact_result_fixture(expected["resultFixture"])
            except (OSError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"exact JSON fixture unavailable: {error}")
            else:
                actual = result
                if observation.workspace is not None:
                    actual = _normalize_workspace_strings(
                        actual, str(observation.workspace.resolve())
                    )
                difference = first_exact_difference(actual, wanted)
                if difference is not None:
                    failures.append(
                        f"exact JSON fixture {expected['resultFixture']}: {difference}"
                    )
        if "errorCode" in expected:
            error_record = result.get("error")
            if (
                error_record is None
                and result.get("schema") == "openprose.runner-dry-run-report/1"
            ):
                error_record = result.get("blockingError")
            failures.extend(
                deep_subset(
                    error_record,
                    {
                        "code": expected["errorCode"],
                        "action": expected["errorAction"],
                    },
                    "$.error",
                )
            )
        if "forwardedTask" in expected:
            task = {
                "schema": "openprose.task-envelope/1",
                **expected["forwardedTask"],
            }
            wanted_digest = sha256(canonical_json(task))
            got_digest = result.get("digests", {}).get("taskSha256")
            if got_digest != wanted_digest:
                failures.append(
                    f"forwarded task digest: expected {wanted_digest}, got {got_digest}"
                )
    fake_expectation = expected.get("fakeObservation")
    if fake_expectation is not None:
        if (
            observation.fake_observation is None
            or not observation.fake_observation.is_file()
        ):
            failures.append("fake harness did not publish an observation")
        else:
            fake_observation = json.loads(
                observation.fake_observation.read_text("utf-8")
            )
            if observation.workspace is not None:
                observed_cwd = str(Path(fake_observation["cwd"]).resolve())
                if observed_cwd != str(observation.workspace):
                    failures.append(
                        "fake harness cwd did not resolve to its product workspace"
                    )
            if fake_expectation.get("exactImageBytes"):
                manifest = json.loads((SENTINEL / "manifest.json").read_text("utf-8"))
                expected_image = b"".join(
                    (SENTINEL / entry["path"]).read_bytes()
                    for entry in manifest["payload"]
                )
                if fake_observation["image"]["sha256"] != sha256(expected_image):
                    failures.append("fake harness observed different image bytes")
            if fake_expectation.get("exactTaskBytes"):
                forwarded = expected.get("forwardedTask")
                if forwarded is not None:
                    wanted_task = canonical_json(
                        {
                            "schema": "openprose.task-envelope/1",
                            **forwarded,
                        }
                    )
                    if fake_observation["task"]["sha256"] != sha256(wanted_task):
                        failures.append("fake harness observed different task bytes")
    for effect in expected.get("fileEffects", []):
        if observation.workspace is None:
            failures.append("file effect cannot be checked without a product workspace")
            continue
        rendered = effect["path"].replace(
            "{{WORKSPACE}}", str(observation.workspace.resolve())
        )
        path = Path(rendered).resolve(strict=False)
        try:
            path.relative_to(observation.workspace.resolve())
        except ValueError:
            failures.append(f"file effect leaves product workspace: {rendered}")
            continue
        if not path.is_file():
            failures.append(f"expected file effect is missing: {rendered}")
            continue
        try:
            actual = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            failures.append(f"cannot read expected file effect {rendered}: {error}")
            continue
        if actual != effect["utf8"]:
            failures.append(
                f"file effect {rendered} differs: "
                f"wanted {sha256(effect['utf8'].encode('utf-8'))}, "
                f"got {sha256(actual.encode('utf-8'))}"
            )
    return failures


def normalized_for_difference(value: Any, workspace: Path | None = None) -> Any:
    """Remove only runner identity, volatile clocks/IDs, and their derived hashes."""
    if isinstance(value, list):
        return [normalized_for_difference(item, workspace) for item in value]
    result = deepcopy(value)
    if not isinstance(result, dict):
        return result
    if result.get("schema") == "openprose.normalized-event/1":
        payload = result.get("payload")
        if isinstance(payload, dict):
            if result.get("type") == "runner.started":
                payload.pop("runnerName", None)
            nested_result = payload.get("result")
            if isinstance(nested_result, dict):
                payload["result"] = normalized_for_difference(nested_result, workspace)
        if workspace is not None:
            result = _normalize_workspace_strings(result, str(workspace.resolve()))
        result.pop("invocationId", None)
        result.pop("timestamp", None)
        return result
    workspace_text = str(workspace.resolve()) if workspace is not None else None
    cwd = result.get("cwd")
    cwd_is_verified = (
        workspace_text is not None
        and isinstance(cwd, dict)
        and cwd.get("path") == workspace_text
        and cwd.get("identitySha256") == sha256(workspace_text.encode("utf-8"))
    )
    if workspace_text is not None:
        result = _normalize_workspace_strings(result, workspace_text)
        cwd = result.get("cwd")
        if cwd_is_verified and isinstance(cwd, dict):
            cwd["identitySha256"] = "{{WORKSPACE_IDENTITY_SHA256}}"
    result.pop("invocationId", None)
    runner = result.get("runner")
    if isinstance(runner, dict):
        runner.pop("name", None)
    timing = result.get("timing")
    if isinstance(timing, dict):
        for key in (
            "startedAt",
            "firstEventAt",
            "cancellationAt",
            "terminalAt",
            "durationMs",
        ):
            timing.pop(key, None)
    digests = result.get("digests")
    if isinstance(digests, dict):
        digests.pop("invocationSha256", None)
        digests.pop("normalizedEventsSha256", None)
    return result


def _normalize_workspace_strings(value: Any, workspace: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_workspace_strings(item, workspace)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_workspace_strings(item, workspace) for item in value]
    if isinstance(value, str) and (
        value == workspace or value.startswith(workspace + os.sep)
    ):
        return "{{WORKSPACE}}" + value[len(workspace) :]
    return value


def compare_pair(observations: list[Observation]) -> list[str]:
    if len(observations) != 2:
        return []
    left, right = observations
    if left.exit_code != right.exit_code:
        return [f"differential exit: {left.exit_code} != {right.exit_code}"]
    if isinstance(left.parsed, (dict, list)) and isinstance(
        right.parsed, type(left.parsed)
    ):
        a = normalized_for_difference(left.parsed, left.workspace)
        b = normalized_for_difference(right.parsed, right.workspace)
        if a != b:
            difference = first_exact_difference(b, a) or "unknown difference"
            return [
                "differential JSON drift after declared normalization: "
                f"{difference}; {left.product.name}={sha256(canonical_json(a))}, "
                f"{right.product.name}={sha256(canonical_json(b))}"
            ]
    elif left.stdout != right.stdout or left.stderr != right.stderr:
        return [
            "differential byte drift: "
            f"stdout {sha256(left.stdout)} != {sha256(right.stdout)}, "
            f"stderr {sha256(left.stderr)} != {sha256(right.stderr)}"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--phase", type=int, default=1)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--rust", type=Path)
    parser.add_argument("--bun", type=Path)
    parser.add_argument(
        "--report-json",
        type=Path,
        help="write one exclusive canonical mechanical-conformance report",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        nargs=3,
        default=[],
        metavar=("LABEL", "RUNNER", "PATH"),
        help=(
            "run an exact installed candidate path without building; RUNNER is "
            "the expected embedded identity (rust or bun); repeat for every surface"
        ),
    )
    parser.add_argument(
        "--candidate-interpreter",
        action="append",
        nargs=2,
        default=[],
        metavar=("LABEL", "PATH"),
        help=(
            "invoke one candidate through an exact explicit interpreter; "
            "the interpreter bytes are bound into the report"
        ),
    )
    args = parser.parse_args(argv)
    if args.candidate and (args.rust is not None or args.bun is not None):
        parser.error("--candidate cannot be combined with --rust or --bun")
    if args.candidate and args.build:
        parser.error("--candidate cannot be combined with --build")
    if args.candidate_interpreter and not args.candidate:
        parser.error("--candidate-interpreter requires --candidate")
    if args.build:
        build_products()
    try:
        products = (
            parse_candidate_specs(args.candidate)
            if args.candidate
            else [
                Product("rust", args.rust or DEFAULT_RUST, "rust"),
                Product("bun", args.bun or DEFAULT_BUN, "bun"),
            ]
        )
        products = attach_candidate_interpreters(products, args.candidate_interpreter)
    except ValueError as error:
        parser.error(str(error))
    verified_products: list[Product] = []
    identities: dict[str, CandidateIdentity] = {}
    for product in products:
        product_path = _absolute_leaf(product.executable)
        verified = Product(
            product.name,
            product_path,
            product.expected_runner_name,
            (
                _absolute_leaf(product.interpreter)
                if product.interpreter is not None
                else None
            ),
        )
        try:
            identity = capture_candidate_identity(verified)
        except (OSError, ValueError) as error:
            print(
                f"invalid {product.name} candidate: {error}",
                file=sys.stderr,
            )
            return 2
        verified_products.append(verified)
        identities[verified.name] = identity
    products = verified_products

    contracts = ContractRegistry()
    failures: list[str] = []
    failure_records: list[dict[str, Any]] = []
    candidate_passed = 0
    candidate_failed = 0
    differential_passed = 0
    differential_failed = 0
    paths = list(case_paths(args.phase, set(args.case)))
    if not paths:
        print("no conformance cases selected", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="openprose-conformance-") as directory:
        suite_root = Path(directory)
        snapshots_root = suite_root / "candidate-snapshots"
        snapshots_root.mkdir(mode=0o700)
        try:
            products = [
                snapshot_product(
                    product,
                    identities[product.name],
                    snapshots_root / product.name,
                )
                for product in products
            ]
        except (OSError, ValueError) as error:
            print(f"cannot create owned candidate snapshot: {error}", file=sys.stderr)
            return 2
        snapshots_root.chmod(0o500)

        def retain_failure(
            case_id: str,
            labels: list[str],
            code: str,
            details: list[str],
        ) -> None:
            scope = "↔".join(labels)
            for detail in details:
                failures.append(f"{case_id} [{scope}] {detail}")
            report_detail = details[0]
            if len(details) > 1:
                report_detail += (
                    f" (+{len(details) - 1} additional validation "
                    f"failure{'s' if len(details) != 2 else ''})"
                )
            failure_records.append(
                {
                    "caseId": case_id,
                    "candidateLabels": labels,
                    "code": code,
                    "detail": sanitize_failure_detail(
                        report_detail, suite_root, products
                    ),
                }
            )

        for path in paths:
            case = json.loads(path.read_text("utf-8"))
            observations: dict[str, Observation] = {}
            candidate_valid: dict[str, bool] = {}
            case_root = suite_root / case["id"]
            for product in products:
                unit_failures: list[tuple[str, str]] = []
                custody_difference = candidate_identity_difference(
                    product, identities[product.name]
                )
                if custody_difference is None:
                    custody_difference = snapshot_identity_difference(
                        product, identities[product.name]
                    )
                if custody_difference is not None:
                    unit_failures.append(
                        ("CANDIDATE_BYTES_CHANGED", custody_difference)
                    )
                else:
                    environment_root, workspace = product_roots(case_root, product.name)
                    workspace.mkdir(parents=True, exist_ok=True)
                    observation = execute(
                        product,
                        case,
                        environment_root,
                        workspace,
                    )
                    observations[product.name] = observation
                    unit_failures.extend(
                        ("OUTPUT_VALIDATION_FAILED", failure)
                        for failure in validate_output(observation, contracts)
                    )
                    custody_difference = candidate_identity_difference(
                        product, identities[product.name]
                    )
                    if custody_difference is None:
                        custody_difference = snapshot_identity_difference(
                            product, identities[product.name]
                        )
                    if custody_difference is not None:
                        unit_failures.append(
                            ("CANDIDATE_BYTES_CHANGED", custody_difference)
                        )
                candidate_valid[product.name] = not unit_failures
                if unit_failures:
                    candidate_failed += 1
                    code = (
                        "CANDIDATE_BYTES_CHANGED"
                        if any(
                            failure_code == "CANDIDATE_BYTES_CHANGED"
                            for failure_code, _ in unit_failures
                        )
                        else "OUTPUT_VALIDATION_FAILED"
                    )
                    retain_failure(
                        case["id"],
                        [product.name],
                        code,
                        [detail for _, detail in unit_failures],
                    )
                else:
                    candidate_passed += 1

            baseline_product = products[0]
            for candidate_product in products[1:]:
                labels = [baseline_product.name, candidate_product.name]
                if not all(candidate_valid.get(label, False) for label in labels):
                    pair_failures = [
                        "differential unavailable because a candidate-case validation failed"
                    ]
                    code = "DIFFERENTIAL_UNAVAILABLE"
                else:
                    pair_failures = compare_pair(
                        [
                            observations[baseline_product.name],
                            observations[candidate_product.name],
                        ]
                    )
                    code = "DIFFERENTIAL_DRIFT"
                if pair_failures:
                    differential_failed += 1
                    retain_failure(case["id"], labels, code, pair_failures)
                else:
                    differential_passed += 1

        report = make_report(
            phase=args.phase,
            case_ids=[json.loads(path.read_text("utf-8"))["id"] for path in paths],
            candidates=[(product, identities[product.name]) for product in products],
            candidate_passed=candidate_passed,
            candidate_failed=candidate_failed,
            differential_passed=differential_passed,
            differential_failed=differential_failed,
            failures=failure_records,
        )
        if args.report_json is not None:
            try:
                write_report(args.report_json, report)
            except (OSError, ValueError) as error:
                print(f"cannot write conformance report: {error}", file=sys.stderr)
                return 2

    if failures:
        print(f"FAIL: {len(failures)} conformance failure(s)", file=sys.stderr)
        for failure in failures:
            stable_failure = sanitize_failure_detail(failure, suite_root, products)
            print(f"- {stable_failure}", file=sys.stderr)
        return 1
    print(f"PASS: {len(paths)} cases × {len(products)} products; differential clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
