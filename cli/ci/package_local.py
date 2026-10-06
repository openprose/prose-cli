#!/usr/bin/env python3
"""Create deterministic local CLI archives, npm packages, and honest evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli"
LAUNCHER = CLI / "bun" / "npm" / "bin" / "prose.js"
LICENSE = ROOT / "LICENSE"
HELLO_EXAMPLE = CLI / "conformance" / "live-alpha" / "hello.prose.md"
HELLO_EXAMPLE_MEMBER = "examples/hello.prose.md"
SENTINEL_IMAGE_MANIFEST = CLI / "shared" / "image" / "sentinel-v1" / "manifest.json"
DIAGNOSTIC_IMAGE_MANIFEST = CLI / "shared" / "image" / "echo-v0" / "manifest.json"
PUBLISHED_KERNEL_POLICY = {
    "schema": "openprose.published-kernel-policy/1",
    "resolution": "latest-published-on-run",
    "entrypoint": "https://pkg.prose.md/kernel.md",
    "pinning": "per-run",
}
FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY = (
    CLI / "shared" / "capabilities" / "adapters" / "functional-alpha.v1.json"
)
OMP_ADAPTER_RECIPE_AUTHORITY = (
    CLI / "shared" / "capabilities" / "adapters" / "recipes" / "omp-rpc.v1.json"
)
RUST_LOCK = CLI / "rust" / "Cargo.lock"
BUN_LOCK = CLI / "bun" / "bun.lock"
WINDOWS_HOST_NAME = "openprose-windows-process-host.exe"
SDK_NAME = "prose-agents-sdk"
SDK_RECEIPT = "agents-sdk-build.json"
SDK_NOTICES = "AGENTS-SDK-NOTICES.txt"
LINUX_MINIMUM_GLIBC = "2.34"
LINUX_EXECUTION_EVIDENCE = "ubuntu-22.04-only"
NODE_MINIMUM = "22.22.3"
NODE_ENGINE = f">={NODE_MINIMUM}"
NPM_COHORT_SCHEMA = "openprose.npm-cohort/1"
NPM_HOMEPAGE = "https://github.com/openprose/prose/tree/main/cli#readme"
NPM_BUGS = {
    "url": "https://github.com/openprose/prose/issues/new?template=openprose-cli-bug.yml"
}
SANITIZED_CLI_REPORT_URL = NPM_BUGS["url"]
HARNESS_MODEL_REQUEST_URL = "https://github.com/openprose/prose/issues/new?template=openprose-cli-harness-model.yml"
BENCHMARK_REQUEST_URL = (
    "https://github.com/openprose/prose/issues/new?"
    "template=openprose-cli-benchmark-profile.yml"
)
PRIVATE_VULNERABILITY_REPORT_URL = (
    "https://github.com/openprose/prose/security/advisories/new"
)
DEPENDENCY_EVIDENCE_SCRIPT = CLI / "ci" / "dependency_evidence.py"
MAX_DEPENDENCY_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
COMMAND_CLEANUP_SECONDS = 3.0
NOT_APPLICABLE_INTEGRITY_REASONS = {"local-source-package", "workspace-package"}
DEPENDENCY_SOURCE_PATHS = {
    "harnesses/agents-sdk/requirements-build.txt",
    "cli/bun/bun.lock",
    "cli/bun/package.json",
    "cli/platform/windows-process-host/Cargo.lock",
    "cli/platform/windows-process-host/Cargo.toml",
    "cli/rust/Cargo.lock",
    "cli/rust/Cargo.toml",
    "cli/rust/crates/prose-cli/Cargo.toml",
    "cli/rust/crates/prose-process-supervisor/Cargo.toml",
    "cli/rust/crates/prose-runner-core/Cargo.toml",
}

PLATFORMS: dict[str, dict[str, Any]] = {
    "darwin-arm64": {"os": ["darwin"], "cpu": ["arm64"]},
    "darwin-x64": {"os": ["darwin"], "cpu": ["x64"]},
    "linux-x64-gnu": {"os": ["linux"], "cpu": ["x64"], "libc": ["glibc"]},
    "linux-arm64-gnu": {"os": ["linux"], "cpu": ["arm64"], "libc": ["glibc"]},
    "win32-x64": {"os": ["win32"], "cpu": ["x64"]},
}

BUN_RUNTIME_BY_PLATFORM = {
    "darwin-arm64": {
        "compileTarget": "bun-darwin-arm64",
        "runtimeVariant": "native",
    },
    "darwin-x64": {
        "compileTarget": "bun-darwin-x64-baseline",
        "runtimeVariant": "baseline",
    },
    "linux-x64-gnu": {
        "compileTarget": "bun-linux-x64-baseline",
        "runtimeVariant": "baseline",
    },
    "linux-arm64-gnu": {
        "compileTarget": "bun-linux-arm64",
        "runtimeVariant": "native",
    },
    "win32-x64": {
        "compileTarget": "bun-windows-x64-baseline",
        "runtimeVariant": "baseline",
    },
}

POSIX_PUBLICATION_PLATFORMS = (
    "darwin-arm64", "darwin-x64", "linux-arm64-gnu", "linux-x64-gnu",
)

ALPHA_HARNESS_SUPPORT = {
    "darwin-arm64": ("prime", "omp", "codex", "claude"),
    "darwin-x64": ("codex",),
    "linux-x64-gnu": ("codex", "omp"),
    "linux-arm64-gnu": ("codex",),
}

SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_REVISION = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")
GLIBC_REQUIREMENT = re.compile(rb"\bName:\s+GLIBC_([0-9]+)\.([0-9]+)(?:\.([0-9]+))?\b")


class PackageError(Exception):
    """A deterministic user-facing packaging refusal."""


def _authority_json(path: Path, label: str) -> dict[str, Any]:
    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise PackageError(f"{label} contains a duplicate field: {key}")
            value[key] = item
        return value

    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise PackageError(f"{label} is unavailable: {error}") from error
    if len(encoded) > 1024 * 1024:
        raise PackageError(f"{label} exceeds its size bound")
    try:
        value = json.loads(encoded, object_pairs_hook=closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise PackageError(f"{label} must contain an object")
    return value


def omp_runtime_prerequisite() -> dict[str, str]:
    """Return the one closed OMP runtime prerequisite from shared authority."""

    manifest = _authority_json(
        FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY,
        "functional-alpha adapter authority",
    )
    adapters = manifest.get("adapters")
    if not isinstance(adapters, list):
        raise PackageError("functional-alpha adapter authority lacks adapters")
    matches = [
        item
        for item in adapters
        if isinstance(item, dict) and item.get("adapterId") == "omp/rpc"
    ]
    if len(matches) != 1:
        raise PackageError(
            "functional-alpha adapter authority must contain one OMP adapter"
        )
    omp = matches[0]
    prerequisites = omp.get("runtimePrerequisites")
    if not isinstance(prerequisites, list) or len(prerequisites) != 1:
        raise PackageError("OMP authority must contain one runtime prerequisite")
    prerequisite = prerequisites[0]
    expected = {
        "runtime": "bun",
        "versionRange": ">=1.3.14",
        "repairCommand": "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
    }
    if (
        prerequisite != expected
        or omp.get("repairCommand") != expected["repairCommand"]
    ):
        raise PackageError("OMP runtime prerequisite or repair authority differs")

    recipe = _authority_json(OMP_ADAPTER_RECIPE_AUTHORITY, "OMP adapter recipe")
    support = recipe.get("support")
    if (
        recipe.get("adapterId") != "omp/rpc"
        or not isinstance(support, dict)
        or support.get("runtimePrerequisites") != prerequisites
        or support.get("repairCommand") != expected["repairCommand"]
    ):
        raise PackageError(
            "OMP recipe runtime prerequisite differs from alpha authority"
        )
    return dict(expected)


def version_tuple(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", value) is None:
        raise PackageError(f"invalid dotted runtime version: {value!r}")
    parts = tuple(int(part) for part in value.split("."))
    if any(part > 1_000_000 for part in parts):
        raise PackageError("runtime version component exceeds the closed bound")
    return parts


def parse_required_glibc_maximum(encoded: bytes, implementation: str) -> str:
    versions = {
        tuple(int(part or b"0") for part in match.groups())
        for match in GLIBC_REQUIREMENT.finditer(encoded)
    }
    if not versions:
        raise PackageError(
            f"{implementation} ELF has no readable GLIBC version requirements"
        )
    maximum = max(versions)
    parts = maximum[:2] if maximum[2] == 0 else maximum
    return ".".join(str(part) for part in parts)


def inspect_linux_glibc(binary: Path, implementation: str, readelf: Path) -> str:
    completed = run_bounded(
        [str(readelf), "--version-info", "--wide", str(binary)],
        cwd=ROOT,
        environment={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
        },
        timeout_seconds=15,
        label=f"{implementation} ELF glibc inspection",
    )
    if completed.returncode != 0 or completed.stderr:
        raise PackageError(f"{implementation} ELF glibc inspection failed closed")
    required = parse_required_glibc_maximum(completed.stdout, implementation)
    if version_tuple(required) > version_tuple(LINUX_MINIMUM_GLIBC):
        raise PackageError(
            f"{implementation} ELF requires GLIBC_{required}, newer than the admitted "
            f"glibc {LINUX_MINIMUM_GLIBC} floor"
        )
    return required


def linux_runtime_record(
    rust_binary: Path,
    bun_binary: Path,
    readelf: Path,
    readelf_length: int,
    readelf_digest: str,
) -> dict[str, Any]:
    verified_snapshot_bytes(readelf, readelf_length, readelf_digest, "readelf")
    rust_required = inspect_linux_glibc(rust_binary, "rust", readelf)
    verified_snapshot_bytes(readelf, readelf_length, readelf_digest, "readelf")
    bun_required = inspect_linux_glibc(bun_binary, "bun", readelf)
    verified_snapshot_bytes(readelf, readelf_length, readelf_digest, "readelf")
    return {
        "minimumGlibc": LINUX_MINIMUM_GLIBC,
        "requiredGlibcMaximum": {
            "rust": rust_required,
            "bun": bun_required,
        },
        "executionEvidence": LINUX_EXECUTION_EVIDENCE,
    }


class BoundedCapture:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.data = bytearray()
        self.overflow = threading.Event()
        self.error: OSError | None = None

    def drain(self, stream: Any) -> None:
        try:
            while True:
                block = stream.read(65_536)
                if not block:
                    return
                remaining = self.maximum + 1 - len(self.data)
                if remaining > 0:
                    self.data.extend(block[:remaining])
                if len(self.data) > self.maximum or len(block) > remaining:
                    self.overflow.set()
        except OSError as error:
            self.error = error
        finally:
            try:
                stream.close()
            except OSError:
                pass


def _process_group_exists(group: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_process(process: subprocess.Popen[bytes], group: int) -> bool:
    if os.name == "nt":
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=COMMAND_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            return False
        return process.poll() is not None
    if group != process.pid or group == os.getpgrp():
        return False
    for sent_signal in (signal.SIGTERM, signal.SIGKILL):
        if not _process_group_exists(group):
            break
        try:
            os.killpg(group, sent_signal)
        except ProcessLookupError:
            break
        except PermissionError:
            return False
        deadline = time.monotonic() + COMMAND_CLEANUP_SECONDS
        while time.monotonic() < deadline and _process_group_exists(group):
            process.poll()
            time.sleep(0.01)
    try:
        process.wait(timeout=COMMAND_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return process.poll() is not None and not _process_group_exists(group)


def run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 600
    ):
        raise PackageError(
            f"{label} timeout must be finite and between 0 and 600 seconds"
        )
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        if os.name == "nt"
        else 0
    )
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
    except OSError as error:
        raise PackageError(f"cannot start {label}: {error}") from error
    group = process.pid
    stdout = BoundedCapture(MAX_COMMAND_OUTPUT_BYTES)
    stderr = BoundedCapture(MAX_COMMAND_OUTPUT_BYTES)
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()
    failure: str | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        while (
            process.poll() is None
            and not stdout.overflow.is_set()
            and not stderr.overflow.is_set()
        ):
            if time.monotonic() >= deadline:
                failure = "timed out"
                break
            time.sleep(0.01)
        if stdout.overflow.is_set() or stderr.overflow.is_set():
            failure = "output exceeded the closed limit"
        remaining = max(0.0, deadline - time.monotonic())
        for reader in readers:
            reader.join(timeout=min(COMMAND_CLEANUP_SECONDS, remaining))
        if any(reader.is_alive() for reader in readers):
            failure = failure or "output streams remained unsettled"
        if os.name != "nt" and _process_group_exists(group):
            failure = failure or "original process group remained unsettled"
        if failure is not None:
            if not _cleanup_process(process, group):
                raise PackageError(f"{label} {failure}; cleanup could not be verified")
            for reader in readers:
                reader.join(timeout=COMMAND_CLEANUP_SECONDS)
            raise PackageError(f"{label} {failure}")
        if stdout.error is not None or stderr.error is not None:
            raise PackageError(f"{label} output could not be read")
        return subprocess.CompletedProcess(
            argv, process.returncode, bytes(stdout.data), bytes(stderr.data)
        )
    except BaseException:
        if process.poll() is None or (os.name != "nt" and _process_group_exists(group)):
            _cleanup_process(process, group)
        raise


def valid_semver(value: str) -> bool:
    """Return whether value is an exact SemVer 2.0.0 version string."""

    return SEMVER.fullmatch(value) is not None


def pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_image_manifest(path: Path) -> tuple[dict[str, Any], str]:
    """Read and validate the package-bound image identity exactly once."""

    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise PackageError(f"cannot read --image-manifest: {error}") from error
    try:
        decoded = encoded.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError(f"cannot read --image-manifest: {error}") from error
    if not isinstance(value, dict):
        raise PackageError("--image-manifest must contain a JSON object")
    if value.get("schema") != "openprose.skill-runtime-image-manifest/1":
        raise PackageError("--image-manifest has an unsupported schema")
    for field in ("imageFormatVersion", "imageVersion"):
        observed = value.get(field)
        if not isinstance(observed, str) or not observed:
            raise PackageError(f"--image-manifest {field} must be a non-empty string")
    release_eligible = value.get("releaseEligible")
    if not isinstance(release_eligible, bool):
        raise PackageError("--image-manifest releaseEligible must be a boolean")
    purpose = value.get("purpose")
    if purpose not in {
        "canonical-language-runtime",
        "functional-alpha-placeholder",
        "sentinel-transport-test",
    }:
        raise PackageError("--image-manifest purpose is unsupported")
    aggregate = value.get("aggregateSha256")
    if not isinstance(aggregate, dict):
        raise PackageError("--image-manifest aggregateSha256 must be an object")
    if aggregate.get("algorithm") != "sha256-path-length-nul-v1":
        raise PackageError("--image-manifest aggregateSha256.algorithm is unsupported")
    digest = aggregate.get("sha256")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise PackageError(
            "--image-manifest aggregateSha256.sha256 must be a lowercase SHA-256 digest"
        )
    return value, sha256_bytes(encoded)


def image_identity(image: dict[str, Any], manifest_sha256: str) -> dict[str, Any]:
    return {
        "formatVersion": image["imageFormatVersion"],
        "version": image["imageVersion"],
        "sha256": image["aggregateSha256"]["sha256"],
        "manifestSha256": manifest_sha256,
        "purpose": image["purpose"],
        "releaseEligible": image["releaseEligible"],
    }


def npm_payload_version(version: str, platform_identifier: str) -> str:
    # Prerelease payloads must sort below the root, including caret RC ranges.
    core, separator, prerelease = version.partition("-")
    return (f"{core}-0.{prerelease}-{platform_identifier}" if separator
            else f"{version}-{platform_identifier}")


def npm_cohort(
    *, mode: str, version: str, source_revision: str, image: dict[str, Any],
    publication_platforms: str | None = None,
) -> dict[str, Any]:
    channels = {
        "development": "development",
        "alpha": "functional-alpha",
        "release": "release-candidate",
        "kernel-rc": "kernel-release-candidate",
    }
    if mode not in channels:
        raise PackageError("npm cohort mode is unsupported")
    admitted_platforms = (
        sorted(ALPHA_HARNESS_SUPPORT) if mode == "alpha" else sorted(PLATFORMS)
    )
    if mode == "kernel-rc" and publication_platforms != "posix-four":
        raise PackageError("kernel-rc packaging requires --publication-platforms posix-four")
    if publication_platforms is not None:
        if publication_platforms != "posix-four":
            raise PackageError("unsupported publication platform set")
        if mode not in {"release", "kernel-rc"} or re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc\.(0|[1-9][0-9]*)",
            version,
        ) is None:
            raise PackageError("posix-four publication packaging requires release mode and an explicit RC version")
        if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
            raise PackageError("posix-four publication packaging requires an exact source commit")
        if mode == "release" and (image.get("purpose") != "canonical-language-runtime" or image.get("releaseEligible") is not True):
            raise PackageError("posix-four publication packaging requires a release-eligible canonical image")
        if mode == "kernel-rc":
            diagnostic, diagnostic_manifest_sha = read_image_manifest(DIAGNOSTIC_IMAGE_MANIFEST)
            if image != image_identity(diagnostic, diagnostic_manifest_sha):
                raise PackageError("kernel-rc requires the exact diagnostic echo image identity")
        admitted_platforms = list(POSIX_PUBLICATION_PLATFORMS)
    if mode == "kernel-rc":
        return {
            "schema": "openprose.npm-cohort/3",
            "version": version,
            "sourceRevision": source_revision,
            "releaseChannel": "kernel-release-candidate",
            "purpose": "published-kernel-loader",
            "imageSource": "published-on-run",
            "embeddedDiagnosticImage": {key: value for key, value in image.items() if key != "releaseEligible"},
            "kernelPolicy": dict(PUBLISHED_KERNEL_POLICY),
            "admittedPlatforms": admitted_platforms,
            "semanticStatus": "unverified",
            "releaseEligible": False,
            "publicationAuthorized": False,
        }
    return {
        "schema": NPM_COHORT_SCHEMA,
        "version": version,
        "sourceRevision": source_revision,
        "releaseChannel": channels[mode],
        "purpose": image["purpose"],
        "image": image,
        "admittedPlatforms": admitted_platforms,
        "semanticStatus": "not-applicable" if mode == "alpha" else "unverified",
        "releaseEligible": False,
        "publicationAuthorized": False,
    }


def snapshot_binary(
    source: Path, destination: Path, implementation: str
) -> tuple[Path, int, str]:
    """Copy a binary from one opened descriptor into owned package staging."""

    try:
        metadata = source.lstat()
    except OSError as error:
        raise PackageError(
            f"cannot inspect {implementation} binary {source}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise PackageError(
            f"{implementation} binary must be a non-symlink non-empty regular file: {source}"
        )
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise PackageError(
            f"{implementation} binary must be a non-empty regular file: {source}"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    byte_length = 0
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as opened, destination.open(
            "xb"
        ) as snapshot:
            opened_metadata = os.fstat(opened.fileno())
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_size <= 0
            ):
                raise PackageError(
                    f"{implementation} binary must be a non-empty regular file: {source}"
                )
            if (metadata.st_dev, metadata.st_ino) != (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
            ):
                raise PackageError(
                    f"{implementation} binary changed while it was opened: {source}"
                )
            for block in iter(lambda: opened.read(1024 * 1024), b""):
                snapshot.write(block)
                digest.update(block)
                byte_length += len(block)
            settled_metadata = os.fstat(opened.fileno())
    except PackageError:
        raise
    except OSError as error:
        raise PackageError(
            f"cannot snapshot {implementation} binary {source}: {error}"
        ) from error
    if (
        byte_length != opened_metadata.st_size
        or opened_metadata.st_size != settled_metadata.st_size
        or opened_metadata.st_mtime_ns != settled_metadata.st_mtime_ns
    ):
        raise PackageError(
            f"{implementation} binary changed while it was snapshotted: {source}"
        )
    destination.chmod(0o500)
    return destination, byte_length, digest.hexdigest()


def snapshot_executable_tool(
    source: Path, destination: Path, name: str
) -> tuple[Path, int, str]:
    """Copy one explicit executable tool into private owned staging."""

    if not source.is_absolute() or ".." in source.parts:
        raise PackageError(f"--{name} must be an absolute executable path")
    current = Path(source.anchor)
    for part in source.parts[1:-1]:
        current /= part
        try:
            parent = current.lstat()
        except OSError as error:
            raise PackageError(
                f"cannot inspect {name} executable ancestor {current}: {error}"
            ) from error
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise PackageError(
                f"--{name} must not traverse a symlink or non-directory ancestor: "
                f"{current}"
            )
    try:
        metadata = source.lstat()
    except OSError as error:
        raise PackageError(
            f"cannot inspect {name} executable {source}: {error}"
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_mode & 0o111 == 0
    ):
        raise PackageError(
            f"--{name} must be a direct non-symlink executable regular file: {source}"
        )
    return snapshot_binary(source, destination, name)


def verified_snapshot_bytes(
    path: Path, byte_length: int, digest: str, implementation: str
) -> bytes:
    """Seal a verified filesystem snapshot into immutable package input bytes."""

    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise PackageError(
            f"cannot verify owned {implementation} snapshot: {error}"
        ) from error
    if len(encoded) != byte_length or sha256_bytes(encoded) != digest:
        raise PackageError(
            f"owned {implementation} snapshot changed during verification"
        )
    return encoded


def read_static_asset(path: Path, label: str, maximum: int = 64 * 1024) -> bytes:
    """Read one bounded repository asset without following a link or accepting drift."""

    try:
        before = path.lstat()
    except OSError as error:
        raise PackageError(f"cannot inspect {label}: {error}") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > maximum
    ):
        raise PackageError(f"{label} must be a bounded non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as opened:
            opened_metadata = os.fstat(opened.fileno())
            if (before.st_dev, before.st_ino) != (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
            ):
                raise PackageError(f"{label} changed before open")
            encoded = opened.read(maximum + 1)
            settled = os.fstat(opened.fileno())
    except PackageError:
        raise
    except OSError as error:
        raise PackageError(f"cannot read {label}: {error}") from error
    if (
        len(encoded) != opened_metadata.st_size
        or len(encoded) > maximum
        or opened_metadata.st_size != settled.st_size
        or opened_metadata.st_mtime_ns != settled.st_mtime_ns
    ):
        raise PackageError(f"{label} changed while it was read")
    return encoded


def current_platform_id() -> str:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(
        platform.system()
    )
    machine = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "AMD64": "x64",
    }.get(platform.machine())
    if system is None or machine is None:
        raise PackageError(
            f"unsupported packaging host: {platform.system()}-{platform.machine()}"
        )
    identifier = f"{system}-{machine}"
    if system == "linux":
        libc_name, _ = platform.libc_ver()
        identifier += "-gnu" if libc_name == "glibc" else "-musl"
    if identifier not in PLATFORMS:
        raise PackageError(
            f"platform {identifier} has no admitted local package target; supported: {', '.join(PLATFORMS)}"
        )
    return identifier


def validated_archive_members(
    members: Iterable[tuple[str, bytes, int]],
) -> list[tuple[str, bytes, int]]:
    """Close archive names, modes, and case-insensitive identity before writing."""

    result: list[tuple[str, bytes, int]] = []
    identities: set[str] = set()
    for record in members:
        if not isinstance(record, tuple) or len(record) != 3:
            raise PackageError("archive member record is malformed")
        name, data, mode = record
        if not isinstance(name, str):
            raise PackageError("archive member name is malformed")
        pure = PurePosixPath(name)
        if (
            not name
            or pure.is_absolute()
            or "\\" in name
            or "\x00" in name
            or any(part in {"", ".", ".."} for part in pure.parts)
            or pure.as_posix() != name
        ):
            raise PackageError(f"unsafe archive member name: {name!r}")
        identity = name.casefold()
        if identity in identities:
            raise PackageError(f"duplicate archive member identity: {name!r}")
        if not isinstance(data, bytes):
            raise PackageError(f"archive member is not immutable bytes: {name!r}")
        if mode not in {0o644, 0o755}:
            raise PackageError(f"archive member mode is unsupported: {name!r}")
        identities.add(identity)
        result.append((name, data, mode))
    return sorted(result, key=lambda item: item[0])


def tar_gz(path: Path, members: Iterable[tuple[str, bytes, int]], epoch: int) -> None:
    closed_members = validated_archive_members(members)
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9
        ) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                for name, data, mode in closed_members:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = mode
                    info.mtime = epoch
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, io.BytesIO(data))


def package_status(mode: str) -> tuple[str, str]:
    if mode == "kernel-rc":
        return (
            "kernel release candidate",
            "This unsigned release candidate resolves the latest published kernel at "
            "https://pkg.prose.md/kernel.md on each run and holds its verified selection "
            "for that run. The embedded echo-v0 image is diagnostic data, not the "
            "kernel executed by a real harness. This release is not Developer ID "
            "signed or notarized; macOS binaries may have ad-hoc signatures. "
            "Review the release qualification evidence before use; this is not a "
            "claim of general program or model conformance.",
        )
    if mode == "alpha":
        return (
            "functional alpha",
            "The bundled echo-v0 image asks the selected harness to echo an opaque "
            "task argument vector. The human-output safety policy may withhold "
            "task-bearing text. This build does not execute OpenProse programs or "
            "claim semantic conformance.",
        )
    if mode == "development":
        return (
            "local development",
            "This local development artifact may contain test seams or a sentinel image. "
            "Do not redistribute it as a release.",
        )
    if mode == "release":
        return (
            "full-release candidate",
            "This is a candidate, not publication authority. Verify the release manifest, "
            "checksums, provenance, SBOM, and promotion attestation before distribution.",
        )
    raise PackageError(f"unsupported package mode for README generation: {mode}")


ALPHA_HARNESS_DISPLAY_NAMES = {
    "prime": "Prime",
    "omp": "OMP",
    "codex": "Codex",
    "claude": "Claude",
}
ALPHA_VERSION_GUIDANCE = {
    "prime": "Prime: exact admitted versions are 0.7.0 and 0.8.1.",
    "omp": "OMP: exact admitted version is 18.0.9. It requires Bun 1.3.14 or newer.",
    "codex": "Codex: exact admitted version is 0.149.0-alpha.4.1.",
    "claude": "Claude: exact admitted version is 2.1.243.",
}
PROVIDER_CHARGE_BOUNDARY = (
    "The run command contacts the selected provider and may incur charges under "
    "the signed-in account. The CLI cannot determine the account or billing route."
)


def alpha_support_guidance(*, markdown: bool) -> str:
    if markdown:
        return (
            "## Support and security\n\n"
            "Report a sanitized CLI problem through the "
            f"[OpenProse CLI bug or install problem]({SANITIZED_CLI_REPORT_URL}) "
            "form. Request a new harness or model route through the "
            f"[harness or model request form]({HARNESS_MODEL_REQUEST_URL}). "
            "Propose a benchmark profile or cell through the "
            f"[benchmark proposal form]({BENCHMARK_REQUEST_URL}). "
            "Report a suspected vulnerability privately through "
            f"[GitHub private vulnerability reporting]({PRIVATE_VULNERABILITY_REPORT_URL}).\n\n"
            "Do not include credentials, account identifiers, private paths, or raw "
            "provider output in a public report.\n\n"
        )
    return (
        "Support and security:\n"
        f"  Report a sanitized CLI problem: {SANITIZED_CLI_REPORT_URL}\n"
        "  Request a new harness or model route: "
        f"{HARNESS_MODEL_REQUEST_URL}\n"
        "  Propose a benchmark profile or cell: "
        f"{BENCHMARK_REQUEST_URL}\n"
        "  Report a suspected vulnerability privately: "
        f"{PRIVATE_VULNERABILITY_REPORT_URL}\n"
        "  Do not include credentials, account identifiers, private paths, or raw "
        "provider output in a public report.\n\n"
    )


def format_harness_names(harnesses: Iterable[str]) -> str:
    names = [ALPHA_HARNESS_DISPLAY_NAMES[harness] for harness in harnesses]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def alpha_version_guidance(harnesses: Iterable[str] | None = None) -> str:
    selected = tuple(
        ALPHA_HARNESS_SUPPORT["darwin-arm64"] if harnesses is None else harnesses
    )
    if "omp" in selected:
        # Reauthenticate the structured manifest/recipe authority before rendering
        # its Standard Technical English form.
        omp_runtime_prerequisite()
    lines = ["Functional-alpha harness versions:"]
    lines.extend(f"  {ALPHA_VERSION_GUIDANCE[harness]}" for harness in selected)
    return "\n".join(lines) + "\n"


ALPHA_JOURNEY_HEADINGS = {
    "prime": "Prime — macOS Apple silicon only:",
    "omp": "OMP — macOS Apple silicon or Linux x64 only:",
    "codex": "Codex — every supported functional-alpha platform:",
    "claude": "Claude — macOS Apple silicon only:",
}
ALPHA_JOURNEY_INSTALLS = {
    "prime": (
        "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh "
        "| sh -s -- 0.8.1"
    ),
    "omp": ("npm install --global bun@1.3.14 " "@oh-my-pi/pi-coding-agent@18.0.9"),
    "codex": "npm install --global @openai/codex@0.149.0-alpha.4.1",
    "claude": "npm install --global @anthropic-ai/claude-code@2.1.243",
}
ALPHA_AUTHENTICATION_BOUNDARY = (
    "Authentication is not automated. Complete the harness sign-in flow before "
    "you run `cli doctor` or `run`."
)
ALPHA_SIGN_IN_GUIDANCE = {
    "prime": (
        "After installation, start the installed `prime-agent` harness separately, "
        "complete its own interactive sign-in or configuration, and then exit it. "
        "OpenProse CLI never invokes or controls that TUI."
    ),
    "omp": (
        "After installation, start the installed `omp` harness separately, complete "
        "its own interactive sign-in or configuration, and then exit it. OpenProse "
        "CLI never invokes or controls that TUI."
    ),
    "codex": "After installation, complete Codex sign-in before selecting it.",
    "claude": "After installation, complete Claude sign-in before selecting it.",
}
PRIME_INSTALL_PROVENANCE_BOUNDARY = (
    "The Prime curl-to-shell command is upstream installation convenience, not "
    "binary provenance; assess the installed harness with the applicable upstream "
    "release evidence."
)


def alpha_harness_journeys(
    harnesses: Iterable[str],
    prose_command: str,
    example_command: str,
    *,
    markdown: bool,
) -> str:
    harnesses = tuple(harnesses)
    omp_prerequisite = omp_runtime_prerequisite() if "omp" in harnesses else None
    lines: list[str] = []
    heading_indent = "" if markdown else "  "
    text_indent = "" if markdown else "  "
    command_indent = "    " if markdown else "  "
    for harness in harnesses:
        if lines and markdown:
            lines.append("")
        lines.append(f"{heading_indent}{ALPHA_JOURNEY_HEADINGS[harness]}")
        if markdown:
            lines.append("")
        lines.append(f"{text_indent}{ALPHA_AUTHENTICATION_BOUNDARY}")
        lines.append(f"{text_indent}{ALPHA_SIGN_IN_GUIDANCE[harness]}")
        if harness == "prime":
            lines.append(f"{text_indent}{PRIME_INSTALL_PROVENANCE_BOUNDARY}")
        lines.append(f"{text_indent}{PROVIDER_CHARGE_BOUNDARY}")
        if markdown:
            lines.append("")
        install = ALPHA_JOURNEY_INSTALLS[harness]
        if harness == "omp":
            assert omp_prerequisite is not None
            install = str(omp_prerequisite["repairCommand"])
        lines.append(f"{command_indent}{install}")
        if harness == "codex":
            lines.append(f"{command_indent}codex login")
        elif harness == "claude":
            lines.append(f"{command_indent}claude auth login")
        selection = f"{command_indent}{prose_command} cli harness use {harness}"
        if harness in {"prime", "omp"}:
            selection += (
                " --model openai-codex/gpt-5.4 "
                f"--auth-profile {harness}-harness-login"
            )
        lines.extend(
            [
                selection,
                f"{command_indent}{prose_command} cli doctor",
                f"{command_indent}{prose_command} run {example_command}",
            ]
        )
    return "\n".join(lines) + ("\n" if lines else "")


def alpha_repair_commands(
    platform_identifier: str,
    prose_command: str,
    example_command: str,
) -> str:
    harnesses = ALPHA_HARNESS_SUPPORT.get(platform_identifier, ())
    return alpha_harness_journeys(
        harnesses, prose_command, example_command, markdown=False
    )


def darwin_gatekeeper_guidance(executable_path: str) -> str:
    return (
        "\nmacOS trust boundary:\n"
        "  This alpha executable is ad-hoc signed and not notarized. It is not a "
        "Developer-signed distribution.\n"
        "  Verify SHA256SUMS first. If Gatekeeper then quarantines the executable, "
        "inspect it and remove only that file's quarantine attribute:\n"
        f"  xattr -d com.apple.quarantine {executable_path}\n"
    )


ALPHA_PLATFORM_RESOLUTION_LINES = (
    "PLATFORM_ID=",
    "PLATFORM_ARCH=",
    'case "$(uname -s):$(uname -m)" in',
    "  Linux:x86_64) PLATFORM_ARCH=linux-x64 ;;",
    "  Linux:aarch64|Linux:arm64) PLATFORM_ARCH=linux-arm64 ;;",
    "  Darwin:arm64) PLATFORM_ID=darwin-arm64 ;;",
    "  Darwin:x86_64) PLATFORM_ID=darwin-x64 ;;",
    '  *) echo "unsupported functional-alpha platform" >&2; exit 1 ;;',
    "esac",
    'if [ -n "$PLATFORM_ARCH" ]; then',
    '  GLIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null) || { echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1; }',
    '  case "$GLIBC_VERSION" in',
    '    "glibc "[0-9]*.[0-9]*) ;;',
    '    *) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;',
    "  esac",
    "  GLIBC_NUMBER=${GLIBC_VERSION#glibc }",
    '  case "$GLIBC_NUMBER" in',
    '    *[!0-9.]*|.*|*.|*.*.*) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;',
    "  esac",
    '  PLATFORM_ID="$PLATFORM_ARCH-gnu"',
    "fi",
)


def alpha_platform_resolution_shell(indent: str = "") -> str:
    """Render one self-contained, fail-closed functional-alpha platform probe."""

    if any(character != " " for character in indent):
        raise PackageError("platform-resolution indentation must contain only spaces")
    return (
        "\n".join(f"{indent}{line}" for line in ALPHA_PLATFORM_RESOLUTION_LINES)
        + "\n\n"
    )


def standalone_uninstall_shell(
    root_name: str, executable: str, indent: str = ""
) -> str:
    """Render a fail-closed exact-root standalone uninstall block."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", root_name) is None:
        raise PackageError("standalone uninstall root name is not shell-safe")
    if executable not in {"prose", "prose.exe"}:
        raise PackageError("standalone uninstall executable is unsupported")
    if any(character != " " for character in indent):
        raise PackageError("standalone uninstall indentation must contain only spaces")
    refusal = 'echo "refusing standalone uninstall" >&2; exit 1'
    lines = (
        "UNINSTALL_ROOT=",
        f'printf "%s" "Absolute physical extracted-root path ending in {root_name}: " >&2',
        f"IFS= read -r UNINSTALL_ROOT || {{ {refusal}; }}",
        'case "$UNINSTALL_ROOT" in',
        "  /*) ;;",
        f"  *) {refusal} ;;",
        "esac",
        f'test "$UNINSTALL_ROOT" != "/" || {{ {refusal}; }}',
        f'test "${{UNINSTALL_ROOT##*/}}" = "{root_name}" || {{ {refusal}; }}',
        f'test -d "$UNINSTALL_ROOT" && test ! -L "$UNINSTALL_ROOT" || {{ {refusal}; }}',
        f'UNINSTALL_CANONICAL=$(CDPATH= cd -P "$UNINSTALL_ROOT" 2>/dev/null && pwd -P) || {{ {refusal}; }}',
        f'test "$UNINSTALL_CANONICAL" = "$UNINSTALL_ROOT" || {{ {refusal}; }}',
        f'test -f "$UNINSTALL_ROOT/README.txt" && test ! -L "$UNINSTALL_ROOT/README.txt" || {{ {refusal}; }}',
        f'test -f "$UNINSTALL_ROOT/{executable}" && test ! -L "$UNINSTALL_ROOT/{executable}" && test -x "$UNINSTALL_ROOT/{executable}" || {{ {refusal}; }}',
        f'test -d "$UNINSTALL_ROOT/examples" && test ! -L "$UNINSTALL_ROOT/examples" || {{ {refusal}; }}',
        f'test -f "$UNINSTALL_ROOT/{HELLO_EXAMPLE_MEMBER}" && test ! -L "$UNINSTALL_ROOT/{HELLO_EXAMPLE_MEMBER}" || {{ {refusal}; }}',
        'rm -rf -- "$UNINSTALL_ROOT"',
    )
    rendered = [f"{indent}/bin/sh -c '"]
    rendered.extend(f"{indent}{line}" for line in lines)
    rendered.append(f"{indent}'")
    return "\n".join(rendered) + "\n"


