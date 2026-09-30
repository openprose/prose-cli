#!/usr/bin/env python3
"""Opt-in, research-only real-harness route canaries.

This lane deliberately cannot award wrapper, semantic, Prose Complete, or
release admission. It records bounded transport observations while canonical
language artifacts are unavailable.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "policy.v1.json"
DEFAULT_MATRIX = HERE / "matrix.v1.json"
SCHEMA = "openprose.real-harness-evidence/1"
SYSTEM_INSTRUCTION = "Emit only the exact answer requested by the user."
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|key|token)-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|authorization)" r"\s*[:=]\s*[^\s,;\"']+"
    ),
)


class ConfigurationError(RuntimeError):
    """A frozen input or invocation violated a safety invariant."""


@dataclass(frozen=True)
class ProcessObservation:
    exit_code: int | None
    timed_out: bool
    output_limit_exceeded: bool
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_bytes_observed: int
    stderr_bytes_observed: int
    stopped_after_first_assistant_failure: bool = False
    leader_reaped: bool = True
    streams_closed: bool = True
    original_process_group_empty: bool | None = None
    detached_descendants_contained: bool = False


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}: expected a JSON object")
    return value


def build_prompt(expected: str) -> str:
    if not re.fullmatch(r"[A-Z0-9_]{8,64}", expected):
        raise ConfigurationError("expectedOutput must be a short ASCII canary token")
    return (
        "Return exactly the ASCII token between <answer> tags, without the tags, "
        "quotes, Markdown, explanation, or surrounding whitespace.\n"
        f"<answer>{expected}</answer>"
    )


def task_digest(expected: str) -> str:
    task = {
        "generator": "exact-ascii-token/1",
        "system": SYSTEM_INSTRUCTION,
        "prompt": build_prompt(expected),
    }
    return sha256_bytes(canonical_json(task))


def validate_frozen_inputs(policy: dict[str, Any], matrix: dict[str, Any]) -> None:
    if policy.get("schema") != "openprose.real-harness-policy/1":
        raise ConfigurationError("unsupported policy schema")
    if matrix.get("schema") != "openprose.real-harness-matrix/1":
        raise ConfigurationError("unsupported matrix schema")
    limits = policy.get("limits", {})
    if limits.get("maximumTrialsPerRoute") != 1 or limits.get("retryCount") != 0:
        raise ConfigurationError(
            "this evidence lane requires one trial and zero retries"
        )
    if not 0 < limits.get("maximumTotalCostUsd", 0) <= 1.0:
        raise ConfigurationError("maximumTotalCostUsd must be in (0, 1]")
    timeout = limits.get("timeoutSecondsPerTrial")
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 3600
    ):
        raise ConfigurationError("timeoutSecondsPerTrial must be in [1, 3600]")
    stream_limit = limits.get("maximumPersistedStreamBytes")
    if (
        not isinstance(stream_limit, int)
        or isinstance(stream_limit, bool)
        or not 64 <= stream_limit <= 1048576
    ):
        raise ConfigurationError("maximumPersistedStreamBytes must be in [64, 1048576]")
    policy_claims = policy.get("claims", {})
    if policy_claims.get("exploratoryRouteCanary") != "performed":
        raise ConfigurationError("policy must identify the exploratory canary lane")
    if policy_claims.get("baseTransportObservation") is not False:
        raise ConfigurationError("policy cannot claim adapter transport")
    isolation = policy.get("isolation", {})
    required_false = (
        "tools",
        "builtinTools",
        "skills",
        "extensions",
        "contextFiles",
        "promptTemplates",
        "themes",
        "savedSession",
    )
    if not isolation.get("freshWorkingDirectory"):
        raise ConfigurationError("freshWorkingDirectory must remain true")
    if any(isolation.get(name) is not False for name in required_false):
        raise ConfigurationError(
            "all ambient tool/resource/session switches must be false"
        )
    claims = matrix.get("claims", {})
    expected_claims = {
        "strictWrapperAdmission": False,
        "semanticConformance": "unknown",
        "proseComplete": "unknown",
        "releaseEligible": False,
    }
    if claims != expected_claims:
        raise ConfigurationError("matrix must remain non-admitting")
    harness = matrix.get("harness", {})
    if harness.get("stableRecipeCompatible") is not False:
        raise ConfigurationError(
            "research matrix must not claim stable recipe compatibility"
        )
    routes = matrix.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ConfigurationError("matrix routes must be a non-empty list")
    ids: set[str] = set()
    projected = 0.0
    for route in routes:
        if not isinstance(route, dict):
            raise ConfigurationError("each route must be an object")
        route_id = route.get("id")
        if not isinstance(route_id, str) or route_id in ids:
            raise ConfigurationError("route ids must be unique strings")
        ids.add(route_id)
        for key in ("provider", "model", "expectedOutput"):
            if not isinstance(route.get(key), str) or not route[key]:
                raise ConfigurationError(f"{route_id}: missing {key}")
        build_prompt(route["expectedOutput"])
        ceiling = route.get("plannedCostCeilingUsd")
        if not isinstance(ceiling, (int, float)) or ceiling <= 0:
            raise ConfigurationError(f"{route_id}: invalid planned cost ceiling")
        projected += float(ceiling)
        names = route.get("credentialEnvironment", [])
        if not isinstance(names, list) or any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(name)) for name in names
        ):
            raise ConfigurationError(f"{route_id}: invalid credentialEnvironment")
    if projected > float(limits["maximumTotalCostUsd"]) + 1e-9:
        raise ConfigurationError("route cost ceilings exceed the frozen total budget")


def parse_dotenv_selected(path: Path | None, names: Iterable[str]) -> dict[str, str]:
    requested = set(names)
    if not requested:
        return {}
    if path is None:
        raise ConfigurationError(
            "selected routes require an explicit --env-file credential source"
        )
    if not path.is_absolute():
        raise ConfigurationError("--env-file must be an absolute path")
    if not path.is_file():
        raise ConfigurationError(f"credential file does not exist: {path}")
    found: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        name, raw = stripped.split("=", 1)
        name = name.strip()
        if name not in requested:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ConfigurationError(f"{name}: unsupported credential encoding")
        found[name] = value
    missing = requested - found.keys()
    if missing:
        raise ConfigurationError(
            "missing requested credential variable(s): " + ", ".join(sorted(missing))
        )
    return found


def isolated_environment(
    credentials: dict[str, str], extra: dict[str, str] | None = None
) -> dict[str, str]:
    keep = (
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    environment = {name: os.environ[name] for name in keep if name in os.environ}
    environment.update(
        {
            "NO_COLOR": "1",
            "CI": "1",
            "TERM": "dumb",
        }
    )
    environment.update(credentials)
    if extra:
        environment.update(extra)
    return environment


def run_bounded(
    argv: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    *,
    maximum_stream_bytes: int,
    stop_after_first_assistant_failure: bool = False,
) -> ProcessObservation:
    """Capture both child streams without allowing either buffer past its cap.

    On POSIX the child starts a new session, so cleanup can authoritatively target
    the original process group. A descendant that deliberately calls setsid(2)
    leaves that group and is outside this lane's containment authority.
    """

    if (
        not isinstance(maximum_stream_bytes, int)
        or isinstance(maximum_stream_bytes, bool)
        or maximum_stream_bytes < 1
    ):
        raise ConfigurationError("maximum stream bytes must be a positive integer")
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name != "nt"),
    )
    if os.name != "nt" and process.pid == os.getpgrp():  # pragma: no cover
        process.kill()
        process.wait()
        raise ConfigurationError("refusing to supervise the runner's process group")

    lock = threading.Lock()
    closing = threading.Event()
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    observed = {"stdout": 0, "stderr": 0}
    state = {
        "output_limit": False,
        "policy_stopped": False,
        "reader_failed": False,
        "termination_requested": False,
    }

    def request_termination() -> None:
        with lock:
            if state["termination_requested"]:
                return
            state["termination_requested"] = True
        terminate_original_process_group(process)

    def consume(name: str, stream: Any, inspect: bool) -> None:
        pending = bytearray()
        try:
            descriptor = stream.fileno()
            while True:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                should_terminate = False
                with lock:
                    observed[name] += len(chunk)
                    remaining = max(0, maximum_stream_bytes - len(captured[name]))
                    kept = chunk[:remaining]
                    captured[name].extend(kept)
                    if len(chunk) > remaining and not state["output_limit"]:
                        state["output_limit"] = True
                        should_terminate = True
                if should_terminate:
                    request_termination()
                if inspect and stop_after_first_assistant_failure and kept:
                    pending.extend(kept)
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        if assistant_failure_line(bytes(line)):
                            with lock:
                                if not state["policy_stopped"]:
                                    state["policy_stopped"] = True
                                    should_terminate = True
                            if should_terminate:
                                request_termination()
                            return
                    if len(pending) > maximum_stream_bytes:
                        pending.clear()
        except (OSError, ValueError):
            if not closing.is_set():
                with lock:
                    state["reader_failed"] = True
                request_termination()

    assert process.stdout is not None and process.stderr is not None
    streams = (process.stdout, process.stderr)
    threads = (
        threading.Thread(
            target=consume,
            args=("stdout", process.stdout, True),
            name="real-harness-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=consume,
            args=("stderr", process.stderr, False),
            name="real-harness-stderr",
            daemon=True,
        ),
    )
    started_threads: list[threading.Thread] = []
    timed_out = False
    settlement: tuple[bool, bool, bool | None] | None = None
    try:
        for thread in threads:
            thread.start()
            started_threads.append(thread)
        try:
            wait_for_process(process, float(timeout_seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
            request_termination()
        settlement = settle_original_process_group(
            process,
            tuple(started_threads),
            streams,
            closing,
            terminate=not state["termination_requested"],
        )
        if state["reader_failed"]:
            raise ConfigurationError("failed to capture harness output safely")
    except BaseException:
        if settlement is None:
            try:
                request_termination()
                settle_original_process_group(
                    process,
                    tuple(started_threads),
                    streams,
                    closing,
                    terminate=False,
                )
            except Exception as cleanup_error:
                raise ConfigurationError(
                    "failed to settle harness process after interrupted observation"
                ) from cleanup_error
        raise

    leader_reaped, streams_closed, original_group_empty = settlement
    duration_ms = int((time.monotonic() - started) * 1000)
    return ProcessObservation(
        exit_code=None if timed_out else process.returncode,
        timed_out=timed_out,
        output_limit_exceeded=state["output_limit"],
        duration_ms=duration_ms,
        stdout=bytes(captured["stdout"]),
        stderr=bytes(captured["stderr"]),
        stdout_bytes_observed=observed["stdout"],
        stderr_bytes_observed=observed["stderr"],
        stopped_after_first_assistant_failure=state["policy_stopped"],
        leader_reaped=leader_reaped,
        streams_closed=streams_closed,
        original_process_group_empty=original_group_empty,
        detached_descendants_contained=False,
    )


def wait_for_process(process: subprocess.Popen[bytes], timeout: float) -> int:
    """Small test seam around the one blocking leader wait."""

    return process.wait(timeout=timeout)


def assistant_failure_line(line: bytes) -> bool:
    try:
        event = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(event, dict) or event.get("type") != "message_end":
        return False
    message = event.get("message")
    return bool(
        isinstance(message, dict)
        and message.get("role") == "assistant"
        and (message.get("stopReason") == "error" or message.get("errorMessage"))
    )


def terminate_original_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate only the group/session created for this exact child."""

    if os.name == "nt":  # pragma: no cover - this research lane currently runs on macOS
        if process.poll() is None:
            process.kill()
        return
    if process.pid == os.getpgrp():  # pragma: no cover - defensive invariant
        raise ConfigurationError("refusing to terminate the runner's process group")
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _original_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:  # pragma: no cover - unexpected local ownership
        raise ConfigurationError(
            "cannot verify original process-group cleanup"
        ) from error
    return True


