from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli"
SCRIPT = CLI / "ci" / "package_local.py"
DEPENDENCY_SCRIPT = CLI / "ci" / "dependency_evidence.py"
IMAGE_MANIFEST = CLI / "shared" / "image" / "sentinel-v1" / "manifest.json"
ECHO_IMAGE_MANIFEST = CLI / "shared" / "image" / "echo-v0" / "manifest.json"
HELLO_EXAMPLE = CLI / "conformance" / "live-alpha" / "hello.prose.md"
VERSION = "0.1.0"
FAKE_HARNESS = CLI / "conformance" / "fake-harness" / "fake_harness.py"
NODE_FLOOR = Path("/opt/homebrew/Cellar/node@22/22.22.3/bin/node")

PACKAGE_LOCAL_SPEC = importlib.util.spec_from_file_location(
    "openprose_package_local", SCRIPT
)
if PACKAGE_LOCAL_SPEC is None or PACKAGE_LOCAL_SPEC.loader is None:
    raise RuntimeError(f"cannot import packaging implementation: {SCRIPT}")
PACKAGE_LOCAL = importlib.util.module_from_spec(PACKAGE_LOCAL_SPEC)
PACKAGE_LOCAL_SPEC.loader.exec_module(PACKAGE_LOCAL)


JOURNEY_HEADINGS = {
    "prime": "Prime — macOS Apple silicon only:",
    "omp": "OMP — macOS Apple silicon or Linux x64 only:",
    "codex": "Codex — every supported functional-alpha platform:",
    "claude": "Claude — macOS Apple silicon only:",
}
JOURNEY_INSTALLS = {
    "prime": (
        "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh "
        "| sh -s -- 0.8.1"
    ),
    "omp": ("npm install --global bun@1.3.14 " "@oh-my-pi/pi-coding-agent@18.0.9"),
    "codex": "npm install --global @openai/codex@0.149.0-alpha.4.1",
    "claude": "npm install --global @anthropic-ai/claude-code@2.1.243",
}
JOURNEY_VERSION_MARKERS = {
    "prime": "Prime: exact admitted versions are 0.7.0 and 0.8.1.",
    "omp": "OMP: exact admitted version is 18.0.9. It requires Bun 1.3.14 or newer.",
    "codex": "Codex: exact admitted version is 0.149.0-alpha.4.1.",
    "claude": "Claude: exact admitted version is 2.1.243.",
}
JOURNEY_DISPLAY_NAMES = {
    "prime": "Prime",
    "omp": "OMP",
    "codex": "Codex",
    "claude": "Claude",
}
PROVIDER_CHARGE_BOUNDARY = (
    "The run command contacts the selected provider and may incur charges under "
    "the signed-in account. The CLI cannot determine the account or billing route."
)
SANITIZED_CLI_REPORT = (
    "https://github.com/openprose/prose/issues/new?template=openprose-cli-bug.yml"
)
HARNESS_MODEL_REQUEST = "https://github.com/openprose/prose/issues/new?template=openprose-cli-harness-model.yml"
BENCHMARK_REQUEST = (
    "https://github.com/openprose/prose/issues/new?"
    "template=openprose-cli-benchmark-profile.yml"
)
PRIVATE_VULNERABILITY_REPORT = (
    "https://github.com/openprose/prose/security/advisories/new"
)


def display_harnesses(harnesses: tuple[str, ...]) -> str:
    names = [JOURNEY_DISPLAY_NAMES[harness] for harness in harnesses]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def assert_complete_harness_journey(
    testcase: unittest.TestCase,
    readme: str,
    *,
    harness: str,
    executable: str,
    example: str,
) -> None:
    heading = JOURNEY_HEADINGS[harness]
    testcase.assertEqual(readme.count(heading), 1)
    start = readme.index(heading)
    later_headings = [
        readme.find(candidate, start + len(heading))
        for candidate in JOURNEY_HEADINGS.values()
    ]
    end = min((index for index in later_headings if index >= 0), default=len(readme))
    block = readme[start:end]
    selection = f"{executable} cli harness use {harness}"
    if harness in {"prime", "omp"}:
        selection += (
            " --model openai-codex/gpt-5.4 " f"--auth-profile {harness}-harness-login"
        )
    expected = [JOURNEY_INSTALLS[harness]]
    if harness == "codex":
        expected.append("codex login")
    elif harness == "claude":
        expected.append("claude auth login")
    expected.extend(
        [
            selection,
            f"{executable} cli doctor",
            f"{executable} run {example}",
        ]
    )
    positions = [block.find(command) for command in expected]
    testcase.assertTrue(all(index >= 0 for index in positions), block)
    testcase.assertEqual(positions, sorted(positions))
    testcase.assertIn(
        "Authentication is not automated. Complete the harness sign-in flow "
        "before you run `cli doctor` or `run`.",
        block,
    )
    if harness in {"prime", "omp"}:
        testcase.assertIn(
            f"start the installed `{harness if harness == 'omp' else 'prime-agent'}` "
            "harness separately",
            block.lower(),
        )
        testcase.assertIn("OpenProse CLI never invokes or controls that TUI.", block)
    testcase.assertEqual(block.count(PROVIDER_CHARGE_BOUNDARY), 1)
    testcase.assertLess(
        block.index(PROVIDER_CHARGE_BOUNDARY),
        block.index(f"{executable} run {example}"),
    )


def assert_actionable_codex_first_run(
    testcase: unittest.TestCase,
    readme: str,
    *,
    executable: str,
    example: str,
) -> None:
    heading = "First run with Codex 0.149.0-alpha.4.1"
    start = readme.index(heading)
    ends = (
        readme.find("Upgrade", start),
        readme.find("## Functional-alpha harness support", start),
    )
    end = min(index for index in ends if index >= 0)
    testcase.assertGreater(end, start)
    block = readme[start:end]
    expected = (
        JOURNEY_INSTALLS["codex"],
        "codex login",
        f"{executable} cli harness list",
        f"{executable} cli harness use codex",
        f"{executable} cli doctor",
        f"{executable} run {example}",
    )
    positions = [block.find(command) for command in expected]
    testcase.assertTrue(all(index >= 0 for index in positions), block)
    testcase.assertEqual(positions, sorted(positions))
    testcase.assertEqual(block.count(PROVIDER_CHARGE_BOUNDARY), 1)
    testcase.assertLess(
        block.index(PROVIDER_CHARGE_BOUNDARY),
        block.index(f"{executable} run {example}"),
    )


def platform_id() -> str:
    systems = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    machines = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}
    system = systems.get(platform.system())
    machine = machines.get(platform.machine())
    if system is None or machine is None:
        raise RuntimeError(
            f"unsupported test host {platform.system()}-{platform.machine()}"
        )
    if system == "linux":
        return f"linux-{machine}-gnu"
    return f"{system}-{machine}"


PLATFORM_ID = platform_id()


def readelf_args() -> list[str]:
    if not PLATFORM_ID.startswith("linux-"):
        return []
    discovered = shutil.which("readelf")
    if discovered is None:
        raise unittest.SkipTest("Linux package tests require readelf")
    return ["--readelf", str(Path(discovered).resolve(strict=True))]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    config = root / "config"
    cache = root / "cache"
    temporary = root / "tmp"
    for directory in (home, config, cache, temporary):
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }


def build_environment(root: Path, build_commit: str) -> dict[str, str]:
    environment = clean_environment(root)
    original_home = Path.home()
    environment.update(
        {
            "CARGO_HOME": os.environ.get("CARGO_HOME", str(original_home / ".cargo")),
            "RUSTUP_HOME": os.environ.get(
                "RUSTUP_HOME", str(original_home / ".rustup")
            ),
            "OPENPROSE_BUILD_COMMIT": build_commit,
        }
    )
    return environment


def run_artifact(
    executable: Path, args: list[str], cwd: Path, env_root: Path
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(executable), *args],
        cwd=cwd,
        env=clean_environment(env_root),
        capture_output=True,
        check=False,
        timeout=10,
    )