def standalone_repair_shell(
    *,
    archive_name: str,
    root_name: str,
    executable: str,
    version: str,
    implementation: str,
    indent: str = "",
) -> str:
    """Render a checksum-bound same-version repair into a fresh exact root."""

    safe_member = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
    if safe_member.fullmatch(archive_name) is None or not archive_name.endswith(
        ".tar.gz"
    ):
        raise PackageError("standalone repair archive name is not shell-safe")
    if safe_member.fullmatch(root_name) is None:
        raise PackageError("standalone repair root name is not shell-safe")
    if executable not in {"prose", "prose.exe"}:
        raise PackageError("standalone repair executable is unsupported")
    if implementation not in {"rust", "bun"}:
        raise PackageError("standalone repair implementation is unsupported")
    if SEMVER.fullmatch(version) is None:
        raise PackageError("standalone repair version is invalid")
    if any(character != " " for character in indent):
        raise PackageError("standalone repair indentation must contain only spaces")
    refusal = 'echo "refusing standalone repair" >&2; exit 1'
    lines = (
        f'ASSET="{archive_name}"',
        f'test -f "$ASSET" && test ! -L "$ASSET" || {{ {refusal}; }}',
        f"test -f SHA256SUMS && test ! -L SHA256SUMS || {{ {refusal}; }}",
        'REPAIR_MATCHES=$(awk -v name="$ASSET" "\\$2 == name { n++ } END { print n+0 }" SHA256SUMS)'
        f" || {{ {refusal}; }}",
        f'test "$REPAIR_MATCHES" -eq 1 || {{ {refusal}; }}',
        'REPAIR_CHECKSUM=$(awk -v name="$ASSET" "\\$2 == name { print }" SHA256SUMS)'
        f" || {{ {refusal}; }}",
        "if command -v sha256sum >/dev/null 2>&1; then",
        f'  printf "%s\\n" "$REPAIR_CHECKSUM" | sha256sum -c - || {{ {refusal}; }}',
        "elif command -v shasum >/dev/null 2>&1; then",
        f'  printf "%s\\n" "$REPAIR_CHECKSUM" | shasum -a 256 -c - || {{ {refusal}; }}',
        "else",
        f"  {refusal}",
        "fi",
        "REPAIR_PARENT=",
        'printf "%s" "Absolute new repair parent (must not already exist): " >&2',
        f"IFS= read -r REPAIR_PARENT || {{ {refusal}; }}",
        "if test ! -t 0; then",
        "  REPAIR_EXTRA=",
        f'  if IFS= read -r REPAIR_EXTRA || test -n "$REPAIR_EXTRA"; then {refusal}; fi',
        "fi",
        'case "$REPAIR_PARENT" in',
        "  [/]*) ;;",
        f"  *) {refusal} ;;",
        "esac",
        f'test "$REPAIR_PARENT" != "/" || {{ {refusal}; }}',
        f'case "$REPAIR_PARENT" in */) {refusal} ;; esac',
        f'case "$REPAIR_PARENT" in *[![:print:]]*) {refusal} ;; esac',
        f'test ! -e "$REPAIR_PARENT" && test ! -L "$REPAIR_PARENT" || {{ {refusal}; }}',
        "REPAIR_LEAF=${REPAIR_PARENT##*/}",
        f'test -n "$REPAIR_LEAF" && test "$REPAIR_LEAF" != "." && test "$REPAIR_LEAF" != ".." || {{ {refusal}; }}',
        "REPAIR_DIRECTORY=${REPAIR_PARENT%/*}",
        'test -n "$REPAIR_DIRECTORY" || REPAIR_DIRECTORY=/',
        f'test -d "$REPAIR_DIRECTORY" && test ! -L "$REPAIR_DIRECTORY" || {{ {refusal}; }}',
        'REPAIR_DIRECTORY_CANONICAL=$(CDPATH= cd -P "$REPAIR_DIRECTORY" 2>/dev/null && pwd -P)'
        f" || {{ {refusal}; }}",
        "REPAIR_CANONICAL=",
        'if test "$REPAIR_DIRECTORY_CANONICAL" = "/"; then',
        '  REPAIR_CANONICAL="/$REPAIR_LEAF"',
        "else",
        '  REPAIR_CANONICAL="$REPAIR_DIRECTORY_CANONICAL/$REPAIR_LEAF"',
        "fi",
        f'test "$REPAIR_CANONICAL" = "$REPAIR_PARENT" || {{ {refusal}; }}',
        "umask 077",
        f'mkdir -- "$REPAIR_PARENT" || {{ {refusal}; }}',
        f'test -d "$REPAIR_PARENT" && test ! -L "$REPAIR_PARENT" || {{ {refusal}; }}',
        'REPAIR_CREATED_CANONICAL=$(CDPATH= cd -P "$REPAIR_PARENT" 2>/dev/null && pwd -P)'
        f" || {{ {refusal}; }}",
        f'test "$REPAIR_CREATED_CANONICAL" = "$REPAIR_PARENT" || {{ {refusal}; }}',
        f'tar -xzf "$ASSET" -C "$REPAIR_PARENT" || {{ {refusal}; }}',
        f'REPAIRED_ROOT="$REPAIR_PARENT/{root_name}"',
        f'test -d "$REPAIRED_ROOT" && test ! -L "$REPAIRED_ROOT" || {{ {refusal}; }}',
        f'test -f "$REPAIRED_ROOT/{executable}" && test ! -L "$REPAIRED_ROOT/{executable}" && test -x "$REPAIRED_ROOT/{executable}" || {{ {refusal}; }}',
        f'test -f "$REPAIRED_ROOT/README.txt" && test ! -L "$REPAIRED_ROOT/README.txt" || {{ {refusal}; }}',
        f'test -f "$REPAIRED_ROOT/LICENSE" && test ! -L "$REPAIRED_ROOT/LICENSE" || {{ {refusal}; }}',
        f'test -d "$REPAIRED_ROOT/examples" && test ! -L "$REPAIRED_ROOT/examples" || {{ {refusal}; }}',
        f'test -f "$REPAIRED_ROOT/{HELLO_EXAMPLE_MEMBER}" && test ! -L "$REPAIRED_ROOT/{HELLO_EXAMPLE_MEMBER}" || {{ {refusal}; }}',
        f'REPAIRED_VERSION=$("$REPAIRED_ROOT/{executable}" --version) || {{ {refusal}; }}',
        f'test "$REPAIRED_VERSION" = "prose {version} ({implementation})" || {{ {refusal}; }}',
        f'printf "Same-version repair complete. Invoke: %s\\n" "$REPAIRED_ROOT/{executable}"',
    )
    rendered = [f"{indent}/bin/sh -c '"]
    rendered.extend(f"{indent}{line}" for line in lines)
    rendered.append(f"{indent}'")
    return "\n".join(rendered) + "\n"


