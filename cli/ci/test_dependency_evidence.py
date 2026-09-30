from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "cli" / "ci" / "dependency_evidence.py"
SPEC = importlib.util.spec_from_file_location("openprose_dependency_evidence", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import dependency evidence implementation: {SCRIPT}")
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)

SHA256_A = "a" * 64
SHA256_B = "b" * 64
SHA256_C = "c" * 64
SHA256_D = "d" * 64
SHA512_ZERO = "sha512-" + base64.b64encode(bytes(64)).decode("ascii")


def make_fixture(root: Path) -> None:
    rust = root / "cli" / "rust"
    member = rust / "crates" / "app"
    bun = root / "cli" / "bun"
    windows_host = root / "cli" / "platform" / "windows-process-host"
    member.mkdir(parents=True)
    bun.mkdir(parents=True)
    windows_host.mkdir(parents=True)
    (rust / "Cargo.toml").write_text(
        """[workspace]
members = ["crates/app"]

[workspace.package]
version = "1.2.3"

[workspace.dependencies]
foo = "1.0.0"
bar = "2.0.0"
""",
        encoding="utf-8",
    )
    (member / "Cargo.toml").write_text(
        """[package]
name = "app"
version.workspace = true

[dependencies]
foo.workspace = true

[dev-dependencies]
bar.workspace = true

[build-dependencies]
bar.workspace = true
""",
        encoding="utf-8",
    )
    (rust / "Cargo.lock").write_text(
        f"""# generated
version = 4

[[package]]
name = "app"
version = "1.2.3"
dependencies = [
 "bar",
 "foo",
]

[[package]]
name = "bar"
version = "2.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{SHA256_B}"

[[package]]
name = "baz"
version = "3.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{SHA256_C}"

[[package]]
name = "foo"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{SHA256_A}"
dependencies = [
 "baz",
]
""",
        encoding="utf-8",
    )
    (bun / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture-bun",
                "version": "1.2.3",
                "private": True,
                "dependencies": {"runtime": "6.0.0"},
                "devDependencies": {"tool": "4.0.0"},
            }
        ),
        encoding="utf-8",
    )
    # Deliberately retain Bun's accepted trailing-comma form.
    (bun / "bun.lock").write_text(
        f"""{{
  "lockfileVersion": 1,
  "configVersion": 1,
  "workspaces": {{
    "": {{
      "name": "fixture-bun",
      "dependencies": {{ "runtime": "6.0.0", }},
      "devDependencies": {{ "tool": "4.0.0", }},
    }},
  }},
  "packages": {{
    "tool": ["tool@4.0.0", "", {{ "dependencies": {{ "utility": "^5.0.0" }} }}, "{SHA512_ZERO}"],
    "runtime": ["runtime@6.0.0", "", {{ "dependencies": {{ "utility": "^5.0.0" }} }}, "{SHA512_ZERO}"],
    "utility": ["utility@5.0.1", "", {{}}, "{SHA512_ZERO}"],
  }},
}}
""",
        encoding="utf-8",
    )
    (windows_host / "Cargo.toml").write_text(
        """[package]
name = "fixture-windows-host"
version = "1.2.3"

[dependencies]
foo = "1.0.0"

[target.'cfg(windows)'.dependencies]
winapi = { version = "6.0.0", features = [
  "jobs",
] }
""",
        encoding="utf-8",
    )
    (windows_host / "Cargo.lock").write_text(
        f"""# generated
version = 4

[[package]]
name = "fixture-windows-host"
version = "1.2.3"
dependencies = [
 "foo",
 "winapi",
]

[[package]]
name = "foo"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{SHA256_A}"

[[package]]
name = "winapi"
version = "6.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{SHA256_D}"
""",
        encoding="utf-8",
    )