def settle_original_process_group(
    process: subprocess.Popen[bytes],
    threads: tuple[threading.Thread, ...],
    streams: tuple[Any, Any],
    closing: threading.Event,
    *,
    terminate: bool,
) -> tuple[bool, bool, bool | None]:
    """Reap the leader, close both pipes, and settle the original POSIX group."""

    if terminate:
        terminate_original_process_group(process)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired as error:  # pragma: no cover - SIGKILL invariant
        raise ConfigurationError("harness process leader did not terminate") from error
    leader_reaped = process.poll() is not None

    deadline = time.monotonic() + 3
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    closing.set()
    for stream in streams:
        if not stream.closed:
            stream.close()
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    streams_closed = all(stream.closed for stream in streams) and all(
        not thread.is_alive() for thread in threads
    )
    if not streams_closed:
        raise ConfigurationError("harness output readers did not settle")

    if os.name == "nt":  # pragma: no cover
        return leader_reaped, streams_closed, None
    while _original_process_group_exists(process.pid):
        if time.monotonic() >= deadline:
            raise ConfigurationError("original harness process group did not settle")
        time.sleep(0.01)
    return leader_reaped, streams_closed, True


def executable_digest(executable: Path) -> str:
    resolved = executable.resolve(strict=True)
    if not resolved.is_file():
        raise ConfigurationError(f"executable is not a file: {resolved}")
    return sha256_bytes(resolved.read_bytes())