def npm_alpha_upgrade_shell(indent: str = "") -> str:
    """Render a strict, prompt-driven functional-alpha npm upgrade block."""

    if any(character != " " for character in indent):
        raise PackageError("npm upgrade indentation must contain only spaces")
    refusal = 'echo "refusing npm functional-alpha upgrade" >&2; exit 1'
    lines = (
        "NEW_VERSION=",
        'printf "%s" "Exact newer functional-alpha version (X.Y.Z-alpha.N): " >&2',
        f"IFS= read -r NEW_VERSION || {{ {refusal}; }}",
        "NEW_CORE=${NEW_VERSION%-alpha.*}",
        "NEW_ALPHA_NUMBER=${NEW_VERSION##*-alpha.}",
        f'test "$NEW_CORE-alpha.$NEW_ALPHA_NUMBER" = "$NEW_VERSION" || {{ {refusal}; }}',
        "NEW_MAJOR=${NEW_CORE%%.*}",
        "NEW_REMAINDER=${NEW_CORE#*.}",
        f'test "$NEW_REMAINDER" != "$NEW_CORE" || {{ {refusal}; }}',
        "NEW_MINOR=${NEW_REMAINDER%%.*}",
        "NEW_PATCH=${NEW_REMAINDER#*.}",
        f'test "$NEW_PATCH" != "$NEW_REMAINDER" || {{ {refusal}; }}',
        f'case "$NEW_PATCH" in *.*) {refusal} ;; esac',
        "NEW_IDENTIFIER=",
        'for NEW_IDENTIFIER in "$NEW_MAJOR" "$NEW_MINOR" "$NEW_PATCH" "$NEW_ALPHA_NUMBER"; do',
        '  case "$NEW_IDENTIFIER" in',
        f'    ""|*[!0-9]*) {refusal} ;;',
        "    0|[1-9]|[1-9][0-9]*) ;;",
        f"    *) {refusal} ;;",
        "  esac",
        "done",
        'npm install --global --ignore-scripts --prefix "$HOME/.local/openprose-cli-$NEW_VERSION" "@openprose/prose-cli@$NEW_VERSION"',
    )
    rendered = [f"{indent}/bin/sh -c '"]
    rendered.extend(f"{indent}{line}" for line in lines)
    rendered.append(f"{indent}'")
    return "\n".join(rendered) + "\n"


