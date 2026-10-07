from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import copy
import gzip
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).with_name("benchmark.py")
SPEC = importlib.util.spec_from_file_location("openprose_installed_benchmark", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import installed benchmark: {SCRIPT}")
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)

VERSION = "0.1.0"
REVISION = "development"
RELEASE_REVISION = "0123456789abcdef0123456789abcdef01234567"
IMAGE = {
    "formatVersion": "openprose.skill-runtime-image/1",
    "version": "sentinel-v1",
    "sha256": "ee13d1cbba24d1623523f4fe8747b4a3387cd6880f5d6fb4a360d2a7949ddf00",
    "manifestSha256": "f" * 64,
    "purpose": "sentinel-transport-test",
    "releaseEligible": False,
}
ECHO_IMAGE = {
    "formatVersion": "openprose.skill-runtime-image/1",
    "version": "echo-v0",
    "sha256": "daf3fab11a27b6c982efdad0d223823b35bd7c8de05d9b1d464a30ed8ec146f2",
    "manifestSha256": "e" * 64,
    "purpose": "functional-alpha-placeholder",
    "releaseEligible": True,
}
OPAQUE_ARGV = [
    "write",
    "installed-package-benchmark",
    "opaque 雪",
    "--model",
    "literal;$(never-shell)",
]
FORWARDED_TASK_ARGV = ["prose", *OPAQUE_ARGV]


def platform_id() -> str:
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
        raise unittest.SkipTest("fixture host is not a packaged platform")
    return f"linux-{machine}-gnu" if system == "linux" else f"{system}-{machine}"


PLATFORM = platform_id()
HELLO_EXAMPLE = ROOT / "cli" / "conformance" / "live-alpha" / "hello.prose.md"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_tar(path: Path, members: list[tuple[str, bytes, int | str]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, data, mode_or_type in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if isinstance(mode_or_type, int):
                info.mode = mode_or_type
                archive.addfile(info, io.BytesIO(data))
            elif isinstance(mode_or_type, tuple):
                info.type = tarfile.DIRTYPE
                info.mode = mode_or_type[1]
                info.size = 0
                archive.addfile(info)
            else:
                info.type = tarfile.SYMTYPE
                info.mode = 0o777
                info.linkname = mode_or_type
                info.size = 0
                archive.addfile(info)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def dependency_fixture() -> dict[str, object]:
    declared = {
        "status": "declared",
        "algorithm": "sha256",
        "digest": "a" * 64,
    }
    package = {
        "name": "fixture-dependency",
        "version": "1.2.3",
        "source": "registry+https://github.com/rust-lang/crates.io-index",
        "integrity": declared,
        "scopes": ["runtime"],
    }
    return {
        "schema": "openprose.dependency-evidence/1",
        "generator": {
            "name": "openprose-dependency-evidence",
            "version": 1,
            "providerFree": True,
            "networkUsed": False,
        },
        "sources": [
            {"path": "cli/bun/bun.lock", "byteLength": 10, "sha256": "1" * 64},
            {"path": "cli/bun/package.json", "byteLength": 11, "sha256": "4" * 64},
            {
                "path": "cli/platform/windows-process-host/Cargo.lock",
                "byteLength": 30,
                "sha256": "3" * 64,
            },
            {
                "path": "cli/platform/windows-process-host/Cargo.toml",
                "byteLength": 31,
                "sha256": "5" * 64,
            },
            {"path": "cli/rust/Cargo.lock", "byteLength": 20, "sha256": "2" * 64},
            {"path": "cli/rust/Cargo.toml", "byteLength": 21, "sha256": "6" * 64},
            {
                "path": "cli/rust/crates/prose-cli/Cargo.toml",
                "byteLength": 22,
                "sha256": "7" * 64,
            },
            {
                "path": "cli/rust/crates/prose-process-supervisor/Cargo.toml",
                "byteLength": 23,
                "sha256": "8" * 64,
            },
            {
                "path": "cli/rust/crates/prose-runner-core/Cargo.toml",
                "byteLength": 24,
                "sha256": "9" * 64,
            },
        ],
        "inventories": {
            "bun": {
                "lockfileVersion": 1,
                "scopeBasis": "package-manifest-direct-kind-plus-lockfile-reachability",
                "packages": [
                    {
                        "name": "@openprose/prose-cli-bun",
                        "version": VERSION,
                        "source": "workspace",
                        "integrity": {
                            "status": "not-applicable",
                            "reason": "workspace-package",
                        },
                        "scopes": ["workspace"],
                    }
                ],
            },
            "cargo": {
                "component": "rust-cli",
                "target": "multi-platform",
                "lockfileVersion": 4,
                "scopeBasis": "workspace-manifest-direct-kind-plus-lockfile-reachability",
                "packages": [package],
            },
            "windowsProcessHostCargo": {
                "component": "windows-process-host",
                "target": "windows",
                "lockfileVersion": 4,
                "scopeBasis": "package-manifest-direct-kind-plus-lockfile-reachability",
                "packages": [package],
            },
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
            "schema": "openprose.dependency-release-policy/1",
            "boundary": "inventory-only",
            "passed": False,
            "blockers": ["fixture inventory has no external authority"],
        },
    }


def fake_binary(implementation: str, *, unsettled: bool = False) -> bytes:
    task = {
        "schema": "openprose.task-envelope/1",
        "argv": FORWARDED_TASK_ARGV,
        "interactionMode": "non-interactive",
    }
    result = {
        "schema": "openprose.runner-result/1",
        "runner": {"name": implementation, "version": VERSION, "commit": REVISION},
        "adapter": {"id": "mock/in-memory", "harnessVersion": "1.0.0"},
        "transport": "deterministic",
        "languageImage": {
            key: IMAGE[key] for key in ("formatVersion", "version", "sha256")
        },
        "digests": {
            "taskSha256": digest(canonical(task)),
            "deliveredImageSha256": IMAGE["sha256"],
        },
        "terminal": {
            "classification": "success",
            "transportCompleted": True,
            "terminalEventObserved": True,
        },
        "semantic": {"status": "not-applicable"},
        "billing": {"owner": "test-fixture", "authCategory": "none-test-only"},
        "runnerExitCode": 0,
    }
    unsettled_source = (
        "import subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        if unsettled
        else ""
    )
    return (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"expected = {['--harness', 'mock', '--output', 'json', *OPAQUE_ARGV]!r}\n"
        "if sys.argv[1:] != expected:\n"
        "    raise SystemExit(2)\n"
        f"{unsettled_source}"
        f"print(json.dumps({result!r}, sort_keys=True, separators=(',', ':')))\n"
    ).encode("utf-8")


def package_manifest(
    name: str, binary: bytes, platform_value: str
) -> dict[str, object]:
    system, cpu, *rest = platform_value.split("-")
    result: dict[str, object] = {
        "name": name,
        "version": VERSION,
        "os": [system],
        "cpu": [cpu],
        "openproseBinary": "bin/prose.exe" if system == "win32" else "bin/prose",
        "openproseBinaryByteLength": len(binary),
        "openproseBinarySha256": digest(binary),
        "openproseSourceRevision": REVISION,
        "openproseImage": IMAGE,
        "openproseCohort": npm_cohort(),
        "openprosePlatform": platform_value,
        "openproseBunCompileTarget": BENCHMARK.BUN_RUNTIME_BY_PLATFORM[platform_value][
            "compileTarget"
        ],
        "openproseBunRuntimeVariant": BENCHMARK.BUN_RUNTIME_BY_PLATFORM[platform_value][
            "runtimeVariant"
        ],
    }
    if rest:
        result["libc"] = ["glibc"]
        result.update(
            {
                "openproseMinimumGlibc": "2.34",
                "openproseRequiredGlibcMaximum": "2.34",
                "openproseLinuxExecutionEvidence": "ubuntu-22.04-only",
            }
        )
    return result


def npm_cohort(
    *,
    revision: str = REVISION,
    image: dict[str, object] = IMAGE,
    channel: str = "development",
) -> dict[str, object]:
    return {
        "schema": "openprose.npm-cohort/1",
        "version": VERSION,
        "sourceRevision": revision,
        "releaseChannel": channel,
        "purpose": image["purpose"],
        "image": image,
        "admittedPlatforms": list(BENCHMARK.SUPPORTED_PLATFORMS),
        "semanticStatus": "unverified",
        "releaseEligible": False,
        "publicationAuthorized": False,
    }


def refresh_evidence(output: Path) -> None:
    release_path = output / "release-manifest.json"
    release = json.loads(release_path.read_text("utf-8"))
    for artifact in release["artifacts"]:
        encoded = (output / artifact["path"]).read_bytes()
        artifact["byteLength"] = len(encoded)
        artifact["sha256"] = digest(encoded)
    dependency = (output / "dependency-evidence.json").read_bytes()
    try:
        dependency_value = json.loads(dependency)
    except json.JSONDecodeError:
        dependency_value = {}
    release["dependencyEvidence"] = {
        "path": "dependency-evidence.json",
        "byteLength": len(dependency),
        "sha256": digest(dependency),
        "releasePolicyPassed": False,
    }
    release_path.write_text(json.dumps(release, sort_keys=True), "utf-8")
    dependency_components = []
    groups = {
        "bun": "bun-cli",
        "cargo": "rust-cli",
        "windowsProcessHostCargo": "windows-process-host",
        "agentsSdkPython": "agents-sdk-python",
    }
    for inventory_name, inventory in dependency_value.get("inventories", {}).items():
        group = groups.get(inventory_name)
        if group is None:
            continue
        for package in inventory.get("packages", []):
            integrity = package["integrity"]
            component = {
                "type": "library",
                "bom-ref": "openprose:dependency:"
                + digest(
                    canonical(
                        [group, package["name"], package["version"], package["source"]]
                    )
                ),
                "group": group,
                "name": package["name"],
                "version": package["version"],
                "properties": [
                    {"name": "openprose:kind", "value": "resolved-dependency"},
                    {"name": "openprose:component", "value": group},
                    {"name": "openprose:source", "value": package["source"]},
                    {"name": "openprose:scopes", "value": ",".join(package["scopes"])},
                    {
                        "name": "openprose:integrity-status",
                        "value": integrity["status"],
                    },
                ],
            }
            if integrity["status"] == "declared":
                component["hashes"] = [
                    {
                        "alg": integrity["algorithm"].upper().replace("SHA", "SHA-"),
                        "content": integrity["digest"],
                    }
                ]
            dependency_components.append(component)
    (output / "sbom.cdx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "components": [
                    {
                        "type": "file",
                        "name": artifact["path"],
                        "hashes": [{"alg": "SHA-256", "content": artifact["sha256"]}],
                    }
                    for artifact in release["artifacts"]
                ]
                + dependency_components,
                "properties": [
                    {
                        "name": "openprose:dependency-inventory",
                        "value": "component-inventory-attached",
                    },
                    {
                        "name": "openprose:dependency-evidence-sha256",
                        "value": digest(dependency),
                    },
                    {
                        "name": "openprose:vulnerability-review",
                        "value": "not-performed",
                    },
                ],
            },
            sort_keys=True,
        ),
        "utf-8",
    )
    (output / "provenance.json").write_text(
        json.dumps(
            {
                "_type": "https://in-toto.io/Statement/v1",
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [
                    {"name": artifact["path"], "digest": {"sha256": artifact["sha256"]}}
                    for artifact in release["artifacts"]
                ],
                "predicate": {
                    "buildDefinition": {
                        "resolvedDependencies": [
                            {
                                "uri": "openprose:dependency-evidence",
                                "digest": {"sha256": digest(dependency)},
                            }
                        ]
                    }
                },
            },
            sort_keys=True,
        ),
        "utf-8",
    )
    if isinstance(release.get('agentsSdk'), dict):
        sdk = release['agentsSdk']
        sbom_path = output / 'sbom.cdx.json'; sbom = json.loads(sbom_path.read_text())
        sbom['components'].append({'type': 'file', 'name': 'prose-agents-sdk',
                                   'hashes': [{'alg': 'SHA-256', 'content': sdk['sha256']}]})
        import kernel_rc_evidence as custody
        native_receipts = []
        for artifact in release['artifacts']:
            if artifact['kind'] == 'standalone-archive':
                with tarfile.open(output / artifact['path']) as archive:
                    native_receipts = [json.loads(archive.extractfile(m).read()) for m in archive.getmembers()
                                       if m.name.endswith('/agents-sdk-build.json')]
                break
        if native_receipts:
            sbom['components'].extend(custody.sdk_native_sbom_components(native_receipts[0]))
        sbom_path.write_text(json.dumps(sbom))
        provenance_path = output / 'provenance.json'; provenance = json.loads(provenance_path.read_text())
        definition = provenance['predicate']['buildDefinition']
        definition['externalParameters'] = {'agentsSdk': sdk}
        definition['resolvedDependencies'] += [
            {'uri': 'openprose:agents-sdk-helper', 'digest': {'sha256': sdk['sha256']}},
            {'uri': 'openprose:agents-sdk-build-receipt', 'digest': {'sha256': sdk['receiptSha256']}},
            {'uri': 'openprose:agents-sdk-python-lock', 'digest': {'sha256': sdk['dependencyLockSha256']}}]
        provenance_path.write_text(json.dumps(provenance))
    paths = sorted(path for path in output.iterdir() if path.name != "SHA256SUMS")
    (output / "SHA256SUMS").write_text(
        "".join(f"{digest(path.read_bytes())}  {path.name}\n" for path in paths),
        "utf-8",
    )


