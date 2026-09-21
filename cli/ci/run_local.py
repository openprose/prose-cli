#!/usr/bin/env python3
"""Run the provider-free OpenProse CLI admission gates in one fail-fast pass."""

from __future__ import annotations

import argparse
import codecs
from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable, Mapping, Sequence, TextIO


CLI_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CLI_ROOT.parent

DEFAULT_GATE_TIMEOUT_SECONDS = 600.0
HEAVY_GATE_TIMEOUT_SECONDS = 1_800.0
MAX_GATE_TIMEOUT_SECONDS = 3_600.0
STREAM_HEAD_BYTES = 64 * 1024
STREAM_TAIL_BYTES = 64 * 1024
MAX_CAPTURE_BYTES = STREAM_HEAD_BYTES + STREAM_TAIL_BYTES
TERMINATION_GRACE_SECONDS = 0.5
KILL_GRACE_SECONDS = 3.0
OUTPUT_SETTLEMENT_SECONDS = 2.0
TIMEOUT_EXIT_CODE = 124
UNSETTLED_EXIT_CODE = 125
INTERRUPTED_EXIT_CODE = 130

SECRET_ENVIRONMENT_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GEMINI_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_API_KEY",
        "NODE_AUTH_TOKEN",
        "NPM_TOKEN",
        "OPENAI_API_KEY",
        "OPENPROSE_TOKEN",
        "OPENROUTER_API_KEY",
    }
)
SECRET_SUFFIXES = ("_API_KEY", "_PASSWORD", "_SECRET", "_TOKEN", "_CREDENTIALS")
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


@dataclass(frozen=True)
class Gate:
    name: str
    cwd: Path
    argv: tuple[str, ...]
    provider_free: bool = True
    quick: bool = True
    timeout_seconds: float = DEFAULT_GATE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= MAX_GATE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "gate timeout must be finite and within the supported bound"
            )


