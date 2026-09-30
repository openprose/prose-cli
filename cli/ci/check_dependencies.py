#!/usr/bin/env python3
"""Fail closed when the local admission toolchain differs from its pinned contract."""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable


CLI = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(?:^|\s|v)([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$|-)")
NODE_MINIMUM = (22, 22, 3)
NPM_MINIMUM = (10, 0, 0)
BUN_ORDINARY_BUILD = (
    "bun --no-env-file --config=./config/empty-bunfig.toml run "
    "./scripts/image-bundle.ts build"
)
BUN_TEST_BUILD = (
    "bun --no-env-file --config=./config/empty-bunfig.toml run "
    "./scripts/image-bundle.ts build-test"
)
BUN_RELEASE_BUILD = f"{BUN_ORDINARY_BUILD} --require-release-eligible"
BUN_CHECK = "bun run typecheck && bun run test && bun run build"
BUN_PRODUCTION_DEPENDENCIES = {
    "ajv": "8.20.0",
    "undici": "7.30.0",
}
BUN_DEVELOPMENT_DEPENDENCIES = {
    "@types/bun": "1.3.5",
    "ajv-formats": "3.0.1",
    "typescript": "5.9.3",
}
BUN_DEPENDENCY_GROUPS = {
    "dependencies": BUN_PRODUCTION_DEPENDENCIES,
    "devDependencies": BUN_DEVELOPMENT_DEPENDENCIES,
    "optionalDependencies": {},
    "peerDependencies": {},
}


def command_version(argv: tuple[str, ...]) -> str | None:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    match = VERSION.search((completed.stdout or completed.stderr).strip())
    return None if match is None else match.group(1)


def expected_versions() -> dict[str, str]:
    rust = (CLI / "rust" / "rust-toolchain.toml").read_text("utf-8")
    bun = json.loads((CLI / "bun" / "package.json").read_text("utf-8"))
    rust_match = re.search(r'^channel = "([0-9]+\.[0-9]+\.[0-9]+)"$', rust, re.MULTILINE)
    bun_match = re.fullmatch(r"bun@([0-9]+\.[0-9]+\.[0-9]+)", bun["packageManager"])
    if rust_match is None or bun_match is None:
        raise ValueError("toolchain manifests do not contain exact versions")
    return {
        "rustc": rust_match.group(1),
        "cargo": rust_match.group(1),
        "bun": bun_match.group(1),
        "jsonschema": "4.23.0",
        "referencing": "0.35.1",
    }


def version_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def bun_manifest_contract_failures(
    manifest: dict[str, object] | None = None,
) -> list[str]:
    if manifest is None:
        loaded = json.loads((CLI / "bun" / "package.json").read_text("utf-8"))
        if not isinstance(loaded, dict):
            return ["bun: package.json must be an object"]
        manifest = loaded

    failures: list[str] = []
    for group, expected in BUN_DEPENDENCY_GROUPS.items():
        observed = manifest.get(group, {})
        if observed != expected:
            failures.append(
                f"bun: {group} differs from the exact production/development dependency contract"
            )
    return failures


def build_mode_contract_failures(
    *,
    scripts: dict[str, object] | None = None,
    rust_build: str | None = None,
    rust_cli_manifest: str | None = None,
    rust_core_manifest: str | None = None,
    rust_runner: str | None = None,
) -> list[str]:
    if scripts is None:
        manifest = json.loads((CLI / "bun" / "package.json").read_text("utf-8"))
        scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return ["bun: package scripts must be an object"]
    if rust_build is None:
        rust_build = (CLI / "rust" / "crates" / "prose-cli" / "build.rs").read_text(
            "utf-8"
        )
    if rust_cli_manifest is None:
        rust_cli_manifest = (
            CLI / "rust" / "crates" / "prose-cli" / "Cargo.toml"
        ).read_text("utf-8")
    if rust_core_manifest is None:
        rust_core_manifest = (
            CLI / "rust" / "crates" / "prose-runner-core" / "Cargo.toml"
        ).read_text("utf-8")
    if rust_runner is None:
        rust_runner = (
            CLI / "rust" / "crates" / "prose-runner-core" / "src" / "runner.rs"
        ).read_text("utf-8")

    failures: list[str] = []
    expected = {
        "build": BUN_ORDINARY_BUILD,
        "build:test": BUN_TEST_BUILD,
        "build:release": BUN_RELEASE_BUILD,
        "check": BUN_CHECK,
    }
    for name, value in expected.items():
        if scripts.get(name) != value:
            failures.append(f"bun: scripts.{name} differs from the closed build-mode contract")
    for name in ("build", "build:release"):
        value = scripts.get(name)
        if isinstance(value, str) and (
            "--test-seams" in value or "sentinel-v1" in value
        ):
            failures.append(f"bun: scripts.{name} exposes test-only image or seams")
    rust_defaults = (
        '"echo-v0"',
        'image_dir.join("embedded/current.bundle.bin")',
        'image_dir.join("embedded/current.bundle.sha256")',
    )
    if any(marker not in rust_build for marker in rust_defaults):
        failures.append("rust: default build is not bound to the committed echo-v0 image")
    rust_test_build = (
        'const TEST_SEAMS_FEATURE_ENV: &str = "CARGO_FEATURE_TEST_SEAMS";',
        '"sentinel-v1"',
        '"test-seams cannot be enabled for a release build"',
    )
    if any(marker not in rust_build for marker in rust_test_build):
        failures.append("rust: explicit test build is not bound to sentinel seams")
    if (
        "[features]\ndefault = []\ntest-seams = "
        '["prose-runner-core/test-seams"]'
        not in rust_cli_manifest
    ):
        failures.append("rust: prose-cli does not explicitly propagate test seams")
    if "[features]\ndefault = []\ntest-seams = []" not in rust_core_manifest:
        failures.append("rust: prose-runner-core test seams are not default-off")
    rust_runner_markers = (
        'cfg!(feature = "test-seams")',
        '#[cfg(all(feature = "test-seams", not(debug_assertions)))]',
        'compile_error!("test-seams cannot be enabled for a release build")',
    )
    if any(marker not in rust_runner for marker in rust_runner_markers):
        failures.append("rust: runner test seams are not explicit and release-closed")
    return failures


def assess(
    command: Callable[[tuple[str, ...]], str | None] = command_version,
    distribution: Callable[[str], str] = metadata.version,
    python_version: tuple[int, int, int] | None = None,
) -> dict[str, object]:
    expected = expected_versions()
    observed: dict[str, str | None] = {
        "rustc": command(("rustc", "--version")),
        "cargo": command(("cargo", "--version")),
        "bun": command(("bun", "--version")),
        "node": command(("node", "--version")),
        "npm": command(("npm", "--version")),
    }
    for package in ("jsonschema", "referencing"):
        try:
            observed[package] = distribution(package)
        except metadata.PackageNotFoundError:
            observed[package] = None
    actual_python = python_version or sys.version_info[:3]
    failures = [
        f"{name}: expected {version}, observed {observed[name] or 'unavailable'}"
        for name, version in expected.items()
        if observed[name] != version
    ]
    failures.extend(bun_manifest_contract_failures())
    failures.extend(build_mode_contract_failures())
    for name, minimum in (("node", NODE_MINIMUM), ("npm", NPM_MINIMUM)):
        value = observed[name]
        if value is None or version_tuple(value) < minimum:
            required = ".".join(map(str, minimum))
            failures.append(
                f"{name}: requires version >= {required}, observed {value or 'unavailable'}"
            )
    if actual_python < (3, 10, 0):
        failures.append(
            f"python: requires >= 3.10.0, observed {'.'.join(map(str, actual_python))}"
        )
    return {
        "schema": "openprose.local-dependency-report/1",
        "status": "pass" if not failures else "fail",
        "python": ".".join(map(str, actual_python)),
        "expected": expected,
        "observed": observed,
        "failures": failures,
    }


def main() -> int:
    try:
        report = assess()
    except (OSError, KeyError, TypeError, ValueError) as error:
        report = {
            "schema": "openprose.local-dependency-report/1",
            "status": "fail",
            "failures": [f"dependency contract could not be read: {error}"],
        }
    print(json.dumps(report, sort_keys=True))
    if report["status"] == "pass":
        return 0
    print(
        "Install cli/ci/requirements-test.txt and the versions pinned by "
        "cli/rust/rust-toolchain.toml and cli/bun/package.json.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
