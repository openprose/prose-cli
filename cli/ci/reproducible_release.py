#!/usr/bin/env python3
"""Capture receipts and compare two independently rooted release cohorts.

The gate intentionally does not normalize artifacts. It first establishes that
the canonical receipts bind the same source, image, runner, exact tool bytes,
and nine-file package inventory, then compares every inventory member byte for
byte. The workflow may upload only the already-compared first cohort.

Platform evidence is reauthenticated against the packaged executables. Linux
uses the exact receipt-bound ``readelf`` to inspect the Rust standalone ELF,
Bun standalone ELF, and npm-platform Bun copy; requires the Bun copies to be
identical; and verifies the recorded GLIBC maxima and fixed floor. Darwin
receipts bind exact ``codesign`` and ``otool`` tools, while the verifier uses
that exact ``otool`` to inspect the equivalent Mach-O copies and enforce the
alpha minimum-OS ceiling. Strict codesign execution remains the packager and
package-admission gates' responsibility.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform as host_platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Callable, Iterable


REPORT_SCHEMA = "openprose.reproducible-release-report/1"
REPORT_VERSION = 1
RECEIPT_SCHEMA = "openprose.release-build-receipt/1"

EXIT_OK = 0
EXIT_INPUT_REJECTED = 2
EXIT_NON_COMPARABLE = 3
EXIT_BYTE_MISMATCH = 4
EXIT_DARWIN_POLICY = 5

MAX_RECEIPT_BYTES = 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOOL_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 128
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_OTOOL_OUTPUT = 1024 * 1024
MAX_READELF_OUTPUT = 4 * 1024 * 1024
OTOOL_TIMEOUT_SECONDS = 10
READELF_TIMEOUT_SECONDS = 15
LINUX_MINIMUM_GLIBC = "2.34"
LINUX_EXECUTION_EVIDENCE = "ubuntu-22.04-only"

HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?\Z"
)
ALPHA_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-alpha\.(?:0|[1-9][0-9]*)\Z"
)
PORTABLE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:/@ -]{0,255}\Z")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
VERSION_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2}\Z")
GLIBC_REQUIREMENT = re.compile(rb"\bName:\s+GLIBC_([0-9]+)\.([0-9]+)(?:\.([0-9]+))?\b")

PLATFORMS = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64-gnu",
    "linux-x64-gnu",
    "win32-x64",
}
BASE_TOOLS = {"bun", "cargo", "linker", "node", "npm", "python", "rustc"}
TOP_KEYS = {
    "channel",
    "image",
    "inventory",
    "platform",
    "runner",
    "schema",
    "sourceRevision",
    "tools",
    "version",
}
IMAGE_KEYS = {"formatVersion", "manifestSha256", "releaseEligible", "sha256", "version"}
RUNNER_KEYS = {"architecture", "image", "imageVersion", "kernel", "os"}
TOOL_KEYS = {"byteLength", "path", "sha256", "version"}
RELEASE_MANIFEST_KEYS = {
    "artifacts",
    "buildProfiles",
    "bunRuntime",
    "claims",
    "dependencyEvidence",
    "externalGates",
    "image",
    "linuxRuntime",
    "lockfiles",
    "mode",
    "platform",
    "promotion",
    "publicationAuthorized",
    "releaseEligible",
    "schema",
    "source",
    "sourceDateEpoch",
    "toolchains",
    "version",
    "windowsJobObjectReleaseAdmission",
    "windowsProcessHost",
}
PACKAGE_IMAGE_KEYS = IMAGE_KEYS | {"purpose"}
ARTIFACT_KEYS = {
    "byteLength",
    "implementation",
    "kind",
    "path",
    "platform",
    "sha256",
}


class GateError(Exception):
    """A deterministic, user-correctable gate input failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, message: str) -> GateError:
    return GateError(code, message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def render_report(report: dict[str, Any]) -> bytes:
    """Return the one deterministic report encoding used by stdout."""

    return _canonical_json(report)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _require_direct_ancestors(path: Path, *, purpose: str, code: str) -> None:
    """Reject a path reached through any symlinked parent component."""

    if ".." in path.parts:
        raise _error(code, f"{purpose} must not contain parent-directory segments")
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:-1]:
        current /= part
        try:
            status = current.lstat()
        except OSError as exc:
            raise _error(
                code,
                f"{purpose} parent is unavailable: {current}: {exc.strerror or exc}",
            ) from None
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise _error(
                code,
                f"{purpose} must not traverse a symlink or non-directory "
                f"parent: {current}",
            )


def _read_regular(path: Path, *, purpose: str, maximum: int, code: str) -> bytes:
    _require_direct_ancestors(path, purpose=purpose, code=code)
    try:
        before = path.lstat()
    except OSError as exc:
        raise _error(
            code, f"{purpose} is unavailable: {path}: {exc.strerror or exc}"
        ) from None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _error(code, f"{purpose} must be a direct regular file: {path}")
    if before.st_size > maximum:
        raise _error(code, f"{purpose} exceeds the {maximum}-byte limit: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ) or not stat.S_ISREG(opened.st_mode):
                raise _error(code, f"{purpose} changed before it could be read: {path}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise _error(
                        code, f"{purpose} exceeds the {maximum}-byte limit: {path}"
                    )
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except GateError:
        raise
    except OSError as exc:
        raise _error(
            code, f"{purpose} could not be read safely: {path}: {exc.strerror or exc}"
        ) from None
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise _error(code, f"{purpose} changed while it was read: {path}")
    try:
        path_after = path.lstat()
    except OSError:
        raise _error(code, f"{purpose} disappeared while it was read: {path}") from None
    path_identity = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_size,
        path_after.st_mtime_ns,
    )
    if stat.S_ISLNK(path_after.st_mode) or path_identity != identity_after:
        raise _error(code, f"{purpose} path changed while it was read: {path}")
    return b"".join(chunks)