def standalone_readme(
    *,
    mode: str,
    implementation: str,
    version: str,
    platform_identifier: str,
    archive_name: str,
    root_name: str,
    linux_runtime: dict[str, Any] | str,
) -> bytes:
    channel, warning = package_status(mode)
    executable = "prose.exe" if platform_identifier.startswith("win32-") else "prose"
    if mode == "development" and platform_identifier == "win32-x64":
        return (
            f"OpenProse CLI {version} ({implementation}, {platform_identifier})\n"
            f"Release channel: {channel}\n\n"
            f"IMPORTANT: {warning}\n\n"
            "This native Windows development archive is for static validation only.\n"
            "Native Windows execution is not admitted. Do not install or execute the "
            "packaged program.\n\n"
            "Validate the exact archive bytes with trusted Windows tooling against the "
            "sibling SHA256SUMS from the same local package output. Inspect the sibling "
            "release-manifest.json, provenance.json, sbom.cdx.json, and "
            "dependency-evidence.json for the package's exact static claims.\n\n"
            f"Expected archive root: {root_name}\n"
            f"Expected program member: {root_name}/{executable}\n"
            f"Expected packaged example: {root_name}/{HELLO_EXAMPLE_MEMBER}\n"
            "No installation, first-use, upgrade, or removal command is provided for "
            "this static-validation artifact.\n"
        ).encode("utf-8")
    if platform_identifier.startswith("win32-"):
        installation = (
            f"  tar -xzf {archive_name}\n"
            f"  Keep {root_name}/{executable} and its sibling files together; invoke the exact extracted path below.\n"
            "  Continue from this same archive/checksum directory; do not change into the extracted root.\n"
            f"  If you already changed into {root_name}, run: cd ..\n"
        )
    else:
        installation = (
            f"  tar -xzf {archive_name}\n"
            "  Continue from this same archive/checksum directory; do not change into the extracted root.\n"
            f"  If you already changed into {root_name}, run: cd ..\n"
        )
    linux_note = ""
    if platform_identifier.startswith("linux-"):
        if not isinstance(linux_runtime, dict):
            raise PackageError("Linux standalone README is missing its runtime floor")
        linux_note = (
            "\nLinux compatibility:\n"
            f"  Minimum glibc: {linux_runtime['minimumGlibc']}.\n"
            "  Required GLIBC maximum for this executable: "
            f"{linux_runtime['requiredGlibcMaximum'][implementation]}.\n"
            "  Execution evidence: Ubuntu 22.04 only; other distributions are unverified.\n"
        )
    bun_note = ""
    if implementation == "bun":
        runtime = BUN_RUNTIME_BY_PLATFORM[platform_identifier]
        bun_note = (
            "\nBun compatibility:\n"
            + (
                "  Requires macOS 13 or newer.\n"
                if platform_identifier.startswith("darwin-")
                else ""
            )
            + f"  Compile target: {runtime['compileTarget']}.\n"
            + f"  Runtime variant: {runtime['runtimeVariant']}.\n"
            + (
                "  The x64 package uses Bun's baseline CPU runtime variant.\n"
                if runtime["runtimeVariant"] == "baseline"
                else "  The ARM64 package uses Bun's native ARM64 runtime variant.\n"
            )
        )
    example_path = f"{root_name}/{HELLO_EXAMPLE_MEMBER}"
    prose_command = f'"$PWD/{root_name}/{executable}"'
    example_command = f'"$PWD/{example_path}"'
    alpha_guidance = ""
    runtime_guidance = ""
    support_guidance = ""
    if mode == "alpha":
        harnesses = ALPHA_HARNESS_SUPPORT.get(platform_identifier)
        support = (
            format_harness_names(harnesses)
            if harnesses is not None
            else "none (Windows is omitted from the functional alpha)"
        )
        profile_harnesses = tuple(
            harness for harness in ("prime", "omp") if harness in (harnesses or ())
        )
        profile_guidance = ""
        if profile_harnesses:
            profile_names = format_harness_names(profile_harnesses)
            if len(profile_harnesses) == 1:
                profile_guidance = (
                    f"The {profile_names} profile selects a harness-managed login route. "
                    "This profile does not establish subscription billing.\n"
                )
            else:
                profile_guidance = (
                    f"The {profile_names} profiles select harness-managed login routes. "
                    "These profiles do not establish subscription billing.\n"
                )
            profile_guidance += (
                "The example model is illustrative. Replace it with a fully qualified "
                "model identifier supported by the selected harness and authentication "
                "route.\n"
            )
        alpha_guidance = (
            f"\nFunctional-alpha harness support on this platform: {support}\n"
            f"{alpha_version_guidance(harnesses)}"
            f"{profile_guidance}"
            "Supported harness install, repair, and run journeys for this platform:\n"
            f"{alpha_repair_commands(platform_identifier, prose_command, example_command)}"
        )
        runtime_guidance = (
            "This first-use journey sends the opaque `prose run <path>` task argument "
            "vector (`argv`) to the selected harness. The echo-v0 image asks the "
            "selected harness to echo that argument vector. The human-output safety "
            "policy may withhold task-bearing text. The runner does not open or read "
            "the packaged file, execute this OpenProse contract, or return its Hello, "
            "world! value.\n"
        )
        support_guidance = alpha_support_guidance(markdown=False)
    gatekeeper = (
        darwin_gatekeeper_guidance(prose_command)
        if mode == "alpha" and platform_identifier.startswith("darwin-")
        else ""
    )
    verification_heading = (
        "Verify the downloaded archive before running its executable:\n"
    )
    checksum_source = "  Download SHA256SUMS from the same GitHub release into the archive directory.\n"
    upgrade_guidance = (
        "  Download and checksum an exact newer archive, extract it beside this root, "
        "and invoke that newer root's executable by its exact path.\n\n"
    )
    evidence_guidance = (
        "Inspect the matching release-manifest.json, provenance.json, sbom.cdx.json, "
        "dependency-evidence.json, and admission report for exact claims.\n"
    )
    if mode == "development":
        verification_heading = (
            "Verify this local archive against the sibling SHA256SUMS before running "
            "its executable:\n"
        )
        checksum_source = (
            "  Use SHA256SUMS from the same local package output directory.\n"
        )
        upgrade_guidance = (
            "  Build and package an exact newer development version into a new output "
            "directory, verify its sibling SHA256SUMS, extract it beside this root, "
            "and invoke that newer root's executable by its exact path.\n\n"
        )
        evidence_guidance = (
            "Inspect the sibling local package evidence: release-manifest.json, "
            "provenance.json, sbom.cdx.json, and dependency-evidence.json.\n"
        )
    if mode in {"kernel-rc", "release"}:
        first_use = (
            "First run with the packaged Agents SDK default:\n"
            "  Keep prose and its sibling prose-agents-sdk together. No Python installation is required.\n"
            "  Supply OPENAI_API_KEY in your environment; requests use your OpenAI API account.\n"
            "  The fresh-install model default is gpt-6.1-sol. Saved explicit harness choices remain effective.\n"
            f"  {prose_command} cli doctor\n"
            f'  {prose_command} run "$PWD/{example_path}"\n\n'
            "  Running contacts the selected provider and may incur charges.\n"
        )
    else:
        first_use = (
            "First run with Codex 0.149.0-alpha.4.1 (install that exact version and "
            "complete Codex sign-in before you continue):\n"
            f"{PROVIDER_CHARGE_BOUNDARY}\n"
            "  npm install --global @openai/codex@0.149.0-alpha.4.1\n"
            "  codex login\n"
            f"  {prose_command} cli harness list\n"
            f"  {prose_command} cli harness use codex\n"
            f"  {prose_command} cli doctor\n"
            f'  {prose_command} run "$PWD/{example_path}"\n\n'
        )
    return (
        f"OpenProse CLI {version} ({implementation}, {platform_identifier})\n"
        f"Release channel: {channel}\n\n"
        f"IMPORTANT: {warning}\n\n"
        f"{verification_heading}"
        f"{checksum_source}"
        f"  ASSET='{archive_name}'\n"
        '  test "$(awk -v name="$ASSET" \'$2 == name { n++ } END { print n+0 }\' '
        "SHA256SUMS)\" -eq 1 || { echo 'missing or duplicate checksum entry' >&2; exit 1; }\n"
        "  Linux:  awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | sha256sum -c -\n"
        "  macOS:  awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | shasum -a 256 -c -\n\n"
        "Install:\n"
        f"{installation}\n"
        f"{first_use}"
        "Upgrade:\n"
        f"{upgrade_guidance}"
        "Repair this same exact version:\n"
        "  Keep the verified archive and SHA256SUMS together. Run the complete "
        "block, then enter one absolute path whose final repair directory does not "
        "already exist. The repair extracts only this closed archive into that fresh "
        "location, validates its required files and exact version, and prints the "
        "only executable path to invoke. It never overlays or removes an earlier "
        "installation.\n"
        f"{standalone_repair_shell(archive_name=archive_name, root_name=root_name, executable=executable, version=version, implementation=implementation, indent='  ')}\n"
        "Uninstall this exact extracted version:\n"
        "  Run the complete block and enter the absolute physical path of the exact "
        "extracted root when prompted. No placeholder path is executable.\n"
        f"{standalone_uninstall_shell(root_name, executable, '  ')}\n"
        f"{runtime_guidance}"
        "The CLI never opens or controls an interactive TUI.\n"
        f"{alpha_guidance}"
        f"{support_guidance}"
        f"{evidence_guidance}"
        f"{linux_note}"
        f"{bun_note}"
        f"{gatekeeper}"
    ).encode("utf-8")


def development_npm_readme(version: str, platform_identifier: str) -> bytes:
    if platform_identifier not in PLATFORMS:
        raise PackageError(
            "development npm README requires an exact supported platform"
        )
    channel, warning = package_status("development")
    platform_package = f"openprose-prose-cli-{platform_identifier}-{version}.tgz"
    meta_package = f"openprose-prose-cli-{version}.tgz"
    prefix = f'"$HOME/.local/openprose-cli-{version}"'
    return (
        f"# OpenProse CLI {version}\n\n"
        f"Release channel: {channel}.\n\n"
        f"**Important:** {warning}\n\n"
        "## Install or repair\n\n"
        "Install or repair only from the two sibling local tarballs produced by the "
        "same packaging run. Run these commands from that local package output "
        "directory. These development packages are not registry artifacts.\n\n"
        "Verify each sibling tarball against the sibling `SHA256SUMS` before "
        "installation:\n\n"
        f'    ASSET="{platform_package}"\n'
        '    test "$(awk -v name="$ASSET" \'$2 == name { n++ } END { print n+0 }\' '
        "SHA256SUMS)\" -eq 1 || { echo 'missing or duplicate checksum entry' >&2; exit 1; }\n"
        "    awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | shasum -a 256 -c -  # macOS\n"
        "    awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | sha256sum -c -      # Linux\n"
        f'    ASSET="{meta_package}"\n'
        '    test "$(awk -v name="$ASSET" \'$2 == name { n++ } END { print n+0 }\' '
        "SHA256SUMS)\" -eq 1 || { echo 'missing or duplicate checksum entry' >&2; exit 1; }\n"
        "    awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | shasum -a 256 -c -  # macOS\n"
        "    awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | sha256sum -c -      # Linux\n\n"
        "Install or repair the exact pair without lifecycle scripts or a registry "
        "lookup:\n\n"
        f"    npm install --global --offline --ignore-scripts --prefix {prefix} "
        f'"./{platform_package}" "./{meta_package}"\n\n'
        f"This local package set is bound to `{platform_identifier}`.\n\n"
        "## First run with Codex 0.149.0-alpha.4.1\n\n"
        "Install that exact version and complete Codex sign-in before you continue.\n\n"
        f"{PROVIDER_CHARGE_BOUNDARY}\n\n"
        "    npm install --global @openai/codex@0.149.0-alpha.4.1\n"
        "    codex login\n"
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" cli harness list\n'
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" cli harness use codex\n'
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" cli doctor\n'
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" run "$HOME/.local/openprose-cli-{version}/lib/node_modules/@openprose/prose-cli/{HELLO_EXAMPLE_MEMBER}"\n\n'
        "The meta package contains the exact source contract at "
        f"`{HELLO_EXAMPLE_MEMBER}`. The CLI never opens or controls an interactive "
        "TUI.\n\n"
        "## Upgrade\n\n"
        "Build a newer local development package set into a new output directory, "
        "verify its sibling checksums, and install its exact sibling tarball pair "
        "into a separate versioned prefix. No registry-upgrade command is available.\n\n"
        "## Uninstall\n\n"
        f"    npm uninstall --global --prefix {prefix} @openprose/prose-cli "
        f'"@openprose/prose-cli-{platform_identifier}"\n\n'
        "The launcher performs no download, compilation, or lifecycle-script work. "
        f"Its consumer compatibility floor is Node.js {NODE_MINIMUM}; release CI and admission use exactly "
        "Node.js 24.20.0. Inspect the sibling release-manifest.json, provenance.json, "
        "sbom.cdx.json, and dependency-evidence.json for exact local claims.\n"
    ).encode("utf-8")


