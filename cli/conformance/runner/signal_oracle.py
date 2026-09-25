#!/usr/bin/env python3
"""Provider-free direct-artifact SIGINT and descendant-cleanup oracle."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("openprose_conformance_host", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
host = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = host
SPEC.loader.exec_module(host)


@dataclass(frozen=True)
class SignalOracleResult:
    product: str
    failures: tuple[str, ...]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    identities: dict[str, Any] | None


def supported() -> bool:
    return os.name == "posix" and hasattr(signal, "SIGINT") and hasattr(os, "killpg")


def build_owned_sentinel_products(snapshot_root: Path) -> tuple[host.Product, ...]:
    """Build the explicit test profile and capture the exact bytes to execute."""
    host.build_products()
    source_products = (
        host.Product("rust", host.DEFAULT_RUST.resolve(), "rust"),
        host.Product("bun", host.DEFAULT_BUN.resolve(), "bun"),
    )
    identities = {
        product.name: host.capture_candidate_identity(product)
        for product in source_products
    }
    snapshot_root.mkdir(mode=0o700)
    products = tuple(
        host.snapshot_product(
            product,
            identities[product.name],
            snapshot_root / product.name,
        )
        for product in source_products
    )
    snapshot_root.chmod(0o500)
    return products


def _single_json(payload: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = payload.decode("utf-8")
        stripped = text.lstrip()
        value, end = json.JSONDecoder().raw_decode(stripped)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"stdout is not one valid UTF-8 JSON value: {error}"
    if stripped[end:].strip():
        return None, "stdout contains data after its one JSON terminal value"
    if not isinstance(value, dict):
        return None, "stdout JSON terminal value is not an object"
    return value, None


def _wait_for_identities(
    path: Path,
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.01)
                continue
            if isinstance(value, dict):
                return value
        if process.poll() is not None:
            return None
        time.sleep(0.01)
    return None


def _valid_identities(value: dict[str, Any] | None) -> bool:
    if value is None:
        return False
    return all(
        isinstance(value.get(name), int)
        and not isinstance(value.get(name), bool)
        and value[name] > 0
        for name in ("childPid", "grandchildPid", "processGroupId")
    )


def _fixture_gone(identities: dict[str, Any]) -> bool:
    return (
        not host.pid_exists(identities["childPid"])
        and not host.pid_exists(identities["grandchildPid"])
        and not host._process_group_exists(identities["processGroupId"])
    )


def _wait_for_fixture_exit(identities: dict[str, Any], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _fixture_gone(identities):
            return True
        time.sleep(0.01)
    return _fixture_gone(identities)


def _cleanup_exact_fixture_group(identities: dict[str, Any] | None) -> None:
    if not _valid_identities(identities):
        return
    assert identities is not None
    process_group_id = identities["processGroupId"]
    if process_group_id == os.getpgrp():
        raise RuntimeError("refusing to clean the test runner's own process group")
    owned_members = []
    for name in ("childPid", "grandchildPid"):
        pid = identities[name]
        try:
            if os.getpgid(pid) == process_group_id:
                owned_members.append(pid)
        except ProcessLookupError:
            pass
    if not owned_members:
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _wait_for_fixture_exit(identities, 2.0)


def probe(product: host.Product, signal_number: int = signal.SIGINT) -> SignalOracleResult:
    if not supported():
        raise RuntimeError("direct SIGINT oracle is unsupported on this platform")
    failures: list[str] = []
    stdout = b""
    stderr = b""
    exit_code: int | None = None
    identities: dict[str, Any] | None = None
    process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(prefix=f"openprose-sigint-{product.name}-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        environment_root = root / "environment"
        observation_path = root / "fake-observation.json"
        identities_path = root / "descendants.json"
        environment = host.hermetic_environment(
            environment_root,
            {
                "OPENPROSE_CONFORMANCE_FAKE_HARNESS": str(host.FAKE_HARNESS),
                "OPENPROSE_CONFORMANCE_FAKE_SCENARIO": "descendant",
                "OPENPROSE_CONFORMANCE_FAKE_OBSERVATION": str(observation_path),
                "OPENPROSE_CONFORMANCE_DESCENDANT_IDENTITIES": str(identities_path),
            },
        )
        try:
            process = subprocess.Popen(
                product.execution_argv(
                    [
                        "--harness",
                        "mock",
                        "--transport",
                        "fake-process",
                        "--output",
                        "json",
                        "run",
                        "fixture.prose.md",
                    ]
                ),
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            identities = _wait_for_identities(identities_path, process, 8.0)
            if not _valid_identities(identities):
                failures.append("fake descendant identities were not published")
            else:
                assert identities is not None
                if identities["processGroupId"] == os.getpgrp():
                    failures.append("fixture reused the test runner process group")
                if not host.pid_exists(identities["childPid"]):
                    failures.append("published child was not alive before SIGINT")
                if not host.pid_exists(identities["grandchildPid"]):
                    failures.append("published grandchild was not alive before SIGINT")
                for name in ("childPid", "grandchildPid"):
                    try:
                        actual_group = os.getpgid(identities[name])
                    except ProcessLookupError:
                        continue
                    if actual_group != identities["processGroupId"]:
                        failures.append(
                            f"published {name} was not in its declared fixture group"
                        )

            if process.poll() is None:
                os.kill(process.pid, signal_number)
                try:
                    exit_code = process.wait(timeout=8.0)
                except subprocess.TimeoutExpired:
                    failures.append("wrapper did not exit after direct SIGINT")
            else:
                exit_code = process.returncode
                failures.append("wrapper exited before direct SIGINT")

            if process.poll() is not None:
                try:
                    stdout, stderr = process.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    failures.append("wrapper stdout/stderr did not settle after exit")

            if exit_code != 24:
                failures.append(f"exit: expected 24, got {exit_code}")
            terminal, parse_failure = _single_json(stdout)
            if parse_failure is not None:
                failures.append(parse_failure)
            elif terminal is not None:
                if terminal.get("schema") != "openprose.runner-result/1":
                    failures.append("terminal schema is not openprose.runner-result/1")
                if terminal.get("runnerExitCode") != 24:
                    failures.append("terminal runnerExitCode is not 24")
                if terminal.get("error", {}).get("code") != "CANCELLED":
                    failures.append("terminal error.code is not CANCELLED")
                if terminal.get("terminal", {}).get("classification") != "cancelled":
                    failures.append("terminal classification is not cancelled")
            if stderr:
                failures.append(f"stderr is not empty: {stderr!r}")
            if _valid_identities(identities):
                assert identities is not None
                if not _wait_for_fixture_exit(identities, 4.0):
                    failures.append(
                        "fake child or grandchild remained after the SIGINT observation window"
                    )
        finally:
            _cleanup_exact_fixture_group(identities)
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    failures.append("direct wrapper cleanup did not settle")
            if process is not None:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
    return SignalOracleResult(
        product.name,
        tuple(failures),
        exit_code,
        stdout,
        stderr,
        identities,
    )


def main() -> int:
    if not supported():
        print("SKIP: direct SIGINT oracle requires native POSIX signals")
        return 0
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="openprose-signal-candidates-") as raw:
        try:
            products = build_owned_sentinel_products(Path(raw) / "snapshots")
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(
                f"FAIL: could not build owned sentinel candidates: {error}",
                file=sys.stderr,
            )
            return 1
        # SIGHUP (a closed terminal) cancels exactly like SIGINT in both products.
        for product in products:
            for signal_number in (signal.SIGINT, signal.SIGHUP):
                result = probe(product, signal_number)
                failures.extend(
                    f"{product.name} {signal.Signals(signal_number).name}: {failure}"
                    for failure in result.failures
                )
    if failures:
        print(f"FAIL: {len(failures)} direct SIGINT oracle failure(s)", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("PASS: Rust and Bun direct SIGINT and SIGHUP cancellation and descendants settled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