def make_package_output(
    root: Path, *, unsettled_rust: bool = False, platform_value: str = PLATFORM
) -> Path:
    output = root / "packages"
    output.mkdir()
    rust = fake_binary("rust", unsettled=unsettled_rust)
    bun = fake_binary("bun")
    executable = "prose.exe" if platform_value.startswith("win32-") else "prose"
    sidecar = (
        b"MZ\x00provider-free-sidecar-fixture\n"
        if platform_value.startswith("win32-")
        else None
    )
    roots = {
        "rust": f"openprose-prose-cli-rust-{VERSION}-{platform_value}",
        "bun": f"openprose-prose-cli-bun-{VERSION}-{platform_value}",
    }
    for implementation, binary in (("rust", rust), ("bun", bun)):
        root_name = roots[implementation]
        archive_members: list[tuple[str, bytes, int | str]] = [
            (f"{root_name}/{executable}", binary, 0o755),
            (f"{root_name}/LICENSE", b"fixture license\n", 0o644),
            (f"{root_name}/README.txt", b"fixture package\n", 0o644),
            (
                f"{root_name}/examples/hello.prose.md",
                HELLO_EXAMPLE.read_bytes(),
                0o644,
            ),
        ]
        if sidecar is not None:
            archive_members.append(
                (f"{root_name}/openprose-windows-process-host.exe", sidecar, 0o755)
            )
        write_tar(output / f"{root_name}.tar.gz", archive_members)

    launcher = (ROOT / "cli" / "bun" / "npm" / "bin" / "prose.js").read_text("utf-8")
    launcher = launcher.replace(
        "__OPENPROSE_COHORT__",
        json.dumps(npm_cohort(), sort_keys=True, separators=(",", ":")),
    ).encode("utf-8")
    meta_manifest = {
        "name": "@openprose/prose-cli",
        "version": VERSION,
        "type": "commonjs",
        "bin": {"prose": "bin/prose.js"},
        "engines": {"node": ">=22.22.3"},
        "optionalDependencies": {
            f"@openprose/prose-cli-{name}": VERSION
            for name in BENCHMARK.SUPPORTED_PLATFORMS
        },
        "openproseCohort": npm_cohort(),
        "openproseLauncher": {
            "path": "bin/prose.js",
            "byteLength": len(launcher),
            "sha256": digest(launcher),
        },
    }
    write_tar(
        output / f"openprose-prose-cli-{VERSION}.tgz",
        [
            ("package/package.json", canonical(meta_manifest), 0o644),
            ("package/bin/prose.js", launcher, 0o755),
            ("package/LICENSE", b"fixture license\n", 0o644),
            ("package/README.md", b"fixture npm package\n", 0o644),
            ("package/examples/hello.prose.md", HELLO_EXAMPLE.read_bytes(), 0o644),
        ],
    )
    platform_name = f"@openprose/prose-cli-{platform_value}"
    platform_manifest = package_manifest(platform_name, bun, platform_value)
    if sidecar is not None:
        platform_manifest.update(
            {
                "openproseWindowsProcessHost": "bin/openprose-windows-process-host.exe",
                "openproseWindowsProcessHostByteLength": len(sidecar),
                "openproseWindowsProcessHostSha256": digest(sidecar),
                "openproseWindowsProcessHostAdmission": False,
            }
        )
    platform_members: list[tuple[str, bytes, int | str]] = [
        ("package/package.json", canonical(platform_manifest), 0o644),
        (f"package/bin/{executable}", bun, 0o755),
        ("package/LICENSE", b"fixture license\n", 0o644),
    ]
    if sidecar is not None:
        platform_members.append(
            ("package/bin/openprose-windows-process-host.exe", sidecar, 0o755)
        )
    write_tar(
        output / f"openprose-prose-cli-{platform_value}-{VERSION}.tgz",
        platform_members,
    )

    artifact_specs = [
        (f"{roots['rust']}.tar.gz", "standalone-archive", "rust", platform_value),
        (f"{roots['bun']}.tar.gz", "standalone-archive", "bun", platform_value),
        (f"openprose-prose-cli-{VERSION}.tgz", "npm-meta", "bun", None),
        (
            f"openprose-prose-cli-{platform_value}-{VERSION}.tgz",
            "npm-platform",
            "bun",
            platform_value,
        ),
    ]
    artifacts = []
    for name, kind, implementation, artifact_platform in artifact_specs:
        encoded = (output / name).read_bytes()
        artifacts.append(
            {
                "path": name,
                "kind": kind,
                "implementation": implementation,
                "platform": artifact_platform,
                "byteLength": len(encoded),
                "sha256": digest(encoded),
            }
        )
    release = {
        "schema": "openprose.local-release-manifest/1",
        "mode": "development",
        "version": VERSION,
        "platform": platform_value,
        "releaseEligible": False,
        "publicationAuthorized": False,
        "source": {"revision": REVISION, "verification": "matched-product-doctor"},
        "sourceDateEpoch": 0,
        "buildProfiles": {
            "rust": {"profile": "development", "testSeamsEnabled": True},
            "bun": {"profile": "development", "testSeamsEnabled": True},
        },
        "bunRuntime": BENCHMARK.BUN_RUNTIME_BY_PLATFORM[platform_value],
        "linuxRuntime": (
            {
                "minimumGlibc": "2.34",
                "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"},
                "executionEvidence": "ubuntu-22.04-only",
            }
            if platform_value.startswith("linux-")
            else "not-applicable"
        ),
        "image": IMAGE,
        "toolchains": {
            "python": "3.10.5",
            "rustc": "rustc 1.87.0",
            "cargo": "cargo 1.87.0",
            "bun": "1.3.5",
            "node": "v20.19.1",
            "npm": "10.0.0",
        },
        "lockfiles": {"cargoSha256": "a" * 64, "bunSha256": "b" * 64},
        "windowsProcessHost": (
            {
                "path": "openprose-windows-process-host.exe",
                "byteLength": len(sidecar),
                "sha256": digest(sidecar),
                "admission": False,
            }
            if sidecar is not None
            else "not-applicable"
        ),
        "windowsJobObjectReleaseAdmission": False,
        "externalGates": {
            "authorityValidatedByPackager": False,
            "canonicalProfile": "unavailable",
            "releaseEvidence": "unavailable",
        },
        "claims": {
            "signing": "not-performed",
            "vulnerabilityReview": "not-performed",
            "networkIsolation": "not-enforced",
            "packageTests": "not-run-by-packager",
        },
        "promotion": {
            "requiredAttestation": "protected-release-validator",
            "status": "not-performed",
        },
        "artifacts": artifacts,
    }
    (output / "release-manifest.json").write_text(
        json.dumps(release, sort_keys=True), "utf-8"
    )
    (output / "dependency-evidence.json").write_text(
        json.dumps(
            dependency_fixture(),
            sort_keys=True,
        ),
        "utf-8",
    )
    refresh_evidence(output)
    return output