def npm_readme(
    mode: str, version: str, platform_identifier: str | None = None,
    *, publication_platforms: str | None = None,
) -> bytes:
    if mode == "kernel-rc":
        return (
            f"# OpenProse CLI {version} (Bun)\n\n"
            "This explicitly unsigned release candidate has no Apple Developer ID signature or notarization.\n"
            "The npm launcher requires Node.js >=22.22.3; the selected binary includes Bun.\n\n"
            f"Install an exact version in an isolated prefix:\n\n```sh\n"
            f'npm install --global --ignore-scripts --prefix "$HOME/.local/openprose-cli-{version}" @openprose/prose-cli@{version}\n'
            f'"$HOME/.local/openprose-cli-{version}/bin/prose" --version\n```\n\n'
            "Only the current platform binary is installed through an exact dependency alias. "
            "All payloads use the same registry name with platform-suffixed versions. "
            "Do not install a platform payload version as the top-level CLI. "
            "Lifecycle scripts are unnecessary. npm installs Bun; Rust has separate standalone downloads.\n\n"
            "A CLI version pins executable bytes; normal startup resolves the latest published kernel per run. "
            "The Agents SDK helper is included and selected by default; no Python installation is required. "
            "Supply OPENAI_API_KEY in your environment to use your OpenAI API account with the fresh-install gpt-6.1-sol default. "
            "Saved explicit harness choices remain effective. No mock execution is admitted in release binaries.\n\n"
            f'First run: `"$HOME/.local/openprose-cli-{version}/bin/prose" run "$HOME/.local/openprose-cli-{version}/lib/node_modules/@openprose/prose-cli/{HELLO_EXAMPLE_MEMBER}"`. '
            "Running contacts the selected provider and may incur charges.\n\n"
            f'Uninstall: `npm uninstall --global --prefix "$HOME/.local/openprose-cli-{version}" @openprose/prose-cli`.\n'
            "For offline installation, populate an isolated npm cache with the exact root and matching payload "
            "versions from a trusted registry first; then install the root with --offline. "
            "For installation without npm registry metadata, use the separately verified standalone archive.\n"
        ).encode()
    if mode == "development":
        if platform_identifier is None:
            raise PackageError(
                "development npm README requires an exact supported platform"
            )
        return development_npm_readme(version, platform_identifier)
    channel, warning = package_status(mode)
    alpha_guidance = ""
    runtime_guidance = ""
    gatekeeper_guidance = ""
    platform_resolution_guidance = ""
    support_guidance = ""
    platform_reference = "<platform>"
    upgrade_guidance = (
        "This development artifact does not provide an executable registry-upgrade "
        "command.\n\n"
    )
    if mode == "alpha":
        platform_reference = "$PLATFORM_ID"
        platform_resolution_guidance = alpha_platform_resolution_shell("    ")
        npm_prose = f'"$HOME/.local/openprose-cli-{version}/bin/prose"'
        npm_example = (
            f'"$HOME/.local/openprose-cli-{version}/lib/node_modules/'
            f'@openprose/prose-cli/{HELLO_EXAMPLE_MEMBER}"'
        )
        alpha_guidance = (
            "## Functional-alpha harness support\n\n"
            "| Platform | Harnesses |\n"
            "| --- | --- |\n"
            "| macOS Apple silicon (`darwin-arm64`) | Prime, OMP, Codex, Claude |\n"
            "| macOS Intel (`darwin-x64`) | Codex |\n"
            "| Linux x64 (`linux-x64-gnu`) | Codex, OMP |\n"
            "| Linux ARM64 (`linux-arm64-gnu`) | Codex |\n"
            "| Windows | Omitted from the functional alpha |\n\n"
            "Linux packages require glibc 2.34 or newer. Execution evidence is "
            "Ubuntu 22.04 only; other Linux distributions are unverified.\n\n"
            "Functional-alpha harness versions:\n\n"
            "- Prime: exact admitted versions are `0.7.0` and `0.8.1`.\n"
            "- OMP: exact admitted version is `18.0.9`; it requires Bun `1.3.14` or newer.\n"
            "- Codex: exact admitted version is `0.149.0-alpha.4.1`.\n"
            "- Claude: exact admitted version is `2.1.243`.\n\n"
            "This is the exact functional-alpha allowlist. Choose only a harness that "
            "the platform table supports. Each block installs or repairs the exact "
            "harness version, saves the selection, checks readiness, and runs the "
            "packaged example.\n\n"
            f"{alpha_harness_journeys(('prime', 'omp', 'codex', 'claude'), npm_prose, npm_example, markdown=True)}\n"
            "Only the Prime and OMP blocks contain example models. The example "
            "model is illustrative. Replace it with a fully qualified model "
            "identifier supported by the selected harness and authentication "
            "route.\n\n"
            "The selection command saves only the harness, model, and profile identifiers; "
            "later `cli doctor` and `run` commands use that saved bundle without overrides when no "
            "higher-precedence project or environment setting is active.\n\n"
            "The Prime and OMP profiles select harness-managed login routes. The CLI "
            "does not verify the provider, account, or billing route. These profiles "
            "do not establish subscription billing.\n\n"
        )
        runtime_guidance = (
            "This first-use journey sends the opaque `prose run <path>` task argument "
            "vector (`argv`) to the selected harness. The echo-v0 image asks the "
            "selected harness to echo that argument vector. The human-output safety "
            "policy may withhold task-bearing text. The runner does not open or read "
            "the packaged file, execute this OpenProse contract, or return its "
            "`Hello, world!` value. "
        )
        support_guidance = alpha_support_guidance(markdown=True)
        gatekeeper_guidance = (
            "macOS alpha executables are ad-hoc signed and not notarized. Verify "
            "`SHA256SUMS` first; if Gatekeeper then quarantines the executable, inspect "
            "the installed native executable and run:\n\n"
            f'    GATEKEEPER_PROSE="$HOME/.local/openprose-cli-{version}/lib/node_modules/@openprose/prose-cli-{platform_reference}/bin/prose"\n'
            '    xattr -d com.apple.quarantine "$GATEKEEPER_PROSE"\n\n'
        )
        upgrade_guidance = (
            "Enter an exact numbered functional-alpha version when prompted. The "
            "block validates it before constructing its prefix or package spec; no "
            "placeholder version is executable.\n\n"
            f"{npm_alpha_upgrade_shell('    ')}\n"
        )
    resolved_gatekeeper_guidance = (
        f"{platform_resolution_guidance}{gatekeeper_guidance}"
        if gatekeeper_guidance
        else ""
    )
    first_run = (
        "## First run with Codex 0.149.0-alpha.4.1\n\n"
        "Install that exact version and complete Codex sign-in before you continue.\n\n"
        f"{PROVIDER_CHARGE_BOUNDARY}\n\n"
        "    npm install --global @openai/codex@0.149.0-alpha.4.1\n"
        "    codex login\n"
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" cli harness list\n'
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" cli harness use codex\n'
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" cli doctor\n'
        f'    "$HOME/.local/openprose-cli-{version}/bin/prose" run "$HOME/.local/openprose-cli-{version}/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"\n\n'
    )
    if mode == "release":
        first_run = (
            "## First run with the packaged Agents SDK default\n\n"
            "The platform package includes the SDK helper and Python runtime. "
            "Supply OPENAI_API_KEY in your environment to use your OpenAI API account with gpt-6.1-sol. "
            "Saved explicit harness choices remain effective. Running contacts the selected provider and may incur charges.\n\n"
            f'    "$HOME/.local/openprose-cli-{version}/bin/prose" cli doctor\n'
            f'    "$HOME/.local/openprose-cli-{version}/bin/prose" run "$HOME/.local/openprose-cli-{version}/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"\n\n'
        )
    return (
        f"# OpenProse CLI {version}\n\n"
        f"Release channel: {channel}.\n\n"
        f"**Important:** {warning}\n\n"
        "## Install\n\n"
        "Registry install into a collision-safe, versioned prefix:\n\n"
        f'    npm install --global --ignore-scripts --prefix "$HOME/.local/openprose-cli-{version}" @openprose/prose-cli@{version}\n\n'
        "Registry repair of the same version must name both the exact platform package and "
        "the meta package:\n\n"
        f"{platform_resolution_guidance}"
        f'    npm install --global --ignore-scripts --prefix "$HOME/.local/openprose-cli-{version}" '
        f'"@openprose/prose-cli-{platform_reference}@{version}" "@openprose/prose-cli@{version}"\n\n'
        "For an offline GitHub Release install, download `SHA256SUMS`, this meta "
        "package, and the matching platform package. For each downloaded package, "
        "set `ASSET` to its exact filename and require exactly one inventory entry:\n\n"
        f"{platform_resolution_guidance}"
        f'    ASSET="openprose-prose-cli-{platform_reference}-{version}.tgz"\n'
        '    test "$(awk -v name="$ASSET" \'$2 == name { n++ } END { print n+0 }\' '
        "SHA256SUMS)\" -eq 1 || { echo 'missing or duplicate checksum entry' >&2; exit 1; }\n"
        "    awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | shasum -a 256 -c -  # macOS\n"
        "    awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | sha256sum -c -      # Linux\n\n"
        "Repeat for the meta package filename. Offline two-tarball repair or install:\n\n"
        "    npm install --global --offline --ignore-scripts "
        f'--prefix "$HOME/.local/openprose-cli-{version}" '
        f'"./openprose-prose-cli-{platform_reference}-{version}.tgz" '
        f'"./openprose-prose-cli-{version}.tgz"\n\n'
        + (
            "Functional-alpha platform IDs are `darwin-arm64`, `darwin-x64`, "
            "`linux-arm64-gnu`, and `linux-x64-gnu`. Windows is unsupported and is "
            "not advertised or installed as an optional dependency. "
            if mode == "alpha"
            else "Release-candidate platform IDs are `darwin-arm64`, `darwin-x64`, "
            "`linux-arm64-gnu`, and `linux-x64-gnu`. Windows is not included in "
            "this release candidate. "
            if publication_platforms == "posix-four"
            else "Supported platform IDs are `darwin-arm64`, `darwin-x64`, "
            "`linux-arm64-gnu`, `linux-x64-gnu`, and `win32-x64`. "
        )
        + "The npm package is Bun-backed: macOS packages require macOS 13 or newer. "
        "The x64 packages use Bun's baseline CPU runtime variants "
        "(`bun-darwin-x64-baseline` and `bun-linux-x64-baseline`); ARM64 packages "
        "use the native `bun-darwin-arm64` and `bun-linux-arm64` targets.\n\n"
        f"{first_run}"
        "The meta package contains the exact source contract at "
        "`examples/hello.prose.md`. "
        f"{runtime_guidance}"
        "The CLI never opens or controls an interactive TUI.\n\n"
        f"{alpha_guidance}"
        f"{resolved_gatekeeper_guidance}"
        f"{support_guidance}"
        "## Upgrade\n\n"
        f"{upgrade_guidance}"
        "## Uninstall\n\n"
        f"{platform_resolution_guidance}"
        f'    npm uninstall --global --prefix "$HOME/.local/openprose-cli-{version}" @openprose/prose-cli "@openprose/prose-cli-{platform_reference}"\n\n'
        "The launcher performs no download, compilation, or lifecycle-script work. "
        f"Its consumer compatibility floor is Node.js {NODE_MINIMUM}; release CI and admission use exactly "
        "Node.js 24.20.0. "
        "See the matching release manifest, provenance, SBOM, "
        "dependency-evidence.json, and admission report for exact claims.\n"
    ).encode("utf-8")


BARE_RUNNER_SELF_INVOCATION = re.compile(
    rb"(?m)^[ \t]*(?:[$>#][ \t]+)?prose(?:\.exe)?[ \t]+cli(?:[ \t]|$)"
)


def require_exact_runner_self_invocation(readme: bytes, surface: str) -> None:
    if BARE_RUNNER_SELF_INVOCATION.search(readme):
        raise PackageError(
            f"{surface} guidance invokes a PATH-resolved prose executable"
        )


def standalone_archive(
    output: Path,
    implementation: str,
    binary: bytes,
    version: str,
    platform_identifier: str,
    epoch: int,
    linux_runtime: dict[str, Any] | str,
    mode: str,
    hello_example: bytes,
    windows_host: bytes | None = None,
    sdk_members: list[tuple[str, bytes, int]] | None = None,
) -> Path:
    root = f"openprose-prose-cli-{implementation}-{version}-{platform_identifier}"
    executable_name = (
        "prose.exe" if platform_identifier.startswith("win32-") else "prose"
    )
    destination = output / f"{root}.tar.gz"
    if (
        not platform_identifier.startswith("linux-")
        and linux_runtime != "not-applicable"
    ):
        raise PackageError("non-Linux standalone archive has a Linux runtime claim")
    readme = standalone_readme(
        mode=mode,
        implementation=implementation,
        version=version,
        platform_identifier=platform_identifier,
        archive_name=destination.name,
        root_name=root,
        linux_runtime=linux_runtime,
    )
    require_exact_runner_self_invocation(readme, "standalone README")
    members = [
        (f"{root}/{executable_name}", binary, 0o755),
        (f"{root}/LICENSE", LICENSE.read_bytes(), 0o644),
        (f"{root}/README.txt", readme, 0o644),
        (f"{root}/{HELLO_EXAMPLE_MEMBER}", hello_example, 0o644),
    ]
    if windows_host is not None:
        members.append((f"{root}/{WINDOWS_HOST_NAME}", windows_host, 0o755))
    members.extend((f"{root}/{name}", data, mode) for name, data, mode in (sdk_members or []))
    tar_gz(destination, members, epoch)
    return destination


