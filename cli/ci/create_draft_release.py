#!/usr/bin/env python3
"""Create one GitHub draft release from a closed, hash-verified assembly."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import zlib
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
ALPHA_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)" r"-alpha\.(0|[1-9][0-9]*)$"
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
AUTHORITY_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
ALPHA_DRAFT_AUTHORITY_SCHEMA = "openprose.alpha-draft-authority/1"
ALPHA_DRAFT_WORKFLOW = ".github/workflows/openprose-cli-alpha-release.yml"
AUTHORITY_OUTPUT_ERROR = (
    "functional-alpha authority output must be an absolute canonical unused path"
)
TARGETS = ("linux-x64", "linux-arm64", "darwin-arm", "darwin-x64", "win-x64")
TARGET_PLATFORMS = {
    "linux-x64": "linux-x64-gnu",
    "linux-arm64": "linux-arm64-gnu",
    "darwin-arm": "darwin-arm64",
    "darwin-x64": "darwin-x64",
    "win-x64": "win32-x64",
}
ALPHA_TARGET_PLATFORMS = {
    target: TARGET_PLATFORMS[target]
    for target in ("linux-x64", "linux-arm64", "darwin-arm", "darwin-x64")
}
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
WINDOWS_HOST_NAME = "openprose-windows-process-host.exe"
LINUX_MINIMUM_GLIBC = "2.34"
LINUX_EXECUTION_EVIDENCE = "ubuntu-22.04-only"
MAX_ASSET_BYTES = 1024 * 1024 * 1024
MAX_ALPHA_ASSET_BYTES = 512 * 1024 * 1024
MAX_ASSEMBLY_BYTES = 4 * 1024 * 1024 * 1024
MAX_ASSEMBLY_ENTRIES = 2_048
MAX_ALPHA_CHECKSUM_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RELEASE_NOTES_BYTES = 1024 * 1024
MAX_TAG_PEEL_DEPTH = 8
MAX_RELEASE_DISCOVERY_PAGES = 10
MAX_RELEASES_PER_PAGE = 100
MAX_RELEASE_ASSETS_PER_PAGE = 100
API_VERSION = "2022-11-28"
DEPENDENCY_SOURCE_PATHS = {
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
NOT_APPLICABLE_INTEGRITY_REASONS = {"local-source-package", "workspace-package"}
CONTROL_ROOT = Path(__file__).resolve().parents[2]
CONTROL_LICENSE = CONTROL_ROOT / "LICENSE"
CONTROL_NPM_LAUNCHER = CONTROL_ROOT / "cli" / "bun" / "npm" / "bin" / "prose.js"
CONTROL_NPM_README = CONTROL_ROOT / "cli" / "bun" / "npm" / "README.md"
CONTROL_HELLO_EXAMPLE = (
    CONTROL_ROOT / "cli" / "conformance" / "live-alpha" / "hello.prose.md"
)
CONTROL_FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY = (
    CONTROL_ROOT
    / "cli"
    / "shared"
    / "capabilities"
    / "adapters"
    / "functional-alpha.v1.json"
)
CONTROL_OMP_ADAPTER_RECIPE = (
    CONTROL_ROOT
    / "cli"
    / "shared"
    / "capabilities"
    / "adapters"
    / "recipes"
    / "omp-rpc.v1.json"
)
CONTROL_ALPHA_ADMISSION_VERIFIER = CONTROL_ROOT / "cli" / "ci" / "alpha_package_admission.py"
CONTROL_ECHO_IMAGE_MANIFEST = (
    CONTROL_ROOT / "cli" / "shared" / "image" / "echo-v0" / "manifest.json"
)
CONTROL_RELEASE_PACKAGE_INVARIANTS = (
    CONTROL_ROOT / "cli" / "conformance" / "release-package" / "invariants.v1.json"
)
CONTROL_RUNNER_HELP = (
    CONTROL_ROOT / "cli" / "conformance" / "cases" / "fixtures" / "runner-help.txt"
)
NPM_PLATFORM_SELECTORS: dict[str, dict[str, list[str]]] = {
    "darwin-arm64": {"os": ["darwin"], "cpu": ["arm64"]},
    "darwin-x64": {"os": ["darwin"], "cpu": ["x64"]},
    "linux-x64-gnu": {"os": ["linux"], "cpu": ["x64"], "libc": ["glibc"]},
    "linux-arm64-gnu": {"os": ["linux"], "cpu": ["arm64"], "libc": ["glibc"]},
    "win32-x64": {"os": ["win32"], "cpu": ["x64"]},
}
NODE_ENGINE = ">=22.22.3"
NPM_COHORT_SCHEMA = "openprose.npm-cohort/1"
NPM_HOMEPAGE = "https://github.com/openprose/prose/tree/main/cli#readme"
NPM_BUGS = {
    "url": "https://github.com/openprose/prose/issues/new?template=openprose-cli-bug.yml"
}
PROTECTED_AUTHORITY_FILES = {
    "protected-authority-run.json",
    "authority-provenance.json",
    "canonical-profile-attestation.json",
    "release-evidence-attestation.json",
}
PROTECTED_AUTHORITY_WORKFLOW = (
    ".github/workflows/openprose-cli-protected-release-authority.yml"
)
PROTECTED_AUTHORITY_ENVIRONMENT = "openprose-cli-release-authority"
PROTECTED_AUTHORITY_ARTIFACT = "openprose-cli-protected-release-authority"
ALPHA_ADMISSION_FIELDS = {
    "schema",
    "status",
    "targetId",
    "version",
    "sourceSha",
    "image",
    "adapterManifest",
    "fixture",
    "package",
    "installations",
    "executionToolchain",
    "surfaces",
    "executions",
    "claims",
}
ALPHA_ADMISSION_CLAIMS = {
    "providerCalls": "none-provider-free-fixture",
    "semanticEvaluation": False,
    "programPortabilityEvaluation": False,
    "releaseEligible": False,
    "publicationAuthorized": False,
    "rankingProduced": False,
    "strictDescendantContainment": False,
    "runtimeNetworkIsolation": False,
    "candidateExecution": "performed-posix-functional-alpha-echo",
}
OMP_RUNTIME_PREREQUISITE = {
    "runtime": "bun",
    "versionRange": ">=1.3.14",
    "repairCommand": (
        "npm install --global bun@1.3.14 "
        "@oh-my-pi/pi-coding-agent@18.0.9"
    ),
}


class DraftReleaseError(RuntimeError):
    """The draft boundary failed closed before or during a GitHub API call."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _require_positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DraftReleaseError(f"{label} must be a positive integer")
    return value


def _positive_cli_integer(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("must be an unsigned positive integer")
    return int(value)


def _authority_output_path(value: Path | None) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise DraftReleaseError(AUTHORITY_OUTPUT_ERROR)
    try:
        parent = value.parent.resolve(strict=True)
        metadata = parent.stat()
    except OSError as error:
        raise DraftReleaseError(AUTHORITY_OUTPUT_ERROR) from error
    if (
        parent != value.parent
        or value != parent / value.name
        or not stat.S_ISDIR(metadata.st_mode)
        or os.path.lexists(value)
    ):
        raise DraftReleaseError(AUTHORITY_OUTPUT_ERROR)
    return value


def validate_alpha_draft_authority(value: Any) -> dict[str, Any]:
    """Validate one closed, self-authenticating functional-alpha handoff."""

    required = {
        "schema",
        "repository",
        "version",
        "sourceSha",
        "controlSha",
        "tag",
        "releaseId",
        "draft",
        "prerelease",
        "publicationAuthorized",
        "releaseBody",
        "assetCount",
        "assets",
        "assetInventorySha256",
        "workflowRun",
        "outcome",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise DraftReleaseError("functional-alpha draft authority shape is not closed")
    repository = value.get("repository")
    version = value.get("version")
    if (
        value.get("schema") != ALPHA_DRAFT_AUTHORITY_SCHEMA
        or not isinstance(repository, str)
        or REPOSITORY.fullmatch(repository) is None
        or not isinstance(version, str)
        or ALPHA_SEMVER.fullmatch(version) is None
        or value.get("tag") != f"cli-v{version}"
        or not isinstance(value.get("sourceSha"), str)
        or FULL_SHA.fullmatch(value["sourceSha"]) is None
        or not isinstance(value.get("controlSha"), str)
        or FULL_SHA.fullmatch(value["controlSha"]) is None
        or not isinstance(value.get("releaseId"), int)
        or isinstance(value.get("releaseId"), bool)
        or value["releaseId"] <= 0
        or value.get("draft") is not True
        or value.get("prerelease") is not True
        or value.get("publicationAuthorized") is not False
        or value.get("outcome") not in {"created", "resumed"}
        or value.get("assetCount") != 38
    ):
        raise DraftReleaseError("functional-alpha draft authority identity differs")
    release_body = value.get("releaseBody")
    if (
        not isinstance(release_body, dict)
        or set(release_body) != {"byteLength", "sha256"}
        or not isinstance(release_body.get("byteLength"), int)
        or isinstance(release_body.get("byteLength"), bool)
        or not 1 <= release_body["byteLength"] <= MAX_RELEASE_NOTES_BYTES
        or not isinstance(release_body.get("sha256"), str)
        or SHA256.fullmatch(release_body["sha256"]) is None
    ):
        raise DraftReleaseError("functional-alpha release-body authority is malformed")
    workflow = value.get("workflowRun")
    if (
        not isinstance(workflow, dict)
        or set(workflow) != {"path", "id", "attempt"}
        or workflow.get("path") != ALPHA_DRAFT_WORKFLOW
    ):
        raise DraftReleaseError("functional-alpha workflow authority is malformed")
    _require_positive_integer(workflow.get("id"), "workflow run ID")
    _require_positive_integer(workflow.get("attempt"), "workflow run attempt")
    assets = value.get("assets")
    if not isinstance(assets, list) or len(assets) != 38:
        raise DraftReleaseError("functional-alpha asset authority is not closed")
    names: list[str] = []
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or set(asset) != {"name", "sha256", "byteLength"}
            or not isinstance(asset.get("name"), str)
            or AUTHORITY_ASSET_NAME.fullmatch(asset["name"]) is None
            or not isinstance(asset.get("sha256"), str)
            or SHA256.fullmatch(asset["sha256"]) is None
            or not isinstance(asset.get("byteLength"), int)
            or isinstance(asset.get("byteLength"), bool)
            or not 1 <= asset["byteLength"] <= MAX_ALPHA_ASSET_BYTES
        ):
            raise DraftReleaseError("functional-alpha asset authority is malformed")
        names.append(asset["name"])
    if names != sorted(names) or len(set(names)) != 38:
        raise DraftReleaseError("functional-alpha asset authority is ambiguous")
    inventory_sha256 = value.get("assetInventorySha256")
    if (
        not isinstance(inventory_sha256, str)
        or SHA256.fullmatch(inventory_sha256) is None
        or inventory_sha256
        != hashlib.sha256(_canonical_json_bytes(assets)[:-1]).hexdigest()
    ):
        raise DraftReleaseError("functional-alpha asset inventory digest differs")
    return value


def _alpha_draft_authority(
    *,
    repository: str,
    version: str,
    source_sha: str,
    control_sha: str,
    tag: str,
    release_id: int,
    release_notes: str,
    assets: tuple[Asset, ...],
    workflow_run_id: int,
    workflow_run_attempt: int,
    resumed: bool,
) -> dict[str, Any]:
    release_body = release_notes.encode("utf-8")
    asset_records = sorted(
        (
            {
                "name": asset.name,
                "sha256": asset.sha256,
                "byteLength": asset.byte_length,
            }
            for asset in assets
        ),
        key=lambda item: item["name"],
    )
    authority = {
        "schema": ALPHA_DRAFT_AUTHORITY_SCHEMA,
        "repository": repository,
        "version": version,
        "sourceSha": source_sha,
        "controlSha": control_sha,
        "tag": tag,
        "releaseId": release_id,
        "draft": True,
        "prerelease": True,
        "publicationAuthorized": False,
        "releaseBody": {
            "byteLength": len(release_body),
            "sha256": hashlib.sha256(release_body).hexdigest(),
        },
        "assetCount": len(asset_records),
        "assets": asset_records,
        "assetInventorySha256": hashlib.sha256(
            _canonical_json_bytes(asset_records)[:-1]
        ).hexdigest(),
        "workflowRun": {
            "path": ALPHA_DRAFT_WORKFLOW,
            "id": workflow_run_id,
            "attempt": workflow_run_attempt,
        },
        "outcome": "resumed" if resumed else "created",
    }
    return validate_alpha_draft_authority(authority)


def _write_alpha_draft_authority(path: Path, value: dict[str, Any]) -> None:
    destination = _authority_output_path(path)
    encoded = _canonical_json_bytes(validate_alpha_draft_authority(value))
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        os.fchmod(descriptor, 0o400)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short authority write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary_name, destination, follow_symlinks=False)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(destination.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise DraftReleaseError(
            "functional-alpha authority output could not be written immutably"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class Asset:
    name: str
    path: Path
    byte_length: int
    sha256: str
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class DraftIdentity:
    release_id: int
    upload_base: str


def _windows_host_identity(
    value: Any, target: str, boundary: str
) -> dict[str, Any] | None:
    if target != "win-x64":
        if value is not None:
            raise DraftReleaseError(
                f"{boundary} has a Windows host on a non-Windows target: {target}"
            )
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256", "byteLength", "admission"}
        or value.get("path") != WINDOWS_HOST_NAME
        or not isinstance(value.get("sha256"), str)
        or SHA256.fullmatch(value["sha256"]) is None
        or not isinstance(value.get("byteLength"), int)
        or isinstance(value.get("byteLength"), bool)
        or value["byteLength"] <= 0
        or value.get("admission") is not False
    ):
        raise DraftReleaseError(
            f"{boundary} Windows host identity is malformed or admitting: {target}"
        )
    return value


def _linux_runtime_identity(
    value: Any, platform: str, boundary: str
) -> dict[str, Any] | None:
    if not platform.startswith("linux-"):
        if value != "not-applicable":
            raise DraftReleaseError(
                f"{boundary} has a Linux runtime claim on {platform}"
            )
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"minimumGlibc", "requiredGlibcMaximum", "executionEvidence"}
        or value.get("minimumGlibc") != LINUX_MINIMUM_GLIBC
        or value.get("executionEvidence") != LINUX_EXECUTION_EVIDENCE
    ):
        raise DraftReleaseError(f"{boundary} Linux runtime floor is malformed")
    required = value.get("requiredGlibcMaximum")
    if not isinstance(required, dict) or set(required) != {"rust", "bun"}:
        raise DraftReleaseError(f"{boundary} Linux ELF requirements are malformed")
    for implementation in ("rust", "bun"):
        observed = required[implementation]
        if (
            not isinstance(observed, str)
            or re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", observed) is None
            or tuple(int(part) for part in observed.split(".")) > (2, 34)
        ):
            raise DraftReleaseError(
                f"{boundary} {implementation} ELF exceeds the fixed glibc floor"
            )
    return value


def _bun_runtime_identity(value: Any, platform: str, boundary: str) -> dict[str, str]:
    expected = BUN_RUNTIME_BY_PLATFORM.get(platform)
    if (
        expected is None
        or not isinstance(value, dict)
        or set(value) != {"compileTarget", "runtimeVariant"}
        or value != expected
    ):
        raise DraftReleaseError(f"{boundary} Bun runtime is not platform-exact")
    return value