def observe_version(
    executable: Path, environment: dict[str, str], maximum_stream_bytes: int
) -> str:
    with tempfile.TemporaryDirectory(prefix="openprose-prime-version-") as raw:
        observation = run_bounded(
            [str(executable), "--version"],
            Path(raw),
            environment,
            10,
            maximum_stream_bytes=maximum_stream_bytes,
        )
    if (
        observation.timed_out
        or observation.output_limit_exceeded
        or observation.exit_code != 0
    ):
        raise ConfigurationError("harness version probe failed")
    value = observation.stdout.decode("utf-8", errors="replace").strip()
    if not value:
        value = observation.stderr.decode("utf-8", errors="replace").strip()
    return value.removeprefix("prime-agent ").strip()


def build_command(
    executable: Path, route: dict[str, Any], prompt: str, cwd: Path
) -> list[str]:
    return [
        str(executable),
        "--print",
        "--mode",
        "json",
        "--cwd",
        str(cwd),
        "--provider",
        route["provider"],
        "--model",
        route["model"],
        "--thinking",
        "off",
        "--no-session",
        "--no-tools",
        "--no-builtin-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--system-prompt",
        SYSTEM_INSTRUCTION,
        "--",
        prompt,
    ]


def parse_json_events(stdout: bytes) -> list[Any]:
    text = stdout.decode("utf-8", errors="replace")
    events: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    if not events and text.strip():
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            events.extend(value if isinstance(value, list) else [value])
    return events