def gates() -> tuple[Gate, ...]:
    python = sys.executable
    return (
        Gate(
            "local-runner-tests",
            REPOSITORY_ROOT,
            (python, "cli/ci/test_run_local.py"),
        ),
        Gate(
            "architecture-tests",
            CLI_ROOT / "ci",
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-p",
                "test_check_*.py",
                "-v",
            ),
        ),
        Gate(
            "release-preflight-tests",
            CLI_ROOT / "ci",
            (python, "test_release_preflight.py"),
        ),
        Gate(
            "release-notes",
            REPOSITORY_ROOT,
            (python, "cli/ci/test_render_release_notes.py"),
        ),
        Gate(
            "alpha-promotion",
            REPOSITORY_ROOT,
            (
                python,
                "cli/ci/test_promote_alpha_release.py",
                "-v",
            ),
        ),
        Gate(
            "post-public-verification",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/ci",
                "-p",
                "test_*public_alpha*.py",
                "-v",
            ),
        ),
        Gate(
            "release-reproducibility",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "-v",
                "cli.ci.test_reproducible_release",
            ),
        ),
        Gate(
            "workflow-policy",
            REPOSITORY_ROOT,
            (python, "cli/ci/check_workflows.py"),
        ),
        Gate(
            "dependency-contract",
            REPOSITORY_ROOT,
            (python, "cli/ci/check_dependencies.py"),
        ),
        Gate(
            "dependency-evidence",
            REPOSITORY_ROOT,
            (python, "cli/ci/test_dependency_evidence.py"),
        ),
        Gate(
            "shared-contracts",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/shared/tests",
                "-p",
                "test_*.py",
            ),
        ),
        Gate(
            "image-bundle",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/shared/image/bundle",
                "-p",
                "test_*.py",
            ),
        ),
        Gate(
            "fake-harness",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/conformance/fake-harness",
                "-p",
                "test_*.py",
            ),
        ),
        Gate(
            "fake-hosted-service",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/conformance/fake-hosted-service",
                "-p",
                "test_*.py",
            ),
        ),
        Gate(
            "adapter-oracle",
            REPOSITORY_ROOT,
            (python, "cli/conformance/adversarial/adapters/test_adapter_oracle.py"),
        ),
        Gate(
            "windows-resolution-oracle",
            CLI_ROOT / "conformance" / "cases" / "adapters" / "windows-resolution",
            (python, "-m", "unittest", "-v", "test_windows_resolution.py"),
        ),
        Gate(
            "real-harness-contract",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/conformance/real-harness",
                "-p",
                "test_*.py",
            ),
        ),
        Gate(
            "functional-alpha-live-contract",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/conformance/live-alpha",
                "-p",
                "test_*.py",
                "-v",
            ),
        ),
        Gate(
            "direct-skill-contract",
            REPOSITORY_ROOT,
            (python, "cli/conformance/direct-skill-real/test_run.py"),
        ),
        Gate(
            "architecture-boundary",
            REPOSITORY_ROOT,
            (python, "cli/ci/check_architecture.py"),
        ),
        Gate(
            "windows-host-static",
            CLI_ROOT / "platform" / "windows-process-host",
            (python, "scripts/verify_static.py"),
        ),
        Gate(
            "windows-host-format",
            CLI_ROOT / "platform" / "windows-process-host",
            ("cargo", "fmt", "--all", "--", "--check"),
            quick=False,
            timeout_seconds=HEAVY_GATE_TIMEOUT_SECONDS,
        ),
        Gate(
            "windows-host-clippy",
            CLI_ROOT / "platform" / "windows-process-host",
            (
                "cargo",
                "clippy",
                "--locked",
                "--offline",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ),
            quick=False,
            timeout_seconds=HEAVY_GATE_TIMEOUT_SECONDS,
        ),
        Gate(
            "windows-host-tests",
            CLI_ROOT / "platform" / "windows-process-host",
            ("cargo", "test", "--locked", "--offline", "--all-features"),
            quick=False,
            timeout_seconds=HEAVY_GATE_TIMEOUT_SECONDS,
        ),
        Gate(
            "rust-format", CLI_ROOT / "rust", ("cargo", "fmt", "--all", "--", "--check")
        ),
        Gate(
            "rust-clippy",
            CLI_ROOT / "rust",
            (
                "cargo",
                "clippy",
                "--workspace",
                "--all-targets",
                "--features",
                "prose-cli/test-seams",
                "--locked",
                "--offline",
                "--",
                "-D",
                "warnings",
            ),
        ),
        Gate(
            "rust-tests",
            CLI_ROOT / "rust",
            (
                "cargo",
                "test",
                "--workspace",
                "--all-targets",
                "--features",
                "prose-cli/test-seams",
                "--locked",
                "--offline",
            ),
        ),
        # The lifecycle driver intentionally builds a test-seam Rust candidate and
        # then an ordinary candidate into Cargo's conventional output path. Run it
        # before the ordinary product build below so an already-fresh no-feature
        # fingerprint cannot leave the test-seam executable at target/debug/prose.
        # The quick plan omits this heavy gate and still gets the ordinary build.
        Gate(
            "package-lifecycle",
            CLI_ROOT / "ci",
            (python, "-m", "unittest", "-v", "test_package_lifecycle.py"),
            quick=False,
            timeout_seconds=HEAVY_GATE_TIMEOUT_SECONDS,
        ),
        Gate(
            "rust-build",
            CLI_ROOT / "rust",
            (
                "cargo",
                "build",
                "--locked",
                "--offline",
                "-p",
                "prose-cli",
                "--bin",
                "prose",
                "--target-dir",
                "target/openprose-ordinary",
            ),
        ),
        Gate("bun-typecheck", CLI_ROOT / "bun", ("bun", "run", "typecheck")),
        Gate("bun-tests", CLI_ROOT / "bun", ("bun", "run", "test")),
        Gate("bun-build", CLI_ROOT / "bun", ("bun", "run", "build")),
        Gate(
            "adapter-product-adversary",
            REPOSITORY_ROOT,
            (
                python,
                "cli/conformance/adversarial/adapter-products/test_adapter_products.py",
            ),
        ),
        Gate(
            "differential-conformance",
            REPOSITORY_ROOT,
            (python, "cli/conformance/runner/run.py", "--build", "--phase", "7"),
        ),
        Gate(
            "staging-service-corpus",
            REPOSITORY_ROOT,
            (python, "cli/conformance/runner/staging_service.py", "--validate"),
        ),
        Gate("staging-service-rust-build", CLI_ROOT / "rust",
             ("cargo", "build", "--locked", "--features", "test-seams", "--bin", "prose")),
        Gate(
            "staging-service-rust",
            REPOSITORY_ROOT,
            (python, "cli/conformance/runner/staging_service.py", "--",
             str(CLI_ROOT / "rust" / "target" / "debug" / "prose")),
        ),
        Gate("staging-service-bun-build", CLI_ROOT / "bun", ("bun", "run", "build:test")),
        Gate(
            "staging-service-bun",
            REPOSITORY_ROOT,
            (python, "cli/conformance/runner/staging_service.py", "--",
             str(CLI_ROOT / "bun" / "dist" / "prose-test")),
        ),
        Gate("service-environment-rust", REPOSITORY_ROOT,
             (python, "cli/conformance/runner/service_environment.py", "--",
              str(CLI_ROOT / "rust" / "target" / "debug" / "prose"))),
        Gate("service-environment-bun", REPOSITORY_ROOT,
             (python, "cli/conformance/runner/service_environment.py", "--",
              str(CLI_ROOT / "bun" / "dist" / "prose-test"))),
        Gate("registry-service-rust", REPOSITORY_ROOT,
             (python, "cli/conformance/runner/registry_service.py", "--",
              str(CLI_ROOT / "rust" / "target" / "debug" / "prose"))),
        Gate("registry-service-bun", REPOSITORY_ROOT,
             (python, "cli/conformance/runner/registry_service.py", "--",
              str(CLI_ROOT / "bun" / "dist" / "prose-test"))),
        Gate(
            "conformance-host",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cli/conformance/runner",
                "-p",
                "test_*.py",
            ),
        ),
        Gate(
            "package-local",
            REPOSITORY_ROOT,
            (python, "-m", "unittest", "-v", "cli.ci.test_package_local"),
            quick=False,
            timeout_seconds=HEAVY_GATE_TIMEOUT_SECONDS,
        ),
        Gate(
            "alpha-package-admission",
            REPOSITORY_ROOT,
            (
                python,
                "-m",
                "unittest",
                "-v",
                "cli.ci.test_alpha_package_admission.AlphaPackageAdmissionTests",
            ),
        ),
        Gate(
            "installed-package-benchmark",
            REPOSITORY_ROOT,
            (python, "-m", "unittest", "-v", "cli.benchmarks.installed.test_benchmark"),
        ),
        Gate(
            "release-package-admission",
            CLI_ROOT / "ci",
            (
                python,
                "-m",
                "unittest",
                "-v",
                "test_release_package_admission.py",
                "test_create_draft_release.py",
            ),
        ),
        Gate(
            "release-rehearsal-contract",
            CLI_ROOT / "ci",
            (python, "-m", "unittest", "-v", "test_rehearse_release.py"),
        ),
        Gate(
            "release-rehearsal-real",
            CLI_ROOT / "ci",
            (python, "-m", "unittest", "-v", "test_rehearse_release_real.py"),
            quick=False,
            timeout_seconds=HEAVY_GATE_TIMEOUT_SECONDS,
        ),
        Gate(
            "benchmark-contract",
            CLI_ROOT / "benchmarks",
            (python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"),
            quick=False,
            timeout_seconds=HEAVY_GATE_TIMEOUT_SECONDS,
        ),
    )