def _json_evidence(encoded: bytes, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise DraftReleaseError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DraftReleaseError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise DraftReleaseError(f"{label} must be a JSON object")
    return value


def _canonical_npm_cohort(
    *, version: str, source_sha: str, image: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": NPM_COHORT_SCHEMA,
        "version": version,
        "sourceRevision": source_sha,
        "releaseChannel": "release-candidate",
        "purpose": image["purpose"],
        "image": image,
        "admittedPlatforms": sorted(NPM_PLATFORM_SELECTORS),
        "semanticStatus": "unverified",
        "releaseEligible": False,
        "publicationAuthorized": False,
    }


def _canonical_npm_launcher(
    *, version: str, source_sha: str, image: dict[str, Any]
) -> bytes:
    try:
        template = CONTROL_NPM_LAUNCHER.read_text("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DraftReleaseError(
            "protected npm launcher template is unavailable"
        ) from error
    if template.count("__OPENPROSE_COHORT__") != 1:
        raise DraftReleaseError(
            "protected npm launcher template has an invalid cohort token"
        )
    cohort = _canonical_npm_cohort(version=version, source_sha=source_sha, image=image)
    return template.replace(
        "__OPENPROSE_COHORT__",
        json.dumps(cohort, sort_keys=True, separators=(",", ":")),
    ).encode()


def _canonical_npm_meta_manifest(
    *, version: str, source_sha: str, image: dict[str, Any]
) -> dict[str, Any]:
    cohort = _canonical_npm_cohort(version=version, source_sha=source_sha, image=image)
    launcher = _canonical_npm_launcher(
        version=version, source_sha=source_sha, image=image
    )
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
            f"@openprose/prose-cli-{identifier}": version
            for identifier in sorted(NPM_PLATFORM_SELECTORS)
        },
        "openproseCohort": cohort,
        "openproseLauncher": {
            "path": "bin/prose.js",
            "byteLength": len(launcher),
            "sha256": hashlib.sha256(launcher).hexdigest(),
        },
    }


def _canonical_npm_platform_manifest(
    *,
    version: str,
    platform: str,
    source_sha: str,
    image: dict[str, Any],
    expected_binary: dict[str, Any],
    windows_host: dict[str, Any] | None,
    linux_runtime: dict[str, Any] | None,
) -> dict[str, Any]:
    selector = NPM_PLATFORM_SELECTORS.get(platform)
    if selector is None:
        raise DraftReleaseError(f"npm package platform is not admitted: {platform}")
    executable_name = "prose.exe" if platform.startswith("win32-") else "prose"
    manifest: dict[str, Any] = {
        "name": f"@openprose/prose-cli-{platform}",
        "version": version,
        "description": f"OpenProse Bun standalone for {platform}",
        "license": "MIT",
        "homepage": NPM_HOMEPAGE,
        "bugs": NPM_BUGS,
        "repository": {
            "type": "git",
            "url": "git+https://github.com/openprose/prose-cli.git",
            "directory": "cli/bun",
        },
        **selector,
        "openproseBinary": f"bin/{executable_name}",
        "openproseBinaryByteLength": expected_binary["byteLength"],
        "openproseBinarySha256": expected_binary["sha256"],
        "openproseSourceRevision": source_sha,
        "openproseImage": image,
        "openproseCohort": _canonical_npm_cohort(
            version=version, source_sha=source_sha, image=image
        ),
        "openprosePlatform": platform,
        "openproseBunCompileTarget": BUN_RUNTIME_BY_PLATFORM[platform]["compileTarget"],
        "openproseBunRuntimeVariant": BUN_RUNTIME_BY_PLATFORM[platform][
            "runtimeVariant"
        ],
    }
    if platform.startswith("linux-"):
        if linux_runtime is None:
            raise DraftReleaseError("Linux npm package is missing its runtime floor")
        manifest.update(
            {
                "openproseMinimumGlibc": linux_runtime["minimumGlibc"],
                "openproseRequiredGlibcMaximum": linux_runtime["requiredGlibcMaximum"][
                    "bun"
                ],
                "openproseLinuxExecutionEvidence": linux_runtime["executionEvidence"],
            }
        )
    if windows_host is not None:
        manifest.update(
            {
                "openproseWindowsProcessHost": f"bin/{WINDOWS_HOST_NAME}",
                "openproseWindowsProcessHostByteLength": windows_host["byteLength"],
                "openproseWindowsProcessHostSha256": windows_host["sha256"],
                "openproseWindowsProcessHostAdmission": False,
            }
        )
    return manifest


def _member_bytes(
    archive: tarfile.TarFile, member: tarfile.TarInfo, name: str
) -> bytes:
    opened = archive.extractfile(member)
    if opened is None:
        raise DraftReleaseError(f"release archive member is unreadable: {name}")
    encoded = opened.read(member.size + 1)
    if len(encoded) != member.size:
        raise DraftReleaseError(f"release archive member size is inconsistent: {name}")
    return encoded