def _text_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_text_parts(item))
        return parts
    if isinstance(value, dict):
        parts = []
        for key in ("text", "content", "output_text", "result"):
            if key in value:
                parts.extend(_text_parts(value[key]))
        return parts
    return []


def assistant_texts(events: Iterable[Any]) -> list[str]:
    answers: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        role = str(value.get("role", "")).lower()
        event_type = str(value.get("type", "")).lower()
        if role == "assistant":
            answers.extend(_text_parts(value.get("content")))
        elif event_type in {"assistant", "assistant_message", "assistant-message"}:
            answers.extend(_text_parts(value))
        for key, item in value.items():
            if key not in {"content", "text", "output_text", "result"}:
                visit(item)

    for event in events:
        visit(event)
    return [answer.strip() for answer in answers if answer.strip()]


def observed_usage(events: list[Any]) -> dict[str, Any] | None:
    attempts = terminal_assistant_attempts(events)
    usages = [
        attempt["usage"]
        for attempt in attempts
        if isinstance(attempt.get("usage"), dict)
    ]
    if not usages:
        return None
    names = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    totals = {
        name: sum(
            float(usage.get(name, 0))
            for usage in usages
            if isinstance(usage.get(name, 0), (int, float))
        )
        for name in names
    }
    return {
        "source": "harness-reported-terminal-message-usage",
        "authoritativeForBilling": False,
        "attempts": len(usages),
        "totals": totals,
    }


def observed_cost(events: list[Any]) -> dict[str, Any] | None:
    attempts = terminal_assistant_attempts(events)
    totals: list[float] = []
    for attempt in attempts:
        usage = attempt.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        total = cost.get("total") if isinstance(cost, dict) else None
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            totals.append(float(total))
    if not totals:
        return None
    return {
        "currency": "USD",
        "source": "harness-reported-terminal-message-usage",
        "authoritativeForBilling": False,
        "attempts": len(totals),
        "totalUsd": sum(totals),
    }