def plan(
    all_gates: Sequence[Gate], *, selected: Sequence[str], quick: bool
) -> tuple[Gate, ...]:
    known = {gate.name for gate in all_gates}
    unknown = sorted(set(selected) - known)
    if unknown:
        raise ValueError(f"unknown gate(s): {', '.join(unknown)}")
    selected_set = set(selected)
    return tuple(
        gate
        for gate in all_gates
        if (not selected_set or gate.name in selected_set) and (not quick or gate.quick)
    )


def provider_free_environment(source: Mapping[str, str]) -> dict[str, str]:
    result = {
        name: value
        for name, value in source.items()
        if (normalized := name.upper()) not in SECRET_ENVIRONMENT_NAMES
        and normalized not in BUILD_OVERRIDE_NAMES
        and not normalized.endswith(SECRET_SUFFIXES)
        and not normalized.startswith("PROSE_")
        and not normalized.startswith("OPENPROSE_")
    }
    result.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    return result


Executor = Callable[[Gate, Mapping[str, str]], int]


class GateExecutionFailure(RuntimeError):
    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class GateInterrupted(BaseException):
    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number
        self.exit_code = 128 + signal_number
        super().__init__(signal.Signals(signal_number).name)


@dataclass
class _StreamCapture:
    name: str
    sink: TextIO
    retained: bytearray
    tail: bytearray
    total_bytes: int = 0
    truncated: bool = False


