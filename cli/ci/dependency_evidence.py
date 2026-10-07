#!/usr/bin/env python3
"""Produce deterministic, provider-free dependency inventory evidence.

This tool deliberately does not claim license, vulnerability, or signing
authority.  Lockfiles describe resolution and declared integrity; they are not
an authority for those three release decisions.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import deque
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Iterable


SCHEMA = "openprose.dependency-evidence/1"
ERROR_SCHEMA = "openprose.dependency-evidence-error/1"
POLICY_SCHEMA = "openprose.dependency-release-policy/1"
MAX_LOCK_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PACKAGES = 16_384
NAME_RE = re.compile(r"^[A-Za-z0-9_@./-]+$")
CRATE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
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


class EvidenceError(Exception):
    """A closed-boundary validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code: str, message: str) -> None:
    raise EvidenceError(code, message)


def safe_read(path: Path, maximum: int) -> bytes:
    """Read one bounded, regular, unchanged file without following symlinks."""
    try:
        before_path = path.lstat()
    except OSError as error:
        fail("SOURCE_UNAVAILABLE", f"cannot inspect {path}: {error}")
    if not stat.S_ISREG(before_path.st_mode):
        fail("SOURCE_UNSAFE", f"source is not a regular file: {path}")
    if before_path.st_size > maximum:
        fail("SOURCE_OVERSIZE", f"source exceeds {maximum} bytes: {path}")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        fail("SOURCE_UNAVAILABLE", f"cannot open {path}: {error}")
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            fail("SOURCE_UNSAFE", f"opened source is not regular: {path}")
        if (first.st_dev, first.st_ino) != (before_path.st_dev, before_path.st_ino):
            fail("SOURCE_MUTATED", f"source identity changed before read: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                fail("SOURCE_OVERSIZE", f"source exceeds {maximum} bytes: {path}")
        after = os.fstat(descriptor)
        identity = (first.st_dev, first.st_ino, first.st_size)
        after_identity = (after.st_dev, after.st_ino, after.st_size)
        timestamps = (first.st_mtime, first.st_mtime_ns)
        after_timestamps = (after.st_mtime, after.st_mtime_ns)
        if identity != after_identity or timestamps != after_timestamps:
            fail("SOURCE_MUTATED", f"source changed while being read: {path}")
        data = b"".join(chunks)
        if len(data) != first.st_size:
            fail("SOURCE_MUTATED", f"source length changed while being read: {path}")
        return data
    finally:
        os.close(descriptor)


def safe_read_under(root: Path, path: Path, maximum: int) -> bytes:
    """Read a file while rejecting symlinks in its root-relative ancestry."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        fail("SOURCE_UNSAFE", f"source escapes evidence root: {path}")
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        try:
            observed = current.lstat()
        except OSError as error:
            fail("SOURCE_UNAVAILABLE", f"cannot inspect source directory {current}: {error}")
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            fail("SOURCE_UNSAFE", f"source directory is not a real directory: {current}")
    return safe_read(path, maximum)


def decode_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        fail("SOURCE_MALFORMED", f"{label} is not UTF-8")


def json_no_duplicates(text: str, label: str) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                fail("SOURCE_DUPLICATE", f"duplicate JSON key {key!r} in {label}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except EvidenceError:
        raise
    except (json.JSONDecodeError, TypeError) as error:
        fail("SOURCE_MALFORMED", f"invalid JSON in {label}: {error}")


def strip_json_trailing_commas(text: str) -> str:
    """Remove Bun lockfile trailing commas, never touching quoted strings."""
    result: list[str] = []
    quoted = False
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quoted:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            index += 1
            continue
        if character == '"':
            quoted = True
            result.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        result.append(character)
        index += 1
    if quoted:
        fail("SOURCE_MALFORMED", "unterminated string in Bun lockfile")
    return "".join(result)


def source_record(root: Path, path: Path, data: bytes) -> dict[str, Any]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        fail("SOURCE_UNSAFE", f"source escapes evidence root: {path}")
    return {
        "path": relative,
        "byteLength": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("SOURCE_MALFORMED", f"{label} must be an object")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail("SOURCE_MALFORMED", f"{label} must be a nonempty string")
    return value


def validate_name(name: str, label: str, crate: bool = False) -> None:
    pattern = CRATE_NAME_RE if crate else NAME_RE
    if len(name) > 256 or not pattern.fullmatch(name) or ".." in name:
        fail("PACKAGE_UNSAFE", f"unsafe package name in {label}: {name!r}")


def validate_version(version: str, label: str) -> None:
    if len(version) > 128 or not VERSION_RE.fullmatch(version):
        fail("PACKAGE_MALFORMED", f"invalid package version in {label}: {version!r}")


def parse_string_literal(value: str, label: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        fail("SOURCE_MALFORMED", f"invalid string in {label}")
    if not isinstance(parsed, str):
        fail("SOURCE_MALFORMED", f"expected string in {label}")
    return parsed


def parse_toml_string_array(lines: list[str], start: int, value: str, label: str) -> tuple[list[str], int]:
    assembled = value
    index = start
    while "]" not in assembled:
        index += 1
        if index >= len(lines):
            fail("SOURCE_MALFORMED", f"unterminated array in {label}")
        assembled += "\n" + lines[index].strip()
    if assembled[assembled.index("]") + 1 :].strip():
        fail("SOURCE_MALFORMED", f"unexpected text after array in {label}")
    inner = assembled[assembled.index("[") + 1 : assembled.index("]")]
    values: list[str] = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"', inner):
        values.append(parse_string_literal(match.group(0), label))
    residue = re.sub(r'"(?:\\.|[^"\\])*"', "", inner).replace(",", "").strip()
    if residue:
        fail("SOURCE_MALFORMED", f"unsupported array syntax in {label}")
    return values, index


def parse_root_manifest(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    section = ""
    members: list[str] | None = None
    version: str | None = None
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            index += 1
            continue
        if "=" not in stripped:
            fail("SOURCE_MALFORMED", "unsupported Cargo workspace manifest syntax")
        key, value = (part.strip() for part in stripped.split("=", 1))
        if section == "workspace" and key == "members":
            members, index = parse_toml_string_array(lines, index, value, "workspace.members")
        elif section == "workspace.package" and key == "version":
            version = parse_string_literal(value, "workspace.package.version")
        index += 1
    if not members or version is None:
        fail("SOURCE_MALFORMED", "Cargo workspace members/version are required")
    validate_version(version, "Cargo workspace")
    if len(members) != len(set(members)):
        fail("SOURCE_DUPLICATE", "Cargo workspace members contain duplicates")
    for member in members:
        pure = PurePosixPath(member)
        if (
            not member
            or pure.is_absolute()
            or ".." in pure.parts
            or "." in pure.parts
            or "\\" in member
            or "\x00" in member
            or any(character in member for character in "*?[]")
        ):
            fail("SOURCE_UNSAFE", f"unsafe Cargo workspace member: {member!r}")
    return members, version


def dependency_section(section: str) -> str | None:
    if section == "dependencies" or section.endswith(".dependencies"):
        return "runtime"
    if section == "dev-dependencies" or section.endswith(".dev-dependencies"):
        return "development"
    if section == "build-dependencies" or section.endswith(".build-dependencies"):
        return "build"
    return None


def toml_assignments(text: str, label: str) -> Iterable[tuple[str, str, str]]:
    """Yield the small closed assignment subset used by Cargo manifests."""
    section = ""
    lines = text.splitlines()
    index = 0
    seen: set[tuple[str, str]] = set()
    array_sections: dict[str, int] = {}
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped.startswith("[[") != stripped.endswith("]]"):
                fail("SOURCE_MALFORMED", f"malformed array table in {label}")
            if stripped.startswith("[[") and stripped.endswith("]]"):
                array_name = stripped[2:-2]
                array_sections[array_name] = array_sections.get(array_name, 0) + 1
                section = f"[[{array_name}]]#{array_sections[array_name]}"
            else:
                section = stripped[1:-1]
            index += 1
            continue
        if "=" not in stripped:
            fail("SOURCE_MALFORMED", f"unsupported assignment in {label}")
        key, value = (part.strip() for part in stripped.split("=", 1))
        if not key:
            fail("SOURCE_MALFORMED", f"empty assignment key in {label}")
        quoted = False
        escaped = False
        square = 0
        curly = 0

        def consume(characters: str) -> None:
            nonlocal quoted, escaped, square, curly
            for character in characters:
                if quoted:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == '"':
                        quoted = False
                    continue
                if character == '"':
                    quoted = True
                elif character == "[":
                    square += 1
                elif character == "]":
                    square -= 1
                elif character == "{":
                    curly += 1
                elif character == "}":
                    curly -= 1
                if square < 0 or curly < 0:
                    fail("SOURCE_MALFORMED", f"unbalanced assignment in {label}")

        consume(value)
        while quoted or square or curly:
            index += 1
            if index >= len(lines):
                fail("SOURCE_MALFORMED", f"unterminated assignment in {label}")
            continuation = lines[index].strip()
            value += "\n" + continuation
            consume(continuation)
        identity = (section, key)
        if identity in seen:
            fail("SOURCE_DUPLICATE", f"duplicate manifest assignment {section}.{key}")
        seen.add(identity)
        yield section, key, value
        index += 1


def parse_member_manifest(
    text: str, workspace_version: str | None
) -> tuple[str, str, dict[str, set[str]]]:
    section = ""
    name: str | None = None
    version: str | None = None
    inherited_version = False
    dependencies: dict[str, set[str]] = {}
    for section, key, value in toml_assignments(text, "Cargo package manifest"):
        if section == "package" and key == "name":
            name = parse_string_literal(value, "package.name")
        elif section == "package" and key == "version":
            version = parse_string_literal(value, "package.version")
        elif section == "package" and key == "version.workspace":
            if value != "true":
                fail("SOURCE_MALFORMED", "package version.workspace must be true")
            inherited_version = True
        scope = dependency_section(section)
        if scope is not None:
            dependency = key.split(".", 1)[0]
            validate_name(dependency, "Cargo manifest dependency", crate=True)
            actual = dependency
            package_match = re.search(r'\bpackage\s*=\s*("(?:\\.|[^"\\])*")', value)
            if package_match:
                actual = parse_string_literal(package_match.group(1), "dependency package alias")
                validate_name(actual, "Cargo dependency package", crate=True)
            dependencies.setdefault(actual, set()).add(scope)
    if name is None or (version is None and not inherited_version):
        fail("SOURCE_MALFORMED", "Cargo member package name/version are required")
    if inherited_version and workspace_version is None:
        fail("SOURCE_MALFORMED", "standalone Cargo package cannot inherit workspace version")
    validate_name(name, "Cargo member", crate=True)
    resolved_version = version if version is not None else workspace_version
    if resolved_version is None:
        fail("SOURCE_MALFORMED", "Cargo package version could not be resolved")
    validate_version(resolved_version, f"Cargo member {name}")
    return name, resolved_version, dependencies


def parse_cargo_lock(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    lock_version: int | None = None
    packages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if stripped == "[[package]]":
            if current is not None:
                if len(packages) >= MAX_PACKAGES:
                    fail("SOURCE_OVERSIZE", "Cargo.lock contains too many packages")
                packages.append(current)
            current = {}
            index += 1
            continue
        if current is None:
            match = re.fullmatch(r"version\s*=\s*([0-9]+)", stripped)
            if match is None or lock_version is not None:
                fail("SOURCE_MALFORMED", "unexpected Cargo.lock top-level content")
            lock_version = int(match.group(1))
            index += 1
            continue
        if "=" not in stripped:
            fail("SOURCE_MALFORMED", "invalid Cargo.lock package field")
        key, value = (part.strip() for part in stripped.split("=", 1))
        if key not in {"name", "version", "source", "checksum", "dependencies"}:
            fail("SOURCE_MALFORMED", f"unsupported Cargo.lock package field: {key}")
        if key in current:
            fail("SOURCE_DUPLICATE", f"duplicate Cargo.lock field: {key}")
        if key == "dependencies":
            dependencies, index = parse_toml_string_array(lines, index, value, "Cargo.lock dependencies")
            current[key] = dependencies
        else:
            current[key] = parse_string_literal(value, f"Cargo.lock {key}")
        index += 1
    if current is not None:
        if len(packages) >= MAX_PACKAGES:
            fail("SOURCE_OVERSIZE", "Cargo.lock contains too many packages")
        packages.append(current)
    if lock_version != 4:
        fail("LOCK_VERSION_UNSUPPORTED", f"Cargo.lock version must be 4, got {lock_version!r}")
    if not packages:
        fail("SOURCE_MALFORMED", "Cargo.lock contains no packages")
    return packages


def resolve_cargo_dependency(
    reference: str,
    by_name: dict[str, list[tuple[str, str | None]]],
    keys: set[tuple[str, str, str | None]],
) -> tuple[str, str, str | None]:
    match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\s+([^\s]+))?(?:\s+\([^)]*\))?", reference)
    if match is None:
        fail("PACKAGE_MALFORMED", f"invalid Cargo dependency reference: {reference!r}")
    name, version = match.group(1), match.group(2)
    candidates = by_name.get(name, [])
    if version is not None:
        candidates = [candidate for candidate in candidates if candidate[0] == version]
    if len(candidates) != 1:
        fail("PACKAGE_UNRESOLVED", f"Cargo dependency does not resolve uniquely: {reference!r}")
    key = (name, candidates[0][0], candidates[0][1])
    if key not in keys:
        fail("PACKAGE_UNRESOLVED", f"Cargo dependency is not in inventory: {reference!r}")
    return key


def resolved_cargo_component(
    raw_packages: list[dict[str, Any]],
    local_versions: dict[str, str],
    direct: list[tuple[str, str]],
    component: str,
    target: str,
    local_scope: str,
) -> dict[str, Any]:
    if len(raw_packages) > MAX_PACKAGES:
        fail("SOURCE_OVERSIZE", f"{component} Cargo inventory contains too many packages")
    packages: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    by_name: dict[str, list[tuple[str, str | None]]] = {}
    for raw in raw_packages:
        name = require_string(raw.get("name"), "Cargo package name")
        version = require_string(raw.get("version"), "Cargo package version")
        validate_name(name, "Cargo.lock", crate=True)
        validate_version(version, f"Cargo package {name}")
        source = raw.get("source")
        checksum = raw.get("checksum")
        if source is None:
            if local_versions.get(name) != version or checksum is not None:
                fail("PACKAGE_INTEGRITY_MISSING", f"unverifiable local Cargo package: {name}")
            key = (name, version, None)
            integrity = {
                "status": "not-applicable",
                "reason": "local-source-package",
            }
            normalized_source = "workspace" if local_scope == "workspace" else "component-local"
        else:
            if (
                not isinstance(source, str)
                or not source.startswith("registry+https://")
                or len(source) > 2048
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in source)
            ):
                fail("PACKAGE_SOURCE_UNSUPPORTED", f"unsupported Cargo source for {name}")
            if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
                fail("PACKAGE_INTEGRITY_MISSING", f"Cargo checksum missing or invalid for {name}")
            key = (name, version, source)
            integrity = {
                "status": "declared",
                "algorithm": "sha256",
                "digest": checksum,
            }
            normalized_source = source
        if key in packages:
            fail(
                "PACKAGE_DUPLICATE",
                f"duplicate Cargo package identity in {component}: {name} {version}",
            )
        package = {
            "name": name,
            "version": version,
            "source": normalized_source,
            "integrity": integrity,
            "dependencies": list(raw.get("dependencies", [])),
            "scopes": set(),
        }
        packages[key] = package
        by_name.setdefault(name, []).append((version, key[2]))

    keys = set(packages)
    for name, version in local_versions.items():
        candidates = by_name.get(name, [])
        if candidates != [(version, None)]:
            fail(
                "MANIFEST_LOCK_MISMATCH",
                f"local package absent or ambiguous in {component} Cargo.lock: {name}",
            )
        packages[(name, version, None)]["scopes"].add(local_scope)

    queue: deque[tuple[tuple[str, str, str | None], str]] = deque()
    scheduled: set[tuple[tuple[str, str, str | None], str]] = set()
    for dependency, scope in direct:
        state = (resolve_cargo_dependency(dependency, by_name, keys), scope)
        if state not in scheduled:
            scheduled.add(state)
            queue.append(state)
    seen: set[tuple[tuple[str, str, str | None], str]] = set()
    while queue:
        key, scope = queue.popleft()
        if (key, scope) in seen:
            continue
        seen.add((key, scope))
        package = packages[key]
        package["scopes"].add(scope)
        for reference in package["dependencies"]:
            state = (resolve_cargo_dependency(reference, by_name, keys), scope)
            if state not in scheduled:
                scheduled.add(state)
                queue.append(state)

    output: list[dict[str, Any]] = []
    for package in packages.values():
        if not package["scopes"]:
            fail(
                "PACKAGE_UNREACHABLE",
                f"Cargo package is not reachable in {component}: {package['name']}",
            )
        output.append(
            {
                "name": package["name"],
                "version": package["version"],
                "source": package["source"],
                "integrity": package["integrity"],
                "scopes": sorted(package["scopes"]),
            }
        )
    output.sort(key=lambda package: (package["name"], package["version"], package["source"]))
    return {
        "component": component,
        "target": target,
        "lockfileVersion": 4,
        "scopeBasis": "package-manifest-direct-kind-plus-lockfile-reachability",
        "packages": output,
    }


def cargo_inventory(
    root: Path,
) -> tuple[dict[str, Any], list[tuple[Path, bytes]]]:
    rust = root / "cli" / "rust"
    root_manifest_path = rust / "Cargo.toml"
    root_bytes = safe_read_under(root, root_manifest_path, MAX_MANIFEST_BYTES)
    members, workspace_version = parse_root_manifest(decode_utf8(root_bytes, "Cargo.toml"))
    sources: list[tuple[Path, bytes]] = [(root_manifest_path, root_bytes)]
    direct: list[tuple[str, str]] = []
    member_versions: dict[str, str] = {}
    for member in members:
        manifest_path = rust.joinpath(*PurePosixPath(member).parts) / "Cargo.toml"
        manifest_bytes = safe_read_under(root, manifest_path, MAX_MANIFEST_BYTES)
        name, version, dependencies = parse_member_manifest(
            decode_utf8(manifest_bytes, manifest_path.as_posix()), workspace_version
        )
        if name in member_versions:
            fail("SOURCE_DUPLICATE", f"duplicate Cargo workspace package: {name}")
        member_versions[name] = version
        for dependency, scopes in dependencies.items():
            for scope in scopes:
                direct.append((dependency, scope))
        sources.append((manifest_path, manifest_bytes))

    lock_path = rust / "Cargo.lock"
    lock_bytes = safe_read_under(root, lock_path, MAX_LOCK_BYTES)
    raw_packages = parse_cargo_lock(decode_utf8(lock_bytes, "Cargo.lock"))
    sources.append((lock_path, lock_bytes))

    inventory = resolved_cargo_component(
        raw_packages,
        member_versions,
        direct,
        component="rust-cli",
        target="multi-platform",
        local_scope="workspace",
    )
    inventory["scopeBasis"] = "workspace-manifest-direct-kind-plus-lockfile-reachability"
    return inventory, sources


def windows_host_cargo_inventory(
    root: Path,
) -> tuple[dict[str, Any], list[tuple[Path, bytes]]]:
    component_root = root / "cli" / "platform" / "windows-process-host"
    manifest_path = component_root / "Cargo.toml"
    manifest_bytes = safe_read_under(root, manifest_path, MAX_MANIFEST_BYTES)
    name, version, dependencies = parse_member_manifest(
        decode_utf8(manifest_bytes, "windows process host Cargo.toml"), None
    )
    direct = [
        (dependency, scope)
        for dependency, scopes in dependencies.items()
        for scope in scopes
    ]
    lock_path = component_root / "Cargo.lock"
    lock_bytes = safe_read_under(root, lock_path, MAX_LOCK_BYTES)
    raw_packages = parse_cargo_lock(
        decode_utf8(lock_bytes, "windows process host Cargo.lock")
    )
    inventory = resolved_cargo_component(
        raw_packages,
        {name: version},
        direct,
        component="windows-process-host",
        target="windows",
        local_scope="component",
    )
    return inventory, [
        (manifest_path, manifest_bytes),
        (lock_path, lock_bytes),
    ]


def validate_dependency_map(value: Any, label: str) -> dict[str, str]:
    mapping = require_dict(value, label)
    result: dict[str, str] = {}
    for name, constraint in mapping.items():
        validate_name(name, label)
        result[name] = require_string(constraint, f"{label}.{name}")
    return result


def parse_sri(value: Any, label: str) -> str:
    integrity = require_string(value, label)
    if not integrity.startswith("sha512-") or len(integrity) > 256:
        fail("PACKAGE_INTEGRITY_MISSING", f"{label} must be sha512 SRI")
    encoded = integrity[len("sha512-") :]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        fail("PACKAGE_INTEGRITY_MISSING", f"{label} is invalid base64")
    if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != encoded:
        fail("PACKAGE_INTEGRITY_MISSING", f"{label} is not canonical SHA-512 SRI")
    return decoded.hex()


def locator_name_version(locator: str) -> tuple[str, str]:
    separator = locator.rfind("@")
    if separator <= 0 or separator == len(locator) - 1:
        fail("PACKAGE_MALFORMED", f"invalid Bun package locator: {locator!r}")
    name, version = locator[:separator], locator[separator + 1 :]
    validate_name(name, "Bun package locator")
    validate_version(version, f"Bun package {name}")
    return name, version


def bun_inventory(root: Path) -> tuple[dict[str, Any], list[tuple[Path, bytes]]]:
    bun = root / "cli" / "bun"
    manifest_path = bun / "package.json"
    manifest_bytes = safe_read_under(root, manifest_path, MAX_MANIFEST_BYTES)
    manifest = require_dict(
        json_no_duplicates(decode_utf8(manifest_bytes, "package.json"), "package.json"),
        "package.json",
    )
    manifest_name = require_string(manifest.get("name"), "package.json.name")
    manifest_version = require_string(manifest.get("version"), "package.json.version")
    validate_name(manifest_name, "package.json")
    validate_version(manifest_version, "package.json")
    category_scopes = {
        "dependencies": "runtime",
        "optionalDependencies": "runtime-optional",
        "devDependencies": "development",
        "peerDependencies": "peer",
    }
    manifest_dependencies: dict[str, dict[str, str]] = {}
    for category in category_scopes:
        manifest_dependencies[category] = validate_dependency_map(
            manifest.get(category, {}), f"package.json.{category}"
        )

    lock_path = bun / "bun.lock"
    lock_bytes = safe_read_under(root, lock_path, MAX_LOCK_BYTES)
    lock = require_dict(
        json_no_duplicates(
            strip_json_trailing_commas(decode_utf8(lock_bytes, "bun.lock")), "bun.lock"
        ),
        "bun.lock",
    )
    if set(lock) != {"lockfileVersion", "configVersion", "workspaces", "packages"}:
        fail("SOURCE_MALFORMED", "Bun lockfile top-level keys are not the supported closed set")
    if lock.get("lockfileVersion") != 1 or lock.get("configVersion") != 1:
        fail("LOCK_VERSION_UNSUPPORTED", "Bun lock/config version must both be 1")
    workspaces = require_dict(lock["workspaces"], "bun.lock.workspaces")
    if set(workspaces) != {""}:
        fail("SOURCE_MALFORMED", "Bun lockfile must contain only its root workspace")
    workspace = require_dict(workspaces[""], "bun.lock.workspaces root")
    if workspace.get("name") != manifest_name:
        fail("MANIFEST_LOCK_MISMATCH", "Bun workspace name differs from package.json")
    allowed_workspace = {"name", *category_scopes}
    if not set(workspace).issubset(allowed_workspace):
        fail("SOURCE_MALFORMED", "unsupported Bun workspace fields")
    for category in category_scopes:
        locked = validate_dependency_map(workspace.get(category, {}), f"bun workspace {category}")
        if locked != manifest_dependencies[category]:
            fail("MANIFEST_LOCK_MISMATCH", f"Bun {category} differs from package.json")

    raw_packages = require_dict(lock["packages"], "bun.lock.packages")
    if len(raw_packages) > MAX_PACKAGES:
        fail("SOURCE_OVERSIZE", "Bun lockfile contains too many packages")
    packages: dict[str, dict[str, Any]] = {}
    for key, raw in raw_packages.items():
        validate_name(key, "Bun package key")
        if not isinstance(raw, list) or len(raw) != 4:
            fail("PACKAGE_MALFORMED", f"Bun package entry must have four fields: {key}")
        locator, resolved, metadata_value, integrity_value = raw
        locator = require_string(locator, f"Bun package locator {key}")
        name, version = locator_name_version(locator)
        if name != key:
            fail("PACKAGE_MALFORMED", f"Bun package key/locator mismatch: {key}")
        if not isinstance(resolved, str):
            fail("PACKAGE_MALFORMED", f"Bun resolved field must be a string: {key}")
        if resolved:
            fail("PACKAGE_SOURCE_UNSUPPORTED", f"non-registry Bun package source is unsupported: {key}")
        metadata = require_dict(metadata_value, f"Bun metadata {key}")
        allowed_metadata = {
            "dependencies",
            "optionalDependencies",
            "peerDependencies",
            "bin",
            "os",
            "cpu",
            "engines",
            "optional",
        }
        if not set(metadata).issubset(allowed_metadata):
            fail("SOURCE_MALFORMED", f"unsupported Bun metadata field for {key}")
        dependencies: set[str] = set()
        for category in ("dependencies", "optionalDependencies", "peerDependencies"):
            dependencies.update(validate_dependency_map(metadata.get(category, {}), f"Bun {key}.{category}"))
        digest = parse_sri(integrity_value, f"Bun integrity {key}")
        if key in packages:
            fail("PACKAGE_DUPLICATE", f"duplicate Bun package: {key}")
        packages[key] = {
            "name": name,
            "version": version,
            "source": "npm-registry",
            "integrity": {
                "status": "declared",
                "algorithm": "sha512",
                "digest": digest,
            },
            "dependencies": sorted(dependencies),
            "scopes": set(),
        }

    queue: deque[tuple[str, str]] = deque()
    scheduled: set[tuple[str, str]] = set()
    for category, scope in category_scopes.items():
        for dependency in manifest_dependencies[category]:
            state = (dependency, scope)
            if state not in scheduled:
                scheduled.add(state)
                queue.append(state)
    seen: set[tuple[str, str]] = set()
    while queue:
        name, scope = queue.popleft()
        if (name, scope) in seen:
            continue
        seen.add((name, scope))
        package = packages.get(name)
        if package is None:
            fail("PACKAGE_UNRESOLVED", f"Bun dependency absent from package inventory: {name}")
        package["scopes"].add(scope)
        for dependency in package["dependencies"]:
            state = (dependency, scope)
            if state not in scheduled:
                scheduled.add(state)
                queue.append(state)
    output = [
        {
            "name": manifest_name,
            "version": manifest_version,
            "source": "workspace",
            "integrity": {"status": "not-applicable", "reason": "workspace-package"},
            "scopes": ["workspace"],
        }
    ]
    for package in packages.values():
        if not package["scopes"]:
            fail("PACKAGE_UNREACHABLE", f"Bun package is not reachable from manifest: {package['name']}")
        output.append(
            {
                "name": package["name"],
                "version": package["version"],
                "source": package["source"],
                "integrity": package["integrity"],
                "scopes": sorted(package["scopes"]),
            }
        )
    output.sort(key=lambda package: (package["name"], package["version"], package["source"]))
    return {
        "lockfileVersion": 1,
        "scopeBasis": "package-manifest-direct-kind-plus-lockfile-reachability",
        "packages": output,
    }, [(manifest_path, manifest_bytes), (lock_path, lock_bytes)]


def build_report(root: Path) -> dict[str, Any]:
    root = root.resolve()
    cargo, cargo_sources = cargo_inventory(root)
    windows_host, windows_host_sources = windows_host_cargo_inventory(root)
    bun, bun_sources = bun_inventory(root)
    python_lock = root / "harnesses/agents-sdk/requirements-build.txt"
    python_sources = []
    python_inventory = None
    if python_lock.exists():
        lock_bytes = safe_read_under(root, python_lock, MAX_LOCK_BYTES)
        text = decode_utf8(lock_bytes, "SDK Python lock").replace("\\\n", "")
        packages = []
        names = set()
        for row in text.splitlines():
            row = row.strip()
            if not row or row.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)", row)
            if match is None:
                fail("SOURCE_MALFORMED", "SDK Python requirements must be exact and hash locked")
            name, version, hashes = match.groups()
            if len(version) > 128 or PYTHON_VERSION_RE.fullmatch(version) is None:
                fail("SOURCE_MALFORMED", "SDK Python requirement version must be an exact pinned Python release")
            name = name.lower().replace("_", "-")
            if name in names:
                fail("SOURCE_DUPLICATE", "duplicate SDK Python requirement")
            names.add(name)
            for digest in sorted(set(re.findall(r"sha256:([0-9a-f]{64})", hashes))):
                packages.append({"name": name, "version": version,
                                 "source": "pypi:wheel-sha256:" + digest,
                                 "scopes": ["frozen-sdk-build"],
                                 "integrity": {"status": "declared", "algorithm": "sha256", "digest": digest}})
        if not packages:
            fail("SOURCE_MALFORMED", "SDK Python requirements are empty")
        packages.sort(key=lambda package: (package["name"], package["version"], package["source"]))
        python_inventory = {"scopeBasis": "hash-locked-four-platform-wheel-candidates", "packages": packages}
        python_sources = [(python_lock, lock_bytes)]
    source_values = [
        source_record(root, path, data)
        for path, data in cargo_sources + windows_host_sources + bun_sources + python_sources
    ]
    source_values.sort(key=lambda source: source["path"])
    blockers = [
        "license authority is not derivable from lockfiles/manifests alone",
        "vulnerability analysis was not performed",
        "package signing authority was not evaluated",
        "declared lockfile integrity was inventoried but package bytes were not fetched or verified",
    ]
    return {
        "schema": SCHEMA,
        "generator": {
            "name": "openprose-dependency-evidence",
            "version": 1,
            "providerFree": True,
            "networkUsed": False,
        },
        "sources": source_values,
        "inventories": {
            "cargo": cargo,
            "bun": bun,
            "windowsProcessHostCargo": windows_host,
            **({"agentsSdkPython": python_inventory} if python_inventory is not None else {}),
        },
        "authority": {
            "licenses": {
                "status": "unknown",
                "reason": "not-derivable-from-lockfiles",
            },
            "vulnerabilities": {
                "status": "not-performed",
                "reason": "requires-an-external-authoritative-dataset",
            },
            "signing": {
                "status": "not-performed",
                "reason": "lockfile-integrity-is-not-package-signing-authority",
            },
        },
        "releasePolicy": {
            "schema": POLICY_SCHEMA,
            "boundary": "inventory-only",
            "passed": False,
            "blockers": blockers,
        },
    }


def release_policy_passes(report: Any) -> bool:
    """Inventory-only evidence can never promote a release candidate."""
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        return False
    policy = report.get("releasePolicy")
    if not isinstance(policy, dict):
        return False
    return False


def render_report(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def render_error(error: EvidenceError) -> bytes:
    return render_report(
        {
            "schema": ERROR_SCHEMA,
            "code": error.code,
            "message": error.message,
        }
    )


class ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        fail("ARGUMENT_INVALID", message)


def parse_arguments(arguments: Iterable[str]) -> argparse.Namespace:
    parser = ClosedArgumentParser(add_help=True)
    parser.add_argument("command", choices=("report", "check"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    return parser.parse_args(list(arguments))


def main(arguments: Iterable[str] | None = None) -> int:
    try:
        options = parse_arguments(sys.argv[1:] if arguments is None else arguments)
        report = build_report(options.root)
        sys.stdout.buffer.write(render_report(report))
        if options.command == "check" and not release_policy_passes(report):
            return 3
        return 0
    except EvidenceError as error:
        sys.stdout.buffer.write(render_error(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