def _require_exact_keys(value: object, keys: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = (
            sorted(value)
            if isinstance(value, dict) and all(isinstance(key, str) for key in value)
            else []
        )
        raise _error(
            "INVALID_RECEIPT",
            f"{context} must have exact keys {sorted(keys)}; got {actual}",
        )
    return value


def _portable_string(value: object, context: str) -> str:
    if (
        not isinstance(value, str)
        or not PORTABLE_VALUE.fullmatch(value)
        or any(ord(char) < 32 for char in value)
    ):
        raise _error(
            "INVALID_RECEIPT", f"{context} must be a non-empty portable string"
        )
    return value


def _absolute_tool_path(value: object, context: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise _error(
            "INVALID_RECEIPT", f"{context} must be a bounded printable absolute path"
        )
    path = value
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    is_absolute = posix.is_absolute() or (windows.is_absolute() and bool(windows.drive))
    parts = posix.parts if posix.is_absolute() else windows.parts
    if not is_absolute or ".." in parts or "." in parts:
        raise _error(
            "INVALID_RECEIPT",
            f"{context} must be an absolute path without dot segments",
        )
    return path


def _inventory(version: str, platform_id: str) -> list[str]:
    return sorted(
        [
            "SHA256SUMS",
            "dependency-evidence.json",
            "provenance.json",
            "release-manifest.json",
            "sbom.cdx.json",
            f"openprose-prose-cli-{version}.tgz",
            f"openprose-prose-cli-{platform_id}-{version}.tgz",
            f"openprose-prose-cli-bun-{version}-{platform_id}.tar.gz",
            f"openprose-prose-cli-rust-{version}-{platform_id}.tar.gz",
        ]
    )


def _load_receipt(path: Path, side: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(
        path,
        purpose=f"{side} receipt",
        maximum=MAX_RECEIPT_BYTES,
        code="UNSAFE_RECEIPT",
    )
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(
            "INVALID_RECEIPT_JSON",
            f"{side} receipt is not duplicate-free UTF-8 JSON: {exc}",
        ) from None
    if raw != _canonical_json(value):
        raise _error(
            "NONCANONICAL_RECEIPT",
            f"{side} receipt must use canonical sorted compact JSON plus one newline",
        )
    return _validate_receipt_value(value, side), raw


def _validate_receipt_value(value: object, side: str) -> dict[str, Any]:
    receipt = _require_exact_keys(value, TOP_KEYS, f"{side} receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise _error(
            "INVALID_RECEIPT", f"{side} receipt schema must be {RECEIPT_SCHEMA}"
        )
    revision = receipt["sourceRevision"]
    if not isinstance(revision, str) or not HEX40.fullmatch(revision):
        raise _error(
            "INVALID_RECEIPT",
            f"{side} sourceRevision must be a lowercase 40-hex commit",
        )
    version = receipt["version"]
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise _error("INVALID_RECEIPT", f"{side} version must be an exact SemVer")
    channel = receipt["channel"]
    if not isinstance(channel, str) or channel not in {"alpha", "release"}:
        raise _error("INVALID_RECEIPT", f"{side} channel must be alpha or release")
    if channel == "alpha" and not ALPHA_SEMVER.fullmatch(version):
        raise _error("INVALID_RECEIPT", f"{side} alpha version must be X.Y.Z-alpha.N")
    platform_id = receipt["platform"]
    if not isinstance(platform_id, str) or platform_id not in PLATFORMS:
        raise _error(
            "INVALID_RECEIPT", f"{side} platform is not admitted: {platform_id!r}"
        )

    image = _require_exact_keys(receipt["image"], IMAGE_KEYS, f"{side} image")
    _portable_string(image["formatVersion"], f"{side} image.formatVersion")
    _portable_string(image["version"], f"{side} image.version")
    for key in ("sha256", "manifestSha256"):
        if not isinstance(image[key], str) or not HEX64.fullmatch(image[key]):
            raise _error(
                "INVALID_RECEIPT", f"{side} image.{key} must be lowercase SHA-256"
            )
    if image["releaseEligible"] is not True:
        raise _error("INVALID_RECEIPT", f"{side} image.releaseEligible must be true")

    runner = _require_exact_keys(receipt["runner"], RUNNER_KEYS, f"{side} runner")
    for key in sorted(RUNNER_KEYS):
        _portable_string(runner[key], f"{side} runner.{key}")
    expected_runner = {
        "darwin-arm64": ("macOS", "arm64"),
        "darwin-x64": ("macOS", "x64"),
        "linux-arm64-gnu": ("Linux", "arm64"),
        "linux-x64-gnu": ("Linux", "x64"),
        "win32-x64": ("Windows", "x64"),
    }[platform_id]
    if (runner["os"], runner["architecture"]) != expected_runner:
        raise _error(
            "INVALID_RECEIPT",
            f"{side} runner os/architecture must match "
            f"{platform_id}: {expected_runner}",
        )

    tools = receipt["tools"]
    if not isinstance(tools, dict) or not BASE_TOOLS.issubset(tools):
        raise _error(
            "INVALID_RECEIPT", f"{side} tools must include {sorted(BASE_TOOLS)}"
        )
    if platform_id.startswith("darwin-") and not {"codesign", "otool"}.issubset(tools):
        raise _error(
            "INVALID_RECEIPT", f"{side} Darwin tools must include codesign and otool"
        )
    if platform_id.startswith("linux-") and "readelf" not in tools:
        raise _error("INVALID_RECEIPT", f"{side} Linux tools must include readelf")
    for name in sorted(tools):
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
            raise _error("INVALID_RECEIPT", f"{side} tool name is invalid: {name!r}")
        tool = _require_exact_keys(tools[name], TOOL_KEYS, f"{side} tool {name}")
        _portable_string(tool["version"], f"{side} tools.{name}.version")
        _absolute_tool_path(tool["path"], f"{side} tools.{name}.path")
        if type(tool["byteLength"]) is not int or tool["byteLength"] <= 0:
            raise _error(
                "INVALID_RECEIPT",
                f"{side} tools.{name}.byteLength must be a positive integer",
            )
        if not isinstance(tool["sha256"], str) or not HEX64.fullmatch(tool["sha256"]):
            raise _error(
                "INVALID_RECEIPT",
                f"{side} tools.{name}.sha256 must be lowercase SHA-256",
            )

    expected = _inventory(version, platform_id)
    actual_inventory = receipt["inventory"]
    if actual_inventory != expected:
        raise _error(
            "RECEIPT_INVENTORY_MISMATCH",
            f"{side} receipt inventory must exactly equal {expected}",
        )
    return receipt


def _safe_package_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and all(ord(char) >= 32 for char in name)
    )


def _direct_path_token(
    path: Path,
    *,
    purpose: str,
    directory: bool,
    code: str,
) -> tuple[Any, ...]:
    """Return one no-follow filesystem identity for an admitted direct path."""

    _require_direct_ancestors(path, purpose=purpose, code=code)
    try:
        observed = path.lstat()
    except OSError as exc:
        raise _error(
            code,
            f"{purpose} is unavailable: {path}: {exc.strerror or exc}",
        ) from None
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if stat.S_ISLNK(observed.st_mode) or not expected_kind(observed.st_mode):
        kind = "directory" if directory else "regular file"
        raise _error(code, f"{purpose} must be a direct {kind}: {path}")
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _filesystem_identity(token: tuple[Any, ...]) -> tuple[Any, Any]:
    return token[0], token[1]


def _reject_aliased_cohorts(
    left_package_tokens: tuple[tuple[Any, ...], ...],
    right_package_tokens: tuple[tuple[Any, ...], ...],
    left_receipt_token: tuple[Any, ...],
    right_receipt_token: tuple[Any, ...],
) -> None:
    """Require separately mutable roots, receipts, and package members."""

    left_root = _filesystem_identity(left_package_tokens[0])
    right_root = _filesystem_identity(right_package_tokens[0])
    left_receipt = _filesystem_identity(left_receipt_token)
    right_receipt = _filesystem_identity(right_receipt_token)
    if left_root == right_root:
        raise _error(
            "ALIASED_BUILD_COHORT",
            "left and right package roots must be distinct filesystem directories",
        )
    if left_receipt == right_receipt:
        raise _error(
            "ALIASED_BUILD_COHORT",
            "left and right receipts must be distinct filesystem files",
        )
    left_members = {
        _filesystem_identity(token) for token in left_package_tokens[1:]
    }
    right_members = {
        _filesystem_identity(token) for token in right_package_tokens[1:]
    }
    if left_members & right_members:
        raise _error(
            "ALIASED_BUILD_COHORT",
            "left and right packages must not share hard-linked inventory members",
        )
    left_cohort = {left_root, left_receipt, *left_members}
    right_cohort = {right_root, right_receipt, *right_members}
    if left_cohort & right_cohort:
        raise _error(
            "ALIASED_BUILD_COHORT",
            "left and right build cohorts must not share filesystem objects",
        )


def _snapshot_package(root: Path, expected: list[str], side: str) -> dict[str, bytes]:
    _require_direct_ancestors(
        root, purpose=f"{side} package root", code="UNSAFE_PACKAGE_ROOT"
    )
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise _error(
            "UNSAFE_PACKAGE_ROOT",
            f"{side} package root is unavailable: {root}: {exc.strerror or exc}",
        ) from None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise _error(
            "UNSAFE_PACKAGE_ROOT",
            f"{side} package root must be a direct directory: {root}",
        )
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise _error(
            "UNSAFE_PACKAGE_ROOT",
            f"{side} package root cannot be enumerated: {root}: {exc.strerror or exc}",
        ) from None
    names = sorted(entry.name for entry in entries)
    if any(not _safe_package_name(name) for name in names) or len(
        {name.casefold() for name in names}
    ) != len(names):
        raise _error(
            "UNSAFE_INVENTORY",
            f"{side} package inventory contains an unsafe or case-colliding name",
        )
    if names != expected:
        raise _error(
            "PACKAGE_INVENTORY_MISMATCH",
            f"{side} package inventory must exactly equal {expected}; got {names}",
        )
    snapshot: dict[str, bytes] = {}
    total = 0
    for name in expected:
        data = _read_regular(
            root / name,
            purpose=f"{side} package member {name}",
            maximum=MAX_FILE_BYTES,
            code="UNSAFE_PACKAGE_MEMBER",
        )
        total += len(data)
        if total > MAX_PACKAGE_BYTES:
            raise _error(
                "PACKAGE_TOO_LARGE",
                f"{side} package exceeds the {MAX_PACKAGE_BYTES}-byte aggregate limit",
            )
        snapshot[name] = data
    after = sorted(entry.name for entry in os.scandir(root))
    try:
        root_after = root.lstat()
    except OSError:
        raise _error(
            "UNSAFE_PACKAGE_ROOT",
            f"{side} package root disappeared after snapshot: {root}",
        ) from None
    root_identity = (root_stat.st_dev, root_stat.st_ino, root_stat.st_mtime_ns)
    root_after_identity = (root_after.st_dev, root_after.st_ino, root_after.st_mtime_ns)
    if after != names or root_after_identity != root_identity:
        raise _error(
            "PACKAGE_CHANGED", f"{side} package changed while it was snapshotted"
        )
    return snapshot


def _package_tokens(
    root: Path,
    expected: list[str],
    side: str = "capture",
    *,
    initial_comparison: bool = False,
) -> tuple[tuple[Any, ...], ...]:
    """Bind directory and member identities around capture without path disclosure."""

    _require_direct_ancestors(
        root, purpose=f"{side} package root", code="UNSAFE_PACKAGE_ROOT"
    )
    paths = [root, *(root / name for name in expected)]
    tokens: list[tuple[Any, ...]] = []
    for index, path in enumerate(paths):
        try:
            observed = path.lstat()
        except OSError as exc:
            unavailable_code = (
                "UNSAFE_PACKAGE_ROOT"
                if initial_comparison and index == 0
                else "PACKAGE_INVENTORY_MISMATCH"
                if initial_comparison
                else "PACKAGE_CHANGED"
            )
            raise _error(
                unavailable_code,
                f"{side} package path is unavailable: {path}: {exc.strerror or exc}",
            ) from None
        expected_kind = stat.S_ISDIR if index == 0 else stat.S_ISREG
        if stat.S_ISLNK(observed.st_mode) or not expected_kind(observed.st_mode):
            unsafe_code = (
                "UNSAFE_PACKAGE_ROOT"
                if initial_comparison and index == 0
                else "UNSAFE_PACKAGE_MEMBER"
                if initial_comparison
                else "PACKAGE_CHANGED"
            )
            raise _error(
                unsafe_code,
                f"{side} package path changed type: {path}",
            )
        tokens.append(
            (
                observed.st_dev,
                observed.st_ino,
                observed.st_mode,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
            )
        )
    return tuple(tokens)


def _parse_package_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(
            "MALFORMED_PACKAGE_METADATA",
            f"{label} is not duplicate-free UTF-8 JSON: {exc}",
        ) from None
    if not isinstance(value, dict):
        raise _error(
            "MALFORMED_PACKAGE_METADATA", f"{label} must contain a JSON object"
        )
    return value


def _validate_package_checksums(
    snapshot: dict[str, bytes], expected: list[str]
) -> None:
    checksummed = [name for name in expected if name != "SHA256SUMS"]
    exact = "".join(
        f"{_sha256(snapshot[name])}  {name}\n" for name in checksummed
    ).encode("utf-8")
    if snapshot["SHA256SUMS"] != exact:
        raise _error(
            "CHECKSUM_MISMATCH",
            "SHA256SUMS must exactly bind every other closed package member",
        )



def _archive_members_for_sdk(data, name):
    members = {}; portable = set(); total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for member in archive:
                if (len(members) >= MAX_ARCHIVE_MEMBERS or not _safe_archive_name(member.name)
                        or member.name.casefold() in portable or not member.isfile()
                        or not 0 <= member.size <= MAX_FILE_BYTES):
                    raise _error('UNSAFE_ARCHIVE', 'Unsafe SDK archive member: ' + name)
                portable.add(member.name.casefold()); total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise _error('UNSAFE_ARCHIVE', 'SDK archive exceeds expanded bound: ' + name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise _error('UNSAFE_ARCHIVE', 'SDK archive member cannot be read: ' + name)
                content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) != member.size:
                    raise _error('UNSAFE_ARCHIVE', 'SDK archive member length differs: ' + name)
                members[member.name] = content
    except tarfile.TarError:
        raise _error('UNSAFE_ARCHIVE', 'Malformed SDK archive: ' + name) from None
    return members


def _capture_image_and_channel(
    snapshot: dict[str, bytes],
    *,
    source_revision: str,
    version: str,
    platform_id: str,
) -> tuple[dict[str, Any], str]:
    manifest = _parse_package_json(
        snapshot["release-manifest.json"], "release-manifest.json"
    )
    if set(manifest) not in (RELEASE_MANIFEST_KEYS, RELEASE_MANIFEST_KEYS | {"agentsSdk"}):
        raise _error(
            "MALFORMED_PACKAGE_METADATA",
            "release-manifest.json top-level keys are not exact",
        )
    if manifest["schema"] != "openprose.local-release-manifest/1":
        raise _error(
            "MALFORMED_PACKAGE_METADATA",
            "release-manifest.json schema is unsupported",
        )
    mode = manifest["mode"]
    if mode not in {"alpha", "release"}:
        raise _error(
            "PACKAGE_IDENTITY_MISMATCH",
            "capture accepts only alpha or release package modes",
        )
    if mode == "release":
        import kernel_rc_evidence as custody
        try:
            custody.validate_sdk_archives(manifest, lambda name: _archive_members_for_sdk(snapshot[name], name))
        except (ValueError, KeyError, TypeError) as error:
            raise _error("PACKAGE_IDENTITY_MISMATCH", str(error)) from None
    for field, supplied in (
        ("version", version),
        ("platform", platform_id),
    ):
        if manifest[field] != supplied:
            raise _error(
                "PACKAGE_IDENTITY_MISMATCH",
                f"release-manifest.json {field} does not match supplied {field}",
            )
    source = manifest["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"revision", "verification"}
        or source["revision"] != source_revision
        or source["verification"] != "matched-product-doctor"
    ):
        raise _error(
            "PACKAGE_IDENTITY_MISMATCH",
            "release-manifest.json source does not match the supplied revision",
        )
    image = _require_exact_keys(
        manifest["image"], PACKAGE_IMAGE_KEYS, "release package image"
    )
    expected_purpose = {
        "alpha": "functional-alpha-placeholder",
        "release": "canonical-language-runtime",
    }[mode]
    if image["purpose"] != expected_purpose:
        raise _error(
            "PACKAGE_IDENTITY_MISMATCH",
            f"release package image purpose must be {expected_purpose}",
        )
    receipt_image = {key: image[key] for key in IMAGE_KEYS}

    artifacts = manifest["artifacts"]
    expected_artifact_identity = {
        f"openprose-prose-cli-{version}.tgz": ("bun", "npm-meta", None),
        f"openprose-prose-cli-{platform_id}-{version}.tgz": (
            "bun",
            "npm-platform",
            platform_id,
        ),
        f"openprose-prose-cli-bun-{version}-{platform_id}.tar.gz": (
            "bun",
            "standalone-archive",
            platform_id,
        ),
        f"openprose-prose-cli-rust-{version}-{platform_id}.tar.gz": (
            "rust",
            "standalone-archive",
            platform_id,
        ),
    }
    expected_artifacts = sorted(expected_artifact_identity)
    if not isinstance(artifacts, list) or len(artifacts) != len(expected_artifacts):
        raise _error(
            "MALFORMED_PACKAGE_METADATA",
            "release-manifest.json artifact inventory is not exact",
        )
    observed: set[str] = set()
    for artifact in artifacts:
        record = _require_exact_keys(
            artifact, ARTIFACT_KEYS, "release package artifact"
        )
        path = record["path"]
        if (
            not isinstance(path, str)
            or path not in expected_artifacts
            or path in observed
        ):
            raise _error(
                "MALFORMED_PACKAGE_METADATA",
                "release-manifest.json artifact path inventory is not exact",
            )
        observed.add(path)
        (
            expected_implementation,
            expected_kind,
            expected_platform,
        ) = expected_artifact_identity[path]
        if (
            type(record["byteLength"]) is not int
            or record["byteLength"] != len(snapshot[path])
            or record["sha256"] != _sha256(snapshot[path])
            or record["implementation"] != expected_implementation
            or record["kind"] != expected_kind
            or record["platform"] != expected_platform
        ):
            raise _error(
                "PACKAGE_IDENTITY_MISMATCH",
                f"release-manifest.json artifact identity is stale: {path}",
            )
    if observed != set(expected_artifacts):
        raise _error(
            "MALFORMED_PACKAGE_METADATA",
            "release-manifest.json artifact inventory is not closed",
        )
    return receipt_image, mode


def _identity_differences(
    left: dict[str, Any], right: dict[str, Any]
) -> list[dict[str, str]]:
    differences: list[dict[str, str]] = []
    for field in (
        "schema",
        "sourceRevision",
        "version",
        "platform",
        "channel",
        "image",
        "inventory",
    ):
        if left[field] != right[field]:
            differences.append({"code": "BUILD_IDENTITY_MISMATCH", "field": field})
    if left["tools"] != right["tools"]:
        differences.append({"code": "TOOL_IDENTITY_MISMATCH", "field": "tools"})
    if left["runner"] != right["runner"]:
        differences.append({"code": "RUNNER_IDENTITY_MISMATCH", "field": "runner"})
    return differences


def _file_rows(
    expected: Iterable[str], left: dict[str, bytes], right: dict[str, bytes]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in expected:
        left_data = left[name]
        right_data = right[name]
        rows.append(
            {
                "equal": left_data == right_data,
                "left": {"byteLength": len(left_data), "sha256": _sha256(left_data)},
                "path": name,
                "right": {"byteLength": len(right_data), "sha256": _sha256(right_data)},
            }
        )
    return rows


def _current_tool(name: str, declared: dict[str, Any]) -> dict[str, Any]:
    path = Path(declared["path"])
    data = _read_regular(
        path,
        purpose=f"declared tool {name}",
        maximum=MAX_TOOL_BYTES,
        code="UNSAFE_TOOL",
    )
    try:
        status = path.lstat()
    except OSError as exc:
        raise _error(
            "UNSAFE_TOOL",
            f"declared tool {name} disappeared after snapshot: {path}: "
            f"{exc.strerror or exc}",
        ) from None
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISREG(status.st_mode)
        or not status.st_mode & 0o111
        or not os.access(path, os.X_OK)
    ):
        raise _error(
            "UNSAFE_TOOL",
            f"declared tool {name} must be a direct executable regular file: {path}",
        )
    return {
        "data": data,
        "token": (
            status.st_dev,
            status.st_ino,
            status.st_mode,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        ),
    }


def _authenticate_tools(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for name in sorted(receipt["tools"]):
        declared = receipt["tools"][name]
        snapshot = _current_tool(name, declared)
        data = snapshot["data"]
        actual = {"byteLength": len(data), "sha256": _sha256(data)}
        for field in ("byteLength", "sha256"):
            if declared[field] != actual[field]:
                raise _error(
                    "TOOL_IDENTITY_MISMATCH",
                    f"declared tool {name} {field} does not match exact path "
                    f"{declared['path']}",
                )
        snapshots[name] = snapshot
    return snapshots


def _reauthenticate_tools(
    receipt: dict[str, Any], snapshots: dict[str, dict[str, Any]]
) -> None:
    for name in sorted(receipt["tools"]):
        try:
            current = _current_tool(name, receipt["tools"][name])
        except GateError as exc:
            raise _error(
                "TOOL_CHANGED",
                f"declared tool {name} became unsafe during comparison: {exc.code}",
            ) from None
        if (
            current["token"] != snapshots[name]["token"]
            or current["data"] != snapshots[name]["data"]
        ):
            raise _error(
                "TOOL_CHANGED",
                f"declared tool {name} changed during comparison: "
                f"{receipt['tools'][name]['path']}",
            )


def _safe_archive_name(name: str) -> bool:
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or any(ord(char) < 32 for char in name)
    ):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(
        part not in {"", ".", ".."} for part in path.parts
    )


def _archive_member(data: bytes, member_name: str, archive_name: str) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            identities: set[str] = set()
            total = 0
            found: bytes | None = None
            count = 0
            for member in archive:
                count += 1
                if count > MAX_ARCHIVE_MEMBERS:
                    raise _error(
                        "UNSAFE_ARCHIVE", f"{archive_name} has too many members"
                    )
                if (
                    not _safe_archive_name(member.name)
                    or member.name.casefold() in identities
                ):
                    raise _error(
                        "UNSAFE_ARCHIVE",
                        f"{archive_name} has unsafe or case-colliding member names",
                    )
                identities.add(member.name.casefold())
                if not member.isfile() or member.issym() or member.islnk():
                    raise _error(
                        "UNSAFE_ARCHIVE",
                        f"{archive_name} contains a non-regular member: {member.name}",
                    )
                if member.size < 0 or member.size > MAX_FILE_BYTES:
                    raise _error(
                        "UNSAFE_ARCHIVE",
                        f"{archive_name} member size is out of bounds: {member.name}",
                    )
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise _error(
                        "UNSAFE_ARCHIVE",
                        f"{archive_name} expanded contents exceed the aggregate limit",
                    )
                if member.name == member_name:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise _error(
                            "UNSAFE_ARCHIVE",
                            f"{archive_name} binary member cannot be read",
                        )
                    found = stream.read(MAX_FILE_BYTES + 1)
                    if len(found) != member.size:
                        raise _error(
                            "UNSAFE_ARCHIVE",
                            f"{archive_name} binary member size is inconsistent",
                        )
            if found is None:
                raise _error(
                    "UNSAFE_ARCHIVE",
                    f"{archive_name} is missing exact member {member_name}",
                )
            return found
    except GateError:
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise _error(
            "UNSAFE_ARCHIVE", f"{archive_name} is not a safe gzip tar archive: {exc}"
        ) from None


def _archive_binary(data: bytes, member_name: str, archive_name: str) -> bytes:
    return _archive_member(data, member_name, archive_name)


def _version_tuple(value: str, context: str) -> tuple[int, int, int]:
    if not VERSION_NUMBER.fullmatch(value):
        raise _error("INVALID_OTOOL_OUTPUT", f"{context} has invalid version {value!r}")
    parts = [int(part) for part in value.split(".")]
    if any(part > 65535 for part in parts):
        raise _error(
            "INVALID_OTOOL_OUTPUT", f"{context} has out-of-range version {value!r}"
        )
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def _parse_otool(output: bytes, binary: Path) -> tuple[str, str]:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise _error("INVALID_OTOOL_OUTPUT", "otool output must be UTF-8") from None
    lines = text.splitlines()
    if not lines or lines[0] != f"{binary}:":
        raise _error(
            "INVALID_OTOOL_OUTPUT",
            "otool output does not identify the exact inspected binary",
        )
    starts = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"Load command [0-9]+", line)
    ]
    blocks: list[list[str]] = []
    for index, start in enumerate(starts):
        blocks.append(
            lines[start : starts[index + 1] if index + 1 < len(starts) else len(lines)]
        )
    candidates: list[tuple[str, str]] = []
    for block in blocks:
        stripped = [line.strip() for line in block]
        if "cmd LC_BUILD_VERSION" in stripped:
            platforms = [
                line.split(maxsplit=1)[1]
                for line in stripped
                if line.startswith("platform ")
            ]
            minimums = [
                line.split(maxsplit=1)[1]
                for line in stripped
                if line.startswith("minos ")
            ]
            sdks = [
                line.split(maxsplit=1)[1]
                for line in stripped
                if line.startswith("sdk ")
            ]
            # Apple otool renders the LC_BUILD_VERSION platform as its numeric
            # Mach-O constant (1), while llvm-otool renders the symbolic name.
            # Admit only those two exact spellings of PLATFORM_MACOS.
            if (
                platforms not in (["MACOS"], ["1"])
                or len(minimums) != 1
                or len(sdks) != 1
            ):
                raise _error(
                    "INVALID_OTOOL_OUTPUT",
                    "LC_BUILD_VERSION must contain one macOS platform, minos, and sdk",
                )
            candidates.append((minimums[0], sdks[0]))
        elif "cmd LC_VERSION_MIN_MACOSX" in stripped:
            minimums = [
                line.split(maxsplit=1)[1]
                for line in stripped
                if line.startswith("version ")
            ]
            sdks = [
                line.split(maxsplit=1)[1]
                for line in stripped
                if line.startswith("sdk ")
            ]
            if len(minimums) != 1 or len(sdks) != 1:
                raise _error(
                    "INVALID_OTOOL_OUTPUT",
                    "LC_VERSION_MIN_MACOSX must contain one version and sdk",
                )
            candidates.append((minimums[0], sdks[0]))
    if len(candidates) != 1:
        raise _error(
            "INVALID_OTOOL_OUTPUT",
            "otool output must contain exactly one macOS minimum-version load command",
        )
    minimum, sdk = candidates[0]
    _version_tuple(minimum, "minimum OS")
    _version_tuple(sdk, "SDK")
    return minimum, sdk