def descendant_authority(platform_name: str) -> bool:
    return platform_name == "posix"


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_until(predicate: Callable[[], bool], *, deadline: float) -> bool:
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))
    return True


def _write_stream_text(sink: TextIO, text: str) -> None:
    if not text:
        return
    try:
        sink.write(text)
        sink.flush()
    except (BrokenPipeError, OSError, ValueError):
        # A closed presentation stream must not stop draining the child pipe.
        return


def _pump_output(pipe: object, capture: _StreamCapture) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        try:
            while True:
                chunk = pipe.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    break
                capture.total_bytes += len(chunk)
                head_remaining = STREAM_HEAD_BYTES - len(capture.retained)
                if head_remaining > 0:
                    retained = chunk[:head_remaining]
                    capture.retained.extend(retained)
                    _write_stream_text(capture.sink, decoder.decode(retained))
                remainder = chunk[head_remaining:]
                if remainder:
                    capture.tail.extend(remainder)
                    if len(capture.tail) > STREAM_TAIL_BYTES:
                        del capture.tail[: len(capture.tail) - STREAM_TAIL_BYTES]
            omitted = capture.total_bytes - len(capture.retained) - len(capture.tail)
            capture.truncated = omitted > 0
            if capture.truncated:
                _write_stream_text(capture.sink, decoder.decode(b"", final=True))
                _write_stream_text(
                    capture.sink,
                    f"\n[local admission: {capture.name} omitted {omitted} "
                    f"middle bytes; {capture.total_bytes} total bytes observed; "
                    "bounded tail follows]\n",
                )
                _write_stream_text(
                    capture.sink, bytes(capture.tail).decode("utf-8", errors="replace")
                )
            else:
                _write_stream_text(
                    capture.sink, decoder.decode(bytes(capture.tail), final=True)
                )
        except (OSError, ValueError):
            # Forced cleanup can close a pipe while its bounded reader is active.
            pass
    finally:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass


def _wait_process(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=max(0.0, timeout))
        return True
    except subprocess.TimeoutExpired:
        return False


def _terminate_owned_boundary(
    process: subprocess.Popen[bytes], *, platform_name: str
) -> tuple[bool, bool]:
    if platform_name == "posix":
        process_group_id = process.pid
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            return process.poll() is not None, False
        _wait_process(process, TERMINATION_GRACE_SECONDS)
        if _process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                return process.poll() is not None, False
        direct_child_reaped = _wait_process(process, KILL_GRACE_SECONDS)
        group_gone = _wait_until(
            lambda: not _process_group_exists(process_group_id),
            deadline=time.monotonic() + KILL_GRACE_SECONDS,
        )
        return direct_child_reaped, group_gone

    if process.poll() is None:
        process.terminate()
        if not _wait_process(process, TERMINATION_GRACE_SECONDS):
            process.kill()
    direct_child_reaped = _wait_process(process, KILL_GRACE_SECONDS)
    # Terminating a direct Windows child is not Job Object descendant authority.
    return direct_child_reaped, False