def terminal_assistant_attempts(events: list[Any]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        attempts.append(message)
    return attempts


def bounded_sanitize(
    data: bytes,
    *,
    prompt: str,
    secrets: Iterable[str],
    maximum_bytes: int,
    workspace: Path,
    private_paths: Iterable[Path | None] = (),
) -> str:
    text = data.decode("utf-8", errors="replace")
    prompt_variants = (
        prompt,
        json.dumps(prompt, ensure_ascii=False)[1:-1],
        SYSTEM_INSTRUCTION,
        json.dumps(SYSTEM_INSTRUCTION, ensure_ascii=False)[1:-1],
    )
    for variant in prompt_variants:
        text = text.replace(variant, "[REDACTED_PROMPT]")
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED_SECRET]")
    text = text.replace(str(workspace), "[DISPOSABLE_CWD]")
    path_values = sorted(
        {str(path) for path in private_paths if path is not None},
        key=len,
        reverse=True,
    )
    for path_value in path_values:
        for variant in (path_value, json.dumps(path_value)[1:-1]):
            text = text.replace(variant, "[PRIVATE_PATH]")
    home = os.environ.get("HOME")
    if home:
        text = text.replace(home, "$HOME")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    marker = b"\n...[TRUNCATED]...\n"
    if maximum_bytes <= len(marker):
        return marker[:maximum_bytes].decode("utf-8", errors="ignore")
    side = max(0, (maximum_bytes - len(marker)) // 2)
    bounded = encoded[:side] + marker + encoded[-side:]
    return bounded.decode("utf-8", errors="ignore")


def classify(
    observation: ProcessObservation,
    exact: bool,
    event_count: int,
    attempts: list[dict[str, Any]] | None = None,
) -> str:
    if observation.timed_out:
        return "timeout"
    if observation.output_limit_exceeded:
        return "output-limit"
    if not exact and any(
        attempt.get("stopReason") == "error" or attempt.get("errorMessage")
        for attempt in (attempts or [])
    ):
        return "route-unavailable"
    if observation.exit_code == 0 and event_count and exact:
        return "route-canary-pass"
    if observation.exit_code != 0:
        return "route-unavailable"
    return "route-canary-fail"


def run_route(
    *,
    executable: Path,
    executable_sha256: str,
    observed_version: str,
    policy: dict[str, Any],
    matrix: dict[str, Any],
    route: dict[str, Any],
    env_file: Path | None,
    credential_values: dict[str, str] | None = None,
    extra_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    limits = policy["limits"]
    prompt = build_prompt(route["expectedOutput"])
    credential_names = set(route["credentialEnvironment"])
    if credential_values is None:
        credentials = parse_dotenv_selected(env_file, credential_names)
    else:
        missing = credential_names - credential_values.keys()
        if missing:
            raise ConfigurationError(
                "missing requested credential variable(s): "
                + ", ".join(sorted(missing))
            )
        credentials = {name: credential_values[name] for name in credential_names}
    environment = isolated_environment(credentials, extra_environment)
    with tempfile.TemporaryDirectory(prefix=f"openprose-{route['id']}-") as raw:
        workspace = Path(raw).resolve()
        observation = run_bounded(
            build_command(executable, route, prompt, workspace),
            workspace,
            environment,
            int(limits["timeoutSecondsPerTrial"]),
            maximum_stream_bytes=int(limits["maximumPersistedStreamBytes"]),
            stop_after_first_assistant_failure=True,
        )
        events = parse_json_events(observation.stdout)
        answers = assistant_texts(events)
        attempts = terminal_assistant_attempts(events)
        exact_answer = next(
            (
                answer
                for answer in reversed(answers)
                if answer == route["expectedOutput"]
            ),
            None,
        )
        outcome = classify(observation, exact_answer is not None, len(events), attempts)
        stdout_text = bounded_sanitize(
            observation.stdout,
            prompt=prompt,
            secrets=credentials.values(),
            maximum_bytes=int(limits["maximumPersistedStreamBytes"]),
            workspace=workspace,
            private_paths=(executable, env_file),
        )
        stderr_text = bounded_sanitize(
            observation.stderr,
            prompt=prompt,
            secrets=credentials.values(),
            maximum_bytes=int(limits["maximumPersistedStreamBytes"]),
            workspace=workspace,
            private_paths=(executable, env_file),
        )
    return {
        "schema": SCHEMA,
        "matrixId": matrix["id"],
        "matrixSha256": sha256_bytes(canonical_json(matrix)),
        "policyId": policy["id"],
        "policySha256": sha256_bytes(canonical_json(policy)),
        "route": {
            "id": route["id"],
            "provider": route["provider"],
            "model": route["model"],
            "trial": 1,
        },
        "harness": {
            "name": matrix["harness"]["name"],
            "executableSha256": executable_sha256,
            "observedVersion": observed_version,
            "stableRecipeVersion": matrix["harness"]["stableRecipeVersion"],
            "stableRecipeCompatible": False,
        },
        "taskSha256": task_digest(route["expectedOutput"]),
        "process": {
            "exitCode": observation.exit_code,
            "timedOut": observation.timed_out,
            "stoppedAfterFirstAssistantFailure": (
                observation.stopped_after_first_assistant_failure
            ),
            "durationMs": observation.duration_ms,
            "stdoutBytes": len(observation.stdout),
            "stderrBytes": len(observation.stderr),
            "stdoutSha256": sha256_bytes(observation.stdout),
            "stderrSha256": sha256_bytes(observation.stderr),
            "sanitizedStdout": stdout_text,
            "sanitizedStderr": stderr_text,
            "capture": {
                "limitBytesPerStream": int(limits["maximumPersistedStreamBytes"]),
                "enforcedLive": True,
                "outputLimitExceeded": observation.output_limit_exceeded,
                "stdoutBytesObserved": observation.stdout_bytes_observed,
                "stderrBytesObserved": observation.stderr_bytes_observed,
            },
            "settlement": {
                "scope": (
                    "original-posix-process-group"
                    if os.name != "nt"
                    else "direct-process-only"
                ),
                "leaderReaped": observation.leader_reaped,
                "streamsClosed": observation.streams_closed,
                "originalProcessGroupEmpty": (observation.original_process_group_empty),
                "detachedDescendantsContained": (
                    observation.detached_descendants_contained
                ),
                "strictContainmentClaimed": False,
            },
        },
        "observation": {
            "classification": outcome,
            "jsonEventsParsed": len(events),
            "assistantExactMatch": exact_answer is not None,
            "assistantText": exact_answer,
            "usage": observed_usage(events),
            "cost": observed_cost(events),
            "assistantTerminalAttempts": len(attempts),
            "harnessInternalRetryObserved": len(attempts) > 1,
            "frozenRetryPolicySatisfied": len(attempts) <= 1,
        },
        "claims": {
            "exploratoryRouteCanary": True,
            "baseTransportObservation": False,
            "strictWrapperAdmission": False,
            "semanticConformance": "unknown",
            "proseComplete": "unknown",
            "releaseEligible": False,
        },
    }


def validate_evidence(value: dict[str, Any]) -> None:
    if value.get("schema") != SCHEMA:
        raise ConfigurationError("unsupported evidence schema")
    for key in (
        "matrixId",
        "matrixSha256",
        "policyId",
        "policySha256",
        "route",
        "harness",
        "taskSha256",
        "process",
        "observation",
        "claims",
    ):
        if key not in value:
            raise ConfigurationError(f"evidence missing {key}")
    for name in ("matrixSha256", "policySha256", "taskSha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", value[name]):
            raise ConfigurationError(f"invalid {name}")
    claims = value["claims"]
    if claims.get("strictWrapperAdmission") is not False:
        raise ConfigurationError("evidence cannot claim wrapper admission")
    if claims.get("semanticConformance") != "unknown":
        raise ConfigurationError("evidence cannot claim semantic conformance")
    if claims.get("proseComplete") != "unknown":
        raise ConfigurationError("evidence cannot claim Prose Complete")
    if claims.get("releaseEligible") is not False:
        raise ConfigurationError("evidence cannot claim release eligibility")
    observation = value["observation"]
    process = value["process"]
    capture = process.get("capture")
    if not isinstance(capture, dict) or set(capture) != {
        "limitBytesPerStream",
        "enforcedLive",
        "outputLimitExceeded",
        "stdoutBytesObserved",
        "stderrBytesObserved",
    }:
        raise ConfigurationError("evidence must contain closed capture facts")
    limit = capture["limitBytesPerStream"]
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ConfigurationError("invalid capture limit")
    for stream in ("stdout", "stderr"):
        captured_bytes = process.get(f"{stream}Bytes")
        observed_bytes = capture.get(f"{stream}BytesObserved")
        if (
            not isinstance(captured_bytes, int)
            or isinstance(captured_bytes, bool)
            or not isinstance(observed_bytes, int)
            or isinstance(observed_bytes, bool)
            or captured_bytes < 0
            or observed_bytes < captured_bytes
        ):
            raise ConfigurationError("observed byte counts cannot understate capture")
    if capture["enforcedLive"] is True:
        exceeded = capture["outputLimitExceeded"]
        if not isinstance(exceeded, bool):
            raise ConfigurationError("live capture must record its output-limit result")
        if process["stdoutBytes"] > limit or process["stderrBytes"] > limit:
            raise ConfigurationError("live capture exceeded its retained byte limit")
        observed_over = (
            capture["stdoutBytesObserved"] > limit
            or capture["stderrBytesObserved"] > limit
        )
        if exceeded is not observed_over:
            raise ConfigurationError(
                "output-limit result disagrees with observed bytes"
            )
    elif capture["enforcedLive"] is False:
        if capture["outputLimitExceeded"] is not None:
            raise ConfigurationError(
                "pre-enforcement evidence cannot infer an output-limit result"
            )
    else:
        raise ConfigurationError("invalid live-capture enforcement fact")
    classified_as_limit = observation.get("classification") == "output-limit"
    if classified_as_limit is not (capture["outputLimitExceeded"] is True):
        raise ConfigurationError("output-limit classification disagrees with capture")
    for stream in ("Stdout", "Stderr"):
        sanitized = process.get(f"sanitized{stream}", "")
        if not isinstance(sanitized, str) or len(sanitized.encode("utf-8")) > limit:
            raise ConfigurationError("sanitized diagnostics exceed the capture limit")
    settlement = process.get("settlement")
    if not isinstance(settlement, dict) or set(settlement) != {
        "scope",
        "leaderReaped",
        "streamsClosed",
        "originalProcessGroupEmpty",
        "detachedDescendantsContained",
        "strictContainmentClaimed",
    }:
        raise ConfigurationError("evidence must contain closed settlement facts")
    if settlement["scope"] not in {
        "original-posix-process-group",
        "direct-process-only",
    }:
        raise ConfigurationError("invalid process settlement scope")
    if (
        settlement["leaderReaped"] is not True
        or settlement["streamsClosed"] is not True
    ):
        raise ConfigurationError("process leader and streams must be settled")
    if settlement["originalProcessGroupEmpty"] not in {True, None}:
        raise ConfigurationError("invalid original process-group settlement fact")
    if settlement["detachedDescendantsContained"] is not False:
        raise ConfigurationError("detached descendants cannot be claimed contained")
    if settlement["strictContainmentClaimed"] is not False:
        raise ConfigurationError(
            "real-harness evidence cannot claim strict containment"
        )
    if (
        settlement["scope"] == "direct-process-only"
        and settlement["originalProcessGroupEmpty"] is not None
    ):
        raise ConfigurationError(
            "direct-process evidence cannot claim group settlement"
        )
    if claims.get("exploratoryRouteCanary") is not True:
        raise ConfigurationError("evidence must identify the exploratory canary lane")
    if claims.get("baseTransportObservation") is not False:
        raise ConfigurationError(
            "direct harness evidence cannot claim adapter transport"
        )
    attempts = observation.get("assistantTerminalAttempts")
    if attempts is not None:
        if observation.get("harnessInternalRetryObserved") is not (attempts > 1):
            raise ConfigurationError(
                "internal retry observation disagrees with attempts"
            )
        if observation.get("frozenRetryPolicySatisfied") is not (attempts <= 1):
            raise ConfigurationError("retry policy observation disagrees with attempts")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")


def report_value(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        evidence, key=lambda item: (item["route"]["id"], item["route"]["trial"])
    )
    rows = [
        {
            "routeId": item["route"]["id"],
            "provider": item["route"]["provider"],
            "model": item["route"]["model"],
            "classification": item["observation"]["classification"],
            "durationMs": item["process"]["durationMs"],
            "usage": item["observation"]["usage"],
            "cost": item["observation"]["cost"],
            "assistantTerminalAttempts": item["observation"].get(
                "assistantTerminalAttempts", 0
            ),
            "frozenRetryPolicySatisfied": item["observation"].get(
                "frozenRetryPolicySatisfied", True
            ),
        }
        for item in ordered
    ]
    known_costs = [
        float(row["cost"]["totalUsd"])
        for row in rows
        if isinstance(row["cost"], dict)
        and isinstance(row["cost"].get("totalUsd"), (int, float))
    ]
    return {
        "schema": "openprose.real-harness-report/1",
        "evidenceCount": len(rows),
        "routeCanaryPasses": sum(
            row["classification"] == "route-canary-pass" for row in rows
        ),
        "harnessReportedCost": {
            "currency": "USD",
            "total": sum(known_costs),
            "observations": len(known_costs),
            "authoritativeForBilling": False,
        },
        "rows": rows,
        "claims": {
            "strictWrapperAdmission": False,
            "semanticConformance": "unknown",
            "proseComplete": "unknown",
            "releaseEligible": False,
        },
    }


def report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Real-harness research observations",
        "",
        "> Research-only direct harness calls. This is not strict wrapper "
        "admission, semantic conformance, Prose Complete evidence, or release "
        "evidence.",
        "> POSIX cleanup covers only the original harness process group; "
        "detached `setsid(2)` descendants are not contained.",
        "",
        "| Route | Provider | Model | Observation | Attempts | Retry policy | "
        "Duration (ms) |",
        "| --- | --- | --- | --- | ---: | --- | ---: |",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['routeId']} | {row['provider']} | {row['model']} | "
            f"{row['classification']} | {row['assistantTerminalAttempts']} | "
            f"{'pass' if row['frozenRetryPolicySatisfied'] else 'VIOLATION'} | "
            f"{row['durationMs']} |"
        )
    lines.extend(
        [
            "",
            "Exploratory route canaries passed: "
            f"{report['routeCanaryPasses']}/{report['evidenceCount']}.",
            "",
            "Harness-reported metered-equivalent cost: "
            f"${report['harnessReportedCost']['total']:.8f} USD "
            "(not billing-authoritative).",
            "",
            "Semantic status: **unknown**. Release eligible: **no**.",
        ]
    )
    return "\n".join(lines) + "\n"


