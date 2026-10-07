#!/usr/bin/env python3
"""Provider-free measurements over already-created OpenProse package artifacts."""

from __future__ import annotations

import argparse
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
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import zlib
from typing import Any, Iterable, Mapping, Sequence


REPORT_SCHEMA = "openprose.installed-package-benchmark/1"
ANALYSIS_SCHEMA = "openprose.installed-package-benchmark-analysis/1"
ERROR_SCHEMA = "openprose.installed-package-benchmark-error/1"
RELEASE_SCHEMA = "openprose.local-release-manifest/1"
LINUX_MINIMUM_GLIBC = "2.34"
LINUX_EXECUTION_EVIDENCE = "ubuntu-22.04-only"
NODE_ENGINE = ">=22.22.3"
NPM_COHORT_SCHEMA = "openprose.npm-cohort/1"
DEPENDENCY_SCHEMA = "openprose.dependency-evidence/1"
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 768 * 1024 * 1024
MAX_MEMBERS = 128
MAX_PROCESS_OUTPUT = 2 * 1024 * 1024
MAX_TOOL_BYTES = 256 * 1024 * 1024
INSTALLED_TREE_SCHEMA = "openprose.installed-tree-identity/1"
MAX_INSTALLED_TREE_ENTRIES = 8_192
MAX_PACKAGE_OUTPUT_ENTRIES = 256
MAX_INSTALLED_TREE_FILE_BYTES = 256 * 1024 * 1024
MAX_INSTALLED_TREE_BYTES = 768 * 1024 * 1024
MAX_INSTALLED_TREE_PATH_BYTES = 4_096
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Exact pinned Python releases use PEP 440 release segments, not npm/Cargo SemVer.
# Epochs are excluded by the hash-locked requirements token grammar.
PYTHON_VERSION_RE = re.compile(
    r"v?[0-9]+(?:\.[0-9]+)*"
    r"(?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?[0-9]*)?"
    r"(?:(?:-[0-9]+)|(?:[-_.]?(?:post|rev|r)[-_.]?[0-9]*))?"
    r"(?:[-_.]?dev[-_.]?[0-9]*)?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?",
    re.IGNORECASE | re.ASCII,
)
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SOURCE_RE = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")
DEPENDENCY_NAME_RE = re.compile(r"^[A-Za-z0-9_@./-]{1,256}$")
EXPECTED_DEPENDENCY_SOURCES = (
    "cli/bun/bun.lock",
    "cli/bun/package.json",
    "cli/platform/windows-process-host/Cargo.lock",
    "cli/platform/windows-process-host/Cargo.toml",
    "cli/rust/Cargo.lock",
    "cli/rust/Cargo.toml",
    "cli/rust/crates/prose-cli/Cargo.toml",
    "cli/rust/crates/prose-process-supervisor/Cargo.toml",
    "cli/rust/crates/prose-runner-core/Cargo.toml",
)
SUPPORTED_PLATFORMS = (
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64-gnu",
    "linux-x64-gnu",
    "win32-x64",
)
BUN_RUNTIME_BY_PLATFORM = {
    "darwin-arm64": {"compileTarget": "bun-darwin-arm64", "runtimeVariant": "native"},
    "darwin-x64": {
        "compileTarget": "bun-darwin-x64-baseline",
        "runtimeVariant": "baseline",
    },
    "linux-arm64-gnu": {"compileTarget": "bun-linux-arm64", "runtimeVariant": "native"},
    "linux-x64-gnu": {
        "compileTarget": "bun-linux-x64-baseline",
        "runtimeVariant": "baseline",
    },
    "win32-x64": {
        "compileTarget": "bun-windows-x64-baseline",
        "runtimeVariant": "baseline",
    },
}
OPAQUE_ARGV = (
    "write",
    "installed-package-benchmark",
    "opaque 雪",
    "--model",
    "literal;$(never-shell)",
)
FORWARDED_TASK_ARGV = ("prose", *OPAQUE_ARGV)
INVOCATION_ARGV = ("--harness", "mock", "--output", "json", *OPAQUE_ARGV)
CURRENT_SENTINEL_IMAGE = {
    "formatVersion": "openprose.skill-runtime-image/1",
    "version": "sentinel-v1",
    "sha256": "ee13d1cbba24d1623523f4fe8747b4a3387cd6880f5d6fb4a360d2a7949ddf00",
}
CURRENT_ECHO_IMAGE = {
    "formatVersion": "openprose.skill-runtime-image/1",
    "version": "echo-v0",
    "sha256": "daf3fab11a27b6c982efdad0d223823b35bd7c8de05d9b1d464a30ed8ec146f2",
}
CANONICAL_LAUNCHER_TEMPLATE_SHA256 = (
    "b46fbc129a778eb60a9e260044f7f8886cc3e597a9450500e1f62138df46e7f3"
)
REQUIRED_EVIDENCE = {
    "release-manifest.json",
    "sbom.cdx.json",
    "provenance.json",
    "dependency-evidence.json",
}
_ACTIVE_GROUPS: dict[int, str] = {}