def forced_exit_failure(
    *,
    gate_name: str,
    reason: str,
    platform_name: str,
    direct_child_reaped: bool,
    process_group_gone: bool,
    output_settled: bool = True,
) -> GateExecutionFailure:
    if not descendant_authority(platform_name):
        return GateExecutionFailure(
            f"{reason}; {platform_name} cannot guarantee descendant cleanup "
            "without an admitted Job Object host",
            exit_code=UNSETTLED_EXIT_CODE,
        )
    if not direct_child_reaped or not process_group_gone or not output_settled:
        return GateExecutionFailure(
            f"{reason}; owned process boundary for {gate_name} did not settle",
            exit_code=UNSETTLED_EXIT_CODE,
        )
    return GateExecutionFailure(reason, exit_code=TIMEOUT_EXIT_CODE)


def _join_output_threads(
    threads: Sequence[threading.Thread], *, deadline: float
) -> bool:
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)
    return all(not thread.is_alive() for thread in threads)


def _settle_output(
    process: subprocess.Popen[bytes],
    threads: Sequence[threading.Thread],
    *,
    deadline: float,
) -> bool:
    settled = _join_output_threads(threads, deadline=deadline)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
    if not settled:
        settled = _join_output_threads(threads, deadline=deadline)
    return settled


def _await_gate_completion(
    gate: Gate,
    process: subprocess.Popen[bytes],
    threads: Sequence[threading.Thread],
    *,
    platform_name: str,
    deadline: float,
) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _wait_process(process, remaining):
        direct_reaped, group_gone = _terminate_owned_boundary(
            process, platform_name=platform_name
        )
        output_settled = _settle_output(
            process,
            threads,
            deadline=time.monotonic() + KILL_GRACE_SECONDS,
        )
        raise forced_exit_failure(
            gate_name=gate.name,
            reason=f"absolute timeout after {gate.timeout_seconds:g}s",
            platform_name=platform_name,
            direct_child_reaped=direct_reaped,
            process_group_gone=group_gone,
            output_settled=output_settled,
        )

    output_deadline = min(deadline, time.monotonic() + OUTPUT_SETTLEMENT_SECONDS)
    output_settled = _settle_output(process, threads, deadline=output_deadline)
    group_gone = (
        not _process_group_exists(process.pid)
        if descendant_authority(platform_name)
        else True
    )
    if output_settled and group_gone and time.monotonic() <= deadline:
        assert process.returncode is not None
        return process.returncode

    deadline_expired = time.monotonic() >= deadline
    direct_reaped, cleaned_group = _terminate_owned_boundary(
        process, platform_name=platform_name
    )
    output_settled = _settle_output(
        process,
        threads,
        deadline=time.monotonic() + KILL_GRACE_SECONDS,
    )
    if deadline_expired:
        raise forced_exit_failure(
            gate_name=gate.name,
            reason=f"absolute timeout after {gate.timeout_seconds:g}s",
            platform_name=platform_name,
            direct_child_reaped=direct_reaped,
            process_group_gone=cleaned_group,
            output_settled=output_settled,
        )
    raise GateExecutionFailure(
        f"gate exited but its owned process/output boundary did not settle; "
        f"cleanup verified={direct_reaped and cleaned_group and output_settled}",
        exit_code=UNSETTLED_EXIT_CODE,
    )