def load_evidence(directory: Path) -> list[dict[str, Any]]:
    evidence = []
    for path in sorted(directory.glob("*.evidence.json")):
        value = read_json(path)
        validate_evidence(value)
        evidence.append(value)
    return evidence


def command_validate(args: argparse.Namespace) -> int:
    policy = read_json(args.policy)
    matrix = read_json(args.matrix)
    validate_frozen_inputs(policy, matrix)
    expected_policy_digest = sha256_bytes(canonical_json(policy))
    expected_matrix_digest = sha256_bytes(canonical_json(matrix))
    for value in load_evidence(args.evidence_dir) if args.evidence_dir else []:
        validate_evidence(value)
        if value["policySha256"] != expected_policy_digest:
            raise ConfigurationError(
                "evidence policy digest does not match frozen policy"
            )
        if value["matrixSha256"] != expected_matrix_digest:
            raise ConfigurationError(
                "evidence matrix digest does not match frozen matrix"
            )
        if value["process"]["capture"]["limitBytesPerStream"] != int(
            policy["limits"]["maximumPersistedStreamBytes"]
        ):
            raise ConfigurationError(
                "evidence stream limit does not match frozen policy"
            )
    return 0


def command_report(args: argparse.Namespace) -> int:
    evidence = load_evidence(args.evidence_dir)
    report = report_value(evidence)
    write_json(args.output_json, report)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text(report_markdown(report), "utf-8")
    return 0