def npm_meta_manifest(
    version: str, cohort: dict[str, Any], launcher: bytes
) -> dict[str, Any]:
    return {
        "name": "@openprose/prose-cli",
        "version": version,
        "description": "OpenProse outer runner (platform-selecting launcher)",
        "license": "MIT",
        "homepage": NPM_HOMEPAGE,
        "bugs": NPM_BUGS,
        "type": "commonjs",
        "repository": {
            "type": "git",
            "url": "git+https://github.com/openprose/prose-cli.git",
            "directory": "cli/bun/npm",
        },
        "bin": {"prose": "bin/prose.js"},
        "engines": {"node": NODE_ENGINE},
        "optionalDependencies": {
            f"@openprose/prose-cli-{identifier}": (
                f"npm:@openprose/prose-cli@{npm_payload_version(version, identifier)}"
                if cohort["schema"] == "openprose.npm-cohort/3" else version
            )
            for identifier in cohort["admittedPlatforms"]
        },
        "openproseCohort": cohort,
        "openproseLauncher": {
            "path": "bin/prose.js",
            "byteLength": len(launcher),
            "sha256": sha256_bytes(launcher),
        },
        **(
            {"publishConfig": {"access": "public"}}
            if cohort.get("releaseChannel") == "functional-alpha"
            else {}
        ),
    }


def npm_platform_manifest(
    version: str,
    platform_identifier: str,
    binary: bytes,
    source_revision: str,
    image: dict[str, Any],
    cohort: dict[str, Any],
    linux_runtime: dict[str, Any] | str,
    windows_host: bytes | None = None,
) -> dict[str, Any]:
    selector = PLATFORMS[platform_identifier]
    executable_name = (
        "prose.exe" if platform_identifier.startswith("win32-") else "prose"
    )
    manifest = {
        "name": ("@openprose/prose-cli" if cohort["schema"] == "openprose.npm-cohort/3"
                 else f"@openprose/prose-cli-{platform_identifier}"),
        "version": (npm_payload_version(version, platform_identifier) if cohort["schema"] == "openprose.npm-cohort/3"
                    else version),
        "description": f"OpenProse Bun standalone for {platform_identifier}",
        "license": "MIT",
        "homepage": NPM_HOMEPAGE,
        "bugs": NPM_BUGS,
        "repository": {
            "type": "git",
            "url": "git+https://github.com/openprose/prose-cli.git",
            "directory": "cli/bun",
        },
        "os": selector["os"],
        "cpu": selector["cpu"],
        **({"libc": selector["libc"]} if "libc" in selector else {}),
        "openproseBinary": f"bin/{executable_name}",
        "openproseBinaryByteLength": len(binary),
        "openproseBinarySha256": sha256_bytes(binary),
        "openproseSourceRevision": source_revision,
        **({
            "openproseEmbeddedDiagnosticImage": cohort["embeddedDiagnosticImage"],
            "openproseKernelPolicy": cohort["kernelPolicy"],
            "openproseImageSource": cohort["imageSource"],
        } if cohort["schema"] in {"openprose.npm-cohort/2", "openprose.npm-cohort/3"} else {"openproseImage": image}),
        "openproseCohort": cohort,
        "openprosePlatform": platform_identifier,
        "openproseBunCompileTarget": BUN_RUNTIME_BY_PLATFORM[platform_identifier][
            "compileTarget"
        ],
        "openproseBunRuntimeVariant": BUN_RUNTIME_BY_PLATFORM[platform_identifier][
            "runtimeVariant"
        ],
        **(
            {"publishConfig": {"access": "public"}}
            if cohort.get("releaseChannel") == "functional-alpha"
            else {}
        ),
    }
    if platform_identifier.startswith("linux-"):
        if not isinstance(linux_runtime, dict):
            raise PackageError("Linux npm package is missing its runtime floor")
        manifest.update(
            {
                "openproseMinimumGlibc": linux_runtime["minimumGlibc"],
                "openproseRequiredGlibcMaximum": linux_runtime["requiredGlibcMaximum"][
                    "bun"
                ],
                "openproseLinuxExecutionEvidence": linux_runtime["executionEvidence"],
            }
        )
    elif linux_runtime != "not-applicable":
        raise PackageError("non-Linux npm package has a Linux runtime claim")
    if windows_host is not None:
        manifest.update(
            {
                "openproseWindowsProcessHost": f"bin/{WINDOWS_HOST_NAME}",
                "openproseWindowsProcessHostByteLength": len(windows_host),
                "openproseWindowsProcessHostSha256": sha256_bytes(windows_host),
                "openproseWindowsProcessHostAdmission": False,
            }
        )
    return manifest


def npm_packages(
    output: Path,
    bun_binary: bytes,
    version: str,
    platform_identifier: str,
    epoch: int,
    source_revision: str,
    image: dict[str, Any],
    linux_runtime: dict[str, Any] | str,
    mode: str,
    hello_example: bytes,
    windows_host: bytes | None = None,
    *,
    package_name: str = "@openprose/prose-cli",
    publication_platforms: str | None = None,
    sdk_members: list[tuple[str, bytes, int]] | None = None,
) -> tuple[Path, Path]:
    if package_name not in {"@openprose/prose-cli", "@openprose/prose"}:
        raise PackageError("npm identity must be explicitly supported")
    if package_name != "@openprose/prose-cli" and mode != "development":
        raise PackageError("new npm identity requires separate release authority; development rehearsal only")

    def identity(value: str) -> str:
        return value.replace("@openprose/prose-cli", package_name)

    def named_manifest(value: dict[str, Any]) -> dict[str, Any]:
        # Only names in generated metadata are transformed. Binary/image bytes
        # and existing release authorities are never rewritten.
        return json.loads(identity(json.dumps(value)))

    filename_prefix = package_name.removeprefix("@").replace("/", "-")
    launcher = identity(LAUNCHER.read_text("utf-8"))
    old_root_check = 'path.basename(metaRoot) !== "prose-cli"'
    if launcher.count(old_root_check) != 1:
        raise PackageError("npm launcher package-root binding changed")
    launcher = launcher.replace(old_root_check, 'path.basename(metaRoot) !== ' + json.dumps(package_name.split('/')[1]))
    cohort = npm_cohort(
        mode=mode,
        version=version,
        source_revision=source_revision,
        image=image,
        publication_platforms=publication_platforms,
    )
    if platform_identifier not in cohort["admittedPlatforms"]:
        raise PackageError("package target is not in the selected publication platform set")
    if launcher.count("__OPENPROSE_COHORT__") != 1:
        raise PackageError(
            "npm launcher template must contain exactly one cohort token"
        )
    launcher = launcher.replace(
        "__OPENPROSE_COHORT__",
        json.dumps(cohort, sort_keys=True, separators=(",", ":")),
    ).encode()
    meta = output / f"{filename_prefix}-{version}.tgz"
    readme = identity(npm_readme(mode, version, platform_identifier, publication_platforms=publication_platforms).decode()).replace("openprose-prose-cli", filename_prefix).encode()
    require_exact_runner_self_invocation(readme, "npm README")
    tar_gz(
        meta,
        [
            (
                "package/package.json",
                pretty_json(named_manifest(npm_meta_manifest(version, cohort, launcher))),
                0o644,
            ),
            ("package/bin/prose.js", launcher, 0o755),
            ("package/LICENSE", LICENSE.read_bytes(), 0o644),
            ("package/README.md", readme, 0o644),
            (f"package/{HELLO_EXAMPLE_MEMBER}", hello_example, 0o644),
        ],
        epoch,
    )
    executable_name = (
        "prose.exe" if platform_identifier.startswith("win32-") else "prose"
    )
    platform_package = (
        output / f"{filename_prefix}-{platform_identifier}-{version}.tgz"
    )
    tar_gz(
        platform_package,
        [
            (
                "package/package.json",
                pretty_json(
                    named_manifest(npm_platform_manifest(
                        version,
                        platform_identifier,
                        bun_binary,
                        source_revision,
                        image,
                        cohort,
                        linux_runtime,
                        windows_host,
                    ))
                ),
                0o644,
            ),
            (f"package/bin/{executable_name}", bun_binary, 0o755),
            *((f"package/bin/{name}", data, mode) for name, data, mode in (sdk_members or [])),
            *(
                [(f"package/bin/{WINDOWS_HOST_NAME}", windows_host, 0o755)]
                if windows_host is not None
                else []
            ),
            ("package/LICENSE", LICENSE.read_bytes(), 0o644),
        ],
        epoch,
    )
    return meta, platform_package


def tool_version(command: list[str]) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        return "unavailable"
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if command[0] in {"rustc", "cargo"}:
        rustup_toolchain = os.environ.get("RUSTUP_TOOLCHAIN")
        if rustup_toolchain:
            environment["RUSTUP_TOOLCHAIN"] = rustup_toolchain
    try:
        completed = run_bounded(
            [executable, *command[1:]],
            cwd=ROOT,
            environment=environment,
            timeout_seconds=10,
            label=f"{command[0]} version probe",
        )
    except PackageError:
        return "unavailable"
    if completed.returncode != 0:
        return "unavailable"
    rendered = (completed.stdout or completed.stderr).decode("utf-8", "replace")
    lines = rendered.strip().splitlines()
    return lines[0] if lines else "unavailable"


def verify_darwin_code_signature(binary: Path, implementation: str) -> None:
    """Refuse a macOS release candidate whose exact snapshot cannot launch safely."""

    codesign = Path("/usr/bin/codesign")
    if not codesign.is_file():
        raise PackageError("macOS packaging requires /usr/bin/codesign")
    completed = run_bounded(
        [str(codesign), "--verify", "--deep", "--strict", str(binary)],
        cwd=ROOT,
        environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        timeout_seconds=15,
        label=f"{implementation} macOS code-signature verification",
    )
    if completed.returncode != 0:
        raise PackageError(
            f"{implementation} binary failed macOS codesign --verify --deep --strict"
        )


