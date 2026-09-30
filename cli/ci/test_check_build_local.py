from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

import build_local


class LocalBuildDriverTest(unittest.TestCase):
    def test_clean_environment_removes_case_variant_credentials_and_overrides(
        self,
    ) -> None:
        sanitized = build_local.clean_environment(
            {
                "PATH": "/safe/bin",
                "openai_api_key": "lowercase-secret",
                "OpenProse_Token": "mixed-secret",
                "aws_secret_access_key": "cloud-secret",
                "node_options": "--require=/untrusted/hook.js",
                "UNRELATED": "kept",
            }
        )
        self.assertEqual(sanitized["PATH"], "/safe/bin")
        self.assertEqual(sanitized["UNRELATED"], "kept")
        self.assertEqual(sanitized["OPENPROSE_BUILD_COMMIT"], "development")
        self.assertFalse(any("secret" in value for value in sanitized.values()))
        self.assertNotIn("node_options", sanitized)

    @staticmethod
    def write_package_evidence(
        package: Path, payload: bytes = b"package-artifact"
    ) -> bytes:
        package.mkdir()
        artifact = package / "artifact.tgz"
        artifact.write_bytes(payload)
        sums = f"{hashlib.sha256(payload).hexdigest()}  {artifact.name}\n".encode()
        (package / "SHA256SUMS").write_bytes(sums)
        return sums

    def fixture(self):
        temporary = TemporaryDirectory(prefix="openprose-local-build-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        rust = root / "target" / "prose"
        bun = root / "dist" / "prose"
        rust.parent.mkdir(parents=True)
        bun.parent.mkdir(parents=True)
        rust.write_bytes(b"rust-candidate")
        bun.write_bytes(b"bun-candidate")
        original = (
            build_local.RUST_BINARY,
            build_local.rust_binary_for_target,
            build_local.BUN_BINARY,
            build_local.WINDOWS_HOST_BINARY,
            build_local.IS_WINDOWS,
        )
        build_local.RUST_BINARY = rust
        build_local.rust_binary_for_target = lambda _target: build_local.RUST_BINARY
        build_local.BUN_BINARY = bun
        build_local.IS_WINDOWS = False
        self.addCleanup(lambda: setattr(build_local, "RUST_BINARY", original[0]))
        self.addCleanup(
            lambda: setattr(build_local, "rust_binary_for_target", original[1])
        )
        self.addCleanup(lambda: setattr(build_local, "BUN_BINARY", original[2]))
        self.addCleanup(
            lambda: setattr(build_local, "WINDOWS_HOST_BINARY", original[3])
        )
        self.addCleanup(lambda: setattr(build_local, "IS_WINDOWS", original[4]))
        return root, rust, bun

    def test_both_candidates_build_and_smoke_without_shell_or_credentials(self) -> None:
        _root, rust, bun = self.fixture()
        calls = []
        smoke_inputs = []

        def execute(argv, cwd, environment):
            calls.append((tuple(argv), cwd, dict(environment)))
            if "image_bundle.py" in " ".join(argv):
                stdout = b""
            elif tuple(argv)[0] not in {"cargo", "bun"}:
                smoke_inputs.append((Path(argv[0]), Path(argv[0]).read_bytes()))
                stdout = json.dumps({"semantic": {"status": "not-applicable"}}).encode()
            else:
                stdout = b""
            return subprocess.CompletedProcess(argv, 0, stdout, b"")

        report = build_local.build(
            selection="both",
            smoke=True,
            package=None,
            install_dir=None,
            ambient={
                "PATH": "/safe",
                "OPENAI_API_KEY": "secret",
                "AWS_ACCESS_KEY_ID": "secret-aws",
                "RUSTFLAGS": "--cfg evil",
                "OPENPROSE_BUILD_COMMIT": "evil",
            },
            executor=execute,
        )
        self.assertIn("image_bundle.py", " ".join(calls[0][0]))
        self.assertEqual([call[0][0] for call in calls[1:3]], ["cargo", "bun"])
        for _argv, _cwd, environment in calls[1:3]:
            self.assertEqual(
                environment["OPENPROSE_IMAGE_SOURCE_DIR"],
                str(build_local.IMAGE_MANIFEST.parent),
            )
            self.assertTrue(
                environment["OPENPROSE_IMAGE_BUNDLE"].endswith("sentinel.bundle.bin")
            )
            self.assertTrue(
                environment["OPENPROSE_IMAGE_BUNDLE_CHECKSUM"].endswith(
                    "sentinel.bundle.sha256"
                )
            )
        bun_argv = calls[2][0]
        rust_argv = calls[1][0]
        self.assertEqual(
            rust_argv[rust_argv.index("--features") + 1],
            "prose-cli/test-seams",
        )
        self.assertEqual(
            bun_argv[bun_argv.index("--image-dir") + 1],
            str(build_local.IMAGE_MANIFEST.parent),
        )
        self.assertIn("--test-seams", bun_argv)
        self.assertEqual(
            [value for _path, value in smoke_inputs],
            [b"rust-candidate", b"bun-candidate"],
        )
        self.assertTrue(all(path not in {rust, bun} for path, _value in smoke_inputs))
        self.assertTrue(
            all(call[2]["OPENPROSE_BUILD_COMMIT"] == "development" for call in calls)
        )
        self.assertTrue(all("OPENAI_API_KEY" not in call[2] for call in calls))
        self.assertTrue(
            all(
                "AWS_ACCESS_KEY_ID" not in call[2] and "RUSTFLAGS" not in call[2]
                for call in calls
            )
        )
        self.assertEqual(
            report["candidates"]["rust"]["sha256"],
            hashlib.sha256(b"rust-candidate").hexdigest(),
        )
        self.assertEqual(
            report["candidates"]["rust"]["buildSourcePath"],
            "$EXTERNAL_BUILD_SOURCE/rust/prose",
        )
        self.assertEqual(
            report["candidates"]["rust"]["snapshotPath"],
            "$OPENPROSE_LOCAL_BUILD/candidates/rust/prose",
        )
        self.assertEqual(
            report["candidates"]["rust"]["snapshotOwnership"], "ephemeral-owned-root"
        )
        self.assertEqual(report["candidates"]["bun"]["smoke"], "pass")
        self.assertFalse(report["globalStateModified"])

    def test_explicit_install_uses_candidate_specific_names_and_never_overwrites(
        self,
    ) -> None:
        root, _rust, _bun = self.fixture()
        install = root / "install"

        def execute(argv, _cwd, _environment):
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        report = build_local.build(
            selection="both",
            smoke=False,
            package=None,
            install_dir=install,
            ambient={},
            executor=execute,
        )
        self.assertEqual((install / "prose-rust").read_bytes(), b"rust-candidate")
        self.assertEqual((install / "prose-bun").read_bytes(), b"bun-candidate")
        self.assertEqual(set(report["install"]), {"rust", "bun"})
        self.assertEqual(
            report["install"]["rust"]["sha256"],
            hashlib.sha256(b"rust-candidate").hexdigest(),
        )
        with self.assertRaisesRegex(
            build_local.LocalBuildError, "must not already exist"
        ):
            build_local.build(
                selection="both",
                smoke=False,
                package=None,
                install_dir=install,
                ambient={},
                executor=lambda *_args: self.fail("must fail before execution"),
            )

    def test_packaging_requires_both_before_any_build_and_invokes_closed_arguments(
        self,
    ) -> None:
        root, _rust, _bun = self.fixture()
        package = root / "package"
        with self.assertRaisesRegex(
            build_local.LocalBuildError, "requires --candidate both"
        ):
            build_local.build(
                selection="rust",
                smoke=False,
                package=package,
                install_dir=None,
                ambient={},
                executor=lambda *_args: self.fail("must fail before execution"),
            )
        calls = []
        packaged_inputs = []

        def execute(argv, _cwd, _environment):
            calls.append(tuple(argv))
            if "package_local.py" in " ".join(argv):
                packaged_inputs.extend(
                    [
                        (
                            Path(argv[argv.index("--rust-binary") + 1]),
                            Path(argv[argv.index("--rust-binary") + 1]).read_bytes(),
                        ),
                        (
                            Path(argv[argv.index("--bun-binary") + 1]),
                            Path(argv[argv.index("--bun-binary") + 1]).read_bytes(),
                        ),
                    ]
                )
                self.write_package_evidence(package)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        report = build_local.build(
            selection="both",
            smoke=False,
            package=package,
            install_dir=None,
            ambient={},
            executor=execute,
        )
        packaging = calls[-1]
        self.assertIn("package_local.py", " ".join(packaging))
        self.assertEqual(packaging[packaging.index("--mode") + 1], "development")
        self.assertEqual(
            packaging[packaging.index("--source-revision") + 1], "development"
        )
        rust_input = Path(packaging[packaging.index("--rust-binary") + 1])
        bun_input = Path(packaging[packaging.index("--bun-binary") + 1])
        self.assertNotEqual(rust_input, build_local.RUST_BINARY)
        self.assertNotEqual(bun_input, build_local.BUN_BINARY)
        self.assertEqual(
            [(path.name, value) for path, value in packaged_inputs],
            [("prose", b"rust-candidate"), ("prose", b"bun-candidate")],
        )
        self.assertEqual(report["package"]["path"], str(package.resolve()))
        self.assertEqual(
            report["package"]["purpose"], build_local.ORDINARY_PACKAGE_PURPOSE
        )
        sums = (package / "SHA256SUMS").read_bytes()
        self.assertEqual(
            report["package"]["sha256Sums"]["sha256"], hashlib.sha256(sums).hexdigest()
        )

    def test_package_uses_a_distinct_ordinary_echo_no_seam_candidate_pair(
        self,
    ) -> None:
        root, rust, bun = self.fixture()
        package = root / "ordinary-package"
        calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        smoke_inputs: list[bytes] = []
        packaged_inputs: list[tuple[Path, bytes]] = []

        def execute(argv, _cwd, environment):
            call = tuple(argv)
            calls.append((call, dict(environment)))
            rendered = " ".join(argv)
            if argv[0] == "cargo":
                rust.write_bytes(
                    b"rust-test-seam"
                    if "prose-cli/test-seams" in argv
                    else b"rust-ordinary-echo"
                )
            elif argv[0] == "bun":
                bun.write_bytes(
                    b"bun-test-seam" if "--test-seams" in argv else b"bun-ordinary-echo"
                )
            elif "package_local.py" in rendered:
                for option in ("--rust-binary", "--bun-binary"):
                    candidate = Path(argv[argv.index(option) + 1])
                    packaged_inputs.append((candidate, candidate.read_bytes()))
                self.write_package_evidence(package)
            elif "image_bundle.py" not in rendered:
                smoke_inputs.append(Path(argv[0]).read_bytes())
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({"semantic": {"status": "not-applicable"}}).encode(),
                    b"",
                )
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        report = build_local.build(
            selection="both",
            smoke=True,
            package=package,
            install_dir=None,
            ambient={"OPENAI_API_KEY": "must-not-survive"},
            executor=execute,
        )

        rust_builds = [call for call, _environment in calls if call[0] == "cargo"]
        bun_builds = [call for call, _environment in calls if call[0] == "bun"]
        self.assertEqual(len(rust_builds), 2)
        self.assertEqual(len(bun_builds), 2)
        self.assertIn("prose-cli/test-seams", rust_builds[0])
        self.assertNotIn("--features", rust_builds[1])
        self.assertIn("--test-seams", bun_builds[0])
        self.assertNotIn("--test-seams", bun_builds[1])

        package_build_environments = [
            environment
            for call, environment in calls
            if (call[0] == "cargo" and "--features" not in call)
            or (call[0] == "bun" and "--test-seams" not in call)
        ]
        self.assertEqual(len(package_build_environments), 2)
        for environment in package_build_environments:
            self.assertEqual(
                environment["OPENPROSE_IMAGE_SOURCE_DIR"],
                str(build_local.ECHO_IMAGE_MANIFEST.parent),
            )
            self.assertEqual(
                environment["OPENPROSE_IMAGE_BUNDLE"],
                str(build_local.ECHO_IMAGE_BUNDLE),
            )
            self.assertEqual(
                environment["OPENPROSE_IMAGE_BUNDLE_CHECKSUM"],
                str(build_local.ECHO_IMAGE_CHECKSUM),
            )
            self.assertNotIn("OPENAI_API_KEY", environment)

        packaging = next(
            call for call, _environment in calls if "package_local.py" in " ".join(call)
        )
        self.assertEqual(
            packaging[packaging.index("--image-manifest") + 1],
            str(build_local.ECHO_IMAGE_MANIFEST),
        )
        self.assertEqual(smoke_inputs, [b"rust-test-seam", b"bun-test-seam"])
        self.assertEqual(
            [value for _path, value in packaged_inputs],
            [b"rust-ordinary-echo", b"bun-ordinary-echo"],
        )
        self.assertTrue(
            all("package-candidates" in path.parts for path, _value in packaged_inputs)
        )
        self.assertEqual(report["profile"], "development")
        self.assertTrue(report["testSeamsEnabled"])
        self.assertEqual(
            report["package"]["inputBuild"],
            {
                "image": "echo-v0",
                "profile": "development",
                "testSeamsEnabled": False,
            },
        )
        self.assertEqual(
            report["package"]["inputs"]["rust"]["sha256"],
            hashlib.sha256(b"rust-ordinary-echo").hexdigest(),
        )
        self.assertEqual(
            report["candidates"]["rust"]["sha256"],
            hashlib.sha256(b"rust-test-seam").hexdigest(),
        )

    def test_internal_mock_package_reuses_only_sentinel_test_seam_candidates(
        self,
    ) -> None:
        root, rust, bun = self.fixture()
        package = root / "mock-package"
        calls: list[tuple[str, ...]] = []
        packaged_inputs: list[bytes] = []

        def execute(argv, _cwd, _environment):
            call = tuple(argv)
            calls.append(call)
            if argv[0] == "cargo":
                rust.write_bytes(b"rust-sentinel-seam")
            elif argv[0] == "bun":
                bun.write_bytes(b"bun-sentinel-seam")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        def package_internal(argv):
            self.assertEqual(
                argv[argv.index("--image-manifest") + 1],
                str(build_local.IMAGE_MANIFEST),
            )
            for option in ("--rust-binary", "--bun-binary"):
                packaged_inputs.append(Path(argv[argv.index(option) + 1]).read_bytes())
            self.write_package_evidence(package)

        report = build_local.build(
            selection="both",
            smoke=False,
            package=package,
            install_dir=None,
            ambient={},
            executor=execute,
            package_purpose=build_local.MOCK_PACKAGE_PURPOSE,
            internal_packager=package_internal,
        )

        self.assertEqual(
            [call[0] for call in calls if call[0] in {"cargo", "bun"}],
            ["cargo", "bun"],
        )
        self.assertEqual(packaged_inputs, [b"rust-sentinel-seam", b"bun-sentinel-seam"])
        self.assertEqual(report["package"]["purpose"], "mock-benchmark")
        self.assertEqual(
            report["package"]["inputBuild"],
            {
                "image": "sentinel-v1",
                "profile": "development",
                "testSeamsEnabled": True,
            },
        )
        self.assertEqual(
            report["package"]["inputs"]["rust"]["sha256"],
            report["candidates"]["rust"]["sha256"],
        )

    def test_cli_has_no_mock_package_selector(self) -> None:
        option_strings = {
            option
            for action in build_local.parser()._actions
            for option in action.option_strings
        }
        self.assertNotIn("--package-purpose", option_strings)
        self.assertNotIn("--internal-package-purpose", option_strings)

    def test_package_purpose_is_closed_and_mock_requires_package(self) -> None:
        self.fixture()
        for purpose, message in (
            ("invented", "unsupported"),
            (build_local.MOCK_PACKAGE_PURPOSE, "requires --package"),
        ):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(
                build_local.LocalBuildError, message
            ):
                build_local.build(
                    selection="both",
                    smoke=False,
                    package=None,
                    install_dir=None,
                    ambient={},
                    executor=lambda *_args: self.fail("must reject before execution"),
                    package_purpose=purpose,
                )

    def test_guides_distinguish_smoke_and_package_build_identities(self) -> None:
        cli_readme = (build_local.CLI / "README.md").read_text("utf-8")
        release_readme = (build_local.CLI / "release" / "README.md").read_text("utf-8")
        normalized_cli = " ".join(cli_readme.split())
        normalized_release = " ".join(release_readme.split())
        for text in (normalized_cli, normalized_release):
            self.assertIn("sentinel/test-seam", text)
            self.assertIn("ordinary", text)
            self.assertIn("echo-v0", text)
            self.assertIn("test seams disabled", text)
        self.assertIn("--package /tmp/openprose-cli-artifacts", normalized_release)
        self.assertIn("[maintained release path](release/README.md)", normalized_cli)
        self.assertIn("cli-kernel-rc.yml", normalized_release)

    def test_preexisting_outputs_are_refused_without_deleting_user_bytes(self) -> None:
        root, _rust, _bun = self.fixture()
        install = root / "install"
        install.mkdir()
        install_marker = install / "user-file"
        install_marker.write_bytes(b"install-must-remain")
        package = root / "package"
        package.mkdir()
        package_marker = package / "user-file"
        package_marker.write_bytes(b"package-must-remain")

        for selected_package, selected_install, message in (
            (package, None, "package output must not already exist"),
            (None, install, "install-dir must not already exist"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(build_local.LocalBuildError, message):
                    build_local.build(
                        selection="both",
                        smoke=False,
                        package=selected_package,
                        install_dir=selected_install,
                        ambient={},
                        executor=lambda *_args: self.fail("must fail before execution"),
                    )
        self.assertEqual(install_marker.read_bytes(), b"install-must-remain")
        self.assertEqual(package_marker.read_bytes(), b"package-must-remain")

    def test_successful_packager_without_sha256sums_fails_closed(self) -> None:
        root, _rust, _bun = self.fixture()
        package = root / "package"

        def execute(argv, _cwd, _environment):
            if "package_local.py" in " ".join(argv):
                package.mkdir()
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with self.assertRaisesRegex(build_local.LocalBuildError, "SHA256SUMS"):
            build_local.build(
                selection="both",
                smoke=False,
                package=package,
                install_dir=None,
                ambient={},
                executor=execute,
            )
        self.assertTrue(package.is_dir())

    def test_build_failure_is_bounded_and_stops_before_later_candidates(self) -> None:
        self.fixture()
        calls = []

        def execute(argv, _cwd, _environment):
            calls.append(tuple(argv))
            if "image_bundle.py" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(
                argv, 7, b"", b"x" * (build_local.MAX_DIAGNOSTIC_BYTES + 100)
            )

        with self.assertRaises(build_local.LocalBuildError) as caught:
            build_local.build(
                selection="both",
                smoke=False,
                package=None,
                install_dir=None,
                ambient={},
                executor=execute,
            )
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(
            len(str(caught.exception)), build_local.MAX_DIAGNOSTIC_BYTES + 100
        )

    def test_smoke_output_fails_closed_on_invalid_or_applicable_semantics(self) -> None:
        _root, rust, _bun = self.fixture()
        cases = (
            (b"not-json", "not JSON"),
            (
                json.dumps({"semantic": {"status": "passed"}}).encode(),
                "sentinel semantics",
            ),
        )
        for output, message in cases:
            with self.subTest(message=message):

                def execute(argv, _cwd, _environment):
                    stdout = output if tuple(argv)[0] != "cargo" else b""
                    return subprocess.CompletedProcess(argv, 0, stdout, b"")

                with self.assertRaisesRegex(build_local.LocalBuildError, message):
                    build_local.build(
                        selection="rust",
                        smoke=True,
                        package=None,
                        install_dir=None,
                        ambient={},
                        executor=execute,
                    )

    def test_candidate_digest_rejects_symlinks(self) -> None:
        root, rust, _bun = self.fixture()
        link = root / "linked-prose"
        link.symlink_to(rust)
        build_local.RUST_BINARY = link

        with self.assertRaisesRegex(build_local.LocalBuildError, "non-symlink"):
            build_local.build(
                selection="rust",
                smoke=False,
                package=None,
                install_dir=None,
                ambient={},
                executor=lambda argv, _cwd, _environment: subprocess.CompletedProcess(
                    argv, 0, b"", b""
                ),
            )

    def test_windows_build_binds_and_installs_the_exact_non_admitted_sidecar(
        self,
    ) -> None:
        root, _rust, _bun = self.fixture()
        host = root / build_local.WINDOWS_HOST_NAME
        host.write_bytes(b"windows-host")
        build_local.WINDOWS_HOST_BINARY = host
        build_local.IS_WINDOWS = True
        package = root / "package"
        install = root / "install"
        calls = []
        packaged_host_bytes = []

        def execute(argv, cwd, environment):
            calls.append((tuple(argv), cwd, dict(environment)))
            rendered = " ".join(argv)
            if "cli/rust/Cargo.toml" in rendered:
                host.write_bytes(b"mutated-after-snapshot")
            if "package_local.py" in rendered:
                host_argument = Path(argv[argv.index("--windows-process-host") + 1])
                packaged_host_bytes.append(host_argument.read_bytes())
                self.assertNotEqual(host_argument, host)
                self.write_package_evidence(package)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        report = build_local.build(
            selection="both",
            smoke=False,
            package=package,
            install_dir=install,
            ambient={"OPENPROSE_WINDOWS_HOST_ADMISSION": "1"},
            executor=execute,
        )

        expected_digest = hashlib.sha256(b"windows-host").hexdigest()
        self.assertIn("windows-process-host/Cargo.toml", " ".join(calls[0][0]))
        self.assertIn("image_bundle.py", " ".join(calls[1][0]))
        self.assertEqual([call[0][0] for call in calls[2:4]], ["cargo", "bun"])
        for _argv, _cwd, environment in calls[2:4]:
            self.assertEqual(
                environment["OPENPROSE_WINDOWS_HOST_SHA256"], expected_digest
            )
            self.assertEqual(environment["OPENPROSE_WINDOWS_HOST_ADMISSION"], "0")
        packaging = calls[-1][0]
        self.assertIn("--windows-process-host", packaging)
        self.assertEqual(packaged_host_bytes, [b"windows-host"])
        self.assertEqual(
            (install / build_local.WINDOWS_HOST_NAME).read_bytes(), b"windows-host"
        )
        self.assertEqual(report["windowsProcessHost"]["sha256"], expected_digest)
        self.assertFalse(report["windowsProcessHost"]["admission"])
        installed_host = report["install"]["windowsProcessHost"]
        self.assertEqual(
            installed_host["path"],
            str((install / build_local.WINDOWS_HOST_NAME).resolve()),
        )
        self.assertEqual(installed_host["sha256"], expected_digest)
        self.assertEqual(
            set(report["windowsProcessHost"]["candidateSiblings"]), {"rust", "bun"}
        )

    def test_windows_sidecar_deployment_refuses_a_symlink_destination(self) -> None:
        root, rust, _bun = self.fixture()
        host = root / build_local.WINDOWS_HOST_NAME
        host.write_bytes(b"windows-host")
        build_local.WINDOWS_HOST_BINARY = host
        build_local.IS_WINDOWS = True
        victim = root / "victim"
        victim.write_bytes(b"must-remain")
        owned_root = root / "owned"
        sibling = owned_root / "candidates" / "rust" / build_local.WINDOWS_HOST_NAME
        sibling.parent.mkdir(parents=True)
        sibling.symlink_to(victim)

        with self.assertRaisesRegex(
            build_local.LocalBuildError, "destination already exists"
        ):
            build_local._build(
                selection="rust",
                smoke=False,
                package=None,
                install_dir=None,
                ambient={},
                owned_root=owned_root,
                executor=lambda argv, _cwd, _environment: subprocess.CompletedProcess(
                    argv, 0, b"", b""
                ),
            )
        self.assertEqual(victim.read_bytes(), b"must-remain")

    def test_post_snapshot_source_mutation_cannot_change_any_consumer(self) -> None:
        root, rust, bun = self.fixture()
        package = root / "package"
        install = root / "install"
        observed: dict[str, object] = {"smoke": [], "package": None}

        def execute(argv, _cwd, _environment):
            rendered = " ".join(argv)
            if argv[0] == "cargo" and "--features" not in argv:
                rust.write_bytes(b"rust-package-candidate")
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if argv[0] == "bun":
                # Each Rust snapshot must already exist before its paired Bun build
                # mutates the shared Rust output path.
                rust.write_bytes(
                    b"rust-mutated-during-bun-build"
                    if "--test-seams" in argv
                    else b"rust-package-mutated-during-bun-build"
                )
                if "--test-seams" not in argv:
                    bun.write_bytes(b"bun-package-candidate")
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if argv[0] not in {"cargo", "bun", str(Path(build_local.sys.executable))}:
                observed["smoke"].append((Path(argv[0]).read_bytes(), str(argv[0])))
                bun.write_bytes(b"bun-mutated-after-snapshot")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({"semantic": {"status": "not-applicable"}}).encode(),
                    b"",
                )
            if "package_local.py" in rendered:
                rust_input = Path(argv[argv.index("--rust-binary") + 1])
                bun_input = Path(argv[argv.index("--bun-binary") + 1])
                observed["package"] = (rust_input.read_bytes(), bun_input.read_bytes())
                self.write_package_evidence(package)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        report = build_local.build(
            selection="both",
            smoke=True,
            package=package,
            install_dir=install,
            ambient={},
            executor=execute,
        )

        self.assertEqual(
            [value for value, _path in observed["smoke"]],
            [b"rust-candidate", b"bun-candidate"],
        )
        self.assertEqual(
            observed["package"],
            (b"rust-package-candidate", b"bun-package-candidate"),
        )
        self.assertEqual((install / "prose-rust").read_bytes(), b"rust-candidate")
        self.assertEqual((install / "prose-bun").read_bytes(), b"bun-candidate")
        self.assertEqual(
            report["candidates"]["rust"]["sha256"],
            hashlib.sha256(b"rust-candidate").hexdigest(),
        )

    def test_owned_snapshot_mutation_fails_closed_before_later_consumers(self) -> None:
        root, _rust, _bun = self.fixture()
        install = root / "install"

        def execute(argv, _cwd, _environment):
            if "image_bundle.py" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if argv[0] != "cargo":
                snapshot = Path(argv[0])
                snapshot.chmod(0o700)
                snapshot.write_bytes(b"overwritten-owned-snapshot")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({"semantic": {"status": "not-applicable"}}).encode(),
                    b"",
                )
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with self.assertRaisesRegex(
            build_local.LocalBuildError, "owned rust snapshot mutated"
        ):
            build_local.build(
                selection="rust",
                smoke=True,
                package=None,
                install_dir=install,
                ambient={},
                executor=execute,
            )
        self.assertFalse(install.exists())

    def test_same_or_hardlinked_product_sources_are_rejected_as_mixed_candidates(
        self,
    ) -> None:
        root, rust, _bun = self.fixture()
        aliases = [rust, root / "hardlinked-bun"]
        os.link(rust, aliases[1])
        for bun_source in aliases:
            with self.subTest(source=bun_source):
                build_local.BUN_BINARY = bun_source
                with self.assertRaisesRegex(
                    build_local.LocalBuildError, "same build source"
                ):
                    build_local.build(
                        selection="both",
                        smoke=False,
                        package=None,
                        install_dir=None,
                        ambient={},
                        executor=lambda argv, _cwd, _environment: subprocess.CompletedProcess(
                            argv, 0, b"", b""
                        ),
                    )

    def test_snapshot_refuses_overwrite_and_preserves_existing_owned_file(self) -> None:
        root, rust, _bun = self.fixture()
        destination = root / "owned"
        destination.write_bytes(b"must-remain")
        with self.assertRaisesRegex(
            build_local.LocalBuildError, "cannot snapshot rust"
        ):
            build_local.snapshot_file(rust, destination, "rust")
        self.assertEqual(destination.read_bytes(), b"must-remain")

    @unittest.skipIf(os.name == "nt", "exact process-group cleanup is POSIX-only")
    def test_default_executor_times_out_and_settles_its_owned_process_group(
        self,
    ) -> None:
        root, _rust, _bun = self.fixture()
        identities = root / "process-identities.json"
        original_timeout = build_local.COMMAND_TIMEOUT_SECONDS
        original_cleanup = build_local.COMMAND_CLEANUP_SECONDS
        build_local.COMMAND_TIMEOUT_SECONDS = 0.25
        build_local.COMMAND_CLEANUP_SECONDS = 1.0
        self.addCleanup(
            lambda: setattr(build_local, "COMMAND_TIMEOUT_SECONDS", original_timeout)
        )
        self.addCleanup(
            lambda: setattr(build_local, "COMMAND_CLEANUP_SECONDS", original_cleanup)
        )
        program = (
            "import json, os, pathlib, subprocess, sys, time; "
            "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            f"pathlib.Path({str(identities)!r}).write_text(json.dumps("
            "{'childPid': child.pid, 'processGroupId': os.getpgrp()})); "
            "time.sleep(30)"
        )
        with self.assertRaisesRegex(
            build_local.LocalBuildError, "timed out or remained unsettled"
        ):
            build_local.execute(
                [sys.executable, "-c", program],
                root,
                {"PATH": os.environ.get("PATH", "")},
            )
        observed = json.loads(identities.read_text("utf-8"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and build_local.process_group_exists(
            observed["processGroupId"]
        ):
            time.sleep(0.01)
        self.assertFalse(build_local.process_group_exists(observed["processGroupId"]))
        with self.assertRaises(ProcessLookupError):
            os.kill(observed["childPid"], 0)

    def test_default_executor_rejects_oversized_output_after_settlement(self) -> None:
        root, _rust, _bun = self.fixture()
        original = build_local.MAX_COMMAND_OUTPUT_BYTES
        build_local.MAX_COMMAND_OUTPUT_BYTES = 32
        self.addCleanup(
            lambda: setattr(build_local, "MAX_COMMAND_OUTPUT_BYTES", original)
        )
        with self.assertRaisesRegex(
            build_local.LocalBuildError, "output exceeded 32 bytes"
        ):
            build_local.execute(
                [sys.executable, "-c", "print('x' * 1024)"],
                root,
                {"PATH": os.environ.get("PATH", "")},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
