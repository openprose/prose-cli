#!/usr/bin/env python3
"""Build, smoke-test, and optionally package local OpenProse CLI candidates."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Mapping, Sequence

import package_local


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli"
IS_WINDOWS = os.name == "nt"
RUST_BINARY = (
    CLI / "rust" / "target" / "debug" / ("prose.exe" if os.name == "nt" else "prose")
)
BUN_BINARY = CLI / "bun" / "dist" / ("prose.exe" if os.name == "nt" else "prose")
WINDOWS_HOST_BINARY = (
    CLI
    / "platform"
    / "windows-process-host"
    / "target"
    / "debug"
    / "openprose-windows-process-host.exe"
)
WINDOWS_HOST_NAME = "openprose-windows-process-host.exe"
IMAGE_MANIFEST = CLI / "shared" / "image" / "sentinel-v1" / "manifest.json"
ECHO_IMAGE_MANIFEST = CLI / "shared" / "image" / "echo-v0" / "manifest.json"
ECHO_IMAGE_BUNDLE = CLI / "shared" / "image" / "embedded" / "current.bundle.bin"
ECHO_IMAGE_CHECKSUM = CLI / "shared" / "image" / "embedded" / "current.bundle.sha256"
IMAGE_BUNDLE_TOOL = CLI / "shared" / "image" / "bundle" / "image_bundle.py"
ORDINARY_PACKAGE_PURPOSE = "ordinary-development"
MOCK_PACKAGE_PURPOSE = "mock-benchmark"
PACKAGE_PURPOSES = frozenset({ORDINARY_PACKAGE_PURPOSE, MOCK_PACKAGE_PURPOSE})
MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 10 * 60.0
COMMAND_CLEANUP_SECONDS = 3.0
SECRET_SUFFIXES = ("_API_KEY", "_PASSWORD", "_SECRET", "_TOKEN", "_CREDENTIALS")
SECRET_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "NODE_AUTH_TOKEN",
    }
)
BUILD_OVERRIDE_NAMES = frozenset(
    {
        "BUN_INSTALL",
        "BUN_RUNTIME_TRANSPILER_CACHE_PATH",
        "CARGO_BUILD_TARGET",
        "CARGO_TARGET_DIR",
        "NODE_OPTIONS",
        "PYTHONHOME",
        "PYTHONPATH",
        "RUSTC",
        "RUSTC_BOOTSTRAP",
        "RUSTC_WRAPPER",
        "RUSTDOCFLAGS",
        "RUSTFLAGS",
    }
)


class LocalBuildError(RuntimeError):
    """A local build boundary failed with a safe actionable message."""


def rust_binary_for_target(target_dir: Path) -> Path:
    """Return the exact debug executable produced in one isolated Cargo target."""

    return target_dir / "debug" / ("prose.exe" if os.name == "nt" else "prose")


@dataclass(frozen=True)
class Candidate:
    name: str
    build_source_path: Path
    snapshot_path: Path
    byte_length: int
    sha256: str
    source_identity: tuple[int, int]


@dataclass(frozen=True)
class SnapshotCapture:
    byte_length: int
    sha256: str
    source_identity: tuple[int, int]


Executor = Callable[
    [Sequence[str], Path, Mapping[str, str]], subprocess.CompletedProcess[bytes]
]


class BoundedCapture:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.data = bytearray()
        self.overflow = False
        self.error: str | None = None

    def drain(self, stream: object) -> None:
        try:
            while True:
                block = stream.read(65536)  # type: ignore[attr-defined]
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
                stream.close()  # type: ignore[attr-defined]
            except OSError:
                pass


def process_group_exists(group: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_owned_process(process: subprocess.Popen[bytes], group: int) -> bool:
    if os.name == "nt":
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=COMMAND_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            return False
        return process.poll() is not None
    if group != process.pid or group == os.getpgrp():
        raise LocalBuildError("refusing unsafe build-process cleanup target")
    for sent_signal in (signal.SIGTERM, signal.SIGKILL):
        if not process_group_exists(group):
            break
        try:
            os.killpg(group, sent_signal)
        except ProcessLookupError:
            break
        except PermissionError:
            process.poll()
            if not process_group_exists(group):
                break
            return False
        deadline = time.monotonic() + COMMAND_CLEANUP_SECONDS
        while time.monotonic() < deadline:
            process.poll()
            if not process_group_exists(group):
                break
            time.sleep(0.01)
    try:
        process.wait(timeout=COMMAND_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return process.poll() is not None and not process_group_exists(group)


def clean_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Remove provider credentials and ambiguous runner/build identity overrides."""
    result = {
        name: value
        for name, value in source.items()
        if not (normalized := name.upper()).startswith("PROSE_")
        and not normalized.startswith("OPENPROSE_")
        and normalized not in SECRET_NAMES
        and normalized not in BUILD_OVERRIDE_NAMES
        and not normalized.endswith(SECRET_SUFFIXES)
    }
    result.update(
        {
            "OPENPROSE_BUILD_COMMIT": "development",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return result


def execute(
    argv: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> subprocess.CompletedProcess[bytes]:
    return execute_bounded(
        argv,
        cwd,
        environment,
        timeout_seconds=COMMAND_TIMEOUT_SECONDS,
    )


def execute_bounded(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    if (
        not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= COMMAND_TIMEOUT_SECONDS
    ):
        raise LocalBuildError(
            "local build command timeout is outside the supported bound"
        )
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
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
        raise LocalBuildError(f"cannot start local build command: {error}") from error
    group = process.pid
    stdout = BoundedCapture(MAX_COMMAND_OUTPUT_BYTES)
    stderr = BoundedCapture(MAX_COMMAND_OUTPUT_BYTES)
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        try:
            process.wait(timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
        for reader in readers:
            reader.join(timeout=COMMAND_CLEANUP_SECONDS)
        unsettled = any(reader.is_alive() for reader in readers)
        descendants_alive = os.name != "nt" and process_group_exists(group)
        if timed_out or unsettled or descendants_alive:
            cleaned = terminate_owned_process(process, group)
            for reader in readers:
                reader.join(timeout=COMMAND_CLEANUP_SECONDS)
            if not cleaned or any(reader.is_alive() for reader in readers):
                raise LocalBuildError(
                    "local build command cleanup could not be verified"
                )
            if os.name == "nt":
                raise LocalBuildError(
                    "local build command timed out or remained unsettled; "
                    "Windows descendant cleanup authority is unavailable"
                )
            raise LocalBuildError("local build command timed out or remained unsettled")
        if (
            process.poll() is None
            or stdout.error is not None
            or stderr.error is not None
        ):
            raise LocalBuildError(
                "local build command process or output readers did not settle"
            )
        if stdout.overflow or stderr.overflow:
            raise LocalBuildError(
                f"local build command output exceeded {MAX_COMMAND_OUTPUT_BYTES} bytes"
            )
        return subprocess.CompletedProcess(
            list(argv), process.returncode, bytes(stdout.data), bytes(stderr.data)
        )
    finally:
        if process.poll() is None or (os.name != "nt" and process_group_exists(group)):
            try:
                terminate_owned_process(process, group)
            except LocalBuildError:
                pass


def run_checked(
    label: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    executor: Executor,
) -> subprocess.CompletedProcess[bytes]:
    completed = executor(argv, cwd, environment)
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout)[:MAX_DIAGNOSTIC_BYTES]
        rendered = diagnostic.decode("utf-8", errors="replace").strip()
        raise LocalBuildError(
            f"{label} failed (exit {completed.returncode}): {rendered}"
        )
    return completed


def digest_file(path: Path) -> dict[str, object]:
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
        ):
            raise OSError("not a non-symlink non-empty regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
    except OSError as error:
        raise LocalBuildError(
            f"built candidate is unavailable: {path}: {error}"
        ) from error
    digest = hashlib.sha256()
    byte_length = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise LocalBuildError(f"built candidate changed before read: {path}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            byte_length += len(block)
        settled = os.fstat(descriptor)
        if byte_length != opened.st_size or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            settled.st_dev,
            settled.st_ino,
            settled.st_size,
            settled.st_mtime_ns,
            settled.st_ctime_ns,
        ):
            raise LocalBuildError(f"built candidate changed while read: {path}")
    finally:
        os.close(descriptor)
    return {
        "path": str(path.resolve()),
        "byteLength": byte_length,
        "sha256": digest.hexdigest(),
    }


def snapshot_file(source: Path, destination: Path, label: str) -> SnapshotCapture:
    """Capture one immutable build input through a verified open descriptor."""
    try:
        before = source.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
        ):
            raise OSError("not a non-symlink non-empty regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        digest = hashlib.sha256()
        byte_length = 0
        with os.fdopen(descriptor, "rb", closefd=True) as opened, destination.open(
            "xb"
        ) as owned:
            opened_metadata = os.fstat(opened.fileno())
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
                opened_metadata.st_size,
                opened_metadata.st_mtime_ns,
                opened_metadata.st_ctime_ns,
            ):
                raise OSError("changed before open")
            for block in iter(lambda: opened.read(1024 * 1024), b""):
                owned.write(block)
                digest.update(block)
                byte_length += len(block)
            settled = os.fstat(opened.fileno())
        if byte_length != opened_metadata.st_size or (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
            opened_metadata.st_size,
            opened_metadata.st_mtime_ns,
            opened_metadata.st_ctime_ns,
        ) != (
            settled.st_dev,
            settled.st_ino,
            settled.st_size,
            settled.st_mtime_ns,
            settled.st_ctime_ns,
        ):
            raise OSError("changed while snapshotted")
    except OSError as error:
        raise LocalBuildError(f"cannot snapshot {label}: {source}: {error}") from error
    destination.chmod(0o500)
    return SnapshotCapture(
        byte_length=byte_length,
        sha256=digest.hexdigest(),
        source_identity=(opened_metadata.st_dev, opened_metadata.st_ino),
    )


def copy_owned_file(
    source: Path, destination: Path, expected_sha256: str, label: str
) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination):
        raise LocalBuildError(f"{label} destination already exists: {destination}")
    captured = snapshot_file(source, destination, label)
    if captured.sha256 != expected_sha256:
        raise LocalBuildError(f"{label} digest changed while copied")
    destination.chmod(0o755)
    record = digest_file(destination)
    if record["sha256"] != expected_sha256:
        raise LocalBuildError(f"{label} digest changed while copied")
    return record


def owned_path_identity(owned_root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(owned_root)
    except ValueError as error:
        raise LocalBuildError(
            f"owned snapshot escaped its staging root: {path}"
        ) from error
    if not relative.parts or ".." in relative.parts:
        raise LocalBuildError(f"owned snapshot has an invalid identity: {path}")
    return "$OPENPROSE_LOCAL_BUILD/" + relative.as_posix()


def capture_candidate(
    name: str,
    source: Path,
    owned_root: Path,
    *,
    snapshot_group: str = "candidates",
) -> Candidate:
    if snapshot_group not in {"candidates", "package-candidates"}:
        raise LocalBuildError("candidate snapshot group is unsupported")
    snapshot_directory = owned_root / snapshot_group / name
    snapshot_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    snapshot_directory_metadata = snapshot_directory.lstat()
    if stat.S_ISLNK(snapshot_directory_metadata.st_mode) or not stat.S_ISDIR(
        snapshot_directory_metadata.st_mode
    ):
        raise LocalBuildError(
            f"candidate snapshot directory is unsafe: {snapshot_directory}"
        )
    snapshot_directory.chmod(0o700)
    snapshot = snapshot_directory / source.name
    try:
        normalized_source = source.resolve(strict=True)
    except OSError as error:
        raise LocalBuildError(
            f"cannot resolve {name} build source: {source}: {error}"
        ) from error
    captured = snapshot_file(source, snapshot, name)
    return Candidate(
        name=name,
        build_source_path=normalized_source,
        snapshot_path=snapshot,
        byte_length=captured.byte_length,
        sha256=captured.sha256,
        source_identity=captured.source_identity,
    )


def require_distinct_candidates(candidates: Sequence[Candidate]) -> None:
    source_identities: dict[tuple[int, int], str] = {}
    source_paths: dict[str, str] = {}
    snapshot_paths: set[Path] = set()
    for candidate in candidates:
        source_path = os.path.normcase(os.path.abspath(candidate.build_source_path))
        previous_path = source_paths.get(source_path)
        if previous_path is not None:
            raise LocalBuildError(
                f"{previous_path} and {candidate.name} resolved to the same build source path"
            )
        source_paths[source_path] = candidate.name
        previous = source_identities.get(candidate.source_identity)
        if previous is not None:
            raise LocalBuildError(
                f"{previous} and {candidate.name} resolved to the same build source identity"
            )
        source_identities[candidate.source_identity] = candidate.name
        resolved_snapshot = candidate.snapshot_path.resolve()
        if resolved_snapshot in snapshot_paths:
            raise LocalBuildError(
                "candidate snapshots have a mixed destination identity"
            )
        snapshot_paths.add(resolved_snapshot)


def verify_candidate_snapshot(candidate: Candidate) -> None:
    verify_owned_file(
        candidate.snapshot_path,
        candidate.byte_length,
        candidate.sha256,
        f"{candidate.name} snapshot",
    )


def verify_owned_file(path: Path, byte_length: int, sha256: str, label: str) -> None:
    observed = digest_file(path)
    if observed["byteLength"] != byte_length or observed["sha256"] != sha256:
        raise LocalBuildError(f"owned {label} mutated")


def candidate_record(candidate: Candidate, owned_root: Path) -> dict[str, object]:
    snapshot_identity = owned_path_identity(owned_root, candidate.snapshot_path)
    try:
        source_relative = candidate.build_source_path.resolve().relative_to(
            ROOT.resolve()
        )
        source_identity = f"$REPOSITORY/{source_relative.as_posix()}"
    except ValueError:
        source_identity = f"$EXTERNAL_BUILD_SOURCE/{candidate.name}/{candidate.build_source_path.name}"
    return {
        "path": snapshot_identity,
        "buildSourcePath": source_identity,
        "snapshotPath": snapshot_identity,
        "snapshotOwnership": "ephemeral-owned-root",
        "byteLength": candidate.byte_length,
        "sha256": candidate.sha256,
    }


def package_checksum_record(package: Path) -> dict[str, object]:
    try:
        metadata = package.lstat()
    except OSError as error:
        raise LocalBuildError(
            f"package output is unavailable: {package}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LocalBuildError(
            f"package output is not a non-symlink directory: {package}"
        )
    record = digest_file(package / "SHA256SUMS")
    return {
        "path": "SHA256SUMS",
        "byteLength": record["byteLength"],
        "sha256": record["sha256"],
    }


def _build(
    *,
    selection: str,
    smoke: bool,
    package: Path | None,
    install_dir: Path | None,
    ambient: Mapping[str, str],
    owned_root: Path,
    executor: Executor = execute,
    package_purpose: str = ORDINARY_PACKAGE_PURPOSE,
    internal_packager: Callable[[Sequence[str]], None] | None = None,
    agents_sdk_build: Path | None = None,
) -> dict[str, object]:
    if package_purpose not in PACKAGE_PURPOSES:
        raise LocalBuildError("package purpose is unsupported")
    if package is None and package_purpose != ORDINARY_PACKAGE_PURPOSE:
        raise LocalBuildError("mock-benchmark purpose requires --package")
    if package is not None and selection != "both":
        raise LocalBuildError("--package requires --candidate both")
    if package is not None and os.path.lexists(package):
        raise LocalBuildError("--package output must not already exist")
    if install_dir is not None and os.path.lexists(install_dir):
        raise LocalBuildError("--install-dir must not already exist")
    environment = clean_environment(ambient)
    sdk_members = []
    if agents_sdk_build is not None:
        sdk_snapshots = owned_root / "sdk-inputs"
        sdk_snapshots.mkdir(mode=0o700)
        sdk_readelf = None
        if sys.platform.startswith("linux"):
            discovered = shutil.which("readelf", path=environment.get("PATH"))
            if discovered is None:
                raise LocalBuildError("Local SDK installation requires readelf")
            sdk_readelf = Path(discovered).resolve(strict=True)
        try:
            sdk_members, _sdk_record = package_local.snapshot_sdk(
                agents_sdk_build, sdk_snapshots, package_local.current_platform_id(), "development", sdk_readelf
            )
        except package_local.PackageError as error:
            raise LocalBuildError(f"Local SDK input verification failed: {error}") from error
    windows_host_record: dict[str, object] | None = None
    owned_windows_host: Path | None = None
    if IS_WINDOWS:
        run_checked(
            "Windows process host build",
            (
                "cargo",
                "build",
                "--manifest-path",
                "cli/platform/windows-process-host/Cargo.toml",
                "--locked",
                "--offline",
                "--bin",
                "openprose-windows-process-host",
            ),
            cwd=ROOT,
            environment=environment,
            executor=executor,
        )
        owned_windows_host = owned_root / WINDOWS_HOST_NAME
        windows_host_capture = snapshot_file(
            WINDOWS_HOST_BINARY, owned_windows_host, "Windows process host"
        )
        windows_host_record = {
            "buildSourcePath": "$REPOSITORY/cli/platform/windows-process-host/target/release/"
            + WINDOWS_HOST_NAME,
            "snapshotPath": owned_path_identity(owned_root, owned_windows_host),
            "snapshotOwnership": "ephemeral-owned-root",
            "byteLength": windows_host_capture.byte_length,
            "sha256": windows_host_capture.sha256,
            "admission": False,
        }
        environment = {
            **environment,
            "OPENPROSE_WINDOWS_HOST_SHA256": str(windows_host_record["sha256"]),
            "OPENPROSE_WINDOWS_HOST_ADMISSION": "0",
        }
    ordinary_environment = dict(environment)
    sentinel_bundle = owned_root / "sentinel.bundle.bin"
    sentinel_checksum = owned_root / "sentinel.bundle.sha256"
    run_checked(
        "Sentinel image bundle",
        (
            sys.executable,
            str(IMAGE_BUNDLE_TOOL),
            "build",
            str(IMAGE_MANIFEST.parent),
            str(sentinel_bundle),
            "--checksum",
            str(sentinel_checksum),
        ),
        cwd=ROOT,
        environment=environment,
        executor=executor,
    )
    environment = {
        **environment,
        "OPENPROSE_IMAGE_SOURCE_DIR": str(IMAGE_MANIFEST.parent),
        "OPENPROSE_IMAGE_BUNDLE": str(sentinel_bundle),
        "OPENPROSE_IMAGE_BUNDLE_CHECKSUM": str(sentinel_checksum),
    }
    candidates: list[Candidate] = []
    windows_siblings: dict[str, Path] = {}
    if selection in {"rust", "both"}:
        rust_sentinel_target = owned_root / "rust-sentinel-target"
        run_checked(
            "Rust build",
            (
                "cargo",
                "build",
                "--manifest-path",
                "cli/rust/Cargo.toml",
                "--locked",
                "--offline",
                "-p",
                "prose-cli",
                "--bin",
                "prose",
                "--features",
                "prose-cli/test-seams",
                "--target-dir",
                str(rust_sentinel_target),
            ),
            cwd=ROOT,
            environment=environment,
            executor=executor,
        )
        candidates.append(
            capture_candidate(
                "rust", rust_binary_for_target(rust_sentinel_target), owned_root
            )
        )
    if selection in {"bun", "both"}:
        run_checked(
            "Bun build",
            (
                "bun",
                "--no-env-file",
                f"--config={CLI / 'bun' / 'config' / 'empty-bunfig.toml'}",
                "run",
                str(CLI / "bun" / "scripts" / "image-bundle.ts"),
                "build",
                "--test-seams",
                "--image-dir",
                str(IMAGE_MANIFEST.parent),
                "--bundle",
                str(sentinel_bundle),
                "--checksum",
                str(sentinel_checksum),
            ),
            cwd=ROOT,
            environment=environment,
            executor=executor,
        )
        candidates.append(capture_candidate("bun", BUN_BINARY, owned_root))
    require_distinct_candidates(candidates)
    records = {
        candidate.name: candidate_record(candidate, owned_root)
        for candidate in candidates
    }
    if owned_windows_host is not None:
        assert windows_host_record is not None
        verify_owned_file(
            owned_windows_host,
            int(windows_host_record["byteLength"]),
            str(windows_host_record["sha256"]),
            "Windows process host snapshot",
        )
        sibling_records: dict[str, dict[str, object]] = {}
        for candidate in candidates:
            sibling = candidate.snapshot_path.parent / WINDOWS_HOST_NAME
            sibling_record = copy_owned_file(
                owned_windows_host,
                sibling,
                str(windows_host_record["sha256"]),
                f"{candidate.name} Windows process host",
            )
            sibling_record["path"] = owned_path_identity(owned_root, sibling)
            sibling_record["ownership"] = "ephemeral-owned-root"
            sibling_records[candidate.name] = sibling_record
            windows_siblings[candidate.name] = sibling
        windows_host_record["candidateSiblings"] = sibling_records
    for candidate in candidates:
        verify_candidate_snapshot(candidate)
        if owned_windows_host is not None:
            assert windows_host_record is not None
            verify_owned_file(
                windows_siblings[candidate.name],
                int(windows_host_record["byteLength"]),
                str(windows_host_record["sha256"]),
                f"{candidate.name} Windows process host sibling",
            )
        if smoke:
            completed = run_checked(
                f"{candidate.name} smoke test",
                (
                    str(candidate.snapshot_path),
                    "--harness",
                    "mock",
                    "--output",
                    "json",
                    "run",
                    "local-build-smoke",
                ),
                cwd=ROOT,
                environment=environment,
                executor=executor,
            )
            verify_candidate_snapshot(candidate)
            if owned_windows_host is not None:
                assert windows_host_record is not None
                verify_owned_file(
                    windows_siblings[candidate.name],
                    int(windows_host_record["byteLength"]),
                    str(windows_host_record["sha256"]),
                    f"{candidate.name} Windows process host sibling",
                )
            try:
                result = json.loads(completed.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise LocalBuildError(
                    f"{candidate.name} smoke output is not JSON"
                ) from error
            if result.get("semantic", {}).get("status") != "not-applicable":
                raise LocalBuildError(
                    f"{candidate.name} smoke did not retain sentinel semantics"
                )
            records[candidate.name]["smoke"] = "pass"
    package_record: dict[str, object] | None = None
    if package is not None:
        for candidate in candidates:
            verify_candidate_snapshot(candidate)
        if owned_windows_host is not None:
            assert windows_host_record is not None
            verify_owned_file(
                owned_windows_host,
                int(windows_host_record["byteLength"]),
                str(windows_host_record["sha256"]),
                "Windows process host snapshot",
            )
        if package_purpose == ORDINARY_PACKAGE_PURPOSE:
            package_environment = {
                **ordinary_environment,
                "OPENPROSE_IMAGE_SOURCE_DIR": str(ECHO_IMAGE_MANIFEST.parent),
                "OPENPROSE_IMAGE_BUNDLE": str(ECHO_IMAGE_BUNDLE),
                "OPENPROSE_IMAGE_BUNDLE_CHECKSUM": str(ECHO_IMAGE_CHECKSUM),
            }
            rust_package_target = owned_root / "rust-package-target"
            run_checked(
                "Rust package-input build",
                (
                    "cargo",
                    "build",
                    "--manifest-path",
                    "cli/rust/Cargo.toml",
                    "--locked",
                    "--offline",
                    "-p",
                    "prose-cli",
                    "--bin",
                    "prose",
                    "--target-dir",
                    str(rust_package_target),
                ),
                cwd=ROOT,
                environment=package_environment,
                executor=executor,
            )
            package_candidates = [
                capture_candidate(
                    "rust",
                    rust_binary_for_target(rust_package_target),
                    owned_root,
                    snapshot_group="package-candidates",
                )
            ]
            run_checked(
                "Bun package-input build",
                (
                    "bun",
                    "--no-env-file",
                    f"--config={CLI / 'bun' / 'config' / 'empty-bunfig.toml'}",
                    "run",
                    str(CLI / "bun" / "scripts" / "image-bundle.ts"),
                    "build",
                    "--image-dir",
                    str(ECHO_IMAGE_MANIFEST.parent),
                    "--bundle",
                    str(ECHO_IMAGE_BUNDLE),
                    "--checksum",
                    str(ECHO_IMAGE_CHECKSUM),
                ),
                cwd=ROOT,
                environment=package_environment,
                executor=executor,
            )
            package_candidates.append(
                capture_candidate(
                    "bun",
                    BUN_BINARY,
                    owned_root,
                    snapshot_group="package-candidates",
                )
            )
            package_image_manifest = ECHO_IMAGE_MANIFEST
            package_image_name = "echo-v0"
            package_test_seams = False
        else:
            package_environment = environment
            package_candidates = list(candidates)
            package_image_manifest = IMAGE_MANIFEST
            package_image_name = "sentinel-v1"
            package_test_seams = True
        require_distinct_candidates(package_candidates)
        package_windows_siblings: dict[str, Path] = {}
        if owned_windows_host is not None:
            assert windows_host_record is not None
            if package_purpose == MOCK_PACKAGE_PURPOSE:
                package_windows_siblings = dict(windows_siblings)
            else:
                for candidate in package_candidates:
                    sibling = candidate.snapshot_path.parent / WINDOWS_HOST_NAME
                    copy_owned_file(
                        owned_windows_host,
                        sibling,
                        str(windows_host_record["sha256"]),
                        f"{candidate.name} package-input Windows process host",
                    )
                    package_windows_siblings[candidate.name] = sibling
        for candidate in package_candidates:
            verify_candidate_snapshot(candidate)
            if owned_windows_host is not None:
                assert windows_host_record is not None
                verify_owned_file(
                    package_windows_siblings[candidate.name],
                    int(windows_host_record["byteLength"]),
                    str(windows_host_record["sha256"]),
                    f"{candidate.name} package-input Windows process host sibling",
                )
        by_name = {candidate.name: candidate for candidate in package_candidates}
        bun_package = json.loads((CLI / "bun" / "package.json").read_text("utf-8"))
        version = bun_package["version"]
        packaging_argv = [
            sys.executable,
            str(CLI / "ci" / "package_local.py"),
            "--mode",
            "development",
            "--version",
            version,
            "--source-revision",
            "development",
            "--source-date-epoch",
            "0",
            "--rust-binary",
            str(by_name["rust"].snapshot_path),
            "--bun-binary",
            str(by_name["bun"].snapshot_path),
            "--image-manifest",
            str(package_image_manifest),
            "--out",
            str(package),
        ]
        if IS_WINDOWS:
            assert owned_windows_host is not None
            packaging_argv.extend(("--windows-process-host", str(owned_windows_host)))
        elif sys.platform.startswith("linux"):
            discovered_readelf = shutil.which("readelf", path=environment.get("PATH"))
            if discovered_readelf is None:
                raise LocalBuildError("Local Linux packaging requires readelf")
            exact_readelf = Path(discovered_readelf).resolve(strict=True)
            packaging_argv.extend(("--readelf", str(exact_readelf)))
        if agents_sdk_build is not None:
            packaging_argv.extend(("--agents-sdk-build", str(agents_sdk_build)))
        if package_purpose == ORDINARY_PACKAGE_PURPOSE:
            run_checked(
                "Local packaging",
                packaging_argv,
                cwd=ROOT,
                environment=package_environment,
                executor=executor,
            )
        else:
            try:
                if internal_packager is None:
                    package_local.build(
                        package_local.parser().parse_args(packaging_argv[2:]),
                        internal_package_purpose=MOCK_PACKAGE_PURPOSE,
                    )
                else:
                    internal_packager(packaging_argv)
            except (package_local.PackageError, OSError) as error:
                raise LocalBuildError(f"Local packaging failed: {error}") from error
        for candidate in candidates:
            verify_candidate_snapshot(candidate)
        for candidate in package_candidates:
            verify_candidate_snapshot(candidate)
            if owned_windows_host is not None:
                assert windows_host_record is not None
                verify_owned_file(
                    package_windows_siblings[candidate.name],
                    int(windows_host_record["byteLength"]),
                    str(windows_host_record["sha256"]),
                    f"{candidate.name} package-input Windows process host sibling",
                )
        if owned_windows_host is not None:
            assert windows_host_record is not None
            verify_owned_file(
                owned_windows_host,
                int(windows_host_record["byteLength"]),
                str(windows_host_record["sha256"]),
                "Windows process host snapshot",
            )
        package_record = {
            "path": str(package.resolve()),
            "mode": "development",
            "purpose": package_purpose,
            "inputBuild": {
                "image": package_image_name,
                "profile": "development",
                "testSeamsEnabled": package_test_seams,
            },
            "inputs": {
                candidate.name: candidate_record(candidate, owned_root)
                for candidate in package_candidates
            },
            "sha256Sums": package_checksum_record(package),
        }
    install_record: dict[str, object] | None = None
    if install_dir is not None:
        install_dir.mkdir(parents=True)
        for name, data, mode in sdk_members:
            destination = install_dir / name
            destination.write_bytes(data)
            destination.chmod(mode)
        install_record = {}
        for candidate in candidates:
            verify_candidate_snapshot(candidate)
            suffix = ".exe" if candidate.snapshot_path.suffix == ".exe" else ""
            destination = install_dir / f"prose-{candidate.name}{suffix}"
            install_record[candidate.name] = copy_owned_file(
                candidate.snapshot_path,
                destination,
                candidate.sha256,
                f"installed {candidate.name}",
            )
            verify_candidate_snapshot(candidate)
        if IS_WINDOWS:
            assert windows_host_record is not None and owned_windows_host is not None
            verify_owned_file(
                owned_windows_host,
                int(windows_host_record["byteLength"]),
                str(windows_host_record["sha256"]),
                "Windows process host snapshot",
            )
            host_destination = install_dir / WINDOWS_HOST_NAME
            install_record["windowsProcessHost"] = copy_owned_file(
                owned_windows_host,
                host_destination,
                str(windows_host_record["sha256"]),
                "installed Windows process host",
            )
    return {
        "schema": "openprose.local-build-report/1",
        "profile": "development",
        "testSeamsEnabled": True,
        "globalStateModified": False,
        "detachedDescendantContainment": "not-enforced",
        "candidates": records,
        "windowsProcessHost": windows_host_record,
        "package": package_record,
        "install": install_record,
    }


def build(
    *,
    selection: str,
    smoke: bool,
    package: Path | None,
    install_dir: Path | None,
    ambient: Mapping[str, str],
    executor: Executor = execute,
    package_purpose: str = ORDINARY_PACKAGE_PURPOSE,
    internal_packager: Callable[[Sequence[str]], None] | None = None,
    agents_sdk_build: Path | None = None,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="openprose-local-build-") as directory:
        return _build(
            selection=selection,
            smoke=smoke,
            package=package,
            install_dir=install_dir,
            ambient=ambient,
            owned_root=Path(directory),
            executor=executor,
            package_purpose=package_purpose,
            internal_packager=internal_packager,
            agents_sdk_build=agents_sdk_build,
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--candidate", choices=("rust", "bun", "both"), default="both")
    result.add_argument("--smoke", action="store_true")
    result.add_argument("--package", type=Path)
    result.add_argument("--install-dir", type=Path)
    result.add_argument("--agents-sdk-build", type=Path)
    result.add_argument("--json", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = build(
            selection=args.candidate,
            smoke=args.smoke,
            package=args.package,
            install_dir=args.install_dir,
            ambient=os.environ,
            agents_sdk_build=args.agents_sdk_build,
        )
    except (LocalBuildError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f"local-build: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for name, record in report["candidates"].items():
            print(f"{name}: {record['path']}  sha256:{record['sha256']}")
        windows_host = report["windowsProcessHost"]
        if windows_host is not None:
            print(
                "windows process host: "
                f"sha256:{windows_host['sha256']}  admission:{str(windows_host['admission']).lower()}"
            )
            for name, sibling in windows_host["candidateSiblings"].items():
                print(f"{name} Windows sibling: {sibling['path']}")
        if report["package"] is not None:
            print(f"package: {report['package']['path']}")
        if report["install"] is not None:
            for name, record in report["install"].items():
                path = record["path"] if isinstance(record, dict) else record
                print(f"installed {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