def make_release_package_output(root: Path, *, platform_value: str = PLATFORM) -> Path:
    output = make_package_output(root, platform_value=platform_value)
    manifest_path = output / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["mode"] = "release"
    manifest["source"]["revision"] = RELEASE_REVISION
    manifest["buildProfiles"] = {
        "rust": {"profile": "release", "testSeamsEnabled": False},
        "bun": {"profile": "release", "testSeamsEnabled": False},
    }
    manifest["image"] = {
        **IMAGE,
        "version": "canonical-fixture-v1",
        "sha256": "c" * 64,
        "manifestSha256": "d" * 64,
        "purpose": "canonical-language-runtime",
        "releaseEligible": True,
    }
    manifest["externalGates"] = {
        "authorityValidatedByPackager": False,
        "canonicalProfile": {"byteLength": 17, "sha256": "e" * 64},
        "releaseEvidence": {"byteLength": 19, "sha256": "f" * 64},
    }
    release_cohort = npm_cohort(
        revision=RELEASE_REVISION,
        image=manifest["image"],
        channel="release-candidate",
    )
    launcher = (ROOT / "cli" / "bun" / "npm" / "bin" / "prose.js").read_text("utf-8")
    launcher = launcher.replace(
        "__OPENPROSE_COHORT__",
        json.dumps(release_cohort, sort_keys=True, separators=(",", ":")),
    ).encode("utf-8")

    meta_package = output / f"openprose-prose-cli-{VERSION}.tgz"

    def release_meta_manifest(members):
        for index, (name, data, mode) in enumerate(members):
            if name == "package/package.json":
                package = json.loads(data)
                package["openproseCohort"] = release_cohort
                package["openproseLauncher"] = {
                    "path": "bin/prose.js",
                    "byteLength": len(launcher),
                    "sha256": digest(launcher),
                }
                members[index] = (name, canonical(package), mode)
            elif name == "package/bin/prose.js":
                members[index] = (name, launcher, mode)

    rewrite_tar(meta_package, release_meta_manifest)

    platform_package = output / f"openprose-prose-cli-{platform_value}-{VERSION}.tgz"

    def release_platform_manifest(members):
        for index, (name, data, mode) in enumerate(members):
            if name == "package/package.json":
                package = json.loads(data)
                package["openproseSourceRevision"] = RELEASE_REVISION
                package["openproseImage"] = manifest["image"]
                package["openproseCohort"] = release_cohort
                members[index] = (name, canonical(package), mode)

    rewrite_tar(platform_package, release_platform_manifest)
    import sys
    ci_path = str(ROOT / 'cli/ci')
    if ci_path not in sys.path:
        sys.path.insert(0, ci_path)
    from test_kernel_rc_evidence import sdk_fixture
    sdk, sdk_table = sdk_fixture(platform_value)
    sdk_members = [(n, d, m) for n, (d, m) in sdk_table["files"].items()]
    sdk_members += [(n, b"", ("directory", m)) for n, m in sdk_table["directories"].items()]
    sdk_members += [(n, b"", target) for n, target in sdk_table["symlinks"].items()]
    manifest['agentsSdk'] = sdk
    for artifact in manifest['artifacts']:
        if artifact['kind'] in ('standalone-archive', 'npm-platform'):
            def append_sdk(members):
                cli_name = next(n for n, d, m in members if n.endswith('/prose') or n.endswith('/prose.exe'))
                prefix = cli_name.rsplit('/', 1)[0] + '/'
                members.extend((prefix + n, d, m) for n, d, m in sdk_members)
            rewrite_tar(output / artifact['path'], append_sdk)
    dependency_path = output / 'dependency-evidence.json'; dependency = json.loads(dependency_path.read_text())
    dependency['sources'].append({'path': 'harnesses/agents-sdk/requirements-build.txt', 'byteLength': 1, 'sha256': sdk['dependencyLockSha256']})
    dependency['inventories']['agentsSdkPython'] = {
        'scopeBasis': 'hash-locked-four-platform-wheel-candidates',
        'packages': [{'name': 'fixture', 'version': '1.0.0', 'source': 'pypi:wheel-sha256:' + 'e'*64,
                      'scopes': ['frozen-sdk-build'], 'integrity': {'status': 'declared', 'algorithm': 'sha256', 'digest': 'e'*64}}]}
    dependency_path.write_text(json.dumps(dependency))
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), "utf-8")
    refresh_evidence(output)
    return output


def make_ordinary_package_output(root: Path, *, platform_value: str = PLATFORM) -> Path:
    output = make_package_output(root, platform_value=platform_value)
    manifest_path = output / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["buildProfiles"] = {
        "rust": {"profile": "development", "testSeamsEnabled": False},
        "bun": {"profile": "development", "testSeamsEnabled": False},
    }
    manifest["image"] = ECHO_IMAGE
    cohort = npm_cohort(image=ECHO_IMAGE, channel="development")
    launcher = (ROOT / "cli" / "bun" / "npm" / "bin" / "prose.js").read_text("utf-8")
    launcher = launcher.replace(
        "__OPENPROSE_COHORT__",
        json.dumps(cohort, sort_keys=True, separators=(",", ":")),
    ).encode("utf-8")

    meta_package = output / f"openprose-prose-cli-{VERSION}.tgz"

    def ordinary_meta_manifest(members):
        for index, (name, data, mode) in enumerate(members):
            if name == "package/package.json":
                package = json.loads(data)
                package["openproseCohort"] = cohort
                package["openproseLauncher"] = {
                    "path": "bin/prose.js",
                    "byteLength": len(launcher),
                    "sha256": digest(launcher),
                }
                members[index] = (name, canonical(package), mode)
            elif name == "package/bin/prose.js":
                members[index] = (name, launcher, mode)

    rewrite_tar(meta_package, ordinary_meta_manifest)

    platform_package = output / f"openprose-prose-cli-{platform_value}-{VERSION}.tgz"

    def ordinary_platform_manifest(members):
        for index, (name, data, mode) in enumerate(members):
            if name == "package/package.json":
                package = json.loads(data)
                package["openproseImage"] = ECHO_IMAGE
                package["openproseCohort"] = cohort
                members[index] = (name, canonical(package), mode)

    rewrite_tar(platform_package, ordinary_platform_manifest)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), "utf-8")
    refresh_evidence(output)
    return output


def rewrite_tar(path: Path, mutate) -> None:
    members: list[tuple[str, bytes, int | str]] = []
    with tarfile.open(path, "r:gz") as archive:
        for info in archive.getmembers():
            if info.isfile():
                extracted = archive.extractfile(info)
                assert extracted is not None
                members.append((info.name, extracted.read(), info.mode))
            elif info.issym():
                members.append((info.name, b"", info.linkname))
            elif info.isdir():
                members.append((info.name, b"", ("directory", info.mode)))
    mutate(members)
    write_tar(path, members)


class PythonSdkDependencyValidationTests(unittest.TestCase):
    @staticmethod
    def generated_report():
        script = ROOT / "cli/ci/dependency_evidence.py"
        spec = importlib.util.spec_from_file_location("sdk_generated_dependency_evidence", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.build_report(ROOT)

    def test_original_generated_sdk_inventory_is_admitted_without_rewriting(self):
        report = self.generated_report()
        before = copy.deepcopy(report)
        BENCHMARK.validate_dependency_evidence(report)
        self.assertEqual(report, before)
        # Exercise the lifecycle's separate source/hash/SBOM verifier, without a build.
        script = ROOT / "cli/ci/package_local.py"
        spec = importlib.util.spec_from_file_location("sdk_dependency_package_local", script)
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)
        encoded, independently_verified = packager.dependency_evidence()
        self.assertEqual(json.loads(encoded), report)
        self.assertEqual(independently_verified, report)
        BENCHMARK.validate_dependency_evidence(independently_verified)
        inventory = report["inventories"]["agentsSdkPython"]
        self.assertEqual(set(inventory), {"scopeBasis", "packages"})
        versions = {package["version"] for package in inventory["packages"]}
        self.assertTrue({"3.20", "26.3", "3.0", "2026.8"}.issubset(versions))

    def test_python_evidence_remains_closed_and_hash_bound(self):
        report = self.generated_report()
        for poison in ("lock-version", "scope-basis", "version", "source", "digest", "scopes", "integrity", "order", "duplicate"):
            with self.subTest(poison=poison):
                value = copy.deepcopy(report)
                inventory = value["inventories"]["agentsSdkPython"]
                package = inventory["packages"][0]
                if poison == "lock-version":
                    inventory["lockfileVersion"] = 1
                elif poison == "scope-basis":
                    inventory["scopeBasis"] = "unverified"
                elif poison == "version":
                    package["version"] = "3..20"
                elif poison == "source":
                    package["source"] = "pypi:wheel-sha256:" + "0" * 64
                elif poison == "digest":
                    package["integrity"]["digest"] = "0" * 63
                elif poison == "scopes":
                    package["scopes"] = ["runtime"]
                elif poison == "integrity":
                    package["integrity"] = {"status": "not-applicable", "reason": "missing"}
                elif poison == "order":
                    inventory["packages"].reverse()
                else:
                    inventory["packages"].append(copy.deepcopy(package))
                with self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.validate_dependency_evidence(value)

    def test_python_versions_do_not_weaken_cargo_or_bun_semver(self):
        for inventory_name in ("bun", "cargo", "windowsProcessHostCargo"):
            with self.subTest(inventory=inventory_name):
                value = dependency_fixture()
                value["inventories"][inventory_name]["packages"][0]["version"] = "3.20"
                with self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.validate_dependency_evidence(value)