def verify_product(
    binary: Path,
    implementation: str,
    version: str,
    source_revision: str,
    image: dict[str, Any],
    mode: str,
    *,
    test_seams_enabled: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix=f"openprose-package-verify-{implementation}-"
    ) as directory:
        root = Path(directory)
        home = root / "home"
        config = root / "config"
        home.mkdir()
        config.mkdir()
        environment = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
        observed_version = run_bounded(
            [str(binary), "--version"],
            cwd=root,
            environment=environment,
            timeout_seconds=10,
            label=f"{implementation} version verification",
        )
        expected = f"prose {version} ({implementation})\n".encode()
        if (
            observed_version.returncode != 0
            or observed_version.stdout != expected
            or observed_version.stderr
        ):
            raise PackageError(
                f"{implementation} version output does not match {expected!r}"
            )
        doctor = run_bounded(
            [str(binary), "--output", "json", "cli", "doctor"],
            cwd=root,
            environment=environment,
            timeout_seconds=10,
            label=f"{implementation} doctor verification",
        )
        try:
            report = json.loads(doctor.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PackageError(
                f"{implementation} doctor did not emit JSON: {error}"
            ) from error
        if doctor.returncode != 10:
            raise PackageError(
                f"{implementation} doctor returned {doctor.returncode}, expected 10"
            )
        if not isinstance(report, dict):
            raise PackageError(f"{implementation} doctor did not emit a JSON object")
        expected_image = {
            "formatVersion": image["imageFormatVersion"],
            "version": image["imageVersion"],
            "sha256": image["aggregateSha256"]["sha256"],
            "releaseEligible": image["releaseEligible"],
        }
        if (
            report.get("schema") != "openprose.doctor-report/1"
            or report.get("image") != expected_image
        ):
            raise PackageError(
                f"{implementation} embedded image identity does not match --image-manifest"
            )
        if mode == "kernel-rc" and report.get("imageSource") != "published-on-run":
            raise PackageError(f"{implementation} kernel-rc must resolve the published kernel on run")
        expected_runner = {
            "name": implementation,
            "version": version,
            "commit": source_revision,
        }
        if report.get("runner") != expected_runner:
            raise PackageError(
                f"{implementation} runner commit does not match --source-revision"
            )
        expected_profile = "development" if mode == "development" else "release"
        expected_build = {
            "profile": expected_profile,
            "testSeamsEnabled": test_seams_enabled,
        }
        if report.get("build") != expected_build:
            raise PackageError(
                f"{implementation} build profile does not match --mode {mode}"
            )
        if doctor.stderr:
            raise PackageError(f"{implementation} doctor contaminated stderr")
        return expected_build


def artifact_record(
    path: Path, implementation: str, kind: str, platform_identifier: str | None
) -> dict[str, Any]:
    return {
        "path": path.name,
        "kind": kind,
        "implementation": implementation,
        "platform": platform_identifier,
        "byteLength": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def external_gate(path: Path | None, option: str) -> dict[str, Any]:
    if path is None or not path.is_file():
        raise PackageError(f"release mode requires {option} FILE")
    data = path.read_bytes()
    if not data:
        raise PackageError(f"{option} must name a non-empty evidence file")
    return {"sha256": sha256_bytes(data), "byteLength": len(data)}


def strict_json_object(encoded: bytes, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise PackageError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError(f"{label} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise PackageError(f"{label} must contain a JSON object")
    return value


def dependency_evidence() -> tuple[bytes, dict[str, Any]]:
    """Generate and independently bind the repository's closed dependency inventory."""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = run_bounded(
        [
            sys.executable,
            "-I",
            str(DEPENDENCY_EVIDENCE_SCRIPT),
            "report",
            "--root",
            str(ROOT),
        ],
        cwd=ROOT,
        environment=environment,
        timeout_seconds=30,
        label="dependency evidence generation",
    )
    if completed.returncode != 0 or completed.stderr:
        raise PackageError(
            "dependency evidence generation did not settle cleanly: "
            f"exit {completed.returncode}"
        )
    encoded = completed.stdout
    if not encoded or len(encoded) > MAX_DEPENDENCY_EVIDENCE_BYTES:
        raise PackageError(
            "dependency evidence is empty or exceeds its closed size limit"
        )
    report = strict_json_object(encoded, "dependency evidence")
    if set(report) != {
        "authority",
        "generator",
        "inventories",
        "releasePolicy",
        "schema",
        "sources",
    }:
        raise PackageError(
            "dependency evidence has an unknown or missing top-level field"
        )
    if report.get("schema") != "openprose.dependency-evidence/1":
        raise PackageError("dependency evidence has an unsupported schema")
    if report.get("generator") != {
        "name": "openprose-dependency-evidence",
        "networkUsed": False,
        "providerFree": True,
        "version": 1,
    }:
        raise PackageError(
            "dependency evidence generator boundary is not provider-free"
        )
    authority = report.get("authority")
    if not isinstance(authority, dict) or {
        name: value.get("status") if isinstance(value, dict) else None
        for name, value in authority.items()
    } != {
        "licenses": "unknown",
        "signing": "not-performed",
        "vulnerabilities": "not-performed",
    }:
        raise PackageError(
            "dependency evidence overclaims or omits an external authority"
        )
    policy = report.get("releasePolicy")
    if (
        not isinstance(policy, dict)
        or policy.get("schema") != "openprose.dependency-release-policy/1"
        or policy.get("boundary") != "inventory-only"
        or policy.get("passed") is not False
        or not isinstance(policy.get("blockers"), list)
        or not policy["blockers"]
    ):
        raise PackageError("dependency evidence release policy is not fail-closed")
    sources = report.get("sources")
    if not isinstance(sources, list):
        raise PackageError("dependency evidence sources are malformed")
    observed_sources: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "path",
            "byteLength",
            "sha256",
        }:
            raise PackageError("dependency evidence source record is malformed")
        relative = source.get("path")
        length = source.get("byteLength")
        digest = source.get("sha256")
        if (
            not isinstance(relative, str)
            or relative in observed_sources
            or relative not in DEPENDENCY_SOURCE_PATHS
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length <= 0
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
        ):
            raise PackageError("dependency evidence source identity is not closed")
        path = ROOT / relative
        try:
            actual_length = path.stat().st_size
            actual_digest = sha256_file(path)
        except OSError as error:
            raise PackageError(
                f"dependency evidence source is unavailable: {relative}: {error}"
            ) from error
        if actual_length != length or actual_digest != digest:
            raise PackageError(
                f"dependency evidence source changed after inventory: {relative}"
            )
        observed_sources.add(relative)
    if observed_sources != DEPENDENCY_SOURCE_PATHS:
        raise PackageError(
            "dependency evidence does not bind every stable manifest and lockfile"
        )
    inventories = report.get("inventories")
    if not isinstance(inventories, dict) or set(inventories) != {
        "bun",
        "cargo",
        "windowsProcessHostCargo",
        "agentsSdkPython",
    }:
        raise PackageError("dependency evidence component inventory is not closed")
    expected_components = {
        "cargo": ("rust-cli", "multi-platform"),
        "windowsProcessHostCargo": ("windows-process-host", "windows"),
    }
    for name, inventory in inventories.items():
        if not isinstance(inventory, dict) or not isinstance(
            inventory.get("packages"), list
        ):
            raise PackageError(f"dependency evidence inventory is malformed: {name}")
        if name in expected_components:
            component, target = expected_components[name]
            if (
                inventory.get("component") != component
                or inventory.get("target") != target
            ):
                raise PackageError(
                    f"dependency evidence component provenance is malformed: {name}"
                )
        if not inventory["packages"]:
            raise PackageError(f"dependency evidence inventory is empty: {name}")
    canonical = (
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if encoded != canonical:
        raise PackageError("dependency evidence is not canonically encoded")
    dependency_sbom_components(report)
    return encoded, report


def dependency_sbom_components(report: dict[str, Any]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    component_names = {
        "cargo": "rust-cli",
        "windowsProcessHostCargo": "windows-process-host",
        "bun": "bun-cli",
        "agentsSdkPython": "agents-sdk-python",
    }
    for inventory_name in ("cargo", "windowsProcessHostCargo", "bun", "agentsSdkPython"):
        if inventory_name not in report["inventories"]:
            continue
        component_name = component_names[inventory_name]
        for package in report["inventories"][inventory_name]["packages"]:
            if not isinstance(package, dict) or set(package) != {
                "integrity",
                "name",
                "scopes",
                "source",
                "version",
            }:
                raise PackageError("dependency evidence package record is malformed")
            name = package.get("name")
            version = package.get("version")
            source = package.get("source")
            scopes = package.get("scopes")
            integrity = package.get("integrity")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(version, str)
                or not version
                or not isinstance(source, str)
                or not source
                or not isinstance(scopes, list)
                or not scopes
                or any(not isinstance(scope, str) or not scope for scope in scopes)
                or len(scopes) != len(set(scopes))
                or not isinstance(integrity, dict)
            ):
                raise PackageError("dependency evidence package identity is malformed")
            integrity_status = integrity.get("status")
            if integrity_status == "declared":
                if set(integrity) != {"status", "algorithm", "digest"}:
                    raise PackageError(
                        "dependency evidence integrity record is not closed"
                    )
            elif integrity_status == "not-applicable":
                if (
                    set(integrity) != {"status", "reason"}
                    or integrity.get("reason") not in NOT_APPLICABLE_INTEGRITY_REASONS
                ):
                    raise PackageError(
                        "dependency evidence integrity reason is unsupported"
                    )
            else:
                raise PackageError(
                    "dependency evidence integrity status is unsupported"
                )
            identity = f"{component_name}\0{name}\0{version}\0{source}".encode()
            record: dict[str, Any] = {
                "bom-ref": f"openprose:dependency:{sha256_bytes(identity)}",
                "type": "library",
                "group": component_name,
                "name": name,
                "version": version,
                "properties": [
                    {"name": "openprose:kind", "value": "resolved-dependency"},
                    {"name": "openprose:component", "value": component_name},
                    {"name": "openprose:source", "value": source},
                    {"name": "openprose:scopes", "value": ",".join(scopes)},
                    {"name": "openprose:integrity-status", "value": integrity_status},
                ],
            }
            if integrity_status == "declared":
                algorithm = {"sha256": "SHA-256", "sha512": "SHA-512"}.get(
                    integrity.get("algorithm")
                )
                digest = integrity.get("digest")
                expected_length = 64 if algorithm == "SHA-256" else 128
                if (
                    algorithm is None
                    or not isinstance(digest, str)
                    or re.fullmatch(f"[0-9a-f]{{{expected_length}}}", digest) is None
                ):
                    raise PackageError(
                        "dependency evidence integrity record is malformed"
                    )
                record["hashes"] = [{"alg": algorithm, "content": digest}]
            components.append(record)
    references = [component["bom-ref"] for component in components]
    if len(references) != len(set(references)):
        raise PackageError(
            "dependency evidence contains duplicate component identities"
        )
    return components


def snapshot_sdk(directory: Path, snapshots: Path, platform_identifier: str,
                 mode: str, readelf: Path | None) -> tuple[list[tuple[str, bytes, int]], dict[str, Any]]:
    """Bind the package-owned helper to its native build receipt and exact source."""
    if directory.is_symlink() or not directory.is_dir():
        raise PackageError("SDK build directory must be a real directory")
    receipt_bytes = read_static_asset(directory / SDK_RECEIPT, "SDK receipt", 4 * 1024 * 1024)
    receipt = strict_json_object(receipt_bytes, "SDK receipt")
    if receipt.get("schema") != "openprose.agents-sdk-build/1" or receipt.get("modelCalls") != 0:
        raise PackageError("Unsupported SDK build receipt")
    expected_os = "darwin" if platform_identifier.startswith("darwin-") else "linux"
    expected_arch = {"darwin-arm64": "arm64", "darwin-x64": "x86_64", "linux-arm64-gnu": "aarch64", "linux-x64-gnu": "x86_64"}.get(platform_identifier)
    if receipt.get("platform") != expected_os or receipt.get("architecture") != expected_arch:
        raise PackageError("SDK helper native target differs from package")
    if receipt.get("python") != "3.10.20" or receipt.get("pyinstaller") != "6.22.3":
        raise PackageError("SDK builder identity differs from pinned toolchain")
    expected_sources = {path: sha256_file(ROOT / path) for path in
                        ("harnesses/agents-sdk/run.py", "harnesses/agents-sdk/requirements-build.txt")}
    if receipt.get("sources") != expected_sources:
        raise PackageError("SDK build sources differ from current package source")
    helper, length, digest = snapshot_binary(directory / SDK_NAME, snapshots / SDK_NAME, "Agents SDK")
    if receipt.get("helper") != {"path": SDK_NAME, "sha256": digest, "byteLength": length}:
        raise PackageError("SDK helper differs from build receipt")
    notices = read_static_asset(directory / SDK_NOTICES, "SDK notices", 16 * 1024 * 1024)
    if receipt.get("notices") != {"path": SDK_NOTICES, "sha256": sha256_bytes(notices), "byteLength": len(notices)}:
        raise PackageError("SDK notices differ from build receipt")
    if expected_os == "darwin" and mode in {"kernel-rc", "release", "alpha"}:
        verify_darwin_code_signature(helper, "Agents SDK")
    if readelf is not None:
        inspect_linux_glibc(helper, "Agents SDK", readelf)
    environment = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    completed = run_bounded([str(helper), "--packaged-self-test"], cwd=snapshots,
                            environment=environment, timeout_seconds=30, label="packaged SDK imports")
    if completed.returncode != 0 or completed.stderr:
        raise PackageError("Packaged SDK import self-test did not settle cleanly")
    self_test = strict_json_object(completed.stdout, "SDK self-test")
    expected_test = {"schema": "openprose.sdk-packaged-self-test/1", "openaiAgents": "0.22.2", "openai": "3.13.0", "certificates": True, "modelCalls": 0}
    if self_test != expected_test or receipt.get("selfTest") != expected_test:
        raise PackageError("Packaged SDK import self-test differs")
    expected_tools = {'schema': 'openprose.sdk-packaged-tools-self-test/1', 'shellEffects': True,
                      'boundedOutput': True, 'shellCancellation': True, 'mockedPublicRetrieval': True,
                      'incompleteHttpRejected': True, 'modelCalls': 0, 'networkUsed': False}
    tool_test = run_bounded([str(helper), "--packaged-tool-self-test"], cwd=snapshots,
                            environment=environment, timeout_seconds=30, label="packaged SDK tools")
    if tool_test.returncode != 0 or tool_test.stderr or strict_json_object(tool_test.stdout, "SDK tool self-test") != expected_tools or receipt.get('toolSelfTest') != expected_tools:
        raise PackageError("Packaged SDK tool self-test differs")
    if expected_os == "linux":
        inspected = run_bounded([str(helper), "--packaged-library-test"], cwd=snapshots,
                                 environment=environment, timeout_seconds=30, label="packaged SDK libraries")
        if inspected.returncode != 0 or inspected.stderr:
            raise PackageError("Packaged SDK Linux library inspection failed")
        libraries = strict_json_object(inspected.stdout, "SDK Linux libraries")
        maximum = libraries.get("requiredGlibcMaximum")
        if libraries != receipt.get("linuxLibraries") or not isinstance(maximum, str) or re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", maximum) is None:
            raise PackageError("Packaged SDK Linux library receipt differs")
        if version_tuple(maximum) > version_tuple(LINUX_MINIMUM_GLIBC):
            raise PackageError("Packaged SDK libraries require newer than the admitted glibc floor")
    helper_bytes = verified_snapshot_bytes(helper, length, digest, "Agents SDK")
    return [(SDK_NAME, helper_bytes, 0o755), (SDK_RECEIPT, receipt_bytes, 0o644), (SDK_NOTICES, notices, 0o644)], {
        "path": SDK_NAME, "byteLength": length, "sha256": digest,
        "receiptSha256": sha256_bytes(receipt_bytes), "noticesSha256": sha256_bytes(notices),
        "python": "3.10.20", "pyinstaller": "6.22.3", "version": "0.1.0",
        "discovery": "canonical-cli-sibling", "selfTest": self_test,
        "toolSelfTest": expected_tools,
        "dependencyLockSha256": expected_sources["harnesses/agents-sdk/requirements-build.txt"]}


def build(
    args: argparse.Namespace, *, internal_package_purpose: str = "ordinary-development"
) -> None:
    if internal_package_purpose not in {
        "ordinary-development",
        "mock-benchmark",
    }:
        raise PackageError("internal package purpose is unsupported")
    if internal_package_purpose == "mock-benchmark" and args.mode != "development":
        raise PackageError("mock-benchmark packages require development mode")
    if not valid_semver(args.version):
        raise PackageError("--version must be an explicit semver without a leading v")
    if SOURCE_REVISION.fullmatch(args.source_revision) is None:
        raise PackageError(
            "--source-revision must contain 1-128 portable identity characters"
        )
    if args.source_date_epoch < 0:
        raise PackageError("--source-date-epoch must be non-negative")
    sdk_directory = getattr(args, "agents_sdk_build", None)
    if args.mode in {"kernel-rc", "release"} and sdk_directory is None:
        raise PackageError("production packaging requires --agents-sdk-build DIRECTORY")
    image, image_manifest_sha256 = read_image_manifest(args.image_manifest)
    image_record = image_identity(image, image_manifest_sha256)
    publication_platforms = getattr(args, "publication_platforms", None)
    if publication_platforms is not None:
        npm_cohort(mode=args.mode, version=args.version,
                   source_revision=args.source_revision, image=image_record,
                   publication_platforms=publication_platforms)
    if args.mode == "kernel-rc":
        if image_manifest_sha256 != sha256_file(DIAGNOSTIC_IMAGE_MANIFEST):
            raise PackageError("kernel-rc requires the exact embedded diagnostic manifest")
    if args.mode == "development":
        if internal_package_purpose == "ordinary-development":
            if (
                image["purpose"] != "functional-alpha-placeholder"
                or image["releaseEligible"] is not True
            ):
                raise PackageError(
                    "ordinary development packages require the release-eligible "
                    "functional-alpha placeholder image"
                )
        else:
            if (
                image["purpose"] != "sentinel-transport-test"
                or image["releaseEligible"] is not False
                or image_manifest_sha256 != sha256_file(SENTINEL_IMAGE_MANIFEST)
            ):
                raise PackageError(
                    "mock-benchmark packages require the exact release-ineligible "
                    "sentinel image"
                )
    dependency_bytes, dependency_report = dependency_evidence()
    dependency_record = {
        "path": "dependency-evidence.json",
        "byteLength": len(dependency_bytes),
        "sha256": sha256_bytes(dependency_bytes),
        "releasePolicyPassed": False,
    }
    hello_example = read_static_asset(HELLO_EXAMPLE, "Hello World example contract")

    canonical_profile = None
    release_evidence = None
    if args.mode in {"alpha", "release"}:
        if image["releaseEligible"] is not True:
            raise PackageError(
                f"{args.mode} mode refused: image manifest releaseEligible is false"
            )
        expected_purpose = (
            "functional-alpha-placeholder"
            if args.mode == "alpha"
            else "canonical-language-runtime"
        )
        if image["purpose"] != expected_purpose:
            raise PackageError(
                f"{args.mode} mode refused: image manifest purpose must be {expected_purpose}"
            )
    if args.mode == "release":
        missing = []
        if args.canonical_profile is None:
            missing.append("--canonical-profile FILE")
        if args.release_evidence is None:
            missing.append("--release-evidence FILE")
        if missing:
            raise PackageError("release mode requires " + " and ".join(missing))
        canonical_profile = external_gate(args.canonical_profile, "--canonical-profile")
        release_evidence = external_gate(args.release_evidence, "--release-evidence")

    platform_identifier = current_platform_id()
    is_windows = platform_identifier.startswith("win32-")
    if publication_platforms == "posix-four" and platform_identifier not in POSIX_PUBLICATION_PLATFORMS:
        raise PackageError("posix-four publication packaging refuses this target")
    is_linux = platform_identifier.startswith("linux-")
    if args.mode == "alpha" and is_windows:
        raise PackageError(
            "alpha mode refuses Windows: win32-x64 has no functional-alpha admission"
        )
    if is_windows and args.windows_process_host is None:
        raise PackageError(
            f"Windows packaging requires --windows-process-host {WINDOWS_HOST_NAME}"
        )
    if not is_windows and args.windows_process_host is not None:
        raise PackageError(
            "--windows-process-host is valid only for a Windows package target"
        )
    if is_linux and args.readelf is None:
        raise PackageError(
            "Linux packaging requires --readelf with an exact executable path"
        )
    if not is_linux and args.readelf is not None:
        raise PackageError("--readelf is valid only for a Linux package target")
    if args.out.exists():
        raise PackageError(f"output path already exists: {args.out}")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".openprose-package-", dir=args.out.parent
    ) as temporary:
        temporary_root = Path(temporary)
        snapshots = temporary_root / "inputs"
        snapshots.mkdir(mode=0o700)
        executable_suffix = ".exe" if platform_identifier.startswith("win32-") else ""
        rust_snapshot, rust_length, rust_digest = snapshot_binary(
            args.rust_binary,
            snapshots / f"rust-prose{executable_suffix}",
            "rust",
        )
        bun_snapshot, bun_length, bun_digest = snapshot_binary(
            args.bun_binary,
            snapshots / f"bun-prose{executable_suffix}",
            "bun",
        )
        readelf_record = (
            snapshot_executable_tool(args.readelf, snapshots / "readelf", "readelf")
            if args.readelf is not None
            else None
        )
        windows_host_snapshot: Path | None = None
        windows_host_length: int | None = None
        windows_host_digest: str | None = None
        if args.windows_process_host is not None:
            (
                windows_host_snapshot,
                windows_host_length,
                windows_host_digest,
            ) = snapshot_binary(
                args.windows_process_host,
                snapshots / WINDOWS_HOST_NAME,
                "Windows process host",
            )
        if platform_identifier.startswith("darwin-") and args.mode in {
            "alpha",
            "release",
            "kernel-rc",
        }:
            verify_darwin_code_signature(rust_snapshot, "rust")
            verify_darwin_code_signature(bun_snapshot, "bun")
        test_seams_enabled = internal_package_purpose == "mock-benchmark"
        rust_build_profile = verify_product(
            rust_snapshot,
            "rust",
            args.version,
            args.source_revision,
            image,
            args.mode,
            test_seams_enabled=test_seams_enabled,
        )
        rust_package_bytes = verified_snapshot_bytes(
            rust_snapshot, rust_length, rust_digest, "rust"
        )
        bun_build_profile = verify_product(
            bun_snapshot,
            "bun",
            args.version,
            args.source_revision,
            image,
            args.mode,
            test_seams_enabled=test_seams_enabled,
        )
        bun_package_bytes = verified_snapshot_bytes(
            bun_snapshot, bun_length, bun_digest, "bun"
        )
        if is_linux:
            assert readelf_record is not None
            readelf_snapshot, readelf_length, readelf_digest = readelf_record
            linux_runtime: dict[str, Any] | str = linux_runtime_record(
                rust_snapshot,
                bun_snapshot,
                readelf_snapshot,
                readelf_length,
                readelf_digest,
            )
        else:
            linux_runtime = "not-applicable"
        windows_host_bytes = None
        if windows_host_snapshot is not None:
            assert windows_host_length is not None and windows_host_digest is not None
            windows_host_bytes = verified_snapshot_bytes(
                windows_host_snapshot,
                windows_host_length,
                windows_host_digest,
                "Windows process host",
            )
        windows_host_record = (
            {
                "path": WINDOWS_HOST_NAME,
                "byteLength": windows_host_length,
                "sha256": windows_host_digest,
                "admission": False,
            }
            if windows_host_bytes is not None
            else "not-applicable"
        )

        sdk_members, sdk_record = (snapshot_sdk(sdk_directory, snapshots, platform_identifier,
                                               args.mode, readelf_record[0] if readelf_record else None)
                                  if sdk_directory is not None else ([], "not-packaged-development-fixture"))

        staging = temporary_root / "result"
        staging.mkdir()
        rust_archive = standalone_archive(
            staging,
            "rust",
            rust_package_bytes,
            args.version,
            platform_identifier,
            args.source_date_epoch,
            linux_runtime,
            args.mode,
            hello_example,
            windows_host_bytes,
            sdk_members,
        )
        bun_archive = standalone_archive(
            staging,
            "bun",
            bun_package_bytes,
            args.version,
            platform_identifier,
            args.source_date_epoch,
            linux_runtime,
            args.mode,
            hello_example,
            windows_host_bytes,
            sdk_members,
        )
        meta_package, platform_package = npm_packages(
            staging,
            bun_package_bytes,
            args.version,
            platform_identifier,
            args.source_date_epoch,
            args.source_revision,
            image_record,
            linux_runtime,
            args.mode,
            hello_example,
            windows_host_bytes,
            package_name=getattr(args, "npm_package_name", "@openprose/prose-cli"),
            publication_platforms=publication_platforms,
            sdk_members=sdk_members,
        )
        artifacts = [
            artifact_record(
                rust_archive, "rust", "standalone-archive", platform_identifier
            ),
            artifact_record(
                bun_archive, "bun", "standalone-archive", platform_identifier
            ),
            artifact_record(meta_package, "bun", "npm-meta", None),
            artifact_record(
                platform_package, "bun", "npm-platform", platform_identifier
            ),
        ]
        toolchains = {
            "python": sys.version.split()[0],
            "rustc": tool_version(["rustc", "--version"]),
            "cargo": tool_version(["cargo", "--version"]),
            "bun": tool_version(["bun", "--version"]),
            "node": tool_version(["node", "--version"]),
            "npm": tool_version(["npm", "--version"]),
        }
        release_manifest = {
            "schema": "openprose.local-release-manifest/1",
            "mode": args.mode,
            "version": args.version,
            "platform": platform_identifier,
            "sourceDateEpoch": args.source_date_epoch,
            # The local packager can validate shape and bind input digests, but
            # it cannot establish the external authority needed for promotion.
            "releaseEligible": False,
            "publicationAuthorized": False,
            "promotion": {
                "status": "not-performed",
                "requiredAttestation": "protected-release-validator",
            },
            "source": {
                "revision": args.source_revision,
                "verification": "matched-product-doctor",
            },
            "buildProfiles": {
                "rust": rust_build_profile,
                "bun": bun_build_profile,
            },
            "bunRuntime": BUN_RUNTIME_BY_PLATFORM[platform_identifier],
            "linuxRuntime": linux_runtime,
            **({
                "embeddedDiagnosticImage": {key: value for key, value in image_record.items() if key != "releaseEligible"},
                "imageSource": "published-on-run",
                "kernelPolicy": dict(PUBLISHED_KERNEL_POLICY),
            } if args.mode == "kernel-rc" else {"image": image_record}),
            "windowsProcessHost": windows_host_record,
            "agentsSdk": sdk_record,
            "windowsJobObjectReleaseAdmission": False,
            "toolchains": toolchains,
            "lockfiles": {
                "cargoSha256": sha256_file(RUST_LOCK),
                "bunSha256": sha256_file(BUN_LOCK),
            },
            "dependencyEvidence": dependency_record,
            "externalGates": {
                "canonicalProfile": canonical_profile or "unavailable",
                "releaseEvidence": release_evidence or "unavailable",
                "authorityValidatedByPackager": False,
            },
            "claims": {
                "signing": "not-performed",
                "vulnerabilityReview": "not-performed",
                "networkIsolation": "not-enforced",
                "packageTests": "not-run-by-packager",
            },
            "artifacts": artifacts,
        }
        (staging / "release-manifest.json").write_bytes(pretty_json(release_manifest))
        (staging / "dependency-evidence.json").write_bytes(dependency_bytes)

        source_key = f"{args.source_revision}\0{args.version}\0{platform_identifier}"
        bom_uuid = uuid.UUID(bytes=hashlib.sha256(source_key.encode()).digest()[:16])
        sbom = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "serialNumber": f"urn:uuid:{bom_uuid}",
            "version": 1,
            "metadata": {
                "component": {
                    "type": "application",
                    "name": "OpenProse CLI local artifacts",
                    "version": args.version,
                }
            },
            "components": [
                {
                    "type": "file",
                    "name": artifact["path"],
                    "version": args.version,
                    "hashes": [{"alg": "SHA-256", "content": artifact["sha256"]}],
                    "properties": [
                        {
                            "name": "openprose:implementation",
                            "value": artifact["implementation"],
                        },
                        {"name": "openprose:kind", "value": artifact["kind"]},
                    ],
                }
                for artifact in artifacts
            ]
            + (
                [
                    {
                        "type": "file",
                        "name": WINDOWS_HOST_NAME,
                        "version": args.version,
                        "hashes": [{"alg": "SHA-256", "content": windows_host_digest}],
                        "properties": [
                            {
                                "name": "openprose:kind",
                                "value": "windows-process-host-sidecar",
                            },
                            {"name": "openprose:release-admission", "value": "false"},
                        ],
                    }
                ]
                if windows_host_bytes is not None
                else []
            )
            + ([{"type": "file", "name": SDK_NAME, "version": "0.1.0",
                 "hashes": [{"alg": "SHA-256", "content": sdk_record["sha256"]}],
                 "properties": [{"name": "openprose:kind", "value": "packaged-agents-sdk-helper"}]}]
               if isinstance(sdk_record, dict) else [])
            + dependency_sbom_components(dependency_report),
            "properties": [
                {
                    "name": "openprose:dependency-inventory",
                    "value": "component-inventory-attached",
                },
                {
                    "name": "openprose:dependency-evidence-sha256",
                    "value": dependency_record["sha256"],
                },
                {"name": "openprose:vulnerability-review", "value": "not-performed"},
            ],
        }
        (staging / "sbom.cdx.json").write_bytes(pretty_json(sbom))

        provenance = {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [
                {"name": artifact["path"], "digest": {"sha256": artifact["sha256"]}}
                for artifact in artifacts
            ],
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildDefinition": {
                    "buildType": "https://openprose.org/build/local-cli-packaging/v1",
                    "externalParameters": {
                        "mode": args.mode,
                        "version": args.version,
                        "platform": platform_identifier,
                        "sourceRevision": args.source_revision,
                        "sourceDateEpoch": args.source_date_epoch,
                        **({
                            "embeddedDiagnosticImage": release_manifest["embeddedDiagnosticImage"],
                            "imageSource": "published-on-run",
                            "kernelPolicy": dict(PUBLISHED_KERNEL_POLICY),
                        } if args.mode == "kernel-rc" else {"image": image_record}),
                        "buildProfiles": release_manifest["buildProfiles"],
                        "bunRuntime": release_manifest["bunRuntime"],
                        "linuxRuntime": linux_runtime,
                        "windowsProcessHost": windows_host_record,
                        "agentsSdk": sdk_record,
                        "windowsJobObjectReleaseAdmission": False,
                    },
                    "resolvedDependencies": [
                        {
                            "uri": "openprose:embedded-diagnostic-image" if args.mode == "kernel-rc" else "openprose:skill-runtime-image",
                            "digest": {"sha256": image_record["sha256"]},
                        },
                        {
                            "uri": "openprose:rust-cargo-lock",
                            "digest": {"sha256": sha256_file(RUST_LOCK)},
                        },
                        {
                            "uri": "openprose:bun-lock",
                            "digest": {"sha256": sha256_file(BUN_LOCK)},
                        },
                        {
                            "uri": "openprose:dependency-evidence",
                            "digest": {"sha256": dependency_record["sha256"]},
                        },
                    ]
                    + ([{"uri": "openprose:agents-sdk-helper", "digest": {"sha256": sdk_record["sha256"]}},
                        {"uri": "openprose:agents-sdk-build-receipt", "digest": {"sha256": sdk_record["receiptSha256"]}},
                        {"uri": "openprose:agents-sdk-python-lock", "digest": {"sha256": sdk_record["dependencyLockSha256"]}}]
                       if isinstance(sdk_record, dict) else [])
                    + (
                        [
                            {
                                "uri": "openprose:windows-process-host",
                                "digest": {"sha256": windows_host_digest},
                            }
                        ]
                        if windows_host_bytes is not None
                        else []
                    ),
                },
                "runDetails": {
                    "builder": {
                        "id": "https://openprose.org/builders/local-untrusted/v1"
                    },
                    "metadata": {
                        "invocationId": "unavailable",
                        "startedOn": "unavailable",
                        "finishedOn": "unavailable",
                    },
                    "byproducts": [
                        {"name": "signing", "value": "not-performed"},
                        {"name": "vulnerability-review", "value": "not-performed"},
                        {"name": "network-isolation", "value": "not-enforced"},
                    ],
                },
            },
        }
        (staging / "provenance.json").write_bytes(pretty_json(provenance))

        checksummed = sorted(
            path for path in staging.iterdir() if path.name != "SHA256SUMS"
        )
        sums = "".join(f"{sha256_file(path)}  {path.name}\n" for path in checksummed)
        (staging / "SHA256SUMS").write_text(sums, "utf-8", newline="\n")
        os.replace(staging, args.out)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--mode", choices=("development", "alpha", "release", "kernel-rc"), required=True
    )
    result.add_argument("--publication-platforms", choices=("posix-four",), help="Explicit four-platform canonical kernel RC cohort; does not grant publication authority")
    result.add_argument("--npm-package-name", choices=("@openprose/prose-cli", "@openprose/prose"), default="@openprose/prose-cli")
    result.add_argument("--version", required=True)
    result.add_argument("--source-revision", required=True)
    result.add_argument("--source-date-epoch", type=int, default=0)
    result.add_argument("--rust-binary", type=Path, required=True)
    result.add_argument("--bun-binary", type=Path, required=True)
    result.add_argument("--windows-process-host", type=Path)
    result.add_argument("--agents-sdk-build", type=Path, help="Verified native frozen SDK build directory; required for production packaging")
    result.add_argument("--readelf", type=Path)
    result.add_argument("--image-manifest", type=Path, required=True)
    result.add_argument("--canonical-profile", type=Path)
    result.add_argument("--release-evidence", type=Path)
    result.add_argument("--out", type=Path, required=True)
    return result


def main() -> int:
    try:
        build(parser().parse_args())
    except PackageError as error:
        print(f"package-local: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