def unpack_archive(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as package:
        destination.mkdir(parents=True, exist_ok=True)
        destination_root = destination.resolve()
        for member in package.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise AssertionError(f"unsafe archive member path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise AssertionError(f"unsafe archive member type: {member.name}")
            extracted = (destination / member_path).resolve()
            if destination_root not in (extracted, *extracted.parents):
                raise AssertionError(
                    f"archive member escapes destination: {member.name}"
                )
        # Python 3.10 predates tarfile's filter= parameter. The complete member
        # validation above provides the equivalent confinement for these tests.
        package.extractall(destination)
    executables = list(destination.glob("*/prose")) + list(
        destination.glob("*/prose.exe")
    )
    if len(executables) != 1:
        raise AssertionError(
            f"archive did not contain exactly one prose executable: {executables}"
        )
    return executables[0]


def read_npm_file(package: Path, member: str) -> bytes:
    with tarfile.open(package, "r:gz") as archive:
        extracted = archive.extractfile(f"package/{member}")
        if extracted is None:
            raise AssertionError(f"missing npm package member {member}")
        return extracted.read()


def npm_executable(prefix: Path) -> Path:
    if platform.system() == "Windows":
        return prefix / "prose.cmd"
    return prefix / "bin" / "prose"


def npm_modules_root(prefix: Path) -> Path:
    if platform.system() == "Windows":
        return prefix / "node_modules"
    return prefix / "lib" / "node_modules"


def npm_platform_root(prefix: Path) -> Path:
    return npm_modules_root(prefix) / "@openprose" / f"prose-cli-{PLATFORM_ID}"


def posix_shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def expected_registry_repair_command(prefix: Path) -> str:
    return (
        "npm install --global --ignore-scripts --prefix "
        f"{posix_shell_quote(str(prefix.resolve()))} "
        f"{posix_shell_quote(f'@openprose/prose-cli-{PLATFORM_ID}@{VERSION}')} "
        f"{posix_shell_quote(f'@openprose/prose-cli@{VERSION}')}"
    )


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = Path(f"/proc/{pid}/stat")
    if status.is_file():
        try:
            fields = status.read_text("utf-8").split()
        except OSError:
            return False
        if len(fields) > 2 and fields[2] == "Z":
            return False
    return True


def wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.02)
    return not process_exists(pid)


class BoundedPackagingProcessTests(unittest.TestCase):
    def test_packager_subprocess_output_and_time_are_bounded(self) -> None:
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = PACKAGE_LOCAL.run_bounded(
                [sys.executable, "-c", "print('exact')"],
                cwd=root,
                environment=environment,
                timeout_seconds=2,
                label="nominal probe",
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, b"exact\n")
            self.assertEqual(completed.stderr, b"")

            with mock.patch.object(PACKAGE_LOCAL, "MAX_COMMAND_OUTPUT_BYTES", 32):
                with self.assertRaisesRegex(
                    PACKAGE_LOCAL.PackageError, "output exceeded"
                ):
                    PACKAGE_LOCAL.run_bounded(
                        [
                            sys.executable,
                            "-c",
                            "import sys,time;sys.stdout.write('x'*100000);sys.stdout.flush();time.sleep(30)",
                        ],
                        cwd=root,
                        environment=environment,
                        timeout_seconds=2,
                        label="overflow probe",
                    )

            with self.assertRaisesRegex(PACKAGE_LOCAL.PackageError, "timed out"):
                PACKAGE_LOCAL.run_bounded(
                    [sys.executable, "-c", "import time;time.sleep(30)"],
                    cwd=root,
                    environment=environment,
                    timeout_seconds=0.05,
                    label="timeout probe",
                )

            for invalid_timeout in (True, 0, -1, float("nan"), float("inf"), 601):
                with self.subTest(timeout=invalid_timeout), self.assertRaisesRegex(
                    PACKAGE_LOCAL.PackageError, "timeout must be finite"
                ):
                    PACKAGE_LOCAL.run_bounded(
                        [sys.executable, "-c", "pass"],
                        cwd=root,
                        environment=environment,
                        timeout_seconds=invalid_timeout,
                        label="invalid probe",
                    )


class SemVerTests(unittest.TestCase):
    def test_accepts_semver_2_versions_including_build_metadata(self) -> None:
        for version in (
            "0.0.0",
            "1.2.3",
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-0.3.7",
            "1.0.0-x.7.z.92",
            "1.0.0+001",
            "1.0.0-beta+exp.sha.5114f85",
        ):
            with self.subTest(version=version):
                self.assertTrue(PACKAGE_LOCAL.valid_semver(version))

    def test_rejects_leading_zero_and_invalid_semver_identifiers(self) -> None:
        for version in (
            "v1.2.3",
            "01.2.3",
            "1.02.3",
            "1.2.03",
            "1.0",
            "1.0.0-01",
            "1.0.0-alpha..1",
            "1.0.0-",
            "1.0.0+",
            "1.0.0+meta..data",
            "1.0.0_alpha",
        ):
            with self.subTest(version=version):
                self.assertFalse(PACKAGE_LOCAL.valid_semver(version))

    def test_source_revision_uses_the_portable_build_identity_alphabet(self) -> None:
        for value in ("development", "0123456789abcdef", "release-1.2.3+build_7"):
            with self.subTest(value=value):
                self.assertIsNotNone(PACKAGE_LOCAL.SOURCE_REVISION.fullmatch(value))
        for value in ("", "contains space", "line\nbreak", "x" * 129, "path/segment"):
            with self.subTest(value=value):
                self.assertIsNone(PACKAGE_LOCAL.SOURCE_REVISION.fullmatch(value))


class SelfReferenceGuidanceTests(unittest.TestCase):
    def test_rejects_command_position_path_resolved_runner_guidance(self) -> None:
        for command in (
            b"prose cli doctor\n",
            b"    prose cli harness use codex\n",
            b"$ prose cli config explain\n",
            b"> prose.exe cli doctor\n",
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    PACKAGE_LOCAL.PackageError,
                    "PATH-resolved prose executable",
                ):
                    PACKAGE_LOCAL.require_exact_runner_self_invocation(
                        command, "fixture README"
                    )

    def test_accepts_exact_path_and_explicit_prose_variable_guidance(self) -> None:
        for command in (
            b'"$PWD/openprose/prose" cli doctor\n',
            b'"$HOME/.local/openprose/bin/prose" cli doctor\n',
            b'"$PROSE" cli doctor\n',
            b"./dist/prose cli doctor\n",
        ):
            with self.subTest(command=command):
                PACKAGE_LOCAL.require_exact_runner_self_invocation(
                    command, "fixture README"
                )

    def test_standalone_readme_anchors_pwd_before_any_generated_command(self) -> None:
        readme = PACKAGE_LOCAL.standalone_readme(
            mode="alpha",
            implementation="rust",
            version="0.15.0-alpha.1",
            platform_identifier="darwin-arm64",
            archive_name="openprose.tar.gz",
            root_name="openprose-root",
            linux_runtime="not-applicable",
        ).decode("utf-8")
        anchor = (
            "Continue from this same archive/checksum directory; "
            "do not change into the extracted root."
        )
        recovery = "If you already changed into openprose-root, run: cd .."
        first_run = '"$PWD/openprose-root/prose" cli harness list'
        self.assertIn(anchor, readme)
        self.assertIn(recovery, readme)
        self.assertLess(readme.index(anchor), readme.index(first_run))


class StandaloneUninstallGuidanceTests(unittest.TestCase):
    ROOT_NAME = "openprose-prose-cli-bun-0.15.0-alpha.1-darwin-arm64"

    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("standalone uninstall guidance uses the POSIX shell")

    @staticmethod
    def populate_root(root: Path, executable: str = "prose") -> None:
        (root / "examples").mkdir(parents=True)
        (root / "README.txt").write_text("package guidance\n", "utf-8")
        binary = root / executable
        binary.write_bytes(b"standalone\n")
        binary.chmod(0o755)
        (root / "examples" / "hello.prose.md").write_text("hello\n", "utf-8")

    def run_guidance(
        self,
        workspace: Path,
        uninstall_root: str,
        *,
        cwd: Path,
        executable: str = "prose",
    ) -> tuple[subprocess.CompletedProcess[str], list[bytes]]:
        fake_bin = workspace / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        arguments = workspace / f"rm-arguments-{uuid.uuid4().hex}"
        rm = fake_bin / "rm"
        rm.write_text(
            "#!/bin/sh\n"
            ': > "$OPENPROSE_RM_ARGUMENTS"\n'
            "for argument do\n"
            '  printf \'%s\\0\' "$argument" >> "$OPENPROSE_RM_ARGUMENTS"\n'
            "done\n",
            "utf-8",
        )
        rm.chmod(0o755)
        environment = clean_environment(workspace / "guidance-environment")
        environment.update(
            {
                "PATH": str(fake_bin),
                "OPENPROSE_RM_ARGUMENTS": str(arguments),
            }
        )
        completed = subprocess.run(
            [
                "/bin/sh",
                "-c",
                PACKAGE_LOCAL.standalone_uninstall_shell(self.ROOT_NAME, executable),
            ],
            cwd=cwd,
            env=environment,
            input=f"{uninstall_root}\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        recorded = (
            arguments.read_bytes().split(b"\0")[:-1] if arguments.exists() else []
        )
        return completed, recorded

    def test_uninstall_revalidates_an_explicit_root_before_removal(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openprose-standalone-uninstall-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            root = workspace / "archive parent with spaces" / self.ROOT_NAME
            self.populate_root(root)
            for cwd in (workspace, root):
                with self.subTest(cwd=cwd):
                    completed, arguments = self.run_guidance(
                        workspace, str(root), cwd=cwd
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(
                        arguments,
                        [b"-rf", b"--", os.fsencode(root)],
                    )
            marker = workspace / "shell-active-absolute-input-ran"
            probe = workspace / "fake-bin" / "_openprose_uninstall_probe"
            probe.write_text(
                "#!/bin/sh\n" f": > {posix_shell_quote(str(marker))}\n",
                "utf-8",
            )
            probe.chmod(0o755)
            hostile_root = workspace / "$(_openprose_uninstall_probe)" / self.ROOT_NAME
            self.populate_root(hostile_root)
            completed, arguments = self.run_guidance(
                workspace, str(hostile_root), cwd=workspace
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                arguments,
                [b"-rf", b"--", os.fsencode(hostile_root)],
            )
            self.assertFalse(marker.exists())

    def test_uninstall_rejects_unbound_or_tampered_roots_without_calling_rm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openprose-standalone-uninstall-hostile-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            valid = workspace / self.ROOT_NAME
            self.populate_root(valid)
            wrong = workspace / "wrong-name"
            self.populate_root(wrong)
            real = workspace / "real-root"
            self.populate_root(real)
            linked = workspace / "linked" / self.ROOT_NAME
            linked.parent.mkdir()
            linked.symlink_to(real, target_is_directory=True)
            linked_parent = workspace / "linked-parent"
            linked_parent.symlink_to(workspace, target_is_directory=True)
            incomplete = workspace / "incomplete" / self.ROOT_NAME
            self.populate_root(incomplete)
            (incomplete / "examples" / "hello.prose.md").unlink()
            nonexecutable = workspace / "nonexecutable" / self.ROOT_NAME
            self.populate_root(nonexecutable)
            (nonexecutable / "prose").chmod(0o644)
            (workspace / "wrong-segment").mkdir()
            relative = self.ROOT_NAME
            hostile_marker = workspace / "shell-active-input-ran"
            hostile = f"$(touch {hostile_marker})/{self.ROOT_NAME}"
            for label, candidate in (
                ("empty", ""),
                ("root", "/"),
                ("relative", relative),
                ("shell-active", hostile),
                ("wrong-basename", str(wrong)),
                ("symlink", str(linked)),
                ("symlinked-parent", str(linked_parent / self.ROOT_NAME)),
                (
                    "noncanonical",
                    f"{workspace}/wrong-segment/../{self.ROOT_NAME}",
                ),
                ("missing-member", str(incomplete)),
                ("nonexecutable", str(nonexecutable)),
            ):
                with self.subTest(label=label):
                    completed, arguments = self.run_guidance(
                        workspace, candidate, cwd=workspace
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(arguments, [])
                    self.assertIn("refusing standalone uninstall", completed.stderr)
            self.assertFalse(hostile_marker.exists())

    def test_uninstall_rejects_symlinked_expected_members(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openprose-standalone-uninstall-member-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            for label, member in (
                ("readme", "README.txt"),
                ("executable", "prose"),
                ("examples-directory", "examples"),
                ("example", "examples/hello.prose.md"),
            ):
                with self.subTest(member=label):
                    root = workspace / label / self.ROOT_NAME
                    self.populate_root(root)
                    target = workspace / label / "moved"
                    path = root / member
                    path.rename(target)
                    path.symlink_to(target, target_is_directory=target.is_dir())
                    completed, arguments = self.run_guidance(
                        workspace, str(root), cwd=workspace
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(arguments, [])


class StandaloneRepairGuidanceTests(unittest.TestCase):
    VERSION = "0.15.0-alpha.1"
    ROOT_NAME = "openprose-prose-cli-rust-0.15.0-alpha.1-darwin-arm64"
    ARCHIVE_NAME = f"{ROOT_NAME}.tar.gz"

    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("standalone repair guidance uses the POSIX shell")

    def make_closed_archive(self, workspace: Path) -> None:
        executable = (
            "#!/bin/sh\n" f'printf "%s\\n" "prose {self.VERSION} (rust)"\n'
        ).encode("utf-8")
        PACKAGE_LOCAL.tar_gz(
            workspace / self.ARCHIVE_NAME,
            [
                (f"{self.ROOT_NAME}/prose", executable, 0o755),
                (f"{self.ROOT_NAME}/README.txt", b"offline guidance\n", 0o644),
                (f"{self.ROOT_NAME}/LICENSE", b"license\n", 0o644),
                (
                    f"{self.ROOT_NAME}/examples/hello.prose.md",
                    b"hello\n",
                    0o644,
                ),
            ],
            0,
        )
        archive = workspace / self.ARCHIVE_NAME
        (workspace / "SHA256SUMS").write_text(
            f"{sha256(archive)}  {self.ARCHIVE_NAME}\n", encoding="ascii"
        )

    def run_guidance(
        self, workspace: Path, repair_parent: str, *, extra_input: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/bin/sh",
                "-c",
                PACKAGE_LOCAL.standalone_repair_shell(
                    archive_name=self.ARCHIVE_NAME,
                    root_name=self.ROOT_NAME,
                    executable="prose",
                    version=self.VERSION,
                    implementation="rust",
                ),
            ],
            cwd=workspace,
            env=clean_environment(workspace / "repair-environment"),
            input=f"{repair_parent}\n{extra_input}",
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )

    def test_same_version_repair_extracts_only_into_a_fresh_exact_root(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openprose-standalone-repair-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            self.make_closed_archive(workspace)
            old_root = workspace / "old installation" / self.ROOT_NAME
            StandaloneUninstallGuidanceTests.populate_root(old_root)
            old_before = {
                path.relative_to(old_root): path.read_bytes()
                for path in old_root.rglob("*")
                if path.is_file()
            }
            marker = workspace / "shell-substitution-ran"
            repair_parent = workspace / "fresh $(_openprose_repair_probe) parent"
            completed = self.run_guidance(workspace, str(repair_parent))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            repaired_root = repair_parent / self.ROOT_NAME
            self.assertEqual(
                sorted(
                    path.relative_to(repaired_root).as_posix()
                    for path in repaired_root.rglob("*")
                    if path.is_file()
                ),
                ["LICENSE", "README.txt", "examples/hello.prose.md", "prose"],
            )
            self.assertIn(str(repaired_root / "prose"), completed.stdout)
            self.assertFalse(marker.exists())
            self.assertEqual(
                old_before,
                {
                    path.relative_to(old_root): path.read_bytes()
                    for path in old_root.rglob("*")
                    if path.is_file()
                },
            )

    def test_repair_refuses_hostile_targets_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openprose-standalone-repair-hostile-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            self.make_closed_archive(workspace)
            existing = workspace / "existing"
            existing.mkdir()
            symlink = workspace / "symlink"
            symlink.symlink_to(existing, target_is_directory=True)
            broken = workspace / "broken"
            broken.symlink_to(workspace / "missing", target_is_directory=True)
            real_parent = workspace / "real-parent"
            real_parent.mkdir()
            linked_parent = workspace / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            clean_environment(workspace / "repair-environment")
            hostile = (
                ("empty", "", ""),
                ("root", "/", ""),
                ("relative", "relative-repair", ""),
                ("existing", str(existing), ""),
                ("symlink", str(symlink), ""),
                ("broken-symlink", str(broken), ""),
                (
                    "noncanonical",
                    f"{workspace}/real-parent/../noncanonical-repair",
                    "",
                ),
                ("symlinked-parent", str(linked_parent / "repair"), ""),
                ("tab-control", str(workspace / "tab\trepair"), ""),
                ("newline-injection", str(workspace / "first"), "second\n"),
            )
            for label, candidate, extra_input in hostile:
                with self.subTest(label=label):
                    before = set(workspace.iterdir())
                    completed = self.run_guidance(
                        workspace, candidate, extra_input=extra_input
                    )
                    self.assertEqual(completed.returncode, 1, completed)
                    self.assertIn("refusing standalone repair", completed.stderr)
                    self.assertEqual(before, set(workspace.iterdir()))

    def test_repair_refuses_tampered_or_ambiguous_archive_before_extraction(
        self,
    ) -> None:
        for label in ("tampered-archive", "duplicate-checksum", "linked-checksum"):
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"openprose-standalone-repair-{label}-"
            ) as temporary:
                workspace = Path(temporary).resolve()
                self.make_closed_archive(workspace)
                if label == "tampered-archive":
                    with (workspace / self.ARCHIVE_NAME).open("ab") as archive:
                        archive.write(b"tampered")
                elif label == "duplicate-checksum":
                    checksum = workspace / "SHA256SUMS"
                    checksum.write_bytes(checksum.read_bytes() * 2)
                else:
                    checksum = workspace / "SHA256SUMS"
                    moved = workspace / "moved-SHA256SUMS"
                    checksum.rename(moved)
                    checksum.symlink_to(moved)
                clean_environment(workspace / "repair-environment")
                repair_parent = workspace / "must-not-be-created"
                completed = self.run_guidance(workspace, str(repair_parent))
                self.assertEqual(completed.returncode, 1, completed)
                self.assertIn("refusing standalone repair", completed.stderr)
                self.assertFalse(repair_parent.exists())

    def test_every_executable_archive_readme_has_distinct_same_version_repair(
        self,
    ) -> None:
        for mode in ("development", "alpha", "release"):
            readme = PACKAGE_LOCAL.standalone_readme(
                mode=mode,
                implementation="rust",
                version=self.VERSION,
                platform_identifier="darwin-arm64",
                archive_name=self.ARCHIVE_NAME,
                root_name=self.ROOT_NAME,
                linux_runtime="not-applicable",
            ).decode("utf-8")
            with self.subTest(mode=mode):
                self.assertEqual(readme.count("Repair this same exact version:"), 1)
                self.assertIn(f'ASSET="{self.ARCHIVE_NAME}"', readme)
                self.assertIn('test ! -e "$REPAIR_PARENT"', readme)
                self.assertIn('"$REPAIRED_ROOT/prose" --version', readme)
                self.assertIn(
                    f'"prose {self.VERSION} (rust)"',
                    readme,
                )
                self.assertNotIn("eval ", readme)
                self.assertLess(readme.index("Upgrade:"), readme.index("Repair this"))
                self.assertLess(readme.index("Repair this"), readme.index("Uninstall"))


class NpmUpgradeGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("npm upgrade guidance uses the POSIX shell")

    def run_guidance(
        self, workspace: Path, value: str
    ) -> tuple[subprocess.CompletedProcess[str], list[bytes], Path]:
        fake_bin = workspace / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        arguments = workspace / f"npm-arguments-{uuid.uuid4().hex}"
        npm = fake_bin / "npm"
        npm.write_text(
            "#!/bin/sh\n"
            ': > "$OPENPROSE_NPM_ARGUMENTS"\n'
            "for argument do\n"
            '  printf \'%s\\0\' "$argument" >> "$OPENPROSE_NPM_ARGUMENTS"\n'
            "done\n",
            "utf-8",
        )
        npm.chmod(0o755)
        marker = workspace / "shell-substitution-ran"
        for name in ("_openprose_upgrade_substitution", "_openprose_upgrade_backtick"):
            probe = fake_bin / name
            probe.write_text(
                "#!/bin/sh\n" f": > {posix_shell_quote(str(marker))}\n",
                "utf-8",
            )
            probe.chmod(0o755)
        environment = clean_environment(workspace / "guidance-environment")
        environment.update(
            {
                "PATH": str(fake_bin),
                "OPENPROSE_NPM_ARGUMENTS": str(arguments),
            }
        )
        completed = subprocess.run(
            ["/bin/sh", "-c", PACKAGE_LOCAL.npm_alpha_upgrade_shell()],
            cwd=workspace,
            env=environment,
            input=f"{value}\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        recorded = (
            arguments.read_bytes().split(b"\0")[:-1] if arguments.exists() else []
        )
        return completed, recorded, marker

    def test_upgrade_accepts_only_an_exact_numbered_functional_alpha(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openprose-npm-upgrade-") as temporary:
            workspace = Path(temporary).resolve()
            home = workspace / "guidance-environment" / "home"
            for value in ("12.3.40-alpha.5", "0.0.0-alpha.0"):
                with self.subTest(value=value):
                    completed, arguments, marker = self.run_guidance(workspace, value)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(
                        arguments,
                        [
                            b"install",
                            b"--global",
                            b"--ignore-scripts",
                            b"--prefix",
                            os.fsencode(home / ".local" / f"openprose-cli-{value}"),
                            f"@openprose/prose-cli@{value}".encode("ascii"),
                        ],
                    )
                    self.assertFalse(marker.exists())

    def test_upgrade_rejects_empty_malformed_and_shell_active_input_before_npm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openprose-npm-upgrade-hostile-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            for label, value in (
                ("empty", ""),
                ("stable", "1.2.3"),
                ("missing-number", "1.2.3-alpha"),
                ("empty-number", "1.2.3-alpha."),
                ("leading-major-zero", "01.2.3-alpha.4"),
                ("leading-minor-zero", "1.02.3-alpha.4"),
                ("leading-patch-zero", "1.2.03-alpha.4"),
                ("leading-alpha-zero", "1.2.3-alpha.04"),
                ("extra-core", "1.2.3.4-alpha.5"),
                ("extra-prerelease", "1.2.3-alpha.4.extra"),
                ("substitution", "$(_openprose_upgrade_substitution)"),
                ("backtick", "`_openprose_upgrade_backtick`"),
                (
                    "suffix-substitution",
                    "1.2.3-alpha.$(_openprose_upgrade_substitution)",
                ),
                (
                    "core-backtick",
                    "1.2.`_openprose_upgrade_backtick`-alpha.4",
                ),
                ("command-separator", "1.2.3-alpha.4; npm install forged"),
            ):
                with self.subTest(label=label):
                    completed, arguments, marker = self.run_guidance(workspace, value)
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(arguments, [])
                    self.assertFalse(marker.exists())
                    self.assertIn(
                        "refusing npm functional-alpha upgrade", completed.stderr
                    )

    def test_generated_upgrade_has_no_executable_version_placeholder(self) -> None:
        alpha = PACKAGE_LOCAL.npm_readme("alpha", "0.15.0-alpha.1").decode("utf-8")
        self.assertEqual(alpha.count(PACKAGE_LOCAL.npm_alpha_upgrade_shell("    ")), 1)
        self.assertNotIn("replace-with-exact-version", alpha)
        self.assertNotIn("NEW_VERSION='<", alpha)
        development = PACKAGE_LOCAL.npm_readme(
            "development", "0.1.0", "darwin-arm64"
        ).decode("utf-8")
        self.assertNotIn("replace-with-exact-version", development)


class DevelopmentPackageGuidanceTests(unittest.TestCase):
    def test_alpha_guidance_exposes_safe_report_routes_across_package_shapes(
        self,
    ) -> None:
        for platform_identifier in PACKAGE_LOCAL.ALPHA_HARNESS_SUPPORT:
            linux_runtime = (
                {
                    "minimumGlibc": "2.34",
                    "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"},
                    "executionEvidence": "ubuntu-22.04-only",
                }
                if platform_identifier.startswith("linux-")
                else "not-applicable"
            )
            for implementation in ("rust", "bun"):
                readme = PACKAGE_LOCAL.standalone_readme(
                    mode="alpha",
                    implementation=implementation,
                    version="0.15.0-alpha.1",
                    platform_identifier=platform_identifier,
                    archive_name=f"alpha-{implementation}-{platform_identifier}.tar.gz",
                    root_name=f"alpha-{implementation}-{platform_identifier}",
                    linux_runtime=linux_runtime,
                ).decode("utf-8")
                with self.subTest(
                    platform=platform_identifier, implementation=implementation
                ):
                    self.assertEqual(readme.count(SANITIZED_CLI_REPORT), 1)
                    self.assertEqual(readme.count(HARNESS_MODEL_REQUEST), 1)
                    self.assertEqual(readme.count(PRIVATE_VULNERABILITY_REPORT), 1)
                    self.assertIn("sanitized CLI problem", readme)
                    self.assertIn("suspected vulnerability privately", readme)
                    self.assertIn(
                        "Do not include credentials, account identifiers, private paths, "
                        "or raw provider output in a public report.",
                        readme,
                    )

        npm = PACKAGE_LOCAL.npm_readme("alpha", "0.15.0-alpha.1").decode("utf-8")
        self.assertEqual(npm.count(SANITIZED_CLI_REPORT), 1)
        self.assertEqual(npm.count(HARNESS_MODEL_REQUEST), 1)
        self.assertEqual(npm.count(PRIVATE_VULNERABILITY_REPORT), 1)
        self.assertIn("## Support and security", npm)
        self.assertIn("sanitized CLI problem", npm)
        self.assertIn("suspected vulnerability privately", npm)

    def test_alpha_guidance_names_dependency_evidence_and_install_provenance_limits(
        self,
    ) -> None:
        guides = [PACKAGE_LOCAL.npm_readme("alpha", "0.15.0-alpha.9").decode("utf-8")]
        guides.append(
            PACKAGE_LOCAL.standalone_readme(
                mode="alpha",
                implementation="rust",
                version="0.15.0-alpha.9",
                platform_identifier="darwin-arm64",
                archive_name="alpha-rust-darwin-arm64.tar.gz",
                root_name="alpha-rust-darwin-arm64",
                linux_runtime="not-applicable",
            ).decode("utf-8")
        )
        for guide in guides:
            with self.subTest(heading=guide.splitlines()[0]):
                self.assertIn("dependency-evidence.json", guide)
                self.assertIn(
                    "upstream installation convenience, not binary provenance",
                    guide,
                )
                self.assertIn("claude auth login", guide)
                self.assertNotIn("prime-agent login", guide)
                self.assertNotIn("omp login", guide)

    def test_development_and_static_guidance_make_no_public_report_claim(self) -> None:
        for platform_identifier in PACKAGE_LOCAL.PLATFORMS:
            guides = [
                PACKAGE_LOCAL.npm_readme(
                    "development", VERSION, platform_identifier
                ).decode("utf-8")
            ]
            linux_runtime = (
                {
                    "minimumGlibc": "2.34",
                    "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"},
                    "executionEvidence": "ubuntu-22.04-only",
                }
                if platform_identifier.startswith("linux-")
                else "not-applicable"
            )
            for implementation in ("rust", "bun"):
                guides.append(
                    PACKAGE_LOCAL.standalone_readme(
                        mode="development",
                        implementation=implementation,
                        version=VERSION,
                        platform_identifier=platform_identifier,
                        archive_name=(
                            f"development-{implementation}-{platform_identifier}.tar.gz"
                        ),
                        root_name=f"development-{implementation}-{platform_identifier}",
                        linux_runtime=linux_runtime,
                    ).decode("utf-8")
                )
            for guide in guides:
                with self.subTest(platform=platform_identifier):
                    self.assertNotIn(SANITIZED_CLI_REPORT, guide)
                    self.assertNotIn(PRIVATE_VULNERABILITY_REPORT, guide)
                    self.assertNotIn("publicly available", guide)
                    self.assertNotIn("published package", guide)

    def test_development_npm_requires_an_exact_supported_platform(self) -> None:
        with self.assertRaisesRegex(
            PACKAGE_LOCAL.PackageError,
            "development npm README requires an exact supported platform",
        ):
            PACKAGE_LOCAL.npm_readme("development", VERSION)
        with self.assertRaisesRegex(
            PACKAGE_LOCAL.PackageError,
            "development npm README requires an exact supported platform",
        ):
            PACKAGE_LOCAL.npm_readme("development", VERSION, "linux-x64-musl")

    def test_development_npm_is_local_platform_bound_and_registry_free(self) -> None:
        for platform_identifier in PACKAGE_LOCAL.PLATFORMS:
            with self.subTest(platform=platform_identifier):
                readme = PACKAGE_LOCAL.npm_readme(
                    "development", VERSION, platform_identifier
                ).decode("utf-8")
                platform_package = (
                    f"openprose-prose-cli-{platform_identifier}-{VERSION}.tgz"
                )
                meta_package = f"openprose-prose-cli-{VERSION}.tgz"
                self.assertNotIn("<platform>", readme)
                self.assertNotIn("Registry install", readme)
                self.assertNotIn("Registry repair", readme)
                self.assertNotIn("GitHub Release install", readme)
                self.assertIn(
                    "Install or repair only from the two sibling local tarballs",
                    readme,
                )
                self.assertIn(f'ASSET="{platform_package}"', readme)
                self.assertIn(f'ASSET="{meta_package}"', readme)
                self.assertIn(f'"./{platform_package}"', readme)
                self.assertIn(f'"./{meta_package}"', readme)
                self.assertIn(f'@openprose/prose-cli-{platform_identifier}"', readme)

    def test_development_npm_repair_and_uninstall_execute_exact_argv(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX npm guidance shim assertion")
        with tempfile.TemporaryDirectory(
            prefix="openprose-development-npm-guidance-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()
            arguments = workspace / "npm-arguments"
            npm = fake_bin / "npm"
            npm.write_text(
                "#!/bin/sh\n"
                ': > "$OPENPROSE_NPM_ARGUMENTS"\n'
                "for argument do\n"
                '  printf \'%s\\0\' "$argument" >> "$OPENPROSE_NPM_ARGUMENTS"\n'
                "done\n",
                "utf-8",
            )
            npm.chmod(0o755)
            home = workspace / "home with spaces"
            environment = clean_environment(workspace / "shell-environment")
            environment.update(
                {
                    "HOME": str(home),
                    "PATH": str(fake_bin),
                    "OPENPROSE_NPM_ARGUMENTS": str(arguments),
                }
            )

            for platform_identifier in PACKAGE_LOCAL.PLATFORMS:
                readme = PACKAGE_LOCAL.npm_readme(
                    "development", VERSION, platform_identifier
                ).decode("utf-8")
                install = next(
                    line.strip()
                    for line in readme.splitlines()
                    if line.strip().startswith("npm install --global --offline")
                )
                uninstall = next(
                    line.strip()
                    for line in readme.splitlines()
                    if line.strip().startswith("npm uninstall --global")
                )
                for label, command, expected in (
                    (
                        "repair",
                        install,
                        [
                            "install",
                            "--global",
                            "--offline",
                            "--ignore-scripts",
                            "--prefix",
                            str(home / ".local" / f"openprose-cli-{VERSION}"),
                            f"./openprose-prose-cli-{platform_identifier}-{VERSION}.tgz",
                            f"./openprose-prose-cli-{VERSION}.tgz",
                        ],
                    ),
                    (
                        "uninstall",
                        uninstall,
                        [
                            "uninstall",
                            "--global",
                            "--prefix",
                            str(home / ".local" / f"openprose-cli-{VERSION}"),
                            "@openprose/prose-cli",
                            f"@openprose/prose-cli-{platform_identifier}",
                        ],
                    ),
                ):
                    with self.subTest(platform=platform_identifier, operation=label):
                        completed = subprocess.run(
                            ["/bin/sh", "-c", command],
                            cwd=workspace,
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=5,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        observed = [
                            item.decode("utf-8")
                            for item in arguments.read_bytes().split(b"\0")[:-1]
                        ]
                        self.assertEqual(observed, expected)

    def test_posix_development_standalone_uses_only_sibling_local_evidence(
        self,
    ) -> None:
        for platform_identifier in PACKAGE_LOCAL.PLATFORMS:
            if platform_identifier == "win32-x64":
                continue
            linux_runtime = (
                {
                    "minimumGlibc": "2.34",
                    "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"},
                    "executionEvidence": "ubuntu-22.04-only",
                }
                if platform_identifier.startswith("linux-")
                else "not-applicable"
            )
            readme = PACKAGE_LOCAL.standalone_readme(
                mode="development",
                implementation="bun",
                version=VERSION,
                platform_identifier=platform_identifier,
                archive_name=f"development-{platform_identifier}.tar.gz",
                root_name=f"development-{platform_identifier}",
                linux_runtime=linux_runtime,
            ).decode("utf-8")
            with self.subTest(platform=platform_identifier):
                self.assertIn("sibling SHA256SUMS", readme)
                self.assertIn("sibling local package evidence", readme)
                self.assertNotIn("same GitHub release", readme)
                self.assertNotIn("Download and checksum an exact newer archive", readme)
                self.assertNotIn("admission report", readme)

    def test_windows_development_archives_are_static_validation_only(self) -> None:
        for implementation in ("rust", "bun"):
            readme = PACKAGE_LOCAL.standalone_readme(
                mode="development",
                implementation=implementation,
                version=VERSION,
                platform_identifier="win32-x64",
                archive_name=f"development-{implementation}-win32-x64.tar.gz",
                root_name=f"development-{implementation}-win32-x64",
                linux_runtime="not-applicable",
            ).decode("utf-8")
            with self.subTest(implementation=implementation):
                self.assertIn("static validation only", readme)
                self.assertIn("Native Windows execution is not admitted", readme)
                for forbidden in (
                    "$PWD",
                    "/bin/sh",
                    "rm -rf",
                    "npm install",
                    "codex login",
                    " cli harness",
                    " cli doctor",
                    " run ",
                    PROVIDER_CHARGE_BOUNDARY,
                ):
                    self.assertNotIn(forbidden, readme)

    def test_alpha_npm_readme_bytes_do_not_depend_on_packaging_platform(self) -> None:
        expected = PACKAGE_LOCAL.npm_readme("alpha", "0.15.0-alpha.1")
        for platform_identifier in PACKAGE_LOCAL.PLATFORMS:
            self.assertEqual(
                PACKAGE_LOCAL.npm_readme(
                    "alpha", "0.15.0-alpha.1", platform_identifier
                ),
                expected,
            )

    def test_source_development_install_binds_the_packaged_platform(self) -> None:
        readme = (CLI / "release" / "README.md").read_text("utf-8")
        development = readme.split("## Development package", 1)[1].split(
            "## Draft workflow", 1
        )[0]
        self.assertIn('manifest.get("mode") != "development"', development)
        self.assertIn('platform_id = manifest.get("platform")', development)
        self.assertIn("platform_id not in supported", development)
        self.assertIn(
            '"$PACKAGE_DIR/openprose-prose-cli-$PLATFORM_ID-$VERSION.tgz"',
            development,
        )
        self.assertNotIn(
            '"$PACKAGE_DIR/openprose-prose-cli-darwin-arm64-0.1.0.tgz"',
            development,
        )

    def test_source_development_install_executes_only_for_manifest_platform(
        self,
    ) -> None:
        if os.name == "nt":
            self.skipTest("source development example uses the documented POSIX shell")
        readme = (CLI / "release" / "README.md").read_text("utf-8")
        development = readme.split("## Development package", 1)[1].split(
            "## Draft workflow", 1
        )[0]
        shell = next(
            block.split("```", 1)[0].strip()
            for block in development.split("```sh")[1:]
            if "PLATFORM_ID=$(" in block.split("```", 1)[0]
        )
        with tempfile.TemporaryDirectory(
            prefix="openprose-source-development-install-"
        ) as temporary:
            workspace = Path(temporary).resolve()
            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()
            arguments = workspace / "npm-arguments"
            npm = fake_bin / "npm"
            npm.write_text(
                "#!/bin/sh\n"
                ': > "$OPENPROSE_NPM_ARGUMENTS"\n'
                "for argument do\n"
                '  printf \'%s\\0\' "$argument" >> "$OPENPROSE_NPM_ARGUMENTS"\n'
                "done\n",
                "utf-8",
            )
            npm.chmod(0o755)
            shell = shell.replace(
                "PACKAGE_DIR=/tmp/openprose-cli-artifacts",
                f"PACKAGE_DIR={posix_shell_quote(str(workspace))}",
                1,
            )
            environment = clean_environment(workspace / "source-guidance-environment")
            environment.update(
                {
                    "HOME": str(workspace / "home with spaces"),
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "OPENPROSE_NPM_ARGUMENTS": str(arguments),
                }
            )
            manifest_path = workspace / "release-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "mode": "development",
                        "version": VERSION,
                        "platform": "linux-arm64-gnu",
                    }
                ),
                "utf-8",
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", shell],
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            observed = [
                item.decode("utf-8")
                for item in arguments.read_bytes().split(b"\0")[:-1]
            ]
            self.assertEqual(
                observed,
                [
                    "install",
                    "--global",
                    "--offline",
                    "--ignore-scripts",
                    "--prefix",
                    str(
                        workspace
                        / "home with spaces"
                        / ".local"
                        / f"openprose-cli-{VERSION}"
                    ),
                    str(
                        workspace / f"openprose-prose-cli-linux-arm64-gnu-{VERSION}.tgz"
                    ),
                    str(workspace / f"openprose-prose-cli-{VERSION}.tgz"),
                ],
            )

            arguments.unlink()
            manifest_path.write_text(
                json.dumps(
                    {
                        "mode": "development",
                        "version": VERSION,
                        "platform": "linux-arm64-gnu; npm install forged",
                    }
                ),
                "utf-8",
            )
            refused = subprocess.run(
                ["/bin/sh", "-c", shell],
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(arguments.exists())

    def test_npm_packaging_passes_its_exact_platform_to_development_readme(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openprose-development-npm-package-readme-"
        ) as temporary:
            output = Path(temporary)
            meta, _ = PACKAGE_LOCAL.npm_packages(
                output,
                b"development-bun-binary",
                VERSION,
                "darwin-x64",
                0,
                "development",
                {
                    "formatVersion": "fixture-format",
                    "version": "fixture-version",
                    "sha256": "0" * 64,
                    "manifestSha256": "1" * 64,
                    "purpose": "sentinel-transport-test",
                    "releaseEligible": False,
                },
                "not-applicable",
                "development",
                b"fixture example\n",
            )
            readme = read_npm_file(meta, "README.md").decode("utf-8")
            self.assertIn(f'"./openprose-prose-cli-darwin-x64-{VERSION}.tgz"', readme)
            self.assertIn('"@openprose/prose-cli-darwin-x64"', readme)
            self.assertNotIn("<platform>", readme)


class NpmPublicMetadataTests(unittest.TestCase):
    @staticmethod
    def alpha_cohort() -> dict[str, object]:
        return PACKAGE_LOCAL.npm_cohort(
            mode="alpha",
            version="0.15.0-alpha.1",
            source_revision="a" * 40,
            image={
                "formatVersion": "fixture-format",
                "version": "fixture-version",
                "sha256": "0" * 64,
                "manifestSha256": "1" * 64,
                "purpose": "functional-alpha-placeholder",
                "releaseEligible": True,
            },
        )

    def test_alpha_meta_and_platform_manifests_have_complete_public_metadata(
        self,
    ) -> None:
        cohort = self.alpha_cohort()
        meta = PACKAGE_LOCAL.npm_meta_manifest(
            "0.15.0-alpha.1", cohort, b"fixture launcher"
        )
        platform_manifest = PACKAGE_LOCAL.npm_platform_manifest(
            "0.15.0-alpha.1",
            "darwin-arm64",
            b"fixture binary",
            "a" * 40,
            cohort["image"],
            cohort,
            "not-applicable",
        )
        common = {
            "homepage": "https://github.com/openprose/prose/tree/main/cli#readme",
            "bugs": {
                "url": "https://github.com/openprose/prose/issues/new?template=openprose-cli-bug.yml"
            },
            "license": "MIT",
            "publishConfig": {"access": "public"},
        }
        for label, manifest, directory in (
            ("meta", meta, "cli/bun/npm"),
            ("platform", platform_manifest, "cli/bun"),
        ):
            with self.subTest(package=label):
                for key, value in common.items():
                    self.assertEqual(manifest[key], value)
                self.assertEqual(
                    manifest["repository"],
                    {
                        "type": "git",
                        "url": "git+https://github.com/openprose/prose-cli.git",
                        "directory": directory,
                    },
                )
                self.assertFalse(manifest["openproseCohort"]["publicationAuthorized"])
        self.assertEqual(meta["name"], "@openprose/prose-cli")
        self.assertEqual(platform_manifest["name"], "@openprose/prose-cli-darwin-arm64")

    def test_development_manifests_do_not_receive_publish_configuration(self) -> None:
        cohort = PACKAGE_LOCAL.npm_cohort(
            mode="development",
            version=VERSION,
            source_revision="development",
            image={
                "formatVersion": "fixture-format",
                "version": "fixture-version",
                "sha256": "0" * 64,
                "manifestSha256": "1" * 64,
                "purpose": "sentinel-transport-test",
                "releaseEligible": False,
            },
        )
        meta = PACKAGE_LOCAL.npm_meta_manifest(VERSION, cohort, b"fixture launcher")
        platform_manifest = PACKAGE_LOCAL.npm_platform_manifest(
            VERSION,
            "darwin-arm64",
            b"fixture binary",
            "development",
            cohort["image"],
            cohort,
            "not-applicable",
        )
        self.assertNotIn("publishConfig", meta)
        self.assertNotIn("publishConfig", platform_manifest)


class DependencyEvidenceBindingTests(unittest.TestCase):
    def test_generated_inventory_rejects_overclaim_tamper_and_noncanonical_json(
        self,
    ) -> None:
        generated = subprocess.run(
            ["python3", str(DEPENDENCY_SCRIPT), "report", "--root", str(ROOT)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr.decode())
        base = json.loads(generated.stdout)
        mutations = []
        overclaim = json.loads(json.dumps(base))
        overclaim["releasePolicy"]["passed"] = True
        mutations.append(
            json.dumps(overclaim, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        changed_source = json.loads(json.dumps(base))
        changed_source["sources"][0]["sha256"] = "0" * 64
        mutations.append(
            json.dumps(changed_source, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        malformed_package = json.loads(json.dumps(base))
        malformed_package["inventories"]["cargo"]["packages"][0]["scopes"] = []
        mutations.append(
            json.dumps(
                malformed_package, sort_keys=True, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        mutations.append(json.dumps(base, sort_keys=True, indent=2).encode() + b"\n")
        mutations.append(
            generated.stdout.replace(
                b'{"authority":',
                b'{"schema":"openprose.dependency-evidence/1","authority":',
                1,
            )
        )
        for encoded in mutations:
            with self.subTest(prefix=encoded[:80]):
                completed = subprocess.CompletedProcess([], 0, encoded, b"")
                with mock.patch.object(
                    PACKAGE_LOCAL, "run_bounded", return_value=completed
                ):
                    with self.assertRaises(PACKAGE_LOCAL.PackageError):
                        PACKAGE_LOCAL.dependency_evidence()

    def test_integrity_records_are_closed_and_reasons_are_allowlisted(self) -> None:
        generated = subprocess.run(
            ["python3", str(DEPENDENCY_SCRIPT), "report", "--root", str(ROOT)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr.decode())
        base = json.loads(generated.stdout)
        mutations = []
        declared = json.loads(json.dumps(base))
        declared["inventories"]["cargo"]["packages"][0]["integrity"][
            "unexpected"
        ] = True
        mutations.append(declared)
        not_applicable = json.loads(json.dumps(base))
        local = next(
            package
            for package in not_applicable["inventories"]["bun"]["packages"]
            if package["integrity"]["status"] == "not-applicable"
        )
        local["integrity"]["reason"] = "invented"
        mutations.append(not_applicable)
        for report in mutations:
            encoded = (
                json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            completed = subprocess.CompletedProcess([], 0, encoded, b"")
            with self.subTest(), mock.patch.object(
                PACKAGE_LOCAL, "run_bounded", return_value=completed
            ):
                with self.assertRaisesRegex(PACKAGE_LOCAL.PackageError, "integrity"):
                    PACKAGE_LOCAL.dependency_evidence()


class LinuxRuntimePolicyTests(unittest.TestCase):
    def test_glibc_requirement_parser_selects_the_maximum_and_rejects_newer_than_floor(
        self,
    ) -> None:
        observed = PACKAGE_LOCAL.parse_required_glibc_maximum(
            b"Version: 1  File: libc.so.6  Cnt: 2\n"
            b"  0x00 0x00000000 4 Name: GLIBC_2.17\n"
            b"  0x00 0x00000000 2 Name: GLIBC_2.34\n",
            "rust",
        )
        self.assertEqual(observed, "2.34")
        completed = subprocess.CompletedProcess([], 0, b"Name: GLIBC_2.35\n", b"")
        with (
            mock.patch.object(
                PACKAGE_LOCAL, "run_bounded", return_value=completed
            ) as run,
            self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "newer than the admitted"
            ),
        ):
            PACKAGE_LOCAL.inspect_linux_glibc(
                Path("/fixture/prose"), "bun", Path("/exact/readelf")
            )
        self.assertEqual(
            run.call_args.args[0],
            ["/exact/readelf", "--version-info", "--wide", "/fixture/prose"],
        )

    def test_readelf_snapshot_requires_a_direct_absolute_executable(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".readelf-custody-") as raw:
            root = Path(raw)
            tool = root / "readelf"
            tool.write_bytes(b"#!/bin/sh\nexit 0\n")
            tool.chmod(0o700)
            snapshot, length, digest = PACKAGE_LOCAL.snapshot_executable_tool(
                tool, root / "owned-readelf", "readelf"
            )
            self.assertEqual(snapshot.read_bytes(), tool.read_bytes())
            self.assertEqual(length, len(tool.read_bytes()))
            self.assertEqual(digest, hashlib.sha256(tool.read_bytes()).hexdigest())
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o500)

            alias = root / "readelf-alias"
            alias.symlink_to(tool)
            with self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "direct non-symlink executable"
            ):
                PACKAGE_LOCAL.snapshot_executable_tool(
                    alias, root / "linked-snapshot", "readelf"
                )

            non_executable = root / "readelf-data"
            non_executable.write_bytes(b"data")
            non_executable.chmod(0o600)
            with self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "direct non-symlink executable"
            ):
                PACKAGE_LOCAL.snapshot_executable_tool(
                    non_executable, root / "data-snapshot", "readelf"
                )

            outside = root / "outside"
            outside.mkdir()
            nested = outside / "readelf"
            nested.write_bytes(tool.read_bytes())
            nested.chmod(0o700)
            parent_alias = root / "alias-parent"
            parent_alias.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "must not traverse a symlink"
            ):
                PACKAGE_LOCAL.snapshot_executable_tool(
                    parent_alias / "readelf",
                    root / "parent-linked-snapshot",
                    "readelf",
                )

            with self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "absolute executable path"
            ):
                PACKAGE_LOCAL.snapshot_executable_tool(
                    Path("relative-readelf"), root / "relative-snapshot", "readelf"
                )

    def test_linux_runtime_reauthenticates_readelf_between_each_inspection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".readelf-drift-") as raw:
            root = Path(raw)
            source = root / "readelf-source"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            source.chmod(0o700)
            snapshot, length, digest = PACKAGE_LOCAL.snapshot_executable_tool(
                source, root / "readelf", "readelf"
            )
            completed = subprocess.CompletedProcess([], 0, b"Name: GLIBC_2.34\n", b"")

            def mutate_after_first_inspection(*_args, **_kwargs):
                snapshot.chmod(0o700)
                snapshot.write_bytes(b"#!/bin/sh\n# changed\nexit 0\n")
                return completed

            with mock.patch.object(
                PACKAGE_LOCAL,
                "run_bounded",
                side_effect=mutate_after_first_inspection,
            ) as run, self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError,
                "owned readelf snapshot changed during verification",
            ):
                PACKAGE_LOCAL.linux_runtime_record(
                    root / "rust",
                    root / "bun",
                    snapshot,
                    length,
                    digest,
                )
            self.assertEqual(run.call_count, 1)


class DarwinCodeSignaturePolicyTests(unittest.TestCase):
    def test_strict_codesign_verification_is_fail_closed(self) -> None:
        accepted = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(
            PACKAGE_LOCAL, "run_bounded", return_value=accepted
        ) as run:
            PACKAGE_LOCAL.verify_darwin_code_signature(Path("/owned/prose"), "bun")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                "/owned/prose",
            ],
        )

        refused = subprocess.CompletedProcess([], 1, b"", b"invalid signature")
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(
            PACKAGE_LOCAL, "run_bounded", return_value=refused
        ), self.assertRaisesRegex(PACKAGE_LOCAL.PackageError, "failed macOS codesign"):
            PACKAGE_LOCAL.verify_darwin_code_signature(Path("/owned/prose"), "rust")


class AlphaPlatformResolutionGuidanceTests(unittest.TestCase):
    @staticmethod
    def run_probe(
        system: str,
        machine: str,
        *,
        glibc_output: str = "",
        getconf_status: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        getconf_body = (
            'test "$#" -eq 1 && test "$1" = GNU_LIBC_VERSION || return 97\n'
            f"printf '%s\\n' {posix_shell_quote(glibc_output)}\n"
            f"return {getconf_status}"
        )
        script = (
            "set -eu\n"
            "uname() {\n"
            '  case "$1" in\n'
            f"    -s) printf '%s\\n' {posix_shell_quote(system)} ;;\n"
            f"    -m) printf '%s\\n' {posix_shell_quote(machine)} ;;\n"
            "    *) return 96 ;;\n"
            "  esac\n"
            "}\n"
            f"getconf() {{\n{getconf_body}\n}}\n"
            f"{PACKAGE_LOCAL.alpha_platform_resolution_shell()}"
            "printf '%s\\n' \"$PLATFORM_ID\"\n"
        )
        with tempfile.TemporaryDirectory(
            prefix="openprose-platform-guidance-"
        ) as temporary:
            return subprocess.run(
                ["/bin/sh", "-c", script],
                cwd=temporary,
                env=clean_environment(Path(temporary) / "env"),
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )

    def test_platform_resolution_requires_exact_positive_glibc_evidence(self) -> None:
        for system, machine, glibc_output, expected in (
            ("Linux", "x86_64", "glibc 2.36", "linux-x64-gnu\n"),
            ("Linux", "aarch64", "glibc 2.17", "linux-arm64-gnu\n"),
            ("Linux", "arm64", "glibc 12.4", "linux-arm64-gnu\n"),
        ):
            with self.subTest(system=system, machine=machine):
                completed = self.run_probe(system, machine, glibc_output=glibc_output)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, expected)
                self.assertEqual(completed.stderr, "")

        darwin = self.run_probe(
            "Darwin", "arm64", glibc_output="must not be inspected", getconf_status=91
        )
        self.assertEqual(darwin.returncode, 0, darwin.stderr)
        self.assertEqual(darwin.stdout, "darwin-arm64\n")

        for label, output, status in (
            ("missing", "", 1),
            ("musl", "musl libc 1.2.5", 0),
            ("missing-minor", "glibc 2", 0),
            ("extra-component", "glibc 2.36.1", 0),
            ("mixed", "glibc 2.36\nmusl libc 1.2.5", 0),
            ("suffix", "glibc 2.36-alpine", 0),
            ("hostile", "glibc 2.36; echo forged", 0),
        ):
            with self.subTest(label=label):
                completed = self.run_probe(
                    "Linux", "x86_64", glibc_output=output, getconf_status=status
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(completed.stdout, "")
                self.assertIn("glibc could not be verified", completed.stderr)

        unsupported = self.run_probe("FreeBSD", "x86_64", glibc_output="glibc 2.36")
        self.assertEqual(unsupported.returncode, 1)
        self.assertEqual(unsupported.stdout, "")
        self.assertIn("unsupported functional-alpha platform", unsupported.stderr)


class DeterministicArchivePolicyTests(unittest.TestCase):
    def test_archive_writer_rejects_unsafe_duplicate_or_case_colliding_members(
        self,
    ) -> None:
        unsafe_sets = (
            [("../escape", b"x", 0o644)],
            [("/absolute", b"x", 0o644)],
            [("back\\slash", b"x", 0o644)],
            [("same", b"x", 0o644), ("same", b"y", 0o644)],
            [("Case", b"x", 0o644), ("case", b"y", 0o644)],
            [("bad-mode", b"x", 0o600)],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, members in enumerate(unsafe_sets):
                destination = root / f"unsafe-{index}.tar.gz"
                with self.subTest(members=members), self.assertRaises(
                    PACKAGE_LOCAL.PackageError
                ):
                    PACKAGE_LOCAL.tar_gz(destination, members, 0)
                self.assertFalse(destination.exists())

    def test_archive_writer_is_order_independent_and_emits_sorted_regular_members(
        self,
    ) -> None:
        members = [
            ("package/z.txt", b"z\n", 0o644),
            ("package/bin/prose", b"binary\n", 0o755),
            ("package/a.txt", b"a\n", 0o644),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"
            PACKAGE_LOCAL.tar_gz(first, members, 123)
            PACKAGE_LOCAL.tar_gz(second, reversed(members), 123)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first, "r:gz") as archive:
                records = archive.getmembers()
            self.assertEqual(
                [item.name for item in records], sorted(item[0] for item in members)
            )
            self.assertTrue(all(item.isfile() for item in records))


class ToolchainReceiptTests(unittest.TestCase):
    def test_rust_version_receipts_preserve_explicit_rustup_toolchain(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"rustc 1.87.0\n", b"")
        with mock.patch.object(
            shutil, "which", return_value="/exact/rustc"
        ), mock.patch.dict(
            os.environ,
            {"RUSTUP_TOOLCHAIN": "1.87.0-x86_64-unknown-linux-gnu"},
            clear=False,
        ), mock.patch.object(
            PACKAGE_LOCAL, "run_bounded", return_value=completed
        ) as run:
            self.assertEqual(
                PACKAGE_LOCAL.tool_version(["rustc", "--version"]), "rustc 1.87.0"
            )
        self.assertEqual(run.call_args.args[0], ["/exact/rustc", "--version"])
        self.assertEqual(
            run.call_args.kwargs["environment"]["RUSTUP_TOOLCHAIN"],
            "1.87.0-x86_64-unknown-linux-gnu",
        )

    def test_non_rust_version_receipt_does_not_inherit_rustup_selector(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"1.3.5\n", b"")
        with mock.patch.object(
            shutil, "which", return_value="/exact/bun"
        ), mock.patch.dict(
            os.environ, {"RUSTUP_TOOLCHAIN": "hostile"}, clear=False
        ), mock.patch.object(
            PACKAGE_LOCAL, "run_bounded", return_value=completed
        ) as run:
            self.assertEqual(PACKAGE_LOCAL.tool_version(["bun", "--version"]), "1.3.5")
        self.assertNotIn("RUSTUP_TOOLCHAIN", run.call_args.kwargs["environment"])


class LocalPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="openprose-package-test-")
        cls.root = Path(cls.temporary.name)
        cls.source_revision = f"package-local-test-{uuid.uuid4().hex}"
        cls.rust_binary = (
            cls.root
            / "rust-target"
            / "debug"
            / ("prose.exe" if os.name == "nt" else "prose")
        )
        cls.bun_binary = (
            cls.root / "bun-build" / ("prose.exe" if os.name == "nt" else "prose")
        )
        cargo = shutil.which("cargo")
        bun = shutil.which("bun")
        if cargo is None or bun is None:
            raise AssertionError(
                f"packaging construction requires cargo and bun; cargo={cargo!r}, bun={bun!r}"
            )
        build_env = build_environment(cls.root / "build-env", cls.source_revision)
        image_bundle = cls.root / "echo-v0.bundle.bin"
        image_checksum = cls.root / "echo-v0.bundle.sha256"
        image_build = subprocess.run(
            [
                sys.executable,
                str(CLI / "shared" / "image" / "bundle" / "image_bundle.py"),
                "build",
                str(ECHO_IMAGE_MANIFEST.parent),
                str(image_bundle),
                "--checksum",
                str(image_checksum),
            ],
            cwd=ROOT,
            env=clean_environment(cls.root / "image-build-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if image_build.returncode != 0:
            raise AssertionError(
                "isolated echo-v0 image construction failed\n"
                f"stdout:\n{image_build.stdout}\nstderr:\n{image_build.stderr}"
            )
        build_env.update(
            {
                "OPENPROSE_IMAGE_SOURCE_DIR": str(ECHO_IMAGE_MANIFEST.parent),
                "OPENPROSE_IMAGE_BUNDLE": str(image_bundle),
                "OPENPROSE_IMAGE_BUNDLE_CHECKSUM": str(image_checksum),
            }
        )
        cls.windows_host: Path | None = None
        if os.name == "nt":
            host_target = cls.root / "windows-host-target"
            host_env = build_environment(
                cls.root / "windows-host-build-env", cls.source_revision
            )
            host_env["CARGO_TARGET_DIR"] = str(host_target)
            host_build = subprocess.run(
                [
                    cargo,
                    "build",
                    "--manifest-path",
                    str(CLI / "platform" / "windows-process-host" / "Cargo.toml"),
                    "--bin",
                    "openprose-windows-process-host",
                    "--release",
                    "--locked",
                    "--offline",
                ],
                cwd=ROOT,
                env=host_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            cls.windows_host = host_target / "release" / PACKAGE_LOCAL.WINDOWS_HOST_NAME
            if host_build.returncode != 0 or not cls.windows_host.is_file():
                raise AssertionError(
                    "isolated Windows process-host construction failed\n"
                    f"stdout:\n{host_build.stdout}\nstderr:\n{host_build.stderr}"
                )
            build_env["OPENPROSE_WINDOWS_HOST_SHA256"] = sha256(cls.windows_host)
            build_env["OPENPROSE_WINDOWS_HOST_ADMISSION"] = "0"
        build_env["CARGO_TARGET_DIR"] = str(cls.root / "rust-target")
        rust_build = subprocess.run(
            [
                cargo,
                "build",
                "--manifest-path",
                str(CLI / "rust" / "Cargo.toml"),
                "--package",
                "prose-cli",
                "--bin",
                "prose",
                "--locked",
                "--offline",
            ],
            cwd=ROOT,
            env=build_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if rust_build.returncode != 0 or not cls.rust_binary.is_file():
            raise AssertionError(
                f"isolated Rust debug construction failed\nstdout:\n{rust_build.stdout}\nstderr:\n{rust_build.stderr}"
            )
        cls.bun_binary.parent.mkdir(parents=True)
        bun_build_env = build_environment(
            cls.root / "bun-build-env", cls.source_revision
        )
        if cls.windows_host is not None:
            bun_build_env["OPENPROSE_WINDOWS_HOST_SHA256"] = sha256(cls.windows_host)
            bun_build_env["OPENPROSE_WINDOWS_HOST_ADMISSION"] = "0"
        bun_build = subprocess.run(
            [
                bun,
                "--no-env-file",
                "--config=./config/empty-bunfig.toml",
                "run",
                "./scripts/image-bundle.ts",
                "build",
                "--outfile",
                str(cls.bun_binary),
                "--image-dir",
                str(ECHO_IMAGE_MANIFEST.parent),
                "--bundle",
                str(image_bundle),
                "--checksum",
                str(image_checksum),
            ],
            cwd=CLI / "bun",
            env=bun_build_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if bun_build.returncode != 0 or not cls.bun_binary.is_file():
            raise AssertionError(
                f"isolated Bun echo-v0 construction failed\nstdout:\n{bun_build.stdout}\nstderr:\n{bun_build.stderr}"
            )
        cls.bun_test_binary: Path | None = None
        if os.name != "nt":
            sentinel_bundle = cls.root / "sentinel.bundle.bin"
            sentinel_checksum = cls.root / "sentinel.bundle.sha256"
            sentinel_build = subprocess.run(
                [
                    sys.executable,
                    str(CLI / "shared" / "image" / "bundle" / "image_bundle.py"),
                    "build",
                    str(IMAGE_MANIFEST.parent),
                    str(sentinel_bundle),
                    "--checksum",
                    str(sentinel_checksum),
                ],
                cwd=ROOT,
                env=clean_environment(cls.root / "sentinel-build-env"),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if sentinel_build.returncode != 0:
                raise AssertionError(
                    "isolated sentinel image construction failed\n"
                    f"stdout:\n{sentinel_build.stdout}\nstderr:\n{sentinel_build.stderr}"
                )
            cls.bun_test_binary = cls.root / "bun-test-build" / "prose"
            cls.bun_test_binary.parent.mkdir(parents=True)
            bun_test_build = subprocess.run(
                [
                    bun,
                    "--no-env-file",
                    "--config=./config/empty-bunfig.toml",
                    "run",
                    "./scripts/image-bundle.ts",
                    "build",
                    "--outfile",
                    str(cls.bun_test_binary),
                    "--image-dir",
                    str(IMAGE_MANIFEST.parent),
                    "--bundle",
                    str(sentinel_bundle),
                    "--checksum",
                    str(sentinel_checksum),
                    "--test-seams",
                ],
                cwd=CLI / "bun",
                env=bun_build_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            if bun_test_build.returncode != 0 or not cls.bun_test_binary.is_file():
                raise AssertionError(
                    "isolated Bun test-seam construction failed\n"
                    f"stdout:\n{bun_test_build.stdout}\nstderr:\n{bun_test_build.stderr}"
                )
        cls.out_a = cls.root / "out-a"
        cls.out_b = cls.root / "out-b"
        for output in (cls.out_a, cls.out_b):
            command = [
                "python3",
                str(SCRIPT),
                "--mode",
                "development",
                "--version",
                VERSION,
                "--source-revision",
                cls.source_revision,
                "--source-date-epoch",
                "0",
                "--rust-binary",
                str(cls.rust_binary),
                "--bun-binary",
                str(cls.bun_binary),
                "--image-manifest",
                str(ECHO_IMAGE_MANIFEST),
            ]
            if cls.windows_host is not None:
                command.extend(["--windows-process-host", str(cls.windows_host)])
            command.extend(readelf_args())
            command.extend(
                [
                    "--out",
                    str(output),
                ]
            )
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=clean_environment(cls.root / f"pack-env-{output.name}"),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if completed.returncode != 0:
                raise AssertionError(
                    f"packaging failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
                )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def artifact(self, prefix: str, suffix: str) -> Path:
        matches = list(self.out_a.glob(f"{prefix}*{suffix}"))
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def run_packager(
        self,
        name: str,
        *,
        rust_binary: Path | None = None,
        bun_binary: Path | None = None,
        image_manifest: Path = ECHO_IMAGE_MANIFEST,
        version: str = VERSION,
        source_revision: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "python3",
            str(SCRIPT),
            "--mode",
            "development",
            "--version",
            version,
            "--source-revision",
            source_revision or self.source_revision,
            "--source-date-epoch",
            "0",
            "--rust-binary",
            str(rust_binary or self.rust_binary),
            "--bun-binary",
            str(bun_binary or self.bun_binary),
            "--image-manifest",
            str(image_manifest),
        ]
        if self.windows_host is not None:
            command.extend(["--windows-process-host", str(self.windows_host)])
        command.extend(readelf_args())
        command.extend(
            [
                "--out",
                str(self.root / name),
            ]
        )
        return subprocess.run(
            command,
            cwd=ROOT,
            env=clean_environment(self.root / f"{name}-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def windows_packager_args(self, name: str, host: Path | None) -> object:
        arguments = [
            "--mode",
            "development",
            "--version",
            VERSION,
            "--source-revision",
            self.source_revision,
            "--source-date-epoch",
            "0",
            "--rust-binary",
            str(self.rust_binary),
            "--bun-binary",
            str(self.bun_binary),
            "--image-manifest",
            str(ECHO_IMAGE_MANIFEST),
            "--out",
            str(self.root / name),
        ]
        if host is not None:
            arguments.extend(["--windows-process-host", str(host)])
        return PACKAGE_LOCAL.parser().parse_args(arguments)

    def run_simulated_windows_launcher(
        self, package_output: Path, name: str, *, tamper_host: bool = False
    ) -> dict[str, object]:
        node = shutil.which("node")
        self.assertIsNotNone(node)
        installation = self.root / name / "node_modules" / "@openprose"
        meta_root = installation / "prose-cli"
        platform_root = installation / "prose-cli-win32-x64"
        (meta_root / "bin").mkdir(parents=True)
        (platform_root / "bin").mkdir(parents=True)
        meta_package = package_output / f"openprose-prose-cli-{VERSION}.tgz"
        platform_package = (
            package_output / f"openprose-prose-cli-win32-x64-{VERSION}.tgz"
        )
        (meta_root / "bin" / "prose.js").write_bytes(
            read_npm_file(meta_package, "bin/prose.js")
        )
        (meta_root / "package.json").write_bytes(
            read_npm_file(meta_package, "package.json")
        )
        for member in (
            "package.json",
            "bin/prose.exe",
            f"bin/{PACKAGE_LOCAL.WINDOWS_HOST_NAME}",
        ):
            destination = platform_root / member
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(read_npm_file(platform_package, member))
        if tamper_host:
            host = platform_root / "bin" / PACKAGE_LOCAL.WINDOWS_HOST_NAME
            host.write_bytes(host.read_bytes() + b"tamper")
        probe = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const filename = process.argv[1];
const source = fs.readFileSync(filename, "utf8").replace(/^#![^\n]*\n/, "");
const spawned = [];
let stderr = "";
const fakeProcess = {
  platform: "win32", arch: "x64", versions: { node: "26.0.0" }, argv: ["node", filename, "--version"], report: undefined,
  stderr: { write(value) { stderr += String(value); } }, exitCode: undefined, pid: 7001,
  on() {}, off() {}, kill() {},
};
function controlledRequire(name) {
  if (name !== "node:child_process") return require(name);
  return { spawn(binary, args, options) {
    spawned.push({ binary, args, options });
    return {
      exitCode: null, signalCode: null, kill() {},
      once(event, callback) { if (event === "exit") { this.exitCode = 0; callback(0, null); } return this; },
    };
  }};
}
vm.runInNewContext(source, {
  require: controlledRequire, process: fakeProcess, Set,
  __dirname: require("node:path").dirname(filename), __filename: filename,
});
process.stdout.write(JSON.stringify({ spawned, stderr, exitCode: fakeProcess.exitCode }));
"""
        completed = subprocess.run(
            [node, "-e", probe, str((meta_root / "bin" / "prose.js").resolve())],
            cwd=self.root,
            env=clean_environment(self.root / f"{name}-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def write_candidate(
        self,
        path: Path,
        implementation: str,
        source_revision: str,
        *,
        mutate_path: Path | None = None,
        image_manifest: Path = IMAGE_MANIFEST,
        build_profile: str = "development",
        test_seams_enabled: bool = True,
        native_executable: bool = False,
    ) -> bytes:
        image = json.loads(image_manifest.read_text("utf-8"))
        expected_image = {
            "formatVersion": image["imageFormatVersion"],
            "version": image["imageVersion"],
            "sha256": image["aggregateSha256"]["sha256"],
            "releaseEligible": image["releaseEligible"],
        }
        if (platform.system() == "Linux" and build_profile == "release") or (
            platform.system() == "Darwin"
            and (build_profile == "release" or native_executable)
        ):
            if mutate_path is not None:
                raise AssertionError("release ELF fixture does not support mutation")
            doctor = json.dumps(
                {
                    "schema": "openprose.doctor-report/1",
                    "runner": {
                        "name": implementation,
                        "version": VERSION,
                        "commit": source_revision,
                    },
                    "build": {
                        "profile": build_profile,
                        "testSeamsEnabled": test_seams_enabled,
                    },
                    "image": expected_image,
                },
                separators=(",", ":"),
            )
            version_output = json.dumps(f"prose {VERSION} ({implementation})\n")
            doctor_output = json.dumps(doctor + "\n")
            source_path = path.with_suffix(".c")
            source_path.write_text(
                "#include <stdio.h>\n#include <string.h>\n"
                "int main(int argc, char **argv) {\n"
                "  for (int i = 1; i < argc; ++i) {\n"
                f'    if (strcmp(argv[i], "--version") == 0) {{ fputs({version_output}, stdout); return 0; }}\n'
                "  }\n"
                f'  if (argc >= 3 && strcmp(argv[argc - 2], "cli") == 0 && strcmp(argv[argc - 1], "doctor") == 0) {{ fputs({doctor_output}, stdout); return 10; }}\n'
                "  return 2;\n}\n",
                "utf-8",
            )
            compiler = shutil.which("cc")
            if compiler is None:
                raise AssertionError("native release packaging fixture requires cc")
            compiled = subprocess.run(
                [compiler, "-O0", "-o", str(path), str(source_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if compiled.returncode != 0:
                raise AssertionError(
                    f"cannot compile Linux release fixture: {compiled.stderr}"
                )
            return path.read_bytes()
        mutation = ""
        if mutate_path is not None:
            mutation = f"    open({str(mutate_path)!r}, 'ab').write(b'changed-after-snapshot')\n"
        source = (
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"version = 'prose {VERSION} ({implementation})'\n"
            f"image = {expected_image!r}\n"
            f"runner = {{'name': '{implementation}', 'version': '{VERSION}', 'commit': {source_revision!r}}}\n"
            f"build = {{'profile': {build_profile!r}, 'testSeamsEnabled': {test_seams_enabled!r}}}\n"
            "if '--version' in sys.argv:\n"
            f"{mutation}"
            "    print(version)\n"
            "elif sys.argv[-2:] == ['cli', 'doctor']:\n"
            "    print(json.dumps({'schema': 'openprose.doctor-report/1', 'runner': runner, 'build': build, 'image': image}, separators=(',', ':')))\n"
            "    raise SystemExit(10)\n"
            "else:\n"
            "    raise SystemExit(2)\n"
        ).encode()
        path.write_bytes(source)
        path.chmod(0o755)
        return source

    def install_npm(self, name: str, include_platform: bool) -> Path:
        npm = shutil.which("npm")
        self.assertIsNotNone(npm)
        prefix = self.root / name
        meta = self.out_a / f"openprose-prose-cli-{VERSION}.tgz"
        packages = [str(meta)]
        optional_flag: list[str] = ["--omit=optional"]
        if include_platform:
            platform_package = (
                self.out_a / f"openprose-prose-cli-{PLATFORM_ID}-{VERSION}.tgz"
            )
            packages.insert(0, str(platform_package))
            optional_flag = []
        completed = subprocess.run(
            [
                npm,
                "install",
                "--global",
                "--offline",
                "--ignore-scripts",
                *optional_flag,
                "--no-audit",
                "--no-fund",
                "--prefix",
                str(prefix),
                *packages,
            ],
            cwd=self.root,
            env={
                **clean_environment(self.root / f"{name}-install-env"),
                "npm_config_cache": str(self.root / f"{name}-npm-cache"),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        executable = npm_executable(prefix)
        self.assertTrue(executable.is_file(), executable)
        return executable

    def test_outputs_are_byte_reproducible_and_sha_manifest_is_complete(self) -> None:
        names_a = sorted(path.name for path in self.out_a.iterdir())
        names_b = sorted(path.name for path in self.out_b.iterdir())
        self.assertEqual(names_a, names_b)
        for name in names_a:
            self.assertEqual(sha256(self.out_a / name), sha256(self.out_b / name), name)

        sums = (self.out_a / "SHA256SUMS").read_text("utf-8").splitlines()
        recorded = {}
        for line in sums:
            digest, name = line.split("  ", 1)
            recorded[name] = digest
        expected = {path.name for path in self.out_a.iterdir()} - {"SHA256SUMS"}
        self.assertEqual(set(recorded), expected)
        for name, digest in recorded.items():
            self.assertEqual(digest, sha256(self.out_a / name), name)

        for path in self.out_a.iterdir():
            if not path.name.endswith((".tar.gz", ".tgz")):
                continue
            encoded = path.read_bytes()
            self.assertEqual(int.from_bytes(encoded[4:8], "little"), 0, path.name)
            with tarfile.open(path, "r:gz") as archive:
                members = archive.getmembers()
                hello_members = [
                    member
                    for member in members
                    if member.name.endswith("/examples/hello.prose.md")
                ]
            self.assertEqual(
                len({member.name.casefold() for member in members}),
                len(members),
                path.name,
            )
            expected_hello_count = (
                0 if f"-{PLATFORM_ID}-{VERSION}.tgz" in path.name else 1
            )
            self.assertEqual(len(hello_members), expected_hello_count, path.name)
            if hello_members:
                with tarfile.open(path, "r:gz") as archive:
                    extracted = archive.extractfile(hello_members[0])
                    self.assertIsNotNone(extracted)
                    self.assertEqual(extracted.read(), HELLO_EXAMPLE.read_bytes())
            for member in members:
                with self.subTest(archive=path.name, member=member.name):
                    self.assertTrue(member.isfile())
                    self.assertEqual(member.uid, 0)
                    self.assertEqual(member.gid, 0)
                    self.assertEqual(member.uname, "")
                    self.assertEqual(member.gname, "")
                    self.assertEqual(member.mtime, 0)
                    executable = member.name.endswith(
                        (
                            "/prose",
                            "/prose.exe",
                            "/prose.js",
                            f"/{PACKAGE_LOCAL.WINDOWS_HOST_NAME}",
                        )
                    )
                    self.assertEqual(member.mode, 0o755 if executable else 0o644)

    def test_input_binaries_are_snapshotted_once_and_original_mutation_cannot_change_packages(
        self,
    ) -> None:
        source_revision = "snapshot-fixture"
        rust = self.root / "snapshot-rust"
        bun = self.root / "snapshot-bun"
        original_rust = self.write_candidate(
            rust,
            "rust",
            source_revision,
            mutate_path=rust,
            image_manifest=ECHO_IMAGE_MANIFEST,
            test_seams_enabled=False,
        )
        original_bun = self.write_candidate(
            bun,
            "bun",
            source_revision,
            image_manifest=ECHO_IMAGE_MANIFEST,
            test_seams_enabled=False,
        )
        completed = self.run_packager(
            "snapshot-output",
            rust_binary=rust,
            bun_binary=bun,
            image_manifest=ECHO_IMAGE_MANIFEST,
            source_revision=source_revision,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotEqual(rust.read_bytes(), original_rust)
        output = self.root / "snapshot-output"
        rust_archive = next(output.glob("openprose-prose-cli-rust-*.tar.gz"))
        bun_archive = next(output.glob("openprose-prose-cli-bun-*.tar.gz"))
        self.assertEqual(
            unpack_archive(
                rust_archive, self.root / "snapshot-rust-unpack"
            ).read_bytes(),
            original_rust,
        )
        self.assertEqual(
            unpack_archive(bun_archive, self.root / "snapshot-bun-unpack").read_bytes(),
            original_bun,
        )

    def test_rejects_symlink_non_regular_and_empty_binary_inputs_before_execution(
        self,
    ) -> None:
        empty = self.root / "empty-binary"
        empty.touch()
        empty_result = self.run_packager("empty-input", rust_binary=empty)
        self.assertEqual(empty_result.returncode, 2)
        self.assertIn("non-empty regular file", empty_result.stderr)
        self.assertNotIn("Traceback", empty_result.stderr)

        directory = self.root / "directory-binary"
        directory.mkdir()
        directory_result = self.run_packager("directory-input", rust_binary=directory)
        self.assertEqual(directory_result.returncode, 2)
        self.assertIn("non-empty regular file", directory_result.stderr)
        self.assertNotIn("Traceback", directory_result.stderr)

        if platform.system() != "Windows":
            linked = self.root / "linked-binary"
            linked.symlink_to(self.rust_binary)
            linked_result = self.run_packager("symlink-input", rust_binary=linked)
            self.assertEqual(linked_result.returncode, 2)
            self.assertIn("non-symlink", linked_result.stderr)
            self.assertNotIn("Traceback", linked_result.stderr)

    def test_windows_packaging_requires_one_verified_sidecar_and_binds_it_everywhere(
        self,
    ) -> None:
        missing = self.windows_packager_args("windows-missing-host", None)
        with mock.patch.object(
            PACKAGE_LOCAL, "current_platform_id", return_value="win32-x64"
        ):
            with self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "requires --windows-process-host"
            ):
                PACKAGE_LOCAL.build(missing)

        host = self.root / PACKAGE_LOCAL.WINDOWS_HOST_NAME
        host_bytes = b"MZ\x00provider-free-windows-host-fixture\n"
        host.write_bytes(host_bytes)
        output = self.root / "windows-output"
        arguments = self.windows_packager_args(output.name, host)
        original_verify = PACKAGE_LOCAL.verify_product
        mutated = False

        def verify_after_mutating_original(*args: object, **kwargs: object) -> object:
            nonlocal mutated
            if not mutated:
                host.write_bytes(host.read_bytes() + b"changed-after-snapshot")
                mutated = True
            return original_verify(*args, **kwargs)

        with (
            mock.patch.object(
                PACKAGE_LOCAL, "current_platform_id", return_value="win32-x64"
            ),
            mock.patch.object(
                PACKAGE_LOCAL,
                "verify_product",
                side_effect=verify_after_mutating_original,
            ),
        ):
            PACKAGE_LOCAL.build(arguments)
        self.assertTrue(mutated)
        self.assertNotEqual(host.read_bytes(), host_bytes)

        expected_digest = hashlib.sha256(host_bytes).hexdigest()
        archives = sorted(output.glob("openprose-prose-cli-*-win32-x64.tar.gz"))
        self.assertEqual(len(archives), 2, archives)
        for archive_path in archives:
            with tarfile.open(archive_path, "r:gz") as archive:
                sidecars = [
                    name
                    for name in archive.getnames()
                    if name.endswith(PACKAGE_LOCAL.WINDOWS_HOST_NAME)
                ]
                self.assertEqual(len(sidecars), 1, archive_path)
                extracted = archive.extractfile(sidecars[0])
                self.assertIsNotNone(extracted)
                self.assertEqual(extracted.read(), host_bytes)

        npm_platform = output / f"openprose-prose-cli-win32-x64-{VERSION}.tgz"
        npm_manifest = json.loads(read_npm_file(npm_platform, "package.json"))
        self.assertEqual(
            npm_manifest["openproseWindowsProcessHost"],
            f"bin/{PACKAGE_LOCAL.WINDOWS_HOST_NAME}",
        )
        self.assertEqual(
            npm_manifest["openproseWindowsProcessHostByteLength"], len(host_bytes)
        )
        self.assertEqual(
            npm_manifest["openproseWindowsProcessHostSha256"], expected_digest
        )
        self.assertFalse(npm_manifest["openproseWindowsProcessHostAdmission"])
        self.assertEqual(
            read_npm_file(npm_platform, f"bin/{PACKAGE_LOCAL.WINDOWS_HOST_NAME}"),
            host_bytes,
        )

        manifest = json.loads((output / "release-manifest.json").read_text("utf-8"))
        self.assertEqual(
            manifest["windowsProcessHost"],
            {
                "path": PACKAGE_LOCAL.WINDOWS_HOST_NAME,
                "byteLength": len(host_bytes),
                "sha256": expected_digest,
                "admission": False,
            },
        )
        self.assertFalse(manifest["windowsJobObjectReleaseAdmission"])
        provenance = json.loads((output / "provenance.json").read_text("utf-8"))
        parameters = provenance["predicate"]["buildDefinition"]["externalParameters"]
        self.assertEqual(
            parameters["windowsProcessHost"], manifest["windowsProcessHost"]
        )
        dependencies = provenance["predicate"]["buildDefinition"][
            "resolvedDependencies"
        ]
        self.assertIn(
            {
                "uri": "openprose:windows-process-host",
                "digest": {"sha256": expected_digest},
            },
            dependencies,
        )
        sbom = json.loads((output / "sbom.cdx.json").read_text("utf-8"))
        sidecar_components = [
            component
            for component in sbom["components"]
            if component["name"] == PACKAGE_LOCAL.WINDOWS_HOST_NAME
        ]
        self.assertEqual(len(sidecar_components), 1)
        self.assertEqual(sidecar_components[0]["hashes"][0]["content"], expected_digest)

        launched = self.run_simulated_windows_launcher(
            output, "simulated-windows-install"
        )
        self.assertEqual(launched["stderr"], "")
        self.assertEqual(len(launched["spawned"]), 1)
        self.assertTrue(launched["spawned"][0]["binary"].endswith("bin/prose.exe"))
        self.assertEqual(launched["spawned"][0]["args"], ["--version"])
        refused = self.run_simulated_windows_launcher(
            output, "simulated-windows-tamper", tamper_host=True
        )
        self.assertEqual(refused["spawned"], [])
        self.assertEqual(refused["exitCode"], 1)
        self.assertIn("Windows process host integrity mismatch", refused["stderr"])
        self.assertIn(
            "npm argument vector (quote for your Windows shell)", refused["stderr"]
        )
        self.assertIn(f'"@openprose/prose-cli-win32-x64@{VERSION}"', refused["stderr"])
        self.assertIn(f'"@openprose/prose-cli@{VERSION}"', refused["stderr"])

    def test_windows_sidecar_refuses_empty_symlink_and_non_windows_confusion(
        self,
    ) -> None:
        empty = self.root / "empty-windows-host.exe"
        empty.touch()
        with mock.patch.object(
            PACKAGE_LOCAL, "current_platform_id", return_value="win32-x64"
        ):
            with self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "non-empty regular file"
            ):
                PACKAGE_LOCAL.build(
                    self.windows_packager_args("windows-empty-host", empty)
                )

        if platform.system() != "Windows":
            linked = self.root / "linked-windows-host.exe"
            linked.symlink_to(self.rust_binary)
            with mock.patch.object(
                PACKAGE_LOCAL, "current_platform_id", return_value="win32-x64"
            ):
                with self.assertRaisesRegex(PACKAGE_LOCAL.PackageError, "non-symlink"):
                    PACKAGE_LOCAL.build(
                        self.windows_packager_args("windows-linked-host", linked)
                    )

        if platform.system() != "Windows":
            host = self.root / "unexpected-windows-host.exe"
            host.write_bytes(b"MZ\x00unexpected")
            with self.assertRaisesRegex(
                PACKAGE_LOCAL.PackageError, "valid only for a Windows"
            ):
                PACKAGE_LOCAL.build(
                    self.windows_packager_args("non-windows-host-confusion", host)
                )

    def test_malformed_image_manifest_shapes_types_and_digests_fail_as_package_errors(
        self,
    ) -> None:
        valid = json.loads(IMAGE_MANIFEST.read_text("utf-8"))
        malformed: list[tuple[str, object | bytes]] = [
            ("root-list", []),
            ("root-null", None),
            (
                "missing-version",
                {key: value for key, value in valid.items() if key != "imageVersion"},
            ),
            ("format-type", {**valid, "imageFormatVersion": 1}),
            ("release-type", {**valid, "releaseEligible": "false"}),
            ("purpose", {**valid, "purpose": "invented"}),
            ("aggregate-type", {**valid, "aggregateSha256": []}),
            (
                "aggregate-missing",
                {
                    **valid,
                    "aggregateSha256": {"algorithm": "sha256-path-length-nul-v1"},
                },
            ),
            (
                "aggregate-algorithm",
                {
                    **valid,
                    "aggregateSha256": {
                        **valid["aggregateSha256"],
                        "algorithm": "sha256",
                    },
                },
            ),
            (
                "aggregate-digest",
                {
                    **valid,
                    "aggregateSha256": {**valid["aggregateSha256"], "sha256": "g" * 64},
                },
            ),
            ("invalid-utf8", b"\xff\xfe"),
        ]
        for name, value in malformed:
            with self.subTest(name=name):
                manifest = self.root / f"malformed-{name}.json"
                if isinstance(value, bytes):
                    manifest.write_bytes(value)
                else:
                    manifest.write_text(json.dumps(value), "utf-8")
                completed = self.run_packager(
                    f"malformed-output-{name}", image_manifest=manifest
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("package-local:", completed.stderr)
                self.assertIn("--image-manifest", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)

    def test_unpacked_rust_and_bun_archives_run_the_public_machine_surface(
        self,
    ) -> None:
        image = json.loads(ECHO_IMAGE_MANIFEST.read_text("utf-8"))
        for implementation in ("rust", "bun"):
            archive = self.artifact(
                f"openprose-prose-cli-{implementation}-{VERSION}-{PLATFORM_ID}",
                ".tar.gz",
            )
            prefix = self.root / f"archive-prefix-{implementation}"
            executable = unpack_archive(archive, prefix)
            self.assertEqual(
                (executable.parent / "LICENSE").read_bytes(),
                (ROOT / "LICENSE").read_bytes(),
            )
            self.assertEqual(
                (executable.parent / "examples" / "hello.prose.md").read_bytes(),
                HELLO_EXAMPLE.read_bytes(),
            )
            workspace = self.root / f"archive-workspace-{implementation}"
            workspace.mkdir()
            (workspace / ".env").write_text(
                "PROSE_HARNESS=mock\nOPENAI_API_KEY=must-not-load\n", "utf-8"
            )
            (workspace / "bunfig.toml").write_text('[run]\nshell = "system"\n', "utf-8")

            version = run_artifact(
                executable,
                ["--version"],
                workspace,
                self.root / f"version-env-{implementation}",
            )
            self.assertEqual(version.returncode, 0)
            self.assertEqual(
                version.stdout, f"prose {VERSION} ({implementation})\n".encode()
            )
            self.assertEqual(version.stderr, b"")

            help_result = run_artifact(
                executable,
                ["--help"],
                workspace,
                self.root / f"help-env-{implementation}",
            )
            self.assertEqual(help_result.returncode, 0)
            self.assertEqual(
                help_result.stdout,
                (CLI / "conformance/cases/fixtures/runner-help.txt").read_bytes(),
            )

            for arguments, schema, exit_code in [
                (
                    ["--output", "json", "cli", "config", "explain"],
                    "openprose.configuration-explanation/1",
                    0,
                ),
                (
                    ["--output", "json", "cli", "harness", "list"],
                    "openprose.harness-list/1",
                    0,
                ),
                (
                    ["--output", "json", "cli", "doctor"],
                    "openprose.doctor-report/1",
                    10,
                ),
                (
                    ["--output", "json", "cli", "auth", "status"],
                    "openprose.account-status/1",
                    10,
                ),
            ]:
                result = run_artifact(
                    executable,
                    arguments,
                    workspace,
                    self.root / f"machine-env-{implementation}-{schema}",
                )
                self.assertEqual(
                    result.returncode, exit_code, (implementation, result.stderr)
                )
                self.assertEqual(result.stderr, b"")
                report = json.loads(result.stdout)
                self.assertEqual(report["schema"], schema)
                if schema == "openprose.configuration-explanation/1":
                    self.assertEqual(report["values"]["harness"]["value"], "openprose")
                    self.assertFalse(report["values"]["color"]["value"])
                if schema == "openprose.doctor-report/1":
                    self.assertEqual(report["selectedHarness"], "openprose")
                    self.assertEqual(report["image"]["version"], image["imageVersion"])
                    self.assertEqual(
                        report["image"]["sha256"], image["aggregateSha256"]["sha256"]
                    )
                    self.assertEqual(
                        report["image"]["releaseEligible"], image["releaseEligible"]
                    )
                    self.assertEqual(
                        report["build"],
                        {"profile": "development", "testSeamsEnabled": False},
                    )
                self.assertNotIn("must-not-load", result.stdout.decode("utf-8"))

    def test_npm_meta_and_platform_packages_install_with_scripts_disabled(self) -> None:
        executable = self.install_npm("npm-positive-prefix", include_platform=True)
        workspace = self.root / "npm-workspace"
        workspace.mkdir()
        (workspace / ".env").write_text(
            "PROSE_HARNESS=mock\nOPENAI_API_KEY=must-not-load\n", "utf-8"
        )
        (workspace / "bunfig.toml").write_text('[run]\nshell = "system"\n', "utf-8")
        version = run_artifact(
            executable, ["--version"], workspace, self.root / "npm-version-env"
        )
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout, f"prose {VERSION} (bun)\n".encode())
        explained = run_artifact(
            executable,
            ["--output", "json", "cli", "config", "explain"],
            workspace,
            self.root / "npm-config-env",
        )
        self.assertEqual(explained.returncode, 0, explained.stderr)
        self.assertEqual(
            json.loads(explained.stdout)["values"]["harness"]["value"], "openprose"
        )
        self.assertNotIn("must-not-load", explained.stdout.decode("utf-8"))
        account = run_artifact(
            executable,
            ["--output", "json", "cli", "auth", "status"],
            workspace,
            self.root / "npm-account-env",
        )
        self.assertEqual(account.returncode, 10)
        self.assertEqual(
            json.loads(account.stdout)["schema"], "openprose.account-status/1"
        )

    def test_meta_package_has_exact_optional_dependencies_and_no_lifecycle_scripts(
        self,
    ) -> None:
        meta = self.out_a / f"openprose-prose-cli-{VERSION}.tgz"
        package = json.loads(read_npm_file(meta, "package.json"))
        self.assertEqual(package["name"], "@openprose/prose-cli")
        self.assertEqual(package["version"], VERSION)
        self.assertEqual(package["engines"], {"node": ">=22.22.3"})
        self.assertEqual(
            package["repository"],
            {
                "type": "git",
                "url": "git+https://github.com/openprose/prose-cli.git",
                "directory": "cli/bun/npm",
            },
        )
        self.assertNotIn("scripts", package)
        self.assertEqual(
            read_npm_file(meta, "examples/hello.prose.md"), HELLO_EXAMPLE.read_bytes()
        )

        self.assertEqual(package["bin"], {"prose": "bin/prose.js"})
        self.assertEqual(
            package["optionalDependencies"],
            {
                f"@openprose/prose-cli-{identifier}": VERSION
                for identifier in sorted(PACKAGE_LOCAL.PLATFORMS)
            },
        )

        platform_package = (
            self.out_a / f"openprose-prose-cli-{PLATFORM_ID}-{VERSION}.tgz"
        )
        platform_manifest = json.loads(read_npm_file(platform_package, "package.json"))
        self.assertEqual(
            platform_manifest["name"], f"@openprose/prose-cli-{PLATFORM_ID}"
        )
        self.assertEqual(platform_manifest["version"], VERSION)
        self.assertEqual(
            platform_manifest["repository"],
            {
                "type": "git",
                "url": "git+https://github.com/openprose/prose-cli.git",
                "directory": "cli/bun",
            },
        )
        self.assertEqual(platform_manifest["os"], [PLATFORM_ID.split("-", 1)[0]])
        self.assertEqual(platform_manifest["cpu"], [PLATFORM_ID.split("-")[1]])
        if PLATFORM_ID.startswith("linux-"):
            self.assertEqual(platform_manifest["libc"], ["glibc"])
            self.assertEqual(platform_manifest["openproseMinimumGlibc"], "2.34")
            self.assertEqual(
                platform_manifest["openproseLinuxExecutionEvidence"],
                "ubuntu-22.04-only",
            )
            self.assertLessEqual(
                PACKAGE_LOCAL.version_tuple(
                    platform_manifest["openproseRequiredGlibcMaximum"]
                ),
                PACKAGE_LOCAL.version_tuple("2.34"),
            )
        else:
            self.assertNotIn("libc", platform_manifest)
            self.assertNotIn("openproseMinimumGlibc", platform_manifest)
        self.assertIn(
            platform_manifest["openproseBinary"], ("bin/prose", "bin/prose.exe")
        )
        self.assertEqual(
            platform_manifest["openproseBinaryByteLength"],
            self.bun_binary.stat().st_size,
        )
        self.assertEqual(
            platform_manifest["openproseBinarySha256"], sha256(self.bun_binary)
        )
        self.assertEqual(
            platform_manifest["openproseSourceRevision"], self.source_revision
        )
        expected_cohort = PACKAGE_LOCAL.npm_cohort(
            mode="development",
            version=VERSION,
            source_revision=self.source_revision,
            image=PACKAGE_LOCAL.image_identity(
                json.loads(ECHO_IMAGE_MANIFEST.read_text("utf-8")),
                sha256(ECHO_IMAGE_MANIFEST),
            ),
        )
        self.assertEqual(package["openproseCohort"], expected_cohort)
        self.assertEqual(platform_manifest["openproseCohort"], expected_cohort)
        self.assertEqual(platform_manifest["openprosePlatform"], PLATFORM_ID)
        self.assertEqual(
            set(package["openproseLauncher"]), {"path", "byteLength", "sha256"}
        )
        self.assertEqual(package["openproseLauncher"]["path"], "bin/prose.js")
        self.assertEqual(
            {
                "compileTarget": platform_manifest["openproseBunCompileTarget"],
                "runtimeVariant": platform_manifest["openproseBunRuntimeVariant"],
            },
            PACKAGE_LOCAL.BUN_RUNTIME_BY_PLATFORM[PLATFORM_ID],
        )
        if PLATFORM_ID == "win32-x64":
            self.assertEqual(
                platform_manifest["openproseWindowsProcessHost"],
                f"bin/{PACKAGE_LOCAL.WINDOWS_HOST_NAME}",
            )
            self.assertEqual(
                platform_manifest["openproseWindowsProcessHostByteLength"],
                self.windows_host.stat().st_size,
            )
            self.assertEqual(
                platform_manifest["openproseWindowsProcessHostSha256"],
                sha256(self.windows_host),
            )
            self.assertFalse(platform_manifest["openproseWindowsProcessHostAdmission"])
        else:
            self.assertNotIn("openproseWindowsProcessHost", platform_manifest)
        image = json.loads(ECHO_IMAGE_MANIFEST.read_text("utf-8"))
        self.assertEqual(
            platform_manifest["openproseImage"],
            {
                "formatVersion": image["imageFormatVersion"],
                "manifestSha256": sha256(ECHO_IMAGE_MANIFEST),
                "purpose": image["purpose"],
                "releaseEligible": image["releaseEligible"],
                "sha256": image["aggregateSha256"]["sha256"],
                "version": image["imageVersion"],
            },
        )
        self.assertNotIn("scripts", platform_manifest)

        launcher = read_npm_file(meta, "bin/prose.js").decode("utf-8")
        launcher_bytes = launcher.encode("utf-8")
        self.assertEqual(
            package["openproseLauncher"]["byteLength"], len(launcher_bytes)
        )
        self.assertEqual(
            package["openproseLauncher"]["sha256"],
            hashlib.sha256(launcher_bytes).hexdigest(),
        )
        self.assertEqual(
            read_npm_file(meta, "LICENSE"), (ROOT / "LICENSE").read_bytes()
        )
        self.assertEqual(
            read_npm_file(platform_package, "LICENSE"), (ROOT / "LICENSE").read_bytes()
        )
        self.assertTrue(launcher.startswith("#!/usr/bin/env node\n"))
        self.assertNotIn("Bun.", launcher)
        self.assertNotIn("postinstall", launcher)
        self.assertNotIn("https:", launcher)
        self.assertNotIn("shell: true", launcher)

    def test_launcher_runs_at_the_exact_locally_available_node_floor(self) -> None:
        if not NODE_FLOOR.is_file():
            self.skipTest("exact local Node 22.22.3 authority is unavailable")
        observed = subprocess.run(
            [str(NODE_FLOOR), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(observed.stdout, "v22.22.3\n")
        prefix = self.root / "npm-node-floor-prefix"
        self.install_npm(prefix.name, include_platform=True)
        launcher = (
            npm_modules_root(prefix) / "@openprose" / "prose-cli" / "bin" / "prose.js"
        )
        completed = subprocess.run(
            [str(NODE_FLOOR), str(launcher), "--version"],
            cwd=self.root,
            env=clean_environment(self.root / "npm-node-floor-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, f"prose {VERSION} (bun)\n")

    def test_launcher_rejects_a_node_runtime_below_the_advertised_floor(self) -> None:
        node_20 = Path("/opt/homebrew/Cellar/node@20/20.19.2/bin/node")
        if not node_20.is_file():
            self.skipTest("local below-floor Node authority is unavailable")
        prefix = self.root / "npm-node-below-floor-prefix"
        self.install_npm(prefix.name, include_platform=True)
        launcher = (
            npm_modules_root(prefix) / "@openprose" / "prose-cli" / "bin" / "prose.js"
        )
        completed = subprocess.run(
            [str(node_20), str(launcher), "--version"],
            cwd=self.root,
            env=clean_environment(self.root / "npm-node-below-floor-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertIn("requires Node.js >=22.22.3", completed.stderr)
        self.assertIn("detected 20.19.2", completed.stderr)

    def test_generated_alpha_guidance_is_platform_exact_and_nonsemantic(self) -> None:
        for (
            platform_identifier,
            expected_harnesses,
        ) in PACKAGE_LOCAL.ALPHA_HARNESS_SUPPORT.items():
            readme = PACKAGE_LOCAL.standalone_readme(
                mode="alpha",
                implementation="bun",
                version="0.1.0-alpha.1",
                platform_identifier=platform_identifier,
                archive_name=f"artifact-{platform_identifier}.tar.gz",
                root_name=f"root-{platform_identifier}",
                linux_runtime=(
                    {
                        "minimumGlibc": "2.34",
                        "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"},
                        "executionEvidence": "ubuntu-22.04-only",
                    }
                    if platform_identifier.startswith("linux-")
                    else "not-applicable"
                ),
            ).decode("utf-8")
            with self.subTest(platform=platform_identifier):
                self.assertIn(
                    "Functional-alpha harness support on this platform: "
                    + display_harnesses(expected_harnesses),
                    readme,
                )
                self.assertIn(
                    f'"$PWD/root-{platform_identifier}/prose" run '
                    f'"$PWD/root-{platform_identifier}/examples/hello.prose.md"',
                    readme,
                )
                self.assertIn(
                    f'"$PWD/root-{platform_identifier}/prose" cli harness list', readme
                )
                self.assertIn(
                    "Continue from this same archive/checksum directory; "
                    "do not change into the extracted root.",
                    readme,
                )
                self.assertIn(
                    f"If you already changed into root-{platform_identifier}, "
                    "run: cd ..",
                    readme,
                )
                self.assertLess(
                    readme.index("Continue from this same archive/checksum directory"),
                    readme.index(
                        f'"$PWD/root-{platform_identifier}/prose" cli harness list'
                    ),
                )
                self.assertNotIn("\n  prose ", readme)
                self.assertIn(
                    "The echo-v0 image asks the selected harness to echo that "
                    "argument vector",
                    readme,
                )
                self.assertIn(
                    "The human-output safety policy may withhold task-bearing text",
                    readme,
                )
                self.assertIn("runner does not open or read the packaged file", readme)
                self.assertNotIn("echoes the contract instructions", readme)
                self.assertNotIn("only echoes", readme)
                for harness, marker in JOURNEY_VERSION_MARKERS.items():
                    if harness in expected_harnesses:
                        self.assertIn(marker, readme)
                    else:
                        self.assertNotIn(marker, readme)
                if "omp" in expected_harnesses:
                    self.assertIn(
                        "npm install --global bun@1.3.14 "
                        "@oh-my-pi/pi-coding-agent@18.0.9",
                        readme,
                    )
                self.assertNotIn("$EXAMPLE", readme)
                packaged_example = (
                    f'"$PWD/root-{platform_identifier}/examples/hello.prose.md"'
                )
                executable = f'"$PWD/root-{platform_identifier}/prose"'
                for harness in expected_harnesses:
                    assert_complete_harness_journey(
                        self,
                        readme,
                        harness=harness,
                        executable=executable,
                        example=packaged_example,
                    )
                for harness in set(JOURNEY_HEADINGS) - set(expected_harnesses):
                    self.assertNotIn(JOURNEY_HEADINGS[harness], readme)
                self.assertEqual(
                    readme.count(PROVIDER_CHARGE_BOUNDARY),
                    1 + len(expected_harnesses),
                )
                self.assertIn(
                    "First run with Codex 0.149.0-alpha.4.1",
                    readme,
                )
                self.assertIn(
                    "install that exact version and complete Codex sign-in before "
                    "you continue",
                    readme,
                )
                assert_actionable_codex_first_run(
                    self,
                    readme,
                    executable=executable,
                    example=packaged_example,
                )
                self.assertNotIn(
                    "with a supported harness already installed and signed in",
                    readme,
                )
                self.assertNotIn("repair pin", readme)
                self.assertNotIn("fully-qualified", readme)
                self.assertNotIn("unknown billing", readme)
                self.assertNotIn("repair/run", readme)
                self.assertNotIn("task argv", readme)
                if "omp" in expected_harnesses:
                    self.assertIn(
                        "fully qualified model identifier supported by the selected "
                        "harness and authentication route",
                        readme,
                    )
                    expected_profile_subject = (
                        "The Prime and OMP profiles"
                        if "prime" in expected_harnesses
                        else "The OMP profile"
                    )
                    self.assertIn(expected_profile_subject, readme)
                else:
                    self.assertNotIn("harness-managed login routes", readme)
                    self.assertNotIn("harness-managed login route", readme)
                    self.assertNotIn("The example model is illustrative", readme)
                self.assertNotIn("printf '# Hello", readme)
                if platform_identifier.startswith("darwin-"):
                    self.assertIn("ad-hoc signed and not notarized", readme)
                    self.assertIn(
                        f'xattr -d com.apple.quarantine "$PWD/root-{platform_identifier}/prose"',
                        readme,
                    )
                    self.assertNotIn(
                        f"xattr -d com.apple.quarantine root-{platform_identifier}/prose",
                        readme,
                    )
                    self.assertNotIn("Developer ID", readme)
                self.assertIn(HARNESS_MODEL_REQUEST, readme)
                self.assertIn(BENCHMARK_REQUEST, readme)

        npm = PACKAGE_LOCAL.npm_readme("alpha", "0.1.0-alpha.1").decode("utf-8")
        for marker in (
            "Prime: exact admitted versions are `0.7.0` and `0.8.1`",
            "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1",
            "OMP: exact admitted version is `18.0.9`",
            "requires Bun `1.3.14` or newer",
            "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
            "Codex: exact admitted version is `0.149.0-alpha.4.1`",
            "Claude: exact admitted version is `2.1.243`",
            "This is the exact functional-alpha allowlist",
            "Authentication is not automated",
            "later `cli doctor` and `run` commands use that saved bundle",
            "fully qualified model identifier supported by the selected harness and authentication route",
            "The CLI does not verify the provider, account, or billing route",
            "These profiles do not establish subscription billing",
            "Windows | Omitted from the functional alpha",
            '"$HOME/.local/openprose-cli-0.1.0-alpha.1/bin/prose" cli harness list',
            'GATEKEEPER_PROSE="$HOME/.local/openprose-cli-0.1.0-alpha.1/lib/node_modules/@openprose/prose-cli-$PLATFORM_ID/bin/prose"',
            'xattr -d com.apple.quarantine "$GATEKEEPER_PROSE"',
            "Registry repair",
            "Registry repair of the same version",
            '"@openprose/prose-cli-$PLATFORM_ID@0.1.0-alpha.1" "@openprose/prose-cli@0.1.0-alpha.1"',
            "Offline two-tarball repair",
            "## Upgrade",
            "## Uninstall",
            "Linux packages require glibc 2.34 or newer",
            "Execution evidence is Ubuntu 22.04 only; other Linux distributions "
            "are unverified",
            HARNESS_MODEL_REQUEST,
            BENCHMARK_REQUEST,
        ):
            self.assertIn(marker, npm)
        for marker in (
            'case "$(uname -s):$(uname -m)" in',
            "Linux:x86_64) PLATFORM_ARCH=linux-x64 ;;",
            "Linux:aarch64|Linux:arm64) PLATFORM_ARCH=linux-arm64 ;;",
            "Darwin:arm64) PLATFORM_ID=darwin-arm64 ;;",
            "Darwin:x86_64) PLATFORM_ID=darwin-x64 ;;",
            '*) echo "unsupported functional-alpha platform" >&2; exit 1 ;;',
            "GLIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null)",
            '"glibc "[0-9]*.[0-9]*) ;;',
            "GLIBC_NUMBER=${GLIBC_VERSION#glibc }",
            'PLATFORM_ID="$PLATFORM_ARCH-gnu"',
            'ASSET="openprose-prose-cli-$PLATFORM_ID-0.1.0-alpha.1.tgz"',
            '"./openprose-prose-cli-$PLATFORM_ID-0.1.0-alpha.1.tgz"',
            '"@openprose/prose-cli-$PLATFORM_ID"',
        ):
            self.assertIn(marker, npm)
        self.assertEqual(
            npm.count(PACKAGE_LOCAL.alpha_platform_resolution_shell("    ")), 4
        )
        self.assertNotIn("<platform>", npm)
        self.assertEqual(npm.count(PROVIDER_CHARGE_BOUNDARY), 5)
        self.assertNotIn("\n    prose ", npm)
        self.assertNotIn("$EXAMPLE", npm)
        npm_example = (
            '"$HOME/.local/openprose-cli-0.1.0-alpha.1/lib/node_modules/'
            '@openprose/prose-cli/examples/hello.prose.md"'
        )
        npm_executable = '"$HOME/.local/openprose-cli-0.1.0-alpha.1/bin/prose"'
        assert_actionable_codex_first_run(
            self,
            npm,
            executable=npm_executable,
            example=npm_example,
        )
        for harness in JOURNEY_HEADINGS:
            assert_complete_harness_journey(
                self,
                npm,
                harness=harness,
                executable=npm_executable,
                example=npm_example,
            )
        self.assertNotIn("# Choose exactly one selection:", npm)
        self.assertIn("run " + npm_example, npm)
        self.assertNotIn("@earendil-works/pi-coding-agent", npm)
        self.assertIn(
            "The echo-v0 image asks the selected harness to echo that argument vector",
            npm,
        )
        self.assertIn(
            "The human-output safety policy may withhold task-bearing text", npm
        )
        self.assertIn("runner does not open or read the packaged file", npm)
        self.assertNotIn("echoes the contract instructions", npm)
        self.assertNotIn("only echoes", npm)
        self.assertNotIn("complete, independently copyable journey", npm)
        self.assertNotIn("with one supported harness already installed", npm)
        self.assertNotIn("repair pin", npm)
        self.assertNotIn("fully-qualified", npm)
        self.assertNotIn("unknown billing", npm)
        self.assertNotIn("doctor/run", npm)
        self.assertNotIn("task argv", npm)
        self.assertEqual(
            npm.count('xattr -d com.apple.quarantine "$GATEKEEPER_PROSE"'), 1
        )

        release_guide = (CLI / "release" / "README.md").read_text("utf-8")
        self.assertNotIn("xattr -d com.apple.quarantine <path-to-prose>", release_guide)
        self.assertIn(
            "The generated package README contains the one exact quarantine\ncommand",
            release_guide,
        )
        self.assertEqual(release_guide.count(PROVIDER_CHARGE_BOUNDARY), 5)
        self.assertEqual(
            release_guide.count(PACKAGE_LOCAL.alpha_platform_resolution_shell()), 4
        )
        platform_blocks = [
            block.split("```", 1)[0]
            for block in release_guide.replace("```bash", "```sh").split("```sh")[1:]
            if 'PLATFORM_ID="$PLATFORM_ARCH-gnu"' in block.split("```", 1)[0]
        ]
        self.assertEqual(len(platform_blocks), 4)
        for block in platform_blocks:
            self.assertEqual(block.count("getconf GNU_LIBC_VERSION"), 1)
            self.assertLess(
                block.index("getconf GNU_LIBC_VERSION"),
                block.index('PLATFORM_ID="$PLATFORM_ARCH-gnu"'),
            )
        for heading, command in (
            (
                "Install the exact pair into a collision-safe",
                "npm install --global --offline",
            ),
            ("The registry repair for that same exact prefix", "npm install --global"),
            ("Remove\nonly this version with:", "npm uninstall --global"),
        ):
            section = release_guide[release_guide.index(heading) :]
            shell = section.split("```sh", 1)[1].split("```", 1)[0]
            self.assertIn("VERSION=0.15.0-alpha.1", shell)
            self.assertIn('INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"', shell)
            self.assertLess(
                shell.index('PLATFORM_ID="$PLATFORM_ARCH-gnu"'),
                shell.index(command),
            )
        for heading in (
            "For the first journey, install exact Codex version",
            *JOURNEY_HEADINGS.values(),
        ):
            start = release_guide.index(heading)
            later = [
                release_guide.find(candidate, start + len(heading))
                for candidate in JOURNEY_HEADINGS.values()
            ]
            end = min(
                (index for index in later if index >= 0), default=len(release_guide)
            )
            block = release_guide[start:end]
            self.assertEqual(block.count(PROVIDER_CHARGE_BOUNDARY), 1)
            self.assertLess(
                block.index(PROVIDER_CHARGE_BOUNDARY), block.index('" run ')
            )

    def test_omp_runtime_prerequisite_authority_fails_closed_on_drift(self) -> None:
        manifest = json.loads(
            PACKAGE_LOCAL.FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY.read_text("utf-8")
        )
        recipe = json.loads(
            PACKAGE_LOCAL.OMP_ADAPTER_RECIPE_AUTHORITY.read_text("utf-8")
        )
        manifest_path = self.root / "functional-alpha-mutated.json"
        recipe_path = self.root / "omp-recipe-copy.json"
        omp = next(
            item for item in manifest["adapters"] if item["adapterId"] == "omp/rpc"
        )
        omp["runtimePrerequisites"][0]["versionRange"] = ">=1.3.13"
        manifest_path.write_text(json.dumps(manifest), "utf-8")
        recipe_path.write_text(json.dumps(recipe), "utf-8")
        with mock.patch.object(
            PACKAGE_LOCAL, "FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY", manifest_path
        ), mock.patch.object(
            PACKAGE_LOCAL, "OMP_ADAPTER_RECIPE_AUTHORITY", recipe_path
        ), self.assertRaisesRegex(
            PACKAGE_LOCAL.PackageError, "runtime prerequisite"
        ):
            PACKAGE_LOCAL.alpha_version_guidance()

        manifest = json.loads(
            PACKAGE_LOCAL.FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY.read_text("utf-8")
        )
        omp = next(
            item for item in manifest["adapters"] if item["adapterId"] == "omp/rpc"
        )
        recipe["support"]["runtimePrerequisites"] = []
        manifest_path.write_text(json.dumps(manifest), "utf-8")
        recipe_path.write_text(json.dumps(recipe), "utf-8")
        with mock.patch.object(
            PACKAGE_LOCAL, "FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY", manifest_path
        ), mock.patch.object(
            PACKAGE_LOCAL, "OMP_ADAPTER_RECIPE_AUTHORITY", recipe_path
        ), self.assertRaisesRegex(
            PACKAGE_LOCAL.PackageError, "recipe"
        ):
            PACKAGE_LOCAL.alpha_version_guidance()

    def test_release_readme_separates_platform_and_admission_ids_and_closes_repairs(
        self,
    ) -> None:
        readme = (ROOT / "cli" / "release" / "README.md").read_text("utf-8")
        platform_assignment = "Darwin:arm64) PLATFORM_ID=darwin-arm64 ;;"
        target_assignment = "darwin-arm64) ADMISSION_TARGET=darwin-arm ;;"
        self.assertIn(platform_assignment, readme)
        self.assertIn(target_assignment, readme)
        self.assertLess(
            readme.index(platform_assignment), readme.index(target_assignment)
        )
        self.assertEqual(readme.count('--target-id "$ADMISSION_TARGET"'), 2)
        self.assertNotIn('--target-id "$TARGET_ID"', readme)
        self.assertIn(
            'PROSE="$INSTALL_PREFIX/bin/prose"',
            readme,
        )
        self.assertNotIn("openprose-cli-0.15.0-alpha.1", readme)
        self.assertIn("npm install --global @openai/codex@0.149.0-alpha.4.1", readme)
        self.assertIn("npm install --global @anthropic-ai/claude-code@2.1.243", readme)
        self.assertIn(
            "admitted OMP version is `18.0.9`, and it requires Bun `>=1.3.14`",
            " ".join(readme.split()),
        )
        self.assertIn(
            "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
            readme,
        )
        self.assertIn("Evidence schema v5 and matrix schema v4", readme)
        self.assertIn(
            "`31d81c55c8c90a7358b1cd8c5a0ccba631290a83` completed a 12-cell Darwin",
            readme,
        )
        self.assertIn(
            "../conformance/live-alpha/evidence/"
            "31d81c55c8c90a7358b1cd8c5a0ccba631290a83/REPORT.md",
            readme,
        )
        self.assertIn("not a reliability sample", readme)
        self.assertIn("provider spend is", readme)
        self.assertIn("unverified and semantic status is `not-applicable`", readme)
        self.assertIn("malformed-protocol failures", readme)
        self.assertIn(
            "The runner does not open or read the packaged example file",
            " ".join(readme.split()),
        )

        development_section = readme.split("## Development package", 1)[1].split(
            "## Draft workflow", 1
        )[0]
        self.assertIn(
            "The explicit `build_local.py` test driver does not produce package inputs",
            " ".join(development_section.split()),
        )
        self.assertNotIn("build_local.py --package", development_section)
        self.assertIn("--out /tmp/openprose-cli-artifacts", development_section)
        self.assertIn(
            "cargo build --manifest-path cli/rust/Cargo.toml --locked -p prose-cli",
            " ".join(development_section.split()),
        )
        self.assertIn("cli/shared/image/echo-v0", development_section)
        self.assertIn("python3 cli/ci/package_local.py", development_section)
        self.assertIn("--mode development", " ".join(development_section.split()))
        normalized_development = " ".join(development_section.split())
        self.assertIn(
            "Ordinary `bun run build` produces the development `echo-v0` profile "
            "with test seams disabled",
            normalized_development,
        )
        self.assertIn(
            "Only the explicit test build includes provider-free conformance seams",
            normalized_development,
        )
        self.assertNotIn(
            "Ordinary `bun run build` deliberately includes hermetic provider-free "
            "conformance seams",
            normalized_development,
        )
        self.assertNotIn(
            "--image-manifest cli/shared/image/sentinel-v1", development_section
        )

        development = PACKAGE_LOCAL.standalone_readme(
            mode="development",
            implementation="bun",
            version="0.0.0-dev",
            platform_identifier="darwin-arm64",
            archive_name="development.tar.gz",
            root_name="development",
            linux_runtime="not-applicable",
        ).decode("utf-8")
        self.assertNotIn("echo-v0 transports", development)
        self.assertNotIn("ad-hoc signed and not notarized", development)
        self.assertNotIn("This alpha executable", development)
        self.assertNotIn(
            "macOS alpha executables",
            PACKAGE_LOCAL.npm_readme("development", "0.0.0-dev", "darwin-arm64").decode(
                "utf-8"
            ),
        )

    def test_missing_platform_package_is_actionable_and_never_downloads(self) -> None:
        executable = self.install_npm("npm-missing-prefix", include_platform=False)
        result = run_artifact(
            executable, ["--version"], self.root, self.root / "npm-missing-run-env"
        )
        expected_package = f"@openprose/prose-cli-{PLATFORM_ID}@{VERSION}"
        self.assertEqual(result.returncode, 1)
        diagnostic = result.stderr.decode("utf-8")
        self.assertIn(expected_package, diagnostic)
        self.assertIn("optional platform package", diagnostic)
        self.assertIn(
            expected_registry_repair_command(self.root / "npm-missing-prefix"),
            diagnostic,
        )

    def test_registry_repair_with_a_control_bearing_prefix_is_not_copyable(
        self,
    ) -> None:
        if platform.system() == "Windows":
            self.skipTest("POSIX control-bearing installation-prefix assertion")
        prefix_name = "npm-prefix-\x1b]0;owned\x07\r\t\x85\u2028\u2029"
        source_prefix = self.root / "npm-control-prefix-source"
        self.install_npm(source_prefix.name, include_platform=False)
        prefix = self.root / prefix_name
        source_prefix.rename(prefix)
        executable = npm_executable(prefix)
        self.assertTrue(executable.is_file(), executable)
        result = run_artifact(
            executable,
            ["--version"],
            self.root,
            self.root / "npm-control-prefix-run-env",
        )
        self.assertEqual(result.returncode, 1)
        diagnostic = result.stderr.decode("utf-8")
        self.assertIn("optional platform package", diagnostic)
        self.assertIn(
            "No copyable registry repair command is available because the npm "
            "installation prefix contains terminal control characters",
            diagnostic,
        )
        self.assertNotIn("npm install --global", diagnostic)
        for unsafe in ("\x1b", "\x07", "\r", "\t", "\x85", "\u2028", "\u2029"):
            self.assertNotIn(unsafe, diagnostic)

    def test_registry_repair_is_platform_complete_and_posix_shell_safe(self) -> None:
        if platform.system() == "Windows":
            self.skipTest("POSIX repair-command assertion")
        prefix_name = (
            "npm repair $(_openprose_substitution_probe) "
            "`_openprose_backtick_probe` ' prefix"
        )
        prefix = self.root / prefix_name
        executable = self.install_npm(prefix_name, include_platform=True)
        platform_root = npm_platform_root(prefix)
        manifest = json.loads((platform_root / "package.json").read_text("utf-8"))
        binary = platform_root / manifest["openproseBinary"]
        binary.write_bytes(binary.read_bytes() + b"tamper")

        refused = run_artifact(
            executable,
            ["--version"],
            self.root,
            self.root / "npm-hostile-repair-run-env",
        )
        self.assertEqual(refused.returncode, 1)
        diagnostic = refused.stderr.decode("utf-8")
        label = "Registry repair in this installation prefix: "
        self.assertIn(label, diagnostic)
        command = diagnostic.split(label, 1)[1].strip()
        expected_arguments = [
            "install",
            "--global",
            "--ignore-scripts",
            "--prefix",
            str(prefix.resolve()),
            f"@openprose/prose-cli-{PLATFORM_ID}@{VERSION}",
            f"@openprose/prose-cli@{VERSION}",
        ]
        expected_command = expected_registry_repair_command(prefix)
        self.assertEqual(command, expected_command)

        fake_bin = self.root / "npm-hostile-repair-bin"
        fake_bin.mkdir()
        argv_path = self.root / "npm-hostile-repair-argv"
        substitution_marker = self.root / "npm-hostile-repair-substitution"
        recorder = fake_bin / "npm"
        recorder.write_text(
            "#!/bin/sh\n"
            'set -eu\n: > "$OPENPROSE_NPM_ARGV"\n'
            'for argument do\n  printf \'%s\\0\' "$argument" >> "$OPENPROSE_NPM_ARGV"\ndone\n',
            "utf-8",
        )
        recorder.chmod(0o755)
        for name in (
            "_openprose_substitution_probe",
            "_openprose_backtick_probe",
        ):
            probe = fake_bin / name
            probe.write_text(
                "#!/bin/sh\n" 'printf executed > "$OPENPROSE_SUBSTITUTION_MARKER"\n',
                "utf-8",
            )
            probe.chmod(0o755)
        environment = clean_environment(self.root / "npm-hostile-repair-shell-env")
        environment.update(
            {
                "PATH": str(fake_bin),
                "OPENPROSE_NPM_ARGV": str(argv_path),
                "OPENPROSE_SUBSTITUTION_MARKER": str(substitution_marker),
            }
        )
        invoked = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(invoked.returncode, 0, invoked.stderr)
        self.assertFalse(substitution_marker.exists())
        self.assertEqual(
            argv_path.read_bytes().split(b"\0")[:-1],
            [argument.encode("utf-8") for argument in expected_arguments],
        )

    def test_installed_launcher_reports_an_actionable_unsupported_platform(
        self,
    ) -> None:
        prefix = self.root / "npm-unsupported-prefix"
        self.install_npm(prefix.name, include_platform=False)
        launcher = (
            npm_modules_root(prefix) / "@openprose" / "prose-cli" / "bin" / "prose.js"
        )
        node = shutil.which("node")
        self.assertIsNotNone(node)
        probe = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const filename = process.argv[1];
const source = fs.readFileSync(filename, "utf8").replace(/^#![^\n]*\n/, "");
let stderr = "";
const fakeProcess = {
  platform: "freebsd",
  arch: "x64",
  versions: { node: "26.0.0" },
  argv: ["node", filename, "--version"],
  report: undefined,
  stderr: { write(value) { stderr += String(value); } },
  exitCode: 0,
};
vm.runInNewContext(source, { require, process: fakeProcess, Set });
process.stdout.write(JSON.stringify({ exitCode: fakeProcess.exitCode, stderr }));
"""
        completed = subprocess.run(
            [node, "-e", probe, str(launcher)],
            cwd=self.root,
            env=clean_environment(self.root / "npm-unsupported-run-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertEqual(observed["exitCode"], 1)
        self.assertIn("platform freebsd-x64 is unsupported", observed["stderr"])
        self.assertIn("Supported platforms: darwin-arm64", observed["stderr"])

    def test_installed_launcher_refuses_a_mismatched_platform_package_version(
        self,
    ) -> None:
        prefix = self.root / "npm-mismatch-prefix"
        executable = self.install_npm(prefix.name, include_platform=True)
        manifest_path = npm_platform_root(prefix) / "package.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["version"] = "9.9.9"
        manifest_path.write_text(json.dumps(manifest), "utf-8")
        result = run_artifact(
            executable, ["--version"], self.root, self.root / "npm-mismatch-run-env"
        )
        self.assertEqual(result.returncode, 1)
        diagnostic = result.stderr.decode("utf-8")
        self.assertIn(f"installed @openprose/prose-cli-{PLATFORM_ID}@9.9.9", diagnostic)
        self.assertIn(f"requires exactly {VERSION}", diagnostic)
        self.assertIn(
            expected_registry_repair_command(prefix),
            diagnostic,
        )

    def test_installed_launcher_escapes_tampered_manifest_identity_controls(
        self,
    ) -> None:
        for field in ("version", "name"):
            with self.subTest(field=field):
                prefix = self.root / f"npm-hostile-manifest-{field}"
                executable = self.install_npm(prefix.name, include_platform=True)
                manifest_path = npm_platform_root(prefix) / "package.json"
                manifest = json.loads(manifest_path.read_text("utf-8"))
                hostile = f"forged-{field}-\x1b]0;owned\x07\r\t\x85\u2028\u2029"
                manifest[field] = hostile
                manifest_path.write_text(json.dumps(manifest), "utf-8")

                result = run_artifact(
                    executable,
                    ["--version"],
                    self.root,
                    self.root / f"npm-hostile-manifest-{field}-run-env",
                )
                self.assertEqual(result.returncode, 1)
                diagnostic = result.stderr.decode("utf-8")
                self.assertIn(f"forged-{field}-", diagnostic)
                self.assertIn("\\u{001B}]0;owned\\u{0007}", diagnostic)
                self.assertIn("\\r\\t\\u{0085}\\u{2028}\\u{2029}", diagnostic)
                for unsafe in (
                    "\x1b",
                    "\x07",
                    "\r",
                    "\t",
                    "\x85",
                    "\u2028",
                    "\u2029",
                ):
                    self.assertNotIn(unsafe, diagnostic)

    def test_installed_launcher_escapes_caught_native_error_text(self) -> None:
        if platform.system() == "Windows":
            self.skipTest("POSIX control-bearing native-path assertion")
        prefix_name = "npm-native-error-\x1b]0;owned\x07\r\t\x85\u2028\u2029"
        source_prefix = self.root / "npm-native-error-source"
        self.install_npm(source_prefix.name, include_platform=True)
        prefix = self.root / prefix_name
        source_prefix.rename(prefix)
        executable = npm_executable(prefix)
        self.assertTrue(executable.is_file(), executable)
        meta_manifest = (
            npm_modules_root(prefix) / "@openprose" / "prose-cli" / "package.json"
        )
        meta_manifest.unlink()

        result = run_artifact(
            executable,
            ["--version"],
            self.root,
            self.root / "npm-native-error-run-env",
        )
        self.assertEqual(result.returncode, 1)
        diagnostic = result.stderr.decode("utf-8")
        self.assertIn("cannot resolve", diagnostic)
        self.assertIn("ENOENT", diagnostic)
        self.assertIn("\\u{001B}]0;owned\\u{0007}", diagnostic)
        self.assertIn("\\r\\t\\u{0085}\\u{2028}\\u{2029}", diagnostic)
        for unsafe in (
            "\x1b",
            "\x07",
            "\r",
            "\t",
            "\x85",
            "\u2028",
            "\u2029",
        ):
            self.assertNotIn(unsafe, diagnostic)

    def test_installed_launcher_refuses_below_floor_glibc_before_spawn(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node)
        scope = self.root / "below-floor" / "node_modules" / "@openprose"
        meta_root = scope / "prose-cli"
        platform_root = scope / "prose-cli-linux-x64-gnu"
        (meta_root / "bin").mkdir(parents=True)
        (platform_root / "bin").mkdir(parents=True)
        image_value = json.loads(IMAGE_MANIFEST.read_text("utf-8"))
        image = PACKAGE_LOCAL.image_identity(image_value, sha256(IMAGE_MANIFEST))
        cohort = PACKAGE_LOCAL.npm_cohort(
            mode="development",
            version=VERSION,
            source_revision=self.source_revision,
            image=image,
        )
        launcher = PACKAGE_LOCAL.LAUNCHER.read_text("utf-8").replace(
            "__OPENPROSE_COHORT__",
            json.dumps(cohort, sort_keys=True, separators=(",", ":")),
        )
        launcher_path = meta_root / "bin" / "prose.js"
        launcher_path.write_text(launcher, "utf-8")
        (meta_root / "package.json").write_text(
            json.dumps(
                PACKAGE_LOCAL.npm_meta_manifest(VERSION, cohort, launcher.encode())
            ),
            "utf-8",
        )
        binary = platform_root / "bin" / "prose"
        binary.write_bytes(b"never-spawn\n")
        manifest = PACKAGE_LOCAL.npm_platform_manifest(
            VERSION,
            "linux-x64-gnu",
            binary.read_bytes(),
            self.source_revision,
            image,
            cohort,
            {
                "minimumGlibc": "2.34",
                "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"},
                "executionEvidence": "ubuntu-22.04-only",
            },
        )
        (platform_root / "package.json").write_text(json.dumps(manifest), "utf-8")
        probe = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const filename = process.argv[1];
const source = fs.readFileSync(filename, "utf8").replace(/^#![^\n]*\n/, "");
const spawned = [];
let stderr = "";
const fakeProcess = {
  platform: "linux", arch: "x64", versions: { node: "26.0.0" }, argv: ["node", filename, "--version"],
  report: { getReport() { return { header: { glibcVersionRuntime: "2.33" } }; } },
  stderr: { write(value) { stderr += String(value); } }, exitCode: undefined,
  pid: 7001, on() {}, off() {}, kill() {},
};
function controlledRequire(name) {
  if (name !== "node:child_process") return require(name);
  return { spawn(...args) { spawned.push(args); throw new Error("spawned"); } };
}
vm.runInNewContext(source, {
  require: controlledRequire, process: fakeProcess, Set,
  __dirname: require("node:path").dirname(filename), __filename: filename,
});
process.stdout.write(JSON.stringify({ spawned, stderr, exitCode: fakeProcess.exitCode }));
"""
        completed = subprocess.run(
            [node, "-e", probe, str(launcher_path.resolve())],
            cwd=self.root,
            env=clean_environment(self.root / "below-floor-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertEqual(observed["spawned"], [])
        self.assertEqual(observed["exitCode"], 1)
        self.assertIn("requires glibc >= 2.34; detected 2.33", observed["stderr"])
        self.assertIn("Ubuntu 22.04", observed["stderr"])
        self.assertLess(len(observed["stderr"]), 600)

    def test_installed_launcher_refuses_byte_length_tamper_and_symlink_substitution(
        self,
    ) -> None:
        if platform.system() == "Windows":
            self.skipTest("POSIX symlink assertion")
        prefix = self.root / "npm-integrity-prefix"
        executable = self.install_npm(prefix.name, include_platform=True)
        platform_root = npm_platform_root(prefix)
        manifest = json.loads((platform_root / "package.json").read_text("utf-8"))
        binary = platform_root / manifest["openproseBinary"]
        original = binary.read_bytes()

        sibling = platform_root.parent / "prose-cli-sibling" / "bin" / "prose"
        sibling.parent.mkdir(parents=True)
        sibling.write_bytes(original)
        sibling.chmod(0o755)
        sibling_manifest = dict(manifest)
        sibling_manifest["openproseBinary"] = "../prose-cli-sibling/bin/prose"
        (platform_root / "package.json").write_text(
            json.dumps(sibling_manifest), "utf-8"
        )
        escaped = run_artifact(
            executable,
            ["--version"],
            self.root,
            self.root / "npm-sibling-escape-env",
        )
        self.assertEqual(escaped.returncode, 1)
        self.assertIn("executable path is invalid", escaped.stderr.decode("utf-8"))
        (platform_root / "package.json").write_text(json.dumps(manifest), "utf-8")

        binary.write_bytes(original + b"tamper")
        tampered = run_artifact(
            executable, ["--version"], self.root, self.root / "npm-tamper-env"
        )
        self.assertEqual(tampered.returncode, 1)
        self.assertIn("executable integrity mismatch", tampered.stderr.decode("utf-8"))

        binary.unlink()
        target = self.root / "outside-platform-package"
        target.write_bytes(original)
        target.chmod(0o755)
        binary.symlink_to(target)
        linked = run_artifact(
            executable, ["--version"], self.root, self.root / "npm-symlink-env"
        )
        self.assertEqual(linked.returncode, 1)
        self.assertIn(
            "ancestry must not contain symlinks", linked.stderr.decode("utf-8")
        )

    def test_same_version_two_package_repair_restores_platform_launch(self) -> None:
        prefix = self.root / "npm-same-version-repair-prefix"
        executable = self.install_npm(prefix.name, include_platform=True)
        platform_root = npm_platform_root(prefix)
        manifest = json.loads((platform_root / "package.json").read_text("utf-8"))
        binary = platform_root / manifest["openproseBinary"]
        original = binary.read_bytes()
        self.assertGreater(len(original), 1)
        binary.write_bytes(original[:-1])

        refused = run_artifact(
            executable,
            ["--version"],
            self.root,
            self.root / "npm-same-version-repair-refused-env",
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("executable integrity mismatch", refused.stderr.decode("utf-8"))

        npm = shutil.which("npm")
        self.assertIsNotNone(npm)
        platform_package = (
            self.out_a / f"openprose-prose-cli-{PLATFORM_ID}-{VERSION}.tgz"
        )
        meta_package = self.out_a / f"openprose-prose-cli-{VERSION}.tgz"
        repaired = subprocess.run(
            [
                npm,
                "install",
                "--global",
                "--offline",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--prefix",
                str(prefix),
                str(platform_package),
                str(meta_package),
            ],
            cwd=self.root,
            env={
                **clean_environment(self.root / "npm-same-version-repair-install-env"),
                "npm_config_cache": str(
                    self.root / "npm-same-version-repair-install-cache"
                ),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(binary.read_bytes(), original)
        launched = run_artifact(
            executable,
            ["--version"],
            self.root,
            self.root / "npm-same-version-repair-launched-env",
        )
        self.assertEqual(launched.returncode, 0, launched.stderr.decode("utf-8"))
        self.assertIn(f"prose {VERSION}", launched.stdout.decode("utf-8"))

    def test_packed_launcher_rejects_symlinked_platform_package_ancestry(self) -> None:
        if platform.system() == "Windows":
            self.skipTest("POSIX directory symlink assertions")
        for alias_kind in ("platform-root", "binary-parent"):
            with self.subTest(alias=alias_kind):
                prefix = self.root / f"npm-parent-alias-{alias_kind}"
                executable = self.install_npm(prefix.name, include_platform=True)
                platform_root = npm_platform_root(prefix)
                if alias_kind == "platform-root":
                    original = self.root / f"outside-{alias_kind}-package"
                    platform_root.rename(original)
                    platform_root.symlink_to(original, target_is_directory=True)
                else:
                    binary_parent = platform_root / "bin"
                    original = self.root / f"outside-{alias_kind}"
                    binary_parent.rename(original)
                    binary_parent.symlink_to(original, target_is_directory=True)
                refused = run_artifact(
                    executable,
                    ["--version"],
                    self.root,
                    self.root / f"npm-parent-alias-{alias_kind}-env",
                )
                self.assertEqual(refused.returncode, 1)
                self.assertIn(
                    "ancestry must not contain symlinks",
                    refused.stderr.decode("utf-8"),
                )

    def test_packed_launcher_reauthenticates_the_closure_immediately_before_spawn(
        self,
    ) -> None:
        prefix = self.root / "npm-pre-spawn-reauth"
        self.install_npm(prefix.name, include_platform=True)
        launcher = (
            npm_modules_root(prefix) / "@openprose" / "prose-cli" / "bin" / "prose.js"
        )
        manifest_path = npm_platform_root(prefix) / "package.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        binary = npm_platform_root(prefix) / manifest["openproseBinary"]
        node = shutil.which("node")
        self.assertIsNotNone(node)
        probe = r"""
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const filename = process.argv[1];
const manifestPath = path.resolve(process.argv[2]);
const binary = path.resolve(process.argv[3]);
const source = fs.readFileSync(filename, "utf8").replace(/^#![^\n]*\n/, "");
let manifestReads = 0;
let stderr = "";
const spawned = [];
const controlledFs = { ...fs, readFileSync(file, ...args) {
  const value = fs.readFileSync(file, ...args);
  if (path.resolve(String(file)) === manifestPath) {
    manifestReads += 1;
    if (manifestReads === 2) fs.appendFileSync(binary, "changed-before-spawn");
  }
  return value;
}};
const fakeProcess = {
  platform: process.platform, arch: process.arch, versions: process.versions,
  argv: ["node", filename, "--version"], report: process.report,
  stderr: { write(value) { stderr += String(value); } }, exitCode: undefined,
  pid: 7001, on() {}, off() {}, kill() {},
};
function controlledRequire(name) {
  if (name === "node:fs") return controlledFs;
  if (name !== "node:child_process") return require(name);
  return { spawn(...args) { spawned.push(args); throw new Error("spawned"); } };
}
vm.runInNewContext(source, {
  require: controlledRequire, process: fakeProcess, Set,
  __dirname: path.dirname(filename), __filename: filename,
});
process.stdout.write(JSON.stringify({ spawned, stderr, exitCode: fakeProcess.exitCode }));
"""
        completed = subprocess.run(
            [
                node,
                "-e",
                probe,
                str(launcher.resolve()),
                str(manifest_path.resolve()),
                str(binary.resolve()),
            ],
            cwd=self.root,
            env=clean_environment(self.root / "npm-pre-spawn-reauth-env"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertEqual(observed["spawned"], [])
        self.assertEqual(observed["exitCode"], 1)
        self.assertIn("package closure changed before spawn", observed["stderr"])

    def test_packed_launcher_rejects_meta_or_platform_cohort_drift(self) -> None:
        for target_name, mutation in (
            (
                "meta",
                lambda prefix: npm_modules_root(prefix)
                / "@openprose"
                / "prose-cli"
                / "package.json",
            ),
            ("platform", lambda prefix: npm_platform_root(prefix) / "package.json"),
        ):
            with self.subTest(target=target_name):
                prefix = self.root / f"npm-cohort-drift-{target_name}"
                executable = self.install_npm(prefix.name, include_platform=True)
                manifest_path = mutation(prefix)
                manifest = json.loads(manifest_path.read_text("utf-8"))
                manifest["openproseCohort"]["sourceRevision"] = "wrong-source"
                manifest_path.write_text(json.dumps(manifest), "utf-8")
                completed = run_artifact(
                    executable,
                    ["--version"],
                    self.root,
                    self.root / f"npm-cohort-drift-{target_name}-env",
                )
                self.assertEqual(completed.returncode, 1)
                diagnostic = completed.stderr.decode("utf-8")
                self.assertIn("cohort", diagnostic)
                if target_name == "platform":
                    self.assertIn(
                        expected_registry_repair_command(prefix),
                        diagnostic,
                    )

    def test_packed_launcher_rejects_unknown_cohort_fields(self) -> None:
        prefix = self.root / "npm-cohort-extra"
        executable = self.install_npm(prefix.name, include_platform=True)
        manifest_path = npm_platform_root(prefix) / "package.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["openproseCohort"]["extra"] = "mutable"
        manifest_path.write_text(json.dumps(manifest), "utf-8")
        completed = run_artifact(
            executable,
            ["--version"],
            self.root,
            self.root / "npm-cohort-extra-env",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("cohort", completed.stderr.decode("utf-8"))

    def test_launcher_forwards_interrupt_and_preserves_the_child_exit(self) -> None:
        if platform.system() == "Windows":
            self.skipTest("POSIX signal relay assertion")
        prefix = self.root / "npm-signal-prefix"
        executable = self.install_npm(prefix.name, include_platform=True)
        platform_root = npm_platform_root(prefix)
        manifest_path = platform_root / "package.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        probe = platform_root / manifest["openproseBinary"]
        probe.write_text(
            "#!/usr/bin/env node\n"
            "const fs = require('node:fs');\n"
            "const observation = process.env.OPENPROSE_SIGNAL_OBSERVATION;\n"
            "fs.writeFileSync(observation, JSON.stringify({pid: process.pid, state: 'ready'}));\n"
            "process.once('SIGINT', () => {\n"
            "  fs.writeFileSync(observation, JSON.stringify({pid: process.pid, signal: 'SIGINT'}));\n"
            "  process.exit(42);\n"
            "});\n"
            "setInterval(() => {}, 1000);\n",
            "utf-8",
        )
        probe.chmod(0o755)
        manifest["openproseBinaryByteLength"] = probe.stat().st_size
        manifest["openproseBinarySha256"] = sha256(probe)
        manifest_path.write_text(json.dumps(manifest), "utf-8")

        observation = self.root / "launcher-signal-observation.json"
        environment = clean_environment(self.root / "launcher-signal-env")
        environment["OPENPROSE_SIGNAL_OBSERVATION"] = str(observation)
        process = subprocess.Popen(
            [str(executable), "opaque", "arguments"],
            cwd=self.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and not observation.exists()
            and process.poll() is None
        ):
            time.sleep(0.02)
        polled = process.poll()
        if polled is not None:
            stdout, stderr = process.communicate(timeout=1)
            self.fail(
                f"launcher exited before interrupt: {polled}; stdout={stdout!r}; stderr={stderr!r}"
            )
        self.assertEqual(json.loads(observation.read_text("utf-8"))["state"], "ready")
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 42, (stdout, stderr))
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, b"")
        self.assertEqual(json.loads(observation.read_text("utf-8"))["signal"], "SIGINT")

    @unittest.skipIf(
        platform.system() == "Windows", "foreground process-group SIGINT is POSIX-only"
    )
    def test_installed_launcher_settles_real_foreground_group_sigint_once(self) -> None:
        executable = self.install_npm(
            "npm-foreground-sigint-prefix", include_platform=True
        )
        self.assertIsNotNone(self.bun_test_binary)
        platform_root = npm_platform_root(self.root / "npm-foreground-sigint-prefix")
        manifest_path = platform_root / "package.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        binary = platform_root / manifest["openproseBinary"]
        binary.write_bytes(self.bun_test_binary.read_bytes())
        binary.chmod(0o755)
        manifest["openproseBinaryByteLength"] = binary.stat().st_size
        manifest["openproseBinarySha256"] = sha256(binary)
        manifest_path.write_text(json.dumps(manifest), "utf-8")
        identities_path = self.root / "npm-foreground-descendants.json"
        observation_path = self.root / "npm-foreground-observation.json"
        environment = clean_environment(self.root / "npm-foreground-sigint-env")
        environment.update(
            {
                "OPENPROSE_CONFORMANCE_FAKE_HARNESS": str(FAKE_HARNESS),
                "OPENPROSE_CONFORMANCE_FAKE_SCENARIO": "descendant",
                "OPENPROSE_CONFORMANCE_FAKE_OBSERVATION": str(observation_path),
                "OPENPROSE_CONFORMANCE_DESCENDANT_IDENTITIES": str(identities_path),
            }
        )
        process: subprocess.Popen[bytes] | None = None
        identities: dict[str, int] | None = None
        try:
            process = subprocess.Popen(
                [
                    str(executable),
                    "--harness",
                    "mock",
                    "--transport",
                    "fake-process",
                    "--output",
                    "json",
                    "run",
                    "fixture.prose.md",
                ],
                cwd=self.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + 8
            while (
                time.monotonic() < deadline
                and not identities_path.exists()
                and process.poll() is None
            ):
                time.sleep(0.02)
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                self.fail(
                    f"launcher exited before descendant publication: {process.returncode}; "
                    f"stdout={stdout!r}; stderr={stderr!r}"
                )
            self.assertTrue(
                identities_path.is_file(),
                "fake descendant identities were not published",
            )
            identities = json.loads(identities_path.read_text("utf-8"))
            for field in ("childPid", "grandchildPid", "processGroupId"):
                self.assertIsInstance(identities.get(field), int, identities)
                self.assertGreater(identities[field], 0, identities)
            self.assertTrue(process_exists(identities["childPid"]))
            self.assertTrue(process_exists(identities["grandchildPid"]))

            # A terminal sends SIGINT to the entire foreground group. This
            # intentionally exercises the launcher's relay while the real Bun
            # child receives the same signal directly.
            os.killpg(process.pid, signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 24, (stdout, stderr))
            self.assertEqual(stderr, b"")
            lines = stdout.splitlines()
            self.assertEqual(len(lines), 1, stdout)
            terminal = json.loads(lines[0])
            self.assertEqual(terminal["schema"], "openprose.runner-result/1")
            self.assertEqual(terminal["runnerExitCode"], 24)
            self.assertEqual(terminal["error"]["code"], "CANCELLED")
            self.assertEqual(terminal["terminal"]["classification"], "cancelled")
            self.assertTrue(wait_for_process_exit(identities["childPid"], 4))
            self.assertTrue(wait_for_process_exit(identities["grandchildPid"], 4))
        finally:
            if identities is not None:
                fixture_group = identities.get("processGroupId")
                if (
                    isinstance(fixture_group, int)
                    and fixture_group > 0
                    and fixture_group != os.getpgrp()
                ):
                    try:
                        os.killpg(fixture_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=2)

    def test_evidence_scaffolding_is_bound_and_explicitly_non_authoritative(
        self,
    ) -> None:
        manifest = json.loads((self.out_a / "release-manifest.json").read_text("utf-8"))
        provenance = json.loads((self.out_a / "provenance.json").read_text("utf-8"))
        sbom = json.loads((self.out_a / "sbom.cdx.json").read_text("utf-8"))
        dependency_bytes = (self.out_a / "dependency-evidence.json").read_bytes()
        dependency = json.loads(dependency_bytes)
        self.assertEqual(manifest["schema"], "openprose.local-release-manifest/1")
        self.assertEqual(manifest["mode"], "development")
        self.assertFalse(manifest["releaseEligible"])
        self.assertEqual(manifest["source"]["revision"], self.source_revision)
        self.assertEqual(manifest["source"]["verification"], "matched-product-doctor")
        self.assertEqual(
            manifest["buildProfiles"],
            {
                "rust": {"profile": "development", "testSeamsEnabled": False},
                "bun": {"profile": "development", "testSeamsEnabled": False},
            },
        )
        self.assertEqual(
            manifest["bunRuntime"],
            PACKAGE_LOCAL.BUN_RUNTIME_BY_PLATFORM[PLATFORM_ID],
        )
        self.assertTrue(manifest["image"]["releaseEligible"])
        if self.windows_host is None:
            self.assertEqual(manifest["windowsProcessHost"], "not-applicable")
        else:
            self.assertEqual(
                manifest["windowsProcessHost"]["path"], PACKAGE_LOCAL.WINDOWS_HOST_NAME
            )
            self.assertEqual(
                manifest["windowsProcessHost"]["sha256"], sha256(self.windows_host)
            )
            self.assertEqual(
                manifest["windowsProcessHost"]["byteLength"],
                self.windows_host.stat().st_size,
            )
            self.assertFalse(manifest["windowsProcessHost"]["admission"])
        self.assertFalse(manifest["windowsJobObjectReleaseAdmission"])
        self.assertEqual(
            manifest["image"]["manifestSha256"], sha256(ECHO_IMAGE_MANIFEST)
        )
        self.assertEqual(
            manifest["lockfiles"]["cargoSha256"], sha256(CLI / "rust" / "Cargo.lock")
        )
        self.assertEqual(
            manifest["lockfiles"]["bunSha256"], sha256(CLI / "bun" / "bun.lock")
        )
        self.assertTrue(
            all(
                isinstance(value, str) and value
                for value in manifest["toolchains"].values()
            )
        )
        self.assertEqual(manifest["claims"]["signing"], "not-performed")
        self.assertEqual(manifest["claims"]["vulnerabilityReview"], "not-performed")
        self.assertEqual(manifest["claims"]["networkIsolation"], "not-enforced")
        self.assertEqual(
            manifest["dependencyEvidence"],
            {
                "path": "dependency-evidence.json",
                "byteLength": len(dependency_bytes),
                "sha256": hashlib.sha256(dependency_bytes).hexdigest(),
                "releasePolicyPassed": False,
            },
        )
        self.assertEqual(dependency["schema"], "openprose.dependency-evidence/1")
        self.assertFalse(dependency["releasePolicy"]["passed"])
        generated = subprocess.run(
            ["python3", str(DEPENDENCY_SCRIPT), "report", "--root", str(ROOT)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr.decode())
        self.assertEqual(dependency_bytes, generated.stdout)
        self.assertEqual(
            set(provenance), {"_type", "subject", "predicateType", "predicate"}
        )
        self.assertEqual(provenance["_type"], "https://in-toto.io/Statement/v1")
        self.assertEqual(provenance["predicateType"], "https://slsa.dev/provenance/v1")
        self.assertNotIn("_predicateType", provenance)
        parameters = provenance["predicate"]["buildDefinition"]["externalParameters"]
        self.assertEqual(parameters["sourceRevision"], self.source_revision)
        self.assertEqual(parameters["image"], manifest["image"])
        self.assertEqual(parameters["buildProfiles"], manifest["buildProfiles"])
        self.assertEqual(parameters["bunRuntime"], manifest["bunRuntime"])
        self.assertEqual(parameters["linuxRuntime"], manifest["linuxRuntime"])
        if PLATFORM_ID.startswith("linux-"):
            self.assertEqual(manifest["linuxRuntime"]["minimumGlibc"], "2.34")
            self.assertEqual(
                manifest["linuxRuntime"]["executionEvidence"],
                "ubuntu-22.04-only",
            )
        else:
            self.assertEqual(manifest["linuxRuntime"], "not-applicable")
        self.assertEqual(sbom["bomFormat"], "CycloneDX")
        self.assertEqual(sbom["properties"][0]["value"], "component-inventory-attached")
        dependency_components = [
            component
            for component in sbom["components"]
            if component.get("properties")
            and any(
                item == {"name": "openprose:kind", "value": "resolved-dependency"}
                for item in component["properties"]
            )
        ]
        # The Rust kernel HTTPS loader adds 51 locked dependencies.
        self.assertEqual(len(dependency_components), 127 + 14 + 12)
        self.assertEqual(
            len({component["bom-ref"] for component in dependency_components}),
            len(dependency_components),
        )
        resolved = provenance["predicate"]["buildDefinition"]["resolvedDependencies"]
        self.assertIn(
            {
                "uri": "openprose:dependency-evidence",
                "digest": {"sha256": hashlib.sha256(dependency_bytes).hexdigest()},
            },
            resolved,
        )
        subjects = {
            entry["name"]: entry["digest"]["sha256"] for entry in provenance["subject"]
        }
        for artifact in manifest["artifacts"]:
            self.assertEqual(subjects[artifact["path"]], artifact["sha256"])
            self.assertEqual(
                (self.out_a / artifact["path"]).stat().st_size, artifact["byteLength"]
            )

    def test_internal_mock_package_requires_exact_sentinel_and_both_test_seams(
        self,
    ) -> None:
        rust = self.root / "internal-mock-rust"
        bun = self.root / "internal-mock-bun"
        for implementation, path in (("rust", rust), ("bun", bun)):
            self.write_candidate(
                path,
                implementation,
                "development",
                image_manifest=IMAGE_MANIFEST,
                test_seams_enabled=True,
            )
        output = self.root / "internal-mock-package"
        arguments = PACKAGE_LOCAL.parser().parse_args(
            [
                "--mode",
                "development",
                "--version",
                VERSION,
                "--source-revision",
                "development",
                "--source-date-epoch",
                "0",
                "--rust-binary",
                str(rust),
                "--bun-binary",
                str(bun),
                "--image-manifest",
                str(IMAGE_MANIFEST),
                "--out",
                str(output),
            ]
        )
        with mock.patch.object(
            PACKAGE_LOCAL, "current_platform_id", return_value="darwin-arm64"
        ):
            PACKAGE_LOCAL.build(arguments, internal_package_purpose="mock-benchmark")
        manifest = json.loads((output / "release-manifest.json").read_text("utf-8"))
        self.assertEqual(
            manifest["buildProfiles"],
            {
                "rust": {"profile": "development", "testSeamsEnabled": True},
                "bun": {"profile": "development", "testSeamsEnabled": True},
            },
        )
        self.assertEqual(manifest["image"]["purpose"], "sentinel-transport-test")
        self.assertFalse(manifest["image"]["releaseEligible"])
        self.assertFalse(manifest["releaseEligible"])
        self.assertFalse(manifest["publicationAuthorized"])

        no_seam = self.root / "internal-mock-no-seam"
        self.write_candidate(
            no_seam,
            "rust",
            "development",
            image_manifest=IMAGE_MANIFEST,
            test_seams_enabled=False,
        )
        refused = argparse.Namespace(**vars(arguments))
        refused.out = self.root / "internal-mock-refused"
        refused.rust_binary = no_seam
        with (
            mock.patch.object(
                PACKAGE_LOCAL, "current_platform_id", return_value="darwin-arm64"
            ),
            self.assertRaisesRegex(PACKAGE_LOCAL.PackageError, "build profile"),
        ):
            PACKAGE_LOCAL.build(refused, internal_package_purpose="mock-benchmark")
        self.assertFalse(refused.out.exists())

    def test_public_packager_cli_cannot_select_mock_and_ordinary_refuses_sentinel(
        self,
    ) -> None:
        option_strings = {
            option
            for action in PACKAGE_LOCAL.parser()._actions
            for option in action.option_strings
        }
        self.assertNotIn("--package-purpose", option_strings)
        self.assertNotIn("--internal-package-purpose", option_strings)
        refused = self.run_packager(
            "ordinary-sentinel-refused", image_manifest=IMAGE_MANIFEST
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("ordinary development packages require", refused.stderr)
        self.assertFalse((self.root / "ordinary-sentinel-refused").exists())

    def test_mock_package_purpose_never_applies_to_alpha_or_release(self) -> None:
        arguments = PACKAGE_LOCAL.parser().parse_args(
            [
                "--mode",
                "alpha",
                "--version",
                VERSION,
                "--source-revision",
                "fixture",
                "--rust-binary",
                str(self.rust_binary),
                "--bun-binary",
                str(self.bun_binary),
                "--image-manifest",
                str(ECHO_IMAGE_MANIFEST),
                "--out",
                str(self.root / "alpha-internal-mock-refused"),
            ]
        )
        with self.assertRaisesRegex(PACKAGE_LOCAL.PackageError, "development mode"):
            PACKAGE_LOCAL.build(arguments, internal_package_purpose="mock-benchmark")

    def test_release_mode_rejects_sentinel_and_missing_external_gates(self) -> None:
        rejected = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--mode",
                "release",
                "--version",
                VERSION,
                "--source-revision",
                "fixture",
                "--rust-binary",
                str(self.rust_binary),
                "--bun-binary",
                str(self.bun_binary),
                "--image-manifest",
                str(IMAGE_MANIFEST),
                "--out",
                str(self.root / "release-rejected"),
            ],
            cwd=ROOT,
            env=clean_environment(self.root / "release-rejected-env"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("releaseEligible is false", rejected.stderr)
        self.assertFalse((self.root / "release-rejected").exists())

        eligible = self.root / "eligible-manifest.json"
        value = json.loads(IMAGE_MANIFEST.read_text("utf-8"))
        value["releaseEligible"] = True
        value["purpose"] = "canonical-language-runtime"
        eligible.write_text(json.dumps(value), "utf-8")
        missing = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--mode",
                "release",
                "--version",
                VERSION,
                "--source-revision",
                "fixture",
                "--rust-binary",
                str(self.rust_binary),
                "--bun-binary",
                str(self.bun_binary),
                "--image-manifest",
                str(eligible),
                *readelf_args(),
                "--out",
                str(self.root / "release-missing-gates"),
            ],
            cwd=ROOT,
            env=clean_environment(self.root / "release-missing-env"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--canonical-profile", missing.stderr)
        self.assertIn("--release-evidence", missing.stderr)
        self.assertFalse((self.root / "release-missing-gates").exists())

    @unittest.skipIf(
        platform.system() == "Windows", "executable fixture uses a POSIX shebang"
    )
    def test_alpha_mode_packages_release_profile_echo_image_without_external_authority(
        self,
    ) -> None:
        eligible = self.root / "alpha-eligible-manifest.json"
        image = json.loads(ECHO_IMAGE_MANIFEST.read_text("utf-8"))
        eligible.write_text(json.dumps(image), "utf-8")
        binaries: dict[str, Path] = {}
        for implementation in ("rust", "bun"):
            binary = self.root / f"alpha-{implementation}"
            self.write_candidate(
                binary,
                implementation,
                "alpha-fixture",
                image_manifest=eligible,
                build_profile="release",
                test_seams_enabled=False,
            )
            binaries[implementation] = binary
        output = self.root / "functional-alpha"
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--mode",
                "alpha",
                "--version",
                VERSION,
                "--source-revision",
                "alpha-fixture",
                "--rust-binary",
                str(binaries["rust"]),
                "--bun-binary",
                str(binaries["bun"]),
                "--image-manifest",
                str(eligible),
                *readelf_args(),
                "--out",
                str(output),
            ],
            cwd=ROOT,
            env=clean_environment(self.root / "functional-alpha-env"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = json.loads((output / "release-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["mode"], "alpha")
        self.assertEqual(
            manifest["buildProfiles"],
            {
                "rust": {"profile": "release", "testSeamsEnabled": False},
                "bun": {"profile": "release", "testSeamsEnabled": False},
            },
        )
        self.assertTrue(manifest["image"]["releaseEligible"])
        self.assertFalse(manifest["releaseEligible"])
        self.assertFalse(manifest["publicationAuthorized"])
        self.assertEqual(manifest["externalGates"]["canonicalProfile"], "unavailable")
        self.assertEqual(manifest["externalGates"]["releaseEvidence"], "unavailable")
        meta_manifest = json.loads(
            read_npm_file(output / f"openprose-prose-cli-{VERSION}.tgz", "package.json")
        )
        platform_manifest = json.loads(
            read_npm_file(
                output / f"openprose-prose-cli-{PLATFORM_ID}-{VERSION}.tgz",
                "package.json",
            )
        )
        for label, package_manifest, directory in (
            ("meta", meta_manifest, "cli/bun/npm"),
            ("platform", platform_manifest, "cli/bun"),
        ):
            with self.subTest(package=label):
                self.assertEqual(
                    package_manifest["homepage"],
                    "https://github.com/openprose/prose/tree/main/cli#readme",
                )
                self.assertEqual(
                    package_manifest["bugs"],
                    {
                        "url": "https://github.com/openprose/prose/issues/new?template=openprose-cli-bug.yml"
                    },
                )
                self.assertEqual(package_manifest["license"], "MIT")
                self.assertEqual(
                    package_manifest["publishConfig"], {"access": "public"}
                )
                self.assertEqual(package_manifest["repository"]["directory"], directory)
                self.assertFalse(
                    package_manifest["openproseCohort"]["publicationAuthorized"]
                )
        self.assertNotIn(
            "@openprose/prose-cli-win32-x64",
            meta_manifest["optionalDependencies"],
        )
        self.assertNotIn(
            "win32-x64", meta_manifest["openproseCohort"]["admittedPlatforms"]
        )
        required_guidance = (
            "Release channel: functional alpha",
            "does not execute OpenProse programs",
            "SHA256SUMS",
            "examples/hello.prose.md",
            "Functional-alpha harness support on this platform: "
            + display_harnesses(PACKAGE_LOCAL.ALPHA_HARNESS_SUPPORT[PLATFORM_ID]),
            "never opens or controls an interactive TUI",
        )
        for implementation in ("rust", "bun"):
            archive_path = output / (
                f"openprose-prose-cli-{implementation}-{VERSION}-{PLATFORM_ID}.tar.gz"
            )
            with tarfile.open(archive_path, "r:gz") as archive:
                member = next(
                    item
                    for item in archive.getmembers()
                    if item.name.endswith("/README.txt")
                )
                extracted = archive.extractfile(member)
                self.assertIsNotNone(extracted)
                standalone = extracted.read().decode("utf-8")
            for guidance in required_guidance:
                with self.subTest(surface=implementation, guidance=guidance):
                    self.assertIn(guidance, standalone)
            self.assertIn(
                f'"$PWD/openprose-prose-cli-{implementation}-{VERSION}-{PLATFORM_ID}/prose" cli harness list',
                standalone,
            )
            self.assertIn(f"tar -xzf {archive_path.name}", standalone)
            self.assertIn(f"ASSET='{archive_path.name}'", standalone)
            self.assertIn("missing or duplicate checksum entry", standalone)
            self.assertIn(
                "awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | shasum -a 256 -c -",
                standalone,
            )
            self.assertNotIn("shasum -a 256 -c SHA256SUMS", standalone)
            if implementation == "bun" and PLATFORM_ID.startswith("darwin-"):
                self.assertIn("Requires macOS 13 or newer", standalone)
            if implementation == "bun":
                runtime = PACKAGE_LOCAL.BUN_RUNTIME_BY_PLATFORM[PLATFORM_ID]
                self.assertIn(f"Compile target: {runtime['compileTarget']}", standalone)
                self.assertIn(
                    f"Runtime variant: {runtime['runtimeVariant']}", standalone
                )
                self.assertNotIn("requires AVX2", standalone)
                self.assertNotIn("no baseline x64 artifact", standalone)

        npm = read_npm_file(
            output / f"openprose-prose-cli-{VERSION}.tgz", "README.md"
        ).decode("utf-8")
        for guidance in (
            "Release channel: functional alpha",
            "does not execute OpenProse programs",
            "SHA256SUMS",
            "npm install --global --ignore-scripts",
            f'"$HOME/.local/openprose-cli-{VERSION}/bin/prose" cli harness use codex',
            "examples/hello.prose.md",
            "never opens or controls an interactive TUI",
        ):
            with self.subTest(surface="npm", guidance=guidance):
                self.assertIn(guidance, npm)
        self.assertIn("macOS packages require macOS 13 or newer", npm)
        self.assertIn("bun-darwin-x64-baseline", npm)
        self.assertIn("bun-linux-x64-baseline", npm)
        self.assertIn("baseline CPU runtime variants", npm)
        self.assertNotIn("requires AVX2", npm)
        self.assertNotIn("no baseline x64 artifact", npm)
        self.assertIn("consumer compatibility floor is Node.js 22.22.3", npm)
        self.assertIn("release CI and admission use exactly Node.js 24.20.0", npm)

    def test_packager_rejects_a_source_revision_not_reported_by_both_products(
        self,
    ) -> None:
        output = self.root / "source-mismatch"
        completed = self.run_packager(
            output.name,
            source_revision="not-the-compiled-revision",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn(
            "runner commit does not match --source-revision", completed.stderr
        )
        self.assertFalse(output.exists())

    @unittest.skipIf(
        platform.system() == "Windows", "executable fixture uses a POSIX shebang"
    )
    def test_release_mode_refuses_each_development_or_test_seam_candidate(self) -> None:
        eligible = self.root / "profile-eligible-manifest.json"
        image = json.loads(IMAGE_MANIFEST.read_text("utf-8"))
        image["releaseEligible"] = True
        image["purpose"] = "canonical-language-runtime"
        eligible.write_text(json.dumps(image), "utf-8")
        canonical_profile = self.root / "profile-canonical.txt"
        release_evidence = self.root / "profile-evidence.txt"
        canonical_profile.write_text("candidate profile\n", "utf-8")
        release_evidence.write_text("candidate evidence\n", "utf-8")

        release_candidates = {}
        development_candidates = {}
        for implementation in ("rust", "bun"):
            release_candidate = self.root / f"profile-release-{implementation}"
            self.write_candidate(
                release_candidate,
                implementation,
                "profile-fixture",
                image_manifest=eligible,
                build_profile="release",
                test_seams_enabled=False,
            )
            release_candidates[implementation] = release_candidate
            development_candidate = self.root / f"profile-development-{implementation}"
            self.write_candidate(
                development_candidate,
                implementation,
                "profile-fixture",
                image_manifest=eligible,
                native_executable=True,
            )
            development_candidates[implementation] = development_candidate

        for implementation in ("rust", "bun"):
            with self.subTest(implementation=implementation):
                output = self.root / f"profile-refused-{implementation}"
                completed = subprocess.run(
                    [
                        "python3",
                        str(SCRIPT),
                        "--mode",
                        "release",
                        "--version",
                        VERSION,
                        "--source-revision",
                        "profile-fixture",
                        "--rust-binary",
                        str(
                            development_candidates["rust"]
                            if implementation == "rust"
                            else release_candidates["rust"]
                        ),
                        "--bun-binary",
                        str(
                            development_candidates["bun"]
                            if implementation == "bun"
                            else release_candidates["bun"]
                        ),
                        "--image-manifest",
                        str(eligible),
                        "--canonical-profile",
                        str(canonical_profile),
                        "--release-evidence",
                        str(release_evidence),
                        "--out",
                        str(output),
                    ],
                    cwd=ROOT,
                    env=clean_environment(
                        self.root / f"profile-refused-env-{implementation}"
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn(
                    f"{implementation} build profile does not match --mode release",
                    completed.stderr,
                )
                self.assertFalse(output.exists())

    @unittest.skipIf(
        platform.system() == "Windows", "executable fixture uses a POSIX shebang"
    )
    def test_arbitrary_gate_files_only_create_an_unpublishable_release_candidate(
        self,
    ) -> None:
        eligible = self.root / "eligible-candidate-manifest.json"
        image = json.loads(IMAGE_MANIFEST.read_text("utf-8"))
        image["releaseEligible"] = True
        image["purpose"] = "canonical-language-runtime"
        eligible.write_text(json.dumps(image), "utf-8")
        binaries: dict[str, Path] = {}
        for implementation in ("rust", "bun"):
            binary = self.root / f"candidate-{implementation}"
            self.write_candidate(
                binary,
                implementation,
                "fixture",
                image_manifest=eligible,
                build_profile="release",
                test_seams_enabled=False,
            )
            binaries[implementation] = binary
        canonical_profile = self.root / "arbitrary-profile.txt"
        release_evidence = self.root / "arbitrary-evidence.txt"
        canonical_profile.write_text("not authority\n", "utf-8")
        release_evidence.write_text("not validation\n", "utf-8")
        output = self.root / "candidate-only-release"
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--mode",
                "release",
                "--version",
                VERSION,
                "--source-revision",
                "fixture",
                "--rust-binary",
                str(binaries["rust"]),
                "--bun-binary",
                str(binaries["bun"]),
                "--image-manifest",
                str(eligible),
                "--canonical-profile",
                str(canonical_profile),
                "--release-evidence",
                str(release_evidence),
                "--out",
                str(output),
            ],
            cwd=ROOT,
            env=clean_environment(self.root / "candidate-release-env"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = json.loads((output / "release-manifest.json").read_text("utf-8"))
        self.assertTrue(manifest["image"]["releaseEligible"])
        self.assertFalse(manifest["releaseEligible"])
        self.assertFalse(manifest["publicationAuthorized"])
        self.assertEqual(
            manifest["buildProfiles"],
            {
                "rust": {"profile": "release", "testSeamsEnabled": False},
                "bun": {"profile": "release", "testSeamsEnabled": False},
            },
        )
        self.assertEqual(manifest["promotion"]["status"], "not-performed")
        self.assertFalse(manifest["externalGates"]["authorityValidatedByPackager"])


if __name__ == "__main__":
    unittest.main()