def _dependency_components(report: dict[str, Any], target: str) -> list[dict[str, Any]]:
    inventories = report.get("inventories")
    if not isinstance(inventories, dict) or set(inventories) != {
        "bun",
        "cargo",
        "windowsProcessHostCargo",
    }:
        raise DraftReleaseError(f"dependency inventories are not closed: {target}")
    component_names = {
        "cargo": ("rust-cli", "multi-platform"),
        "windowsProcessHostCargo": ("windows-process-host", "windows"),
        "bun": ("bun-cli", None),
    }
    components: list[dict[str, Any]] = []
    identities: set[str] = set()
    for inventory_name in ("cargo", "windowsProcessHostCargo", "bun"):
        inventory = inventories[inventory_name]
        if not isinstance(inventory, dict) or not isinstance(
            inventory.get("packages"), list
        ):
            raise DraftReleaseError(
                f"dependency inventory is malformed: {target}/{inventory_name}"
            )
        component_name, expected_target = component_names[inventory_name]
        if expected_target is not None and (
            inventory.get("component") != component_name
            or inventory.get("target") != expected_target
        ):
            raise DraftReleaseError(
                f"dependency component provenance is malformed: {target}/{inventory_name}"
            )
        if not inventory["packages"]:
            raise DraftReleaseError(
                f"dependency inventory is empty: {target}/{inventory_name}"
            )
        for package in inventory["packages"]:
            if not isinstance(package, dict) or set(package) != {
                "integrity",
                "name",
                "scopes",
                "source",
                "version",
            }:
                raise DraftReleaseError(
                    f"dependency package is malformed: {target}/{inventory_name}"
                )
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
                raise DraftReleaseError(
                    f"dependency package identity is malformed: {target}"
                )
            integrity_status = integrity.get("status")
            if integrity_status == "declared":
                if set(integrity) != {"status", "algorithm", "digest"}:
                    raise DraftReleaseError(
                        f"dependency integrity record is not closed: {target}"
                    )
            elif integrity_status == "not-applicable":
                if (
                    set(integrity) != {"status", "reason"}
                    or integrity.get("reason") not in NOT_APPLICABLE_INTEGRITY_REASONS
                ):
                    raise DraftReleaseError(
                        f"dependency integrity reason is unsupported: {target}"
                    )
            else:
                raise DraftReleaseError(
                    f"dependency integrity status is unsupported: {target}"
                )
            identity = f"{component_name}\0{name}\0{version}\0{source}".encode()
            bom_ref = f"openprose:dependency:{hashlib.sha256(identity).hexdigest()}"
            if bom_ref in identities:
                raise DraftReleaseError(
                    f"dependency package identity is duplicated: {target}"
                )
            identities.add(bom_ref)
            record: dict[str, Any] = {
                "bom-ref": bom_ref,
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
                    raise DraftReleaseError(
                        f"dependency integrity is malformed: {target}"
                    )
                record["hashes"] = [{"alg": algorithm, "content": digest}]
            components.append(record)
    return components


def _validate_dependency_evidence(
    *,
    captured: dict[str, bytes],
    target: str,
    manifest: dict[str, Any],
) -> bytes:
    encoded = captured[f"{target}-dependency-evidence.json"]
    report = _json_evidence(encoded, f"dependency evidence {target}")
    if set(report) != {
        "authority",
        "generator",
        "inventories",
        "releasePolicy",
        "schema",
        "sources",
    }:
        raise DraftReleaseError(
            f"dependency evidence has unknown or missing fields: {target}"
        )
    if report.get("schema") != "openprose.dependency-evidence/1" or report.get(
        "generator"
    ) != {
        "name": "openprose-dependency-evidence",
        "networkUsed": False,
        "providerFree": True,
        "version": 1,
    }:
        raise DraftReleaseError(f"dependency evidence generator is not exact: {target}")
    if (
        encoded
        != (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ):
        raise DraftReleaseError(
            f"dependency evidence is not canonically encoded: {target}"
        )
    authority = report.get("authority")
    if not isinstance(authority, dict) or {
        key: value.get("status") if isinstance(value, dict) else None
        for key, value in authority.items()
    } != {
        "licenses": "unknown",
        "signing": "not-performed",
        "vulnerabilities": "not-performed",
    }:
        raise DraftReleaseError(
            f"dependency evidence authority is not fail-closed: {target}"
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
        raise DraftReleaseError(
            f"dependency release policy is not fail-closed: {target}"
        )
    sources = report.get("sources")
    if not isinstance(sources, list):
        raise DraftReleaseError(f"dependency sources are malformed: {target}")
    observed_paths: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "byteLength",
            "path",
            "sha256",
        }:
            raise DraftReleaseError(f"dependency source is malformed: {target}")
        path = source.get("path")
        if (
            not isinstance(path, str)
            or path in observed_paths
            or path not in DEPENDENCY_SOURCE_PATHS
            or not isinstance(source.get("byteLength"), int)
            or isinstance(source.get("byteLength"), bool)
            or source["byteLength"] <= 0
            or not isinstance(source.get("sha256"), str)
            or SHA256.fullmatch(source["sha256"]) is None
        ):
            raise DraftReleaseError(
                f"dependency source identity is not closed: {target}"
            )
        observed_paths.add(path)
    if observed_paths != DEPENDENCY_SOURCE_PATHS:
        raise DraftReleaseError(f"dependency source inventory is incomplete: {target}")
    digest = hashlib.sha256(encoded).hexdigest()
    if manifest.get("dependencyEvidence") != {
        "path": "dependency-evidence.json",
        "byteLength": len(encoded),
        "sha256": digest,
        "releasePolicyPassed": False,
    }:
        raise DraftReleaseError(
            f"dependency evidence is not release-manifest-bound: {target}"
        )
    expected_components = _dependency_components(report, target)
    sbom = _json_evidence(captured[f"{target}-sbom.cdx.json"], f"SBOM {target}")
    components = sbom.get("components")
    if not isinstance(components, list):
        raise DraftReleaseError(f"SBOM components are malformed: {target}")
    dependency_components = []
    for component in components:
        if not isinstance(component, dict):
            continue
        properties = component.get("properties")
        if (
            isinstance(properties, list)
            and {
                "name": "openprose:kind",
                "value": "resolved-dependency",
            }
            in properties
        ):
            dependency_components.append(component)
    if dependency_components != expected_components:
        raise DraftReleaseError(
            f"SBOM dependency components are not inventory-bound: {target}"
        )
    sbom_properties = {
        item.get("name"): item.get("value")
        for item in sbom.get("properties", [])
        if isinstance(item, dict)
    }
    if (
        sbom_properties.get("openprose:dependency-inventory")
        != "component-inventory-attached"
        or sbom_properties.get("openprose:dependency-evidence-sha256") != digest
    ):
        raise DraftReleaseError(
            f"SBOM dependency evidence property is not exact: {target}"
        )
    provenance = _json_evidence(
        captured[f"{target}-provenance.json"], f"provenance {target}"
    )
    try:
        dependencies = provenance["predicate"]["buildDefinition"][
            "resolvedDependencies"
        ]
    except (KeyError, TypeError) as error:
        raise DraftReleaseError(
            f"provenance dependency boundary is malformed: {target}"
        ) from error
    evidence_dependencies = [
        item
        for item in dependencies
        if isinstance(item, dict) and item.get("uri") == "openprose:dependency-evidence"
    ]
    if evidence_dependencies != [
        {"uri": "openprose:dependency-evidence", "digest": {"sha256": digest}}
    ]:
        raise DraftReleaseError(
            f"provenance dependency evidence is not exact: {target}"
        )
    return encoded


def _recapture_asset(asset: Asset) -> bytes:
    if (
        len(asset.body) != asset.byte_length
        or hashlib.sha256(asset.body).hexdigest() != asset.sha256
    ):
        raise DraftReleaseError(
            f"captured assembly member identity is invalid: {asset.name}"
        )
    return asset.body


def _member_identity(
    archive: tarfile.TarFile, member: tarfile.TarInfo, name: str
) -> tuple[int, str]:
    opened = archive.extractfile(member)
    if opened is None:
        raise DraftReleaseError(f"release archive member is unreadable: {name}")
    digest = hashlib.sha256()
    byte_length = 0
    for block in iter(lambda: opened.read(1024 * 1024), b""):
        byte_length += len(block)
        if byte_length > MAX_ASSET_BYTES:
            raise DraftReleaseError(f"release archive member exceeds its limit: {name}")
        digest.update(block)
    return byte_length, digest.hexdigest()


def _validate_single_gzip_stream(encoded: bytes, name: str) -> None:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    expanded = 0
    for offset in range(0, len(encoded), 64 * 1024):
        pending = encoded[offset : offset + 64 * 1024]
        while pending:
            block = decompressor.decompress(
                pending,
                min(1024 * 1024, MAX_ASSET_BYTES + 1024 * 1024 - expanded + 1),
            )
            expanded += len(block)
            if expanded > MAX_ASSET_BYTES + 1024 * 1024:
                raise DraftReleaseError(
                    f"release archive expanded stream exceeds its limit: {name}"
                )
            pending = decompressor.unconsumed_tail
            if decompressor.eof:
                if (
                    decompressor.unused_data
                    or pending
                    or offset + 64 * 1024 < len(encoded)
                ):
                    raise DraftReleaseError(
                        f"release archive has trailing or concatenated gzip data: {name}"
                    )
                break
        if decompressor.eof:
            break
    tail = decompressor.flush()
    expanded += len(tail)
    if expanded > MAX_ASSET_BYTES + 1024 * 1024 or not decompressor.eof:
        raise DraftReleaseError(
            f"release archive gzip stream is truncated or oversized: {name}"
        )


def _preflight_tar_stream(encoded: bytes, name: str) -> None:
    expanded_members = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(encoded), mode="r|gz") as archive:
        for member in archive:
            count += 1
            if count > 128 or member.size <= 0 or member.size > MAX_ASSET_BYTES:
                raise DraftReleaseError(
                    f"release archive has an oversized member header: {name}"
                )
            expanded_members += member.size
            if expanded_members > MAX_ASSET_BYTES:
                raise DraftReleaseError(
                    f"release archive expanded members exceed their limit: {name}"
                )
    if count == 0:
        raise DraftReleaseError(f"release archive is empty: {name}")
    _validate_single_gzip_stream(encoded, name)


def _inspect_package_archive_inner(
    encoded: bytes,
    *,
    name: str,
    kind: str,
    implementation: str,
    platform: str,
    version: str,
    source_sha: str,
    image: dict[str, Any],
    expected_binary: dict[str, Any] | None,
    windows_host: dict[str, Any] | None,
    linux_runtime: dict[str, Any] | None,
) -> None:
    _preflight_tar_stream(encoded, name)
    archive = tarfile.open(fileobj=io.BytesIO(encoded), mode="r:gz")
    with archive:
        archive_host = windows_host if kind != "npm-meta" else None
        members = archive.getmembers()
        if not 1 <= len(members) <= 128:
            raise DraftReleaseError(
                f"release archive has an invalid member count: {name}"
            )
        member_names: set[str] = set()
        member_by_name: dict[str, tarfile.TarInfo] = {}
        expanded_bytes = 0
        for member in members:
            segments = member.name.split("/")
            if (
                not member.isfile()
                or not segments
                or any(
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", part) is None
                    for part in segments
                )
                or member.name in member_names
                or member.size <= 0
                or member.size > MAX_ASSET_BYTES
                or member.uid != 0
                or member.gid != 0
                or member.uname != ""
                or member.gname != ""
                or member.mtime != 0
            ):
                raise DraftReleaseError(f"release archive has an unsafe member: {name}")
            expanded_bytes += member.size
            if expanded_bytes > MAX_ASSET_BYTES:
                raise DraftReleaseError(
                    f"release archive expanded size exceeds its limit: {name}"
                )
            member_names.add(member.name)
            member_by_name[member.name] = member
        executable_name = "prose.exe" if platform.startswith("win32-") else "prose"
        if kind == "standalone-archive":
            root = f"openprose-prose-cli-{implementation}-{version}-{platform}"
            expected_members = {
                f"{root}/{executable_name}",
                f"{root}/LICENSE",
                f"{root}/README.txt",
                f"{root}/examples/hello.prose.md",
                *(
                    {f"{root}/{WINDOWS_HOST_NAME}"}
                    if archive_host is not None
                    else set()
                ),
            }
        elif kind == "npm-meta":
            expected_members = {
                "package/package.json",
                "package/bin/prose.js",
                "package/LICENSE",
                "package/README.md",
                "package/examples/hello.prose.md",
            }
        else:
            expected_members = {
                "package/package.json",
                f"package/bin/{executable_name}",
                "package/LICENSE",
                *(
                    {f"package/bin/{WINDOWS_HOST_NAME}"}
                    if archive_host is not None
                    else set()
                ),
            }
        if member_names != expected_members:
            raise DraftReleaseError(
                f"release archive member inventory is not exact: {name}"
            )
        expected_modes = {
            member_name: (
                0o755
                if member_name.endswith(
                    ("/prose", "/prose.exe", f"/{WINDOWS_HOST_NAME}")
                )
                or member_name == "package/bin/prose.js"
                else 0o644
            )
            for member_name in expected_members
        }
        if any(
            member_by_name[path].mode != mode for path, mode in expected_modes.items()
        ):
            raise DraftReleaseError(
                f"release archive member modes are not exact: {name}"
            )
        try:
            protected_license = CONTROL_LICENSE.read_bytes()
        except OSError as error:
            raise DraftReleaseError(
                "protected license source is unavailable"
            ) from error
        license_path = next(
            path for path in expected_members if path.endswith("/LICENSE")
        )
        if (
            _member_bytes(archive, member_by_name[license_path], name)
            != protected_license
        ):
            raise DraftReleaseError(
                f"release archive license is not protected-source exact: {name}"
            )
        if kind in {"standalone-archive", "npm-meta"}:
            try:
                protected_example = CONTROL_HELLO_EXAMPLE.read_bytes()
            except OSError as error:
                raise DraftReleaseError(
                    "protected Hello World example source is unavailable"
                ) from error
            example_path = next(
                path
                for path in expected_members
                if path.endswith("/examples/hello.prose.md")
            )
            if (
                _member_bytes(archive, member_by_name[example_path], name)
                != protected_example
            ):
                raise DraftReleaseError(
                    f"release archive Hello World example is not protected-source exact: {name}"
                )
        if kind == "standalone-archive":
            readme_path = next(
                path for path in expected_members if path.endswith("/README.txt")
            )
            expected_readme = (
                f"OpenProse CLI {implementation} local artifact {version} for {platform}.\n"
                "This development artifact may contain a release-ineligible test image; inspect release-manifest.json.\n"
            )
            if platform.startswith("linux-"):
                if linux_runtime is None:
                    raise DraftReleaseError(
                        f"standalone README lacks Linux runtime authority: {name}"
                    )
                expected_readme += (
                    f"Minimum glibc: {linux_runtime['minimumGlibc']}.\n"
                    f"ELF required GLIBC maximum: {linux_runtime['requiredGlibcMaximum'][implementation]}.\n"
                    "Execution evidence: Ubuntu 22.04 only; other Linux environments are unverified.\n"
                )
            expected_readme = expected_readme.encode()
            if (
                _member_bytes(archive, member_by_name[readme_path], name)
                != expected_readme
            ):
                raise DraftReleaseError(
                    f"standalone README is not protected-source exact: {name}"
                )
        elif kind == "npm-meta":
            try:
                expected_readme = CONTROL_NPM_README.read_bytes()
            except OSError as error:
                raise DraftReleaseError(
                    "protected npm README source is unavailable"
                ) from error
            if (
                _member_bytes(archive, member_by_name["package/README.md"], name)
                != expected_readme
            ):
                raise DraftReleaseError(
                    f"npm README is not protected-source exact: {name}"
                )
            if _member_bytes(
                archive, member_by_name["package/bin/prose.js"], name
            ) != _canonical_npm_launcher(
                version=version, source_sha=source_sha, image=image
            ):
                raise DraftReleaseError(
                    f"npm launcher is not protected-source exact: {name}"
                )
        host_members = [
            member
            for member in members
            if member.name.split("/")[-1] == WINDOWS_HOST_NAME
        ]
        if archive_host is None:
            if host_members:
                raise DraftReleaseError(
                    f"non-Windows archive contains a Windows process host: {name}"
                )
        else:
            if len(host_members) != 1:
                raise DraftReleaseError(
                    f"Windows archive does not contain exactly one process host: {name}"
                )
            host_member = host_members[0]
            if host_member.size != archive_host["byteLength"]:
                raise DraftReleaseError(
                    f"Windows archive process host size is not bound: {name}"
                )
            observed_length, observed_digest = _member_identity(
                archive, host_member, name
            )
            if (
                observed_length != archive_host["byteLength"]
                or observed_digest != archive_host["sha256"]
            ):
                raise DraftReleaseError(
                    f"Windows archive process host digest is not bound: {name}"
                )
        if kind in {"standalone-archive", "npm-platform"}:
            if expected_binary is None:
                raise DraftReleaseError(
                    f"release archive has no native binary authority: {name}"
                )
            executable_path = (
                f"openprose-prose-cli-{implementation}-{version}-{platform}/{executable_name}"
                if kind == "standalone-archive"
                else f"package/bin/{executable_name}"
            )
            executable_member = next(
                member for member in members if member.name == executable_path
            )
            observed_length, observed_digest = _member_identity(
                archive, executable_member, name
            )
            if observed_length != expected_binary.get(
                "byteLength"
            ) or observed_digest != expected_binary.get("sha256"):
                raise DraftReleaseError(
                    f"release archive executable is not native-byte-bound: {name}"
                )
        if kind in {"npm-meta", "npm-platform"}:
            package_member = next(
                (member for member in members if member.name == "package/package.json"),
                None,
            )
            if package_member is None or package_member.size > 1024 * 1024:
                raise DraftReleaseError(
                    f"npm package manifest is unavailable or oversized: {name}"
                )
            opened = archive.extractfile(package_member)
            if opened is None:
                raise DraftReleaseError(f"npm package manifest is unreadable: {name}")
            package = _json_evidence(
                opened.read(1024 * 1024 + 1), f"npm manifest {name}"
            )
            if kind == "npm-meta":
                if package != _canonical_npm_meta_manifest(
                    version=version, source_sha=source_sha, image=image
                ):
                    raise DraftReleaseError(f"npm meta identity is not exact: {name}")
            else:
                assert expected_binary is not None
                expected_platform = _canonical_npm_platform_manifest(
                    version=version,
                    platform=platform,
                    source_sha=source_sha,
                    image=image,
                    expected_binary=expected_binary,
                    windows_host=windows_host,
                    linux_runtime=linux_runtime,
                )
                if package != expected_platform:
                    raise DraftReleaseError(
                        f"npm platform identity is not exact: {name}"
                    )
            host_keys = {
                "openproseWindowsProcessHost",
                "openproseWindowsProcessHostByteLength",
                "openproseWindowsProcessHostSha256",
                "openproseWindowsProcessHostAdmission",
            }
            if kind == "npm-meta" or windows_host is None:
                if host_keys.intersection(package):
                    raise DraftReleaseError(
                        f"npm package has an inapplicable Windows host claim: {name}"
                    )
            else:
                expected = {
                    "openproseWindowsProcessHost": f"bin/{WINDOWS_HOST_NAME}",
                    "openproseWindowsProcessHostByteLength": windows_host["byteLength"],
                    "openproseWindowsProcessHostSha256": windows_host["sha256"],
                    "openproseWindowsProcessHostAdmission": False,
                }
                if any(package.get(key) != value for key, value in expected.items()):
                    raise DraftReleaseError(
                        f"npm Windows host metadata is not bound: {name}"
                    )
                if f"package/bin/{WINDOWS_HOST_NAME}" not in member_names:
                    raise DraftReleaseError(
                        f"npm Windows host is not in the canonical sibling path: {name}"
                    )


def _inspect_package_archive(**arguments: Any) -> None:
    name = str(arguments.get("name", "archive"))
    try:
        _inspect_package_archive_inner(**arguments)
    except DraftReleaseError:
        raise
    except (EOFError, OSError, tarfile.TarError, ValueError, zlib.error) as error:
        raise DraftReleaseError(
            f"release artifact is not a valid bounded tar.gz archive: {name}"
        ) from error


def _validate_supply_chain_host(
    *,
    captured: dict[str, bytes],
    target: str,
    windows_host: dict[str, Any] | None,
    bun_runtime: dict[str, str],
) -> None:
    provenance = _json_evidence(
        captured[f"{target}-provenance.json"], f"provenance {target}"
    )
    sbom = _json_evidence(captured[f"{target}-sbom.cdx.json"], f"SBOM {target}")
    expected_parameter: Any = (
        windows_host if windows_host is not None else "not-applicable"
    )
    try:
        definition = provenance["predicate"]["buildDefinition"]
        parameters = definition["externalParameters"]
        dependencies = definition["resolvedDependencies"]
    except (KeyError, TypeError) as error:
        raise DraftReleaseError(
            f"provenance host boundary is malformed: {target}"
        ) from error
    if (
        parameters.get("windowsProcessHost") != expected_parameter
        or parameters.get("bunRuntime") != bun_runtime
        or not isinstance(dependencies, list)
    ):
        raise DraftReleaseError(
            f"provenance host identity is not package-bound: {target}"
        )
    host_dependencies = [
        item
        for item in dependencies
        if isinstance(item, dict)
        and item.get("uri") == "openprose:windows-process-host"
    ]
    expected_dependencies = (
        [
            {
                "uri": "openprose:windows-process-host",
                "digest": {"sha256": windows_host["sha256"]},
            }
        ]
        if windows_host is not None
        else []
    )
    if host_dependencies != expected_dependencies:
        raise DraftReleaseError(f"provenance host dependency is not exact: {target}")
    components = sbom.get("components")
    if not isinstance(components, list):
        raise DraftReleaseError(f"SBOM components are malformed: {target}")
    host_components = [
        item
        for item in components
        if isinstance(item, dict) and item.get("name") == WINDOWS_HOST_NAME
    ]
    if windows_host is None:
        if host_components:
            raise DraftReleaseError(
                f"non-Windows SBOM contains a Windows process host: {target}"
            )
        return
    if len(host_components) != 1:
        raise DraftReleaseError(
            f"Windows SBOM does not contain exactly one process host: {target}"
        )
    component = host_components[0]
    if component.get("type") != "file" or component.get("hashes") != [
        {"alg": "SHA-256", "content": windows_host["sha256"]}
    ]:
        raise DraftReleaseError(
            f"Windows SBOM process host digest is not exact: {target}"
        )
    properties = {
        item.get("name"): item.get("value")
        for item in component.get("properties", [])
        if isinstance(item, dict)
    }
    if properties != {
        "openprose:kind": "windows-process-host-sidecar",
        "openprose:release-admission": "false",
    }:
        raise DraftReleaseError(
            f"Windows SBOM process host claims are not exact: {target}"
        )


def _validate_protected_authority(
    *,
    captured: dict[str, bytes],
    repository: str,
    source_sha: str,
    control_sha: str,
    version: str,
    package_image: dict[str, Any],
    package_gates: dict[str, Any],
) -> None:
    """Validate the exact protected producer chain at the write boundary."""

    canonical_bytes = captured["canonical-profile-attestation.json"]
    evidence_bytes = captured["release-evidence-attestation.json"]
    metadata_bytes = captured["protected-authority-run.json"]
    provenance_bytes = captured["authority-provenance.json"]
    preflight_bytes = captured["profile-preflight.json"]

    canonical = _json_evidence(canonical_bytes, "canonical profile attestation")
    evidence = _json_evidence(evidence_bytes, "release evidence attestation")
    metadata = _json_evidence(metadata_bytes, "protected authority run metadata")
    provenance = _json_evidence(provenance_bytes, "protected authority provenance")
    preflight = _json_evidence(preflight_bytes, "profile preflight")

    package_image_keys = {
        "formatVersion",
        "version",
        "sha256",
        "manifestSha256",
        "purpose",
        "releaseEligible",
    }
    if (
        not isinstance(package_image, dict)
        or set(package_image) != package_image_keys
        or not isinstance(package_image.get("formatVersion"), str)
        or not package_image["formatVersion"]
        or not isinstance(package_image.get("version"), str)
        or not package_image["version"]
        or package_image.get("releaseEligible") is not True
        or package_image.get("purpose") != "canonical-language-runtime"
        or not isinstance(package_image.get("sha256"), str)
        or SHA256.fullmatch(package_image["sha256"]) is None
        or not isinstance(package_image.get("manifestSha256"), str)
        or SHA256.fullmatch(package_image["manifestSha256"]) is None
    ):
        raise DraftReleaseError(
            "protected authority package image identity is malformed"
        )
    image_sha256 = package_image["sha256"]
    image_manifest_sha256 = package_image["manifestSha256"]

    expected_canonical = {
        "schema": "openprose.canonical-profile-attestation/1",
        "authority": "protected-canonical-profile",
        "sourceSha": source_sha,
        "imageSha256": image_sha256,
        "imageManifestSha256": image_manifest_sha256,
        "status": "pass",
    }
    if canonical != expected_canonical:
        raise DraftReleaseError("canonical profile attestation is not exact")
    expected_checks = {
        "benchmarkTrust": "pass",
        "conformance": "pass",
        "portability": "pass",
        "vulnerabilityReview": "pass",
    }
    if evidence != {
        "schema": "openprose.release-evidence-attestation/1",
        "authority": "protected-release-evidence",
        "sourceSha": source_sha,
        "imageSha256": image_sha256,
        "imageManifestSha256": image_manifest_sha256,
        "status": "pass",
        "checks": expected_checks,
    }:
        raise DraftReleaseError("release evidence attestation is not exact")

    metadata_keys = {
        "schema",
        "repository",
        "runId",
        "runAttempt",
        "headSha",
        "headBranch",
        "workflowPath",
        "event",
        "conclusion",
        "artifactId",
        "artifactName",
    }
    if not isinstance(metadata, dict) or set(metadata) != metadata_keys:
        raise DraftReleaseError("protected authority run metadata shape is not closed")
    producer_repository = metadata.get("repository")
    run_id = metadata.get("runId")
    run_attempt = metadata.get("runAttempt")
    producer_sha = metadata.get("headSha")
    artifact_id = metadata.get("artifactId")
    if (
        metadata.get("schema") != "openprose.github-protected-authority-run/1"
        or producer_repository != repository
        or not isinstance(run_id, int)
        or isinstance(run_id, bool)
        or run_id <= 0
        or not isinstance(run_attempt, int)
        or isinstance(run_attempt, bool)
        or run_attempt <= 0
        or not isinstance(producer_sha, str)
        or FULL_SHA.fullmatch(producer_sha) is None
        or not isinstance(artifact_id, int)
        or isinstance(artifact_id, bool)
        or artifact_id <= 0
        or metadata.get("headBranch") != "main"
        or metadata.get("workflowPath") != PROTECTED_AUTHORITY_WORKFLOW
        or metadata.get("event") != "workflow_dispatch"
        or metadata.get("conclusion") != "success"
        or metadata.get("artifactName") != PROTECTED_AUTHORITY_ARTIFACT
    ):
        raise DraftReleaseError("protected authority run metadata is not exact")

    def attestation_record(path: str, encoded: bytes) -> dict[str, Any]:
        return {
            "path": path,
            "byteLength": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    attestations = {
        "canonicalProfile": attestation_record(
            "canonical-profile-attestation.json", canonical_bytes
        ),
        "releaseEvidence": attestation_record(
            "release-evidence-attestation.json", evidence_bytes
        ),
    }
    expected_package_gates = {
        "canonicalProfile": {
            "byteLength": len(canonical_bytes),
            "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
        },
        "releaseEvidence": {
            "byteLength": len(evidence_bytes),
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        },
        "authorityValidatedByPackager": False,
    }
    if package_gates != expected_package_gates:
        raise DraftReleaseError(
            "package manifests do not bind the exact protected gates"
        )
    expected_producer = {
        "repository": producer_repository,
        "runId": run_id,
        "runAttempt": run_attempt,
        "headSha": producer_sha,
        "workflowPath": PROTECTED_AUTHORITY_WORKFLOW,
        "environment": PROTECTED_AUTHORITY_ENVIRONMENT,
    }
    expected_subject = {
        "sourceSha": source_sha,
        "imageSha256": image_sha256,
        "imageManifestSha256": image_manifest_sha256,
    }
    if provenance != {
        "schema": "openprose.protected-release-authority-provenance/1",
        "producer": expected_producer,
        "subject": expected_subject,
        "attestations": attestations,
        "publicationAuthorized": False,
    }:
        raise DraftReleaseError("protected authority provenance is not exact")

    preflight_keys = {
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
    }
    if not isinstance(preflight, dict) or set(preflight) != preflight_keys:
        raise DraftReleaseError("profile preflight shape is not closed")
    if (
        preflight.get("schema") != "openprose.release-preflight-report/1"
        or preflight.get("status") != "pass"
        or preflight.get("releaseKind") != "draft-only"
        or preflight.get("publicationAuthorized") is not False
        or preflight.get("version") != version
        or preflight.get("sourceSha") != source_sha
        or preflight.get("checkedOutSha") != source_sha
        or preflight.get("controlSha") != control_sha
        or preflight.get("controlRef") != "refs/heads/main"
        or preflight.get("productVersions") != {"rust": version, "bun": version}
        or preflight.get("failures") != []
    ):
        raise DraftReleaseError("profile preflight identity is not exact")
    preflight_image = preflight.get("image")
    if (
        not isinstance(preflight_image, dict)
        or set(preflight_image)
        != {
            "manifestSha256",
            "bundleSha256",
            "checksumSha256",
            "imageSha256",
            "version",
            "purpose",
            "releaseEligible",
        }
        or preflight_image.get("manifestSha256") != image_manifest_sha256
        or preflight_image.get("imageSha256") != image_sha256
        or preflight_image.get("releaseEligible") is not True
        or preflight_image.get("version") != package_image["version"]
        or preflight_image.get("purpose") != package_image["purpose"]
        or any(
            not isinstance(preflight_image.get(key), str)
            or SHA256.fullmatch(preflight_image[key]) is None
            for key in ("bundleSha256", "checksumSha256")
        )
    ):
        raise DraftReleaseError("profile preflight image identity is not exact")

    expected_authority = {
        "status": "pass",
        "artifactId": str(artifact_id),
        "producerRunId": run_id,
        "producerRunAttempt": run_attempt,
        "producerSha": producer_sha,
        "runMetadataSha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "provenanceSha256": hashlib.sha256(provenance_bytes).hexdigest(),
        "attestations": attestations,
    }
    if preflight.get("protectedAuthority") != expected_authority:
        raise DraftReleaseError("profile preflight protected authority is not exact")
    expected_gates = {
        "canonicalProfile": {
            "status": "pass",
            "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
        },
        "releaseEvidence": {
            "status": "pass",
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        },
    }
    if preflight.get("gates") != expected_gates:
        raise DraftReleaseError("profile preflight gates are not exact")


def _regular_file(
    path: Path, *, maximum: int = MAX_ASSET_BYTES
) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as error:
        raise DraftReleaseError(
            f"assembly file is unavailable: {path.name}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise DraftReleaseError(
            f"assembly member must be a non-symlink regular file: {path.name}"
        )
    if before.st_size <= 0 or before.st_size > maximum:
        raise DraftReleaseError(f"assembly member has an invalid size: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise DraftReleaseError(f"assembly member changed before open: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = source.read(maximum + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(value) > maximum or len(value) != opened.st_size:
        raise DraftReleaseError(
            f"assembly member changed or exceeded its limit: {path.name}"
        )
    if opened.st_size != after.st_size or opened.st_mtime_ns != after.st_mtime_ns:
        raise DraftReleaseError(f"assembly member changed while read: {path.name}")
    return value, opened


def _release_notes_renderer() -> Any:
    renderer_path = Path(__file__).with_name("render_release_notes.py")
    spec = importlib.util.spec_from_file_location(
        "openprose_protected_release_notes", renderer_path
    )
    if spec is None or spec.loader is None:
        raise DraftReleaseError("release-note renderer is unavailable")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    return renderer


def _alpha_admission_verifier() -> Any:
    spec = importlib.util.spec_from_file_location(
        "openprose_protected_alpha_admission", CONTROL_ALPHA_ADMISSION_VERIFIER
    )
    if spec is None or spec.loader is None:
        raise DraftReleaseError("alpha admission verifier is unavailable")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier


def _alpha_adapter_manifest_identity() -> dict[str, Any]:
    """Rebuild the exact W71 authority identity at the draft boundary."""

    manifest_bytes, _ = _regular_file(
        CONTROL_FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY,
        maximum=MAX_EVIDENCE_BYTES,
    )
    recipe_bytes, _ = _regular_file(
        CONTROL_OMP_ADAPTER_RECIPE,
        maximum=MAX_EVIDENCE_BYTES,
    )
    manifest = _json_evidence(
        manifest_bytes, "functional-alpha adapter authority"
    )
    recipe = _json_evidence(recipe_bytes, "OMP adapter recipe authority")
    adapters = manifest.get("adapters")
    expected_ids = [
        "prime/rpc",
        "omp/rpc",
        "codex/exec-json",
        "claude/print-stream-json",
    ]
    if (
        manifest.get("schema")
        != "openprose.functional-alpha-adapter-oracle/1"
        or manifest.get("status") != "functional-alpha"
        or manifest.get("imageVersion") != "echo-v0"
        or manifest.get("semanticStatus") != "not-applicable"
        or manifest.get("fallback") != "forbidden"
        or not isinstance(adapters, list)
        or [
            item.get("adapterId") if isinstance(item, dict) else None
            for item in adapters
        ]
        != expected_ids
    ):
        raise DraftReleaseError("functional-alpha adapter authority is not exact")
    omp = adapters[1]
    if (
        omp.get("runtimePrerequisites") != [OMP_RUNTIME_PREREQUISITE]
        or omp.get("repairCommand") != OMP_RUNTIME_PREREQUISITE["repairCommand"]
    ):
        raise DraftReleaseError("functional-alpha OMP runtime authority is not exact")
    support = recipe.get("support")
    if (
        recipe.get("adapterId") != "omp/rpc"
        or not isinstance(support, dict)
        or support.get("runtimePrerequisites") != [OMP_RUNTIME_PREREQUISITE]
        or support.get("repairCommand")
        != OMP_RUNTIME_PREREQUISITE["repairCommand"]
    ):
        raise DraftReleaseError("OMP recipe runtime authority is not exact")
    return {
        "path": "cli/shared/capabilities/adapters/functional-alpha.v1.json",
        "byteLength": len(manifest_bytes),
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "adapterIds": expected_ids,
        "ompRuntimePrerequisite": dict(OMP_RUNTIME_PREREQUISITE),
        "ompRecipe": {
            "path": "cli/shared/capabilities/adapters/recipes/omp-rpc.v1.json",
            "byteLength": len(recipe_bytes),
            "sha256": hashlib.sha256(recipe_bytes).hexdigest(),
        },
    }


def _validate_alpha_admission_authority(
    admission: dict[str, Any],
    encoded: bytes,
    target: str,
    expected_adapter_manifest: dict[str, Any],
) -> None:
    if set(admission) != ALPHA_ADMISSION_FIELDS:
        raise DraftReleaseError(
            f"alpha package admission has unknown or missing fields: {target}"
        )
    canonical = (
        json.dumps(admission, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if encoded != canonical:
        raise DraftReleaseError(
            f"alpha package admission is not canonical JSON: {target}"
        )
    if admission.get("claims") != ALPHA_ADMISSION_CLAIMS:
        raise DraftReleaseError(f"alpha package admission claims differ: {target}")
    if admission.get("adapterManifest") != expected_adapter_manifest:
        raise DraftReleaseError(
            f"alpha package admission adapter authority differs: {target}"
        )
    if (
        not isinstance(admission.get("image"), dict)
        or not admission["image"]
        or not isinstance(admission.get("fixture"), dict)
        or not admission["fixture"]
        or not isinstance(admission.get("package"), dict)
        or not admission["package"]
        or not isinstance(admission.get("installations"), list)
        or len(admission["installations"]) != 3
        or not isinstance(admission.get("executionToolchain"), dict)
        or not admission["executionToolchain"]
        or not isinstance(admission.get("surfaces"), dict)
        or set(admission["surfaces"])
        != {"direct-rust", "direct-bun", "npm-launcher"}
        or not isinstance(admission.get("executions"), list)
        or not admission["executions"]
    ):
        raise DraftReleaseError(
            f"alpha package admission evidence shape differs: {target}"
        )


def _deep_validate_alpha_admission(
    *,
    admission: dict[str, Any],
    target: str,
    platform: str,
    version: str,
    source_sha: str,
    manifest: dict[str, Any],
    assets_by_name: dict[str, Asset],
    expected_adapter_manifest: dict[str, Any],
) -> None:
    """Reauthenticate the full provider-free report before any network call."""

    verifier = _alpha_admission_verifier()
    try:
        benchmark = verifier.load_benchmark()
        image, _ = verifier.image_identity(benchmark, CONTROL_ECHO_IMAGE_MANIFEST)
        fixture_bytes, _ = _regular_file(
            Path(verifier.FAKE_HARNESS), maximum=MAX_EVIDENCE_BYTES
        )
    except Exception as error:
        raise DraftReleaseError(
            f"alpha package admission authority is unavailable: {target}"
        ) from error

    artifact_names = {
        f"openprose-prose-cli-rust-{version}-{platform}.tar.gz",
        f"openprose-prose-cli-bun-{version}-{platform}.tar.gz",
        f"openprose-prose-cli-{platform}-{version}.tgz",
        f"openprose-prose-cli-{version}.tgz",
    }
    records = manifest.get("artifacts")
    if (
        not isinstance(records, list)
        or len(records) != len(artifact_names)
        or {record.get("path") for record in records if isinstance(record, dict)}
        != artifact_names
    ):
        raise DraftReleaseError(
            f"alpha package admission artifact closure differs: {target}"
        )
    build_profiles = manifest.get("buildProfiles")
    if build_profiles != {
        "rust": {"profile": "release", "testSeamsEnabled": False},
        "bun": {"profile": "release", "testSeamsEnabled": False},
    }:
        raise DraftReleaseError(
            f"alpha package admission build profiles differ: {target}"
        )

    source_assets = {
        "release-manifest.json": assets_by_name[f"{target}-release-manifest.json"],
        "sbom.cdx.json": assets_by_name[f"{target}-sbom.cdx.json"],
        "provenance.json": assets_by_name[f"{target}-provenance.json"],
        "dependency-evidence.json": assets_by_name[
            f"{target}-dependency-evidence.json"
        ],
        "SHA256SUMS": assets_by_name[f"{target}-SHA256SUMS"],
    }
    source_assets.update({name: assets_by_name[name] for name in artifact_names})
    package_files = sorted(
        (
            {
                "path": name,
                "byteLength": asset.byte_length,
                "sha256": asset.sha256,
            }
            for name, asset in source_assets.items()
        ),
        key=lambda item: item["path"],
    )
    target_sums = source_assets["SHA256SUMS"]
    target_manifest = source_assets["release-manifest.json"]
    expected_package = {
        "mode": "alpha",
        "platform": platform,
        "files": package_files,
        "sha256Sums": {
            "byteLength": target_sums.byte_length,
            "sha256": target_sums.sha256,
        },
        "releaseManifest": {
            "byteLength": target_manifest.byte_length,
            "sha256": target_manifest.sha256,
        },
        "buildProfiles": build_profiles,
    }
    expected_artifacts = {
        name: assets_by_name[name].sha256 for name in artifact_names
    }
    try:
        verifier.validate_report_contract(
            report=admission,
            target_id=target,
            version=version,
            source_sha=source_sha,
            image=image,
            adapter_manifest=expected_adapter_manifest,
            fixture={
                "path": str(verifier.FAKE_HARNESS.relative_to(verifier.CLI.parent)),
                "byteLength": len(fixture_bytes),
                "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            },
            expected_package=expected_package,
            expected_artifacts=expected_artifacts,
            image_manifest=CONTROL_ECHO_IMAGE_MANIFEST,
            benchmark_module=benchmark,
        )
    except Exception as error:
        if error.__class__.__name__ == "AdmissionError":
            raise DraftReleaseError(
                f"alpha package admission deep validation failed: {target}"
            ) from error
        raise DraftReleaseError(
            f"alpha package admission verifier failed: {target}"
        ) from error


def _release_notes(assets: tuple[Asset, ...]) -> str:
    try:
        renderer = _release_notes_renderer()
        captured = {asset.name: asset.body for asset in assets}
        manifests = {
            target: _json_evidence(
                captured[f"{target}-release-manifest.json"],
                f"release manifest {target}",
            )
            for target in TARGETS
        }
        encoded = renderer.render_release_notes(manifests)
        return encoded.decode("utf-8")
    except Exception as error:
        if error.__class__.__name__ == "ReleaseNotesError":
            raise DraftReleaseError(
                f"release-note evidence failed closed: {error}"
            ) from error
        raise DraftReleaseError(f"release-note renderer failed: {error}") from error


def _functional_alpha_release_notes(version: str) -> str:
    try:
        encoded = _release_notes_renderer().render_functional_alpha_notes(version)
        return encoded.decode("utf-8")
    except Exception as error:
        if error.__class__.__name__ == "ReleaseNotesError":
            raise DraftReleaseError(
                f"functional-alpha release-note authority failed closed: {error}"
            ) from error
        if isinstance(error, DraftReleaseError):
            raise
        raise DraftReleaseError(
            f"functional-alpha release-note renderer failed: {error}"
        ) from error


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _digest_record(encoded: bytes) -> dict[str, Any]:
    return {"byteLength": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}


def _release_package_corpus() -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    try:
        encoded = CONTROL_RELEASE_PACKAGE_INVARIANTS.read_bytes()
    except OSError as error:
        raise DraftReleaseError(
            "protected release package corpus is unavailable"
        ) from error
    if not encoded or len(encoded) > MAX_EVIDENCE_BYTES:
        raise DraftReleaseError(
            "protected release package corpus exceeds its closed bound"
        )
    corpus = _json_evidence(encoded, "protected release package corpus")
    if (
        set(corpus) != {"schema", "surfaces", "cases", "claims"}
        or corpus.get("schema") != "openprose.release-package-invariants/1"
        or corpus.get("surfaces")
        != [
            {"id": "direct-rust", "runner": "rust"},
            {"id": "direct-bun", "runner": "bun"},
            {"id": "npm-launcher", "runner": "bun"},
        ]
        or corpus.get("claims")
        != {
            "fullPhase7Corpus": False,
            "languageSemantics": False,
            "programPortability": False,
            "providerCalls": "none",
        }
        or not isinstance(corpus.get("cases"), list)
        or not corpus["cases"]
    ):
        raise DraftReleaseError("protected release package corpus is not closed")
    cases: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        if (
            not isinstance(case, dict)
            or set(case) != {"id", "argv", "exitCode", "stdout"}
            or not isinstance(case.get("id"), str)
            or not isinstance(case.get("argv"), list)
            or not all(isinstance(value, str) for value in case["argv"])
            or not isinstance(case.get("exitCode"), int)
            or isinstance(case.get("exitCode"), bool)
            or not isinstance(case.get("stdout"), dict)
            or case["stdout"].get("kind") not in {"version", "exact-file", "json"}
        ):
            raise DraftReleaseError("protected release package case is malformed")
        cases.append(case)
    if len({case["id"] for case in cases}) != len(cases):
        raise DraftReleaseError("protected release package case identity is duplicated")
    return tuple(cases), _digest_record(encoded)


def _expected_release_projection(
    case: dict[str, Any],
    *,
    runner: str,
    version: str,
    source_sha: str,
    image: dict[str, Any],
) -> Any:
    case_id = case["id"]
    if case_id == "version":
        return {"kind": "version", "runner": runner, "version": version}
    if case_id == "help":
        return {
            "kind": "help",
            "byteLength": case["stdout"]["byteLength"],
            "sha256": case["stdout"]["sha256"],
        }
    if case_id == "config-explain":
        return {
            "schema": "openprose.configuration-explanation/1",
            "harness": "openprose",
            "transport": "auto",
            "output": "json",
            "color": False,
        }
    if case_id == "harness-list":
        return {
            "schema": "openprose.harness-list/1",
            "selected": "openprose",
            "availability": "unavailable",
            "detectedVersion": None,
            "strictWrapperConformant": False,
            "testOnly": True,
            "admissionBlock": "test-seams-disabled",
        }
    if case_id in {"doctor-default", "doctor-mock-refused"}:
        harness = "mock" if case_id == "doctor-mock-refused" else "openprose"
        return {
            "schema": "openprose.doctor-report/1",
            "runner": {"name": runner, "version": version, "commit": source_sha},
            "ready": False,
            "harness": harness,
            "error": "HARNESS_UNAVAILABLE"
            if harness == "mock"
            else "HOSTED_UNAVAILABLE",
            "build": {"profile": "release", "testSeamsEnabled": False},
            "image": {
                key: image[key]
                for key in ("formatVersion", "version", "sha256", "releaseEligible")
            },
        }
    if case_id in {"run-mock-refused", "dry-run-mock-refused"}:
        return {
            "schema": "openprose.runner-result/1",
            "runner": {"name": runner, "version": version, "commit": source_sha},
            "adapter": {
                "id": "mock/unavailable",
                "harnessVersion": None,
                "descriptorDigestSha256": (
                    "599d3baf7b7aee1f97855dd4ca13e780"
                    "c4de0aff410ad5860000631c0ac22fa4"
                ),
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
    raise DraftReleaseError(f"protected release package case is unsupported: {case_id}")


def _validate_release_package_admission(
    *,
    report: dict[str, Any],
    target: str,
    source_sha: str,
    control_sha: str,
    version: str,
    captured: dict[str, bytes],
    assets_by_name: dict[str, Asset],
    manifest: dict[str, Any],
    native: dict[str, Any],
    verification: dict[str, Any],
) -> tuple[int, int]:
    """Independently bind one package-admission report to assembly-owned bytes."""
    report_keys = {
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
    }
    if not isinstance(report, dict) or set(report) != report_keys:
        raise DraftReleaseError(
            f"release package admission shape is not closed: {target}"
        )
    windows = target == "win-x64"
    expected_status = (
        "blocked-before-execution"
        if windows
        else "passed-provider-free-release-invariants"
    )
    if (
        report.get("schema") != "openprose.release-package-admission/3"
        or report.get("status") != expected_status
        or report.get("targetId") != target
        or report.get("version") != version
        or report.get("sourceSha") != source_sha
        or report.get("controlSha") != control_sha
    ):
        raise DraftReleaseError(
            f"release package admission identity is not exact: {target}"
        )

    workflow = report.get("workflowRun")
    if (
        not isinstance(workflow, dict)
        or set(workflow) != {"id", "attempt"}
        or not _positive_integer(workflow.get("id"))
        or not _positive_integer(workflow.get("attempt"))
    ):
        raise DraftReleaseError(
            f"release package admission workflow is malformed: {target}"
        )
    workflow_identity = (workflow["id"], workflow["attempt"])

    authority = report.get("authorityInputs")
    expected_authority = {
        "profilePreflight": _digest_record(captured["profile-preflight.json"]),
        "nativeVerification": _digest_record(captured[f"{target}-verification.json"]),
    }
    if authority != expected_authority:
        raise DraftReleaseError(
            f"release package admission authority is stale: {target}"
        )

    protected_cases, protected_corpus = _release_package_corpus()
    if report.get("corpus") != protected_corpus:
        raise DraftReleaseError(
            f"release package invariant corpus is not byte-bound: {target}"
        )

    package = report.get("package")
    package_keys = {
        "platform",
        "mode",
        "sha256Sums",
        "releaseManifest",
        "files",
        "image",
        "buildProfiles",
        "nativeLineage",
    }
    if not isinstance(package, dict) or set(package) != package_keys:
        raise DraftReleaseError(
            f"release package admission package shape is not closed: {target}"
        )
    if (
        package.get("platform") != TARGET_PLATFORMS[target]
        or package.get("mode") != "release"
        or package.get("sha256Sums") != _digest_record(captured[f"{target}-SHA256SUMS"])
        or package.get("releaseManifest")
        != _digest_record(captured[f"{target}-release-manifest.json"])
        or package.get("image") != manifest.get("image")
        or package.get("buildProfiles")
        != {
            "rust": {"profile": "release", "testSeamsEnabled": False},
            "bun": {"profile": "release", "testSeamsEnabled": False},
        }
    ):
        raise DraftReleaseError(
            f"release package admission package is not byte-bound: {target}"
        )

    artifact_records = manifest["artifacts"]
    original_to_assembly = {
        "SHA256SUMS": f"{target}-SHA256SUMS",
        "release-manifest.json": f"{target}-release-manifest.json",
        "sbom.cdx.json": f"{target}-sbom.cdx.json",
        "provenance.json": f"{target}-provenance.json",
        "dependency-evidence.json": f"{target}-dependency-evidence.json",
        **{record["path"]: record["path"] for record in artifact_records},
    }
    expected_package_files = [
        {
            "path": original,
            "byteLength": assets_by_name[assembly_name].byte_length,
            "sha256": assets_by_name[assembly_name].sha256,
        }
        for original, assembly_name in sorted(original_to_assembly.items())
    ]
    if package.get("files") != expected_package_files:
        raise DraftReleaseError(
            f"release package admission file map is stale: {target}"
        )

    verification_artifact = verification.get("nativeArtifact")
    if (
        not isinstance(verification_artifact, dict)
        or set(verification_artifact)
        != {"name", "workflowRunId", "workflowRunAttempt", "files"}
        or verification_artifact.get("name") != f"native-build-{target}"
        or verification_artifact.get("workflowRunId") != workflow_identity[0]
        or verification_artifact.get("workflowRunAttempt") != workflow_identity[1]
        or not isinstance(verification_artifact.get("files"), dict)
    ):
        raise DraftReleaseError(
            f"release package admission native artifact is stale: {target}"
        )
    expected_lineage_products = {
        implementation: {
            "nativePath": native["products"][implementation]["path"],
            "nativeSha256": native["products"][implementation]["sha256"],
            "packagedBinarySha256": native["products"][implementation]["sha256"],
        }
        for implementation in ("rust", "bun")
    }
    expected_lineage = {
        "nativeArtifact": {
            "name": f"native-build-{target}",
            "workflowRunId": workflow_identity[0],
            "workflowRunAttempt": workflow_identity[1],
        },
        "nativeManifestSha256": hashlib.sha256(
            captured[f"{target}-native-manifest.json"]
        ).hexdigest(),
        "files": verification_artifact["files"],
        "products": expected_lineage_products,
        "windowsProcessHost": (
            native.get("windowsProcessHost") if windows else "not-applicable"
        ),
    }
    if package.get("nativeLineage") != expected_lineage:
        raise DraftReleaseError(
            f"release package admission native lineage is stale: {target}"
        )

    expected_installation_surfaces = ("direct-rust", "direct-bun", "npm-launcher")
    installations = report.get("installations")
    if windows:
        if installations != []:
            raise DraftReleaseError(
                "Windows release package admission claims an installation"
            )
        installation_trees: dict[str, str] = {}
    else:
        if not isinstance(installations, list) or len(installations) != 3:
            raise DraftReleaseError(
                f"release package installation inventory is not closed: {target}"
            )
        expected_methods = (
            "validated-archive-extraction",
            "validated-archive-extraction",
            "npm-global-offline-two-local-tarballs",
        )
        installation_trees = {}
        for record, surface, method in zip(
            installations, expected_installation_surfaces, expected_methods, strict=True
        ):
            if (
                not isinstance(record, dict)
                or set(record)
                != {"surface", "method", "installedByteCount", "treeSha256"}
                or record.get("surface") != surface
                or record.get("method") != method
                or not _nonnegative_integer(record.get("installedByteCount"))
                or not isinstance(record.get("treeSha256"), str)
                or SHA256.fullmatch(record["treeSha256"]) is None
            ):
                raise DraftReleaseError(
                    f"release package installation is malformed: {target}"
                )
            installation_trees[surface] = record["treeSha256"]

    toolchain = report.get("executionToolchain")
    if not isinstance(toolchain, dict) or set(toolchain) != {
        "authority",
        "node",
        "npm",
    }:
        raise DraftReleaseError(
            f"release package execution toolchain is not closed: {target}"
        )
    if windows:
        if toolchain != {
            "authority": "not-observed-no-candidate-execution",
            "node": None,
            "npm": None,
        }:
            raise DraftReleaseError(
                "Windows release package admission claims execution tools"
            )
    else:
        if (
            toolchain.get("authority")
            != "reporter-observed-and-finally-reauthenticated-executable-bytes"
        ):
            raise DraftReleaseError(
                f"release package toolchain authority is false: {target}"
            )
        for name in ("node", "npm"):
            tool = toolchain.get(name)
            if (
                not isinstance(tool, dict)
                or set(tool) != {"command", "resolvedPath", "sha256"}
                or not isinstance(tool.get("command"), str)
                or tool.get("command") != tool.get("resolvedPath")
                or not Path(tool["command"]).is_absolute()
                or "\x00" in tool["command"]
                or not isinstance(tool.get("sha256"), str)
                or SHA256.fullmatch(tool["sha256"]) is None
            ):
                raise DraftReleaseError(
                    f"release package {name} executable identity is malformed: {target}"
                )

    artifact_by_shape = {
        (record["implementation"], record["kind"]): record
        for record in artifact_records
    }
    launcher_sha = hashlib.sha256(
        _canonical_npm_launcher(
            version=version, source_sha=source_sha, image=manifest["image"]
        )
    ).hexdigest()
    expected_surface_bindings = {
        "direct-rust": {
            "runner": "rust",
            "binarySha256": native["products"]["rust"]["sha256"],
            "artifactSha256": artifact_by_shape[("rust", "standalone-archive")][
                "sha256"
            ],
            "metaArtifactSha256": None,
            "launcherSourceSha256": None,
        },
        "direct-bun": {
            "runner": "bun",
            "binarySha256": native["products"]["bun"]["sha256"],
            "artifactSha256": artifact_by_shape[("bun", "standalone-archive")][
                "sha256"
            ],
            "metaArtifactSha256": None,
            "launcherSourceSha256": None,
        },
        "npm-launcher": {
            "runner": "bun",
            "binarySha256": native["products"]["bun"]["sha256"],
            "artifactSha256": artifact_by_shape[("bun", "npm-platform")]["sha256"],
            "metaArtifactSha256": artifact_by_shape[("bun", "npm-meta")]["sha256"],
            "launcherSourceSha256": launcher_sha,
        },
    }
    surfaces = report.get("surfaces")
    surface_keys = {
        "runner",
        "binarySha256",
        "artifactSha256",
        "metaArtifactSha256",
        "installationTreeSha256",
        "launcherSourceSha256",
        "launcherCommandIdentity",
        "execution",
    }
    if not isinstance(surfaces, dict) or set(surfaces) != set(
        expected_installation_surfaces
    ):
        raise DraftReleaseError(f"release package surfaces are not canonical: {target}")
    for surface, binding in expected_surface_bindings.items():
        record = surfaces.get(surface)
        if not isinstance(record, dict) or set(record) != surface_keys:
            raise DraftReleaseError(
                f"release package surface shape is not closed: {target}"
            )
        for key, expected in binding.items():
            if record.get(key) != expected:
                raise DraftReleaseError(
                    f"release package surface is not byte-bound: {target}"
                )
        expected_execution = "blocked-before-execution" if windows else "verified-posix"
        if record.get("execution") != expected_execution or record.get(
            "installationTreeSha256"
        ) != (None if windows else installation_trees[surface]):
            raise DraftReleaseError(
                f"release package surface execution is false: {target}"
            )
        command = record.get("launcherCommandIdentity")
        if surface != "npm-launcher" or windows:
            if command is not None:
                raise DraftReleaseError(
                    f"release package surface has an invalid launcher: {target}"
                )
        elif not isinstance(command, dict):
            raise DraftReleaseError(
                f"release package launcher identity is unavailable: {target}"
            )
        elif command.get("kind") == "symlink":
            if (
                set(command)
                != {"kind", "linkTarget", "linkTextSha256", "resolvedLauncherSha256"}
                or not isinstance(command.get("linkTarget"), str)
                or not command["linkTarget"]
                or command["linkTarget"].startswith(("/", "\\"))
                or ".." in Path(command["linkTarget"]).parts
                or not isinstance(command.get("linkTextSha256"), str)
                or SHA256.fullmatch(command["linkTextSha256"]) is None
                or command.get("resolvedLauncherSha256") != launcher_sha
            ):
                raise DraftReleaseError(
                    f"release package symlink launcher is malformed: {target}"
                )
        elif command.get("kind") == "regular-shim":
            if (
                set(command) != {"kind", "sha256"}
                or not isinstance(command.get("sha256"), str)
                or SHA256.fullmatch(command["sha256"]) is None
            ):
                raise DraftReleaseError(
                    f"release package shim launcher is malformed: {target}"
                )
        else:
            raise DraftReleaseError(
                f"release package launcher kind is unsupported: {target}"
            )

    cases = report.get("cases")
    if windows:
        if cases != []:
            raise DraftReleaseError(
                "Windows release package admission claims candidate execution"
            )
    else:
        if not isinstance(cases, list) or len(cases) != len(protected_cases):
            raise DraftReleaseError(
                f"release package invariant corpus is empty: {target}"
            )
        seen_case_ids: set[str] = set()
        help_bytes, _ = _regular_file(CONTROL_RUNNER_HELP, maximum=MAX_EVIDENCE_BYTES)
        for case, protected_case in zip(cases, protected_cases, strict=True):
            if (
                not isinstance(case, dict)
                or set(case) != {"id", "argv", "observations"}
                or not isinstance(case.get("id"), str)
                or not case["id"]
                or case["id"] in seen_case_ids
                or not isinstance(case.get("argv"), list)
                or not all(isinstance(item, str) for item in case["argv"])
                or not isinstance(case.get("observations"), list)
                or len(case["observations"]) != 3
            ):
                raise DraftReleaseError(
                    f"release package invariant case is malformed: {target}"
                )
            if (
                case["id"] != protected_case["id"]
                or case["argv"] != protected_case["argv"]
            ):
                raise DraftReleaseError(
                    f"release package invariant corpus is stale: {target}"
                )
            seen_case_ids.add(case["id"])
            for observation, surface in zip(
                case["observations"], expected_installation_surfaces, strict=True
            ):
                if not isinstance(observation, dict) or set(observation) != {
                    "surface",
                    "exitCode",
                    "stdout",
                    "stderr",
                    "settlement",
                    "settlementAuthority",
                    "projection",
                }:
                    raise DraftReleaseError(
                        f"release package observation is not closed: {target}"
                    )
                runner = "rust" if surface == "direct-rust" else "bun"
                expected_projection = _expected_release_projection(
                    protected_case,
                    runner=runner,
                    version=version,
                    source_sha=source_sha,
                    image=manifest["image"],
                )
                if (
                    observation.get("surface") != surface
                    or observation.get("exitCode") != protected_case["exitCode"]
                    or observation.get("settlement") != "settled"
                    or observation.get("settlementAuthority")
                    != "direct-and-original-process-group-settled"
                    or observation.get("projection") != expected_projection
                ):
                    raise DraftReleaseError(
                        f"release package observation did not pass: {target}"
                    )
                for stream in ("stdout", "stderr"):
                    value = observation.get(stream)
                    if (
                        not isinstance(value, dict)
                        or set(value) != {"byteLength", "sha256"}
                        or not _nonnegative_integer(value.get("byteLength"))
                        or not isinstance(value.get("sha256"), str)
                        or SHA256.fullmatch(value["sha256"]) is None
                    ):
                        raise DraftReleaseError(
                            f"release package observation output is malformed: {target}"
                        )
                if observation["stderr"] != _digest_record(b""):
                    raise DraftReleaseError(
                        f"release package observation wrote stderr: {target}"
                    )
                output_kind = protected_case["stdout"]["kind"]
                if output_kind == "version":
                    expected_stdout = _digest_record(
                        f"prose {version} ({runner})\n".encode("utf-8")
                    )
                    if observation["stdout"] != expected_stdout:
                        raise DraftReleaseError(
                            f"release package version output is not exact: {target}"
                        )
                elif output_kind == "exact-file":
                    if observation["stdout"] != _digest_record(help_bytes):
                        raise DraftReleaseError(
                            f"release package help output is not exact: {target}"
                        )
                elif observation["stdout"]["byteLength"] == 0:
                    raise DraftReleaseError(
                        f"release package JSON output is empty: {target}"
                    )

    expected_claims = {
        "providerCalls": "not-observed",
        "semanticEvaluation": False,
        "programPortabilityEvaluation": False,
        "releaseEligible": False,
        "publicationAuthorized": False,
        "rankingProduced": False,
        "strictDescendantContainment": False,
        "runtimeNetworkIsolation": False,
        "candidateExecution": (
            "blocked-before-execution" if windows else "performed-posix-invariants"
        ),
    }
    if report.get("claims") != expected_claims:
        raise DraftReleaseError(
            f"release package admission overclaims authority: {target}"
        )
    return workflow_identity


def load_alpha_assembly(
    root: Path,
    source_sha: str,
    version: str,
) -> tuple[Asset, ...]:
    """Capture and validate one closed functional-alpha upload assembly."""

    if root.is_symlink() or not root.is_dir():
        raise DraftReleaseError("assembly root must be a non-symlink directory")
    checksum_bytes, checksum_metadata = _regular_file(
        root / "SHA256SUMS", maximum=MAX_ALPHA_CHECKSUM_BYTES
    )
    if b"\r" in checksum_bytes or not checksum_bytes.endswith(b"\n"):
        raise DraftReleaseError("SHA256SUMS is not canonical")
    try:
        checksum_text = checksum_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise DraftReleaseError("SHA256SUMS must be ASCII") from error
    lines = checksum_text.splitlines()
    if not lines:
        raise DraftReleaseError("SHA256SUMS must declare at least one asset")
    declared: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(
            r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,254})", line
        )
        if match is None:
            raise DraftReleaseError("SHA256SUMS contains a malformed or unsafe entry")
        digest, name = match.groups()
        if name == "SHA256SUMS" or name in declared:
            raise DraftReleaseError(
                "SHA256SUMS contains a duplicate or recursive entry"
            )
        if len(declared) >= MAX_ASSEMBLY_ENTRIES:
            raise DraftReleaseError("SHA256SUMS declares too many assembly entries")
        declared[name] = digest
    if list(declared) != sorted(declared):
        raise DraftReleaseError("SHA256SUMS is not canonically ordered")

    try:
        members = list(root.iterdir())
    except OSError as error:
        raise DraftReleaseError("release assembly cannot be enumerated") from error
    if len(members) > MAX_ASSEMBLY_ENTRIES + 1:
        raise DraftReleaseError("release assembly has too many entries")
    if {path.name for path in members} != {"SHA256SUMS", *declared}:
        raise DraftReleaseError("assembly membership does not exactly match SHA256SUMS")

    assets = [
        Asset(
            "SHA256SUMS",
            root / "SHA256SUMS",
            checksum_metadata.st_size,
            hashlib.sha256(checksum_bytes).hexdigest(),
            checksum_bytes,
        )
    ]
    assets_by_name: dict[str, Asset] = {}
    aggregate_size = checksum_metadata.st_size
    for name, expected_digest in declared.items():
        value, metadata = _regular_file(root / name, maximum=MAX_ALPHA_ASSET_BYTES)
        aggregate_size += metadata.st_size
        if aggregate_size > MAX_ASSEMBLY_BYTES:
            raise DraftReleaseError("release assembly exceeds the aggregate size limit")
        observed_digest = hashlib.sha256(value).hexdigest()
        if observed_digest != expected_digest:
            raise DraftReleaseError(f"assembly digest mismatch: {name}")
        asset = Asset(name, root / name, metadata.st_size, observed_digest, value)
        assets.append(asset)
        assets_by_name[name] = asset

    evidence_suffixes = (
        "release-manifest.json",
        "sbom.cdx.json",
        "provenance.json",
        "dependency-evidence.json",
        "SHA256SUMS",
        "alpha-admission.json",
    )
    required = {
        f"{target}-{suffix}"
        for target in ALPHA_TARGET_PLATFORMS
        for suffix in evidence_suffixes
    }
    artifact_names: set[str] = set()
    adapter_manifest = _alpha_adapter_manifest_identity()
    for target, platform in ALPHA_TARGET_PLATFORMS.items():
        manifest_name = f"{target}-release-manifest.json"
        if manifest_name not in assets_by_name:
            raise DraftReleaseError(
                f"alpha assembly is missing release manifest: {target}"
            )
        manifest = _json_evidence(
            assets_by_name[manifest_name].body, f"alpha release manifest {target}"
        )
        if (
            manifest.get("schema") != "openprose.local-release-manifest/1"
            or manifest.get("mode") != "alpha"
            or manifest.get("platform") != platform
            or manifest.get("version") != version
            or manifest.get("releaseEligible") is not False
            or manifest.get("publicationAuthorized") is not False
            or manifest.get("source", {}).get("revision") != source_sha
        ):
            raise DraftReleaseError(
                f"alpha release manifest is not source-bound and draft-safe: {target}"
            )
        records = manifest.get("artifacts")
        if not isinstance(records, list) or not records or len(records) > 16:
            raise DraftReleaseError(f"alpha artifact inventory is malformed: {target}")
        for record in records:
            if not isinstance(record, dict):
                raise DraftReleaseError(f"alpha artifact record is malformed: {target}")
            name = record.get("path")
            asset = assets_by_name.get(name) if isinstance(name, str) else None
            if (
                asset is None
                or record.get("byteLength") != asset.byte_length
                or record.get("sha256") != asset.sha256
            ):
                raise DraftReleaseError(
                    f"alpha artifact is absent or not digest-bound: {target}"
                )
            artifact_names.add(name)

        admission_name = f"{target}-alpha-admission.json"
        if admission_name not in assets_by_name:
            raise DraftReleaseError(f"alpha assembly is missing admission: {target}")
        admission = _json_evidence(
            assets_by_name[admission_name].body, f"alpha package admission {target}"
        )
        _validate_alpha_admission_authority(
            admission,
            assets_by_name[admission_name].body,
            target,
            adapter_manifest,
        )
        if (
            admission.get("schema") != "openprose.alpha-package-admission/1"
            or admission.get("status") != "passed-provider-free-functional-alpha"
            or admission.get("targetId") != target
            or admission.get("version") != version
            or admission.get("sourceSha") != source_sha
        ):
            raise DraftReleaseError(f"alpha package admission is not exact: {target}")
        _deep_validate_alpha_admission(
            admission=admission,
            target=target,
            platform=platform,
            version=version,
            source_sha=source_sha,
            manifest=manifest,
            assets_by_name=assets_by_name,
            expected_adapter_manifest=adapter_manifest,
        )

    if set(declared) != required | artifact_names:
        raise DraftReleaseError(
            "alpha assembly inventory is not the exact admitted set"
        )
    if _alpha_adapter_manifest_identity() != adapter_manifest:
        raise DraftReleaseError(
            "functional-alpha adapter authority changed during draft admission"
        )
    return tuple(assets)


def _capture_release_notes(path: Path) -> str:
    encoded, _ = _regular_file(path, maximum=MAX_RELEASE_NOTES_BYTES)
    if b"\r" in encoded or not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise DraftReleaseError("release notes are not canonical UTF-8 text")
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DraftReleaseError("release notes are not canonical UTF-8 text") from error


def load_assembly(
    root: Path,
    source_sha: str,
    version: str,
    repository: str,
    control_sha: str,
) -> tuple[Asset, ...]:
    """Validate a closed release assembly and return its immutable asset inventory."""
    if root.is_symlink() or not root.is_dir():
        raise DraftReleaseError("assembly root must be a non-symlink directory")
    checksum_bytes, checksum_metadata = _regular_file(
        root / "SHA256SUMS", maximum=4 * 1024 * 1024
    )
    try:
        checksum_text = checksum_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise DraftReleaseError("SHA256SUMS must be ASCII") from error
    declared: dict[str, str] = {}
    for line in checksum_text.splitlines():
        match = re.fullmatch(
            r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,254})", line
        )
        if match is None:
            raise DraftReleaseError("SHA256SUMS contains a malformed or unsafe entry")
        digest, name = match.groups()
        if name == "SHA256SUMS" or name in declared:
            raise DraftReleaseError(
                "SHA256SUMS contains a duplicate or recursive entry"
            )
        if len(declared) >= MAX_ASSEMBLY_ENTRIES:
            raise DraftReleaseError("SHA256SUMS declares too many assembly entries")
        declared[name] = digest
    observed: set[str] = set()
    aggregate_size = checksum_metadata.st_size
    for index, path in enumerate(root.iterdir()):
        if index > MAX_ASSEMBLY_ENTRIES:
            raise DraftReleaseError("release assembly has too many entries")
        observed.add(path.name)
        if path.name != "SHA256SUMS":
            aggregate_size += path.lstat().st_size
    if observed != {"SHA256SUMS", *declared}:
        raise DraftReleaseError("assembly membership does not exactly match SHA256SUMS")
    if aggregate_size > MAX_ASSEMBLY_BYTES:
        raise DraftReleaseError("release assembly exceeds the aggregate size limit")
    required = {
        "profile-admission.json",
        "profile-preflight.json",
        "protected-dependency-evidence.json",
        *PROTECTED_AUTHORITY_FILES,
    }
    windows_host_authority: dict[str, Any] | None = None
    dependency_evidence_authority: bytes | None = None
    artifact_inventory: set[str] = set()
    verification_digests: dict[str, str] = {}
    package_admission_workflow: tuple[int, int] | None = None
    release_image_authority: dict[str, Any] | None = None
    release_gate_authority: dict[str, Any] | None = None
    for target in TARGETS:
        required.update(
            {
                f"{target}-release-manifest.json",
                f"{target}-sbom.cdx.json",
                f"{target}-provenance.json",
                f"{target}-dependency-evidence.json",
                f"{target}-SHA256SUMS",
                f"{target}-native-manifest.json",
                f"{target}-verification.json",
                f"{target}-release-package-admission.json",
            }
        )
    missing = sorted(required - declared.keys())
    if missing:
        raise DraftReleaseError(
            f"assembly is missing required release evidence: {missing}"
        )
    checksum_digest = hashlib.sha256(checksum_bytes).hexdigest()
    assets: list[Asset] = [
        Asset(
            "SHA256SUMS",
            root / "SHA256SUMS",
            checksum_metadata.st_size,
            checksum_digest,
            checksum_bytes,
        )
    ]
    assets_by_name: dict[str, Asset] = {}
    captured: dict[str, bytes] = {}
    for name, expected in sorted(declared.items()):
        value, metadata = _regular_file(
            root / name,
            maximum=MAX_EVIDENCE_BYTES if name in required else MAX_ASSET_BYTES,
        )
        observed_digest = hashlib.sha256(value).hexdigest()
        if observed_digest != expected:
            raise DraftReleaseError(f"assembly digest mismatch: {name}")
        asset = Asset(name, root / name, metadata.st_size, observed_digest, value)
        assets.append(asset)
        assets_by_name[name] = asset
        if name in required:
            captured[name] = value
    for target in TARGETS:
        manifest_name = f"{target}-release-manifest.json"
        manifest = _json_evidence(captured[manifest_name], f"release manifest {target}")
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != "openprose.local-release-manifest/1"
            or manifest.get("mode") != "release"
            or manifest.get("platform") != TARGET_PLATFORMS[target]
            or manifest.get("version") != version
            or manifest.get("publicationAuthorized") is not False
            or manifest.get("source", {}).get("revision") != source_sha
        ):
            raise DraftReleaseError(
                f"release manifest is not bound and draft-safe: {target}"
            )
        manifest_image = manifest.get("image")
        if not isinstance(manifest_image, dict):
            raise DraftReleaseError(f"release manifest image is malformed: {target}")
        linux_runtime = _linux_runtime_identity(
            manifest.get("linuxRuntime"), TARGET_PLATFORMS[target], "release manifest"
        )
        bun_runtime = _bun_runtime_identity(
            manifest.get("bunRuntime"), TARGET_PLATFORMS[target], "release manifest"
        )
        if release_image_authority is None:
            release_image_authority = manifest_image
        elif manifest_image != release_image_authority:
            raise DraftReleaseError(
                "release manifest image identity diverges across targets"
            )
        manifest_gates = manifest.get("externalGates")
        if not isinstance(manifest_gates, dict):
            raise DraftReleaseError(f"release manifest gates are malformed: {target}")
        if release_gate_authority is None:
            release_gate_authority = manifest_gates
        elif manifest_gates != release_gate_authority:
            raise DraftReleaseError("release manifest gates diverge across targets")
        artifact_records = manifest.get("artifacts")
        if not isinstance(artifact_records, list) or len(artifact_records) != 4:
            raise DraftReleaseError(
                f"release manifest has the wrong artifact inventory: {target}"
            )
        record_shapes: set[tuple[str, str, str | None]] = set()
        record_names_by_shape: dict[tuple[str, str, str | None], str] = {}
        target_artifact_names: set[str] = set()
        for record in artifact_records:
            if not isinstance(record, dict):
                raise DraftReleaseError(
                    f"release manifest artifact is malformed: {target}"
                )
            name = record.get("path")
            digest = record.get("sha256")
            length = record.get("byteLength")
            implementation = record.get("implementation")
            kind = record.get("kind")
            platform = record.get("platform")
            if (
                not isinstance(name, str)
                or name not in assets_by_name
                or not isinstance(digest, str)
                or SHA256.fullmatch(digest) is None
                or not isinstance(length, int)
                or length <= 0
                or assets_by_name[name].sha256 != digest
                or assets_by_name[name].byte_length != length
            ):
                raise DraftReleaseError(
                    f"release artifact is absent or not digest-bound: {target}"
                )
            if (
                not isinstance(implementation, str)
                or not isinstance(kind, str)
                or platform not in {None, TARGET_PLATFORMS[target]}
            ):
                raise DraftReleaseError(
                    f"release artifact classification is malformed: {target}"
                )
            shape = (implementation, kind, platform)
            record_shapes.add(shape)
            record_names_by_shape[shape] = name
            target_artifact_names.add(name)
        expected_shapes = {
            ("rust", "standalone-archive", TARGET_PLATFORMS[target]),
            ("bun", "standalone-archive", TARGET_PLATFORMS[target]),
            ("bun", "npm-meta", None),
            ("bun", "npm-platform", TARGET_PLATFORMS[target]),
        }
        if record_shapes != expected_shapes or len(target_artifact_names) != 4:
            raise DraftReleaseError(
                f"release artifact inventory is duplicated or not closed: {target}"
            )
        platform_identifier = TARGET_PLATFORMS[target]
        expected_names_by_shape = {
            (
                "rust",
                "standalone-archive",
                platform_identifier,
            ): f"openprose-prose-cli-rust-{version}-{platform_identifier}.tar.gz",
            (
                "bun",
                "standalone-archive",
                platform_identifier,
            ): f"openprose-prose-cli-bun-{version}-{platform_identifier}.tar.gz",
            ("bun", "npm-meta", None): f"openprose-prose-cli-{version}.tgz",
            (
                "bun",
                "npm-platform",
                platform_identifier,
            ): f"openprose-prose-cli-{platform_identifier}-{version}.tgz",
        }
        if record_names_by_shape != expected_names_by_shape:
            raise DraftReleaseError(
                f"release artifact filenames are not canonical: {target}"
            )
        artifact_inventory.update(target_artifact_names)
        native = _json_evidence(
            captured[f"{target}-native-manifest.json"], f"native manifest {target}"
        )
        verification = _json_evidence(
            captured[f"{target}-verification.json"], f"native verification {target}"
        )
        native_bytes = captured[f"{target}-native-manifest.json"]
        if (
            not isinstance(native, dict)
            or native.get("schema") != "openprose.native-build/1"
            or native.get("target") != target
            or native.get("sourceSha") != source_sha
            or native.get("build") != {"profile": "release", "testSeamsEnabled": False}
            or set(native.get("products", {})) != {"rust", "bun"}
        ):
            raise DraftReleaseError(f"native manifest is not candidate-bound: {target}")
        native_host = _windows_host_identity(
            native.get("windowsProcessHost"), target, "native manifest"
        )
        if target == "win-x64":
            windows_host_authority = native_host
        native_windows_admission = native.get("windowsJobObjectReleaseAdmission")
        if native_windows_admission is not False:
            raise DraftReleaseError(
                f"native Windows admission is not draft-safe: {target}"
            )
        for product, record in native["products"].items():
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("path"), str)
                or not isinstance(record.get("sha256"), str)
                or SHA256.fullmatch(record["sha256"]) is None
                or not isinstance(record.get("byteLength"), int)
                or record["byteLength"] <= 0
            ):
                raise DraftReleaseError(
                    f"native product identity is malformed: {target}/{product}"
                )
        verification_valid = (
            isinstance(verification, dict)
            and verification.get("schema") == "openprose.native-verification/1"
            and verification.get("target") == target
            and verification.get("sourceSha") == source_sha
            and verification.get("nativeManifestSha256")
            == hashlib.sha256(native_bytes).hexdigest()
            and verification.get("windowsJobObjectReleaseAdmission")
            == native_windows_admission
            and verification.get("windowsProcessHost") == native_host
            and set(verification.get("reports", {})) == {"rust", "bun"}
        )
        if not verification_valid:
            raise DraftReleaseError(
                f"native verification is not manifest-bound: {target}"
            )
        verification_artifact = verification.get("nativeArtifact")
        expected_native_files = {
            "native-manifest.json": _digest_record(native_bytes),
            native["products"]["rust"]["path"]: {
                "byteLength": native["products"]["rust"]["byteLength"],
                "sha256": native["products"]["rust"]["sha256"],
            },
            native["products"]["bun"]["path"]: {
                "byteLength": native["products"]["bun"]["byteLength"],
                "sha256": native["products"]["bun"]["sha256"],
            },
        }
        if native_host is not None:
            expected_native_files[native_host["path"]] = {
                "byteLength": native_host["byteLength"],
                "sha256": native_host["sha256"],
            }
        if (
            not isinstance(verification_artifact, dict)
            or set(verification_artifact)
            != {"name", "workflowRunId", "workflowRunAttempt", "files"}
            or verification_artifact.get("name") != f"native-build-{target}"
            or not _positive_integer(verification_artifact.get("workflowRunId"))
            or not _positive_integer(verification_artifact.get("workflowRunAttempt"))
            or verification_artifact.get("files") != expected_native_files
        ):
            raise DraftReleaseError(
                f"native verification file map is not byte-bound: {target}"
            )
        verification_digests[target] = hashlib.sha256(
            captured[f"{target}-verification.json"]
        ).hexdigest()
        release_host = manifest.get("windowsProcessHost")
        if target == "win-x64":
            if release_host != native_host:
                raise DraftReleaseError(
                    "Windows package host is not native-manifest-bound"
                )
        elif release_host != "not-applicable":
            raise DraftReleaseError(
                f"non-Windows package claims a Windows host: {target}"
            )
        if manifest.get("windowsJobObjectReleaseAdmission") is not False:
            raise DraftReleaseError(
                f"release manifest Windows admission is not draft-safe: {target}"
            )
        for record in artifact_records:
            archive_bytes = _recapture_asset(assets_by_name[record["path"]])
            expected_binary = None
            if record["kind"] == "standalone-archive":
                expected_binary = native["products"][record["implementation"]]
            elif record["kind"] == "npm-platform":
                expected_binary = native["products"]["bun"]
            _inspect_package_archive(
                encoded=archive_bytes,
                name=record["path"],
                kind=record["kind"],
                implementation=record["implementation"],
                platform=TARGET_PLATFORMS[target],
                version=version,
                source_sha=source_sha,
                image=manifest.get("image"),
                expected_binary=expected_binary,
                windows_host=native_host,
                linux_runtime=linux_runtime,
            )
        package_admission = _json_evidence(
            captured[f"{target}-release-package-admission.json"],
            f"release package admission {target}",
        )
        target_package_admission_workflow = _validate_release_package_admission(
            report=package_admission,
            target=target,
            source_sha=source_sha,
            control_sha=control_sha,
            version=version,
            captured=captured,
            assets_by_name=assets_by_name,
            manifest=manifest,
            native=native,
            verification=verification,
        )
        if package_admission_workflow is None:
            package_admission_workflow = target_package_admission_workflow
        elif package_admission_workflow != target_package_admission_workflow:
            raise DraftReleaseError(
                "release package admissions come from different workflow runs"
            )
        _validate_supply_chain_host(
            captured=captured,
            target=target,
            windows_host=native_host,
            bun_runtime=bun_runtime,
        )
        dependency_bytes = _validate_dependency_evidence(
            captured=captured,
            target=target,
            manifest=manifest,
        )
        if dependency_evidence_authority is None:
            dependency_evidence_authority = dependency_bytes
        elif dependency_bytes != dependency_evidence_authority:
            raise DraftReleaseError("per-target dependency evidence diverges")
    protected_dependency_bytes = captured["protected-dependency-evidence.json"]
    if (
        dependency_evidence_authority is None
        or protected_dependency_bytes != dependency_evidence_authority
    ):
        raise DraftReleaseError(
            "per-target dependency evidence is not byte-bound to protected evidence"
        )
    if set(declared) != required | artifact_inventory:
        raise DraftReleaseError("assembly contains an undeclared release asset class")
    if release_image_authority is None:
        raise DraftReleaseError("release image authority is unavailable")
    if release_gate_authority is None:
        raise DraftReleaseError("release gate authority is unavailable")
    _validate_protected_authority(
        captured=captured,
        repository=repository,
        source_sha=source_sha,
        control_sha=control_sha,
        version=version,
        package_image=release_image_authority,
        package_gates=release_gate_authority,
    )
    admission = _json_evidence(captured["profile-admission.json"], "profile admission")
    admission_keys = {
        "schema",
        "status",
        "sourceSha",
        "publicationAuthorized",
        "windowsJobObjectReleaseAdmission",
        "windowsProcessHost",
        "preflightSha256",
        "verifiedProfiles",
        "dependencyEvidence",
    }
    if not isinstance(admission, dict) or set(admission) != admission_keys:
        raise DraftReleaseError("profile admission shape is not closed")
    expected_admission = {
        "schema": "openprose.protected-profile-admission/1",
        "status": "draft-packaging-only",
        "sourceSha": source_sha,
        "publicationAuthorized": False,
        "windowsJobObjectReleaseAdmission": False,
    }
    for key, expected in expected_admission.items():
        if admission.get(key) != expected:
            raise DraftReleaseError(f"profile admission field is not draft-safe: {key}")
    if (
        windows_host_authority is None
        or admission.get("windowsProcessHost") != windows_host_authority
    ):
        raise DraftReleaseError(
            "profile admission is not bound to the exact Windows process host"
        )
    if (
        admission.get("preflightSha256")
        != hashlib.sha256(captured["profile-preflight.json"]).hexdigest()
    ):
        raise DraftReleaseError("profile admission is not bound to the exact preflight")
    if admission.get("verifiedProfiles") != verification_digests:
        raise DraftReleaseError(
            "profile admission is not bound to every verification report"
        )
    if admission.get("dependencyEvidence") != {
        "path": "protected-dependency-evidence.json",
        "byteLength": len(protected_dependency_bytes),
        "sha256": hashlib.sha256(protected_dependency_bytes).hexdigest(),
        "releasePolicyPassed": False,
    }:
        raise DraftReleaseError(
            "profile admission is not bound to the protected dependency evidence"
        )
    return tuple(assets)


def _response_bytes(
    request: Request,
    *,
    expected_statuses: frozenset[int],
    opener: Callable[..., Any],
) -> tuple[int, bytes]:
    try:
        with opener(request, timeout=60) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        try:
            error.close()
        except Exception:
            pass
        if not isinstance(status, int) or isinstance(status, bool):
            raise DraftReleaseError(
                "GitHub API returned a malformed HTTP status"
            ) from None
        raise DraftReleaseError(
            f"GitHub API returned unexpected status: {status}"
        ) from None
    except (URLError, OSError):
        raise DraftReleaseError("GitHub API request failed before a response") from None
    except Exception:
        raise DraftReleaseError("GitHub API response transport is malformed") from None
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or not isinstance(body, bytes)
    ):
        raise DraftReleaseError("GitHub API response transport is malformed")
    if status not in expected_statuses or len(body) > MAX_RESPONSE_BYTES:
        raise DraftReleaseError(
            f"GitHub API returned unexpected status or response size: {status}"
        )
    return status, body


def _decode_json_response(body: bytes) -> Any:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DraftReleaseError("GitHub API response is not valid JSON") from None
    return value


def _json_request(
    request: Request,
    *,
    expected_status: int,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    _, body = _response_bytes(
        request,
        expected_statuses=frozenset({expected_status}),
        opener=opener,
    )
    value = _decode_json_response(body)
    if not isinstance(value, dict):
        raise DraftReleaseError("GitHub API response must be an object")
    return value


def _json_array_request(
    request: Request,
    *,
    opener: Callable[..., Any],
) -> list[Any]:
    _, body = _response_bytes(
        request,
        expected_statuses=frozenset({200}),
        opener=opener,
    )
    value = _decode_json_response(body)
    if not isinstance(value, list):
        raise DraftReleaseError("GitHub API response must be an array")
    return value


def _git_object(
    value: Any,
    *,
    repository: str,
    boundary: str,
) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"type", "sha", "url"}:
        raise DraftReleaseError(f"{boundary} Git object shape is not closed")
    object_type = value.get("type")
    sha = value.get("sha")
    if object_type not in {"commit", "tag"} or not isinstance(sha, str):
        raise DraftReleaseError(f"{boundary} Git object identity is malformed")
    if FULL_SHA.fullmatch(sha) is None:
        raise DraftReleaseError(f"{boundary} Git object SHA is malformed")
    expected_url = (
        f"https://api.github.com/repos/{repository}/git/"
        f"{'commits' if object_type == 'commit' else 'tags'}/{sha}"
    )
    if value.get("url") != expected_url:
        raise DraftReleaseError(f"{boundary} Git object URL is not exact")
    return object_type, sha


def _resolve_existing_tag(
    *,
    repository: str,
    tag: str,
    source_sha: str,
    headers: dict[str, str],
    opener: Callable[..., Any],
) -> None:
    """Require an existing release tag and peel it to one exact commit."""

    encoded_tag = quote(tag, safe="")
    ref_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{encoded_tag}"
    ref = _json_request(
        Request(ref_url, headers=headers, method="GET"),
        expected_status=200,
        opener=opener,
    )
    if set(ref) != {"ref", "node_id", "url", "object"}:
        raise DraftReleaseError("release tag reference shape is not closed")
    if (
        ref.get("ref") != f"refs/tags/{tag}"
        or not isinstance(ref.get("node_id"), str)
        or not ref["node_id"]
        or ref.get("url")
        != f"https://api.github.com/repos/{repository}/git/refs/tags/{encoded_tag}"
    ):
        raise DraftReleaseError("release tag reference identity is not exact")
    object_type, object_sha = _git_object(
        ref.get("object"),
        repository=repository,
        boundary="release tag reference",
    )
    seen: set[str] = set()
    depth = 0
    while object_type == "tag":
        if depth >= MAX_TAG_PEEL_DEPTH:
            raise DraftReleaseError("release tag exceeds the closed peel-depth limit")
        if object_sha in seen:
            raise DraftReleaseError("release tag object chain is cyclic")
        seen.add(object_sha)
        tag_url = f"https://api.github.com/repos/{repository}/git/tags/{object_sha}"
        annotated = _json_request(
            Request(tag_url, headers=headers, method="GET"),
            expected_status=200,
            opener=opener,
        )
        if set(annotated) != {
            "node_id",
            "tag",
            "sha",
            "url",
            "message",
            "tagger",
            "object",
            "verification",
        }:
            raise DraftReleaseError("annotated release tag shape is not closed")
        tagger = annotated.get("tagger")
        verification = annotated.get("verification")
        if (
            not isinstance(annotated.get("node_id"), str)
            or not annotated["node_id"]
            or not isinstance(annotated.get("tag"), str)
            or not annotated["tag"]
            or annotated.get("sha") != object_sha
            or annotated.get("url") != tag_url
            or not isinstance(annotated.get("message"), str)
            or not isinstance(tagger, dict)
            or set(tagger) != {"name", "email", "date"}
            or any(not isinstance(tagger[key], str) for key in tagger)
            or not isinstance(verification, dict)
            or set(verification)
            != {"verified", "reason", "signature", "payload", "verified_at"}
            or not isinstance(verification.get("verified"), bool)
            or not isinstance(verification.get("reason"), str)
            or any(
                verification.get(key) is not None
                and not isinstance(verification[key], str)
                for key in ("signature", "payload", "verified_at")
            )
        ):
            raise DraftReleaseError("annotated release tag identity is malformed")
        object_type, object_sha = _git_object(
            annotated.get("object"),
            repository=repository,
            boundary="annotated release tag",
        )
        depth += 1
    if object_sha != source_sha:
        raise DraftReleaseError(
            "release tag does not resolve to the requested source SHA"
        )


def _draft_identity(
    value: dict[str, Any],
    *,
    repository: str,
    tag: str,
    release_name: str,
    release_notes: str,
    prerelease: bool,
) -> DraftIdentity:
    release_id = value.get("id")
    if (
        not isinstance(release_id, int)
        or isinstance(release_id, bool)
        or release_id <= 0
    ):
        raise DraftReleaseError("GitHub draft release ID is malformed")
    encoded_tag = quote(tag, safe="")
    release_url = f"https://api.github.com/repos/{repository}/releases/{release_id}"
    upload_template = (
        f"https://uploads.github.com/repos/{repository}/releases/"
        f"{release_id}/assets{{?name,label}}"
    )
    expected_identity = {
        "url": release_url,
        "assets_url": f"{release_url}/assets",
        "upload_url": upload_template,
        "html_url": f"https://github.com/{repository}/releases/tag/{encoded_tag}",
        "tag_name": tag,
        "name": release_name,
        "body": release_notes,
        "draft": True,
        "prerelease": prerelease,
        "immutable": False,
    }
    if any(value.get(key) != expected for key, expected in expected_identity.items()):
        raise DraftReleaseError("GitHub draft release identity is not exact")
    # GitHub documents target_commitish as ignored when the tag already exists;
    # the protected Git ref peel before and after reconciliation is authoritative.
    if (
        not isinstance(value.get("target_commitish"), str)
        or not value["target_commitish"]
    ):
        raise DraftReleaseError("GitHub draft target commitish is malformed")
    if not isinstance(value.get("node_id"), str) or not value["node_id"]:
        raise DraftReleaseError("GitHub draft release node identity is malformed")
    return DraftIdentity(
        release_id=release_id, upload_base=upload_template.split("{", 1)[0]
    )


def _discover_draft(
    *,
    repository: str,
    tag: str,
    release_name: str,
    release_notes: str,
    prerelease: bool,
    headers: dict[str, str],
    opener: Callable[..., Any],
) -> DraftIdentity | None:
    matches: list[dict[str, Any]] = []
    for page in range(1, MAX_RELEASE_DISCOVERY_PAGES + 1):
        query = urlencode({"per_page": MAX_RELEASES_PER_PAGE, "page": page})
        values = _json_array_request(
            Request(
                f"https://api.github.com/repos/{repository}/releases?{query}",
                headers=headers,
                method="GET",
            ),
            opener=opener,
        )
        if len(values) > MAX_RELEASES_PER_PAGE:
            raise DraftReleaseError(
                "GitHub release discovery page exceeds the closed limit"
            )
        for value in values:
            if not isinstance(value, dict) or not isinstance(
                value.get("tag_name"), str
            ):
                raise DraftReleaseError(
                    "GitHub release discovery identity is malformed"
                )
            if value["tag_name"] == tag:
                matches.append(value)
                if len(matches) > 1:
                    raise DraftReleaseError(
                        "GitHub release discovery is ambiguous for the tag"
                    )
        if len(values) < MAX_RELEASES_PER_PAGE:
            if not matches:
                return None
            return _draft_identity(
                matches[0],
                repository=repository,
                tag=tag,
                release_name=release_name,
                release_notes=release_notes,
                prerelease=prerelease,
            )
    raise DraftReleaseError(
        "GitHub release discovery exceeds the closed pagination limit"
    )


def _validate_remote_asset(
    value: Any,
    *,
    repository: str,
    expected: dict[str, Asset],
) -> str:
    if not isinstance(value, dict):
        raise DraftReleaseError("GitHub release asset identity is malformed")
    name = value.get("name")
    if not isinstance(name, str) or name not in expected:
        raise DraftReleaseError("GitHub draft contains an unexpected release asset")
    asset = expected[name]
    asset_id = value.get("id")
    if (
        not isinstance(asset_id, int)
        or isinstance(asset_id, bool)
        or asset_id <= 0
        or value.get("url")
        != f"https://api.github.com/repos/{repository}/releases/assets/{asset_id}"
        or not isinstance(value.get("node_id"), str)
        or not value["node_id"]
        or value.get("state") != "uploaded"
        or value.get("content_type") != "application/octet-stream"
        or value.get("size") != asset.byte_length
        or value.get("digest") != f"sha256:{asset.sha256}"
    ):
        raise DraftReleaseError(
            "GitHub release asset does not match the admitted bytes"
        )
    return name


def _list_exact_assets(
    *,
    repository: str,
    release_id: int,
    assets: tuple[Asset, ...],
    headers: dict[str, str],
    opener: Callable[..., Any],
) -> dict[str, dict[str, Any]]:
    expected = {asset.name: asset for asset in assets}
    observed: dict[str, dict[str, Any]] = {}
    observed_ids: set[int] = set()
    maximum_pages = len(expected) // MAX_RELEASE_ASSETS_PER_PAGE + 2
    for page in range(1, maximum_pages + 1):
        query = urlencode({"per_page": MAX_RELEASE_ASSETS_PER_PAGE, "page": page})
        values = _json_array_request(
            Request(
                f"https://api.github.com/repos/{repository}/releases/"
                f"{release_id}/assets?{query}",
                headers=headers,
                method="GET",
            ),
            opener=opener,
        )
        if len(values) > MAX_RELEASE_ASSETS_PER_PAGE:
            raise DraftReleaseError(
                "GitHub release asset page exceeds the closed limit"
            )
        for value in values:
            name = _validate_remote_asset(
                value, repository=repository, expected=expected
            )
            if name in observed:
                raise DraftReleaseError(
                    "GitHub draft contains a duplicate release asset"
                )
            asset_id = value["id"]
            if asset_id in observed_ids:
                raise DraftReleaseError("GitHub draft contains a duplicate asset ID")
            observed_ids.add(asset_id)
            observed[name] = value
        if len(observed) > len(expected):
            raise DraftReleaseError("GitHub draft contains too many release assets")
        if len(values) < MAX_RELEASE_ASSETS_PER_PAGE:
            return observed
    raise DraftReleaseError("GitHub release asset pagination exceeds the closed limit")


def create_draft_release(
    *,
    repository: str,
    version: str,
    source_sha: str,
    control_sha: str,
    token: str,
    assembly: Path,
    release_kind: str = "full",
    release_notes_path: Path | None = None,
    workflow_run_id: int | None = None,
    workflow_run_attempt: int | None = None,
    authority_output: Path | None = None,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Create or exactly resume a draft and reconcile one closed assembly."""
    if SEMVER.fullmatch(version) is None:
        raise DraftReleaseError("version must be exact SemVer")
    if FULL_SHA.fullmatch(source_sha) is None:
        raise DraftReleaseError("source SHA must be full lowercase hexadecimal")
    if FULL_SHA.fullmatch(control_sha) is None:
        raise DraftReleaseError("control SHA must be full lowercase hexadecimal")
    if REPOSITORY.fullmatch(repository) is None:
        raise DraftReleaseError("GitHub repository must be owner/name")
    if not token or "\r" in token or "\n" in token:
        raise DraftReleaseError("GitHub token is unavailable or malformed")
    if release_kind == "full":
        if any(
            value is not None
            for value in (workflow_run_id, workflow_run_attempt, authority_output)
        ):
            raise DraftReleaseError(
                "functional-alpha handoff inputs cannot be used for a full release"
            )
        if release_notes_path is not None:
            raise DraftReleaseError(
                "full release notes must be rendered from the assembly"
            )
        assets = load_assembly(assembly, source_sha, version, repository, control_sha)
        release_notes = _release_notes(assets)
        tag = f"openprose-cli-v{version}"
        release_name = f"OpenProse CLI v{version}"
        prerelease = False
    elif release_kind == "functional-alpha":
        run_id = _require_positive_integer(workflow_run_id, "workflow run ID")
        run_attempt = _require_positive_integer(
            workflow_run_attempt, "workflow run attempt"
        )
        handoff_path = _authority_output_path(authority_output)
        if ALPHA_SEMVER.fullmatch(version) is None:
            raise DraftReleaseError(
                "functional-alpha version must be numbered alpha SemVer"
            )
        if release_notes_path is None:
            raise DraftReleaseError("functional-alpha release notes are required")
        assets = load_alpha_assembly(assembly, source_sha, version)
        release_notes = _capture_release_notes(release_notes_path)
        canonical_release_notes = _functional_alpha_release_notes(version)
        if release_notes != canonical_release_notes:
            raise DraftReleaseError(
                "functional-alpha release notes differ from canonical authority"
            )
        tag = f"cli-v{version}"
        release_name = f"OpenProse CLI v{version} functional alpha"
        prerelease = True
    else:
        raise DraftReleaseError("release kind is unsupported")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "openprose-cli-draft-release/1",
        "X-GitHub-Api-Version": API_VERSION,
    }
    _resolve_existing_tag(
        repository=repository,
        tag=tag,
        source_sha=source_sha,
        headers=headers,
        opener=opener,
    )
    identity = _discover_draft(
        repository=repository,
        tag=tag,
        release_name=release_name,
        release_notes=release_notes,
        prerelease=prerelease,
        headers=headers,
        opener=opener,
    )
    resumed = identity is not None
    payload = json.dumps(
        {
            "tag_name": tag,
            # GitHub ignores this while the tag exists. If it is concurrently
            # deleted, this is not a branch/commit from which to recreate it.
            "target_commitish": f"refs/tags/{tag}",
            "name": release_name,
            "body": release_notes,
            "draft": True,
            "prerelease": prerelease,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if identity is None:
        release = _json_request(
            Request(
                f"https://api.github.com/repos/{repository}/releases",
                data=payload,
                headers=headers,
                method="POST",
            ),
            expected_status=201,
            opener=opener,
        )
        identity = _draft_identity(
            release,
            repository=repository,
            tag=tag,
            release_name=release_name,
            release_notes=release_notes,
            prerelease=prerelease,
        )

    existing = _list_exact_assets(
        repository=repository,
        release_id=identity.release_id,
        assets=assets,
        headers=headers,
        opener=opener,
    )
    uploaded_names: set[str] = set()
    expected = {asset.name: asset for asset in assets}
    for asset in assets:
        if asset.name in existing:
            continue
        body = _recapture_asset(asset)
        upload = _json_request(
            Request(
                f"{identity.upload_base}?{urlencode({'name': asset.name})}",
                data=body,
                headers={**headers, "Content-Type": "application/octet-stream"},
                method="POST",
            ),
            expected_status=201,
            opener=opener,
        )
        uploaded_name = _validate_remote_asset(
            upload, repository=repository, expected=expected
        )
        if uploaded_name != asset.name:
            raise DraftReleaseError("GitHub returned the wrong uploaded asset identity")
        uploaded_names.add(asset.name)

    final_assets = _list_exact_assets(
        repository=repository,
        release_id=identity.release_id,
        assets=assets,
        headers=headers,
        opener=opener,
    )
    if set(final_assets) != set(expected):
        raise DraftReleaseError(
            "GitHub draft does not contain the exact admitted asset set"
        )
    final_identity = _discover_draft(
        repository=repository,
        tag=tag,
        release_name=release_name,
        release_notes=release_notes,
        prerelease=prerelease,
        headers=headers,
        opener=opener,
    )
    if final_identity is None or final_identity != identity:
        raise DraftReleaseError("GitHub draft release identity changed")
    _resolve_existing_tag(
        repository=repository,
        tag=tag,
        source_sha=source_sha,
        headers=headers,
        opener=opener,
    )
    if release_kind == "functional-alpha":
        authority = _alpha_draft_authority(
            repository=repository,
            version=version,
            source_sha=source_sha,
            control_sha=control_sha,
            tag=tag,
            release_id=identity.release_id,
            release_notes=release_notes,
            assets=assets,
            workflow_run_id=run_id,
            workflow_run_attempt=run_attempt,
            resumed=resumed,
        )
        _write_alpha_draft_authority(handoff_path, authority)
    return {
        "schema": "openprose.draft-release-result/2",
        "draft": True,
        "prerelease": prerelease,
        "publicationAuthorized": False,
        "tag": tag,
        "sourceSha": source_sha,
        "releaseId": identity.release_id,
        "resumed": resumed,
        "assetCount": len(assets),
        "uploadedAssetCount": len(uploaded_names),
        "reusedAssetCount": len(existing),
        "assets": [
            {
                "name": asset.name,
                "sha256": asset.sha256,
                "byteLength": asset.byte_length,
            }
            for asset in assets
        ],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repository", required=True)
    result.add_argument("--version", required=True)
    result.add_argument("--source-sha", required=True)
    result.add_argument("--control-sha", required=True)
    result.add_argument("--assembly", type=Path, required=True)
    result.add_argument(
        "--release-kind",
        choices=("full", "functional-alpha"),
        default="full",
    )
    result.add_argument("--release-notes", type=Path)
    result.add_argument("--workflow-run-id", type=_positive_cli_integer)
    result.add_argument("--workflow-run-attempt", type=_positive_cli_integer)
    result.add_argument("--authority-output", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = create_draft_release(
            repository=args.repository,
            version=args.version,
            source_sha=args.source_sha,
            control_sha=args.control_sha,
            token=os.environ.get("GITHUB_TOKEN", ""),
            assembly=args.assembly,
            release_kind=args.release_kind,
            release_notes_path=args.release_notes,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            authority_output=args.authority_output,
        )
    except DraftReleaseError as error:
        print(f"draft-release: {error}", file=sys.stderr)
        return 1
    if args.release_kind == "functional-alpha":
        summary = {
            "schema": "openprose.alpha-draft-handoff-summary/1",
            "status": "written",
            "tag": result["tag"],
            "releaseId": result["releaseId"],
            "outcome": "resumed" if result["resumed"] else "created",
            "assetCount": result["assetCount"],
        }
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