def command_run(args: argparse.Namespace) -> int:
    if not args.i_understand_this_spends_money:
        raise ConfigurationError(
            "real probes are opt-in; pass --i-understand-this-spends-money"
        )
    policy = read_json(args.policy)
    matrix = read_json(args.matrix)
    validate_frozen_inputs(policy, matrix)
    if not args.executable.is_absolute():
        raise ConfigurationError("--executable must be an absolute path")
    if not args.accept_research_only_incompatible_version:
        raise ConfigurationError(
            "0.7.0 is incompatible with the stable 0.8.1 recipe; pass "
            "--accept-research-only-incompatible-version to record research evidence"
        )
    selected = set(args.route)
    routes = [
        route for route in matrix["routes"] if not selected or route["id"] in selected
    ]
    unknown = selected - {route["id"] for route in routes}
    if unknown:
        raise ConfigurationError("unknown route(s): " + ", ".join(sorted(unknown)))
    credential_names = {
        name for route in routes for name in route["credentialEnvironment"]
    }
    credentials = parse_dotenv_selected(args.env_file, credential_names)
    projected = sum(float(route["plannedCostCeilingUsd"]) for route in routes)
    if projected > float(policy["limits"]["maximumTotalCostUsd"]) + 1e-9:
        raise ConfigurationError("selected planned cost ceilings exceed total budget")
    executable = args.executable.resolve(strict=True)
    base_environment = isolated_environment({})
    version = observe_version(
        executable,
        base_environment,
        int(policy["limits"]["maximumPersistedStreamBytes"]),
    )
    expected_version = matrix["harness"]["expectedObservedVersion"]
    if version != expected_version:
        raise ConfigurationError(
            f"observed harness version {version!r}, expected research version "
            f"{expected_version!r}"
        )
    digest = executable_digest(executable)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    evidence: list[dict[str, Any]] = []
    for route in routes:
        value = run_route(
            executable=executable,
            executable_sha256=digest,
            observed_version=version,
            policy=policy,
            matrix=matrix,
            route=route,
            env_file=args.env_file,
            credential_values=credentials,
        )
        validate_evidence(value)
        write_json(args.output_dir / f"{route['id']}.evidence.json", value)
        evidence.append(value)
    report = report_value(evidence)
    write_json(args.output_dir / "report.json", report)
    (args.output_dir / "REPORT.md").write_text(report_markdown(report), "utf-8")
    return (
        0
        if all(
            item["observation"]["classification"] == "route-canary-pass"
            for item in evidence
        )
        else 1
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate frozen inputs/evidence")
    validate.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    validate.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    validate.add_argument("--evidence-dir", type=Path)
    validate.set_defaults(handler=command_validate)

    report = subparsers.add_parser("report", help="deterministically rebuild reports")
    report.add_argument("--evidence-dir", type=Path, required=True)
    report.add_argument("--output-json", type=Path, required=True)
    report.add_argument("--output-markdown", type=Path, required=True)
    report.set_defaults(handler=command_report)

    run = subparsers.add_parser("run", help="perform explicitly authorized real probes")
    run.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    run.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    run.add_argument("--executable", type=Path, required=True)
    run.add_argument("--env-file", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--route", action="append", default=[])
    run.add_argument("--i-understand-this-spends-money", action="store_true")
    run.add_argument("--accept-research-only-incompatible-version", action="store_true")
    run.set_defaults(handler=command_run)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (ConfigurationError, FileNotFoundError, json.JSONDecodeError) as error:
        print(f"real-harness: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