class DependencyEvidenceTests(unittest.TestCase):
    def test_real_stable_locks_emit_complete_deterministic_inventory(self) -> None:
        first = EVIDENCE.render_report(EVIDENCE.build_report(ROOT))
        second = EVIDENCE.render_report(EVIDENCE.build_report(ROOT))
        self.assertEqual(first, second)
        report = json.loads(first)
        self.assertEqual(report["schema"], "openprose.dependency-evidence/1")
        self.assertEqual(
            [source["path"] for source in report["sources"]],
            sorted(source["path"] for source in report["sources"]),
        )
        self.assertEqual(
            [source["path"] for source in report["sources"]],
            [
                "cli/bun/bun.lock",
                "cli/bun/package.json",
                "cli/platform/windows-process-host/Cargo.lock",
                "cli/platform/windows-process-host/Cargo.toml",
                "cli/rust/Cargo.lock",
                "cli/rust/Cargo.toml",
                "cli/rust/crates/prose-cli/Cargo.toml",
                "cli/rust/crates/prose-process-supervisor/Cargo.toml",
                "cli/rust/crates/prose-runner-core/Cargo.toml",
            ],
        )
        self.assertGreater(len(report["inventories"]["cargo"]["packages"]), 50)
        self.assertEqual(report["inventories"]["cargo"]["component"], "rust-cli")
        self.assertEqual(
            report["inventories"]["windowsProcessHostCargo"]["component"],
            "windows-process-host",
        )
        self.assertEqual(
            len(report["inventories"]["windowsProcessHostCargo"]["packages"]), 14
        )
        self.assertEqual(len(report["inventories"]["bun"]["packages"]), 13)
        for source in report["sources"]:
            self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(source["byteLength"], 0)
            expected = (ROOT / source["path"]).read_bytes()
            self.assertEqual(source["byteLength"], len(expected))
            self.assertEqual(source["sha256"], hashlib.sha256(expected).hexdigest())

    def test_scopes_use_manifest_kinds_and_lockfile_reachability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            report = EVIDENCE.build_report(root)
        cargo = {
            package["name"]: package
            for package in report["inventories"]["cargo"]["packages"]
        }
        self.assertEqual(cargo["app"]["scopes"], ["workspace"])
        self.assertEqual(cargo["foo"]["scopes"], ["runtime"])
        self.assertEqual(cargo["baz"]["scopes"], ["runtime"])
        self.assertEqual(cargo["bar"]["scopes"], ["build", "development"])
        windows_host = {
            package["name"]: package
            for package in report["inventories"]["windowsProcessHostCargo"]["packages"]
        }
        self.assertEqual(windows_host["fixture-windows-host"]["scopes"], ["component"])
        self.assertEqual(windows_host["fixture-windows-host"]["source"], "component-local")
        self.assertEqual(windows_host["foo"]["scopes"], ["runtime"])
        self.assertEqual(windows_host["winapi"]["scopes"], ["runtime"])
        self.assertEqual(cargo["foo"]["integrity"], windows_host["foo"]["integrity"])
        bun = {
            package["name"]: package
            for package in report["inventories"]["bun"]["packages"]
        }
        self.assertEqual(bun["fixture-bun"]["scopes"], ["workspace"])
        self.assertEqual(bun["runtime"]["scopes"], ["runtime"])
        self.assertEqual(bun["tool"]["scopes"], ["development"])
        self.assertEqual(bun["utility"]["scopes"], ["development", "runtime"])

    def test_real_bun_runtime_and_development_closures_have_honest_scopes(self) -> None:
        report = EVIDENCE.build_report(ROOT)
        bun = {
            package["name"]: package["scopes"]
            for package in report["inventories"]["bun"]["packages"]
        }
        self.assertEqual(bun["ajv"], ["development", "runtime"])
        self.assertEqual(bun["undici"], ["runtime"])
        self.assertEqual(bun["ajv-formats"], ["development"])
        for name in (
            "fast-deep-equal",
            "fast-uri",
            "json-schema-traverse",
            "require-from-string",
        ):
            self.assertEqual(bun[name], ["development", "runtime"], name)
        for name in ("@types/bun", "@types/node", "bun-types", "typescript", "undici-types"):
            self.assertEqual(bun[name], ["development"], name)

    def test_authority_is_honestly_blocked_and_cannot_be_weakened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            report = EVIDENCE.build_report(root)
        self.assertEqual(report["authority"]["licenses"]["status"], "unknown")
        self.assertEqual(report["authority"]["vulnerabilities"]["status"], "not-performed")
        self.assertEqual(report["authority"]["signing"]["status"], "not-performed")
        self.assertFalse(report["releasePolicy"]["passed"])
        self.assertFalse(EVIDENCE.release_policy_passes(report))
        for authority in report["authority"].values():
            authority["status"] = "verified"
        report["releasePolicy"]["passed"] = True
        self.assertFalse(EVIDENCE.release_policy_passes(report))

    def test_cli_report_succeeds_and_release_check_fails_with_same_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            report = subprocess.run(
                [sys.executable, str(SCRIPT), "report", "--root", str(root)],
                capture_output=True,
                check=False,
                timeout=10,
            )
            check = subprocess.run(
                [sys.executable, str(SCRIPT), "check", "--root", str(root)],
                capture_output=True,
                check=False,
                timeout=10,
            )
        self.assertEqual(report.returncode, 0, report.stderr.decode())
        self.assertEqual(check.returncode, 3, check.stderr.decode())
        self.assertEqual(report.stdout, check.stdout)
        self.assertEqual(report.stderr, b"")
        self.assertEqual(check.stderr, b"")

    def test_cargo_rejects_unsupported_version_missing_integrity_and_duplicates(self) -> None:
        mutations = (
            ("version = 4", "version = 3"),
            (f'checksum = "{SHA256_A}"\n', ""),
            (
                'name = "foo"\nversion = "1.0.0"',
                'name = "bar"\nversion = "2.0.0"',
            ),
        )
        for old, new in mutations:
            with self.subTest(old=old):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    make_fixture(root)
                    lock = root / "cli" / "rust" / "Cargo.lock"
                    encoded = lock.read_text("utf-8")
                    self.assertIn(old, encoded)
                    lock.write_text(encoded.replace(old, new, 1), encoding="utf-8")
                    with self.assertRaises(EVIDENCE.EvidenceError):
                        EVIDENCE.build_report(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            lock = root / "cli" / "platform" / "windows-process-host" / "Cargo.lock"
            encoded = lock.read_text("utf-8")
            old = 'name = "winapi"\nversion = "6.0.0"'
            self.assertIn(old, encoded)
            lock.write_text(
                encoded.replace(old, 'name = "foo"\nversion = "1.0.0"', 1),
                encoding="utf-8",
            )
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)

    def test_package_counts_are_bounded_before_dependency_graph_traversal(self) -> None:
        cargo = """version = 4

[[package]]
name = "one"
version = "1.0.0"

[[package]]
name = "two"
version = "1.0.0"
"""
        with mock.patch.object(EVIDENCE, "MAX_PACKAGES", 1), self.assertRaises(
            EVIDENCE.EvidenceError
        ) as cargo_error:
            EVIDENCE.parse_cargo_lock(cargo)
        self.assertEqual(cargo_error.exception.code, "SOURCE_OVERSIZE")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            with mock.patch.object(EVIDENCE, "MAX_PACKAGES", 1), self.assertRaises(
                EVIDENCE.EvidenceError
            ) as bun_error:
                EVIDENCE.bun_inventory(root)
        self.assertEqual(bun_error.exception.code, "SOURCE_OVERSIZE")

    def test_bun_rejects_unsupported_version_missing_integrity_and_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            lock = root / "cli" / "bun" / "bun.lock"
            lock.write_text(lock.read_text("utf-8").replace('"lockfileVersion": 1', '"lockfileVersion": 2'), encoding="utf-8")
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            lock = root / "cli" / "bun" / "bun.lock"
            lock.write_text(lock.read_text("utf-8").replace(f', "{SHA512_ZERO}"]', ', ""]', 1), encoding="utf-8")
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            lock = root / "cli" / "bun" / "bun.lock"
            lock.write_text(lock.read_text("utf-8").replace('"configVersion": 1,', '"configVersion": 1,\n  "configVersion": 1,'), encoding="utf-8")
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)

    def test_manifest_and_lock_disagreement_or_traversal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            manifest = root / "cli" / "bun" / "package.json"
            value = json.loads(manifest.read_text("utf-8"))
            value["devDependencies"]["tool"] = "4.0.1"
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            manifest = root / "cli" / "rust" / "Cargo.toml"
            manifest.write_text(manifest.read_text("utf-8").replace('"crates/app"', '"../escape"'), encoding="utf-8")
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            lock = root / "cli" / "bun" / "bun.lock"
            lock.write_text(
                lock.read_text("utf-8").replace(
                    '"tool": ["tool@4.0.0"', '"../tool": ["../tool@4.0.0"'
                ),
                encoding="utf-8",
            )
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)

    def test_safe_reader_rejects_symlink_oversize_and_observed_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "regular"
            regular.write_bytes(b"abc")
            link = root / "link"
            try:
                link.symlink_to(regular)
            except OSError:
                link = None
            if link is not None:
                with self.assertRaises(EVIDENCE.EvidenceError):
                    EVIDENCE.safe_read(link, 10)
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.safe_read(root, 10)
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.safe_read(regular, 2)

            real_fstat = os.fstat
            calls = 0

            def changed(descriptor: int):
                nonlocal calls
                calls += 1
                observed = real_fstat(descriptor)
                if calls < 2:
                    return observed
                values = list(observed)
                values[8] += 1
                return os.stat_result(values)

            with mock.patch.object(EVIDENCE.os, "fstat", side_effect=changed):
                with self.assertRaises(EVIDENCE.EvidenceError):
                    EVIDENCE.safe_read(regular, 10)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_fixture(root)
            rust = root / "cli" / "rust"
            real_rust = root / "cli" / "rust-real"
            rust.rename(real_rust)
            try:
                rust.symlink_to(real_rust, target_is_directory=True)
            except OSError:
                return
            with self.assertRaises(EVIDENCE.EvidenceError):
                EVIDENCE.build_report(root)

    def test_malformed_cli_is_machine_readable_and_has_no_override_switch(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "check", "--allow-unknown-licenses"],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stdout)
        self.assertEqual(error["schema"], "openprose.dependency-evidence-error/1")
        self.assertEqual(error["code"], "ARGUMENT_INVALID")
        self.assertNotIn("license", result.stderr.decode().lower())


if __name__ == "__main__":
    unittest.main()
