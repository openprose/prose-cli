#!/usr/bin/env python3
"""Run one provider-free, non-publishing local OpenProse release rehearsal."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

import build_local


SCHEMA = "openprose.local-release-rehearsal/1"
ERROR_SCHEMA = "openprose.local-release-rehearsal-error/1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_BUDGET_SECONDS = 3_600.0
MIN_BUDGET_SECONDS = 60.0
EVIDENCE_FILES = (
    "build-report.json",
    "installed-package-raw.json",
    "installed-package-analysis.json",
    "installed-mechanical-conformance.json",
    "rehearsal-summary.json",
)
MANIFEST_NAME = "rehearsal-manifest.json"
CHECKSUM_NAME = "REHEARSAL-SHA256SUMS"
MARKER_NAME = ".openprose-local-rehearsal-root"
MARKER_BYTES = b"owned non-publishing rehearsal root\n"
SAFE_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
CONFORMANCE_PHASE = 7
CONFORMANCE_CASES = 50
CONFORMANCE_CANDIDATE_VALIDATIONS = 150
CONFORMANCE_DIFFERENTIAL_VALIDATIONS = 100
CONFORMANCE_TOTAL_VALIDATIONS = 250
SECRET_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "NODE_AUTH_TOKEN",
    }
)
SECRET_SUFFIXES = ("_API_KEY", "_PASSWORD", "_SECRET", "_TOKEN", "_CREDENTIALS")
AMBIENT_ALLOWLIST = frozenset({"PATH", "DEVELOPER_DIR", "SDKROOT"})


class RehearsalError(RuntimeError):
    """A local rehearsal boundary failed closed."""


@dataclass(frozen=True)
class OwnedOutput:
    path: Path
    device: int
    inode: int


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RehearsalError(f"{label} must be an object")
    return value


def require_exact(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise RehearsalError(f"{label} has an unsupported shape")


def require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise RehearsalError(f"{label} is not a lowercase SHA-256 digest")
    return value


def load_installed_benchmark() -> Any:
    path = (
        Path(__file__).resolve().parents[1]
        / "benchmarks"
        / "installed"
        / "benchmark.py"
    )
    spec = importlib.util.spec_from_file_location("openprose_installed_benchmark", path)
    if spec is None or spec.loader is None:
        raise RehearsalError("installed-package benchmark cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_conformance_runner() -> Any:
    path = Path(__file__).resolve().parents[1] / "conformance" / "runner" / "run.py"
    spec = importlib.util.spec_from_file_location("openprose_conformance_runner", path)
    if spec is None or spec.loader is None:
        raise RehearsalError("mechanical conformance runner cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def validate_cross_bindings(
    build_report: Any,
    raw_report: Any,
    analysis_report: Any,
) -> dict[str, Any]:
    build = require_object(build_report, "build report")
    require_exact(
        build,
        {
            "schema",
            "profile",
            "testSeamsEnabled",
            "globalStateModified",
            "detachedDescendantContainment",
            "candidates",
            "windowsProcessHost",
            "package",
            "install",
        },
        "build report",
    )
    if (
        build.get("schema") != "openprose.local-build-report/1"
        or build.get("profile") != "development"
        or build.get("testSeamsEnabled") is not True
        or build.get("globalStateModified") is not False
        or build.get("detachedDescendantContainment") != "not-enforced"
        or build.get("windowsProcessHost") is not None
        or build.get("install") is not None
    ):
        raise RehearsalError(
            "build report is not a provider-free POSIX development build"
        )
    candidates = require_object(build.get("candidates"), "build candidates")
    if set(candidates) != {"rust", "bun"}:
        raise RehearsalError("build report does not bind both candidates")
    candidate_digests: dict[str, str] = {}
    for name in ("rust", "bun"):
        candidate = require_object(candidates[name], f"{name} candidate")
        require_exact(
            candidate,
            {
                "path",
                "buildSourcePath",
                "snapshotPath",
                "snapshotOwnership",
                "byteLength",
                "sha256",
                "smoke",
            },
            f"{name} candidate",
        )
        if (
            candidate.get("snapshotOwnership") != "ephemeral-owned-root"
            or candidate.get("smoke") != "pass"
            or candidate.get("path") != candidate.get("snapshotPath")
            or not str(candidate.get("snapshotPath", "")).startswith(
                f"$OPENPROSE_LOCAL_BUILD/candidates/{name}/"
            )
        ):
            raise RehearsalError(f"{name} candidate lacks owned snapshot custody")
        if (
            not isinstance(candidate.get("byteLength"), int)
            or candidate["byteLength"] < 1
        ):
            raise RehearsalError(f"{name} candidate length is invalid")
        candidate_digests[name] = require_sha(
            candidate.get("sha256"), f"{name} candidate"
        )
    package = require_object(build.get("package"), "build package")
    require_exact(
        package,
        {"path", "mode", "purpose", "inputBuild", "inputs", "sha256Sums"},
        "build package",
    )
    if (
        package.get("mode") != "development"
        or package.get("purpose") != build_local.MOCK_PACKAGE_PURPOSE
        or package.get("inputBuild")
        != {
            "image": "sentinel-v1",
            "profile": "development",
            "testSeamsEnabled": True,
        }
    ):
        raise RehearsalError("build package is not exact mock-benchmark evidence")
    package_inputs = require_object(package.get("inputs"), "build package inputs")
    if set(package_inputs) != {"rust", "bun"}:
        raise RehearsalError("build package does not bind both mock inputs")
    for name in ("rust", "bun"):
        package_input = require_object(
            package_inputs[name], f"build package {name} input"
        )
        require_exact(
            package_input,
            {
                "path",
                "buildSourcePath",
                "snapshotPath",
                "snapshotOwnership",
                "byteLength",
                "sha256",
            },
            f"build package {name} input",
        )
        if (
            package_input.get("path") != candidates[name].get("path")
            or package_input.get("snapshotPath") != candidates[name].get("snapshotPath")
            or package_input.get("snapshotOwnership") != "ephemeral-owned-root"
            or package_input.get("byteLength") != candidates[name].get("byteLength")
            or package_input.get("sha256") != candidate_digests[name]
        ):
            raise RehearsalError(
                f"build package {name} input differs from the smoked candidate"
            )
    sums = require_object(package.get("sha256Sums"), "package SHA256SUMS")
    require_exact(sums, {"path", "byteLength", "sha256"}, "package SHA256SUMS")
    if sums.get("path") != "SHA256SUMS" or not isinstance(sums.get("byteLength"), int):
        raise RehearsalError("build package checksum identity is malformed")
    sums_digest = require_sha(sums.get("sha256"), "build package SHA256SUMS")

    raw = require_object(raw_report, "installed benchmark report")
    if raw.get("schema") != "openprose.installed-package-benchmark/1":
        raise RehearsalError("installed benchmark report schema differs")
    surfaces = require_object(raw.get("surfaces"), "installed benchmark surfaces")
    if set(surfaces) != {"direct-rust", "direct-bun", "npm-launcher"}:
        raise RehearsalError("installed benchmark does not bind exactly three surfaces")
    surface_digests = {
        name: require_sha(require_object(record, name).get("binarySha256"), name)
        for name, record in surfaces.items()
    }
    if surface_digests["direct-rust"] != candidate_digests["rust"]:
        raise RehearsalError(
            "installed Rust bytes diverge from the owned build candidate"
        )
    if (
        surface_digests["direct-bun"] != candidate_digests["bun"]
        or surface_digests["npm-launcher"] != candidate_digests["bun"]
    ):
        raise RehearsalError(
            "installed Bun bytes diverge across build/archive/npm custody"
        )
    measurement_plan = require_object(raw.get("measurementPlan"), "measurement plan")
    if (
        isinstance(measurement_plan.get("trials"), bool)
        or not isinstance(measurement_plan.get("trials"), int)
        or measurement_plan["trials"] < 1
        or isinstance(measurement_plan.get("timeoutSeconds"), bool)
        or not isinstance(measurement_plan.get("timeoutSeconds"), (int, float))
        or not math.isfinite(measurement_plan["timeoutSeconds"])
        or measurement_plan["timeoutSeconds"] <= 0
        or measurement_plan.get("deadlineApplied") is not True
    ):
        raise RehearsalError("installed benchmark measurement plan is not bounded")
    package_identity = require_object(raw.get("packageIdentity"), "package identity")
    require_exact(
        package_identity,
        {
            "version",
            "sourceRevision",
            "releaseManifestSha256",
            "dependencyEvidenceSha256",
            "sha256SumsSha256",
            "rustBinarySha256",
            "bunBinarySha256",
        },
        "package identity",
    )
    dependency_digest = require_sha(
        package_identity.get("dependencyEvidenceSha256"), "dependency evidence"
    )
    if (
        require_sha(package_identity.get("rustBinarySha256"), "package Rust binary")
        != candidate_digests["rust"]
        or require_sha(package_identity.get("bunBinarySha256"), "package Bun binary")
        != candidate_digests["bun"]
    ):
        raise RehearsalError("package binary identity differs from build candidates")
    if package_identity.get("sha256SumsSha256") != sums_digest:
        raise RehearsalError(
            "installed benchmark package differs from the build output"
        )
    if package_identity.get("sourceRevision") != "development":
        raise RehearsalError("installed benchmark source identity is not development")

    analysis = require_object(analysis_report, "installed benchmark analysis")
    if analysis.get("schema") != "openprose.installed-package-benchmark-analysis/1":
        raise RehearsalError("installed benchmark analysis schema differs")
    raw_digest = digest(canonical_json(raw))
    if analysis.get("sourceReportSha256") != raw_digest:
        raise RehearsalError(
            "installed benchmark analysis is not bound to the raw report"
        )
    if analysis.get("packageIdentity") != package_identity:
        raise RehearsalError("installed benchmark analysis changed package identity")
    expected_limitations = {
        "detachedDescendantContainment": "not-enforced",
        "providerCalls": "none",
        "semanticEvaluation": "not-performed",
        "portabilityEvaluation": "not-performed",
        "releaseEvaluation": "not-performed",
        "ranking": "not-produced",
        "runtimeNetworkIsolation": "not-enforced",
    }
    if (
        raw.get("limitations") != expected_limitations
        or analysis.get("limitations") != expected_limitations
    ):
        raise RehearsalError("benchmark limitations were weakened")
    return {
        "rustCandidateSha256": candidate_digests["rust"],
        "bunCandidateSha256": candidate_digests["bun"],
        "packageSha256SumsSha256": sums_digest,
        "rawReportSha256": raw_digest,
        "releaseManifestSha256": require_sha(
            package_identity.get("releaseManifestSha256"), "release manifest"
        ),
        "dependencyEvidenceSha256": dependency_digest,
        "measurementPlanSha256": digest(canonical_json(measurement_plan)),
        "threeInstalledSurfacesBound": True,
    }


def _deadline_remaining(deadline: float, stage: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RehearsalError(f"rehearsal deadline expired {stage}")
    return remaining


def _canonical_output(path: Path) -> Path:
    if not isinstance(path, Path) or SAFE_BASENAME.fullmatch(path.name) is None:
        raise RehearsalError("rehearsal output basename is unsafe")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise RehearsalError(
            f"rehearsal output parent is unavailable: {error}"
        ) from error
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise RehearsalError(
            f"cannot inspect rehearsal output parent: {error}"
        ) from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise RehearsalError("rehearsal output parent must resolve to a directory")
    return parent / path.name


def _owned_output(path: Path) -> OwnedOutput:
    output = _canonical_output(path)
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RehearsalError(f"cannot inspect rehearsal output: {error}") from error
    else:
        raise RehearsalError("rehearsal output must not already exist")
    try:
        output.mkdir(mode=0o700)
        metadata = output.lstat()
    except OSError as error:
        raise RehearsalError(f"cannot create rehearsal output: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RehearsalError("rehearsal output ownership is unsafe")
    return OwnedOutput(output, metadata.st_dev, metadata.st_ino)


def _existing_output(path: Path) -> OwnedOutput:
    output = _canonical_output(path)
    try:
        metadata = output.lstat()
    except OSError as error:
        raise RehearsalError(f"rehearsal output is unavailable: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RehearsalError("rehearsal output must be a non-symlink directory")
    return OwnedOutput(output, metadata.st_dev, metadata.st_ino)


def _verify_root(root: OwnedOutput) -> None:
    try:
        metadata = root.path.lstat()
        canonical = root.path.resolve(strict=True)
    except OSError as error:
        raise RehearsalError(f"rehearsal output ownership changed: {error}") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != (root.device, root.inode)
        or canonical != root.path
    ):
        raise RehearsalError("rehearsal output ownership changed")


def _read_regular(path: Path, label: str, maximum: int = MAX_EVIDENCE_BYTES) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise RehearsalError(f"cannot inspect {label}: {error}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RehearsalError(f"{label} must be a non-symlink regular file")
    if before.st_size < 1 or before.st_size > maximum:
        raise RehearsalError(f"{label} has an invalid size")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RehearsalError(f"cannot open {label}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise RehearsalError(f"{label} identity changed before read")
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not block:
                break
            blocks.append(block)
            total += len(block)
            if total > maximum:
                raise RehearsalError(f"{label} exceeded its size limit")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if total != opened.st_size or (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
        raise RehearsalError(f"{label} changed while read")
    return b"".join(blocks)


def _write_exclusive(
    root: OwnedOutput, name: str, encoded: bytes, *, deadline: float
) -> None:
    _deadline_remaining(deadline, f"before writing {name}")
    _verify_root(root)
    if name not in {*EVIDENCE_FILES, MANIFEST_NAME, CHECKSUM_NAME, MARKER_NAME}:
        raise RehearsalError(f"unsupported rehearsal evidence path: {name}")
    path = root.path / name
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise RehearsalError(
            f"cannot write rehearsal evidence {name}: {error}"
        ) from error
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        written = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(written.st_mode) or written.st_size != len(encoded):
        raise RehearsalError(f"written rehearsal evidence identity differs: {name}")
    if _read_regular(path, f"written rehearsal evidence {name}") != encoded:
        raise RehearsalError(f"written rehearsal evidence bytes differ: {name}")
    _verify_root(root)


def _secret_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in SECRET_NAMES or normalized.endswith(SECRET_SUFFIXES)


def _original_secret_values(source: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value
                for name, value in source.items()
                if _secret_name(name) and isinstance(value, str) and value
            }
        )
    )


def _isolated_environment(source: Mapping[str, str], runtime: Path) -> dict[str, str]:
    directories = {
        "HOME": runtime / "home",
        "XDG_CONFIG_HOME": runtime / "config",
        "XDG_CACHE_HOME": runtime / "cache",
        "TMPDIR": runtime / "tmp",
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700, parents=True)
    environment = {
        name: value
        for name, value in source.items()
        if name in AMBIENT_ALLOWLIST and isinstance(value, str) and "\x00" not in value
    }
    environment.setdefault("PATH", os.defpath)
    environment.update({name: str(path) for name, path in directories.items()})
    cargo_home = runtime / "cargo-home"
    cargo_home.mkdir(mode=0o700)
    original_home = Path(source["HOME"]) if source.get("HOME") else None
    original_cargo = (
        Path(source["CARGO_HOME"])
        if source.get("CARGO_HOME")
        else original_home / ".cargo"
        if original_home is not None
        else None
    )
    if original_cargo is not None:
        for cache_name in ("registry", "git"):
            cache = original_cargo / cache_name
            try:
                resolved_cache = cache.resolve(strict=True)
                metadata = resolved_cache.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                (cargo_home / cache_name).symlink_to(
                    resolved_cache, target_is_directory=True
                )
    environment["CARGO_HOME"] = str(cargo_home)
    original_rustup = (
        Path(source["RUSTUP_HOME"])
        if source.get("RUSTUP_HOME")
        else original_home / ".rustup"
        if original_home is not None
        else None
    )
    if original_rustup is not None:
        try:
            resolved_rustup = original_rustup.resolve(strict=True)
            rustup_metadata = resolved_rustup.lstat()
        except OSError:
            pass
        else:
            if stat.S_ISDIR(rustup_metadata.st_mode) and not stat.S_ISLNK(
                rustup_metadata.st_mode
            ):
                environment["RUSTUP_HOME"] = str(resolved_rustup)
    environment.update(
        {
            "TEMP": str(directories["TMPDIR"]),
            "TMP": str(directories["TMPDIR"]),
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "CARGO_NET_OFFLINE": "true",
            "npm_config_offline": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    return environment


def _source_identity(value: Any) -> dict[str, Any]:
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    module_name = getattr(value, "__module__", None) or type(value).__module__
    source_path: str | None
    try:
        source_path = inspect.getsourcefile(value)
    except (OSError, TypeError):
        source_path = None
    source_digest = None
    if source_path is not None:
        try:
            source_digest = digest(_read_regular(Path(source_path), "tool source"))
        except RehearsalError:
            source_digest = None
    return {
        "name": f"{module_name}.{name or type(value).__name__}",
        "sourceSha256": source_digest,
    }


def _resolved_tool_identity(
    requested: str, environment: Mapping[str, str], *, executable: Path | None = None
) -> dict[str, Any]:
    located = (
        str(executable)
        if executable is not None
        else shutil.which(requested, path=environment.get("PATH"))
    )
    if located is None:
        return {"requested": requested, "status": "unavailable"}
    try:
        resolved = Path(located).resolve(strict=True)
        record = build_local.digest_file(resolved)
    except (OSError, build_local.LocalBuildError) as error:
        raise RehearsalError(
            f"cannot identify required build tool {requested}: {error}"
        ) from error
    return {
        "requested": requested,
        "status": "resolved",
        "resolvedPath": record["path"],
        "byteLength": record["byteLength"],
        "sha256": record["sha256"],
    }


def _assert_no_secret_evidence(
    reports: Mapping[str, bytes], secrets: Sequence[str]
) -> None:
    for secret in secrets:
        candidates = {
            secret.encode("utf-8"),
            json.dumps(secret, ensure_ascii=False)[1:-1].encode("utf-8"),
        }
        if any(
            candidate and candidate in encoded
            for encoded in reports.values()
            for candidate in candidates
        ):
            raise RehearsalError(
                "original secret environment value entered rehearsal evidence"
            )


def _current_conformance_case_ids(
    runner: Any, *, require_frozen_count: bool = False
) -> list[str]:
    try:
        paths = list(runner.case_paths(CONFORMANCE_PHASE, set()))
        observed_ids = [
            require_object(json.loads(path.read_text("utf-8")), "conformance case").get(
                "id"
            )
            for path in paths
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise RehearsalError(
            f"cannot identify the current Phase 7 corpus: {error}"
        ) from error
    if (
        not observed_ids
        or any(not isinstance(case_id, str) or not case_id for case_id in observed_ids)
        or len(set(observed_ids)) != len(observed_ids)
    ):
        raise RehearsalError(
            "current Phase 7 corpus must contain unique nonempty case IDs"
        )
    if require_frozen_count and len(observed_ids) != CONFORMANCE_CASES:
        raise RehearsalError(
            "current Phase 7 corpus must contain exactly "
            f"{CONFORMANCE_CASES} cases for a new rehearsal"
        )
    return sorted(observed_ids)


def _installed_path(install: Path, value: Any, label: str) -> Path:
    prefix = "$INSTALL_ROOT/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise RehearsalError(f"{label} is not an install-root-relative path")
    relative = PurePosixPath(value[len(prefix) :])
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RehearsalError(f"{label} is unsafe")
    return install.joinpath(*relative.parts)


def _conformance_inputs(
    install: Path, raw_report: Mapping[str, Any]
) -> tuple[list[tuple[str, str, Path]], Path, str]:
    plan = require_object(raw_report.get("measurementPlan"), "measurement plan")
    paths = require_object(plan.get("executablePaths"), "executable paths")
    if set(paths) != {"direct-rust", "direct-bun", "npm-launcher"}:
        raise RehearsalError("installed executable path inventory differs")
    candidates = [
        (
            "direct-rust",
            "rust",
            _installed_path(install, paths["direct-rust"], "Rust executable"),
        ),
        (
            "direct-bun",
            "bun",
            _installed_path(install, paths["direct-bun"], "Bun executable"),
        ),
        (
            "npm-launcher",
            "bun",
            _installed_path(install, paths["npm-launcher"], "npm launcher"),
        ),
    ]
    for label, _runner, path in candidates:
        _require_custodied_path(
            install, path, label, allow_leaf_symlink=label == "npm-launcher"
        )
    toolchain = require_object(raw_report.get("toolchain"), "benchmark toolchain")
    node = require_object(toolchain.get("node"), "Node tool identity")
    if set(node) != {"command", "resolvedPath", "sha256", "version"}:
        raise RehearsalError("Node tool identity has an unsupported shape")
    node_version = node.get("version")
    if (
        not isinstance(node_version, str)
        or re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", node_version) is None
    ):
        raise RehearsalError("Node tool version is malformed")
    node_digest = require_sha(node.get("sha256"), "Node tool")
    resolved = node.get("resolvedPath")
    if not isinstance(resolved, str) or not Path(resolved).is_absolute():
        raise RehearsalError("Node tool path is not absolute")
    try:
        node_path = Path(resolved).resolve(strict=True)
        observed = build_local.digest_file(node_path)
    except (OSError, build_local.LocalBuildError) as error:
        raise RehearsalError(
            f"cannot authenticate exact Node interpreter: {error}"
        ) from error
    if observed["sha256"] != node_digest:
        raise RehearsalError("exact Node interpreter differs from benchmark evidence")
    return candidates, node_path, node_digest


def _run_mechanical_conformance(
    runner: Any,
    raw_report: Mapping[str, Any],
    install: Path,
    report_path: Path,
    environment: Mapping[str, str],
    deadline: float,
    executor: Callable[[Sequence[str], Path, Mapping[str, str]], Any] | None,
) -> bytes:
    candidates, node_path, _node_digest = _conformance_inputs(install, raw_report)
    argv = [
        sys.executable,
        str(Path(runner.__file__).resolve()),
        "--phase",
        str(CONFORMANCE_PHASE),
        "--report-json",
        str(report_path),
    ]
    for label, expected_runner, executable in candidates:
        argv.extend(("--candidate", label, expected_runner, str(executable)))
    argv.extend(("--candidate-interpreter", "npm-launcher", str(node_path)))
    remaining = _deadline_remaining(deadline, "before Phase 7 conformance")
    execute = executor
    if execute is None:
        execute = lambda arguments, cwd, env: build_local.execute_bounded(
            arguments,
            cwd,
            env,
            timeout_seconds=min(build_local.COMMAND_TIMEOUT_SECONDS, remaining),
        )
    completed = execute(argv, Path(__file__).resolve().parents[2], environment)
    _deadline_remaining(deadline, "after Phase 7 conformance")
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout)[:4096]
        raise RehearsalError(
            "installed Phase 7 conformance failed: "
            + diagnostic.decode("utf-8", "replace").strip()
        )
    if completed.stderr:
        raise RehearsalError("installed Phase 7 conformance emitted diagnostics")
    return _read_regular(report_path, "installed mechanical conformance report")


def validate_mechanical_conformance(
    report: Any,
    encoded: bytes,
    runner: Any,
    raw_report: Mapping[str, Any],
    *,
    require_current_admission: bool = True,
) -> dict[str, Any]:
    value = require_object(report, "mechanical conformance report")
    try:
        failures = runner.validate_report(value)
        rendered = runner.render_report(value)
    except (TypeError, ValueError) as error:
        raise RehearsalError(
            f"mechanical conformance report is invalid: {error}"
        ) from error
    if failures:
        raise RehearsalError(
            "mechanical conformance report is invalid: " + "; ".join(failures)
        )
    if rendered != encoded:
        raise RehearsalError("mechanical conformance report is not canonical")
    case_ids = value.get("caseIds")
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or [
        item.get("label") for item in candidates
    ] != ["direct-rust", "direct-bun", "npm-launcher"]:
        raise RehearsalError("mechanical conformance candidate order differs")
    if not isinstance(case_ids, list):
        raise RehearsalError(
            "mechanical conformance result is not the exact admitted pass"
        )
    if require_current_admission:
        candidate_validations = CONFORMANCE_CANDIDATE_VALIDATIONS
        differential_validations = CONFORMANCE_DIFFERENTIAL_VALIDATIONS
        total_validations = CONFORMANCE_TOTAL_VALIDATIONS
    else:
        candidate_validations = len(case_ids) * len(candidates)
        differential_validations = len(case_ids) * max(0, len(candidates) - 1)
        total_validations = candidate_validations + differential_validations
    validations = require_object(value.get("validations"), "conformance validations")
    expected_validations = {
        "candidateCases": {
            "total": candidate_validations,
            "passed": candidate_validations,
            "failed": 0,
        },
        "differential": {
            "total": differential_validations,
            "passed": differential_validations,
            "failed": 0,
        },
        "total": total_validations,
        "passed": total_validations,
        "failed": 0,
        "status": "pass",
    }
    expected_claims = {
        "scope": "selected-provider-free-mechanical-corpus-only",
        "detachedDescendantContainment": False,
        "semanticConformance": False,
        "releaseAdmission": False,
    }
    if (
        value.get("phase") != CONFORMANCE_PHASE
        or (require_current_admission and len(case_ids) != CONFORMANCE_CASES)
        or validations != expected_validations
        or value.get("failures") != []
        or value.get("claims") != expected_claims
    ):
        raise RehearsalError(
            "mechanical conformance result is not the exact admitted pass"
        )
    surfaces = require_object(raw_report.get("surfaces"), "installed surfaces")
    rust_surface = require_object(surfaces.get("direct-rust"), "Rust surface")
    bun_surface = require_object(surfaces.get("direct-bun"), "Bun surface")
    npm_surface = require_object(surfaces.get("npm-launcher"), "npm surface")
    toolchain = require_object(raw_report.get("toolchain"), "benchmark toolchain")
    node = require_object(toolchain.get("node"), "Node tool identity")
    expected = (
        ("rust", require_sha(rust_surface.get("binarySha256"), "Rust binary"), None),
        ("bun", require_sha(bun_surface.get("binarySha256"), "Bun binary"), None),
        ("bun", None, require_sha(node.get("sha256"), "Node")),
    )
    launcher_identity = require_object(
        npm_surface.get("launcherCommandIdentity"),
        "npm launcher command identity",
    )
    npm_target_sha = require_sha(
        (
            launcher_identity.get("resolvedLauncherSha256")
            if launcher_identity.get("kind") == "symlink"
            else launcher_identity.get("sha256")
        ),
        "npm launcher target",
    )
    expected = (expected[0], expected[1], ("bun", npm_target_sha, expected[2][2]))
    for candidate, (runner_name, target_sha, interpreter_sha) in zip(
        candidates, expected
    ):
        if (
            candidate.get("expectedRunnerIdentity") != runner_name
            or require_object(candidate.get("resolvedTarget"), "candidate target").get(
                "sha256"
            )
            != target_sha
        ):
            raise RehearsalError("mechanical conformance candidate bytes differ")
        interpreter = candidate.get("interpreter")
        if interpreter_sha is None:
            if interpreter is not None:
                raise RehearsalError(
                    "direct candidate unexpectedly used an interpreter"
                )
        elif (
            not isinstance(interpreter, dict)
            or require_object(interpreter.get("resolvedTarget"), "Node target").get(
                "sha256"
            )
            != interpreter_sha
        ):
            raise RehearsalError("mechanical conformance Node interpreter differs")
    return {
        "reportSha256": digest(encoded),
        "phase": CONFORMANCE_PHASE,
        "caseIds": case_ids,
        "caseIdsSha256": digest(canonical_json(case_ids)),
        "candidateLabels": ["direct-rust", "direct-bun", "npm-launcher"],
        "candidateCaseValidations": candidate_validations,
        "differentialValidations": differential_validations,
        "totalValidations": total_validations,
        "nodeInterpreterSha256": expected[2][2],
        "semanticConformance": False,
        "releaseAdmission": False,
    }


def rehearse(
    output: Path,
    *,
    trials: int = 3,
    timeout_seconds: float = 10.0,
    budget_seconds: float = 1_200.0,
    ambient: Mapping[str, str] | None = None,
    builder: Callable[..., dict[str, Any]] = build_local.build,
    benchmark_module: Any | None = None,
    conformance_module: Any | None = None,
    conformance_executor: Callable[[Sequence[str], Path, Mapping[str, str]], Any]
    | None = None,
) -> dict[str, Any]:
    if os.name == "nt":
        raise RehearsalError(
            "Windows rehearsal requires native Job Object containment before any build or spawn"
        )
    if isinstance(trials, bool) or not isinstance(trials, int) or not 1 <= trials <= 20:
        raise RehearsalError("trials must be between 1 and 20")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 1 <= timeout_seconds <= 120
    ):
        raise RehearsalError("timeout must be between 1 and 120 seconds")
    if (
        isinstance(budget_seconds, bool)
        or not isinstance(budget_seconds, (int, float))
        or not math.isfinite(budget_seconds)
        or not MIN_BUDGET_SECONDS <= budget_seconds <= MAX_BUDGET_SECONDS
    ):
        raise RehearsalError("budget must be between 60 and 3600 seconds")
    started = time.monotonic()
    deadline = started + float(budget_seconds)
    root = _owned_output(output)
    _write_exclusive(root, MARKER_NAME, MARKER_BYTES, deadline=deadline)
    package = root.path / "package"
    install = root.path / "installed"
    module = (
        benchmark_module if benchmark_module is not None else load_installed_benchmark()
    )
    runner = (
        conformance_module
        if conformance_module is not None
        else load_conformance_runner()
    )
    ambient_source = dict(os.environ if ambient is None else ambient)
    original_secrets = _original_secret_values(ambient_source)

    def deadline_executor(
        argv: Sequence[str], cwd: Path, environment: Mapping[str, str]
    ) -> Any:
        remaining = _deadline_remaining(deadline, "before a build subprocess")
        completed = build_local.execute_bounded(
            argv,
            cwd,
            environment,
            timeout_seconds=min(build_local.COMMAND_TIMEOUT_SECONDS, remaining),
        )
        _deadline_remaining(deadline, "after a build subprocess")
        return completed

    with tempfile.TemporaryDirectory(
        prefix="openprose-rehearsal-runtime-"
    ) as temporary:
        environment = _isolated_environment(ambient_source, Path(temporary))
        build_tools = {
            "cargo": _resolved_tool_identity("cargo", environment),
            "bun": _resolved_tool_identity("bun", environment),
            "python": _resolved_tool_identity(
                "python", environment, executable=Path(sys.executable)
            ),
        }
        _deadline_remaining(deadline, "before build/package")
        build_report = builder(
            selection="both",
            smoke=True,
            package=package,
            install_dir=None,
            ambient=environment,
            executor=deadline_executor,
            package_purpose=build_local.MOCK_PACKAGE_PURPOSE,
        )
        _deadline_remaining(deadline, "after build/package")
        _verify_root(root)
        raw_report = module.run_benchmark(
            package,
            install,
            trials=trials,
            timeout_seconds=timeout_seconds,
            deadline_monotonic=deadline,
        )
        _deadline_remaining(deadline, "before retained tree authentication")
        try:
            trees_before = module.verify_retained_install_trees(install, raw_report)
        except Exception as error:
            if error.__class__.__name__ != "BenchmarkError":
                raise
            raise RehearsalError(str(error)) from error
        current_case_ids = _current_conformance_case_ids(
            runner, require_frozen_count=True
        )
        conformance_path = Path(temporary) / "installed-mechanical-conformance.json"
        encoded_conformance = _run_mechanical_conformance(
            runner,
            raw_report,
            install,
            conformance_path,
            environment,
            deadline,
            conformance_executor,
        )
        conformance_report = _json_no_duplicates(
            encoded_conformance, "mechanical conformance report"
        )
        conformance_binding = validate_mechanical_conformance(
            conformance_report, encoded_conformance, runner, raw_report
        )
        if conformance_binding["caseIds"] != current_case_ids:
            raise RehearsalError(
                "mechanical conformance report differs from the current Phase 7 corpus"
            )
        try:
            trees_after = module.verify_retained_install_trees(install, raw_report)
        except Exception as error:
            if error.__class__.__name__ != "BenchmarkError":
                raise
            raise RehearsalError(str(error)) from error
        if trees_before != trees_after:
            raise RehearsalError("installed tree identities changed during conformance")
        _conformance_inputs(install, raw_report)
        _verify_current_package_and_install(module, package, install, raw_report)
    _deadline_remaining(deadline, "after installed trials")
    _verify_root(root)
    _deadline_remaining(deadline, "before benchmark analysis")
    analysis_report = module.analyse_report(raw_report)
    _deadline_remaining(deadline, "after benchmark analysis")
    encoded_raw = module.render_json(raw_report)
    if module.render_json(
        module.analyse_report(module.json_no_duplicates(encoded_raw, "raw report"))
    ) != module.render_json(analysis_report):
        raise RehearsalError("installed benchmark reanalysis is not byte-stable")
    bindings = validate_cross_bindings(build_report, raw_report, analysis_report)
    bindings["installedTreeDigests"] = trees_after
    bindings["mechanicalConformance"] = conformance_binding
    measurement_plan = require_object(
        raw_report.get("measurementPlan"), "measurement plan"
    )
    if (
        measurement_plan.get("trials") != trials
        or measurement_plan.get("timeoutSeconds") != timeout_seconds
        or measurement_plan.get("deadlineApplied") is not True
    ):
        raise RehearsalError("benchmark measurement plan differs from rehearsal policy")
    _deadline_remaining(deadline, "before evidence construction")
    generator_identity = {
        "name": "openprose-local-release-rehearsal",
        "version": 1,
        "pythonVersion": platform.python_version(),
        "rehearsalSourceSha256": digest(
            _read_regular(Path(__file__).resolve(), "rehearsal generator source")
        ),
        "builder": _source_identity(builder),
        "benchmark": _source_identity(module),
        "conformance": _source_identity(runner),
        "boundedExecutor": _source_identity(build_local.execute_bounded),
        "buildTools": build_tools,
        "benchmarkToolchain": raw_report.get("toolchain", "unavailable"),
    }
    _deadline_remaining(deadline, "after tool identity capture")
    elapsed_before_write = round(time.monotonic() - started, 6)
    reports = {
        "build-report.json": canonical_json(build_report),
        "installed-package-raw.json": encoded_raw,
        "installed-package-analysis.json": module.render_json(analysis_report),
        "installed-mechanical-conformance.json": encoded_conformance,
    }
    summary = {
        "schema": SCHEMA,
        "status": "passed-local-development-rehearsal",
        "packageIdentity": raw_report["packageIdentity"],
        "bindings": bindings,
        "surfaceTimingSummaries": analysis_report["surfaceTimingSummaries"],
        "claims": {
            "deterministicMockSelected": True,
            "providerCallMonitoring": "not-performed",
            "networkIsolation": "not-enforced",
            "detachedDescendantContainment": "not-enforced",
            "semanticConformance": False,
            "portabilityEvaluated": False,
            "releaseEligible": False,
            "publicationAuthorized": False,
            "rankingProduced": False,
            "nativeWindowsAdmission": False,
        },
        "limitations": raw_report["limitations"],
        "policy": {
            "trials": trials,
            "timeoutSeconds": timeout_seconds,
            "budgetSeconds": budget_seconds,
            "execution": "deterministic-provider-free-mock",
        },
        "timing": {
            "elapsedSeconds": elapsed_before_write,
            "measurement": "variable-wall-clock",
            "scope": "through-analysis-and-tool-identity-before-evidence-write",
        },
        "generator": generator_identity,
    }
    reports["rehearsal-summary.json"] = canonical_json(summary)
    _assert_no_secret_evidence(reports, original_secrets)
    for name in EVIDENCE_FILES:
        _write_exclusive(root, name, reports[name], deadline=deadline)
    manifest = {
        "schema": "openprose.local-release-rehearsal-manifest/1",
        "status": "non-publishing-development-evidence",
        "files": [
            {
                "path": name,
                "byteLength": len(reports[name]),
                "sha256": digest(reports[name]),
            }
            for name in EVIDENCE_FILES
        ],
        "releaseEligible": False,
        "publicationAuthorized": False,
    }
    manifest_bytes = canonical_json(manifest)
    _write_exclusive(root, MANIFEST_NAME, manifest_bytes, deadline=deadline)
    checksum_members = {**reports, MANIFEST_NAME: manifest_bytes}
    sums_bytes = "".join(
        f"{digest(value)}  {name}\n" for name, value in sorted(checksum_members.items())
    ).encode("ascii")
    _assert_no_secret_evidence(
        {**reports, MANIFEST_NAME: manifest_bytes, CHECKSUM_NAME: sums_bytes},
        original_secrets,
    )
    _write_exclusive(root, CHECKSUM_NAME, sums_bytes, deadline=deadline)
    _deadline_remaining(deadline, "after final evidence write")
    _verify_root(root)
    return summary


def _json_no_duplicates(encoded: bytes, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise RehearsalError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RehearsalError(f"{label} is not valid UTF-8 JSON") from error
    return require_object(value, label)


def _parse_checksums(encoded: bytes) -> dict[str, str]:
    try:
        text = encoded.decode("ascii")
    except UnicodeDecodeError as error:
        raise RehearsalError("rehearsal checksums must be ASCII") from error
    if not text.endswith("\n"):
        raise RehearsalError("rehearsal checksums must end in LF")
    records: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(
            r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,127})", line
        )
        if match is None:
            raise RehearsalError("rehearsal checksum record is malformed")
        value, name = match.groups()
        if name in records:
            raise RehearsalError("rehearsal checksum record is duplicated")
        records[name] = value
    return records


def _verify_current_package_and_install(
    module: Any,
    package: Path,
    install: Path,
    raw: Mapping[str, Any],
) -> None:
    try:
        package_metadata = package.lstat()
        install_metadata = install.lstat()
    except OSError as error:
        raise RehearsalError(
            f"cannot inspect retained package/install roots: {error}"
        ) from error
    if any(
        stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
        for metadata in (package_metadata, install_metadata)
    ):
        raise RehearsalError("retained package/install roots are unsafe")
    platform_value = raw.get("platform")
    if not isinstance(platform_value, str):
        raise RehearsalError("installed benchmark platform is unavailable")
    context = module.verify_package_output(package, expected_platform=platform_value)
    package_identity = require_object(raw.get("packageIdentity"), "package identity")
    sums_path = package / "SHA256SUMS"
    _require_custodied_path(package, sums_path, "retained package SHA256SUMS")
    sums_bytes = module.safe_read(
        sums_path,
        getattr(module, "MAX_EVIDENCE_BYTES", MAX_EVIDENCE_BYTES),
    )
    observed_identity = {
        "version": context["release"]["version"],
        "sourceRevision": context["release"]["source"]["revision"],
        "releaseManifestSha256": context["checksums"]["release-manifest.json"],
        "dependencyEvidenceSha256": context["checksums"]["dependency-evidence.json"],
        "sha256SumsSha256": module.sha256_bytes(sums_bytes),
        "rustBinarySha256": package_identity.get("rustBinarySha256"),
        "bunBinarySha256": package_identity.get("bunBinarySha256"),
    }
    if package_identity != observed_identity:
        raise RehearsalError("current package identity differs from benchmark evidence")
    raw_artifacts = raw.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise RehearsalError("installed benchmark artifact records are unavailable")
    observed_artifacts = {
        name: {
            "path": name,
            "kind": record["kind"],
            "implementation": record["implementation"],
            "platform": record["platform"],
            "byteLength": len(context["encoded"][name]),
            "sha256": context["checksums"][name],
        }
        for name, record in context["artifacts"].items()
    }
    if raw_artifacts != [
        observed_artifacts[name] for name in sorted(observed_artifacts)
    ]:
        raise RehearsalError("current package artifacts differ from benchmark evidence")
    if raw.get("evidence") != context["evidence"]:
        raise RehearsalError("current package evidence differs from benchmark evidence")

    version = package_identity["version"]
    executable_name = "prose.exe" if platform_value.startswith("win32-") else "prose"
    surfaces = require_object(raw.get("surfaces"), "installed surfaces")
    for implementation in ("rust", "bun"):
        root_name = f"openprose-prose-cli-{implementation}-{version}-{platform_value}"
        binary = install / f"{implementation}-standalone" / root_name / executable_name
        _require_custodied_path(install, binary, f"installed {implementation} binary")
        encoded = module.safe_read(
            binary, getattr(module, "MAX_MEMBER_BYTES", MAX_EVIDENCE_BYTES)
        )
        if (
            module.sha256_bytes(encoded)
            != surfaces[f"direct-{implementation}"]["binarySha256"]
        ):
            raise RehearsalError(
                f"current installed {implementation} binary differs from benchmark evidence"
            )
    command, meta_root, platform_root, _modules = module.npm_layout(
        install / "npm-prefix", platform_value
    )
    launcher_source = meta_root / "bin" / "prose.js"
    platform_manifest = platform_root / "package.json"
    packaged_binary = platform_root / "bin" / executable_name
    _require_custodied_path(install, launcher_source, "installed npm launcher source")
    _require_custodied_path(install, platform_manifest, "installed platform manifest")
    _require_custodied_path(install, packaged_binary, "installed npm binary")
    _require_custodied_path(
        install, command, "installed npm launcher command", allow_leaf_symlink=True
    )
    launcher_bytes = module.safe_read(
        launcher_source, getattr(module, "MAX_MEMBER_BYTES", MAX_EVIDENCE_BYTES)
    )
    manifest_bytes = module.safe_read(
        platform_manifest, getattr(module, "MAX_EVIDENCE_BYTES", MAX_EVIDENCE_BYTES)
    )
    binary_bytes = module.safe_read(
        packaged_binary, getattr(module, "MAX_MEMBER_BYTES", MAX_EVIDENCE_BYTES)
    )
    npm_surface = require_object(surfaces.get("npm-launcher"), "npm launcher surface")
    command_identity = module.launcher_command_identity(command, launcher_source)
    launcher = require_object(raw.get("launcherResolution"), "launcher resolution")
    if (
        module.sha256_bytes(launcher_bytes) != npm_surface.get("launcherSourceSha256")
        or module.sha256_bytes(manifest_bytes) != launcher.get("platformManifestSha256")
        or module.sha256_bytes(binary_bytes) != npm_surface.get("binarySha256")
        or module.sha256_bytes(binary_bytes) != launcher.get("packagedBinarySha256")
        or command_identity != npm_surface.get("launcherCommandIdentity")
        or command_identity != launcher.get("launcherCommand")
    ):
        raise RehearsalError(
            "current npm launcher identity differs from benchmark evidence"
        )
    try:
        package_after = package.lstat()
        install_after = install.lstat()
    except OSError as error:
        raise RehearsalError(
            f"retained package/install roots changed: {error}"
        ) from error
    if (package_after.st_dev, package_after.st_ino) != (
        package_metadata.st_dev,
        package_metadata.st_ino,
    ) or (install_after.st_dev, install_after.st_ino) != (
        install_metadata.st_dev,
        install_metadata.st_ino,
    ):
        raise RehearsalError("retained package/install root identity changed")


def _validate_recorded_tool(record: Any, requested: str) -> bool | None:
    value = require_object(record, f"recorded {requested} tool")
    if value.get("status") == "unavailable":
        if value != {"requested": requested, "status": "unavailable"}:
            raise RehearsalError(
                f"recorded {requested} unavailable identity is malformed"
            )
        return None
    if (
        set(value)
        != {
            "requested",
            "status",
            "resolvedPath",
            "byteLength",
            "sha256",
        }
        or value.get("requested") != requested
        or value.get("status") != "resolved"
    ):
        raise RehearsalError(f"recorded {requested} tool identity is malformed")
    path = value.get("resolvedPath")
    length = value.get("byteLength")
    recorded_sha = require_sha(value.get("sha256"), f"recorded {requested} tool")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or isinstance(length, bool)
        or not isinstance(length, int)
        or length < 1
    ):
        raise RehearsalError(f"recorded {requested} tool identity is malformed")
    try:
        current = build_local.digest_file(Path(path).resolve(strict=True))
    except (OSError, build_local.LocalBuildError):
        return False
    return current["byteLength"] == length and current["sha256"] == recorded_sha


def _require_custodied_path(
    root: Path, path: Path, label: str, *, allow_leaf_symlink: bool = False
) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RehearsalError(f"{label} escapes its owned root") from error
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise RehearsalError(f"cannot inspect {label} custody: {error}") from error
        is_leaf = index == len(parts) - 1
        is_link = (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(current, "is_junction", lambda: False)())
            or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        )
        if is_link and not (is_leaf and allow_leaf_symlink):
            raise RehearsalError(f"{label} has a symlink or reparse ancestor")
        if not is_leaf and not stat.S_ISDIR(metadata.st_mode):
            raise RehearsalError(f"{label} ancestor is not a directory")


def verify_rehearsal(
    output: Path,
    *,
    benchmark_module: Any | None = None,
    conformance_module: Any | None = None,
) -> dict[str, Any]:
    root = _existing_output(output)
    _verify_root(root)
    expected_top = {
        MARKER_NAME,
        "package",
        "installed",
        *EVIDENCE_FILES,
        MANIFEST_NAME,
        CHECKSUM_NAME,
    }
    try:
        members = {path.name: path for path in root.path.iterdir()}
    except OSError as error:
        raise RehearsalError(f"cannot enumerate rehearsal output: {error}") from error
    if set(members) != expected_top:
        raise RehearsalError("rehearsal output top-level membership differs")
    for name in ("package", "installed"):
        metadata = members[name].lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RehearsalError(f"rehearsal {name} must be a non-symlink directory")
    if (
        _read_regular(members[MARKER_NAME], "rehearsal ownership marker")
        != MARKER_BYTES
    ):
        raise RehearsalError("rehearsal ownership marker differs")
    encoded = {
        name: _read_regular(members[name], f"rehearsal evidence {name}")
        for name in (*EVIDENCE_FILES, MANIFEST_NAME, CHECKSUM_NAME)
    }
    checksums = _parse_checksums(encoded[CHECKSUM_NAME])
    checksum_members = {*EVIDENCE_FILES, MANIFEST_NAME}
    if set(checksums) != checksum_members:
        raise RehearsalError("rehearsal checksum membership differs")
    if any(digest(encoded[name]) != expected for name, expected in checksums.items()):
        raise RehearsalError("rehearsal checksum digest differs")
    manifest = _json_no_duplicates(encoded[MANIFEST_NAME], "rehearsal manifest")
    require_exact(
        manifest,
        {"schema", "status", "files", "releaseEligible", "publicationAuthorized"},
        "rehearsal manifest",
    )
    expected_records = [
        {
            "path": name,
            "byteLength": len(encoded[name]),
            "sha256": digest(encoded[name]),
        }
        for name in EVIDENCE_FILES
    ]
    if (
        manifest.get("schema") != "openprose.local-release-rehearsal-manifest/1"
        or manifest.get("status") != "non-publishing-development-evidence"
        or manifest.get("files") != expected_records
        or manifest.get("releaseEligible") is not False
        or manifest.get("publicationAuthorized") is not False
        or encoded[MANIFEST_NAME] != canonical_json(manifest)
    ):
        raise RehearsalError("rehearsal manifest is not exact and closed")

    build = _json_no_duplicates(encoded["build-report.json"], "build report")
    raw = _json_no_duplicates(
        encoded["installed-package-raw.json"], "installed benchmark report"
    )
    analysis = _json_no_duplicates(
        encoded["installed-package-analysis.json"], "installed benchmark analysis"
    )
    conformance = _json_no_duplicates(
        encoded["installed-mechanical-conformance.json"],
        "mechanical conformance report",
    )
    summary = _json_no_duplicates(
        encoded["rehearsal-summary.json"], "rehearsal summary"
    )
    if canonical_json(build) != encoded["build-report.json"]:
        raise RehearsalError("build report is not canonically encoded")
    if canonical_json(summary) != encoded["rehearsal-summary.json"]:
        raise RehearsalError("rehearsal summary is not canonically encoded")
    module = (
        benchmark_module if benchmark_module is not None else load_installed_benchmark()
    )
    runner = (
        conformance_module
        if conformance_module is not None
        else load_conformance_runner()
    )
    if module.render_json(raw) != encoded["installed-package-raw.json"]:
        raise RehearsalError("installed benchmark report is not canonically encoded")
    reanalysis = module.analyse_report(raw)
    if module.render_json(reanalysis) != encoded["installed-package-analysis.json"]:
        raise RehearsalError("installed benchmark reanalysis differs byte-for-byte")
    if module.render_json(analysis) != encoded["installed-package-analysis.json"]:
        raise RehearsalError("installed benchmark analysis is not canonically encoded")
    bindings = validate_cross_bindings(build, raw, analysis)
    conformance_binding = validate_mechanical_conformance(
        conformance,
        encoded["installed-mechanical-conformance.json"],
        runner,
        raw,
        require_current_admission=False,
    )
    try:
        tree_digests = module.verify_retained_install_trees(
            root.path / "installed", raw
        )
    except Exception as error:
        if error.__class__.__name__ != "BenchmarkError":
            raise
        raise RehearsalError(str(error)) from error
    bindings["installedTreeDigests"] = tree_digests
    bindings["mechanicalConformance"] = conformance_binding
    require_exact(
        summary,
        {
            "schema",
            "status",
            "packageIdentity",
            "bindings",
            "surfaceTimingSummaries",
            "claims",
            "limitations",
            "policy",
            "timing",
            "generator",
        },
        "rehearsal summary",
    )
    claims = require_object(summary.get("claims"), "rehearsal claims")
    expected_claims = {
        "deterministicMockSelected": True,
        "providerCallMonitoring": "not-performed",
        "networkIsolation": "not-enforced",
        "detachedDescendantContainment": "not-enforced",
        "semanticConformance": False,
        "portabilityEvaluated": False,
        "releaseEligible": False,
        "publicationAuthorized": False,
        "rankingProduced": False,
        "nativeWindowsAdmission": False,
    }
    policy = require_object(summary.get("policy"), "rehearsal policy")
    measurement_plan = require_object(raw.get("measurementPlan"), "measurement plan")
    timing = require_object(summary.get("timing"), "rehearsal timing")
    generator = require_object(summary.get("generator"), "rehearsal generator")
    if (
        summary.get("schema") != SCHEMA
        or summary.get("status") != "passed-local-development-rehearsal"
        or summary.get("packageIdentity") != raw.get("packageIdentity")
        or summary.get("bindings") != bindings
        or summary.get("surfaceTimingSummaries")
        != analysis.get("surfaceTimingSummaries")
        or summary.get("limitations") != raw.get("limitations")
        or claims != expected_claims
        or set(policy) != {"trials", "timeoutSeconds", "budgetSeconds", "execution"}
        or isinstance(policy.get("trials"), bool)
        or not isinstance(policy.get("trials"), int)
        or not 1 <= policy["trials"] <= 20
        or any(
            isinstance(policy.get(name), bool)
            or not isinstance(policy.get(name), (int, float))
            or not math.isfinite(policy[name])
            or policy[name] <= 0
            for name in ("timeoutSeconds", "budgetSeconds")
        )
        or not 1 <= policy["timeoutSeconds"] <= 120
        or not MIN_BUDGET_SECONDS <= policy["budgetSeconds"] <= MAX_BUDGET_SECONDS
        or policy.get("execution") != "deterministic-provider-free-mock"
        or policy.get("trials") != measurement_plan.get("trials")
        or policy.get("timeoutSeconds") != measurement_plan.get("timeoutSeconds")
        or measurement_plan.get("deadlineApplied") is not True
        or set(timing) != {"elapsedSeconds", "measurement", "scope"}
        or isinstance(timing.get("elapsedSeconds"), bool)
        or not isinstance(timing.get("elapsedSeconds"), (int, float))
        or not math.isfinite(timing["elapsedSeconds"])
        or timing["elapsedSeconds"] < 0
        or timing["elapsedSeconds"] > policy["budgetSeconds"]
        or timing.get("measurement") != "variable-wall-clock"
        or timing.get("scope")
        != "through-analysis-and-tool-identity-before-evidence-write"
        or set(generator)
        != {
            "name",
            "version",
            "pythonVersion",
            "rehearsalSourceSha256",
            "builder",
            "benchmark",
            "conformance",
            "boundedExecutor",
            "buildTools",
            "benchmarkToolchain",
        }
        or generator.get("name") != "openprose-local-release-rehearsal"
        or type(generator.get("version")) is not int
        or generator.get("version") != 1
        or not isinstance(generator.get("pythonVersion"), str)
    ):
        raise RehearsalError("rehearsal summary is not exact and evidence-bound")
    recorded_rehearsal_sha = require_sha(
        generator.get("rehearsalSourceSha256"), "rehearsal source"
    )
    for name in ("builder", "benchmark", "conformance", "boundedExecutor"):
        identity = require_object(generator.get(name), f"generator {name}")
        if set(identity) != {"name", "sourceSha256"} or not isinstance(
            identity.get("name"), str
        ):
            raise RehearsalError(f"generator {name} identity is malformed")
        if identity.get("sourceSha256") is not None:
            require_sha(identity["sourceSha256"], f"generator {name} source")
    build_tools = require_object(generator.get("buildTools"), "generator build tools")
    if set(build_tools) != {"cargo", "bun", "python"}:
        raise RehearsalError("generator build tool inventory is not closed")
    tool_matches = {
        name: _validate_recorded_tool(build_tools[name], name)
        for name in ("cargo", "bun", "python")
    }
    if generator.get("benchmarkToolchain") != raw.get("toolchain", "unavailable"):
        raise RehearsalError("recorded benchmark toolchain differs from raw evidence")
    _verify_current_package_and_install(
        module, root.path / "package", root.path / "installed", raw
    )
    _conformance_inputs(root.path / "installed", raw)
    current_case_ids = _current_conformance_case_ids(runner)
    _verify_root(root)
    return {
        "schema": "openprose.local-release-rehearsal-verification/1",
        "status": "verified-local-development-rehearsal",
        "packageIdentity": raw["packageIdentity"],
        "manifestSha256": digest(encoded[MANIFEST_NAME]),
        "checksumSha256": digest(encoded[CHECKSUM_NAME]),
        "matchesCurrentVerifier": recorded_rehearsal_sha
        == digest(_read_regular(Path(__file__).resolve(), "rehearsal verifier source")),
        "recordedBuildToolsMatchCurrent": tool_matches,
        "mechanicalConformanceCurrentCorpusMatch": current_case_ids
        == conformance_binding["caseIds"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", nargs="?", choices=("run", "verify"), default="run")
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--trials", type=int, default=3)
    result.add_argument("--timeout-seconds", type=float, default=10.0)
    result.add_argument("--budget-seconds", type=float, default=1_200.0)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parser().parse_args(argv)
        summary = (
            verify_rehearsal(options.output)
            if options.command == "verify"
            else rehearse(
                options.output,
                trials=options.trials,
                timeout_seconds=options.timeout_seconds,
                budget_seconds=options.budget_seconds,
            )
        )
        sys.stdout.buffer.write(canonical_json(summary))
        return 0
    except (RehearsalError, build_local.LocalBuildError) as error:
        sys.stdout.buffer.write(
            canonical_json({"schema": ERROR_SCHEMA, "message": str(error)})
        )
        return 2
    except Exception as error:
        if error.__class__.__name__ == "BenchmarkError":
            sys.stdout.buffer.write(
                canonical_json({"schema": ERROR_SCHEMA, "message": str(error)})
            )
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