class InstalledPackageBenchmarkTests(unittest.TestCase):
    def test_release_cannot_omit_sdk_identity_after_rehashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = make_release_package_output(Path(temporary))
            path = output / 'release-manifest.json'; manifest = json.loads(path.read_text())
            manifest.pop('agentsSdk'); path.write_text(json.dumps(manifest)); refresh_evidence(output)
            with self.assertRaisesRegex(BENCHMARK.BenchmarkError, 'SDK identity'):
                BENCHMARK.verify_package_output(output, purpose='release-invariants')

    def test_package_verification_purpose_separates_mock_and_release_invariants(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            development_root = root / "development"
            development_root.mkdir()
            development = make_package_output(development_root)
            BENCHMARK.verify_package_output(development)
            with self.assertRaises(BENCHMARK.BenchmarkError) as mismatch:
                BENCHMARK.verify_package_output(
                    development, purpose="release-invariants"
                )
            self.assertEqual(mismatch.exception.code, "PURPOSE_MISMATCH")

            release_root = root / "release"
            release_root.mkdir()
            release = make_release_package_output(release_root)
            context = BENCHMARK.verify_package_output(
                release, purpose="release-invariants"
            )
            self.assertEqual(context["release"]["mode"], "release")
            self.assertEqual(context["purpose"], "release-invariants")
            BENCHMARK.assert_package_output_unchanged(context)
            with self.assertRaises(BENCHMARK.BenchmarkError) as mock_refusal:
                BENCHMARK.verify_package_output(release)
            self.assertEqual(mock_refusal.exception.code, "PURPOSE_MISMATCH")

            with self.assertRaises(BENCHMARK.BenchmarkError) as unsupported:
                BENCHMARK.verify_package_output(release, purpose="weakened")
            self.assertEqual(unsupported.exception.code, "ARGUMENT_INVALID")

    def test_ordinary_development_verification_is_exact_and_never_mock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ordinary_root = root / "ordinary"
            ordinary_root.mkdir()
            ordinary = make_ordinary_package_output(ordinary_root)
            context = BENCHMARK.verify_package_output(
                ordinary, purpose="ordinary-development"
            )
            self.assertEqual(context["release"]["image"]["version"], "echo-v0")
            self.assertEqual(
                context["release"]["buildProfiles"],
                {
                    "rust": {
                        "profile": "development",
                        "testSeamsEnabled": False,
                    },
                    "bun": {
                        "profile": "development",
                        "testSeamsEnabled": False,
                    },
                },
            )
            with self.assertRaises(BENCHMARK.BenchmarkError) as mock_refusal:
                BENCHMARK.verify_package_output(ordinary)
            self.assertEqual(mock_refusal.exception.code, "IDENTITY_DIVERGENCE")

            mock_root = root / "mock"
            mock_root.mkdir()
            mock_package = make_package_output(mock_root)
            with self.assertRaises(BENCHMARK.BenchmarkError) as ordinary_refusal:
                BENCHMARK.verify_package_output(
                    mock_package, purpose="ordinary-development"
                )
            self.assertEqual(ordinary_refusal.exception.code, "IDENTITY_DIVERGENCE")

    def test_package_output_and_archive_member_enumeration_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            package.mkdir()
            for index in range(BENCHMARK.MAX_PACKAGE_OUTPUT_ENTRIES + 1):
                (package / f"member-{index:04d}").write_bytes(b"x")
            with self.assertRaisesRegex(BENCHMARK.BenchmarkError, "too many members"):
                BENCHMARK.verify_package_output(package, PLATFORM)

            archive = root / "too-many.tar.gz"
            write_tar(
                archive,
                [
                    (f"member-{index:04d}", b"x", 0o644)
                    for index in range(BENCHMARK.MAX_MEMBERS + 1)
                ],
            )
            with self.assertRaisesRegex(BENCHMARK.BenchmarkError, "more than"):
                BENCHMARK.read_archive_members(archive)

    def require_tools(self) -> None:
        if (
            BENCHMARK.resolve_tool("npm") is None
            or BENCHMARK.resolve_tool("node") is None
        ):
            self.skipTest(
                "npm and node are required for the installed-package benchmark"
            )

    def test_full_package_install_and_three_surface_measurement(self) -> None:
        self.require_tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            before = {
                path.name: digest(path.read_bytes()) for path in packages.iterdir()
            }
            final_reauthentications = []
            original_verify = BENCHMARK.verify_retained_install_trees

            def record_final_reauthentication(install_root, candidate_report):
                result = original_verify(install_root, candidate_report)
                final_reauthentications.append(result)
                return result

            BENCHMARK.verify_retained_install_trees = record_final_reauthentication
            try:
                report = BENCHMARK.run_benchmark(
                    packages, root / "install", trials=2, timeout_seconds=5
                )
            finally:
                BENCHMARK.verify_retained_install_trees = original_verify
            self.assertEqual(len(final_reauthentications), 1)
            retained = BENCHMARK.verify_retained_install_trees(root / "install", report)
            rust_root = root / "install" / "rust-standalone"
            rust_member_root = next(rust_root.iterdir())
            readme = rust_member_root / "README.txt"
            original_readme = readme.read_bytes()
            original_readme_mode = readme.stat().st_mode & 0o777
            readme.write_bytes(original_readme + b"post-capture mutation")
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_retained_install_trees(root / "install", report)
            readme.write_bytes(original_readme)
            readme.chmod(original_readme_mode)
            extra = rust_member_root / "added-zero"
            extra.write_bytes(b"")
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_retained_install_trees(root / "install", report)
            extra.unlink()
            self.assertEqual(
                BENCHMARK.verify_retained_install_trees(root / "install", report),
                retained,
            )
            after = {
                path.name: digest(path.read_bytes()) for path in packages.iterdir()
            }
        self.assertEqual(before, after)
        self.assertEqual(report["schema"], "openprose.installed-package-benchmark/1")
        self.assertEqual(
            report["limitations"]["detachedDescendantContainment"],
            "not-enforced",
        )
        self.assertEqual(report["limitations"]["semanticEvaluation"], "not-performed")
        self.assertEqual(
            report["limitations"]["portabilityEvaluation"], "not-performed"
        )
        self.assertEqual(report["limitations"]["releaseEvaluation"], "not-performed")
        self.assertEqual(report["limitations"]["ranking"], "not-produced")
        self.assertEqual(len(report["installations"]), 3)
        self.assertTrue(
            all(item["installedByteCount"] > 0 for item in report["installations"])
        )
        self.assertEqual(
            retained,
            {
                item["surface"]: item["treeIdentity"]["digestSha256"]
                for item in report["installations"]
            },
        )
        self.assertTrue(
            all(
                item["treeIdentity"]["byteCount"] == item["installedByteCount"]
                for item in report["installations"]
            )
        )
        self.assertEqual(len(report["invocations"]), 6)
        self.assertEqual(
            {item["surface"] for item in report["invocations"]},
            {"direct-rust", "direct-bun", "npm-launcher"},
        )
        self.assertTrue(
            all(item["settlement"] == "settled" for item in report["invocations"])
        )
        self.assertTrue(
            all(
                item["settlementAuthority"]
                == "direct-and-original-process-group-settled"
                for item in report["invocations"]
            )
        )
        self.assertTrue(
            all(
                item["transportValidation"] == "passed"
                for item in report["invocations"]
            )
        )
        self.assertTrue(report["launcherResolution"]["matchesStandaloneBun"])
        self.assertEqual(
            report["launcherResolution"]["packagedBinarySha256"],
            report["surfaces"]["direct-bun"]["binarySha256"],
        )
        self.assertEqual(report["measurementPlan"]["trials"], 2)
        self.assertEqual(
            report["measurementPlan"]["surfaceOrder"],
            ["direct-rust", "direct-bun", "npm-launcher"],
        )
        self.assertNotIn("winner", json.dumps(report).lower())

    def test_cli_report_and_reanalysis_are_machine_readable_and_deterministic(
        self,
    ) -> None:
        self.require_tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "run",
                    "--packages",
                    str(packages),
                    "--install-root",
                    str(root / "install"),
                    "--trials",
                    "1",
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stderr, b"")
            raw = root / "raw.json"
            raw.write_bytes(completed.stdout)
            first = subprocess.run(
                [sys.executable, str(SCRIPT), "analyse", "--report", str(raw)],
                capture_output=True,
                check=False,
                timeout=10,
            )
            second = subprocess.run(
                [sys.executable, str(SCRIPT), "analyse", "--report", str(raw)],
                capture_output=True,
                check=False,
                timeout=10,
            )
            weakened = json.loads(completed.stdout)
            weakened["limitations"]["ranking"] = "winner-selected"
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.analyse_report(weakened)
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        self.assertEqual(first.stdout, second.stdout)
        analysis = json.loads(first.stdout)
        self.assertEqual(
            analysis["schema"], "openprose.installed-package-benchmark-analysis/1"
        )
        self.assertEqual(
            analysis["authority"],
            {
                "reportIdentity": "capture-bound",
                "packageReauthentication": "not-performed",
                "packageAndInstallDirectoriesRequiredForReauthentication": True,
            },
        )
        self.assertNotIn("winner", first.stdout.decode().lower())

    def test_tamper_extra_member_and_malformed_evidence_fail_closed(self) -> None:
        mutations = (
            "tamper",
            "extra",
            "malformed-evidence",
            "duplicate-checksum",
            "policy-weakening",
            "missing-dependency",
            "dependency-release-binding",
            "dependency-sbom-binding",
            "dependency-provenance-binding",
            "dependency-source-duplicate",
            "dependency-sbom-component",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    packages = make_package_output(root)
                    if mutation == "tamper":
                        artifact = next(packages.glob("*rust*.tar.gz"))
                        artifact.write_bytes(artifact.read_bytes() + b"tamper")
                    elif mutation == "extra":
                        (packages / "unexpected.bin").write_bytes(b"extra")
                        refresh_evidence(packages)
                    elif mutation == "missing-dependency":
                        (packages / "dependency-evidence.json").unlink()
                        sums = packages / "SHA256SUMS"
                        sums.write_text(
                            "".join(
                                line + "\n"
                                for line in sums.read_text("utf-8").splitlines()
                                if not line.endswith("  dependency-evidence.json")
                            ),
                            "utf-8",
                        )
                    else:
                        if mutation == "malformed-evidence":
                            (packages / "dependency-evidence.json").write_text(
                                "{}", "utf-8"
                            )
                            refresh_evidence(packages)
                        elif mutation == "duplicate-checksum":
                            sums = packages / "SHA256SUMS"
                            first = sums.read_text("utf-8").splitlines()[0]
                            sums.write_text(
                                sums.read_text("utf-8") + first + "\n", "utf-8"
                            )
                        elif mutation == "dependency-release-binding":
                            refresh_evidence(packages)
                            release_path = packages / "release-manifest.json"
                            release = json.loads(release_path.read_text("utf-8"))
                            release["dependencyEvidence"]["byteLength"] += 1
                            release_path.write_text(json.dumps(release), "utf-8")
                            paths = sorted(
                                p for p in packages.iterdir() if p.name != "SHA256SUMS"
                            )
                            (packages / "SHA256SUMS").write_text(
                                "".join(
                                    f"{digest(p.read_bytes())}  {p.name}\n"
                                    for p in paths
                                ),
                                "utf-8",
                            )
                        elif mutation == "dependency-sbom-binding":
                            refresh_evidence(packages)
                            sbom_path = packages / "sbom.cdx.json"
                            sbom = json.loads(sbom_path.read_text("utf-8"))
                            sbom["properties"][1]["value"] = "0" * 64
                            sbom_path.write_text(json.dumps(sbom), "utf-8")
                            paths = sorted(
                                p for p in packages.iterdir() if p.name != "SHA256SUMS"
                            )
                            (packages / "SHA256SUMS").write_text(
                                "".join(
                                    f"{digest(p.read_bytes())}  {p.name}\n"
                                    for p in paths
                                ),
                                "utf-8",
                            )
                        elif mutation == "dependency-provenance-binding":
                            refresh_evidence(packages)
                            provenance_path = packages / "provenance.json"
                            provenance = json.loads(provenance_path.read_text("utf-8"))
                            provenance["predicate"]["buildDefinition"][
                                "resolvedDependencies"
                            ] = []
                            provenance_path.write_text(json.dumps(provenance), "utf-8")
                            paths = sorted(
                                p for p in packages.iterdir() if p.name != "SHA256SUMS"
                            )
                            (packages / "SHA256SUMS").write_text(
                                "".join(
                                    f"{digest(p.read_bytes())}  {p.name}\n"
                                    for p in paths
                                ),
                                "utf-8",
                            )
                        elif mutation == "dependency-source-duplicate":
                            dependency_path = packages / "dependency-evidence.json"
                            dependency = json.loads(dependency_path.read_text("utf-8"))
                            dependency["sources"].append(dependency["sources"][0])
                            dependency_path.write_text(json.dumps(dependency), "utf-8")
                            refresh_evidence(packages)
                        elif mutation == "dependency-sbom-component":
                            refresh_evidence(packages)
                            sbom_path = packages / "sbom.cdx.json"
                            sbom = json.loads(sbom_path.read_text("utf-8"))
                            sbom["components"] = [
                                component
                                for component in sbom["components"]
                                if component.get("type") != "library"
                            ]
                            sbom_path.write_text(json.dumps(sbom), "utf-8")
                            paths = sorted(
                                p for p in packages.iterdir() if p.name != "SHA256SUMS"
                            )
                            (packages / "SHA256SUMS").write_text(
                                "".join(
                                    f"{digest(p.read_bytes())}  {p.name}\n"
                                    for p in paths
                                ),
                                "utf-8",
                            )
                        else:
                            release_path = packages / "release-manifest.json"
                            release = json.loads(release_path.read_text("utf-8"))
                            release["publicationAuthorized"] = True
                            release_path.write_text(json.dumps(release), "utf-8")
                            refresh_evidence(packages)
                    with self.assertRaises(BENCHMARK.BenchmarkError):
                        BENCHMARK.verify_package_output(packages)

    def test_reanalysis_rejects_structural_identity_and_metric_mutations(self) -> None:
        self.require_tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = BENCHMARK.run_benchmark(
                make_package_output(root), root / "install", trials=2, timeout_seconds=5
            )
        mutations = []

        def changed(change):
            value = copy.deepcopy(report)
            change(value)
            mutations.append(value)

        def reseal_tree(tree):
            tree["entries"].sort(key=lambda entry: entry["path"])
            tree["entryCount"] = len(tree["entries"])
            tree["directoryCount"] = sum(
                entry["type"] == "directory" for entry in tree["entries"]
            )
            tree["regularFileCount"] = sum(
                entry["type"] == "regular" for entry in tree["entries"]
            )
            tree["symlinkCount"] = sum(
                entry["type"] == "symlink" for entry in tree["entries"]
            )
            tree["byteCount"] = sum(
                entry.get("byteLength", 0)
                for entry in tree["entries"]
                if entry["type"] == "regular"
            )
            tree["digestSha256"] = digest(
                BENCHMARK.canonical_json(
                    {"schema": tree["schema"], "entries": tree["entries"]}
                )
            )

        def add_resealed_zero_file(value):
            tree = value["installations"][0]["treeIdentity"]
            tree["entries"].append(
                {
                    "path": "unexpected-empty",
                    "type": "regular",
                    "mode": 0o644,
                    "byteLength": 0,
                    "sha256": digest(b""),
                }
            )
            reseal_tree(tree)

        changed(lambda value: value.__setitem__("unexpected", True))
        changed(lambda value: value.pop("artifacts"))
        changed(lambda value: value["surfaces"].pop("direct-rust"))
        changed(lambda value: value["surfaces"].__setitem__("extra", {}))
        changed(
            lambda value: value["installations"].append(
                copy.deepcopy(value["installations"][0])
            )
        )
        changed(lambda value: value["installations"][0].__setitem__("wallMs", -1))
        changed(
            lambda value: value["installations"][0].__setitem__(
                "installedByteCount", -1
            )
        )
        changed(
            lambda value: value["installations"][0]["treeIdentity"]["entries"].append(
                {
                    "path": "unexpected-empty",
                    "type": "regular",
                    "mode": 0o644,
                    "byteLength": 0,
                    "sha256": digest(b""),
                }
            )
        )
        changed(add_resealed_zero_file)
        changed(
            lambda value: value["installations"][0]["treeIdentity"].__setitem__(
                "digestSha256", "0" * 64
            )
        )
        changed(
            lambda value: value["installations"][0]["treeIdentity"].__setitem__(
                "unexpected", True
            )
        )
        changed(
            lambda value: value["installations"][0]["treeIdentity"].__setitem__(
                "entryCount", True
            )
        )
        changed(lambda value: value["invocations"][0].__setitem__("wallMs", math.nan))
        changed(lambda value: value["invocations"].pop())
        changed(lambda value: value["invocations"].__delitem__(slice(-3, None)))
        changed(lambda value: value["invocations"][0].__setitem__("ordinal", 1))
        changed(lambda value: value["invocations"][0].__setitem__("ordinal", False))
        changed(
            lambda value: value["invocations"][0].__setitem__("settlement", "unsettled")
        )
        changed(
            lambda value: value["invocations"][0].__setitem__(
                "transportValidation", "skipped"
            )
        )
        changed(lambda value: value["invocations"][0].__setitem__("exitCode", 1))
        changed(lambda value: value["invocations"][0].__setitem__("exitCode", False))
        changed(lambda value: value["task"].__setitem__("taskSha256", "0" * 64))
        changed(
            lambda value: value["invocations"][0]["observedIdentity"].__setitem__(
                "runner", "other"
            )
        )
        changed(
            lambda value: value["surfaces"]["direct-rust"].__setitem__(
                "packageArtifactSha256", "0" * 64
            )
        )
        changed(
            lambda value: value["surfaces"]["direct-rust"].__setitem__(
                "binarySha256", "0" * 64
            )
        )
        changed(
            lambda value: next(
                item
                for item in value["evidence"]
                if item["path"] == "dependency-evidence.json"
            ).__setitem__("sha256", "0" * 64)
        )
        changed(
            lambda value: value["installations"][2]["installer"]["argv"].extend(
                ["--foreground-scripts", "$INSTALL_ROOT/extra.tgz"]
            )
        )
        changed(
            lambda value: value["installations"][2]["installer"].__setitem__(
                "sha256", "0" * 64
            )
        )
        changed(
            lambda value: value["invocations"][0]["command"].__setitem__(
                "executable", "$INSTALL_ROOT/attacker/prose"
            )
        )
        changed(
            lambda value: value["surfaces"]["npm-launcher"].__setitem__(
                "launcherCommand", "$INSTALL_ROOT/attacker/prose"
            )
        )
        changed(lambda value: value["measurementPlan"].__setitem__("unexpected", True))
        changed(lambda value: value["measurementPlan"].__setitem__("trials", 1))

        def rewrite_version(value):
            value["packageIdentity"]["version"] = "9.9.9"
            for invocation in value["invocations"]:
                invocation["observedIdentity"]["runner"]["version"] = "9.9.9"

        def rewrite_revision(value):
            value["packageIdentity"]["sourceRevision"] = "forged-revision"
            for invocation in value["invocations"]:
                invocation["observedIdentity"]["runner"]["commit"] = "forged-revision"

        def rewrite_image(value):
            value["task"]["image"]["sha256"] = "0" * 64
            for invocation in value["invocations"]:
                invocation["observedIdentity"]["image"]["sha256"] = "0" * 64

        changed(rewrite_version)
        changed(rewrite_revision)
        changed(rewrite_image)
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.analyse_report(mutation)

    def test_windows_measurement_refuses_before_any_spawn_without_job_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root, platform_value="win32-x64")
            original = BENCHMARK.current_platform_id
            original_run = BENCHMARK.run_owned_process
            calls = []
            BENCHMARK.current_platform_id = lambda: "win32-x64"
            BENCHMARK.run_owned_process = lambda *args, **kwargs: calls.append(args)
            try:
                with self.assertRaisesRegex(BENCHMARK.BenchmarkError, "Job Object"):
                    BENCHMARK.run_benchmark(packages, root / "install", trials=1)
            finally:
                BENCHMARK.current_platform_id = original
                BENCHMARK.run_owned_process = original_run
            self.assertEqual(calls, [])

    def test_archive_traversal_symlink_duplicate_and_binary_divergence_fail(
        self,
    ) -> None:
        mutations = ("traversal", "symlink", "duplicate", "divergence")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    packages = make_package_output(root)
                    if mutation in {"traversal", "symlink", "duplicate"}:
                        artifact = next(packages.glob("*rust*.tar.gz"))

                        def mutate(members):
                            if mutation == "traversal":
                                members.append(("../escape", b"no", 0o644))
                            elif mutation == "symlink":
                                members.append(("linked", b"", "/tmp/outside"))
                            else:
                                members.append(members[0])

                        rewrite_tar(artifact, mutate)
                    else:
                        platform_package = (
                            packages / f"openprose-prose-cli-{PLATFORM}-{VERSION}.tgz"
                        )

                        def mutate(members):
                            for index, (name, data, mode) in enumerate(members):
                                if name.endswith("/prose") or name.endswith(
                                    "/prose.exe"
                                ):
                                    changed = data + b"# divergence\n"
                                    members[index] = (name, changed, mode)
                                elif name == "package/package.json":
                                    manifest = json.loads(data)
                                    changed_binary = (
                                        fake_binary("bun") + b"# divergence\n"
                                    )
                                    manifest["openproseBinaryByteLength"] = len(
                                        changed_binary
                                    )
                                    manifest["openproseBinarySha256"] = digest(
                                        changed_binary
                                    )
                                    members[index] = (name, canonical(manifest), mode)

                        rewrite_tar(platform_package, mutate)
                    refresh_evidence(packages)
                    with self.assertRaises(BENCHMARK.BenchmarkError):
                        BENCHMARK.run_benchmark(
                            packages, root / "install", trials=1, timeout_seconds=2
                        )
                    self.assertFalse((root / "escape").exists())

    def test_schema_two_capable_launcher_retains_exact_template_boundary(self) -> None:
        template = (ROOT / "cli/bun/npm/bin/prose.js").read_bytes()
        self.assertEqual(hashlib.sha256(template).hexdigest(), BENCHMARK.CANONICAL_LAUNCHER_TEMPLATE_SHA256)
        cohort = {"schema": "openprose.npm-cohort/1", "version": "0.1.0"}
        bound = template.replace(b"__OPENPROSE_COHORT__", json.dumps(cohort, sort_keys=True, separators=(",", ":")).encode())
        BENCHMARK.validate_canonical_launcher(bound, cohort)
        with self.assertRaises(BENCHMARK.BenchmarkError):
            BENCHMARK.validate_canonical_launcher(bound + b"\n", cohort)

    def test_opaque_archive_suffix_and_noncanonical_launcher_fail_closed(self) -> None:
        mutations = ("gzip-suffix", "tar-suffix", "launcher-byte")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    packages = make_package_output(root)
                    if mutation in {"gzip-suffix", "tar-suffix"}:
                        artifact = next(packages.glob("*rust*.tar.gz"))
                        if mutation == "gzip-suffix":
                            artifact.write_bytes(artifact.read_bytes() + b"opaque")
                        else:
                            decoded = gzip.decompress(artifact.read_bytes()) + b"opaque"
                            artifact.write_bytes(gzip.compress(decoded, mtime=0))
                    else:
                        meta = packages / f"openprose-prose-cli-{VERSION}.tgz"

                        def mutate(members):
                            for index, (name, data, mode) in enumerate(members):
                                if name == "package/bin/prose.js":
                                    members[index] = (name, data + b"\n", mode)

                        rewrite_tar(meta, mutate)
                    refresh_evidence(packages)
                    with self.assertRaises(BENCHMARK.BenchmarkError):
                        BENCHMARK.run_benchmark(
                            packages, root / "install", trials=1, timeout_seconds=2
                        )

    def test_release_manifest_closure_and_semver_prerelease_rules(self) -> None:
        self.assertTrue(BENCHMARK.is_exact_semver("1.2.3-alpha.1+build.01"))
        self.assertFalse(BENCHMARK.is_exact_semver("1.2.3-01"))
        self.assertFalse(BENCHMARK.is_exact_semver("1.2.3-alpha.01"))
        mutations = ("top-level", "source", "claims")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    packages = make_package_output(root)
                    release_path = packages / "release-manifest.json"
                    release = json.loads(release_path.read_text("utf-8"))
                    if mutation == "top-level":
                        release["unexpected"] = True
                    elif mutation == "source":
                        release["source"]["unexpected"] = True
                    else:
                        release["claims"]["signing"] = "performed"
                    release_path.write_text(json.dumps(release), "utf-8")
                    refresh_evidence(packages)
                    with self.assertRaises(BENCHMARK.BenchmarkError):
                        BENCHMARK.verify_package_output(packages)

    def test_resolved_tool_command_is_the_hashed_executable(self) -> None:
        for name in ("node", "npm"):
            with self.subTest(name=name):
                tool = BENCHMARK.resolve_tool(name)
                if tool is None:
                    self.skipTest(f"{name} is unavailable")
                self.assertEqual(tool["command"], tool["resolvedPath"])
                self.assertEqual(
                    digest(Path(tool["command"]).read_bytes()), tool["sha256"]
                )

    @unittest.skipIf(
        os.name == "nt", "portable tree custody fixture uses POSIX symlinks"
    )
    def test_tree_identity_is_closed_bounded_and_detects_every_retained_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tree"
            target = root / "lib" / "launcher.js"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"launcher\n")
            target.chmod(0o755)
            empty = root / "empty"
            empty.write_bytes(b"")
            empty.chmod(0o640)
            link = root / "bin" / "prose"
            link.parent.mkdir()
            link.symlink_to(Path("../lib/launcher.js"))
            allowed = {link: target}
            identity = BENCHMARK.capture_installed_tree(root, allowed)
            self.assertEqual(identity["entryCount"], 5)
            self.assertEqual(identity["regularFileCount"], 2)
            self.assertEqual(identity["directoryCount"], 2)
            self.assertEqual(identity["symlinkCount"], 1)
            self.assertEqual(identity["byteCount"], len(b"launcher\n"))
            self.assertEqual(identity["entries"][-1]["path"], "lib/launcher.js")
            BENCHMARK.validate_installed_tree_identity(identity, "fixture tree")
            self.assertEqual(
                BENCHMARK.verify_installed_tree(root, identity, allowed),
                identity["digestSha256"],
            )

            added = root / "added-zero"
            added.write_bytes(b"")
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_installed_tree(root, identity, allowed)
            added.unlink()

            renamed = root / "renamed"
            empty.rename(renamed)
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_installed_tree(root, identity, allowed)
            renamed.rename(empty)

            original_mode = target.stat().st_mode & 0o777
            target.chmod(0o644)
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_installed_tree(root, identity, allowed)
            target.chmod(original_mode)

            target.write_bytes(b"mutated\n")
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_installed_tree(root, identity, allowed)
            target.write_bytes(b"launcher\n")
            target.chmod(original_mode)

            escape = root / "escape"
            escape.symlink_to(Path("/tmp"))
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_installed_tree(root, identity, allowed)
            escape.unlink()

            with mock.patch.object(BENCHMARK, "MAX_INSTALLED_TREE_ENTRIES", 1):
                with self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.capture_installed_tree(root, allowed)
            with mock.patch.object(BENCHMARK, "MAX_INSTALLED_TREE_FILE_BYTES", 1):
                with self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.capture_installed_tree(root, allowed)

    def test_tree_regular_file_concurrent_read_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "member"
            path.write_bytes(b"stable")
            original_fstat = BENCHMARK.os.fstat
            calls = 0

            def changed_fstat(descriptor):
                nonlocal calls
                calls += 1
                observed = original_fstat(descriptor)
                values = {
                    name: getattr(observed, name)
                    for name in (
                        "st_mode",
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                }
                if calls == 2:
                    values["st_mtime_ns"] += 1
                return types.SimpleNamespace(**values)

            with mock.patch.object(BENCHMARK.os, "fstat", side_effect=changed_fstat):
                with self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.capture_installed_tree(Path(temporary))

    def test_verified_package_set_mutation_is_detected_before_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            context = BENCHMARK.verify_package_output(packages)
            artifact = next(packages.glob("*rust*.tar.gz"))
            artifact.write_bytes(artifact.read_bytes() + b"changed-after-verification")
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.assert_package_output_unchanged(context)

    def test_lifecycle_script_and_existing_install_root_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            meta = packages / f"openprose-prose-cli-{VERSION}.tgz"

            def mutate(members):
                for index, (name, data, mode) in enumerate(members):
                    if name == "package/package.json":
                        manifest = json.loads(data)
                        manifest["scripts"] = {
                            "postinstall": "curl https://example.invalid"
                        }
                        members[index] = (name, canonical(manifest), mode)

            rewrite_tar(meta, mutate)
            refresh_evidence(packages)
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.run_benchmark(
                    packages, root / "install", trials=1, timeout_seconds=2
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            install = root / "install"
            install.mkdir()
            marker = install / "user-marker"
            marker.write_text("preserve", "utf-8")
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.run_benchmark(packages, install, trials=1, timeout_seconds=2)
            self.assertEqual(marker.read_text("utf-8"), "preserve")

        if os.name != "nt":
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                packages = make_package_output(root)
                linked = root / "linked-packages"
                linked.symlink_to(packages, target_is_directory=True)
                with self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.verify_package_output(linked)

    @unittest.skipIf(os.name == "nt", "POSIX group settlement probe")
    def test_unsettled_descendant_is_killed_and_never_becomes_a_measurement(
        self,
    ) -> None:
        self.require_tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root, unsettled_rust=True)
            with self.assertRaisesRegex(BENCHMARK.BenchmarkError, "unsettled"):
                BENCHMARK.run_benchmark(
                    packages, root / "install", trials=1, timeout_seconds=0.25
                )
            time.sleep(0.1)
            # The benchmark owns a new group for each probe; the helper exposes
            # all cleanup failures as BenchmarkError before returning evidence.
            self.assertFalse(
                any(
                    "sleep(30)" in line
                    for line in BENCHMARK.live_owned_probe_descriptions()
                )
            )

    @unittest.skipIf(os.name == "nt", "POSIX top-level SIGINT containment probe")
    def test_top_level_sigint_cleans_child_and_grandchild_before_registry_drop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / "process-identities.json"
            interrupt = root / "interrupt-result.json"
            child_source = (
                "import json, os, pathlib, subprocess, sys, time\n"
                "grandchild = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(30)'])\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
                "'childPid': os.getpid(), 'childPgid': os.getpgrp(), "
                "'grandchildPid': grandchild.pid, "
                "'grandchildPgid': os.getpgid(grandchild.pid)}))\n"
                "time.sleep(30)\n"
            )
            wrapper_source = (
                "import importlib.util, json, pathlib, sys\n"
                "script, identity, result, workspace, child = sys.argv[1:]\n"
                "spec = importlib.util.spec_from_file_location('installed_benchmark', script)\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "try:\n"
                " module.run_owned_process([sys.executable, '-c', child, identity], "
                "pathlib.Path(workspace), {}, 20, 'top-level-sigint-probe')\n"
                "except KeyboardInterrupt:\n"
                " pathlib.Path(result).write_text(json.dumps({"
                "'registry': module.live_owned_probe_descriptions()}))\n"
                " raise\n"
            )
            wrapper = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    wrapper_source,
                    str(SCRIPT),
                    str(identity),
                    str(interrupt),
                    str(root),
                    child_source,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            published = None
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    if identity.exists():
                        published = json.loads(identity.read_text("utf-8"))
                        break
                    if wrapper.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(
                    published, "fixture did not publish process identities"
                )
                assert published is not None
                child_pid = int(published["childPid"])
                child_group = int(published["childPgid"])
                grandchild_pid = int(published["grandchildPid"])
                self.assertEqual(child_pid, child_group)
                self.assertEqual(child_group, int(published["grandchildPgid"]))
                self.assertNotEqual(child_group, os.getpgrp())
                self.assertNotEqual(child_group, wrapper.pid)
                os.kill(wrapper.pid, signal.SIGINT)
                _stdout, stderr = wrapper.communicate(timeout=5)
                self.assertNotEqual(wrapper.returncode, 0)
                self.assertIn(b"KeyboardInterrupt", stderr)
                self.assertEqual(
                    json.loads(interrupt.read_text("utf-8")), {"registry": []}
                )
                for pid in (child_pid, grandchild_pid):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(child_group, 0)
            finally:
                if wrapper.poll() is None:
                    os.killpg(wrapper.pid, signal.SIGKILL)
                    wrapper.communicate(timeout=3)
                if published is not None:
                    group = int(published["childPgid"])
                    child = int(published["childPid"])
                    if group == child and group not in {os.getpgrp(), wrapper.pid}:
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_cleanup_permission_failure_is_normalized_after_pipes_are_closed(
        self,
    ) -> None:
        class Process:
            pid = 123456789

            @staticmethod
            def poll():
                return 0

        class Reader:
            def __init__(self) -> None:
                self.joins = 0

            def join(self, timeout=None) -> None:
                self.joins += 1

            @staticmethod
            def is_alive() -> bool:
                return False

        class Stream:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        readers = [Reader(), Reader()]
        streams = [Stream(), Stream()]
        with mock.patch.object(
            BENCHMARK, "cleanup_owned_process", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(BENCHMARK.BenchmarkError) as raised:
                BENCHMARK.cleanup_after_exception(
                    Process(),
                    Process.pid,
                    readers,
                    streams,
                    "permission-probe",
                    KeyboardInterrupt(),
                )
        self.assertEqual(raised.exception.code, "CLEANUP_UNVERIFIED")
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertTrue(all(reader.joins == 2 for reader in readers))

    @unittest.skipIf(os.name == "nt", "POSIX deadline process containment probe")
    def test_absolute_deadline_caps_process_wait_and_cleans_registry(self) -> None:
        self.require_tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            with self.assertRaises(BENCHMARK.BenchmarkError) as raised:
                BENCHMARK.run_benchmark(
                    packages,
                    root / "install",
                    trials=1,
                    timeout_seconds=20,
                    deadline_monotonic=time.monotonic() + 0.05,
                )
            self.assertEqual(raised.exception.code, "DEADLINE_EXCEEDED")
            self.assertEqual(BENCHMARK.live_owned_probe_descriptions(), [])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            for invalid in (float("nan"), float("inf"), time.monotonic() - 1):
                with self.subTest(deadline=invalid), self.assertRaises(
                    BENCHMARK.BenchmarkError
                ):
                    BENCHMARK.run_benchmark(
                        packages,
                        root / f"install-{str(invalid).replace('/', '-')}",
                        trials=1,
                        deadline_monotonic=invalid,
                    )

    def test_unsupported_platform_is_a_machine_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.verify_package_output(
                    packages, expected_platform="freebsd-x64"
                )

    def test_windows_sidecar_is_bound_across_all_package_payloads_without_running_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root, platform_value="win32-x64")
            context = BENCHMARK.verify_package_output(
                packages, expected_platform="win32-x64"
            )
            rust_name = f"openprose-prose-cli-rust-{VERSION}-win32-x64.tar.gz"
            bun_name = f"openprose-prose-cli-bun-{VERSION}-win32-x64.tar.gz"
            rust = BENCHMARK.read_archive_members(packages / rust_name)
            bun = BENCHMARK.read_archive_members(packages / bun_name)
            bun_binary = next(
                data for name, (data, _) in bun.items() if name.endswith("/prose.exe")
            )
            npm = BENCHMARK.validate_npm_packages(context, bun_binary)
            expected = context["release"]["windowsProcessHost"]["sha256"]
            payloads = [
                next(
                    data
                    for name, (data, _) in rust.items()
                    if name.endswith("process-host.exe")
                ),
                next(
                    data
                    for name, (data, _) in bun.items()
                    if name.endswith("process-host.exe")
                ),
                npm["sidecar"],
            ]
        self.assertTrue(all(digest(payload) == expected for payload in payloads))

    def test_packaged_hello_contract_is_in_the_closed_user_facing_surfaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            context = BENCHMARK.verify_package_output(
                packages, expected_platform=PLATFORM
            )
            payloads = BENCHMARK.validate_package_payloads(context)
            expected = HELLO_EXAMPLE.read_bytes()
            for implementation in ("rust", "bun"):
                extracted = payloads["extracted"][implementation]
                member = f"{extracted['rootName']}/examples/hello.prose.md"
                self.assertEqual(extracted["members"][member], (expected, 0o644))
            meta = BENCHMARK.decode_archive_members(
                context["encoded"][f"openprose-prose-cli-{VERSION}.tgz"],
                "npm meta",
            )
            self.assertEqual(meta["package/examples/hello.prose.md"], (expected, 0o644))

    def test_bun_runtime_evidence_is_platform_exact_and_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = make_package_output(root)
            context = BENCHMARK.verify_package_output(
                packages, expected_platform=PLATFORM
            )
            self.assertEqual(
                context["release"]["bunRuntime"],
                BENCHMARK.BUN_RUNTIME_BY_PLATFORM[PLATFORM],
            )
            manifest_path = packages / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["bunRuntime"]["compileTarget"] = "bun-linux-x64"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), "utf-8")
            refresh_evidence(packages)
            with self.assertRaises(BENCHMARK.BenchmarkError) as rejected:
                BENCHMARK.verify_package_output(packages, expected_platform=PLATFORM)
            self.assertEqual(rejected.exception.code, "IDENTITY_DIVERGENCE")