def _validate_otool(
    path: Path,
    receipt: dict[str, Any],
    tool_snapshots: dict[str, dict[str, Any]],
) -> bytes:
    if not path.is_absolute():
        raise _error("INVALID_OTOOL_PATH", "--otool must be an absolute path")
    declared = receipt["tools"]["otool"]
    if str(path) != declared["path"]:
        raise _error(
            "OTOOL_IDENTITY_MISMATCH",
            "--otool must exactly equal the comparable receipts' tools.otool.path",
        )
    return tool_snapshots["otool"]["data"]


def _run_otool(otool: Path, expected_otool: bytes, binary: Path) -> tuple[str, str]:
    before = _read_regular(
        otool, purpose="otool", maximum=MAX_FILE_BYTES, code="UNSAFE_OTOOL"
    )
    if before != expected_otool:
        raise _error("OTOOL_IDENTITY_MISMATCH", "otool changed before invocation")
    try:
        result = subprocess.run(
            [str(otool), "-l", str(binary)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout=OTOOL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _error("OTOOL_FAILED", f"otool invocation failed: {exc}") from None
    after = _read_regular(
        otool, purpose="otool", maximum=MAX_FILE_BYTES, code="UNSAFE_OTOOL"
    )
    if after != expected_otool:
        raise _error("OTOOL_IDENTITY_MISMATCH", "otool changed during invocation")
    if len(result.stdout) > MAX_OTOOL_OUTPUT or len(result.stderr) > MAX_OTOOL_OUTPUT:
        raise _error("OTOOL_FAILED", "otool output exceeds the bounded output limit")
    if result.returncode != 0 or result.stderr:
        raise _error(
            "OTOOL_FAILED",
            f"otool must exit zero with empty stderr; exit={result.returncode}",
        )
    return _parse_otool(result.stdout, binary)


def _validate_readelf(
    path: Path,
    receipt: dict[str, Any],
    tool_snapshots: dict[str, dict[str, Any]],
) -> bytes:
    if not path.is_absolute():
        raise _error("INVALID_READELF_PATH", "--readelf must be an absolute path")
    declared = receipt["tools"]["readelf"]
    if str(path) != declared["path"]:
        raise _error(
            "READELF_IDENTITY_MISMATCH",
            "--readelf must exactly equal the comparable receipts' tools.readelf.path",
        )
    return tool_snapshots["readelf"]["data"]


def _glibc_tuple(value: str, context: str) -> tuple[int, int, int]:
    if not VERSION_NUMBER.fullmatch(value):
        raise _error(
            "INVALID_LINUX_RUNTIME", f"{context} has invalid version {value!r}"
        )
    parts = [int(part) for part in value.split(".")]
    if any(part > 1_000_000 for part in parts):
        raise _error(
            "INVALID_LINUX_RUNTIME",
            f"{context} has out-of-range version {value!r}",
        )
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def _parse_readelf_glibc(output: bytes, implementation: str) -> str:
    versions = {
        tuple(int(part or b"0") for part in match.groups())
        for match in GLIBC_REQUIREMENT.finditer(output)
    }
    if not versions:
        raise _error(
            "INVALID_READELF_OUTPUT",
            f"{implementation} ELF has no readable GLIBC version requirements",
        )
    maximum = max(versions)
    parts = maximum[:2] if maximum[2] == 0 else maximum
    value = ".".join(str(part) for part in parts)
    _glibc_tuple(value, f"{implementation} required glibc")
    return value


def _run_readelf(
    readelf: Path,
    expected_readelf: bytes,
    binary: Path,
    implementation: str,
) -> str:
    before = _read_regular(
        readelf, purpose="readelf", maximum=MAX_TOOL_BYTES, code="UNSAFE_READELF"
    )
    if before != expected_readelf:
        raise _error("READELF_IDENTITY_MISMATCH", "readelf changed before invocation")
    try:
        result = subprocess.run(
            [str(readelf), "--version-info", "--wide", str(binary)],
            cwd=binary.parent,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=READELF_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _error("READELF_FAILED", f"readelf invocation failed: {exc}") from None
    after = _read_regular(
        readelf, purpose="readelf", maximum=MAX_TOOL_BYTES, code="UNSAFE_READELF"
    )
    if after != expected_readelf:
        raise _error("READELF_IDENTITY_MISMATCH", "readelf changed during invocation")
    if (
        len(result.stdout) > MAX_READELF_OUTPUT
        or len(result.stderr) > MAX_READELF_OUTPUT
    ):
        raise _error(
            "READELF_FAILED", "readelf output exceeds the bounded output limit"
        )
    if result.returncode != 0 or result.stderr:
        raise _error(
            "READELF_FAILED",
            f"readelf must exit zero with empty stderr; exit={result.returncode}",
        )
    return _parse_readelf_glibc(result.stdout, implementation)


def _linux_runtime_claim(
    snapshot: dict[str, bytes], receipt: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _parse_package_json(
        snapshot["release-manifest.json"], "release-manifest.json"
    )
    runtime = _require_exact_keys(
        manifest.get("linuxRuntime"),
        {"executionEvidence", "minimumGlibc", "requiredGlibcMaximum"},
        "release-manifest.json linuxRuntime",
    )
    if runtime["minimumGlibc"] != LINUX_MINIMUM_GLIBC:
        raise _error(
            "INVALID_LINUX_RUNTIME",
            f"Linux minimum glibc must be {LINUX_MINIMUM_GLIBC}",
        )
    if runtime["executionEvidence"] != LINUX_EXECUTION_EVIDENCE:
        raise _error(
            "INVALID_LINUX_RUNTIME",
            f"Linux execution evidence must be {LINUX_EXECUTION_EVIDENCE}",
        )
    required = _require_exact_keys(
        runtime["requiredGlibcMaximum"],
        {"bun", "rust"},
        "release-manifest.json requiredGlibcMaximum",
    )
    for implementation in ("bun", "rust"):
        value = required[implementation]
        if not isinstance(value, str):
            raise _error(
                "INVALID_LINUX_RUNTIME",
                f"{implementation} required glibc must be a dotted version",
            )
        if _glibc_tuple(value, f"{implementation} required glibc") > _glibc_tuple(
            LINUX_MINIMUM_GLIBC, "minimum glibc"
        ):
            raise _error(
                "LINUX_GLIBC_FLOOR_EXCEEDED",
                f"{implementation} requires glibc {value}, newer than the admitted "
                f"{LINUX_MINIMUM_GLIBC} floor",
            )

    platform_id = receipt["platform"]
    version = receipt["version"]
    npm_name = f"openprose-prose-cli-{platform_id}-{version}.tgz"
    npm_manifest = _parse_package_json(
        _archive_member(snapshot[npm_name], "package/package.json", npm_name),
        f"{npm_name}:package/package.json",
    )
    expected_npm_claims = {
        "openproseLinuxExecutionEvidence": LINUX_EXECUTION_EVIDENCE,
        "openproseMinimumGlibc": LINUX_MINIMUM_GLIBC,
        "openproseRequiredGlibcMaximum": required["bun"],
    }
    for key, expected in expected_npm_claims.items():
        if npm_manifest.get(key) != expected:
            raise _error(
                "INVALID_LINUX_RUNTIME",
                f"npm Linux runtime claim {key} does not match release-manifest.json",
            )
    return runtime, npm_manifest


def _linux_evidence(
    snapshot: dict[str, bytes],
    receipt: dict[str, Any],
    readelf: Path,
    expected_readelf: bytes,
) -> list[dict[str, Any]]:
    runtime, _npm_manifest = _linux_runtime_claim(snapshot, receipt)
    version = receipt["version"]
    platform_id = receipt["platform"]
    binaries: list[tuple[str, str, str, bytes]] = []
    for implementation in ("bun", "rust"):
        archive_name = (
            f"openprose-prose-cli-{implementation}-{version}-{platform_id}.tar.gz"
        )
        member_name = (
            f"openprose-prose-cli-{implementation}-{version}-{platform_id}/prose"
        )
        binaries.append(
            (
                implementation,
                archive_name,
                member_name,
                _archive_binary(snapshot[archive_name], member_name, archive_name),
            )
        )
    npm_name = f"openprose-prose-cli-{platform_id}-{version}.tgz"
    npm_binary = _archive_binary(snapshot[npm_name], "package/bin/prose", npm_name)
    if npm_binary != binaries[0][3]:
        raise _error(
            "BUN_SURFACE_MISMATCH",
            "Bun standalone and npm platform binaries must be byte-identical",
        )

    evidence: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="openprose-elf-") as temp:
        root = Path(temp).resolve()
        for implementation, archive_name, member_name, data in binaries:
            binary = root / f"{implementation}-prose"
            descriptor = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            try:
                offset = 0
                while offset < len(data):
                    written = os.write(descriptor, data[offset:])
                    if written <= 0:
                        raise _error(
                            "INSPECTED_BINARY_WRITE_FAILED",
                            f"could not materialize {implementation} ELF",
                        )
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            observed = _run_readelf(readelf, expected_readelf, binary, implementation)
            if (
                _read_regular(
                    binary,
                    purpose=f"{implementation} inspected ELF",
                    maximum=MAX_FILE_BYTES,
                    code="INSPECTED_BINARY_CHANGED",
                )
                != data
            ):
                raise _error(
                    "INSPECTED_BINARY_CHANGED",
                    f"readelf changed the {implementation} inspected binary",
                )
            if _glibc_tuple(
                observed, f"{implementation} observed glibc"
            ) > _glibc_tuple(LINUX_MINIMUM_GLIBC, "minimum glibc"):
                raise _error(
                    "LINUX_GLIBC_FLOOR_EXCEEDED",
                    f"{implementation} requires glibc {observed}, newer than the "
                    f"admitted {LINUX_MINIMUM_GLIBC} floor",
                )
            claimed = runtime["requiredGlibcMaximum"][implementation]
            if observed != claimed:
                raise _error(
                    "LINUX_RUNTIME_CLAIM_MISMATCH",
                    f"{implementation} manifest requires glibc {claimed} but exact "
                    f"readelf observed {observed}",
                )
            evidence.append(
                {
                    "archive": archive_name,
                    "byteLength": len(data),
                    "implementation": implementation,
                    "member": member_name,
                    "requiredGlibcMaximum": observed,
                    "sha256": _sha256(data),
                }
            )
    return sorted(evidence, key=lambda row: row["implementation"])


def _darwin_evidence(
    snapshot: dict[str, bytes],
    receipt: dict[str, Any],
    otool: Path,
    expected_otool: bytes,
) -> list[dict[str, Any]]:
    version = receipt["version"]
    platform_id = receipt["platform"]
    binaries: list[tuple[str, str, str, bytes]] = []
    for implementation in ("bun", "rust"):
        archive_name = (
            f"openprose-prose-cli-{implementation}-{version}-{platform_id}.tar.gz"
        )
        member_name = (
            f"openprose-prose-cli-{implementation}-{version}-{platform_id}/prose"
        )
        binaries.append(
            (
                implementation,
                archive_name,
                member_name,
                _archive_binary(snapshot[archive_name], member_name, archive_name),
            )
        )
    npm_name = f"openprose-prose-cli-{platform_id}-{version}.tgz"
    npm_binary = _archive_binary(snapshot[npm_name], "package/bin/prose", npm_name)
    if npm_binary != binaries[0][3]:
        raise _error(
            "BUN_SURFACE_MISMATCH",
            "Bun standalone and npm platform binaries must be byte-identical",
        )

    evidence: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="openprose-mach-o-") as temp:
        root = Path(temp).resolve()
        for implementation, archive_name, member_name, data in binaries:
            binary = root / f"{implementation}-prose"
            descriptor = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            try:
                offset = 0
                while offset < len(data):
                    written = os.write(descriptor, data[offset:])
                    if written <= 0:
                        raise _error(
                            "INSPECTED_BINARY_WRITE_FAILED",
                            f"could not materialize {implementation} binary",
                        )
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            minimum, sdk = _run_otool(otool, expected_otool, binary)
            if (
                _read_regular(
                    binary,
                    purpose=f"{implementation} inspected binary",
                    maximum=MAX_FILE_BYTES,
                    code="INSPECTED_BINARY_CHANGED",
                )
                != data
            ):
                raise _error(
                    "INSPECTED_BINARY_CHANGED",
                    f"otool changed the {implementation} inspected binary",
                )
            evidence.append(
                {
                    "archive": archive_name,
                    "byteLength": len(data),
                    "implementation": implementation,
                    "member": member_name,
                    "minimumOs": minimum,
                    "sdk": sdk,
                    "sha256": _sha256(data),
                }
            )
    return sorted(evidence, key=lambda row: row["implementation"])


def _base_report() -> dict[str, Any]:
    return {"reportVersion": REPORT_VERSION, "schema": REPORT_SCHEMA}


def _capture_failure(error: GateError) -> tuple[int, dict[str, Any]]:
    report = _base_report()
    report.update(
        {
            "classification": "capture-input-rejected",
            "errors": [{"code": error.code, "message": error.message}],
            "status": "error",
        }
    )
    return EXIT_INPUT_REJECTED, report


def _refuse_existing_output(output: Path) -> None:
    try:
        output.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _error(
            "UNSAFE_OUTPUT",
            f"receipt output cannot be inspected: {output}: {exc.strerror or exc}",
        ) from None
    raise _error("OUTPUT_EXISTS", f"receipt output already exists: {output}")


def _write_exclusive(
    output: Path, data: bytes, before_publish: Callable[[], None]
) -> None:
    _refuse_existing_output(output)
    _require_direct_ancestors(output, purpose="receipt output", code="UNSAFE_OUTPUT")
    parent = output.parent
    try:
        parent_status = parent.lstat()
    except OSError as exc:
        raise _error(
            "UNSAFE_OUTPUT",
            f"receipt output parent is unavailable: {parent}: {exc.strerror or exc}",
        ) from None
    if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
        raise _error(
            "UNSAFE_OUTPUT", "receipt output parent must be a direct directory"
        )
    parent_identity = (
        parent_status.st_dev,
        parent_status.st_ino,
        parent_status.st_mode,
    )

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".openprose-receipt-", dir=parent
        )
    except OSError as exc:
        raise _error(
            "OUTPUT_WRITE_FAILED",
            f"temporary receipt could not be created: {exc.strerror or exc}",
        ) from None
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise _error("OUTPUT_WRITE_FAILED", "receipt output write stalled")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if (
            _read_regular(
                temporary,
                purpose="temporary receipt output",
                maximum=MAX_RECEIPT_BYTES,
                code="OUTPUT_WRITE_FAILED",
            )
            != data
        ):
            raise _error("OUTPUT_WRITE_FAILED", "temporary receipt bytes are not exact")
        before_publish()
        try:
            parent_after = parent.lstat()
        except OSError:
            raise _error(
                "UNSAFE_OUTPUT", "receipt output parent changed before publication"
            ) from None
        if (
            parent_after.st_dev,
            parent_after.st_ino,
            parent_after.st_mode,
        ) != parent_identity:
            raise _error(
                "UNSAFE_OUTPUT", "receipt output parent changed before publication"
            )
        _refuse_existing_output(output)
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError:
            raise _error("OUTPUT_EXISTS", f"receipt output already exists: {output}")
        except OSError as exc:
            raise _error(
                "OUTPUT_WRITE_FAILED",
                f"receipt output could not be published atomically: "
                f"{exc.strerror or exc}",
            ) from None
        published = True
        if (
            _read_regular(
                output,
                purpose="published receipt output",
                maximum=MAX_RECEIPT_BYTES,
                code="OUTPUT_WRITE_FAILED",
            )
            != data
        ):
            raise _error("OUTPUT_WRITE_FAILED", "published receipt bytes are not exact")
        try:
            directory_descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise _error(
                "OUTPUT_WRITE_FAILED",
                f"receipt output directory could not be synchronized: "
                f"{exc.strerror or exc}",
            ) from None
    except BaseException as exc:
        if published:
            try:
                temporary_status = temporary.lstat()
                output_status = output.lstat()
                if (temporary_status.st_dev, temporary_status.st_ino) == (
                    output_status.st_dev,
                    output_status.st_ino,
                ):
                    output.unlink()
                    published = False
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise _error(
                "OUTPUT_WRITE_FAILED",
                f"receipt output operation failed: {exc.strerror or exc}",
            ) from None
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if not published:
            _refuse_existing_output(output)


def capture_release_receipt(
    package: Path | str,
    output: Path | str,
    *,
    source_revision: str,
    version: str,
    platform_id: str,
    runner: dict[str, str],
    tool_paths: dict[str, Path],
    tool_versions: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    """Capture one canonical receipt from a closed release-package directory."""

    try:
        package_path = Path(package)
        output_path = Path(output)
        _refuse_existing_output(output_path)
        if not isinstance(source_revision, str) or not HEX40.fullmatch(source_revision):
            raise _error(
                "INVALID_CAPTURE_INPUT",
                "--source-revision must be a lowercase 40-hex commit",
            )
        if not isinstance(version, str) or not SEMVER.fullmatch(version):
            raise _error("INVALID_CAPTURE_INPUT", "--version must be an exact SemVer")
        if platform_id not in PLATFORMS:
            raise _error(
                "INVALID_CAPTURE_INPUT", f"--platform is unsupported: {platform_id}"
            )
        if set(runner) != RUNNER_KEYS:
            raise _error(
                "INVALID_CAPTURE_INPUT",
                f"all exact runner fields are required: {sorted(RUNNER_KEYS)}",
            )
        if not tool_paths or set(tool_paths) != set(tool_versions):
            raise _error(
                "INVALID_CAPTURE_INPUT",
                "--tool and --tool-version names must form one non-empty closed set",
            )
        absolute_package = Path(os.path.abspath(package_path))
        absolute_output = Path(os.path.abspath(output_path))
        try:
            absolute_output.relative_to(absolute_package)
        except ValueError:
            pass
        else:
            raise _error(
                "UNSAFE_OUTPUT", "receipt output must be outside the package tree"
            )

        tool_records: dict[str, dict[str, Any]] = {}
        tool_snapshots: dict[str, dict[str, Any]] = {}
        for name in sorted(tool_paths):
            if not TOOL_NAME.fullmatch(name):
                raise _error("INVALID_CAPTURE_INPUT", f"tool name is invalid: {name!r}")
            tool_path = Path(tool_paths[name])
            path_text = _absolute_tool_path(str(tool_path), f"capture tool {name}")
            tool_version = _portable_string(
                tool_versions[name], f"capture tool {name} version"
            )
            current = _current_tool(name, {"path": path_text})
            tool_snapshots[name] = current
            tool_records[name] = {
                "byteLength": len(current["data"]),
                "path": path_text,
                "sha256": _sha256(current["data"]),
                "version": tool_version,
            }

        expected = _inventory(version, platform_id)
        package_tokens = _package_tokens(package_path, expected)
        snapshot = _snapshot_package(package_path, expected, "capture")
        if _package_tokens(package_path, expected) != package_tokens:
            raise _error("PACKAGE_CHANGED", "package changed during capture")
        _validate_package_checksums(snapshot, expected)
        image, channel = _capture_image_and_channel(
            snapshot,
            source_revision=source_revision,
            version=version,
            platform_id=platform_id,
        )

        receipt = {
            "channel": channel,
            "image": image,
            "inventory": expected,
            "platform": platform_id,
            "runner": dict(runner),
            "schema": RECEIPT_SCHEMA,
            "sourceRevision": source_revision,
            "tools": tool_records,
            "version": version,
        }
        receipt = _validate_receipt_value(receipt, "captured")
        if platform_id.startswith("linux-"):
            readelf = Path(tool_paths["readelf"])
            expected_readelf = _validate_readelf(readelf, receipt, tool_snapshots)
            _linux_evidence(snapshot, receipt, readelf, expected_readelf)
            _reauthenticate_tools(receipt, tool_snapshots)
        else:
            manifest = _parse_package_json(
                snapshot["release-manifest.json"], "release-manifest.json"
            )
            if manifest.get("linuxRuntime") != "not-applicable":
                raise _error(
                    "INVALID_LINUX_RUNTIME",
                    "non-Linux package must declare linuxRuntime as not-applicable",
                )
        encoded = _canonical_json(receipt)

        def reauthenticate() -> None:
            if _package_tokens(package_path, expected) != package_tokens:
                raise _error("PACKAGE_CHANGED", "package changed during capture")
            _reauthenticate_tools(receipt, tool_snapshots)

        _write_exclusive(output_path, encoded, reauthenticate)
        return EXIT_OK, receipt
    except GateError as exc:
        return _capture_failure(exc)


def compare_release_packages(
    left_package: Path | str,
    right_package: Path | str,
    left_receipt: Path | str,
    right_receipt: Path | str,
    *,
    otool_path: Path | str | None = None,
    readelf_path: Path | str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Compare two release packages and return ``(exit_code, report)``."""

    try:
        left_package_path = Path(left_package)
        right_package_path = Path(right_package)
        left_receipt_path = Path(left_receipt)
        right_receipt_path = Path(right_receipt)
        left_root_token = _direct_path_token(
            left_package_path,
            purpose="left package root",
            directory=True,
            code="UNSAFE_PACKAGE_ROOT",
        )
        right_root_token = _direct_path_token(
            right_package_path,
            purpose="right package root",
            directory=True,
            code="UNSAFE_PACKAGE_ROOT",
        )
        left_receipt_token = _direct_path_token(
            left_receipt_path,
            purpose="left receipt",
            directory=False,
            code="UNSAFE_RECEIPT",
        )
        right_receipt_token = _direct_path_token(
            right_receipt_path,
            purpose="right receipt",
            directory=False,
            code="UNSAFE_RECEIPT",
        )
        _reject_aliased_cohorts(
            (left_root_token,),
            (right_root_token,),
            left_receipt_token,
            right_receipt_token,
        )
        left_receipt_value, left_receipt_bytes = _load_receipt(
            left_receipt_path, "left"
        )
        right_receipt_value, right_receipt_bytes = _load_receipt(
            right_receipt_path, "right"
        )
        differences = _identity_differences(left_receipt_value, right_receipt_value)
        if differences:
            report = _base_report()
            report.update(
                {
                    "classification": "build-identity-mismatch",
                    "differences": differences,
                    "receipts": {
                        "left": {
                            "byteLength": len(left_receipt_bytes),
                            "sha256": _sha256(left_receipt_bytes),
                        },
                        "right": {
                            "byteLength": len(right_receipt_bytes),
                            "sha256": _sha256(right_receipt_bytes),
                        },
                    },
                    "status": "non-comparable",
                }
            )
            return EXIT_NON_COMPARABLE, report

        receipt = left_receipt_value
        expected = receipt["inventory"]
        left_package_tokens = _package_tokens(
            left_package_path, expected, "left", initial_comparison=True
        )
        right_package_tokens = _package_tokens(
            right_package_path, expected, "right", initial_comparison=True
        )
        if (
            left_package_tokens[0] != left_root_token
            or right_package_tokens[0] != right_root_token
        ):
            raise _error(
                "PACKAGE_CHANGED",
                "a package root changed before its inventory could be bound",
            )
        _reject_aliased_cohorts(
            left_package_tokens,
            right_package_tokens,
            left_receipt_token,
            right_receipt_token,
        )

        def reauthenticate_cohorts() -> None:
            if (
                _package_tokens(left_package_path, expected, "left")
                != left_package_tokens
                or _package_tokens(right_package_path, expected, "right")
                != right_package_tokens
            ):
                raise _error(
                    "PACKAGE_CHANGED",
                    "a package cohort changed during comparison",
                )
            if (
                _direct_path_token(
                    left_receipt_path,
                    purpose="left receipt",
                    directory=False,
                    code="UNSAFE_RECEIPT",
                )
                != left_receipt_token
                or _direct_path_token(
                    right_receipt_path,
                    purpose="right receipt",
                    directory=False,
                    code="UNSAFE_RECEIPT",
                )
                != right_receipt_token
            ):
                raise _error(
                    "RECEIPT_CHANGED",
                    "a build receipt changed during comparison",
                )
            _reject_aliased_cohorts(
                left_package_tokens,
                right_package_tokens,
                left_receipt_token,
                right_receipt_token,
            )

        tool_snapshots = _authenticate_tools(receipt)
        left_snapshot = _snapshot_package(left_package_path, expected, "left")
        right_snapshot = _snapshot_package(right_package_path, expected, "right")
        files = _file_rows(expected, left_snapshot, right_snapshot)
        reauthenticate_cohorts()
        _reauthenticate_tools(receipt, tool_snapshots)
        identity = {
            field: receipt[field]
            for field in (
                "channel",
                "image",
                "inventory",
                "platform",
                "runner",
                "schema",
                "sourceRevision",
                "tools",
                "version",
            )
        }
        receipt_evidence = {
            "left": {
                "byteLength": len(left_receipt_bytes),
                "sha256": _sha256(left_receipt_bytes),
            },
            "right": {
                "byteLength": len(right_receipt_bytes),
                "sha256": _sha256(right_receipt_bytes),
            },
        }
        if any(not row["equal"] for row in files):
            reauthenticate_cohorts()
            report = _base_report()
            report.update(
                {
                    "classification": "byte-mismatch",
                    "files": files,
                    "identity": identity,
                    "receipts": receipt_evidence,
                    "status": "fail",
                }
            )
            return EXIT_BYTE_MISMATCH, report

        system = host_platform.system()
        is_darwin = receipt["platform"].startswith("darwin-")
        is_linux = receipt["platform"].startswith("linux-")
        if not is_darwin and otool_path is not None:
            raise _error(
                "OTOOL_FORBIDDEN",
                "--otool is accepted only for Darwin package identities",
            )
        if not is_linux and readelf_path is not None:
            raise _error(
                "READELF_FORBIDDEN",
                "--readelf is accepted only for Linux package identities",
            )
        darwin: list[dict[str, Any]] | None = None
        linux: list[dict[str, Any]] | None = None
        if is_darwin:
            if system != "Darwin":
                raise _error(
                    "DARWIN_HOST_REQUIRED",
                    "Darwin Mach-O admission may run only on a macOS host",
                )
            if otool_path is None:
                raise _error(
                    "OTOOL_REQUIRED",
                    "Darwin package identities require an exact --otool path",
                )
            otool = Path(otool_path)
            expected_otool = _validate_otool(otool, receipt, tool_snapshots)
            darwin = _darwin_evidence(left_snapshot, receipt, otool, expected_otool)
            _reauthenticate_tools(receipt, tool_snapshots)
            if receipt["channel"] == "alpha":
                violations = [
                    {
                        "implementation": row["implementation"],
                        "maximumOs": "13.0",
                        "minimumOs": row["minimumOs"],
                    }
                    for row in darwin
                    if _version_tuple(row["minimumOs"], "minimum OS") > (13, 0, 0)
                ]
                if violations:
                    reauthenticate_cohorts()
                    report = _base_report()
                    report.update(
                        {
                            "classification": "darwin-minimum-os-exceeded",
                            "darwinMachO": darwin,
                            "files": files,
                            "identity": identity,
                            "receipts": receipt_evidence,
                            "status": "fail",
                            "violations": violations,
                        }
                    )
                    return EXIT_DARWIN_POLICY, report
        elif is_linux:
            if readelf_path is None:
                raise _error(
                    "READELF_REQUIRED",
                    "Linux package identities require an exact --readelf path",
                )
            readelf = Path(readelf_path)
            expected_readelf = _validate_readelf(readelf, receipt, tool_snapshots)
            linux = _linux_evidence(left_snapshot, receipt, readelf, expected_readelf)
            _reauthenticate_tools(receipt, tool_snapshots)
        else:
            manifest = _parse_package_json(
                left_snapshot["release-manifest.json"], "release-manifest.json"
            )
            if manifest.get("linuxRuntime") != "not-applicable":
                raise _error(
                    "INVALID_LINUX_RUNTIME",
                    "non-Linux package must declare linuxRuntime as not-applicable",
                )

        reauthenticate_cohorts()
        report = _base_report()
        report.update(
            {
                "classification": "byte-for-byte-reproducible",
                "files": files,
                "identity": identity,
                "receipts": receipt_evidence,
                "status": "pass",
            }
        )
        if darwin is not None:
            report["darwinMachO"] = darwin
        if linux is not None:
            report["linuxElf"] = linux
        return EXIT_OK, report
    except GateError as exc:
        report = _base_report()
        report.update(
            {
                "classification": "input-rejected",
                "errors": [{"code": exc.code, "message": exc.message}],
                "status": "error",
            }
        )
        return EXIT_INPUT_REJECTED, report


def _verify_parser(*, program: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program,
        description=(
            "Compare two receipt-compatible release package directories without "
            "normalizing any artifact bytes. The legacy flag-only invocation remains "
            "supported."
        ),
    )
    parser.add_argument(
        "--left-package",
        type=Path,
        required=True,
        help="first independently built package directory",
    )
    parser.add_argument(
        "--right-package",
        type=Path,
        required=True,
        help="second independently built package directory",
    )
    parser.add_argument(
        "--left-receipt",
        type=Path,
        required=True,
        help="canonical JSON receipt for the first build",
    )
    parser.add_argument(
        "--right-receipt",
        type=Path,
        required=True,
        help="canonical JSON receipt for the second build",
    )
    parser.add_argument(
        "--otool",
        type=Path,
        help="exact otool executable path; required only for Darwin package identities",
    )
    parser.add_argument(
        "--readelf",
        type=Path,
        help="exact readelf executable path; required only for Linux package identities",
    )
    return parser


def _capture_parser(*, program: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program,
        description=(
            "Capture one canonical release-build receipt from an exact nine-file "
            "package directory. Every runner value, tool path, and tool version is "
            "explicit; ambient tool discovery is never used. Output creation is "
            "atomic and refuses replacement."
        ),
    )
    parser.add_argument(
        "--package", type=Path, required=True, help="closed nine-file package directory"
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        required=True,
        help="new canonical receipt path; output must be absent and parent present",
    )
    parser.add_argument(
        "--source-revision",
        required=True,
        help="exact lowercase 40-hex source commit, cross-checked with the manifest",
    )
    parser.add_argument(
        "--version", required=True, help="exact package SemVer, without a leading v"
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=sorted(PLATFORMS),
        help="exact package platform identity",
    )
    parser.add_argument("--runner-os", required=True, help="exact runner OS identity")
    parser.add_argument(
        "--runner-architecture",
        required=True,
        help="exact runner architecture identity",
    )
    parser.add_argument(
        "--runner-image", required=True, help="exact runner image identity"
    )
    parser.add_argument(
        "--runner-image-version",
        required=True,
        help="exact runner image version",
    )
    parser.add_argument(
        "--runner-kernel", required=True, help="exact runner kernel identity"
    )
    parser.add_argument(
        "--tool",
        action="append",
        required=True,
        metavar="NAME=/ABSOLUTE/PATH",
        help=("explicit direct executable tool path; repeat once per closed-set tool"),
    )
    parser.add_argument(
        "--tool-version",
        action="append",
        required=True,
        metavar="NAME=VERSION",
        help="explicit version for each --tool name; repeat with no extra names",
    )
    return parser


def _top_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture or verify byte-for-byte reproducible OpenProse release packages."
        ),
        epilog=(
            "Use 'capture --help' or 'verify --help'. For backward compatibility, "
            "legacy verify flags may be supplied without the 'verify' command."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("capture", "verify"),
        help="capture a canonical receipt or verify two independent builds",
    )
    return parser


def _assignments(
    parser: argparse.ArgumentParser, values: list[str], option: str, *, paths: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        name, separator, assigned = value.partition("=")
        if (
            separator != "="
            or not TOOL_NAME.fullmatch(name)
            or not assigned
            or name in result
        ):
            assignment = "/ABSOLUTE/PATH" if paths else "VERSION"
            parser.error(
                f"{option} values must be unique NAME={assignment} assignments"
            )
        result[name] = Path(assigned) if paths else assigned
    return result


def main(argv: list[str] | None = None) -> int:
    incoming = list(sys.argv[1:] if argv is None else argv)
    program = Path(sys.argv[0]).name
    if not incoming or incoming == ["--help"] or incoming == ["-h"]:
        parser = _top_parser()
        if not incoming:
            parser.print_help(sys.stderr)
            return EXIT_INPUT_REJECTED
        parser.print_help(sys.stdout)
        return EXIT_OK
    if incoming[0] == "capture":
        parser = _capture_parser(program=f"{program} capture")
        arguments = parser.parse_args(incoming[1:])
        tool_paths = _assignments(parser, arguments.tool, "--tool", paths=True)
        tool_versions = _assignments(
            parser, arguments.tool_version, "--tool-version", paths=False
        )
        code, payload = capture_release_receipt(
            arguments.package,
            arguments.receipt,
            source_revision=arguments.source_revision,
            version=arguments.version,
            platform_id=arguments.platform,
            runner={
                "architecture": arguments.runner_architecture,
                "image": arguments.runner_image,
                "imageVersion": arguments.runner_image_version,
                "kernel": arguments.runner_kernel,
                "os": arguments.runner_os,
            },
            tool_paths=tool_paths,
            tool_versions=tool_versions,
        )
        sys.stdout.buffer.write(_canonical_json(payload))
        return code
    if incoming[0] == "verify":
        parser = _verify_parser(program=f"{program} verify")
        incoming = incoming[1:]
    else:
        parser = _verify_parser(program=program)
    arguments = parser.parse_args(incoming)
    code, report = compare_release_packages(
        arguments.left_package,
        arguments.right_package,
        arguments.left_receipt,
        arguments.right_receipt,
        otool_path=arguments.otool,
        readelf_path=arguments.readelf,
    )
    sys.stdout.buffer.write(render_report(report))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