class BenchmarkError(Exception):
    """A fail-closed package or process boundary error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code: str, message: str) -> None:
    raise BenchmarkError(code, message)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def render_json(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_read(path: Path, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        fail("INPUT_UNAVAILABLE", f"cannot inspect {path}: {error}")
    if not stat.S_ISREG(before.st_mode):
        fail("INPUT_UNSAFE", f"input is not a non-symlink regular file: {path}")
    if before.st_size < 1 or before.st_size > maximum:
        fail("INPUT_SIZE_INVALID", f"input size is outside 1..{maximum} bytes: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        fail("INPUT_UNAVAILABLE", f"cannot open {path}: {error}")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            fail("INPUT_UNSAFE", f"opened input is not regular: {path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            fail("INPUT_MUTATED", f"input identity changed before read: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                fail("INPUT_SIZE_INVALID", f"input exceeds {maximum} bytes: {path}")
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total != opened.st_size:
            fail("INPUT_MUTATED", f"input changed while being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def json_no_duplicates(encoded: bytes, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if name in result:
                fail("EVIDENCE_DUPLICATE", f"duplicate JSON key {name!r} in {label}")
            result[name] = value
        return result

    try:
        text = encoded.decode("utf-8")
        return json.loads(text, object_pairs_hook=pairs)
    except BenchmarkError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail("EVIDENCE_MALFORMED", f"invalid JSON in {label}: {error}")


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("EVIDENCE_MALFORMED", f"{label} must be an object")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail("EVIDENCE_MALFORMED", f"{label} must be a nonempty string")
    return value


def require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        fail("EVIDENCE_MALFORMED", f"{label} must be a lowercase SHA-256 digest")
    return value


def require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        fail("EVIDENCE_MALFORMED", f"{label} has an unsupported shape")


def require_nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("REPORT_MALFORMED", f"{label} must be a number")
    observed = float(value)
    if not math.isfinite(observed) or observed < 0:
        fail("REPORT_MALFORMED", f"{label} must be finite and nonnegative")
    return observed


def require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail("REPORT_MALFORMED", f"{label} must be a positive integer")
    return value


def is_exact_semver(value: str) -> bool:
    match = SEMVER_RE.fullmatch(value)
    if match is None:
        return False
    prerelease = match.group(4)
    if prerelease is None:
        return True
    return all(
        not (
            identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        )
        for identifier in prerelease.split(".")
    )


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
        fail(
            "PLATFORM_UNSUPPORTED",
            f"unsupported host {platform.system()}-{platform.machine()}",
        )
    value = f"linux-{machine}-gnu" if system == "linux" else f"{system}-{machine}"
    if value not in SUPPORTED_PLATFORMS:
        fail("PLATFORM_UNSUPPORTED", f"unsupported package platform {value}")
    return value


def resolve_tool(name: str) -> dict[str, Any] | None:
    observed = shutil.which(name)
    if observed is None:
        return None
    command = Path(observed)
    try:
        resolved = command.resolve(strict=True)
        encoded = safe_read(resolved, MAX_TOOL_BYTES)
    except (OSError, BenchmarkError):
        return None
    return {
        "command": str(resolved),
        "resolvedPath": str(resolved),
        "sha256": sha256_bytes(encoded),
    }


def validate_basename(name: str, label: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name.startswith(".")
    ):
        fail("MEMBERSHIP_UNSAFE", f"unsafe {label}: {name!r}")


def parse_sha_sums(encoded: bytes) -> dict[str, str]:
    try:
        text = encoded.decode("ascii")
    except UnicodeDecodeError:
        fail("CHECKSUM_MALFORMED", "SHA256SUMS must be ASCII")
    records: dict[str, str] = {}
    order: list[str] = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            fail("CHECKSUM_MALFORMED", "SHA256SUMS contains a non-canonical line")
        digest, name = match.groups()
        validate_basename(name, "checksum member")
        if name in records:
            fail("CHECKSUM_DUPLICATE", f"duplicate checksum member: {name}")
        records[name] = digest
        order.append(name)
    if not records or order != sorted(order):
        fail(
            "CHECKSUM_MALFORMED",
            "SHA256SUMS must be nonempty and sorted by member name",
        )
    return records


def evidence_record(name: str, encoded: bytes) -> dict[str, Any]:
    return {"path": name, "byteLength": len(encoded), "sha256": sha256_bytes(encoded)}


def validate_release_manifest(
    value: Any, expected_platform: str, purpose: str = "mock-benchmark"
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if purpose not in {
        "mock-benchmark",
        "ordinary-development",
        "release-invariants",
        "alpha-invariants",
    }:
        fail("ARGUMENT_INVALID", "package verification purpose is unsupported")
    release = require_object(value, "release-manifest.json")
    require_exact_keys(
        release,
        {
            "schema",
            "mode",
            "version",
            "platform",
            "releaseEligible",
            "publicationAuthorized",
            "source",
            "sourceDateEpoch",
            "buildProfiles",
            "bunRuntime",
            "linuxRuntime",
            "image",
            "toolchains",
            "lockfiles",
            "windowsProcessHost",
            "windowsJobObjectReleaseAdmission",
            "externalGates",
            "claims",
            "promotion",
            "artifacts",
            "dependencyEvidence",
        } | ({"agentsSdk"} if "agentsSdk" in release else set()),
        "release manifest",
    )
    if release.get("schema") != RELEASE_SCHEMA:
        fail("EVIDENCE_MALFORMED", "unsupported release manifest schema")
    version = require_string(release.get("version"), "release version")
    if not is_exact_semver(version):
        fail("EVIDENCE_MALFORMED", "release version must be exact SemVer")
    if release.get("platform") != expected_platform:
        fail("IDENTITY_DIVERGENCE", "release platform does not match this host")
    mode = release.get("mode")
    if mode not in {"development", "alpha", "release"}:
        fail("EVIDENCE_MALFORMED", "release mode is unsupported")
    expected_mode = {
        "mock-benchmark": "development",
        "ordinary-development": "development",
        "alpha-invariants": "alpha",
        "release-invariants": "release",
    }[purpose]
    if mode != expected_mode:
        fail(
            "PURPOSE_MISMATCH",
            f"{purpose} verification requires {expected_mode} packages",
        )
    if (
        release.get("releaseEligible") is not False
        or release.get("publicationAuthorized") is not False
    ):
        fail(
            "POLICY_WEAKENED",
            "local package output must remain non-publishable evidence",
        )
    profiles = require_object(release.get("buildProfiles"), "buildProfiles")
    require_exact_keys(profiles, {"rust", "bun"}, "buildProfiles")
    for implementation in ("rust", "bun"):
        profile = require_object(
            profiles.get(implementation), f"buildProfiles.{implementation}"
        )
        expected_seams = purpose == "mock-benchmark"
        expected_profile = "development" if expected_seams else "release"
        if mode == "development":
            expected_profile = "development"
        if profile != {
            "profile": expected_profile,
            "testSeamsEnabled": expected_seams,
        }:
            fail(
                "IDENTITY_DIVERGENCE",
                f"{implementation} build profile differs from release mode",
            )
    bun_runtime = require_object(release.get("bunRuntime"), "Bun runtime")
    require_exact_keys(bun_runtime, {"compileTarget", "runtimeVariant"}, "Bun runtime")
    if bun_runtime != BUN_RUNTIME_BY_PLATFORM[expected_platform]:
        fail("IDENTITY_DIVERGENCE", "Bun runtime differs from package platform")
    linux_runtime = release.get("linuxRuntime")
    if expected_platform.startswith("linux-"):
        runtime = require_object(linux_runtime, "Linux runtime")
        require_exact_keys(
            runtime,
            {"minimumGlibc", "requiredGlibcMaximum", "executionEvidence"},
            "Linux runtime",
        )
        required = require_object(
            runtime.get("requiredGlibcMaximum"), "Linux ELF glibc requirements"
        )
        require_exact_keys(required, {"rust", "bun"}, "Linux ELF glibc requirements")
        if (
            runtime.get("minimumGlibc") != LINUX_MINIMUM_GLIBC
            or runtime.get("executionEvidence") != LINUX_EXECUTION_EVIDENCE
            or any(
                not isinstance(required.get(name), str)
                or re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", required[name]) is None
                or tuple(int(part) for part in required[name].split(".")) > (2, 34)
                for name in ("rust", "bun")
            )
        ):
            fail(
                "EVIDENCE_MALFORMED",
                "Linux runtime floor or ELF requirement is invalid",
            )
    elif linux_runtime != "not-applicable":
        fail("POLICY_WEAKENED", "non-Linux package contains a Linux runtime claim")
    source = require_object(release.get("source"), "release source")
    require_exact_keys(source, {"revision", "verification"}, "release source")
    revision = require_string(source.get("revision"), "source revision")
    if source.get("verification") != "matched-product-doctor":
        fail("IDENTITY_DIVERGENCE", "source verification differs")
    if (
        purpose in {"mock-benchmark", "ordinary-development"}
        and revision != "development"
    ):
        fail("IDENTITY_DIVERGENCE", "development source identity differs")
    if (
        purpose in {"alpha-invariants", "release-invariants"}
        and re.fullmatch(r"[0-9a-f]{40}", revision) is None
    ):
        fail(
            "IDENTITY_DIVERGENCE",
            "release source revision must be a full lowercase SHA",
        )
    if release.get("sourceDateEpoch") != 0 or isinstance(
        release.get("sourceDateEpoch"), bool
    ):
        fail("IDENTITY_DIVERGENCE", "development source date epoch differs")
    image = require_object(release.get("image"), "release image")
    if set(image) != {
        "formatVersion",
        "version",
        "sha256",
        "manifestSha256",
        "purpose",
        "releaseEligible",
    }:
        fail("EVIDENCE_MALFORMED", "release image identity has an unsupported shape")
    require_string(image.get("formatVersion"), "image formatVersion")
    require_string(image.get("version"), "image version")
    require_sha(image.get("sha256"), "image sha256")
    require_sha(image.get("manifestSha256"), "image manifestSha256")
    purpose_value = require_string(image.get("purpose"), "image purpose")
    expected_image_purpose = {
        "development": (
            "sentinel-transport-test"
            if purpose == "mock-benchmark"
            else "functional-alpha-placeholder"
        ),
        "alpha": "functional-alpha-placeholder",
        "release": "canonical-language-runtime",
    }[mode]
    if purpose_value != expected_image_purpose:
        fail("IDENTITY_DIVERGENCE", "image purpose differs from package mode")
    if not isinstance(image.get("releaseEligible"), bool):
        fail("EVIDENCE_MALFORMED", "image releaseEligible must be boolean")
    if purpose == "mock-benchmark":
        if image["releaseEligible"] is not False:
            fail(
                "MOCK_UNAVAILABLE",
                "provider-free mock measurement requires the sentinel image",
            )
        if {
            key: image[key] for key in CURRENT_SENTINEL_IMAGE
        } != CURRENT_SENTINEL_IMAGE:
            fail(
                "IDENTITY_DIVERGENCE",
                "release image differs from the benchmark sentinel",
            )
    elif purpose == "ordinary-development":
        if image["releaseEligible"] is not True:
            fail(
                "IMAGE_INELIGIBLE",
                "ordinary development verification requires the echo image",
            )
        if {key: image[key] for key in CURRENT_ECHO_IMAGE} != CURRENT_ECHO_IMAGE:
            fail(
                "IDENTITY_DIVERGENCE",
                "release image differs from the ordinary development echo image",
            )
    elif image["releaseEligible"] is not True:
        fail(
            "IMAGE_INELIGIBLE",
            "release invariant verification requires a release-eligible image",
        )
    toolchains = require_object(release.get("toolchains"), "release toolchains")
    require_exact_keys(
        toolchains,
        {"python", "rustc", "cargo", "bun", "node", "npm"},
        "release toolchains",
    )
    if any(not isinstance(item, str) or not item for item in toolchains.values()):
        fail(
            "EVIDENCE_MALFORMED",
            "release toolchain identities must be nonempty strings",
        )
    lockfiles = require_object(release.get("lockfiles"), "release lockfiles")
    require_exact_keys(lockfiles, {"cargoSha256", "bunSha256"}, "release lockfiles")
    require_sha(lockfiles.get("cargoSha256"), "release Cargo lock digest")
    require_sha(lockfiles.get("bunSha256"), "release Bun lock digest")
    external = require_object(release.get("externalGates"), "release external gates")
    if purpose in {
        "mock-benchmark",
        "ordinary-development",
        "alpha-invariants",
    }:
        if external != {
            "authorityValidatedByPackager": False,
            "canonicalProfile": "unavailable",
            "releaseEvidence": "unavailable",
        }:
            fail("POLICY_WEAKENED", "release external gate boundary differs")
    else:
        require_exact_keys(
            external,
            {"authorityValidatedByPackager", "canonicalProfile", "releaseEvidence"},
            "release external gates",
        )
        if external.get("authorityValidatedByPackager") is not False:
            fail("POLICY_WEAKENED", "local packager must not claim gate authority")
        for name in ("canonicalProfile", "releaseEvidence"):
            record = require_object(external.get(name), f"external gate {name}")
            require_exact_keys(
                record, {"byteLength", "sha256"}, f"external gate {name}"
            )
            require_positive_int(
                record.get("byteLength"), f"external gate {name} byteLength"
            )
            require_sha(record.get("sha256"), f"external gate {name} sha256")
    claims = require_object(release.get("claims"), "release claims")
    if claims != {
        "signing": "not-performed",
        "vulnerabilityReview": "not-performed",
        "networkIsolation": "not-enforced",
        "packageTests": "not-run-by-packager",
    }:
        fail("POLICY_WEAKENED", "release claims were weakened or changed")
    promotion = require_object(release.get("promotion"), "release promotion")
    if promotion != {
        "requiredAttestation": "protected-release-validator",
        "status": "not-performed",
    }:
        fail("POLICY_WEAKENED", "release promotion boundary differs")
    raw_artifacts = release.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != 4:
        fail(
            "MEMBERSHIP_MALFORMED",
            "release manifest must contain exactly four artifacts",
        )
    artifacts: dict[str, dict[str, Any]] = {}
    identities: set[tuple[Any, ...]] = set()
    for raw in raw_artifacts:
        artifact = require_object(raw, "release artifact")
        if set(artifact) != {
            "path",
            "kind",
            "implementation",
            "platform",
            "byteLength",
            "sha256",
        }:
            fail("EVIDENCE_MALFORMED", "release artifact has an unsupported shape")
        name = require_string(artifact.get("path"), "artifact path")
        validate_basename(name, "artifact path")
        if name in artifacts:
            fail("MEMBERSHIP_DUPLICATE", f"duplicate release artifact: {name}")
        if (
            not isinstance(artifact.get("byteLength"), int)
            or artifact["byteLength"] < 1
        ):
            fail("EVIDENCE_MALFORMED", f"invalid artifact byte length: {name}")
        require_sha(artifact.get("sha256"), f"artifact sha256 {name}")
        identity = (
            artifact.get("implementation"),
            artifact.get("kind"),
            artifact.get("platform"),
        )
        if identity in identities:
            fail("MEMBERSHIP_DUPLICATE", f"duplicate artifact role: {identity}")
        identities.add(identity)
        artifacts[name] = artifact
    expected_names = {
        f"openprose-prose-cli-rust-{version}-{expected_platform}.tar.gz": (
            "rust",
            "standalone-archive",
            expected_platform,
        ),
        f"openprose-prose-cli-bun-{version}-{expected_platform}.tar.gz": (
            "bun",
            "standalone-archive",
            expected_platform,
        ),
        f"openprose-prose-cli-{version}.tgz": ("bun", "npm-meta", None),
        f"openprose-prose-cli-{expected_platform}-{version}.tgz": (
            "bun",
            "npm-platform",
            expected_platform,
        ),
    }
    if set(artifacts) != set(expected_names):
        fail(
            "MEMBERSHIP_MALFORMED", "artifact names do not match version/platform roles"
        )
    for name, role in expected_names.items():
        artifact = artifacts[name]
        if (artifact["implementation"], artifact["kind"], artifact["platform"]) != role:
            fail("IDENTITY_DIVERGENCE", f"artifact role mismatch: {name}")
    if release.get("windowsJobObjectReleaseAdmission") is not False:
        fail(
            "IDENTITY_DIVERGENCE",
            "package evidence must not claim Windows Job Object admission",
        )
    sidecar = release.get("windowsProcessHost")
    if expected_platform.startswith("win32-"):
        host = require_object(sidecar, "Windows process host")
        if set(host) != {"path", "byteLength", "sha256", "admission"}:
            fail(
                "EVIDENCE_MALFORMED",
                "Windows process host record has an unsupported shape",
            )
        if (
            host.get("path") != "openprose-windows-process-host.exe"
            or not isinstance(host.get("byteLength"), int)
            or host["byteLength"] < 1
            or host.get("admission") is not False
        ):
            fail(
                "IDENTITY_DIVERGENCE", "Windows process host identity/admission differs"
            )
        require_sha(host.get("sha256"), "Windows process host sha256")
    elif sidecar != "not-applicable":
        fail("IDENTITY_DIVERGENCE", "non-Windows package has a Windows sidecar record")
    return release, artifacts


def validate_dependency_evidence(value: Any) -> dict[str, Any]:
    dependency = require_object(value, "dependency-evidence.json")
    require_exact_keys(
        dependency,
        {"schema", "generator", "sources", "inventories", "authority", "releasePolicy"},
        "dependency evidence",
    )
    if dependency.get("schema") != DEPENDENCY_SCHEMA:
        fail("EVIDENCE_MALFORMED", "dependency evidence has an unsupported schema")
    generator = require_object(dependency.get("generator"), "dependency generator")
    if generator != {
        "name": "openprose-dependency-evidence",
        "version": 1,
        "providerFree": True,
        "networkUsed": False,
    }:
        fail("EVIDENCE_MALFORMED", "dependency generator boundary differs")

    sources = dependency.get("sources")
    if not isinstance(sources, list) or not sources:
        fail("EVIDENCE_MALFORMED", "dependency sources must be a nonempty array")
    source_paths: list[str] = []
    portable_paths: set[str] = set()
    for raw in sources:
        source = require_object(raw, "dependency source")
        require_exact_keys(
            source, {"path", "byteLength", "sha256"}, "dependency source"
        )
        path = require_string(source.get("path"), "dependency source path")
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or "\\" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            fail("EVIDENCE_MALFORMED", f"unsafe dependency source path: {path!r}")
        portable = path.casefold()
        if portable in portable_paths:
            fail("EVIDENCE_DUPLICATE", f"duplicate dependency source: {path}")
        portable_paths.add(portable)
        source_paths.append(path)
        if (
            isinstance(source.get("byteLength"), bool)
            or not isinstance(source.get("byteLength"), int)
            or source["byteLength"] < 1
        ):
            fail("EVIDENCE_MALFORMED", f"invalid dependency source length: {path}")
        require_sha(source.get("sha256"), f"dependency source sha256 {path}")
    if tuple(source_paths) not in (EXPECTED_DEPENDENCY_SOURCES, EXPECTED_DEPENDENCY_SOURCES + ("harnesses/agents-sdk/requirements-build.txt",)):
        fail(
            "EVIDENCE_MALFORMED",
            "dependency sources differ from the closed stable source set",
        )

    inventories = require_object(
        dependency.get("inventories"), "dependency inventories"
    )
    require_exact_keys(
        inventories,
        {"bun", "cargo", "windowsProcessHostCargo"} | ({"agentsSdkPython"} if "agentsSdkPython" in inventories else set()),
        "dependency inventories",
    )
    inventory_shapes = {
        "bun": (
            {"lockfileVersion", "scopeBasis", "packages"},
            None,
            None,
            1,
            "package-manifest-direct-kind-plus-lockfile-reachability",
        ),
        "cargo": (
            {"component", "target", "lockfileVersion", "scopeBasis", "packages"},
            "rust-cli",
            "multi-platform",
            4,
            "workspace-manifest-direct-kind-plus-lockfile-reachability",
        ),
        "windowsProcessHostCargo": (
            {"component", "target", "lockfileVersion", "scopeBasis", "packages"},
            "windows-process-host",
            "windows",
            4,
            "package-manifest-direct-kind-plus-lockfile-reachability",
        ),
    }
    if "agentsSdkPython" in inventories:
        inventory_shapes["agentsSdkPython"] = ({"scopeBasis", "packages"}, None, None, None,
                                             "hash-locked-four-platform-wheel-candidates")
    allowed_scopes = {"runtime", "development", "build", "workspace", "component", "frozen-sdk-build"}
    for inventory_name, (
        keys,
        expected_component,
        expected_target,
        expected_lock_version,
        expected_scope_basis,
    ) in inventory_shapes.items():
        inventory = require_object(inventories.get(inventory_name), inventory_name)
        require_exact_keys(inventory, keys, f"dependency inventory {inventory_name}")
        if (
            (
                expected_lock_version is not None
                and (
                    inventory.get("lockfileVersion") != expected_lock_version
                    or isinstance(inventory.get("lockfileVersion"), bool)
                )
            )
            or inventory.get("scopeBasis") != expected_scope_basis
        ):
            fail(
                "EVIDENCE_MALFORMED",
                f"lockfile/scope basis differs for {inventory_name}",
            )
        if expected_component is not None and (
            inventory.get("component") != expected_component
            or inventory.get("target") != expected_target
        ):
            fail(
                "IDENTITY_DIVERGENCE",
                f"component provenance differs for {inventory_name}",
            )
        packages = inventory.get("packages")
        if not isinstance(packages, list) or not packages:
            fail(
                "EVIDENCE_MALFORMED", f"packages must be nonempty for {inventory_name}"
            )
        identities: list[tuple[str, str, str]] = []
        for raw_package in packages:
            package = require_object(raw_package, f"package in {inventory_name}")
            require_exact_keys(
                package,
                {"name", "version", "source", "integrity", "scopes"},
                f"package in {inventory_name}",
            )
            name = require_string(package.get("name"), "dependency package name")
            version = require_string(
                package.get("version"), "dependency package version"
            )
            source = require_string(package.get("source"), "dependency package source")
            python_package = inventory_name == "agentsSdkPython"
            valid_version = (
                len(version) <= 128 and PYTHON_VERSION_RE.fullmatch(version) is not None
                if python_package else is_exact_semver(version)
            )
            if (
                DEPENDENCY_NAME_RE.fullmatch(name) is None
                or ".." in name
                or not valid_version
                or len(source) > 1024
                or any(ord(character) < 0x20 for character in source)
            ):
                fail("EVIDENCE_MALFORMED", "dependency package identity is unsafe")
            identity = (name, version, source)
            if identity in identities:
                fail(
                    "EVIDENCE_DUPLICATE",
                    f"duplicate package in {inventory_name}: {identity}",
                )
            identities.append(identity)
            scopes = package.get("scopes")
            if (
                not isinstance(scopes, list)
                or not scopes
                or scopes != sorted(set(scopes))
                or any(scope not in allowed_scopes for scope in scopes)
            ):
                fail("EVIDENCE_MALFORMED", f"invalid scopes for {identity}")
            integrity = require_object(package.get("integrity"), "dependency integrity")
            if integrity.get("status") == "declared":
                require_exact_keys(
                    integrity, {"status", "algorithm", "digest"}, "declared integrity"
                )
                algorithm = integrity.get("algorithm")
                digest = integrity.get("digest")
                expected_length = {"sha256": 64, "sha512": 128}.get(algorithm)
                if (
                    expected_length is None
                    or not isinstance(digest, str)
                    or len(digest) != expected_length
                    or re.fullmatch(r"[0-9a-f]+", digest) is None
                ):
                    fail(
                        "EVIDENCE_MALFORMED",
                        f"invalid declared integrity for {identity}",
                    )
            elif integrity.get("status") == "not-applicable":
                require_exact_keys(
                    integrity, {"status", "reason"}, "not-applicable integrity"
                )
                require_string(integrity.get("reason"), "integrity reason")
            else:
                fail(
                    "EVIDENCE_MALFORMED", f"unsupported integrity status for {identity}"
                )
            if python_package and (
                scopes != ["frozen-sdk-build"]
                or integrity.get("status") != "declared"
                or integrity.get("algorithm") != "sha256"
                or source != "pypi:wheel-sha256:" + integrity.get("digest", "")
            ):
                fail("EVIDENCE_MALFORMED", "SDK Python wheel provenance differs")
        if identities != sorted(identities):
            fail("EVIDENCE_MALFORMED", f"packages must be sorted for {inventory_name}")

    authority = require_object(dependency.get("authority"), "dependency authority")
    require_exact_keys(
        authority, {"licenses", "vulnerabilities", "signing"}, "dependency authority"
    )
    expected_authority = {
        "licenses": "unknown",
        "vulnerabilities": "not-performed",
        "signing": "not-performed",
    }
    for name, expected_status in expected_authority.items():
        claim = require_object(authority.get(name), f"dependency authority {name}")
        require_exact_keys(claim, {"status", "reason"}, f"dependency authority {name}")
        if claim.get("status") != expected_status:
            fail("POLICY_WEAKENED", f"dependency {name} authority was overstated")
        require_string(claim.get("reason"), f"dependency authority reason {name}")

    policy = require_object(
        dependency.get("releasePolicy"), "dependency release policy"
    )
    require_exact_keys(
        policy,
        {"schema", "boundary", "passed", "blockers"},
        "dependency release policy",
    )
    blockers = policy.get("blockers")
    if (
        policy.get("schema") != "openprose.dependency-release-policy/1"
        or policy.get("boundary") != "inventory-only"
        or policy.get("passed") is not False
        or not isinstance(blockers, list)
        or not blockers
        or any(not isinstance(item, str) or not item for item in blockers)
    ):
        fail("POLICY_WEAKENED", "dependency evidence cannot satisfy release policy")
    return dependency


def validate_sbom(
    value: Any,
    artifacts: Mapping[str, Mapping[str, Any]],
    dependency_sha256: str,
    dependency: Mapping[str, Any],
) -> None:
    sbom = require_object(value, "sbom.cdx.json")
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5":
        fail("EVIDENCE_MALFORMED", "unsupported SBOM identity")
    components = sbom.get("components")
    if not isinstance(components, list):
        fail("EVIDENCE_MALFORMED", "SBOM components must be an array")
    for name, artifact in artifacts.items():
        matches = [
            item
            for item in components
            if isinstance(item, dict) and item.get("name") == name
        ]
        if len(matches) != 1:
            fail("IDENTITY_DIVERGENCE", f"SBOM must bind artifact exactly once: {name}")
        hashes = matches[0].get("hashes")
        expected = {"alg": "SHA-256", "content": artifact["sha256"]}
        if not isinstance(hashes, list) or expected not in hashes:
            fail("IDENTITY_DIVERGENCE", f"SBOM digest differs for {name}")
    properties = sbom.get("properties")
    if not isinstance(properties, list):
        fail("EVIDENCE_MALFORMED", "SBOM properties must be an array")
    observed: dict[str, str] = {}
    for raw in properties:
        item = require_object(raw, "SBOM property")
        require_exact_keys(item, {"name", "value"}, "SBOM property")
        name = require_string(item.get("name"), "SBOM property name")
        value = require_string(item.get("value"), "SBOM property value")
        if name in observed:
            fail("EVIDENCE_DUPLICATE", f"duplicate SBOM property: {name}")
        observed[name] = value
    if (
        observed.get("openprose:dependency-inventory") != "component-inventory-attached"
        or observed.get("openprose:dependency-evidence-sha256") != dependency_sha256
        or observed.get("openprose:vulnerability-review") != "not-performed"
    ):
        fail("IDENTITY_DIVERGENCE", "SBOM dependency evidence binding differs")

    inventory_groups = {
        "bun": "bun-cli",
        "cargo": "rust-cli",
        "windowsProcessHostCargo": "windows-process-host",
    }
    if "agentsSdkPython" in dependency.get("inventories", {}):
        inventory_groups["agentsSdkPython"] = "agents-sdk-python"
    expected_packages: set[
        tuple[str, str, str, str, str, str, str | None, str | None]
    ] = set()
    inventories = require_object(
        dependency.get("inventories"), "dependency inventories"
    )
    for inventory_name, group in inventory_groups.items():
        inventory = require_object(inventories.get(inventory_name), inventory_name)
        for package in inventory["packages"]:
            integrity = package["integrity"]
            expected_packages.add(
                (
                    group,
                    package["name"],
                    package["version"],
                    package["source"],
                    ",".join(package["scopes"]),
                    integrity["status"],
                    integrity.get("algorithm"),
                    integrity.get("digest"),
                )
            )
    observed_packages: set[
        tuple[str, str, str, str, str, str, str | None, str | None]
    ] = set()
    bom_refs: set[str] = set()
    for raw in components:
        if not isinstance(raw, dict):
            fail("EVIDENCE_MALFORMED", "SBOM component must be an object")
        raw_properties = raw.get("properties", [])
        if not isinstance(raw_properties, list):
            fail("EVIDENCE_MALFORMED", "SBOM component properties must be an array")
        component_properties: dict[str, str] = {}
        for raw_property in raw_properties:
            if not isinstance(raw_property, dict):
                fail("EVIDENCE_MALFORMED", "SBOM component property must be an object")
            name = raw_property.get("name")
            property_value = raw_property.get("value")
            if not isinstance(name, str) or not isinstance(property_value, str):
                fail(
                    "EVIDENCE_MALFORMED", "SBOM component property must contain strings"
                )
            if name in component_properties:
                fail("EVIDENCE_DUPLICATE", f"duplicate SBOM component property: {name}")
            component_properties[name] = property_value
        if component_properties.get("openprose:kind") != "resolved-dependency":
            continue
        component_keys = {"type", "bom-ref", "group", "name", "version", "properties"}
        if component_properties.get("openprose:integrity-status") == "declared":
            component_keys.add("hashes")
        if set(raw) != component_keys:
            fail(
                "EVIDENCE_MALFORMED",
                "SBOM dependency component has an unsupported shape",
            )
        expected_property_names = {
            "openprose:kind",
            "openprose:component",
            "openprose:source",
            "openprose:scopes",
            "openprose:integrity-status",
        }
        if (
            set(component_properties) != expected_property_names
            or raw.get("type") != "library"
        ):
            fail(
                "EVIDENCE_MALFORMED",
                "SBOM dependency component has an unsupported shape",
            )
        bom_ref = require_string(raw.get("bom-ref"), "SBOM dependency bom-ref")
        if not bom_ref.startswith("openprose:dependency:") or any(
            ord(character) < 0x21 for character in bom_ref
        ):
            fail("EVIDENCE_MALFORMED", "SBOM dependency bom-ref is unsafe")
        if bom_ref in bom_refs:
            fail("EVIDENCE_DUPLICATE", f"duplicate SBOM dependency bom-ref: {bom_ref}")
        bom_refs.add(bom_ref)
        group = require_string(raw.get("group"), "SBOM dependency group")
        if component_properties["openprose:component"] != group:
            fail("IDENTITY_DIVERGENCE", "SBOM dependency group/component differs")
        name = require_string(raw.get("name"), "SBOM dependency name")
        version = require_string(raw.get("version"), "SBOM dependency version")
        status = component_properties["openprose:integrity-status"]
        algorithm: str | None = None
        digest: str | None = None
        hashes = raw.get("hashes")
        if status == "declared":
            if (
                not isinstance(hashes, list)
                or len(hashes) != 1
                or not isinstance(hashes[0], dict)
            ):
                fail(
                    "EVIDENCE_MALFORMED", "declared SBOM dependency must have one hash"
                )
            hash_record = hashes[0]
            if set(hash_record) != {"alg", "content"}:
                fail(
                    "EVIDENCE_MALFORMED",
                    "SBOM dependency hash has an unsupported shape",
                )
            algorithm = {"SHA-256": "sha256", "SHA-512": "sha512"}.get(
                hash_record.get("alg")
            )
            digest = hash_record.get("content")
            if algorithm is None or not isinstance(digest, str):
                fail("EVIDENCE_MALFORMED", "SBOM dependency hash is unsupported")
        elif status == "not-applicable":
            if "hashes" in raw:
                fail(
                    "EVIDENCE_MALFORMED",
                    "not-applicable SBOM dependency must not have hashes",
                )
        else:
            fail(
                "EVIDENCE_MALFORMED", "SBOM dependency integrity status is unsupported"
            )
        identity = (
            group,
            name,
            version,
            component_properties["openprose:source"],
            component_properties["openprose:scopes"],
            status,
            algorithm,
            digest,
        )
        if identity in observed_packages:
            fail(
                "EVIDENCE_DUPLICATE", f"duplicate SBOM dependency component: {identity}"
            )
        observed_packages.add(identity)
    if observed_packages != expected_packages:
        fail(
            "IDENTITY_DIVERGENCE",
            "SBOM dependency components differ from dependency evidence",
        )


def validate_provenance(
    value: Any,
    artifacts: Mapping[str, Mapping[str, Any]],
    dependency_sha256: str,
) -> None:
    provenance = require_object(value, "provenance.json")
    require_exact_keys(
        provenance,
        {"_type", "subject", "predicateType", "predicate"},
        "provenance.json",
    )
    if (
        provenance.get("_type") != "https://in-toto.io/Statement/v1"
        or provenance.get("predicateType") != "https://slsa.dev/provenance/v1"
    ):
        fail("EVIDENCE_MALFORMED", "unsupported provenance identity")
    subjects = provenance.get("subject")
    if not isinstance(subjects, list):
        fail("EVIDENCE_MALFORMED", "provenance subject must be an array")
    observed: dict[str, str] = {}
    for subject in subjects:
        item = require_object(subject, "provenance subject")
        name = require_string(item.get("name"), "provenance subject name")
        digest_value = require_object(item.get("digest"), "provenance subject digest")
        digest = require_sha(digest_value.get("sha256"), "provenance subject sha256")
        if name in observed:
            fail("EVIDENCE_DUPLICATE", f"duplicate provenance subject: {name}")
        observed[name] = digest
    if observed != {name: artifact["sha256"] for name, artifact in artifacts.items()}:
        fail("IDENTITY_DIVERGENCE", "provenance subjects differ from release artifacts")
    predicate = require_object(provenance.get("predicate"), "provenance predicate")
    definition = require_object(
        predicate.get("buildDefinition"), "provenance buildDefinition"
    )
    dependencies = definition.get("resolvedDependencies")
    if not isinstance(dependencies, list):
        fail("EVIDENCE_MALFORMED", "provenance resolvedDependencies must be an array")
    matches = []
    for raw in dependencies:
        item = require_object(raw, "provenance resolved dependency")
        uri = require_string(item.get("uri"), "provenance dependency URI")
        digest = require_object(item.get("digest"), "provenance dependency digest")
        sha = require_sha(digest.get("sha256"), "provenance dependency sha256")
        if uri == "openprose:dependency-evidence":
            matches.append(sha)
    if matches != [dependency_sha256]:
        fail("IDENTITY_DIVERGENCE", "provenance dependency evidence binding differs")


def verify_package_output(
    package_output: Path,
    expected_platform: str | None = None,
    *,
    purpose: str = "mock-benchmark",
) -> dict[str, Any]:
    try:
        metadata = package_output.lstat()
    except OSError as error:
        fail("INPUT_UNAVAILABLE", f"cannot inspect package output: {error}")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("INPUT_UNSAFE", "package output must be a non-symlink directory")
    package_output = package_output.resolve()
    platform_value = expected_platform or current_platform_id()
    if platform_value not in SUPPORTED_PLATFORMS:
        fail("PLATFORM_UNSUPPORTED", f"unsupported expected platform: {platform_value}")
    entries: dict[str, Path] = {}
    portable_entries: set[str] = set()
    for index, path in enumerate(package_output.iterdir()):
        if index >= MAX_PACKAGE_OUTPUT_ENTRIES:
            fail("MEMBERSHIP_MALFORMED", "package output has too many members")
        validate_basename(path.name, "package output member")
        portable = path.name.casefold()
        if portable in portable_entries:
            fail(
                "MEMBERSHIP_DUPLICATE",
                f"duplicate portable package member: {path.name}",
            )
        portable_entries.add(portable)
        observed = path.lstat()
        if not stat.S_ISREG(observed.st_mode):
            fail("INPUT_UNSAFE", f"package output member is not regular: {path.name}")
        if path.name in entries:
            fail("MEMBERSHIP_DUPLICATE", f"duplicate package member: {path.name}")
        entries[path.name] = path
    sums_path = entries.get("SHA256SUMS")
    if sums_path is None:
        fail("MEMBERSHIP_MALFORMED", "package output lacks SHA256SUMS")
    sums_bytes = safe_read(sums_path, MAX_EVIDENCE_BYTES)
    sums = parse_sha_sums(sums_bytes)
    if set(sums) != set(entries) - {"SHA256SUMS"}:
        fail(
            "MEMBERSHIP_MALFORMED",
            "SHA256SUMS membership differs from package directory",
        )
    encoded: dict[str, bytes] = {}
    for name, expected_digest in sums.items():
        maximum = (
            MAX_ARCHIVE_BYTES
            if name.endswith((".tgz", ".tar.gz"))
            else MAX_EVIDENCE_BYTES
        )
        value = safe_read(entries[name], maximum)
        if sha256_bytes(value) != expected_digest:
            fail("CHECKSUM_MISMATCH", f"SHA256SUMS mismatch: {name}")
        encoded[name] = value
    release_encoded = encoded.get("release-manifest.json")
    if release_encoded is None:
        fail("MEMBERSHIP_MALFORMED", "release-manifest.json is required")
    release, artifacts = validate_release_manifest(
        json_no_duplicates(release_encoded, "release-manifest.json"),
        platform_value,
        purpose,
    )
    evidence_names = set(encoded) - set(artifacts)
    if evidence_names != REQUIRED_EVIDENCE:
        fail(
            "MEMBERSHIP_MALFORMED",
            "package evidence membership is not the closed supported set",
        )
    for name, artifact in artifacts.items():
        if (
            artifact["byteLength"] != len(encoded[name])
            or artifact["sha256"] != sums[name]
        ):
            fail(
                "IDENTITY_DIVERGENCE", f"release artifact digest/length differs: {name}"
            )
    dependency_encoded = encoded["dependency-evidence.json"]
    dependency_digest = sha256_bytes(dependency_encoded)
    dependency_record = require_object(
        release.get("dependencyEvidence"), "release dependency evidence"
    )
    require_exact_keys(
        dependency_record,
        {"path", "byteLength", "sha256", "releasePolicyPassed"},
        "release dependency evidence",
    )
    if dependency_record != {
        "path": "dependency-evidence.json",
        "byteLength": len(dependency_encoded),
        "sha256": dependency_digest,
        "releasePolicyPassed": False,
    }:
        fail("IDENTITY_DIVERGENCE", "release dependency evidence binding differs")
    dependency = validate_dependency_evidence(
        json_no_duplicates(dependency_encoded, "dependency-evidence.json")
    )
    validate_sbom(
        json_no_duplicates(encoded["sbom.cdx.json"], "sbom.cdx.json"),
        artifacts,
        dependency_digest,
        dependency,
    )
    validate_provenance(
        json_no_duplicates(encoded["provenance.json"], "provenance.json"),
        artifacts,
        dependency_digest,
    )
    if release['mode'] in ('release', 'kernel-rc'):
        import sys
        ci_path = str(Path(__file__).resolve().parents[2] / 'ci')
        if ci_path not in sys.path:
            sys.path.insert(0, ci_path)
        import kernel_rc_evidence as custody
        try:
            sdk = custody.validate_sdk_archives(release, lambda name: {
                path: data for path, (data, mode) in decode_archive_members(encoded[name], name).items()})
        except (ValueError, KeyError, TypeError) as error:
            fail('IDENTITY_DIVERGENCE', str(error))
        if 'agentsSdkPython' not in dependency['inventories']:
            fail('IDENTITY_DIVERGENCE', 'Release lacks SDK Python dependency inventory')
        source = next((p for p in dependency['sources'] if p['path'] == 'harnesses/agents-sdk/requirements-build.txt'), {})
        if source.get('sha256') != sdk['dependencyLockSha256']:
            fail('IDENTITY_DIVERGENCE', 'Release SDK lock differs from dependency source')
        provenance = json_no_duplicates(encoded['provenance.json'], 'provenance.json')
        definition = provenance['predicate']['buildDefinition']
        if definition.get('externalParameters', {}).get('agentsSdk') != sdk:
            fail('IDENTITY_DIVERGENCE', 'Provenance SDK identity differs')
        for uri, digest in (('openprose:agents-sdk-helper', sdk['sha256']),
                            ('openprose:agents-sdk-build-receipt', sdk['receiptSha256']),
                            ('openprose:agents-sdk-python-lock', sdk['dependencyLockSha256'])):
            matches = [p for p in definition['resolvedDependencies'] if p.get('uri') == uri]
            if matches != [{'uri': uri, 'digest': {'sha256': digest}}]:
                fail('IDENTITY_DIVERGENCE', 'Provenance SDK dependency differs: ' + uri)
        sbom = json_no_duplicates(encoded['sbom.cdx.json'], 'sbom.cdx.json')
        matches = [p for p in sbom['components'] if p.get('name') == 'prose-agents-sdk']
        if len(matches) != 1 or matches[0].get('hashes') != [{'alg': 'SHA-256', 'content': sdk['sha256']}]:
            fail('IDENTITY_DIVERGENCE', 'SBOM lacks exact frozen SDK helper binding')
        native_receipts = []
        for name, artifact in artifacts.items():
            if artifact['kind'] not in ('standalone-archive', 'npm-platform'):
                continue
            members = decode_archive_members(encoded[name], name)
            native_receipts.extend(json_no_duplicates(data, 'SDK receipt') for path, (data, mode) in members.items()
                                   if path.endswith('/agents-sdk-build.json'))
        try:
            if len(native_receipts) != 3 or any(row != native_receipts[0] for row in native_receipts[1:]):
                raise ValueError('SDK native receipts differ between payloads')
            custody.validate_sdk_native_sbom(sbom['components'], native_receipts[0])
        except (ValueError, KeyError, TypeError) as error:
            fail('IDENTITY_DIVERGENCE', str(error))
    else:
        # Native components cannot appear without an artifact-bound production receipt.
        import sys
        ci_path = str(Path(__file__).resolve().parents[2] / 'ci')
        if ci_path not in sys.path:
            sys.path.insert(0, ci_path)
        import kernel_rc_evidence as custody
        try:
            custody.validate_sdk_native_sbom(json_no_duplicates(encoded['sbom.cdx.json'], 'sbom.cdx.json')['components'])
        except (ValueError, KeyError, TypeError) as error:
            fail('IDENTITY_DIVERGENCE', str(error))
    return {
        "root": package_output,
        "purpose": purpose,
        "platform": platform_value,
        "release": release,
        "artifacts": artifacts,
        "encoded": encoded,
        "checksums": sums,
        "evidence": [
            evidence_record(name, encoded[name]) for name in sorted(evidence_names)
        ]
        + [evidence_record("SHA256SUMS", sums_bytes)],
    }


def assert_package_output_unchanged(context: Mapping[str, Any]) -> None:
    observed = verify_package_output(
        context["root"],
        expected_platform=context["platform"],
        purpose=context.get("purpose", "mock-benchmark"),
    )
    if (
        observed["checksums"] != context["checksums"]
        or set(observed["encoded"]) != set(context["encoded"])
        or any(
            observed["encoded"][name] != encoded
            for name, encoded in context["encoded"].items()
        )
    ):
        fail("INPUT_MUTATED", "package output changed during benchmark execution")


def snapshot_verified_artifacts(
    context: Mapping[str, Any], destination: Path, names: Sequence[str]
) -> dict[str, Path]:
    if destination.exists():
        fail("INSTALL_ROOT_UNSAFE", "artifact snapshot destination already exists")
    destination.mkdir(mode=0o700)
    snapshots: dict[str, Path] = {}
    for name in names:
        encoded = context["encoded"][name]
        target = destination / name
        try:
            with target.open("xb") as output:
                output.write(encoded)
            target.chmod(0o400)
        except OSError as error:
            fail("INSTALL_FAILED", f"cannot snapshot verified artifact {name}: {error}")
        if safe_read(target, MAX_ARCHIVE_BYTES) != encoded:
            fail("INPUT_MUTATED", f"owned artifact snapshot differs: {name}")
        snapshots[name] = target
    return snapshots


def safe_member_name(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        fail("ARCHIVE_UNSAFE", f"archive member has an unsafe path: {name!r}")
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        fail("ARCHIVE_UNSAFE", f"archive member has an unsafe path: {name!r}")
    return path


def decode_exact_gzip_tar(archive_bytes: bytes, label: str) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(archive_bytes, MAX_EXTRACTED_BYTES + 1)
        if decoder.unconsumed_tail or len(decoded) > MAX_EXTRACTED_BYTES:
            fail(
                "ARCHIVE_OVERSIZE",
                f"archive expands beyond {MAX_EXTRACTED_BYTES} bytes",
            )
        decoded += decoder.flush(MAX_EXTRACTED_BYTES + 1 - len(decoded))
    except zlib.error as error:
        fail("ARCHIVE_MALFORMED", f"cannot decompress archive {label}: {error}")
    if len(decoded) > MAX_EXTRACTED_BYTES:
        fail("ARCHIVE_OVERSIZE", f"archive expands beyond {MAX_EXTRACTED_BYTES} bytes")
    if not decoder.eof or decoder.unused_data:
        fail(
            "ARCHIVE_MALFORMED",
            f"archive has incomplete or trailing gzip bytes: {label}",
        )
    offset = 0
    terminator = None
    while offset + 512 <= len(decoded):
        header = decoded[offset : offset + 512]
        if header == bytes(512):
            if offset + 1024 > len(decoded) or decoded[
                offset + 512 : offset + 1024
            ] != bytes(512):
                fail(
                    "ARCHIVE_MALFORMED",
                    f"archive lacks the canonical tar terminator: {label}",
                )
            terminator = offset + 1024
            break
        raw_size = header[124:136].rstrip(b"\x00 ")
        try:
            size = int(raw_size or b"0", 8)
        except ValueError:
            fail("ARCHIVE_MALFORMED", f"archive has an invalid tar size field: {label}")
        offset += 512 + ((size + 511) // 512) * 512
    if terminator is None or any(decoded[terminator:]):
        fail("ARCHIVE_MALFORMED", f"archive has opaque trailing tar bytes: {label}")
    return decoded


def decode_archive_members(
    archive_bytes: bytes, label: str
) -> dict[str, tuple[bytes, int]]:
    decoded = decode_exact_gzip_tar(archive_bytes, label)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(decoded), mode="r:")
    except (tarfile.TarError, OSError) as error:
        fail("ARCHIVE_MALFORMED", f"cannot read archive {label}: {error}")
    values: dict[str, tuple[bytes, int]] = {}
    portable_names: set[str] = set()
    total = 0
    try:
        for index, member in enumerate(archive):
            if index >= MAX_MEMBERS:
                fail(
                    "ARCHIVE_OVERSIZE",
                    f"archive has more than {MAX_MEMBERS} members: {label}",
                )
            member_path = safe_member_name(member.name)
            portable = member_path.as_posix().casefold()
            if portable in portable_names:
                fail(
                    "ARCHIVE_DUPLICATE",
                    f"duplicate portable archive member: {member.name}",
                )
            portable_names.add(portable)
            if not member.isfile():
                fail(
                    "ARCHIVE_UNSAFE",
                    f"archive member is not a regular file: {member.name}",
                )
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                fail("ARCHIVE_OVERSIZE", f"archive member is oversized: {member.name}")
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                fail(
                    "ARCHIVE_OVERSIZE",
                    f"archive expands beyond {MAX_EXTRACTED_BYTES} bytes",
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                fail("ARCHIVE_MALFORMED", f"cannot read archive member: {member.name}")
            data = extracted.read(member.size + 1)
            if len(data) != member.size:
                fail("ARCHIVE_MALFORMED", f"archive member size differs: {member.name}")
            values[member_path.as_posix()] = (data, member.mode & 0o777)
    finally:
        archive.close()
    return values


def read_archive_members(path: Path) -> dict[str, tuple[bytes, int]]:
    return decode_archive_members(safe_read(path, MAX_ARCHIVE_BYTES), path.name)


def extract_members(
    members: Mapping[str, tuple[bytes, int]], destination: Path
) -> None:
    if destination.exists():
        fail(
            "INSTALL_ROOT_UNSAFE",
            f"owned extraction destination already exists: {destination}",
        )
    destination.mkdir(mode=0o700)
    for name in sorted(members):
        data, mode = members[name]
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as output:
                output.write(data)
            target.chmod(mode)
        except OSError as error:
            fail("INSTALL_FAILED", f"cannot extract owned member {name}: {error}")


def expected_standalone_members(
    artifact_name: str, platform_value: str
) -> tuple[str, set[str]]:
    root_name = artifact_name[: -len(".tar.gz")]
    executable = "prose.exe" if platform_value.startswith("win32-") else "prose"
    names = {
        f"{root_name}/{executable}",
        f"{root_name}/LICENSE",
        f"{root_name}/README.txt",
        f"{root_name}/examples/hello.prose.md",
    }
    if platform_value.startswith("win32-"):
        names.add(f"{root_name}/openprose-windows-process-host.exe")
    return root_name, names


def expected_npm_cohort(release: Mapping[str, Any]) -> dict[str, Any]:
    mode = release["mode"]
    admitted = (
        [name for name in SUPPORTED_PLATFORMS if not name.startswith("win32-")]
        if mode == "alpha"
        else list(SUPPORTED_PLATFORMS)
    )
    return {
        "schema": NPM_COHORT_SCHEMA,
        "version": release["version"],
        "sourceRevision": release["source"]["revision"],
        "releaseChannel": {
            "development": "development",
            "alpha": "functional-alpha",
            "release": "release-candidate",
        }[mode],
        "purpose": release["image"]["purpose"],
        "image": release["image"],
        "admittedPlatforms": admitted,
        "semanticStatus": "not-applicable" if mode == "alpha" else "unverified",
        "releaseEligible": False,
        "publicationAuthorized": False,
    }


def validate_npm_cohort(value: Any, release: Mapping[str, Any], label: str) -> None:
    cohort = require_object(value, label)
    require_exact_keys(
        cohort,
        {
            "schema",
            "version",
            "sourceRevision",
            "releaseChannel",
            "purpose",
            "image",
            "admittedPlatforms",
            "semanticStatus",
            "releaseEligible",
            "publicationAuthorized",
        },
        label,
    )
    if cohort != expected_npm_cohort(release):
        fail("IDENTITY_DIVERGENCE", f"{label} differs from release evidence")


def validate_canonical_launcher(launcher: bytes, cohort: Mapping[str, Any]) -> None:
    cohort_line = (
        "const COHORT = "
        + json.dumps(cohort, sort_keys=True, separators=(",", ":"))
        + ";"
    ).encode("ascii")
    template_line = b"const COHORT = __OPENPROSE_COHORT__;"
    if launcher.count(cohort_line) != 1:
        fail("HIDDEN_EXECUTION", "npm launcher lacks the exact package cohort binding")
    normalized = launcher.replace(cohort_line, template_line)
    if sha256_bytes(normalized) != CANONICAL_LAUNCHER_TEMPLATE_SHA256:
        fail(
            "HIDDEN_EXECUTION",
            "npm launcher bytes differ from the canonical plain-Node launcher",
        )


def validate_npm_packages(
    context: Mapping[str, Any], bun_binary: bytes
) -> dict[str, Any]:
    release = context["release"]
    version = release["version"]
    platform_value = context["platform"]
    meta_name = f"openprose-prose-cli-{version}.tgz"
    platform_name = f"openprose-prose-cli-{platform_value}-{version}.tgz"
    meta_members = decode_archive_members(context["encoded"][meta_name], meta_name)
    platform_members = decode_archive_members(
        context["encoded"][platform_name], platform_name
    )
    if set(meta_members) != {
        "package/package.json",
        "package/bin/prose.js",
        "package/LICENSE",
        "package/README.md",
        "package/examples/hello.prose.md",
    }:
        fail("MEMBERSHIP_MALFORMED", "npm meta package members are not the closed set")
    if meta_members["package/bin/prose.js"][1] & 0o111 == 0:
        fail("ARCHIVE_UNSAFE", "npm launcher is not executable")
    executable = "prose.exe" if platform_value.startswith("win32-") else "prose"
    expected_platform_members = {
        "package/package.json",
        f"package/bin/{executable}",
        "package/LICENSE",
    }
    if platform_value.startswith("win32-"):
        expected_platform_members.add("package/bin/openprose-windows-process-host.exe")
    if isinstance(release.get('agentsSdk'), dict):
        expected_platform_members.update('package/bin/' + name for name in ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'))
    if set(platform_members) != expected_platform_members:
        fail(
            "MEMBERSHIP_MALFORMED",
            "npm platform package members are not the closed set",
        )
    meta = require_object(
        json_no_duplicates(
            meta_members["package/package.json"][0], "npm meta package.json"
        ),
        "npm meta package.json",
    )
    if meta.get("name") != "@openprose/prose-cli" or meta.get("version") != version:
        fail("IDENTITY_DIVERGENCE", "npm meta package identity differs")
    if "scripts" in meta or meta.get("bin") != {"prose": "bin/prose.js"}:
        fail(
            "HIDDEN_EXECUTION",
            "npm meta package contains scripts or an unexpected launcher",
        )
    expected_cohort = expected_npm_cohort(release)
    validate_npm_cohort(meta.get("openproseCohort"), release, "npm meta cohort")
    if meta.get("engines") != {"node": NODE_ENGINE}:
        fail("IDENTITY_DIVERGENCE", "npm Node engine floor differs")
    launcher_identity = require_object(
        meta.get("openproseLauncher"), "npm launcher identity"
    )
    require_exact_keys(
        launcher_identity, {"path", "byteLength", "sha256"}, "npm launcher identity"
    )
    optional = require_object(
        meta.get("optionalDependencies"), "npm optionalDependencies"
    )
    expected_optional = {
        f"@openprose/prose-cli-{name}": version
        for name in expected_cohort["admittedPlatforms"]
    }
    if optional != expected_optional:
        fail(
            "IDENTITY_DIVERGENCE",
            "npm optional platform packages are not the exact version set",
        )
    launcher = meta_members["package/bin/prose.js"][0]
    if (
        launcher_identity.get("path") != "bin/prose.js"
        or launcher_identity.get("byteLength") != len(launcher)
        or launcher_identity.get("sha256") != sha256_bytes(launcher)
    ):
        fail("IDENTITY_DIVERGENCE", "npm launcher bytes differ from meta manifest")
    validate_canonical_launcher(launcher, expected_cohort)
    platform_manifest_bytes = platform_members["package/package.json"][0]
    platform_manifest = require_object(
        json_no_duplicates(platform_manifest_bytes, "npm platform package.json"),
        "npm platform package.json",
    )
    if "scripts" in platform_manifest:
        fail("HIDDEN_EXECUTION", "npm platform package contains lifecycle scripts")
    validate_npm_cohort(
        platform_manifest.get("openproseCohort"), release, "npm platform cohort"
    )
    if (
        platform_manifest.get("name") != f"@openprose/prose-cli-{platform_value}"
        or platform_manifest.get("version") != version
        or platform_manifest.get("openproseSourceRevision")
        != release["source"]["revision"]
        or platform_manifest.get("openproseImage") != release["image"]
        or platform_manifest.get("openproseBunCompileTarget")
        != release["bunRuntime"]["compileTarget"]
        or platform_manifest.get("openproseBunRuntimeVariant")
        != release["bunRuntime"]["runtimeVariant"]
        or platform_manifest.get("openprosePlatform") != platform_value
    ):
        fail(
            "IDENTITY_DIVERGENCE", "npm platform identity differs from release evidence"
        )
    packaged_binary = platform_members[f"package/bin/{executable}"][0]
    if platform_members[f"package/bin/{executable}"][1] & 0o111 == 0:
        fail("ARCHIVE_UNSAFE", "npm platform binary is not executable")
    if (
        platform_manifest.get("openproseBinary") != f"bin/{executable}"
        or platform_manifest.get("openproseBinaryByteLength") != len(packaged_binary)
        or platform_manifest.get("openproseBinarySha256")
        != sha256_bytes(packaged_binary)
    ):
        fail("IDENTITY_DIVERGENCE", "npm platform binary differs from its manifest")
    if packaged_binary != bun_binary:
        fail(
            "IDENTITY_DIVERGENCE",
            "npm packaged executable differs from standalone Bun executable",
        )
    system, cpu, *libc = platform_value.split("-")
    if platform_manifest.get("os") != [system] or platform_manifest.get("cpu") != [cpu]:
        fail("IDENTITY_DIVERGENCE", "npm OS/CPU selector differs from platform")
    if libc and platform_manifest.get("libc") != ["glibc"]:
        fail("IDENTITY_DIVERGENCE", "npm libc selector differs from platform")
    if not libc and "libc" in platform_manifest:
        fail("IDENTITY_DIVERGENCE", "npm package has an unexpected libc selector")
    linux_keys = {
        "openproseMinimumGlibc",
        "openproseRequiredGlibcMaximum",
        "openproseLinuxExecutionEvidence",
    }
    if libc:
        runtime = release["linuxRuntime"]
        if {
            "openproseMinimumGlibc": platform_manifest.get("openproseMinimumGlibc"),
            "openproseRequiredGlibcMaximum": platform_manifest.get(
                "openproseRequiredGlibcMaximum"
            ),
            "openproseLinuxExecutionEvidence": platform_manifest.get(
                "openproseLinuxExecutionEvidence"
            ),
        } != {
            "openproseMinimumGlibc": runtime["minimumGlibc"],
            "openproseRequiredGlibcMaximum": runtime["requiredGlibcMaximum"]["bun"],
            "openproseLinuxExecutionEvidence": runtime["executionEvidence"],
        }:
            fail(
                "IDENTITY_DIVERGENCE",
                "npm Linux runtime floor differs from release evidence",
            )
    elif linux_keys.intersection(platform_manifest):
        fail("IDENTITY_DIVERGENCE", "non-Linux npm package has a Linux runtime claim")
    if platform_value.startswith("win32-"):
        sidecar = platform_members["package/bin/openprose-windows-process-host.exe"][0]
        host = require_object(release.get("windowsProcessHost"), "Windows process host")
        if (
            platform_manifest.get("openproseWindowsProcessHost")
            != "bin/openprose-windows-process-host.exe"
            or platform_manifest.get("openproseWindowsProcessHostByteLength")
            != len(sidecar)
            or platform_manifest.get("openproseWindowsProcessHostSha256")
            != sha256_bytes(sidecar)
            or platform_manifest.get("openproseWindowsProcessHostAdmission")
            is not False
            or host.get("byteLength") != len(sidecar)
            or host.get("sha256") != sha256_bytes(sidecar)
        ):
            fail(
                "IDENTITY_DIVERGENCE",
                "npm Windows sidecar differs from package evidence",
            )
    elif any(
        key.startswith("openproseWindowsProcessHost") for key in platform_manifest
    ):
        fail("IDENTITY_DIVERGENCE", "non-Windows npm package contains sidecar metadata")
    return {
        "metaName": meta_name,
        "platformName": platform_name,
        "launcher": launcher,
        "platformManifest": platform_manifest,
        "platformManifestBytes": platform_manifest_bytes,
        "binary": packaged_binary,
        "sidecar": platform_members.get(
            "package/bin/openprose-windows-process-host.exe", (None, 0)
        )[0],
    }


class BoundedCapture:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.data = bytearray()
        self.overflow = False
        self.error: str | None = None

    def drain(self, stream: Any) -> None:
        try:
            while True:
                block = stream.read(65536)
                if not block:
                    return
                remaining = self.maximum + 1 - len(self.data)
                if remaining > 0:
                    self.data.extend(block[:remaining])
                if len(self.data) > self.maximum or len(block) > remaining:
                    self.overflow = True
        except OSError as error:
            self.error = str(error)
        finally:
            try:
                stream.close()
            except OSError:
                pass


def group_exists(group: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_owned_process(
    process: subprocess.Popen[bytes], group: int, description: str
) -> bool:
    if os.name == "nt":
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return False
        return process.poll() is not None
    if group != process.pid or group == os.getpgrp():
        fail(
            "PROCESS_IDENTITY_INVALID",
            f"refusing unsafe cleanup target for {description}",
        )

    def reaped() -> bool:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return False
        return process.poll() is not None

    for sent_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sent_signal)
        except ProcessLookupError:
            return reaped()
        except PermissionError:
            if not group_exists(group):
                return reaped()
            return False
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            process.poll()
            if not group_exists(group):
                return reaped()
            time.sleep(0.01)
    direct_reaped = reaped()
    return direct_reaped and not group_exists(group)


def cleanup_after_exception(
    process: subprocess.Popen[bytes],
    group: int,
    readers: Sequence[threading.Thread],
    streams: Sequence[Any],
    description: str,
    original: BaseException,
) -> None:
    cleanup_failure: BaseException | None = None
    try:
        cleaned = cleanup_owned_process(process, group, description)
    except BaseException as cleanup_error:
        cleaned = False
        cleanup_failure = cleanup_error
    for reader in readers:
        reader.join(timeout=1)
    for stream in streams:
        if not stream.closed:
            try:
                stream.close()
            except OSError:
                pass
    for reader in readers:
        reader.join(timeout=0.25)
    if cleanup_failure is not None:
        raise BenchmarkError(
            "CLEANUP_UNVERIFIED",
            f"exceptional {description} cleanup failed: {cleanup_failure}",
        ) from original
    if (
        not cleaned
        or process.poll() is None
        or any(reader.is_alive() for reader in readers)
    ):
        raise BenchmarkError(
            "CLEANUP_UNVERIFIED",
            f"exceptional {description} cleanup could not be verified",
        ) from original


def run_owned_process(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    description: str,
) -> dict[str, Any]:
    if os.name == "nt":
        fail(
            "PLATFORM_UNSUPPORTED",
            "Windows measurement requires native Job Object containment before any process spawn",
        )
    if not argv or not all(isinstance(item, str) and item for item in argv):
        fail("COMMAND_MALFORMED", f"invalid argv for {description}")
    if timeout_seconds <= 0:
        fail("COMMAND_MALFORMED", "timeout must be positive")
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    started = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
    except OSError as error:
        fail("SPAWN_FAILED", f"cannot start {description}: {error}")
    group = process.pid
    _ACTIVE_GROUPS[group] = description
    stdout = BoundedCapture(MAX_PROCESS_OUTPUT)
    stderr = BoundedCapture(MAX_PROCESS_OUTPUT)
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()
    streams = (process.stdout, process.stderr)
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        unsettled = any(reader.is_alive() for reader in readers)
        group_alive = group_exists(group)
        if timed_out or unsettled or group_alive:
            cleaned = cleanup_owned_process(process, group, description)
            for reader in readers:
                reader.join(timeout=1)
            if os.name == "nt":
                fail(
                    "CLEANUP_UNVERIFIED",
                    f"unsettled {description}; Windows descendant cleanup authority is unavailable",
                )
            if not cleaned or any(reader.is_alive() for reader in readers):
                fail(
                    "CLEANUP_UNVERIFIED",
                    f"unsettled {description}; cleanup was not verified",
                )
            fail("PROCESS_UNSETTLED", f"unsettled {description} was terminated")
        if (
            process.poll() is None
            or stdout.error is not None
            or stderr.error is not None
        ):
            fail("PROCESS_UNSETTLED", f"unsettled streams or process for {description}")
        if stdout.overflow or stderr.overflow:
            fail("OUTPUT_OVERSIZE", f"bounded output exceeded for {description}")
        return {
            "exitCode": process.returncode,
            "stdout": bytes(stdout.data),
            "stderr": bytes(stderr.data),
            "wallMs": round((time.perf_counter_ns() - started) / 1_000_000, 6),
            "settlement": "settled",
            "settlementAuthority": (
                "direct-and-original-process-group-settled"
                if os.name != "nt"
                else "direct-process-and-closed-pipes"
            ),
        }
    except BaseException as original:
        cleanup_after_exception(process, group, readers, streams, description, original)
        raise
    finally:
        _ACTIVE_GROUPS.pop(group, None)


def live_owned_probe_descriptions() -> list[str]:
    return sorted(_ACTIVE_GROUPS.values())


def validate_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is None:
        return
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(float(deadline_monotonic))
        or float(deadline_monotonic) <= time.monotonic()
    ):
        fail(
            "ARGUMENT_INVALID",
            "deadline_monotonic must be a finite future monotonic time",
        )


def remaining_before(deadline_monotonic: float | None, stage: str) -> float | None:
    if deadline_monotonic is None:
        return None
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining <= 0:
        fail("DEADLINE_EXCEEDED", f"installed benchmark deadline expired {stage}")
    return remaining


def run_process_before_deadline(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    description: str,
    deadline_monotonic: float | None,
) -> dict[str, Any]:
    remaining = remaining_before(deadline_monotonic, f"before {description}")
    effective_timeout = (
        timeout_seconds if remaining is None else min(timeout_seconds, remaining)
    )
    try:
        outcome = run_owned_process(
            argv, cwd, environment, effective_timeout, description
        )
    except BenchmarkError as error:
        if (
            error.code == "PROCESS_UNSETTLED"
            and deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            fail(
                "DEADLINE_EXCEEDED",
                f"installed benchmark deadline expired during {description}",
            )
        raise
    remaining_before(deadline_monotonic, f"after {description}")
    return outcome


def clean_environment(root: Path, tool_paths: Sequence[Path]) -> dict[str, str]:
    home = root / "home"
    config = root / "config"
    cache = root / "cache"
    temporary = root / "tmp"
    for directory in (home, config, cache, temporary):
        directory.mkdir(parents=True, exist_ok=True)
    path_entries = [str(path) for path in tool_paths]
    path_entries.extend([str(Path(sys.executable).parent), "/usr/bin", "/bin"])
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
        "LANG": "C",
        "LC_ALL": "C",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }


def installed_stat_identity(observed: os.stat_result) -> tuple[int, ...]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def portable_installed_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        try:
            relative = (
                path.resolve(strict=True)
                .relative_to(root.resolve(strict=True))
                .as_posix()
            )
        except (OSError, ValueError):
            fail("INSTALL_UNSAFE", "installed tree member escapes its root")
    safe_member_name(relative)
    if len(relative.encode("utf-8")) > MAX_INSTALLED_TREE_PATH_BYTES:
        fail("INSTALL_UNSAFE", "installed tree member path is oversized")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in relative):
        fail(
            "INSTALL_UNSAFE", "installed tree member path contains a control character"
        )
    return relative


def read_installed_regular(path: Path, expected: os.stat_result) -> tuple[int, str]:
    if expected.st_size > MAX_INSTALLED_TREE_FILE_BYTES:
        fail("INSTALL_OVERSIZE", f"installed regular file is oversized: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    observed_digest = hashlib.sha256()
    length = 0
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                fail(
                    "INSTALL_MUTATED",
                    f"installed member changed type while opened: {path}",
                )
            if installed_stat_identity(expected) != installed_stat_identity(opened):
                fail(
                    "INSTALL_MUTATED",
                    f"installed member changed before reading: {path}",
                )
            for block in iter(lambda: source.read(65_536), b""):
                length += len(block)
                if length > MAX_INSTALLED_TREE_FILE_BYTES:
                    fail(
                        "INSTALL_OVERSIZE",
                        f"installed regular file is oversized: {path}",
                    )
                observed_digest.update(block)
            settled = os.fstat(source.fileno())
    except BenchmarkError:
        raise
    except OSError as error:
        fail("INSTALL_MUTATED", f"cannot read installed regular file {path}: {error}")
    try:
        after = path.lstat()
    except OSError as error:
        fail("INSTALL_MUTATED", f"installed regular file disappeared: {error}")
    expected_identity = installed_stat_identity(expected)
    if (
        length != expected.st_size
        or expected_identity != installed_stat_identity(settled)
        or expected_identity != installed_stat_identity(after)
    ):
        fail("INSTALL_MUTATED", f"installed regular file changed while reading: {path}")
    return length, observed_digest.hexdigest()


def capture_installed_tree(
    root: Path, allowed_symlinks: Mapping[Path, Path] | None = None
) -> dict[str, Any]:
    try:
        root_metadata = root.lstat()
        canonical_root = root.resolve(strict=True)
    except OSError as error:
        fail("INSTALL_UNSAFE", f"cannot inspect installed tree root: {error}")
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        fail("INSTALL_UNSAFE", "installed tree root must be a non-symlink directory")
    allowed: dict[Path, Path] = {}
    for raw_link, raw_target in (allowed_symlinks or {}).items():
        link = raw_link.absolute()
        try:
            link.relative_to(root.absolute())
            target = raw_target.resolve(strict=True)
            target.relative_to(canonical_root)
        except (OSError, ValueError):
            fail("INSTALL_UNSAFE", "allowed installed symlink is outside its tree")
        if link in allowed:
            fail("INSTALL_UNSAFE", "allowed installed symlink is duplicated")
        allowed[link] = target
    seen_allowed: set[Path] = set()
    seen_portable: set[str] = set()
    entries: list[dict[str, Any]] = []
    pending: list[tuple[Path, os.stat_result | None]] = [(root, None)]
    visited_directories: list[tuple[Path, os.stat_result]] = []
    observed_members: list[tuple[Path, os.stat_result]] = []
    total_bytes = 0
    while pending:
        directory, expected_directory = pending.pop()
        try:
            before_directory = directory.lstat()
        except OSError as error:
            fail("INSTALL_MUTATED", f"installed directory disappeared: {error}")
        if not stat.S_ISDIR(before_directory.st_mode) or stat.S_ISLNK(
            before_directory.st_mode
        ):
            fail("INSTALL_UNSAFE", f"installed directory changed type: {directory}")
        if expected_directory is not None and installed_stat_identity(
            before_directory
        ) != installed_stat_identity(expected_directory):
            fail(
                "INSTALL_MUTATED",
                f"installed directory changed before traversal: {directory}",
            )
        visited_directories.append((directory, before_directory))
        try:
            children = []
            with os.scandir(directory) as iterator:
                for child in iterator:
                    children.append(child)
                    if len(entries) + len(children) > MAX_INSTALLED_TREE_ENTRIES:
                        fail("INSTALL_OVERSIZE", "installed tree has too many entries")
            children.sort(key=lambda child: child.name)
        except BenchmarkError:
            raise
        except OSError as error:
            fail("INSTALL_MUTATED", f"cannot enumerate installed directory: {error}")
        child_directories: list[tuple[Path, os.stat_result]] = []
        for child in children:
            path = Path(child.path)
            relative = portable_installed_path(root, path)
            folded = relative.casefold()
            if folded in seen_portable:
                fail("INSTALL_UNSAFE", f"installed paths collide portably: {relative}")
            seen_portable.add(folded)
            if len(entries) >= MAX_INSTALLED_TREE_ENTRIES:
                fail("INSTALL_OVERSIZE", "installed tree has too many entries")
            try:
                observed = child.stat(follow_symlinks=False)
            except OSError as error:
                fail("INSTALL_MUTATED", f"cannot inspect installed member: {error}")
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if getattr(observed, "st_file_attributes", 0) & reparse_flag:
                fail(
                    "INSTALL_UNSAFE", f"installed member is a reparse point: {relative}"
                )
            mode = stat.S_IMODE(observed.st_mode)
            if mode > 0o777:
                fail(
                    "INSTALL_UNSAFE",
                    f"installed member has special permission bits: {relative}",
                )
            if stat.S_ISDIR(observed.st_mode):
                entries.append({"path": relative, "type": "directory", "mode": mode})
                child_directories.append((path, observed))
            elif stat.S_ISREG(observed.st_mode):
                length, sha = read_installed_regular(path, observed)
                observed_members.append((path, observed))
                total_bytes += length
                if total_bytes > MAX_INSTALLED_TREE_BYTES:
                    fail(
                        "INSTALL_OVERSIZE",
                        "installed tree byte count exceeds its bound",
                    )
                entries.append(
                    {
                        "path": relative,
                        "type": "regular",
                        "mode": mode,
                        "byteLength": length,
                        "sha256": sha,
                    }
                )
            elif stat.S_ISLNK(observed.st_mode):
                expected_target = allowed.get(path.absolute())
                if expected_target is None:
                    fail("INSTALL_UNSAFE", f"unexpected installed symlink: {relative}")
                try:
                    link_target = os.readlink(path)
                    resolved = path.resolve(strict=True)
                    resolved_relative = portable_installed_path(root, resolved)
                    after = path.lstat()
                except OSError as error:
                    fail(
                        "INSTALL_MUTATED", f"cannot resolve installed symlink: {error}"
                    )
                if (
                    not link_target
                    or "\x00" in link_target
                    or len(os.fsencode(link_target)) > MAX_INSTALLED_TREE_PATH_BYTES
                    or resolved != expected_target
                    or installed_stat_identity(observed)
                    != installed_stat_identity(after)
                ):
                    fail(
                        "INSTALL_MUTATED",
                        f"installed symlink identity changed: {relative}",
                    )
                target_metadata = resolved.lstat()
                if not stat.S_ISREG(target_metadata.st_mode):
                    fail(
                        "INSTALL_UNSAFE",
                        f"installed symlink target is not regular: {relative}",
                    )
                target_length, target_sha = read_installed_regular(
                    resolved, target_metadata
                )
                observed_members.extend(((path, observed), (resolved, target_metadata)))
                entries.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "linkTarget": link_target,
                        "linkTextSha256": sha256_bytes(os.fsencode(link_target)),
                        "resolvedPath": resolved_relative,
                        "resolvedByteLength": target_length,
                        "resolvedSha256": target_sha,
                    }
                )
                seen_allowed.add(path.absolute())
            else:
                fail(
                    "INSTALL_UNSAFE",
                    f"installed member type is unsupported: {relative}",
                )
        try:
            after_directory = directory.lstat()
        except OSError as error:
            fail(
                "INSTALL_MUTATED",
                f"installed directory disappeared after traversal: {error}",
            )
        if installed_stat_identity(before_directory) != installed_stat_identity(
            after_directory
        ):
            fail(
                "INSTALL_MUTATED",
                f"installed directory changed during traversal: {directory}",
            )
        pending.extend(reversed(child_directories))
    if seen_allowed != set(allowed):
        fail(
            "INSTALL_UNSAFE",
            "an allowed installed symlink was not observed exactly once",
        )
    for path, expected in observed_members:
        try:
            settled = path.lstat()
        except OSError as error:
            fail(
                "INSTALL_MUTATED",
                f"installed member disappeared after capture: {error}",
            )
        if installed_stat_identity(settled) != installed_stat_identity(expected):
            fail(
                "INSTALL_MUTATED",
                f"installed member changed during tree capture: {path}",
            )
    for directory, expected in visited_directories:
        try:
            settled = directory.lstat()
        except OSError as error:
            fail(
                "INSTALL_MUTATED",
                f"installed directory disappeared after capture: {error}",
            )
        if installed_stat_identity(settled) != installed_stat_identity(expected):
            fail(
                "INSTALL_MUTATED",
                f"installed directory changed during tree capture: {directory}",
            )
    entries.sort(key=lambda entry: entry["path"])
    payload = {"schema": INSTALLED_TREE_SCHEMA, "entries": entries}
    return {
        "schema": INSTALLED_TREE_SCHEMA,
        "entryCount": len(entries),
        "directoryCount": sum(entry["type"] == "directory" for entry in entries),
        "regularFileCount": sum(entry["type"] == "regular" for entry in entries),
        "symlinkCount": sum(entry["type"] == "symlink" for entry in entries),
        "byteCount": total_bytes,
        "digestSha256": sha256_bytes(canonical_json(payload)),
        "entries": entries,
    }


def validate_installed_tree_identity(value: Any, label: str) -> dict[str, Any]:
    identity = require_object(value, label)
    if (
        set(identity)
        != {
            "schema",
            "entryCount",
            "directoryCount",
            "regularFileCount",
            "symlinkCount",
            "byteCount",
            "digestSha256",
            "entries",
        }
        or identity.get("schema") != INSTALLED_TREE_SCHEMA
    ):
        fail("REPORT_MALFORMED", f"{label} has an unsupported shape")
    entries = identity.get("entries")
    integer_fields = (
        "entryCount",
        "directoryCount",
        "regularFileCount",
        "symlinkCount",
        "byteCount",
    )
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > MAX_INSTALLED_TREE_ENTRIES
        or any(
            isinstance(identity.get(name), bool)
            or not isinstance(identity.get(name), int)
            or identity[name] < 0
            for name in integer_fields
        )
        or identity.get("entryCount") != len(entries)
    ):
        fail("REPORT_MALFORMED", f"{label} entry count is invalid")
    require_sha(identity.get("digestSha256"), f"{label} digest")
    paths: list[str] = []
    portable: set[str] = set()
    counts = {"directory": 0, "regular": 0, "symlink": 0}
    byte_count = 0
    for raw in entries:
        entry = require_object(raw, f"{label} entry")
        path = require_string(entry.get("path"), f"{label} entry path")
        safe_member_name(path)
        if (
            len(path.encode("utf-8")) > MAX_INSTALLED_TREE_PATH_BYTES
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in path
            )
            or path.casefold() in portable
        ):
            fail("REPORT_MALFORMED", f"{label} entry path is unsafe or duplicated")
        portable.add(path.casefold())
        paths.append(path)
        kind = entry.get("type")
        if kind == "directory":
            if set(entry) != {"path", "type", "mode"}:
                fail("REPORT_MALFORMED", f"{label} directory entry shape differs")
        elif kind == "regular":
            if set(entry) != {"path", "type", "mode", "byteLength", "sha256"}:
                fail("REPORT_MALFORMED", f"{label} regular entry shape differs")
            length = entry.get("byteLength")
            if (
                isinstance(length, bool)
                or not isinstance(length, int)
                or not 0 <= length <= MAX_INSTALLED_TREE_FILE_BYTES
            ):
                fail("REPORT_MALFORMED", f"{label} regular entry length is invalid")
            require_sha(entry.get("sha256"), f"{label} regular entry digest")
            byte_count += length
        elif kind == "symlink":
            if set(entry) != {
                "path",
                "type",
                "linkTarget",
                "linkTextSha256",
                "resolvedPath",
                "resolvedByteLength",
                "resolvedSha256",
            }:
                fail("REPORT_MALFORMED", f"{label} symlink entry shape differs")
            link_target = require_string(
                entry.get("linkTarget"), f"{label} link target"
            )
            if (
                "\x00" in link_target
                or len(os.fsencode(link_target)) > MAX_INSTALLED_TREE_PATH_BYTES
                or entry.get("linkTextSha256") != sha256_bytes(os.fsencode(link_target))
            ):
                fail("REPORT_MALFORMED", f"{label} link text identity differs")
            resolved_path = require_string(
                entry.get("resolvedPath"), f"{label} resolved path"
            )
            safe_member_name(resolved_path)
            resolved_length = entry.get("resolvedByteLength")
            if (
                isinstance(resolved_length, bool)
                or not isinstance(resolved_length, int)
                or not 0 <= resolved_length <= MAX_INSTALLED_TREE_FILE_BYTES
            ):
                fail("REPORT_MALFORMED", f"{label} resolved link length is invalid")
            require_sha(entry.get("resolvedSha256"), f"{label} resolved link digest")
        else:
            fail("REPORT_MALFORMED", f"{label} entry type is unsupported")
        if kind in {"directory", "regular"}:
            mode = entry.get("mode")
            if (
                isinstance(mode, bool)
                or not isinstance(mode, int)
                or not 0 <= mode <= 0o777
            ):
                fail("REPORT_MALFORMED", f"{label} entry mode is invalid")
        counts[kind] += 1
    if paths != sorted(paths) or byte_count > MAX_INSTALLED_TREE_BYTES:
        fail("REPORT_MALFORMED", f"{label} entries are unordered or oversized")
    if (
        identity.get("directoryCount") != counts["directory"]
        or identity.get("regularFileCount") != counts["regular"]
        or identity.get("symlinkCount") != counts["symlink"]
        or identity.get("byteCount") != byte_count
        or identity.get("digestSha256")
        != sha256_bytes(
            canonical_json({"schema": INSTALLED_TREE_SCHEMA, "entries": entries})
        )
    ):
        fail("REPORT_MALFORMED", f"{label} counts or digest differ from its entries")
    return identity


def verify_installed_tree(
    root: Path,
    expected: Any,
    allowed_symlinks: Mapping[Path, Path] | None = None,
) -> str:
    validated = validate_installed_tree_identity(expected, "retained installed tree")
    observed = capture_installed_tree(root, allowed_symlinks)
    if observed != validated:
        fail("INSTALL_MUTATED", f"retained installed tree identity differs: {root}")
    return observed["digestSha256"]


def installed_tree_entries_by_path(
    identity: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    return {entry["path"]: entry for entry in identity["entries"]}


def validate_installation_tree_relationships(
    surface: str,
    identity: Mapping[str, Any],
    version: str,
    platform_value: str,
    surfaces: Mapping[str, Mapping[str, Any]],
    launcher: Mapping[str, Any],
) -> None:
    entries = installed_tree_entries_by_path(identity)
    executable = "prose.exe" if platform_value.startswith("win32-") else "prose"
    if surface in {"direct-rust", "direct-bun"}:
        implementation = surface.removeprefix("direct-")
        root_name = f"openprose-prose-cli-{implementation}-{version}-{platform_value}"
        expected_paths = {
            root_name,
            f"{root_name}/LICENSE",
            f"{root_name}/README.txt",
            f"{root_name}/examples",
            f"{root_name}/examples/hello.prose.md",
            f"{root_name}/{executable}",
        }
        if platform_value.startswith("win32-"):
            expected_paths.add(f"{root_name}/openprose-windows-process-host.exe")
        if set(entries) != expected_paths:
            fail("REPORT_MALFORMED", f"{surface} installed tree membership differs")
        if (
            entries[root_name].get("type") != "directory"
            or entries[f"{root_name}/examples"].get("type") != "directory"
            or entries[f"{root_name}/LICENSE"].get("type") != "regular"
            or entries[f"{root_name}/README.txt"].get("type") != "regular"
            or entries[f"{root_name}/examples/hello.prose.md"].get("type") != "regular"
        ):
            fail("REPORT_MALFORMED", f"{surface} installed member types differ")
        binary = entries[f"{root_name}/{executable}"]
        if (
            binary.get("type") != "regular"
            or binary.get("sha256") != surfaces[surface]["binarySha256"]
            or binary.get("mode", 0) & 0o111 == 0
        ):
            fail("REPORT_MALFORMED", f"{surface} installed binary identity differs")
        return

    if surface != "npm-launcher":
        fail("REPORT_MALFORMED", "installed tree has an unknown surface")
    modules = "lib/node_modules"
    meta = f"{modules}/@openprose/prose-cli"
    platform_root = f"{modules}/@openprose/prose-cli-{platform_value}"
    required_paths = {
        "bin",
        "bin/prose",
        "lib",
        modules,
        f"{modules}/@openprose",
        meta,
        f"{meta}/LICENSE",
        f"{meta}/README.md",
        f"{meta}/package.json",
        f"{meta}/bin",
        f"{meta}/bin/prose.js",
        f"{meta}/examples",
        f"{meta}/examples/hello.prose.md",
        platform_root,
        f"{platform_root}/LICENSE",
        f"{platform_root}/package.json",
        f"{platform_root}/bin",
        f"{platform_root}/bin/{executable}",
    }
    if set(entries) != required_paths:
        fail("REPORT_MALFORMED", "npm installed tree membership differs")
    directory_paths = {
        "bin",
        "lib",
        modules,
        f"{modules}/@openprose",
        meta,
        f"{meta}/bin",
        f"{meta}/examples",
        platform_root,
        f"{platform_root}/bin",
    }
    regular_paths = required_paths - directory_paths - {"bin/prose"}
    if any(entries[path].get("type") != "directory" for path in directory_paths):
        fail("REPORT_MALFORMED", "npm installed tree directory type differs")
    if any(entries[path].get("type") != "regular" for path in regular_paths):
        fail("REPORT_MALFORMED", "npm installed tree regular-file type differs")
    launcher_source = entries[f"{meta}/bin/prose.js"]
    packaged_binary = entries[f"{platform_root}/bin/{executable}"]
    platform_manifest = entries[f"{platform_root}/package.json"]
    if (
        launcher_source.get("type") != "regular"
        or launcher_source.get("sha256") != surfaces[surface]["launcherSourceSha256"]
        or launcher_source.get("mode", 0) & 0o111 == 0
        or packaged_binary.get("type") != "regular"
        or packaged_binary.get("sha256") != surfaces[surface]["binarySha256"]
        or packaged_binary.get("mode", 0) & 0o111 == 0
        or platform_manifest.get("type") != "regular"
        or platform_manifest.get("sha256") != launcher["platformManifestSha256"]
    ):
        fail("REPORT_MALFORMED", "npm installed package identity differs")
    command = entries["bin/prose"]
    command_identity = launcher["launcherCommand"]
    if command_identity["kind"] == "symlink":
        if (
            command.get("type") != "symlink"
            or command.get("linkTarget") != command_identity["linkTarget"]
            or command.get("linkTextSha256") != command_identity["linkTextSha256"]
            or command.get("resolvedPath") != f"{meta}/bin/prose.js"
            or command.get("resolvedByteLength") != launcher_source["byteLength"]
            or command.get("resolvedSha256")
            != command_identity["resolvedLauncherSha256"]
        ):
            fail("REPORT_MALFORMED", "npm launcher symlink custody differs")
    elif (
        command.get("type") != "regular"
        or command.get("sha256") != command_identity["sha256"]
        or command.get("mode", 0) & 0o111 == 0
    ):
        fail("REPORT_MALFORMED", "npm launcher shim custody differs")


def prepare_install_root(path: Path) -> Path:
    absolute = path.absolute()
    try:
        absolute.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        fail("INSTALL_ROOT_UNSAFE", f"cannot inspect install root: {error}")
    else:
        fail("INSTALL_ROOT_UNSAFE", "install root must not already exist")
    parent = absolute.parent
    try:
        observed = parent.lstat()
    except OSError as error:
        fail("INSTALL_ROOT_UNSAFE", f"install root parent is unavailable: {error}")
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        fail(
            "INSTALL_ROOT_UNSAFE", "install root parent must be a non-symlink directory"
        )
    try:
        absolute.mkdir(mode=0o700)
        marker = absolute / ".openprose-installed-benchmark-root"
        marker.write_text("owned disposable benchmark root\n", "utf-8")
    except OSError as error:
        fail("INSTALL_FAILED", f"cannot create install root: {error}")
    return absolute


def npm_layout(prefix: Path, platform_value: str) -> tuple[Path, Path, Path, Path]:
    if os.name == "nt":
        modules = prefix / "node_modules"
        command = prefix / "prose.cmd"
    else:
        modules = prefix / "lib" / "node_modules"
        command = prefix / "bin" / "prose"
    meta = modules / "@openprose" / "prose-cli"
    platform_root = modules / "@openprose" / f"prose-cli-{platform_value}"
    return command, meta, platform_root, modules


def launcher_command_identity(command: Path, launcher_source: Path) -> dict[str, Any]:
    try:
        observed = command.lstat()
    except OSError as error:
        fail("INSTALL_UNSAFE", f"cannot inspect npm launcher command: {error}")
    if stat.S_ISLNK(observed.st_mode):
        try:
            target = os.readlink(command)
            resolved = command.resolve(strict=True)
        except OSError as error:
            fail("INSTALL_UNSAFE", f"cannot resolve npm launcher symlink: {error}")
        if resolved != launcher_source.resolve(strict=True):
            fail(
                "IDENTITY_DIVERGENCE",
                "npm command does not resolve to the packaged launcher",
            )
        return {
            "kind": "symlink",
            "linkTarget": target,
            "linkTextSha256": sha256_bytes(os.fsencode(target)),
            "resolvedLauncherSha256": sha256_bytes(
                safe_read(resolved, MAX_MEMBER_BYTES)
            ),
        }
    if not stat.S_ISREG(observed.st_mode):
        fail(
            "INSTALL_UNSAFE",
            "npm launcher command is neither a regular shim nor a symlink",
        )
    encoded = safe_read(command, MAX_MEMBER_BYTES)
    return {"kind": "regular-shim", "sha256": sha256_bytes(encoded)}


def validate_runner_result(
    encoded: bytes,
    expected_runner: str,
    release: Mapping[str, Any],
    expected_task_digest: str,
) -> dict[str, Any]:
    result = require_object(
        json_no_duplicates(encoded, "runner result"), "runner result"
    )
    if result.get("schema") != "openprose.runner-result/1":
        fail("RESULT_MALFORMED", "runner result schema differs")
    expected_identity = {
        "name": expected_runner,
        "version": release["version"],
        "commit": release["source"]["revision"],
    }
    if result.get("runner") != expected_identity:
        fail("IDENTITY_DIVERGENCE", f"{expected_runner} runner identity differs")
    expected_image = {
        key: release["image"][key] for key in ("formatVersion", "version", "sha256")
    }
    if result.get("languageImage") != expected_image:
        fail("IDENTITY_DIVERGENCE", f"{expected_runner} image identity differs")
    adapter = require_object(result.get("adapter"), "runner adapter")
    digests = require_object(result.get("digests"), "runner digests")
    terminal = require_object(result.get("terminal"), "runner terminal")
    semantic = require_object(result.get("semantic"), "runner semantic boundary")
    if (
        adapter.get("id") != "mock/in-memory"
        or result.get("transport") != "deterministic"
        or digests.get("taskSha256") != expected_task_digest
        or digests.get("deliveredImageSha256") != release["image"]["sha256"]
        or result.get("runnerExitCode") != 0
        or terminal.get("classification") != "success"
        or terminal.get("transportCompleted") is not True
        or terminal.get("terminalEventObserved") is not True
        or semantic.get("status") != "not-applicable"
    ):
        fail(
            "RESULT_INVALID",
            f"{expected_runner} mock transport result did not settle exactly",
        )
    return {
        "runner": expected_identity,
        "image": expected_image,
        "taskSha256": expected_task_digest,
        "adapterId": "mock/in-memory",
        "transport": "deterministic",
    }


def relative_install_path(path: Path, install_root: Path) -> str:
    try:
        return "$INSTALL_ROOT/" + path.relative_to(install_root).as_posix()
    except ValueError:
        return str(path)


def invoke_surface(
    surface: str,
    executable: Path,
    runner: str,
    release: Mapping[str, Any],
    task_digest: str,
    workspace: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    install_root: Path,
    ordinal: int,
    deadline_monotonic: float | None,
) -> dict[str, Any]:
    outcome = run_process_before_deadline(
        [str(executable), *INVOCATION_ARGV],
        workspace,
        environment,
        timeout_seconds,
        f"{surface} invocation {ordinal}",
        deadline_monotonic,
    )
    if outcome["exitCode"] != 0 or outcome["stderr"]:
        fail(
            "INVOCATION_FAILED",
            f"{surface} returned {outcome['exitCode']} or contaminated stderr",
        )
    identity = validate_runner_result(outcome["stdout"], runner, release, task_digest)
    return {
        "surface": surface,
        "ordinal": ordinal,
        "wallMs": outcome["wallMs"],
        "exitCode": outcome["exitCode"],
        "stdoutSha256": sha256_bytes(outcome["stdout"]),
        "stdoutByteLength": len(outcome["stdout"]),
        "stderrSha256": sha256_bytes(outcome["stderr"]),
        "stderrByteLength": len(outcome["stderr"]),
        "settlement": outcome["settlement"],
        "settlementAuthority": outcome["settlementAuthority"],
        "transportValidation": "passed",
        "observedIdentity": identity,
        "command": {
            "executable": relative_install_path(executable, install_root),
            "argv": list(INVOCATION_ARGV),
            "shell": False,
        },
    }


def validate_package_payloads(context: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every packaged payload without writing or executing it."""

    release = require_object(context.get("release"), "release manifest")
    platform_value = require_string(context.get("platform"), "package platform")
    version = require_string(release.get("version"), "package version")
    extracted: dict[str, dict[str, Any]] = {}
    for implementation in ("rust", "bun"):
        artifact_name = (
            f"openprose-prose-cli-{implementation}-{version}-{platform_value}.tar.gz"
        )
        members = decode_archive_members(
            context["encoded"][artifact_name], artifact_name
        )
        root_name, expected = expected_standalone_members(artifact_name, platform_value)
        if isinstance(context['release'].get('agentsSdk'), dict):
            expected.update(root_name + '/' + name for name in ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'))
            if members.get(root_name + '/prose-agents-sdk', (b'', 0))[1] & 0o111 == 0:
                fail('ARCHIVE_UNSAFE', 'SDK helper is not executable')
        if set(members) != expected:
            fail(
                "MEMBERSHIP_MALFORMED",
                f"{implementation} standalone archive members differ",
            )
        executable_name = (
            "prose.exe" if platform_value.startswith("win32-") else "prose"
        )
        binary_name = f"{root_name}/{executable_name}"
        binary_bytes, binary_mode = members[binary_name]
        if binary_mode & 0o111 == 0:
            fail(
                "ARCHIVE_UNSAFE",
                f"{implementation} standalone binary is not executable",
            )
        extracted[implementation] = {
            "artifactName": artifact_name,
            "rootName": root_name,
            "binaryName": binary_name,
            "binaryBytes": binary_bytes,
            "binarySha256": sha256_bytes(binary_bytes),
            "artifact": context["artifacts"][artifact_name],
            "members": members,
        }

    npm_package = validate_npm_packages(context, extracted["bun"]["binaryBytes"])
    if platform_value.startswith("win32-"):
        host_record = require_object(
            release.get("windowsProcessHost"), "Windows sidecar record"
        )
        host_digest = require_sha(host_record.get("sha256"), "Windows sidecar sha256")
        if host_record.get("admission") is not False:
            fail(
                "IDENTITY_DIVERGENCE",
                "packaged Windows sidecar admission must remain false",
            )
        standalone_hosts = []
        for implementation in ("rust", "bun"):
            matches = [
                data
                for name, (data, _) in extracted[implementation]["members"].items()
                if name.endswith("/openprose-windows-process-host.exe")
            ]
            if len(matches) != 1:
                fail(
                    "IDENTITY_DIVERGENCE",
                    "standalone archive lacks one Windows sidecar",
                )
            standalone_hosts.append(matches[0])
        if any(
            value is None or sha256_bytes(value) != host_digest
            for value in [*standalone_hosts, npm_package["sidecar"]]
        ):
            fail(
                "IDENTITY_DIVERGENCE",
                "Windows sidecar bytes differ across package surfaces",
            )
        sidecar_evidence: Any = {
            "sha256": host_digest,
            "byteLength": host_record.get("byteLength"),
            "presentIn": ["direct-rust", "direct-bun", "npm-platform"],
            "admission": False,
        }
    else:
        if release.get("windowsProcessHost") != "not-applicable":
            fail("IDENTITY_DIVERGENCE", "non-Windows package has a sidecar record")
        sidecar_evidence = "not-applicable"
    return {
        "release": release,
        "platform": platform_value,
        "version": version,
        "extracted": extracted,
        "npmPackage": npm_package,
        "sidecarEvidence": sidecar_evidence,
    }


def install_verified_package_set(
    context: Mapping[str, Any],
    install_root: Path,
    *,
    timeout_seconds: float,
    deadline_monotonic: float | None,
) -> dict[str, Any]:
    """Install an already verified package set into one fresh owned root."""

    payloads = validate_package_payloads(context)
    platform_value = payloads["platform"]
    if platform_value.startswith("win32-") or os.name == "nt":
        fail(
            "PLATFORM_UNSUPPORTED",
            "Windows measurement requires native Job Object containment before any process spawn",
        )
    install = prepare_install_root(install_root)
    installations: list[dict[str, Any]] = []
    extracted = payloads["extracted"]
    for implementation in ("rust", "bun"):
        remaining_before(deadline_monotonic, f"before {implementation} extraction")
        destination = install / f"{implementation}-standalone"
        started = time.perf_counter_ns()
        extract_members(extracted[implementation]["members"], destination)
        wall_ms = round((time.perf_counter_ns() - started) / 1_000_000, 6)
        binary = destination / extracted[implementation]["binaryName"]
        binary_bytes = safe_read(binary, MAX_MEMBER_BYTES)
        if binary_bytes != extracted[implementation]["binaryBytes"]:
            fail(
                "INPUT_MUTATED",
                f"installed {implementation} binary differs from archive",
            )
        tree_identity = capture_installed_tree(destination)
        installations.append(
            {
                "surface": f"direct-{implementation}",
                "method": "validated-archive-extraction",
                "wallMs": wall_ms,
                "installedByteCount": tree_identity["byteCount"],
                "byteCountMethod": "sum-of-regular-file-lengths",
                "treeIdentity": tree_identity,
            }
        )
        extracted[implementation] = {
            **extracted[implementation],
            "destination": destination,
            "binary": binary,
        }
        remaining_before(deadline_monotonic, f"after {implementation} extraction")

    remaining_before(deadline_monotonic, "before npm package validation")
    npm_package = payloads["npmPackage"]
    npm_tool = resolve_tool("npm")
    node_tool = resolve_tool("node")
    if npm_tool is None or node_tool is None:
        fail(
            "TOOL_UNAVAILABLE", "npm and Node are required for npm package measurement"
        )
    npm_prefix = install / "npm-prefix"
    npm_cache = install / "npm-cache"
    npm_env_root = install / "npm-environment"
    npm_prefix.mkdir()
    npm_cache.mkdir()
    npm_env_root.mkdir()
    npm_snapshots = snapshot_verified_artifacts(
        context,
        install / "npm-package-snapshots",
        [npm_package["platformName"], npm_package["metaName"]],
    )
    user_config = npm_env_root / "npmrc"
    user_config.write_text("", "utf-8")
    npm_environment = clean_environment(
        npm_env_root,
        [Path(npm_tool["command"]).parent, Path(node_tool["command"]).parent],
    )
    npm_environment.update(
        {
            "npm_config_cache": str(npm_cache),
            "npm_config_userconfig": str(user_config),
            "npm_config_registry": "http://127.0.0.1:9/",
            "npm_config_offline": "true",
            "npm_config_ignore_scripts": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
        }
    )
    npm_argv = [
        npm_tool["command"],
        "install",
        "--global",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
        "--prefix",
        str(npm_prefix),
        str(npm_snapshots[npm_package["platformName"]]),
        str(npm_snapshots[npm_package["metaName"]]),
    ]
    npm_outcome = run_process_before_deadline(
        npm_argv,
        install,
        npm_environment,
        max(timeout_seconds, 30),
        "offline npm installation",
        deadline_monotonic,
    )
    if npm_outcome["exitCode"] != 0:
        diagnostic = npm_outcome["stderr"].decode("utf-8", "replace")[:1000]
        fail("INSTALL_FAILED", f"offline npm install failed: {diagnostic}")
    command, meta_root, platform_root, _ = npm_layout(npm_prefix, platform_value)
    launcher_source = meta_root / "bin" / "prose.js"
    platform_manifest_path = platform_root / "package.json"
    executable_name = "prose.exe" if platform_value.startswith("win32-") else "prose"
    packaged_binary_path = platform_root / "bin" / executable_name
    installed_launcher = safe_read(launcher_source, MAX_MEMBER_BYTES)
    installed_manifest = safe_read(platform_manifest_path, MAX_EVIDENCE_BYTES)
    installed_binary = safe_read(packaged_binary_path, MAX_MEMBER_BYTES)
    if (
        installed_launcher != npm_package["launcher"]
        or installed_manifest != npm_package["platformManifestBytes"]
        or installed_binary != npm_package["binary"]
    ):
        fail(
            "IDENTITY_DIVERGENCE",
            "npm installed bytes differ from the two input tarballs",
        )
    if not command.exists():
        fail("INSTALL_FAILED", "npm did not create the launcher command")
    command_identity = launcher_command_identity(command, launcher_source)
    allowed_links = {command: launcher_source} if command.is_symlink() else {}
    npm_tree_identity = capture_installed_tree(npm_prefix, allowed_links)
    npm_report_argv = [
        "$NPM",
        "install",
        "--global",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
        "--prefix",
        "$INSTALL_ROOT/npm-prefix",
        f"$INSTALL_ROOT/npm-package-snapshots/{npm_package['platformName']}",
        f"$INSTALL_ROOT/npm-package-snapshots/{npm_package['metaName']}",
    ]
    installations.append(
        {
            "surface": "npm-launcher",
            "method": "npm-global-offline-two-local-tarballs",
            "wallMs": npm_outcome["wallMs"],
            "installedByteCount": npm_tree_identity["byteCount"],
            "byteCountMethod": "sum-of-regular-file-lengths",
            "treeIdentity": npm_tree_identity,
            "installer": {
                "sha256": npm_tool["sha256"],
                "resolvedPath": npm_tool["resolvedPath"],
                "argv": npm_report_argv,
                "shell": False,
                "ownedCache": "$INSTALL_ROOT/npm-cache",
                "registryMode": "offline-with-loopback-invalid-registry",
                "lifecycleScripts": "disabled",
            },
        }
    )
    workspace = install / "workspace"
    workspace.mkdir()
    run_environment = clean_environment(
        install / "run-environment", [Path(node_tool["command"]).parent]
    )
    surfaces = {
        "direct-rust": {
            "runner": "rust",
            "executable": extracted["rust"]["binary"],
            "binarySha256": extracted["rust"]["binarySha256"],
            "packageArtifactSha256": extracted["rust"]["artifact"]["sha256"],
        },
        "direct-bun": {
            "runner": "bun",
            "executable": extracted["bun"]["binary"],
            "binarySha256": extracted["bun"]["binarySha256"],
            "packageArtifactSha256": extracted["bun"]["artifact"]["sha256"],
        },
        "npm-launcher": {
            "runner": "bun",
            "executable": command,
            "binarySha256": sha256_bytes(installed_binary),
            "packageArtifactSha256": context["artifacts"][npm_package["platformName"]][
                "sha256"
            ],
            "metaPackageSha256": context["artifacts"][npm_package["metaName"]][
                "sha256"
            ],
            "launcherSourceSha256": sha256_bytes(installed_launcher),
            "launcherCommand": relative_install_path(command, install),
            "launcherCommandIdentity": command_identity,
        },
    }
    return {
        **payloads,
        "install": install,
        "installations": installations,
        "npmTool": npm_tool,
        "nodeTool": node_tool,
        "npmPrefix": npm_prefix,
        "npmReportArgv": npm_report_argv,
        "installedLauncher": installed_launcher,
        "installedManifest": installed_manifest,
        "installedBinary": installed_binary,
        "commandIdentity": command_identity,
        "workspace": workspace,
        "runEnvironment": run_environment,
        "surfaces": surfaces,
    }


def run_benchmark(
    package_output: Path,
    install_root: Path,
    *,
    trials: int = 5,
    timeout_seconds: float = 10,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    if not isinstance(trials, int) or not 1 <= trials <= 100:
        fail("ARGUMENT_INVALID", "trials must be between 1 and 100")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 0 < float(timeout_seconds) <= 120
    ):
        fail("ARGUMENT_INVALID", "timeout_seconds must be finite and in (0, 120]")
    validate_deadline(deadline_monotonic)
    remaining_before(deadline_monotonic, "before package verification")
    context = verify_package_output(package_output)
    remaining_before(deadline_monotonic, "after package verification")
    if context["platform"].startswith("win32-"):
        fail(
            "PLATFORM_UNSUPPORTED",
            "Windows measurement requires native Job Object containment before any process spawn",
        )
    install = prepare_install_root(install_root)
    release = context["release"]
    platform_value = context["platform"]
    version = release["version"]
    installations: list[dict[str, Any]] = []
    extracted: dict[str, dict[str, Any]] = {}
    for implementation in ("rust", "bun"):
        remaining_before(deadline_monotonic, f"before {implementation} extraction")
        artifact_name = (
            f"openprose-prose-cli-{implementation}-{version}-{platform_value}.tar.gz"
        )
        members = decode_archive_members(
            context["encoded"][artifact_name], artifact_name
        )
        root_name, expected = expected_standalone_members(artifact_name, platform_value)
        if isinstance(context['release'].get('agentsSdk'), dict):
            expected.update(root_name + '/' + name for name in ('prose-agents-sdk', 'agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'))
            if members.get(root_name + '/prose-agents-sdk', (b'', 0))[1] & 0o111 == 0:
                fail('ARCHIVE_UNSAFE', 'SDK helper is not executable')
        if set(members) != expected:
            fail(
                "MEMBERSHIP_MALFORMED",
                f"{implementation} standalone archive members differ",
            )
        destination = install / f"{implementation}-standalone"
        started = time.perf_counter_ns()
        extract_members(members, destination)
        wall_ms = round((time.perf_counter_ns() - started) / 1_000_000, 6)
        executable_name = (
            "prose.exe" if platform_value.startswith("win32-") else "prose"
        )
        binary = destination / root_name / executable_name
        if members[f"{root_name}/{executable_name}"][1] & 0o111 == 0:
            fail(
                "ARCHIVE_UNSAFE",
                f"{implementation} standalone binary is not executable",
            )
        binary_bytes = safe_read(binary, MAX_MEMBER_BYTES)
        tree_identity = capture_installed_tree(destination)
        installations.append(
            {
                "surface": f"direct-{implementation}",
                "method": "validated-archive-extraction",
                "wallMs": wall_ms,
                "installedByteCount": tree_identity["byteCount"],
                "byteCountMethod": "sum-of-regular-file-lengths",
                "treeIdentity": tree_identity,
            }
        )
        extracted[implementation] = {
            "destination": destination,
            "binary": binary,
            "binaryBytes": binary_bytes,
            "binarySha256": sha256_bytes(binary_bytes),
            "artifact": context["artifacts"][artifact_name],
            "members": members,
        }
        remaining_before(deadline_monotonic, f"after {implementation} extraction")

    remaining_before(deadline_monotonic, "before npm package validation")
    npm_package = validate_npm_packages(context, extracted["bun"]["binaryBytes"])
    npm_tool = resolve_tool("npm")
    node_tool = resolve_tool("node")
    if npm_tool is None or node_tool is None:
        fail(
            "TOOL_UNAVAILABLE", "npm and Node are required for npm package measurement"
        )
    npm_prefix = install / "npm-prefix"
    npm_cache = install / "npm-cache"
    npm_env_root = install / "npm-environment"
    npm_prefix.mkdir()
    npm_cache.mkdir()
    npm_env_root.mkdir()
    npm_snapshots = snapshot_verified_artifacts(
        context,
        install / "npm-package-snapshots",
        [npm_package["platformName"], npm_package["metaName"]],
    )
    user_config = npm_env_root / "npmrc"
    user_config.write_text("", "utf-8")
    npm_environment = clean_environment(
        npm_env_root,
        [Path(npm_tool["command"]).parent, Path(node_tool["command"]).parent],
    )
    npm_environment.update(
        {
            "npm_config_cache": str(npm_cache),
            "npm_config_userconfig": str(user_config),
            "npm_config_registry": "http://127.0.0.1:9/",
            "npm_config_offline": "true",
            "npm_config_ignore_scripts": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
        }
    )
    npm_argv = [
        npm_tool["command"],
        "install",
        "--global",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
        "--prefix",
        str(npm_prefix),
        str(npm_snapshots[npm_package["platformName"]]),
        str(npm_snapshots[npm_package["metaName"]]),
    ]
    npm_outcome = run_process_before_deadline(
        npm_argv,
        install,
        npm_environment,
        max(timeout_seconds, 30),
        "offline npm installation",
        deadline_monotonic,
    )
    if npm_outcome["exitCode"] != 0:
        diagnostic = npm_outcome["stderr"].decode("utf-8", "replace")[:1000]
        fail("INSTALL_FAILED", f"offline npm install failed: {diagnostic}")
    command, meta_root, platform_root, _ = npm_layout(npm_prefix, platform_value)
    launcher_source = meta_root / "bin" / "prose.js"
    platform_manifest_path = platform_root / "package.json"
    executable_name = "prose.exe" if platform_value.startswith("win32-") else "prose"
    packaged_binary_path = platform_root / "bin" / executable_name
    installed_launcher = safe_read(launcher_source, MAX_MEMBER_BYTES)
    installed_manifest = safe_read(platform_manifest_path, MAX_EVIDENCE_BYTES)
    installed_binary = safe_read(packaged_binary_path, MAX_MEMBER_BYTES)
    if (
        installed_launcher != npm_package["launcher"]
        or installed_manifest != npm_package["platformManifestBytes"]
        or installed_binary != npm_package["binary"]
    ):
        fail(
            "IDENTITY_DIVERGENCE",
            "npm installed bytes differ from the two input tarballs",
        )
    if not command.exists():
        fail("INSTALL_FAILED", "npm did not create the launcher command")
    command_identity = launcher_command_identity(command, launcher_source)
    allowed_links = {command: launcher_source} if command.is_symlink() else {}
    npm_tree_identity = capture_installed_tree(npm_prefix, allowed_links)
    npm_report_argv = [
        "$NPM",
        "install",
        "--global",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
        "--prefix",
        "$INSTALL_ROOT/npm-prefix",
        f"$INSTALL_ROOT/npm-package-snapshots/{npm_package['platformName']}",
        f"$INSTALL_ROOT/npm-package-snapshots/{npm_package['metaName']}",
    ]
    installations.append(
        {
            "surface": "npm-launcher",
            "method": "npm-global-offline-two-local-tarballs",
            "wallMs": npm_outcome["wallMs"],
            "installedByteCount": npm_tree_identity["byteCount"],
            "byteCountMethod": "sum-of-regular-file-lengths",
            "treeIdentity": npm_tree_identity,
            "installer": {
                "sha256": npm_tool["sha256"],
                "resolvedPath": npm_tool["resolvedPath"],
                "argv": npm_report_argv,
                "shell": False,
                "ownedCache": "$INSTALL_ROOT/npm-cache",
                "registryMode": "offline-with-loopback-invalid-registry",
                "lifecycleScripts": "disabled",
            },
        }
    )

    if platform_value.startswith("win32-"):
        host_record = require_object(
            release.get("windowsProcessHost"), "Windows sidecar record"
        )
        host_digest = require_sha(host_record.get("sha256"), "Windows sidecar sha256")
        if host_record.get("admission") is not False:
            fail(
                "IDENTITY_DIVERGENCE",
                "packaged Windows sidecar admission must remain false",
            )
        standalone_hosts = []
        for implementation in ("rust", "bun"):
            matches = [
                data
                for name, (data, _) in extracted[implementation]["members"].items()
                if name.endswith("/openprose-windows-process-host.exe")
            ]
            if len(matches) != 1:
                fail(
                    "IDENTITY_DIVERGENCE",
                    "standalone archive lacks one Windows sidecar",
                )
            standalone_hosts.append(matches[0])
        installed_host = safe_read(
            platform_root / "bin" / "openprose-windows-process-host.exe",
            MAX_MEMBER_BYTES,
        )
        all_hosts = [*standalone_hosts, npm_package["sidecar"], installed_host]
        if any(
            value is None or sha256_bytes(value) != host_digest for value in all_hosts
        ):
            fail(
                "IDENTITY_DIVERGENCE",
                "Windows sidecar bytes differ across package surfaces",
            )
        sidecar_evidence: Any = {
            "sha256": host_digest,
            "byteLength": host_record.get("byteLength"),
            "presentIn": ["direct-rust", "direct-bun", "npm-platform", "npm-install"],
            "admission": False,
        }
    else:
        if release.get("windowsProcessHost") != "not-applicable":
            fail("IDENTITY_DIVERGENCE", "non-Windows package has a sidecar record")
        sidecar_evidence = "not-applicable"

    workspace = install / "workspace"
    workspace.mkdir()
    run_environment = clean_environment(
        install / "run-environment", [Path(node_tool["command"]).parent]
    )
    task = {
        "schema": "openprose.task-envelope/1",
        "argv": list(FORWARDED_TASK_ARGV),
        "interactionMode": "non-interactive",
    }
    task_digest = sha256_bytes(canonical_json(task))
    surfaces = {
        "direct-rust": {
            "runner": "rust",
            "executable": extracted["rust"]["binary"],
            "binarySha256": extracted["rust"]["binarySha256"],
            "packageArtifactSha256": extracted["rust"]["artifact"]["sha256"],
        },
        "direct-bun": {
            "runner": "bun",
            "executable": extracted["bun"]["binary"],
            "binarySha256": extracted["bun"]["binarySha256"],
            "packageArtifactSha256": extracted["bun"]["artifact"]["sha256"],
        },
        "npm-launcher": {
            "runner": "bun",
            "executable": command,
            "binarySha256": sha256_bytes(installed_binary),
            "packageArtifactSha256": context["artifacts"][npm_package["platformName"]][
                "sha256"
            ],
            "metaPackageSha256": context["artifacts"][npm_package["metaName"]][
                "sha256"
            ],
            "launcherSourceSha256": sha256_bytes(installed_launcher),
            "launcherCommand": relative_install_path(command, install),
            "launcherCommandIdentity": command_identity,
        },
    }
    invocations: list[dict[str, Any]] = []
    for ordinal in range(trials):
        for surface in ("direct-rust", "direct-bun", "npm-launcher"):
            remaining_before(
                deadline_monotonic, f"before {surface} invocation {ordinal}"
            )
            target = surfaces[surface]
            invocations.append(
                invoke_surface(
                    surface,
                    target["executable"],
                    target["runner"],
                    release,
                    task_digest,
                    workspace,
                    run_environment,
                    timeout_seconds,
                    install,
                    ordinal,
                    deadline_monotonic,
                )
            )
    artifact_records = [
        {
            "path": name,
            "kind": artifact["kind"],
            "implementation": artifact["implementation"],
            "platform": artifact["platform"],
            "byteLength": artifact["byteLength"],
            "sha256": artifact["sha256"],
        }
        for name, artifact in sorted(context["artifacts"].items())
    ]
    surface_records = {
        name: {
            key: value
            for key, value in target.items()
            if key not in {"runner", "executable"}
        }
        for name, target in surfaces.items()
    }
    node_version_outcome = run_process_before_deadline(
        [node_tool["resolvedPath"], "--version"],
        install,
        {
            "PATH": str(Path(node_tool["resolvedPath"]).parent),
            "LANG": "C",
            "LC_ALL": "C",
        },
        min(timeout_seconds, 10),
        "Node version evidence",
        deadline_monotonic,
    )
    try:
        node_version = node_version_outcome["stdout"].decode("ascii").strip()
    except UnicodeDecodeError:
        node_version = ""
    if (
        node_version_outcome["exitCode"] != 0
        or node_version_outcome["stderr"]
        or re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", node_version) is None
    ):
        fail("TOOL_UNAVAILABLE", "Node version evidence is malformed")
    remaining_before(deadline_monotonic, "before final package verification")
    assert_package_output_unchanged(context)
    remaining_before(deadline_monotonic, "before report return")
    package_identity = {
        "version": version,
        "sourceRevision": release["source"]["revision"],
        "releaseManifestSha256": context["checksums"]["release-manifest.json"],
        "dependencyEvidenceSha256": context["checksums"]["dependency-evidence.json"],
        "sha256SumsSha256": sha256_bytes(
            safe_read(context["root"] / "SHA256SUMS", MAX_EVIDENCE_BYTES)
        ),
        "rustBinarySha256": extracted["rust"]["binarySha256"],
        "bunBinarySha256": extracted["bun"]["binarySha256"],
    }
    executable_paths = {
        name: relative_install_path(target["executable"], install)
        for name, target in surfaces.items()
    }
    measurement_plan = {
        "trials": trials,
        "timeoutSeconds": float(timeout_seconds),
        "deadlineApplied": deadline_monotonic is not None,
        "surfaceOrder": ["direct-rust", "direct-bun", "npm-launcher"],
        "installationMethods": {
            "direct-rust": "validated-archive-extraction",
            "direct-bun": "validated-archive-extraction",
            "npm-launcher": "npm-global-offline-two-local-tarballs",
        },
        "npmInstallArgv": npm_report_argv,
        "invocationArgv": list(INVOCATION_ARGV),
        "executablePaths": executable_paths,
        "expectedIdentity": {
            "version": version,
            "sourceRevision": "development",
            "image": CURRENT_SENTINEL_IMAGE,
            "taskSha256": task_digest,
            "rustBinarySha256": extracted["rust"]["binarySha256"],
            "bunBinarySha256": extracted["bun"]["binarySha256"],
            "dependencyEvidenceSha256": context["checksums"][
                "dependency-evidence.json"
            ],
        },
    }
    report = {
        "schema": REPORT_SCHEMA,
        "platform": platform_value,
        "packageIdentity": package_identity,
        "measurementPlan": measurement_plan,
        "artifacts": artifact_records,
        "evidence": context["evidence"],
        "installations": installations,
        "surfaces": surface_records,
        "launcherResolution": {
            "launcherSourceSha256": sha256_bytes(installed_launcher),
            "platformManifestSha256": sha256_bytes(installed_manifest),
            "packagedBinarySha256": sha256_bytes(installed_binary),
            "matchesStandaloneBun": installed_binary == extracted["bun"]["binaryBytes"],
            "resolvedInsideInstalledPlatformPackage": True,
            "launcherCommand": command_identity,
            "windowsProcessHost": sidecar_evidence,
        },
        "task": {
            "purpose": "opaque-provider-free-mock-transport",
            "argv": list(FORWARDED_TASK_ARGV),
            "taskSha256": task_digest,
            "image": {
                key: release["image"][key]
                for key in ("formatVersion", "version", "sha256")
            },
        },
        "invocations": invocations,
        "toolchain": {
            "node": {**node_tool, "version": node_version},
            "npm": npm_tool,
        },
        "limitations": {
            "detachedDescendantContainment": "not-enforced",
            "providerCalls": "none",
            "semanticEvaluation": "not-performed",
            "portabilityEvaluation": "not-performed",
            "releaseEvaluation": "not-performed",
            "ranking": "not-produced",
            "runtimeNetworkIsolation": "not-enforced",
        },
    }
    remaining_before(deadline_monotonic, "before final installed-tree reauthentication")
    verify_retained_install_trees(install, report)
    remaining_before(deadline_monotonic, "after final installed-tree reauthentication")
    return report


def analyse_report(report: Any) -> dict[str, Any]:
    value = require_object(report, "benchmark report")
    expected_top_level = {
        "schema",
        "platform",
        "packageIdentity",
        "measurementPlan",
        "artifacts",
        "evidence",
        "installations",
        "surfaces",
        "launcherResolution",
        "task",
        "invocations",
        "toolchain",
        "limitations",
    }
    if set(value) != expected_top_level:
        fail(
            "REPORT_MALFORMED",
            "benchmark report has missing or unknown top-level fields",
        )
    if value.get("schema") != REPORT_SCHEMA:
        fail("REPORT_MALFORMED", "unsupported installed benchmark report schema")
    platform_value = value.get("platform")
    if platform_value not in SUPPORTED_PLATFORMS or str(platform_value).startswith(
        "win32-"
    ):
        fail(
            "REPORT_MALFORMED",
            "report platform lacks supported measurement containment",
        )
    expected_limitations = {
        "detachedDescendantContainment": "not-enforced",
        "providerCalls": "none",
        "semanticEvaluation": "not-performed",
        "portabilityEvaluation": "not-performed",
        "releaseEvaluation": "not-performed",
        "ranking": "not-produced",
        "runtimeNetworkIsolation": "not-enforced",
    }
    if value.get("limitations") != expected_limitations:
        fail("REPORT_MALFORMED", "benchmark limitations were weakened or changed")
    package_identity = require_object(value.get("packageIdentity"), "package identity")
    if set(package_identity) != {
        "version",
        "sourceRevision",
        "releaseManifestSha256",
        "dependencyEvidenceSha256",
        "sha256SumsSha256",
        "rustBinarySha256",
        "bunBinarySha256",
    }:
        fail("REPORT_MALFORMED", "package identity has an unsupported shape")
    if not is_exact_semver(
        require_string(package_identity.get("version"), "package version")
    ):
        fail("REPORT_MALFORMED", "package version is not SemVer")
    if (
        require_string(package_identity.get("sourceRevision"), "source revision")
        != "development"
    ):
        fail("REPORT_MALFORMED", "source revision differs from development capture")
    require_sha(
        package_identity.get("releaseManifestSha256"), "release manifest sha256"
    )
    require_sha(
        package_identity.get("dependencyEvidenceSha256"), "dependency evidence sha256"
    )
    require_sha(package_identity.get("sha256SumsSha256"), "SHA256SUMS sha256")
    require_sha(package_identity.get("rustBinarySha256"), "Rust binary sha256")
    require_sha(package_identity.get("bunBinarySha256"), "Bun binary sha256")

    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != 4:
        fail("REPORT_MALFORMED", "report must bind exactly four package artifacts")
    artifacts: dict[tuple[str, str, Any], dict[str, Any]] = {}
    artifact_names: list[str] = []
    for raw in raw_artifacts:
        artifact = require_object(raw, "report artifact")
        if set(artifact) != {
            "path",
            "kind",
            "implementation",
            "platform",
            "byteLength",
            "sha256",
        }:
            fail("REPORT_MALFORMED", "report artifact has an unsupported shape")
        path = require_string(artifact.get("path"), "report artifact path")
        validate_basename(path, "report artifact path")
        require_positive_int(artifact.get("byteLength"), "report artifact byte length")
        require_sha(artifact.get("sha256"), "report artifact sha256")
        role = (
            artifact.get("implementation"),
            artifact.get("kind"),
            artifact.get("platform"),
        )
        if role in artifacts or path in artifact_names:
            fail("REPORT_MALFORMED", "report artifact identities are duplicated")
        artifacts[role] = artifact
        artifact_names.append(path)
    expected_roles = {
        ("rust", "standalone-archive", platform_value),
        ("bun", "standalone-archive", platform_value),
        ("bun", "npm-meta", None),
        ("bun", "npm-platform", platform_value),
    }
    if set(artifacts) != expected_roles or artifact_names != sorted(artifact_names):
        fail("REPORT_MALFORMED", "report artifact roles/order differ")
    version = package_identity["version"]
    expected_artifact_names = {
        (
            "rust",
            "standalone-archive",
            platform_value,
        ): f"openprose-prose-cli-rust-{version}-{platform_value}.tar.gz",
        (
            "bun",
            "standalone-archive",
            platform_value,
        ): f"openprose-prose-cli-bun-{version}-{platform_value}.tar.gz",
        ("bun", "npm-meta", None): f"openprose-prose-cli-{version}.tgz",
        (
            "bun",
            "npm-platform",
            platform_value,
        ): f"openprose-prose-cli-{platform_value}-{version}.tgz",
    }
    if any(
        artifacts[role]["path"] != name
        for role, name in expected_artifact_names.items()
    ):
        fail(
            "REPORT_MALFORMED",
            "artifact filenames differ from package version/platform",
        )

    evidence = value.get("evidence")
    expected_evidence_order = [*sorted(REQUIRED_EVIDENCE), "SHA256SUMS"]
    if not isinstance(evidence, list) or len(evidence) != len(expected_evidence_order):
        fail("REPORT_MALFORMED", "report evidence membership differs")
    evidence_by_path: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(evidence):
        record = require_object(raw, "report evidence record")
        if set(record) != {"path", "byteLength", "sha256"}:
            fail("REPORT_MALFORMED", "report evidence record has an unsupported shape")
        path = require_string(record.get("path"), "report evidence path")
        if path != expected_evidence_order[index] or path in evidence_by_path:
            fail(
                "REPORT_MALFORMED",
                "report evidence is missing, extra, duplicated, or unordered",
            )
        require_positive_int(record.get("byteLength"), "report evidence byte length")
        require_sha(record.get("sha256"), "report evidence sha256")
        evidence_by_path[path] = record
    if (
        evidence_by_path["release-manifest.json"]["sha256"]
        != package_identity["releaseManifestSha256"]
        or evidence_by_path["dependency-evidence.json"]["sha256"]
        != package_identity["dependencyEvidenceSha256"]
        or evidence_by_path["SHA256SUMS"]["sha256"]
        != package_identity["sha256SumsSha256"]
    ):
        fail(
            "REPORT_MALFORMED", "report package identity differs from evidence digests"
        )

    task = require_object(value.get("task"), "benchmark task")
    if set(task) != {"purpose", "argv", "taskSha256", "image"}:
        fail("REPORT_MALFORMED", "benchmark task has an unsupported shape")
    image = require_object(task.get("image"), "benchmark task image")
    if set(image) != {"formatVersion", "version", "sha256"}:
        fail("REPORT_MALFORMED", "benchmark task image has an unsupported shape")
    if image != CURRENT_SENTINEL_IMAGE:
        fail("REPORT_MALFORMED", "task image differs from the benchmark sentinel")
    expected_task_envelope = {
        "schema": "openprose.task-envelope/1",
        "argv": list(FORWARDED_TASK_ARGV),
        "interactionMode": "non-interactive",
    }
    expected_task_digest = sha256_bytes(canonical_json(expected_task_envelope))
    if (
        task.get("purpose") != "opaque-provider-free-mock-transport"
        or task.get("argv") != list(FORWARDED_TASK_ARGV)
        or task.get("taskSha256") != expected_task_digest
    ):
        fail("REPORT_MALFORMED", "benchmark task identity differs")

    raw_surfaces = require_object(value.get("surfaces"), "benchmark surfaces")
    expected_surfaces = ("direct-rust", "direct-bun", "npm-launcher")
    if set(raw_surfaces) != set(expected_surfaces):
        fail("REPORT_MALFORMED", "benchmark surfaces are missing, extra, or duplicated")
    surfaces: dict[str, dict[str, Any]] = {}
    for surface in expected_surfaces:
        record = require_object(raw_surfaces.get(surface), f"surface {surface}")
        expected_keys = {"binarySha256", "packageArtifactSha256"}
        if surface == "npm-launcher":
            expected_keys |= {
                "metaPackageSha256",
                "launcherSourceSha256",
                "launcherCommand",
                "launcherCommandIdentity",
            }
        if set(record) != expected_keys:
            fail("REPORT_MALFORMED", f"surface {surface} has an unsupported shape")
        require_sha(record.get("binarySha256"), f"surface binary digest {surface}")
        require_sha(
            record.get("packageArtifactSha256"), f"surface package digest {surface}"
        )
        surfaces[surface] = record
    expected_package_digests = {
        "direct-rust": artifacts[("rust", "standalone-archive", platform_value)][
            "sha256"
        ],
        "direct-bun": artifacts[("bun", "standalone-archive", platform_value)][
            "sha256"
        ],
        "npm-launcher": artifacts[("bun", "npm-platform", platform_value)]["sha256"],
    }
    if any(
        surfaces[name]["packageArtifactSha256"] != digest
        for name, digest in expected_package_digests.items()
    ):
        fail(
            "REPORT_MALFORMED",
            "surface package identity differs from artifact evidence",
        )
    if (
        surfaces["direct-rust"]["binarySha256"] != package_identity["rustBinarySha256"]
        or surfaces["direct-bun"]["binarySha256"] != package_identity["bunBinarySha256"]
    ):
        fail("REPORT_MALFORMED", "surface binary identity differs from package custody")
    npm_surface = surfaces["npm-launcher"]
    require_sha(npm_surface.get("metaPackageSha256"), "npm meta package digest")
    require_sha(npm_surface.get("launcherSourceSha256"), "npm launcher source digest")
    require_string(npm_surface.get("launcherCommand"), "npm launcher command")
    if (
        npm_surface["metaPackageSha256"]
        != artifacts[("bun", "npm-meta", None)]["sha256"]
        or npm_surface["binarySha256"] != surfaces["direct-bun"]["binarySha256"]
    ):
        fail("REPORT_MALFORMED", "npm launcher package/binary identity differs")

    launcher = require_object(value.get("launcherResolution"), "launcher resolution")
    if set(launcher) != {
        "launcherSourceSha256",
        "platformManifestSha256",
        "packagedBinarySha256",
        "matchesStandaloneBun",
        "resolvedInsideInstalledPlatformPackage",
        "launcherCommand",
        "windowsProcessHost",
    }:
        fail("REPORT_MALFORMED", "launcher resolution has an unsupported shape")
    require_sha(launcher.get("platformManifestSha256"), "platform manifest digest")
    command_identity = require_object(
        launcher.get("launcherCommand"), "launcher command identity"
    )
    if command_identity.get("kind") == "symlink":
        if set(command_identity) != {
            "kind",
            "linkTarget",
            "linkTextSha256",
            "resolvedLauncherSha256",
        }:
            fail(
                "REPORT_MALFORMED", "symlink launcher identity has an unsupported shape"
            )
        require_string(command_identity.get("linkTarget"), "launcher link target")
        require_sha(command_identity.get("linkTextSha256"), "launcher link digest")
        if (
            command_identity.get("resolvedLauncherSha256")
            != npm_surface["launcherSourceSha256"]
        ):
            fail("REPORT_MALFORMED", "resolved launcher digest differs")
    elif command_identity.get("kind") == "regular-shim":
        if set(command_identity) != {"kind", "sha256"}:
            fail(
                "REPORT_MALFORMED", "regular launcher identity has an unsupported shape"
            )
        require_sha(command_identity.get("sha256"), "launcher shim digest")
    else:
        fail("REPORT_MALFORMED", "launcher command identity kind is unsupported")
    if (
        launcher.get("launcherSourceSha256") != npm_surface["launcherSourceSha256"]
        or launcher.get("packagedBinarySha256") != npm_surface["binarySha256"]
        or launcher.get("matchesStandaloneBun") is not True
        or launcher.get("resolvedInsideInstalledPlatformPackage") is not True
        or launcher.get("launcherCommand") != npm_surface["launcherCommandIdentity"]
        or launcher.get("windowsProcessHost") != "not-applicable"
    ):
        fail("REPORT_MALFORMED", "launcher resolution identity was weakened")

    expected_npm_argv = [
        "$NPM",
        "install",
        "--global",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
        "--prefix",
        "$INSTALL_ROOT/npm-prefix",
        f"$INSTALL_ROOT/npm-package-snapshots/openprose-prose-cli-{platform_value}-{version}.tgz",
        f"$INSTALL_ROOT/npm-package-snapshots/openprose-prose-cli-{version}.tgz",
    ]
    executable_name = (
        "prose.exe" if str(platform_value).startswith("win32-") else "prose"
    )
    expected_executable_paths = {
        "direct-rust": (
            f"$INSTALL_ROOT/rust-standalone/openprose-prose-cli-rust-{version}-"
            f"{platform_value}/{executable_name}"
        ),
        "direct-bun": (
            f"$INSTALL_ROOT/bun-standalone/openprose-prose-cli-bun-{version}-"
            f"{platform_value}/{executable_name}"
        ),
        "npm-launcher": "$INSTALL_ROOT/npm-prefix/bin/prose",
    }
    if npm_surface["launcherCommand"] != expected_executable_paths["npm-launcher"]:
        fail(
            "REPORT_MALFORMED",
            "npm launcher command path differs from the installed plan",
        )
    plan = require_object(value.get("measurementPlan"), "measurement plan")
    if set(plan) != {
        "trials",
        "timeoutSeconds",
        "deadlineApplied",
        "surfaceOrder",
        "installationMethods",
        "npmInstallArgv",
        "invocationArgv",
        "executablePaths",
        "expectedIdentity",
    }:
        fail("REPORT_MALFORMED", "measurement plan has an unsupported shape")
    planned_trials = plan.get("trials")
    if (
        isinstance(planned_trials, bool)
        or not isinstance(planned_trials, int)
        or not 1 <= planned_trials <= 100
    ):
        fail("REPORT_MALFORMED", "measurement plan trial count is invalid")
    planned_timeout = require_nonnegative_number(
        plan.get("timeoutSeconds"), "measurement plan timeout"
    )
    if planned_timeout <= 0 or planned_timeout > 120:
        fail("REPORT_MALFORMED", "measurement plan timeout is outside (0, 120]")
    expected_identity = {
        "version": version,
        "sourceRevision": "development",
        "image": CURRENT_SENTINEL_IMAGE,
        "taskSha256": expected_task_digest,
        "rustBinarySha256": package_identity["rustBinarySha256"],
        "bunBinarySha256": package_identity["bunBinarySha256"],
        "dependencyEvidenceSha256": package_identity["dependencyEvidenceSha256"],
    }
    if (
        not isinstance(plan.get("deadlineApplied"), bool)
        or plan.get("surfaceOrder") != list(expected_surfaces)
        or plan.get("installationMethods")
        != {
            "direct-rust": "validated-archive-extraction",
            "direct-bun": "validated-archive-extraction",
            "npm-launcher": "npm-global-offline-two-local-tarballs",
        }
        or plan.get("npmInstallArgv") != expected_npm_argv
        or plan.get("invocationArgv") != list(INVOCATION_ARGV)
        or plan.get("executablePaths") != expected_executable_paths
        or plan.get("expectedIdentity") != expected_identity
    ):
        fail(
            "REPORT_MALFORMED", "measurement plan identity or execution policy differs"
        )

    installations = value.get("installations")
    invocations = value.get("invocations")
    if not isinstance(installations, list) or not isinstance(invocations, list):
        fail("REPORT_MALFORMED", "benchmark measurements must be arrays")
    install_summary = []
    if len(installations) != 3:
        fail("REPORT_MALFORMED", "benchmark must contain exactly three installations")
    npm_installer: dict[str, Any] | None = None
    for index, item in enumerate(installations):
        record = require_object(item, "installation record")
        surface = expected_surfaces[index]
        expected_keys = {
            "surface",
            "method",
            "wallMs",
            "installedByteCount",
            "byteCountMethod",
            "treeIdentity",
        }
        if surface == "npm-launcher":
            expected_keys.add("installer")
        if set(record) != expected_keys or record.get("surface") != surface:
            fail(
                "REPORT_MALFORMED",
                "installation surfaces are missing, extra, or duplicated",
            )
        wall = require_nonnegative_number(
            record.get("wallMs"), "installation wall time"
        )
        installed_bytes = require_positive_int(
            record.get("installedByteCount"), "installed byte count"
        )
        tree_identity = validate_installed_tree_identity(
            record.get("treeIdentity"), f"installation tree {surface}"
        )
        if tree_identity["byteCount"] != installed_bytes:
            fail(
                "REPORT_MALFORMED", "installation byte count differs from tree custody"
            )
        validate_installation_tree_relationships(
            surface,
            tree_identity,
            version,
            platform_value,
            surfaces,
            launcher,
        )
        if record.get("byteCountMethod") != "sum-of-regular-file-lengths":
            fail("REPORT_MALFORMED", "installation byte count method differs")
        expected_method = (
            "npm-global-offline-two-local-tarballs"
            if surface == "npm-launcher"
            else "validated-archive-extraction"
        )
        if record.get("method") != expected_method:
            fail("REPORT_MALFORMED", "installation method differs")
        if surface == "npm-launcher":
            installer = require_object(record.get("installer"), "npm installer")
            npm_installer = installer
            if set(installer) != {
                "sha256",
                "resolvedPath",
                "argv",
                "shell",
                "ownedCache",
                "registryMode",
                "lifecycleScripts",
            }:
                fail("REPORT_MALFORMED", "npm installer has an unsupported shape")
            require_sha(installer.get("sha256"), "npm installer digest")
            require_string(installer.get("resolvedPath"), "npm installer resolved path")
            argv = installer.get("argv")
            if (
                argv != expected_npm_argv
                or installer.get("shell") is not False
                or installer.get("ownedCache") != "$INSTALL_ROOT/npm-cache"
                or installer.get("registryMode")
                != "offline-with-loopback-invalid-registry"
                or installer.get("lifecycleScripts") != "disabled"
            ):
                fail("REPORT_MALFORMED", "npm installation boundary was weakened")
        install_summary.append(
            {
                "surface": surface,
                "wallMs": wall,
                "installedByteCount": installed_bytes,
                "treeDigestSha256": tree_identity["digestSha256"],
                "installedEntryCount": tree_identity["entryCount"],
            }
        )

    toolchain = require_object(value.get("toolchain"), "benchmark toolchain")
    if set(toolchain) != {"node", "npm"}:
        fail("REPORT_MALFORMED", "benchmark toolchain has an unsupported shape")
    for name in ("node", "npm"):
        tool = require_object(toolchain.get(name), f"toolchain {name}")
        expected_tool_keys = (
            {"command", "resolvedPath", "sha256", "version"}
            if name == "node"
            else {"command", "resolvedPath", "sha256"}
        )
        if set(tool) != expected_tool_keys:
            fail("REPORT_MALFORMED", f"toolchain {name} has an unsupported shape")
        require_string(tool.get("command"), f"toolchain {name} command")
        require_string(tool.get("resolvedPath"), f"toolchain {name} resolved path")
        require_sha(tool.get("sha256"), f"toolchain {name} sha256")
        if tool.get("command") != tool.get("resolvedPath"):
            fail(
                "REPORT_MALFORMED",
                f"toolchain {name} did not execute its hashed resolved target",
            )
        if (
            name == "node"
            and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", str(tool.get("version")))
            is None
        ):
            fail("REPORT_MALFORMED", "toolchain Node version is malformed")
    assert npm_installer is not None
    if (
        npm_installer.get("sha256") != toolchain["npm"]["sha256"]
        or npm_installer.get("resolvedPath") != toolchain["npm"]["resolvedPath"]
    ):
        fail(
            "REPORT_MALFORMED", "npm installer identity differs from toolchain custody"
        )

    by_surface: dict[str, list[float]] = {}
    if len(invocations) != planned_trials * 3:
        fail(
            "REPORT_MALFORMED",
            "invocation count differs from the retained measurement plan",
        )
    trials = planned_trials
    expected_sequence = [
        (surface, ordinal) for ordinal in range(trials) for surface in expected_surfaces
    ]
    for index, item in enumerate(invocations):
        record = require_object(item, "invocation record")
        if set(record) != {
            "surface",
            "ordinal",
            "wallMs",
            "exitCode",
            "stdoutSha256",
            "stdoutByteLength",
            "stderrSha256",
            "stderrByteLength",
            "settlement",
            "settlementAuthority",
            "transportValidation",
            "observedIdentity",
            "command",
        }:
            fail("REPORT_MALFORMED", "invocation record has an unsupported shape")
        surface, ordinal = expected_sequence[index]
        observed_ordinal = record.get("ordinal")
        if (
            record.get("surface") != surface
            or isinstance(observed_ordinal, bool)
            or not isinstance(observed_ordinal, int)
            or observed_ordinal != ordinal
        ):
            fail("REPORT_MALFORMED", "invocation surfaces/trial ordinals differ")
        wall = require_nonnegative_number(record.get("wallMs"), "invocation wall time")
        if (
            isinstance(record.get("exitCode"), bool)
            or record.get("exitCode") != 0
            or record.get("settlement") != "settled"
            or record.get("settlementAuthority")
            != "direct-and-original-process-group-settled"
            or record.get("transportValidation") != "passed"
            or isinstance(record.get("stderrByteLength"), bool)
            or record.get("stderrByteLength") != 0
            or record.get("stderrSha256") != sha256_bytes(b"")
        ):
            fail(
                "REPORT_MALFORMED",
                "invocation execution/settlement boundary was weakened",
            )
        require_positive_int(record.get("stdoutByteLength"), "invocation stdout length")
        require_sha(record.get("stdoutSha256"), "invocation stdout digest")
        identity = require_object(record.get("observedIdentity"), "observed identity")
        if set(identity) != {"runner", "image", "taskSha256", "adapterId", "transport"}:
            fail("REPORT_MALFORMED", "observed identity has an unsupported shape")
        expected_runner = {
            "name": "rust" if surface == "direct-rust" else "bun",
            "version": package_identity["version"],
            "commit": package_identity["sourceRevision"],
        }
        if (
            identity.get("runner") != expected_runner
            or identity.get("image") != image
            or identity.get("taskSha256") != expected_task_digest
            or identity.get("adapterId") != "mock/in-memory"
            or identity.get("transport") != "deterministic"
        ):
            fail("REPORT_MALFORMED", "observed task/runner/image identity differs")
        command = require_object(record.get("command"), "invocation command")
        executable = command.get("executable")
        if (
            set(command) != {"executable", "argv", "shell"}
            or executable != expected_executable_paths[surface]
            or command.get("argv") != list(INVOCATION_ARGV)
            or command.get("shell") is not False
        ):
            fail("REPORT_MALFORMED", "invocation command provenance differs")
        by_surface.setdefault(surface, []).append(wall)
    if set(by_surface) != set(expected_surfaces) or any(
        len(by_surface[surface]) != trials for surface in expected_surfaces
    ):
        fail("REPORT_MALFORMED", "invocation trial counts differ across surfaces")
    summaries = []
    for surface in sorted(by_surface):
        values = by_surface[surface]
        summaries.append(
            {
                "surface": surface,
                "count": len(values),
                "minimumWallMs": round(min(values), 6),
                "medianWallMs": round(statistics.median(values), 6),
                "maximumWallMs": round(max(values), 6),
            }
        )
    return {
        "schema": ANALYSIS_SCHEMA,
        "sourceReportSha256": sha256_bytes(render_json(value)),
        "packageIdentity": package_identity,
        "authority": {
            "reportIdentity": "capture-bound",
            "packageReauthentication": "not-performed",
            "packageAndInstallDirectoriesRequiredForReauthentication": True,
        },
        "installations": install_summary,
        "surfaceTimingSummaries": summaries,
        "limitations": value.get("limitations"),
    }


def verify_retained_install_trees(install_root: Path, report: Any) -> dict[str, str]:
    """Recompute every captured tree against an explicitly retained install root."""
    value = require_object(report, "benchmark report")
    analyse_report(value)
    root = install_root.absolute()
    try:
        metadata = root.lstat()
        canonical_root = root.resolve(strict=True)
    except OSError as error:
        fail("INSTALL_UNSAFE", f"cannot inspect retained install root: {error}")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("INSTALL_UNSAFE", "retained install root must be a non-symlink directory")
    marker = root / ".openprose-installed-benchmark-root"
    if safe_read(marker, 128) != b"owned disposable benchmark root\n":
        fail("INSTALL_UNSAFE", "retained install ownership marker differs")
    records = {
        require_string(record.get("surface"), "installation surface"): record
        for record in value["installations"]
    }
    roots = {
        "direct-rust": root / "rust-standalone",
        "direct-bun": root / "bun-standalone",
        "npm-launcher": root / "npm-prefix",
    }
    verified: dict[str, str] = {}
    for surface in ("direct-rust", "direct-bun", "npm-launcher"):
        tree_root = roots[surface]
        try:
            tree_root.resolve(strict=True).relative_to(canonical_root)
        except (OSError, ValueError):
            fail("INSTALL_UNSAFE", f"retained {surface} root escapes the install root")
        allowed: dict[Path, Path] = {}
        if surface == "npm-launcher":
            command, meta, _, _ = npm_layout(tree_root, value["platform"])
            try:
                if command.is_symlink():
                    allowed[command] = meta / "bin" / "prose.js"
            except OSError as error:
                fail("INSTALL_MUTATED", f"cannot inspect retained npm command: {error}")
        verified[surface] = verify_installed_tree(
            tree_root,
            records[surface]["treeIdentity"],
            allowed,
        )
    return verified


class ClosedParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        fail("ARGUMENT_INVALID", message)


def parser() -> argparse.ArgumentParser:
    result = ClosedParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--packages", type=Path, required=True)
    run.add_argument("--install-root", type=Path, required=True)
    run.add_argument("--trials", type=int, default=5)
    run.add_argument("--timeout-seconds", type=float, default=10)
    analyse = commands.add_parser("analyse")
    analyse.add_argument("--report", type=Path, required=True)
    return result


def main(arguments: Iterable[str] | None = None) -> int:
    try:
        options = parser().parse_args(
            list(arguments) if arguments is not None else None
        )
        if options.command == "run":
            value = run_benchmark(
                options.packages,
                options.install_root,
                trials=options.trials,
                timeout_seconds=options.timeout_seconds,
            )
        else:
            encoded = safe_read(options.report, 64 * 1024 * 1024)
            value = analyse_report(json_no_duplicates(encoded, "benchmark report"))
        sys.stdout.buffer.write(render_json(value))
        return 0
    except BenchmarkError as error:
        sys.stdout.buffer.write(
            render_json(
                {"schema": ERROR_SCHEMA, "code": error.code, "message": error.message}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