class NativeSdkSbomCustodyTests(unittest.TestCase):
    def test_artifact_native_sbom_poison_is_rejected_after_rehash(self):
        with tempfile.TemporaryDirectory() as directory:
            output = make_release_package_output(Path(directory), platform_value='linux-x64-gnu')
            BENCHMARK.verify_package_output(output, expected_platform='linux-x64-gnu', purpose='release-invariants')
            sbom_path = output / 'sbom.cdx.json'
            original = json.loads(sbom_path.read_text())
            native = [row for row in original['components'] if any(p.get('value') == 'packaged-sdk-native-file'
                      for p in row.get('properties', []))]
            self.assertEqual(len(native), 1)
            for kind in ('omission', 'duplicate', 'license'):
                sbom = copy.deepcopy(original)
                if kind == 'omission': sbom['components'].remove(native[0])
                elif kind == 'duplicate': sbom['components'].append(native[0])
                else:
                    origin = next(row for row in sbom['components'] if any(p.get('value') == 'packaged-sdk-native-origin'
                                  for p in row.get('properties', [])))
                    origin['properties'][-1]['value'] = '{}'
                sbom_path.write_text(json.dumps(sbom))
                # Rehash the altered artifact without regenerating its poisoned SBOM.
                paths = sorted(path for path in output.iterdir() if path.name != 'SHA256SUMS')
                (output / 'SHA256SUMS').write_text(
                    ''.join(f'{digest(path.read_bytes())}  {path.name}\n' for path in paths), 'utf-8')
                with self.subTest(kind=kind), self.assertRaises(BENCHMARK.BenchmarkError) as rejected:
                    BENCHMARK.verify_package_output(output, expected_platform='linux-x64-gnu', purpose='release-invariants')
                self.assertEqual(rejected.exception.code, 'IDENTITY_DIVERGENCE')
                self.assertEqual(rejected.exception.message, 'Native SDK SBOM differs from bound receipt')



