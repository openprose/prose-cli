#!/usr/bin/env python3
"""Render deterministic, draft-safe release notes from checksum-bound manifests.

Exact candidate identity and assembly belong to ``assemble_kernel_rc.py``.
This renderer consumes its five already-validated package manifests, verifies
their exact bytes against the assembly checksum inventory, and applies a second
closed validation to every fact that becomes human-visible Markdown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping, Sequence


CLI = Path(__file__).resolve().parents[1]
FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY = (
    CLI / "shared" / "capabilities" / "adapters" / "functional-alpha.v1.json"
)
OMP_ADAPTER_RECIPE_AUTHORITY = (
    CLI / "shared" / "capabilities" / "adapters" / "recipes" / "omp-rpc.v1.json"
)
TARGET_PLATFORMS = {
    "linux-x64": "linux-x64-gnu",
    "linux-arm64": "linux-arm64-gnu",
    "darwin-arm": "darwin-arm64",
    "darwin-x64": "darwin-x64",
    "win-x64": "win32-x64",
}
TARGET_LABELS = {
    "linux-x64": "Linux x64",
    "linux-arm64": "Linux ARM64",
    "darwin-arm": "macOS Apple silicon",
    "darwin-x64": "macOS Intel",
    "win-x64": "Windows x64",
}
FUNCTIONAL_ALPHA_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)" r"-alpha\.(0|[1-9][0-9]*)$"
)
BUN_RUNTIME_BY_TARGET = {
    "linux-x64": {
        "compileTarget": "bun-linux-x64-baseline",
        "runtimeVariant": "baseline",
    },
    "linux-arm64": {
        "compileTarget": "bun-linux-arm64",
        "runtimeVariant": "native",
    },
    "darwin-arm": {
        "compileTarget": "bun-darwin-arm64",
        "runtimeVariant": "native",
    },
    "darwin-x64": {
        "compileTarget": "bun-darwin-x64-baseline",
        "runtimeVariant": "baseline",
    },
    "win-x64": {
        "compileTarget": "bun-windows-x64-baseline",
        "runtimeVariant": "baseline",
    },
}
WINDOWS_HOST_NAME = "openprose-windows-process-host.exe"
MANIFEST_SCHEMA = "openprose.local-release-manifest/1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
PORTABLE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,254})")
PROVIDER_CHARGE_BOUNDARY = (
    "The run command contacts the selected provider and may incur charges under "
    "the signed-in account. The CLI cannot determine the account or billing route."
)

TOP_LEVEL_FIELDS = {
    "schema",
    "mode",
    "version",
    "platform",
    "sourceDateEpoch",
    "releaseEligible",
    "publicationAuthorized",
    "promotion",
    "source",
    "buildProfiles",
    "bunRuntime",
    "linuxRuntime",
    "image",
    "windowsProcessHost",
    "windowsJobObjectReleaseAdmission",
    "toolchains",
    "lockfiles",
    "dependencyEvidence",
    "externalGates",
    "claims",
    "artifacts",
}
ARTIFACT_FIELDS = {"path", "kind", "implementation", "platform", "byteLength", "sha256"}


class ReleaseNotesError(RuntimeError):
    """A release-note input failed its closed validation boundary."""


def _authority_json(path: Path, label: str) -> dict[str, Any]:
    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise ReleaseNotesError(f"{label} is unavailable: {error}") from error
    if len(encoded) > 1024 * 1024:
        raise ReleaseNotesError(f"{label} exceeds its size bound")
    return _json_without_duplicates(encoded, label)


def omp_runtime_prerequisite() -> dict[str, str]:
    """Load the closed OMP runtime prerequisite used in published guidance."""

    manifest = _authority_json(
        FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY,
        "functional-alpha adapter authority",
    )
    adapters = manifest.get("adapters")
    if not isinstance(adapters, list):
        raise ReleaseNotesError("functional-alpha adapter authority lacks adapters")
    matches = [
        item
        for item in adapters
        if isinstance(item, dict) and item.get("adapterId") == "omp/rpc"
    ]
    if len(matches) != 1:
        raise ReleaseNotesError(
            "functional-alpha adapter authority must contain one OMP adapter"
        )
    omp = matches[0]
    prerequisites = omp.get("runtimePrerequisites")
    if not isinstance(prerequisites, list) or len(prerequisites) != 1:
        raise ReleaseNotesError("OMP authority must contain one runtime prerequisite")
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
        raise ReleaseNotesError("OMP runtime prerequisite or repair authority differs")
    recipe = _authority_json(OMP_ADAPTER_RECIPE_AUTHORITY, "OMP adapter recipe")
    support = recipe.get("support")
    if (
        recipe.get("adapterId") != "omp/rpc"
        or not isinstance(support, dict)
        or support.get("runtimePrerequisites") != prerequisites
        or support.get("repairCommand") != expected["repairCommand"]
    ):
        raise ReleaseNotesError(
            "OMP recipe runtime prerequisite differs from alpha authority"
        )
    return dict(expected)


def _exact_mapping(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ReleaseNotesError(f"{label} has unknown or missing fields")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseNotesError(f"{label} must be a positive integer")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ReleaseNotesError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _portable_identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or PORTABLE_IDENTITY.fullmatch(value) is None:
        raise ReleaseNotesError(f"{label} is not a safe portable identity")
    return value


def _validate_image(value: Any, target: str) -> dict[str, Any]:
    image = _exact_mapping(
        value,
        {
            "formatVersion",
            "version",
            "sha256",
            "manifestSha256",
            "purpose",
            "releaseEligible",
        },
        f"image identity for {target}",
    )
    _portable_identity(image["formatVersion"], f"image identity for {target}")
    _portable_identity(image["version"], f"image identity for {target}")
    _digest(image["sha256"], f"image digest for {target}")
    _digest(image["manifestSha256"], f"image manifest digest for {target}")
    if (
        image["purpose"] != "canonical-language-runtime"
        or image["releaseEligible"] is not True
    ):
        raise ReleaseNotesError(f"image identity for {target} is not release eligible")
    return image


def _validate_source(value: Any, target: str) -> dict[str, Any]:
    source = _exact_mapping(value, {"revision", "verification"}, f"source for {target}")
    if (
        not isinstance(source["revision"], str)
        or FULL_SHA.fullmatch(source["revision"]) is None
    ):
        raise ReleaseNotesError(
            f"source for {target} is not a full lowercase commit SHA"
        )
    if source["verification"] != "matched-product-doctor":
        raise ReleaseNotesError(
            f"source for {target} lacks product-doctor verification"
        )
    return source


def _validate_shared_shape(manifest: Any, target: str) -> dict[str, Any]:
    value = _exact_mapping(manifest, TOP_LEVEL_FIELDS, f"manifest {target}")
    if value["schema"] != MANIFEST_SCHEMA or value["mode"] != "release":
        raise ReleaseNotesError(f"manifest {target} has an unsupported schema or mode")
    if (
        not isinstance(value["version"], str)
        or SEMVER.fullmatch(value["version"]) is None
    ):
        raise ReleaseNotesError(f"manifest {target} has invalid SemVer")
    if value["platform"] != TARGET_PLATFORMS[target]:
        raise ReleaseNotesError(f"manifest {target} has the wrong platform")
    if (
        not isinstance(value["sourceDateEpoch"], int)
        or isinstance(value["sourceDateEpoch"], bool)
        or value["sourceDateEpoch"] < 0
    ):
        raise ReleaseNotesError(f"manifest {target} has an invalid source date epoch")
    if (
        value["releaseEligible"] is not False
        or value["publicationAuthorized"] is not False
    ):
        raise ReleaseNotesError(f"manifest {target} is not draft-safe")
    if value["windowsJobObjectReleaseAdmission"] is not False:
        raise ReleaseNotesError(
            f"manifest {target} makes an admitted native-Windows claim"
        )
    promotion = _exact_mapping(
        value["promotion"], {"status", "requiredAttestation"}, f"promotion for {target}"
    )
    if promotion != {
        "status": "not-performed",
        "requiredAttestation": "protected-release-validator",
    }:
        raise ReleaseNotesError(f"promotion for {target} is not draft-safe")
    _validate_source(value["source"], target)
    _validate_image(value["image"], target)
    return value


def _validate_build_and_evidence(value: dict[str, Any], target: str) -> None:
    profiles = _exact_mapping(
        value["buildProfiles"], {"rust", "bun"}, f"build profiles for {target}"
    )
    expected_profile = {"profile": "release", "testSeamsEnabled": False}
    if profiles != {"rust": expected_profile, "bun": expected_profile}:
        raise ReleaseNotesError(
            f"build profiles for {target} are not closed release profiles"
        )

    runtime = _exact_mapping(
        value["bunRuntime"],
        {"compileTarget", "runtimeVariant"},
        f"Bun runtime for {target}",
    )
    if runtime != BUN_RUNTIME_BY_TARGET[target]:
        raise ReleaseNotesError(f"Bun runtime for {target} differs from its platform")

    toolchains = _exact_mapping(
        value["toolchains"],
        {"python", "rustc", "cargo", "bun", "node", "npm"},
        f"toolchains for {target}",
    )
    if any(
        not isinstance(item, str) or not item or "\n" in item or "\r" in item
        for item in toolchains.values()
    ):
        raise ReleaseNotesError(f"toolchains for {target} contain malformed values")

    lockfiles = _exact_mapping(
        value["lockfiles"], {"cargoSha256", "bunSha256"}, f"lockfiles for {target}"
    )
    _digest(lockfiles["cargoSha256"], f"Cargo lock digest for {target}")
    _digest(lockfiles["bunSha256"], f"Bun lock digest for {target}")

    dependency = _exact_mapping(
        value["dependencyEvidence"],
        {"path", "byteLength", "sha256", "releasePolicyPassed"},
        f"dependency evidence for {target}",
    )
    if dependency["path"] != "dependency-evidence.json":
        raise ReleaseNotesError(f"dependency evidence path for {target} is not exact")
    _positive_integer(
        dependency["byteLength"], f"dependency evidence byte length for {target}"
    )
    _digest(dependency["sha256"], f"dependency evidence digest for {target}")
    if dependency["releasePolicyPassed"] is not False:
        raise ReleaseNotesError(
            f"dependency evidence for {target} overclaims release policy"
        )

    gates = _exact_mapping(
        value["externalGates"],
        {"canonicalProfile", "releaseEvidence", "authorityValidatedByPackager"},
        f"external gates for {target}",
    )
    for name in ("canonicalProfile", "releaseEvidence"):
        record = _exact_mapping(
            gates[name], {"sha256", "byteLength"}, f"{name} for {target}"
        )
        _digest(record["sha256"], f"{name} digest for {target}")
        _positive_integer(record["byteLength"], f"{name} byte length for {target}")
    if gates["authorityValidatedByPackager"] is not False:
        raise ReleaseNotesError(
            f"external gates for {target} overclaim packager authority"
        )

    claims = _exact_mapping(
        value["claims"],
        {"signing", "vulnerabilityReview", "networkIsolation", "packageTests"},
        f"claims for {target}",
    )
    if claims != {
        "signing": "not-performed",
        "vulnerabilityReview": "not-performed",
        "networkIsolation": "not-enforced",
        "packageTests": "not-run-by-packager",
    }:
        raise ReleaseNotesError(f"claims for {target} are not the closed draft claims")


def _validate_windows_host(value: dict[str, Any], target: str) -> dict[str, Any] | None:
    host = value["windowsProcessHost"]
    if target != "win-x64":
        if host != "not-applicable":
            raise ReleaseNotesError(
                f"Windows process host is present for non-Windows target {target}"
            )
        return None
    host_record = _exact_mapping(
        host, {"path", "sha256", "byteLength", "admission"}, "Windows process host"
    )
    if (
        host_record["path"] != WINDOWS_HOST_NAME
        or host_record["admission"] is not False
    ):
        raise ReleaseNotesError("Windows process host identity or admission is invalid")
    _digest(host_record["sha256"], "Windows process host digest")
    _positive_integer(host_record["byteLength"], "Windows process host byte length")
    return host_record


def _validate_linux_runtime(
    value: dict[str, Any], target: str
) -> dict[str, Any] | None:
    runtime = value["linuxRuntime"]
    if not target.startswith("linux-"):
        if runtime != "not-applicable":
            raise ReleaseNotesError(f"Linux runtime claim is present for {target}")
        return None
    record = _exact_mapping(
        runtime,
        {"minimumGlibc", "requiredGlibcMaximum", "executionEvidence"},
        f"Linux runtime for {target}",
    )
    if (
        record["minimumGlibc"] != "2.34"
        or record["executionEvidence"] != "ubuntu-22.04-only"
    ):
        raise ReleaseNotesError(f"Linux runtime floor for {target} is not exact")
    required = _exact_mapping(
        record["requiredGlibcMaximum"],
        {"rust", "bun"},
        f"Linux ELF requirements for {target}",
    )
    for implementation, observed in required.items():
        if (
            not isinstance(observed, str)
            or re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", observed) is None
            or tuple(int(part) for part in observed.split(".")) > (2, 34)
        ):
            raise ReleaseNotesError(
                f"Linux ELF requirement for {target}/{implementation} exceeds the floor"
            )
    return record


def _validate_artifacts(
    value: dict[str, Any], target: str, version: str
) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    records = value["artifacts"]
    if not isinstance(records, list) or len(records) != 4:
        raise ReleaseNotesError(
            f"artifact inventory for {target} is not exactly four records"
        )
    platform = TARGET_PLATFORMS[target]
    expected_names = {
        (
            "rust",
            "standalone-archive",
            platform,
        ): f"openprose-prose-cli-rust-{version}-{platform}.tar.gz",
        (
            "bun",
            "standalone-archive",
            platform,
        ): f"openprose-prose-cli-bun-{version}-{platform}.tar.gz",
        ("bun", "npm-meta", None): f"openprose-prose-cli-{version}.tgz",
        (
            "bun",
            "npm-platform",
            platform,
        ): f"openprose-prose-cli-{platform}-{version}.tgz",
    }
    by_shape: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for index, raw in enumerate(records):
        record = _exact_mapping(raw, ARTIFACT_FIELDS, f"artifact {target}/{index}")
        if (
            not isinstance(record["path"], str)
            or not isinstance(record["implementation"], str)
            or not isinstance(record["kind"], str)
            or record["platform"] is not None
            and not isinstance(record["platform"], str)
        ):
            raise ReleaseNotesError(
                f"artifact {target}/{index} has malformed identity fields"
            )
        shape = (record["implementation"], record["kind"], record["platform"])
        if shape not in expected_names or shape in by_shape:
            raise ReleaseNotesError(
                f"artifact inventory for {target} is duplicated or unrecognized"
            )
        if record["path"] != expected_names[shape]:
            raise ReleaseNotesError(
                f"artifact inventory for {target} has a noncanonical filename"
            )
        _positive_integer(record["byteLength"], f"artifact byte length for {target}")
        _digest(record["sha256"], f"artifact digest for {target}")
        by_shape[shape] = record
    if set(by_shape) != set(expected_names):
        raise ReleaseNotesError(f"artifact inventory for {target} is incomplete")
    return by_shape


def normalize_manifests(manifests: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize all facts that are allowed into release notes."""
    if not isinstance(manifests, Mapping) or set(manifests) != set(TARGET_PLATFORMS):
        raise ReleaseNotesError(
            "release notes require exactly all five target manifests"
        )
    values = {
        target: _validate_shared_shape(manifests[target], target)
        for target in TARGET_PLATFORMS
    }
    baseline = values["linux-x64"]
    for target, value in values.items():
        if value["version"] != baseline["version"]:
            raise ReleaseNotesError(f"version divergence across manifests: {target}")
        if value["source"] != baseline["source"]:
            raise ReleaseNotesError(f"source divergence across manifests: {target}")
        if value["image"] != baseline["image"]:
            raise ReleaseNotesError(f"image divergence across manifests: {target}")
        for field, label in (
            ("buildProfiles", "build profile"),
            ("lockfiles", "lockfile"),
            ("dependencyEvidence", "dependency evidence"),
            ("externalGates", "external gate"),
            ("claims", "claim"),
        ):
            if value[field] != baseline[field]:
                raise ReleaseNotesError(
                    f"{label} divergence across manifests: {target}"
                )

    targets: dict[str, Any] = {}
    shared_meta: dict[str, Any] | None = None
    for target, value in values.items():
        _validate_build_and_evidence(value, target)
        host = _validate_windows_host(value, target)
        linux_runtime = _validate_linux_runtime(value, target)
        artifacts = _validate_artifacts(value, target, baseline["version"])
        meta = artifacts[("bun", "npm-meta", None)]
        if shared_meta is None:
            shared_meta = meta
        elif meta != shared_meta:
            raise ReleaseNotesError(
                f"npm meta-package divergence across manifests: {target}"
            )
        targets[target] = {
            "platform": TARGET_PLATFORMS[target],
            "artifacts": artifacts,
            "bunRuntime": value["bunRuntime"],
            "windowsProcessHost": host,
            "linuxRuntime": linux_runtime,
        }
    assert shared_meta is not None
    return {
        "version": baseline["version"],
        "sourceSha": baseline["source"]["revision"],
        "image": baseline["image"],
        "sharedNpmMeta": shared_meta,
        "targets": targets,
    }