def execute_gate(gate: Gate, environment: Mapping[str, str]) -> int:
    platform_name = os.name
    if not descendant_authority(platform_name):
        raise GateExecutionFailure(
            "native Windows local admission requires an admitted Job Object "
            "host before any gate process can start",
            exit_code=UNSETTLED_EXIT_CODE,
        )
    creation_flags = 0
    if platform_name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    started = time.monotonic()
    deadline = started + gate.timeout_seconds
    try:
        process = subprocess.Popen(
            gate.argv,
            cwd=gate.cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=platform_name == "posix",
            creationflags=creation_flags,
        )
    except OSError as error:
        raise GateExecutionFailure(
            f"could not start gate executable: {error}", exit_code=127
        ) from error

    started_threads: list[threading.Thread] = []
    try:
        assert process.stdout is not None and process.stderr is not None
        captures = (
            _StreamCapture("stdout", sys.stdout, bytearray(), bytearray()),
            _StreamCapture("stderr", sys.stderr, bytearray(), bytearray()),
        )
        threads = tuple(
            threading.Thread(
                target=_pump_output,
                args=(pipe, capture),
                name=f"openprose-local-{gate.name}-{capture.name}",
                daemon=True,
            )
            for pipe, capture in zip((process.stdout, process.stderr), captures)
        )
        for thread in threads:
            thread.start()
            started_threads.append(thread)
        return _await_gate_completion(
            gate,
            process,
            started_threads,
            platform_name=platform_name,
            deadline=deadline,
        )
    except (KeyboardInterrupt, GateInterrupted):
        direct_reaped, group_gone = _terminate_owned_boundary(
            process, platform_name=platform_name
        )
        output_settled = _settle_output(
            process,
            started_threads,
            deadline=time.monotonic() + KILL_GRACE_SECONDS,
        )
        if (
            not descendant_authority(platform_name)
            or not direct_reaped
            or not group_gone
            or not output_settled
        ):
            raise forced_exit_failure(
                gate_name=gate.name,
                reason="interrupt received",
                platform_name=platform_name,
                direct_child_reaped=direct_reaped,
                process_group_gone=group_gone,
                output_settled=output_settled,
            )
        raise
    except GateExecutionFailure:
        raise
    except Exception as error:
        direct_reaped, group_gone = _terminate_owned_boundary(
            process, platform_name=platform_name
        )
        output_settled = _settle_output(
            process,
            started_threads,
            deadline=time.monotonic() + KILL_GRACE_SECONDS,
        )
        raise GateExecutionFailure(
            "gate supervisor failed; "
            f"cleanup verified={direct_reaped and group_gone and output_settled}",
            exit_code=UNSETTLED_EXIT_CODE,
        ) from error


def run(
    selected_gates: Iterable[Gate],
    *,
    environment: Mapping[str, str],
    execute: Executor = execute_gate,
) -> int:
    sanitized = provider_free_environment(environment)
    completed = 0
    previous_sigterm: object | None = None
    if os.name == "posix" and threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def interrupt_on_sigterm(signal_number: int, _frame: object) -> None:
            raise GateInterrupted(signal_number)

        signal.signal(signal.SIGTERM, interrupt_on_sigterm)
    try:
        for gate in selected_gates:
            print(
                f"[{gate.name}] {' '.join(gate.argv)} "
                f"(absolute timeout: {gate.timeout_seconds:g}s)",
                flush=True,
            )
            try:
                exit_code = execute(gate, sanitized)
            except GateExecutionFailure as error:
                print(f"FAILED: {gate.name} ({error})", file=sys.stderr)
                return error.exit_code
            except (KeyboardInterrupt, GateInterrupted) as error:
                exit_code = (
                    error.exit_code
                    if isinstance(error, GateInterrupted)
                    else INTERRUPTED_EXIT_CODE
                )
                print(
                    f"INTERRUPTED: {gate.name} (owned process boundary settled)",
                    file=sys.stderr,
                )
                return exit_code
            if exit_code != 0:
                print(f"FAILED: {gate.name} (exit {exit_code})", file=sys.stderr)
                return exit_code
            completed += 1
        print(
            f"PASS: {completed} provider-free limited local development gates; "
            "detached descendant containment is not enforced",
            flush=True,
        )
        return 0
    except GateInterrupted as error:
        print("INTERRUPTED: no gate process boundary was active", file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print("INTERRUPTED: no gate process boundary was active", file=sys.stderr)
        return INTERRUPTED_EXIT_CODE
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--list", action="store_true", help="list gate names and exit")
    result.add_argument(
        "--quick",
        action="store_true",
        help="omit build-heavy package and benchmark gates",
    )
    result.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="GATE",
        help="run only this gate; repeatable",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    all_gates = gates()
    if arguments.list:
        for gate in all_gates:
            print(gate.name)
        return 0
    try:
        selected = plan(
            all_gates, selected=tuple(arguments.only), quick=arguments.quick
        )
    except ValueError as error:
        print(f"local admission: {error}", file=sys.stderr)
        return 2
    if not selected:
        print("local admission: selection contains no runnable gates", file=sys.stderr)
        return 2
    return run(selected, environment=os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