class CompleteSdkInstalledTreeTests(unittest.TestCase):
    def fixture(self, root):
        BENCHMARK.sdk_modules()
        from test_kernel_rc_evidence import sdk_fixture
        from test_sdk_native_inventory import onedir_fixture
        _, _, inventory = BENCHMARK.sdk_modules()
        sdk, table = sdk_fixture('darwin-x64')
        view = onedir_fixture()
        sibling = root / 'installed'; sibling.mkdir()
        inventory.materialize_macos_payload(sibling, view['payload'], view['files'], view['directories'], view['symlinks'], view['architecture'])
        for name in ('agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'):
            data, mode = table['files'][name]
            (sibling / name).write_bytes(data); (sibling / name).chmod(mode)
        encoded = table['files']['agents-sdk-build.json'][0].decode()
        context = {'receiptSha256': sdk['receiptSha256'], 'prefix': 'installed', 'encoded': encoded}
        return sibling, context, {sdk['receiptSha256']: encoded}

    def test_complete_alias_tree_capture_and_retained_reauthentication(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); sibling, context, evidence = self.fixture(root)
            identity = BENCHMARK.capture_installed_tree(root, sdk_context=context)
            aliases = [row for row in identity['entries'] if row['type'] == 'sdk-directory-symlink']
            self.assertEqual(len(aliases), 1)
            BENCHMARK.validate_installed_tree_identity(identity, 'SDK fixture', evidence)
            self.assertEqual(BENCHMARK.verify_installed_tree(root, identity, sdk_evidence=evidence), identity['digestSha256'])
            (sibling / 'prose-agents-sdk-runtime/base_library.zip').write_bytes(b'mutation')
            with self.assertRaises(ValueError):
                BENCHMARK.verify_installed_tree(root, identity, sdk_evidence=evidence)

    def test_generic_directory_alias_stays_forbidden(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); target = root / 'directory'; target.mkdir(); link = root / 'link'; link.symlink_to(target)
            with self.assertRaises(BENCHMARK.BenchmarkError):
                BENCHMARK.capture_installed_tree(root, {link: target})

    def test_payload_poison_with_rehashed_report_identity_is_refused(self):
        import copy
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); _, context, evidence = self.fixture(root)
            valid = BENCHMARK.capture_installed_tree(root, sdk_context=context)
            for poison in ('missing-reference', 'wrong-evidence', 'directory-hash', 'extra-support', 'link-text', 'helper-length', 'helper-mode', 'receipt-length', 'notices-length', 'notices-mode'):
                changed = copy.deepcopy(valid)
                if poison == 'missing-reference': changed.pop('sdkEvidenceRef')
                elif poison == 'wrong-evidence': changed['sdkEvidenceRef']['receiptSha256'] = '0' * 64
                elif poison == 'directory-hash':
                    next(row for row in changed['entries'] if row['type'] == 'sdk-directory-symlink')['resolvedDirectorySha256'] = '0' * 64
                elif poison == 'extra-support':
                    changed['entries'].append({'path': 'installed/prose-agents-sdk-runtime/foreign', 'type': 'directory', 'mode': 0o755})
                    changed['entries'].sort(key=lambda r: r['path']); changed['entryCount'] += 1; changed['directoryCount'] += 1
                elif poison in ('helper-length', 'helper-mode', 'receipt-length', 'notices-length', 'notices-mode'):
                    name = 'prose-agents-sdk' if poison.startswith('helper') else ('agents-sdk-build.json' if poison.startswith('receipt') else 'AGENTS-SDK-NOTICES.txt')
                    row = next(row for row in changed['entries'] if row['path'] == 'installed/' + name)
                    if poison.endswith('length'): row['byteLength'] += 1; changed['byteCount'] += 1
                    else: row['mode'] = 0o644 if name == 'prose-agents-sdk' else 0o755
                else:
                    row = next(row for row in changed['entries'] if row['type'] == 'sdk-directory-symlink')
                    row['linkTarget'] = '../escape'; row['linkTextSha256'] = BENCHMARK.sha256_bytes(b'../escape')
                changed['digestSha256'] = BENCHMARK.sha256_bytes(BENCHMARK.canonical_json({'schema': BENCHMARK.INSTALLED_TREE_SCHEMA, 'entries': changed['entries']}))
                with self.subTest(poison=poison), self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.validate_installed_tree_identity(changed, 'SDK fixture', evidence)

    def test_linux_onefile_trio_stays_closed_without_mac_support_reference(self):
        import copy
        BENCHMARK.sdk_modules()
        from test_kernel_rc_evidence import sdk_fixture
        platform = 'linux-x64-gnu'; version = '0.15.0-rc.4'
        name = 'openprose-prose-cli-rust-' + version + '-' + platform
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); sibling = root / name; sibling.mkdir()
            (sibling / 'examples').mkdir()
            for path in ('LICENSE', 'README.txt', 'examples/hello.prose.md', 'prose'):
                (sibling / path).write_bytes(b'fixture'); (sibling / path).chmod(0o755 if path == 'prose' else 0o644)
            _, table = sdk_fixture(platform)
            for path, (data, mode) in table['files'].items():
                (sibling / path).write_bytes(data); (sibling / path).chmod(mode)
            identity = BENCHMARK.capture_installed_tree(root)
            surfaces = {'direct-rust': {'binarySha256': BENCHMARK.sha256_bytes(b'fixture')}}
            BENCHMARK.validate_installation_tree_relationships('direct-rust', identity, version, platform, surfaces, {})
            self.assertNotIn('sdkEvidenceRef', identity)
            for poison in ('partial', 'mode', 'alias', 'support'):
                changed = copy.deepcopy(identity)
                if poison == 'partial': changed['entries'] = [r for r in changed['entries'] if not r['path'].endswith('/agents-sdk-build.json')]
                elif poison == 'mode': next(r for r in changed['entries'] if r['path'].endswith('/prose-agents-sdk'))['mode'] = 0o644
                elif poison == 'alias': next(r for r in changed['entries'] if r['path'].endswith('/prose-agents-sdk'))['type'] = 'symlink'
                else: changed['entries'].append({'path': name + '/prose-agents-sdk-runtime', 'type': 'directory', 'mode': 0o755})
                with self.subTest(poison=poison), self.assertRaises(BENCHMARK.BenchmarkError):
                    BENCHMARK.validate_installation_tree_relationships('direct-rust', changed, version, platform, surfaces, {})

    def test_synthetic_report_dictionary_avoids_receipt_duplication(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); _, context, evidence = self.fixture(root)
            identity = BENCHMARK.capture_installed_tree(root, sdk_context=context)
            report = {'sdkEvidence': evidence, 'trees': [identity, identity, identity]}
            encoded = BENCHMARK.render_json(report)
            self.assertLess(len(encoded), BENCHMARK.MAX_EVIDENCE_BYTES)
            with self.assertRaisesRegex(BENCHMARK.BenchmarkError, 'architecture differs'):
                BENCHMARK.sdk_report_receipt(context, 'darwin-arm64')
            self.assertEqual(len(evidence), 1)
            self.assertEqual(len([t for t in report['trees'] if t['sdkEvidenceRef']['receiptSha256'] in evidence]), 3)
            self.synthetic_size = len(encoded)

if __name__ == "__main__":
    unittest.main()