def _standalone_commands(
    archive: str, platform: str, implementation: str, version: str
) -> list[str]:
    root = f"openprose-prose-cli-{implementation}-{version}-{platform}"
    if platform == "win32-x64":
        return [f"tar -xzf .\\{archive}", f".\\{root}\\prose.exe --version"]
    return [f"tar -xzf ./{archive}", f"./{root}/prose --version"]


def _code_block(lines: Sequence[str], language: str = "sh") -> list[str]:
    return [f"```{language}", *lines, "```"]


def render_release_notes(manifests: Mapping[str, Any]) -> bytes:
    """Return stable UTF-8 Markdown for exactly five validated manifests."""
    model = normalize_manifests(manifests)
    omp_prerequisite = omp_runtime_prerequisite()
    version = model["version"]
    source = model["sourceSha"]
    image = model["image"]
    meta_name = model["sharedNpmMeta"]["path"]
    lines = [
        f"# OpenProse CLI v{version} — draft candidate",
        "",
        "> This is a draft candidate only. It is not publication-authorized, signed,",
        "> semantically admitted, or a claim that either implementation is preferable.",
        "",
        f"Source revision: `{source}`",
        "",
        "## Skill Runtime Image identity",
        "",
        f"- Format: `{image['formatVersion']}`",
        f"- Version: `{image['version']}`",
        f"- Image SHA-256: `{image['sha256']}`",
        f"- Manifest SHA-256: `{image['manifestSha256']}`",
        "- The image manifest is marked release-eligible, but that necessary "
        "input does not authorize publication.",
        "",
        "## Artifact map and installation",
        "",
        "The Rust archive contains the native Rust implementation. The Bun archive",
        "contains the Bun-authored standalone. The npm installation uses the shared",
        "plain-Node launcher plus the exact Bun platform package; it performs no",
        "runtime download or install script.",
        "Bun x64 artifacts use baseline CPU runtime variants; ARM64 artifacts use",
        "native Bun runtime variants. Each platform section names the exact compile target.",
        "",
        f"Shared npm launcher package: `{meta_name}`",
        "",
    ]
    for target in TARGET_PLATFORMS:
        platform = TARGET_PLATFORMS[target]
        target_value = model["targets"][target]
        artifacts = target_value["artifacts"]
        rust = artifacts[("rust", "standalone-archive", platform)]["path"]
        bun = artifacts[("bun", "standalone-archive", platform)]["path"]
        npm = artifacts[("bun", "npm-platform", platform)]["path"]
        shell = "powershell" if target == "win-x64" else "sh"
        lines.extend(
            [
                f"### {TARGET_LABELS[target]}",
                "",
                f"- Rust standalone: `{rust}`",
                f"- Bun standalone: `{bun}`",
                f"- npm Bun platform package: `{npm}`",
                f"- Bun compile target: `{target_value['bunRuntime']['compileTarget']}`",
                f"- Bun runtime variant: `{target_value['bunRuntime']['runtimeVariant']}`",
                "",
                "Rust standalone:",
                "",
                *_code_block(
                    _standalone_commands(rust, platform, "rust", version), shell
                ),
                "",
                "Bun standalone:",
                "",
                *_code_block(
                    _standalone_commands(bun, platform, "bun", version), shell
                ),
                "",
                "npm-compatible global installation:",
                "",
                *_code_block(
                    [
                        f"npm install --global --ignore-scripts ./{npm} ./{meta_name}",
                        "prose --version",
                    ],
                    shell,
                ),
                "",
            ]
        )
        if target_value["linuxRuntime"] is not None:
            runtime = target_value["linuxRuntime"]
            lines.extend(
                [
                    f"Linux runtime: glibc >= `{runtime['minimumGlibc']}`; execution evidence is Ubuntu 22.04 only.",
                    "",
                ]
            )

    lines.extend(
        [
            "## Functional-alpha first run",
            "",
            "Every standalone archive and the shared npm meta package contain the exact",
            "contract at `examples/hello.prose.md`. Functional-alpha harness support is",
            "platform-specific:",
            "",
            "| Platform | Supported functional-alpha harnesses |",
            "| --- | --- |",
            "| macOS Apple silicon | Prime, OMP, Codex, Claude |",
            "| macOS Intel | Codex |",
            "| Linux x64 | Codex, OMP |",
            "| Linux ARM64 | Codex |",
            "| Windows | Omitted from the functional alpha |",
            "",
            "Admission and repair versions:",
            "",
            "- Prime exact admitted versions: `0.7.0`, `0.8.1`; official repair: "
            "`curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1` "
            "from the official versioned prime-agent release tarball.",
            "- OMP exact admitted version: `18.0.9`; requires "
            f"`{omp_prerequisite['runtime']} {omp_prerequisite['versionRange']}`; "
            f"repair: `{omp_prerequisite['repairCommand']}`.",
            "- Codex exact admitted version: `0.149.0-alpha.4.1`; repair pin: "
            "`npm install --global @openai/codex@0.149.0-alpha.4.1`.",
            "- Claude exact admitted version: `2.1.243`; repair "
            "pin: `npm install --global @anthropic-ai/claude-code@2.1.243`.",
            "",
            "These versions are the exact functional-alpha allowlist.",
            "",
            "For a previously extracted Rust standalone archive, select Codex",
            "(supported on every POSIX alpha target) and use its packaged example:",
            "",
            *_code_block(
                [
                    f"PROSE='./openprose-prose-cli-rust-{version}-<platform>/prose'",
                    f"EXAMPLE='./openprose-prose-cli-rust-{version}-<platform>/examples/hello.prose.md'",
                    '"$PROSE" cli harness list',
                    '"$PROSE" cli harness use codex',
                    '"$PROSE" cli doctor',
                    '"$PROSE" run "$EXAMPLE"',
                ]
            ),
            "",
            "For a previously extracted Bun standalone archive:",
            "",
            *_code_block(
                [
                    f"PROSE='./openprose-prose-cli-bun-{version}-<platform>/prose'",
                    f"EXAMPLE='./openprose-prose-cli-bun-{version}-<platform>/examples/hello.prose.md'",
                    '"$PROSE" cli harness list',
                    '"$PROSE" cli harness use codex',
                    '"$PROSE" cli doctor',
                    '"$PROSE" run "$EXAMPLE"',
                ]
            ),
            "",
            "For the npm/global surface, run the complete journey with the installed meta-package copy:",
            "",
            *_code_block(
                [
                    "PROSE=prose",
                    'EXAMPLE="$(npm root --global)/@openprose/prose-cli/examples/hello.prose.md"',
                    '"$PROSE" cli harness list',
                    '"$PROSE" cli harness use codex',
                    '"$PROSE" cli doctor',
                    '"$PROSE" run "$EXAMPLE"',
                ]
            ),
            "",
            "The independently copyable alternatives below target the npm/global",
            "surface installed above; standalone archives carry their own exact-path",
            "journeys. Use only one that the platform table supports. Prime and OMP",
            "require a fully qualified model plus their explicit harness-login profile.",
            "",
            "Prime — macOS Apple silicon only:",
            "",
            *_code_block(
                [
                    'NPM_PREFIX="$(npm prefix --global)"',
                    'PROSE="$NPM_PREFIX/bin/prose"',
                    'EXAMPLE="$NPM_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
                    'test -x "$PROSE" && test -f "$EXAMPLE"',
                    '"$PROSE" cli harness use prime --model openai-codex/gpt-5.4 '
                    "--auth-profile prime-harness-login",
                    '"$PROSE" cli doctor',
                    '"$PROSE" run "$EXAMPLE"',
                ]
            ),
            "",
            "OMP — macOS Apple silicon or Linux x64 only:",
            "",
            *_code_block(
                [
                    'NPM_PREFIX="$(npm prefix --global)"',
                    'PROSE="$NPM_PREFIX/bin/prose"',
                    'EXAMPLE="$NPM_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
                    'test -x "$PROSE" && test -f "$EXAMPLE"',
                    '"$PROSE" cli harness use omp --model openai-codex/gpt-5.4 '
                    "--auth-profile omp-harness-login",
                    '"$PROSE" cli doctor',
                    '"$PROSE" run "$EXAMPLE"',
                ]
            ),
            "",
            "Claude — macOS Apple silicon only:",
            "",
            *_code_block(
                [
                    'NPM_PREFIX="$(npm prefix --global)"',
                    'PROSE="$NPM_PREFIX/bin/prose"',
                    'EXAMPLE="$NPM_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
                    'test -x "$PROSE" && test -f "$EXAMPLE"',
                    '"$PROSE" cli harness use claude',
                    '"$PROSE" cli doctor',
                    '"$PROSE" run "$EXAMPLE"',
                ]
            ),
            "",
            "The example model is illustrative; replace it with a fully-qualified provider/model exposed by your selected harness login.",
            "",
            "Those profile names mean harness-managed login with unknown billing, not a subscription-billing claim.",
            "",
            "If the selected image identity is `echo-v0`, echo-v0 transports and echoes",
            "only the opaque `prose run <path>` task argv. The runner does not open or",
            "read the packaged example file, does not evaluate the OpenProse contract,",
            "and does not return its `Hello, world!` value.",
            "",
            "macOS alpha standalone executables are ad-hoc signed and not notarized.",
            "Verify the artifact against `SHA256SUMS` first. If Gatekeeper then",
            "quarantines the standalone selected above, remove only the invoked",
            "`$PROSE` file's quarantine attribute:",
            "",
            *_code_block(['xattr -d com.apple.quarantine "$PROSE"']),
            "",
        ]
    )

    host = model["targets"]["win-x64"]["windowsProcessHost"]
    assert host is not None
    lines.extend(
        [
            "## Windows process-host status",
            "",
            f"The Windows packages contain `{host['path']}` beside each executable.",
            f"Its SHA-256 is `{host['sha256']}` and its byte length is `{host['byteLength']}`.",
            "Job Object release admission: **false**. Carrying this exact sidecar does",
            "not establish native Windows readiness or authorize external harness runs.",
            "",
            "## Verify downloaded bytes",
            "",
            "The release asset `SHA256SUMS` binds every uploaded byte, but it does not authenticate the publisher. Set `ASSET` to each exact downloaded filename. On Linux:",
            "",
            *_code_block(
                [
                    f"ASSET='openprose-prose-cli-rust-{version}-<platform>.tar.gz'",
                    "test \"$(awk -v name=\"$ASSET\" '$2 == name { n++ } END { print n+0 }' SHA256SUMS)\" -eq 1 || { echo 'missing or duplicate checksum entry' >&2; exit 1; }",
                    "awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | sha256sum -c -",
                ]
            ),
            "",
            "On macOS:",
            "",
            *_code_block(
                [
                    f"ASSET='openprose-prose-cli-rust-{version}-<platform>.tar.gz'",
                    "test \"$(awk -v name=\"$ASSET\" '$2 == name { n++ } END { print n+0 }' SHA256SUMS)\" -eq 1 || { echo 'missing or duplicate checksum entry' >&2; exit 1; }",
                    "awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | shasum -a 256 -c -",
                ]
            ),
            "",
            "On Windows PowerShell:",
            "",
            *_code_block(
                [
                    f"$Asset = 'openprose-prose-cli-rust-{version}-win32-x64.tar.gz'",
                    "$Pattern = '^([0-9a-f]{64})  ' + [regex]::Escape($Asset) + '$'",
                    "$Records = @(Get-Content .\\SHA256SUMS | Where-Object { $_ -match $Pattern })",
                    "if ($Records.Count -ne 1) { throw 'Missing or duplicate checksum entry' }",
                    "$Expected = $Records[0].Substring(0, 64)",
                    "if ((Get-FileHash -Algorithm SHA256 $Asset).Hash.ToLowerInvariant() "
                    '-ne $Expected) { throw "Checksum mismatch: $Asset" }',
                ],
                "powershell",
            ),
            "",
            "`SHA256SUMS` authenticates no publisher identity on its own. Signing and",
            "protected promotion authority remain unavailable for this draft.",
            "",
            "## Explicit external blockers",
            "",
            "- Prime, OMP, Codex, and Claude installed-wrapper execution still "
            "requires strict adapter authority.",
            "- Hosted OpenProse identity, billing, quota, retention, and spend controls "
            "remain externally gated.",
            "- Semantic equivalence, Prose Complete, program-portability, and public "
            "benchmark claims require the protected language corpus, terminal "
            "validators, and candidate-bound evidence.",
            "- Native Windows process containment requires retained native workflow "
            "evidence and an intentional admission change; the packaged value remains false.",
            "- Protected signing, governance approval, and promotion remain separate external authorities.",
            "- A checksummed dependency component inventory is attached, but license authority, "
            "vulnerability analysis, fetched-package verification, and package signing remain unresolved.",
            "",
            "No aggregate winner or cross-implementation recommendation is asserted.",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise ReleaseNotesError(
            f"input is unavailable: {path.name}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReleaseNotesError(
            f"input must be a non-symlink regular file: {path.name}"
        )
    if before.st_size <= 0 or before.st_size > maximum:
        raise ReleaseNotesError(f"input has an invalid size: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseNotesError(
            f"input cannot be opened safely: {path.name}: {error}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ReleaseNotesError(f"input changed before open: {path.name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise ReleaseNotesError(f"input exceeds its size limit: {path.name}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        opened.st_size != total
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
    ):
        raise ReleaseNotesError(f"input changed while read: {path.name}")
    return b"".join(chunks)


def _json_without_duplicates(encoded: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ReleaseNotesError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=pairs)
    except ReleaseNotesError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseNotesError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ReleaseNotesError(f"{label} must contain an object")
    return value


def load_assembly_manifests(assembly: Path) -> dict[str, dict[str, Any]]:
    """Load five checksum-bound manifests from an already-closed assembly."""
    try:
        root_metadata = assembly.lstat()
    except OSError as error:
        raise ReleaseNotesError(f"assembly is unavailable: {error}") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ReleaseNotesError("assembly must be a non-symlink directory")
    checksum_bytes = _read_regular(assembly / "SHA256SUMS", MAX_CHECKSUM_BYTES)
    try:
        checksum_text = checksum_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise ReleaseNotesError("SHA256SUMS is not ASCII") from error
    declared: dict[str, str] = {}
    for line in checksum_text.splitlines():
        match = CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise ReleaseNotesError("SHA256SUMS contains a malformed entry")
        digest, name = match.groups()
        if name == "SHA256SUMS" or name in declared:
            raise ReleaseNotesError(
                "SHA256SUMS contains a duplicate or recursive entry"
            )
        declared[name] = digest

    manifests: dict[str, dict[str, Any]] = {}
    for target in TARGET_PLATFORMS:
        name = f"{target}-release-manifest.json"
        if name not in declared:
            raise ReleaseNotesError(f"missing checksum for release manifest: {target}")
        encoded = _read_regular(assembly / name, MAX_MANIFEST_BYTES)
        if hashlib.sha256(encoded).hexdigest() != declared[name]:
            raise ReleaseNotesError(f"release manifest digest mismatch: {target}")
        manifests[target] = _json_without_duplicates(
            encoded, f"release manifest {target}"
        )
    normalize_manifests(manifests)
    return manifests


def render_functional_alpha_notes(version: str) -> bytes:
    """Render the one controller-owned promotable functional-alpha body."""

    if FUNCTIONAL_ALPHA_SEMVER.fullmatch(version) is None:
        raise ReleaseNotesError(
            "functional-alpha version must be numbered alpha SemVer"
        )
    omp_prerequisite = omp_runtime_prerequisite()
    template = """## OpenProse CLI functional alpha

This functional alpha exercises installed-harness transport with the nonsemantic `echo-v0` image. It does not execute the OpenProse language or claim semantic conformance, program portability, signing, or publication authority. Candidate evidence and these release notes do not authorize or prove publication. `echo-v0` transports and echoes only the opaque `prose run <path>` task argv. The runner does not open or read the packaged example file, does not evaluate the packaged OpenProse contract, and does not return its `Hello, world!` value.

### Choose an artifact

Replace `<platform>` with `darwin-arm64`, `darwin-x64`, `linux-arm64-gnu`, or `linux-x64-gnu`.

- Rust standalone: `openprose-prose-cli-rust-__VERSION__-<platform>.tar.gz`
- Bun standalone: `openprose-prose-cli-bun-__VERSION__-<platform>.tar.gz`
- npm/global: download both `openprose-prose-cli-__VERSION__.tgz` and `openprose-prose-cli-<platform>-__VERSION__.tgz`

Bun and npm artifacts on macOS require macOS 13 or newer. The x64 packages use Bun's baseline CPU targets `bun-darwin-x64-baseline` and `bun-linux-x64-baseline`; ARM64 packages use the native `bun-darwin-arm64` and `bun-linux-arm64` targets. Each release manifest binds the exact `bunRuntime` compile target and runtime variant. The Rust standalone does not inherit Bun's runtime floor.

The npm launcher has a Node.js 22.22.3 consumer compatibility floor. Release CI and admission use exactly Node.js 24.20.0.

Functional-alpha harness support is target-specific:

| Platform | Harnesses |
| --- | --- |
| `darwin-arm64` | Prime, OMP, Codex, Claude |
| `darwin-x64` | Codex |
| `linux-x64-gnu` | Codex, OMP |
| `linux-arm64-gnu` | Codex |
| Windows | Omitted from the functional alpha |

Exact admission and repair versions:

- Prime: exact `0.7.0` and `0.8.1` are admitted; repair with `curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1`, which installs the official versioned prime-agent release tarball.
- OMP: exactly `18.0.9`; requires `__OMP_RUNTIME__ __OMP_RUNTIME_RANGE__`; repair with `__OMP_REPAIR_COMMAND__`.
- Codex: exactly `0.149.0-alpha.4.1`; repair pin `npm install --global @openai/codex@0.149.0-alpha.4.1`.
- Claude: exactly `2.1.243`; repair pin `npm install --global @anthropic-ai/claude-code@2.1.243`.

These versions are the exact functional-alpha allowlist.

Download `SHA256SUMS` from this release with the selected artifact(s). Set `ASSET` to each downloaded filename and verify its exact inventory entry before extraction:

    test "$(awk -v name="$ASSET" '$2 == name { n++ } END { print n+0 }' SHA256SUMS)" -eq 1 || { echo 'missing or duplicate checksum entry' >&2; exit 1; }
    awk -v name="$ASSET" '$2 == name' SHA256SUMS | shasum -a 256 -c -     # macOS
    awk -v name="$ASSET" '$2 == name' SHA256SUMS | sha256sum -c -         # Linux

Then verify the GitHub Actions build provenance for each downloaded release asset, including the aggregate `SHA256SUMS` asset. Repeat this command with `ASSET` set to each local filename:

    gh attestation verify "$ASSET" --repo openprose/prose

This attestation binds the exact local bytes to GitHub Actions build provenance. It does not sign or notarize binaries, does not authorize publication, and does not establish that a release is public. Verification requires access to the repository's attestation record at the applicable release stage.

### Install

For a standalone archive, extract it and bind the exact installed executable path:

    IMPLEMENTATION=rust  # or bun
    PLATFORM=darwin-arm64  # choose the matching platform ID above
    ARCHIVE="openprose-prose-cli-$IMPLEMENTATION-__VERSION__-$PLATFORM.tar.gz"
    tar -xzf "$ARCHIVE"
    SOURCE="${ARCHIVE%.tar.gz}"
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-$IMPLEMENTATION-$PLATFORM"
    test ! -e "$INSTALL_PREFIX" || { echo "refusing to overwrite $INSTALL_PREFIX" >&2; exit 1; }
    umask 077
    mkdir -p "$HOME/.local"
    mkdir "$INSTALL_PREFIX" "$INSTALL_PREFIX/bin" "$INSTALL_PREFIX/examples"
    install -m 0755 "$SOURCE/prose" "$INSTALL_PREFIX/bin/prose"
    install -m 0644 "$SOURCE/examples/hello.prose.md" "$INSTALL_PREFIX/examples/hello.prose.md"
    install -m 0644 "$SOURCE/README.txt" "$INSTALL_PREFIX/README.txt"
    install -m 0644 "$SOURCE/LICENSE" "$INSTALL_PREFIX/LICENSE"
    test -f "$INSTALL_PREFIX/README.txt" && test ! -L "$INSTALL_PREFIX/README.txt"
    test -f "$INSTALL_PREFIX/LICENSE" && test ! -L "$INSTALL_PREFIX/LICENSE"
    PROSE="$INSTALL_PREFIX/bin/prose"
    EXAMPLE="$INSTALL_PREFIX/examples/hello.prose.md"
    GATEKEEPER_PROSE="$PROSE"

The installed `README.txt` retains offline support and safe same-version repair guidance from the packaged archive. `LICENSE` retains the exact packaged license beside that guidance; neither file depends on a registry or a PATH-resolved `prose` command.

Only after this functional alpha has been independently authorized and publicly promoted, choose one registry installation. Candidate evidence and these notes do not establish that condition.

For the convenient moving alpha channel:

    ALPHA_PREFIX="$HOME/.local/openprose-cli-alpha"
    test ! -e "$ALPHA_PREFIX" || { echo "refusing to overwrite $ALPHA_PREFIX" >&2; exit 1; }
    npm install --global --ignore-scripts --prefix "$ALPHA_PREFIX" "@openprose/prose-cli@alpha"

For this exact promoted version:

    PLATFORM=darwin-arm64  # choose the matching platform ID above
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"
    test ! -e "$INSTALL_PREFIX" || { echo "refusing to overwrite $INSTALL_PREFIX" >&2; exit 1; }
    npm install --global --ignore-scripts --prefix "$INSTALL_PREFIX" "@openprose/prose-cli@__VERSION__"
    EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
    PROSE="$INSTALL_PREFIX/bin/prose"
    GATEKEEPER_PROSE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli-$PLATFORM/bin/prose"

The first-run commands below use the exact-version prefix. The offline two-tarball route remains available before promotion and for custody verification.

For npm/global installation from the two downloaded tarballs:

    PLATFORM=darwin-arm64  # choose the matching platform ID above
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"
    test ! -e "$INSTALL_PREFIX" || { echo "refusing to overwrite $INSTALL_PREFIX" >&2; exit 1; }
    umask 077
    npm install --global --offline --ignore-scripts --prefix "$INSTALL_PREFIX" "./openprose-prose-cli-$PLATFORM-__VERSION__.tgz" "./openprose-prose-cli-__VERSION__.tgz"
    EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
    PROSE="$INSTALL_PREFIX/bin/prose"
    GATEKEEPER_PROSE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli-$PLATFORM/bin/prose"

### First run

Every standalone archive and the npm meta package contain the exact `examples/hello.prose.md` contract. After setting `EXAMPLE` for the chosen surface above, copy those exact bytes and select Codex (supported on every POSIX alpha target):

__PROVIDER_CHARGE_BOUNDARY__

    npm install --global @openai/codex@0.149.0-alpha.4.1
    codex login
    test ! -e hello.prose.md || { echo 'refusing to overwrite hello.prose.md' >&2; exit 1; }
    cp "$EXAMPLE" hello.prose.md
    cmp -s "$EXAMPLE" hello.prose.md
    "$PROSE" cli harness list
    "$PROSE" cli harness use codex
    "$PROSE" cli doctor
    "$PROSE" run hello.prose.md

The independently copyable alternatives below target the npm/global surface installed above; standalone archives carry exact-path journeys in their embedded README. Use only one that the platform table supports. Prime and OMP require a fully-qualified model and their explicit login profile.

Prime — macOS Apple silicon only:

__PROVIDER_CHARGE_BOUNDARY__

    PLATFORM=darwin-arm64
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"
    PROSE="$INSTALL_PREFIX/bin/prose"
    EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
    "$PROSE" cli harness use prime --model openai-codex/gpt-5.4 --auth-profile prime-harness-login
    "$PROSE" cli doctor
    "$PROSE" run "$EXAMPLE"

OMP — macOS Apple silicon:

__PROVIDER_CHARGE_BOUNDARY__

    PLATFORM=darwin-arm64
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"
    PROSE="$INSTALL_PREFIX/bin/prose"
    EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
    "$PROSE" cli harness use omp --model openai-codex/gpt-5.4 --auth-profile omp-harness-login
    "$PROSE" cli doctor
    "$PROSE" run "$EXAMPLE"

OMP — Linux x64:

__PROVIDER_CHARGE_BOUNDARY__

    PLATFORM=linux-x64-gnu
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"
    PROSE="$INSTALL_PREFIX/bin/prose"
    EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
    "$PROSE" cli harness use omp --model openai-codex/gpt-5.4 --auth-profile omp-harness-login
    "$PROSE" cli doctor
    "$PROSE" run "$EXAMPLE"

Claude — macOS Apple silicon only:

__PROVIDER_CHARGE_BOUNDARY__

    PLATFORM=darwin-arm64
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"
    PROSE="$INSTALL_PREFIX/bin/prose"
    EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
    "$PROSE" cli harness use claude
    "$PROSE" cli doctor
    "$PROSE" run "$EXAMPLE"

The example model is illustrative; replace it with a fully-qualified provider/model exposed by your selected harness login.

The selection command saves only the harness, model, and profile identifiers. Later doctor/run commands use that saved bundle without overrides when no higher-precedence project or environment setting is active.

Those profile names mean harness-managed login with unknown billing, not a subscription-billing claim. The CLI is noninteractive and never opens a TUI; interactive TUI use remains owned by the harness plus its installed OpenProse skill.

Installed harnesses run with the user's authority; this alpha is not a sandbox or strict descendant-containment boundary. Prime and Claude isolation is advisory, while OMP and Codex isolation is unsupported. Ambient harness configuration, plugins, skills, cached account/provider state, and provider-side routing remain external and unbound.

macOS alpha executables are ad-hoc signed and not notarized. Verify `SHA256SUMS` first. If Gatekeeper then quarantines the installed native executable selected above, inspect it and remove only that file's quarantine attribute with `xattr -d com.apple.quarantine "$GATEKEEPER_PROSE"`.

### Remove

For a standalone prefix created by the installation block above, choose the same implementation and platform. The block removes only the four installed files and then removes directories only when they are empty:

    IMPLEMENTATION=rust  # or bun
    PLATFORM=darwin-arm64  # choose the matching platform ID above
    case "$IMPLEMENTATION" in rust|bun) ;; *) echo "unsupported implementation" >&2; exit 1 ;; esac
    case "$PLATFORM" in darwin-arm64|darwin-x64|linux-arm64-gnu|linux-x64-gnu) ;; *) echo "unsupported functional-alpha platform" >&2; exit 1 ;; esac
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-$IMPLEMENTATION-$PLATFORM"
    test -d "$INSTALL_PREFIX" && test ! -L "$INSTALL_PREFIX" || { echo "standalone prefix is missing or unsafe" >&2; exit 1; }
    test -f "$INSTALL_PREFIX/bin/prose" && test ! -L "$INSTALL_PREFIX/bin/prose" || { echo "standalone executable is missing or unsafe" >&2; exit 1; }
    test -f "$INSTALL_PREFIX/examples/hello.prose.md" && test ! -L "$INSTALL_PREFIX/examples/hello.prose.md" || { echo "standalone example is missing or unsafe" >&2; exit 1; }
    test -f "$INSTALL_PREFIX/README.txt" && test ! -L "$INSTALL_PREFIX/README.txt" || { echo "standalone README is missing or unsafe" >&2; exit 1; }
    test -f "$INSTALL_PREFIX/LICENSE" && test ! -L "$INSTALL_PREFIX/LICENSE" || { echo "standalone license is missing or unsafe" >&2; exit 1; }
    rm -f -- "$INSTALL_PREFIX/bin/prose" "$INSTALL_PREFIX/examples/hello.prose.md" "$INSTALL_PREFIX/README.txt" "$INSTALL_PREFIX/LICENSE"
    rmdir "$INSTALL_PREFIX/bin" "$INSTALL_PREFIX/examples" "$INSTALL_PREFIX"

For the exact-version or offline npm prefix, choose the same platform and uninstall both package names:

    PLATFORM=darwin-arm64  # choose the matching platform ID above
    case "$PLATFORM" in darwin-arm64|darwin-x64|linux-arm64-gnu|linux-x64-gnu) ;; *) echo "unsupported functional-alpha platform" >&2; exit 1 ;; esac
    INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"
    npm uninstall --global --prefix "$INSTALL_PREFIX" @openprose/prose-cli "@openprose/prose-cli-$PLATFORM"

If the moving alpha-channel prefix was installed, use the same exact platform selection with `INSTALL_PREFIX="$HOME/.local/openprose-cli-alpha"` and the same two-package `npm uninstall` command. npm removal intentionally does not recursively delete the prefix.

### Support and security

Read the [CLI support and compatibility guide](https://github.com/openprose/prose/blob/main/cli/SUPPORT.md). Report a reproducible CLI or installation problem through the [sanitized CLI report form](https://github.com/openprose/prose/issues/new?template=openprose-cli-bug.yml). Report a suspected vulnerability only through [private vulnerability reporting](https://github.com/openprose/prose/security/advisories/new). Do not put credentials, tokens, account identifiers, private paths, or raw provider output in a public issue.

Inspect the per-platform release manifest, SBOM, provenance, dependency evidence, and admission report for the exact tested boundary.
"""
    return (
        template.replace("__VERSION__", version)
        .replace("__OMP_RUNTIME__", omp_prerequisite["runtime"])
        .replace("__OMP_RUNTIME_RANGE__", omp_prerequisite["versionRange"])
        .replace("__OMP_REPAIR_COMMAND__", omp_prerequisite["repairCommand"])
        .replace("__PROVIDER_CHARGE_BOUNDARY__", PROVIDER_CHARGE_BOUNDARY)
        .encode("utf-8")
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument("--assembly", type=Path)
    source.add_argument("--functional-alpha-version")
    result.add_argument("--output", type=Path)
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    try:
        notes = (
            render_functional_alpha_notes(options.functional_alpha_version)
            if options.functional_alpha_version is not None
            else render_release_notes(load_assembly_manifests(options.assembly))
        )
        if options.output is None:
            sys.stdout.buffer.write(notes)
        else:
            try:
                with options.output.open("xb") as destination:
                    destination.write(notes)
            except FileExistsError as error:
                raise ReleaseNotesError(
                    f"output already exists: {options.output}"
                ) from error
            except OSError as error:
                raise ReleaseNotesError(
                    f"output cannot be created: {options.output}: {error}"
                ) from error
        return 0
    except ReleaseNotesError as error:
        print(f"release notes: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
