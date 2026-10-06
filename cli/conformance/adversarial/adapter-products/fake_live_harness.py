#!/usr/bin/env python3
"""Provider-free installed harness used by the functional-alpha parity oracle."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import stat
import sys
import time
from typing import Any


VERSIONS = {
    "prose-agents-sdk": "prose-agents-sdk 0.1.0",
    "codex": "codex-cli 0.149.0-alpha.4.1",
    "claude": "2.1.243 (Claude Code)",
    "prime-agent": "prime-agent 0.7.0",
    "omp": "omp/18.0.9",
}
ADAPTERS = {
    "prose-agents-sdk": "agents-sdk/jsonl",
    "codex": "codex/exec-json",
    "claude": "claude/print-stream-json",
    "prime-agent": "prime/rpc",
    "omp": "omp/rpc",
}


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def emit(*records: dict[str, Any]) -> None:
    for record in records:
        sys.stdout.write(compact(record) + "\n")
    sys.stdout.flush()


def record_forbidden_auth_probe() -> None:
    guard = Path.cwd() / ".forbid-harness-login-auth-probe"
    if guard.is_file():
        Path(guard.read_text("utf-8")).write_text(
            "unexpected installed auth probe", encoding="utf-8"
        )


def task_from_launch(argv: list[str], stdin: bytes) -> dict[str, Any]:
    for value in reversed(argv):
        try:
            candidate = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("argv"), list):
            return candidate

    for line in stdin.splitlines():
        try:
            candidate = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("argv"), list):
            return candidate
        message = candidate.get("message") if isinstance(candidate, dict) else None
        if isinstance(message, str):
            try:
                nested = json.loads(message)
            except json.JSONDecodeError:
                nested = None
            if isinstance(nested, dict) and isinstance(nested.get("argv"), list):
                return nested

    text = stdin.decode("utf-8", errors="strict")
    marker = '<task sha256="'
    task_start = text.find(marker)
    body_start = text.find(">\n", task_start)
    body_end = text.find("\n</task>", body_start + 2)
    if task_start >= 0 and body_start >= 0 and body_end >= 0:
        candidate = json.loads(text[body_start + 2 : body_end])
        if isinstance(candidate, dict) and isinstance(candidate.get("argv"), list):
            return candidate
    raise ValueError("functional-alpha probe did not receive a task envelope")


def prompt_id(stdin: bytes) -> str | None:
    for line in stdin.splitlines():
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("type") == "prompt":
            value = record.get("id")
            return value if isinstance(value, str) else None
    return None


def prompt_files(argv: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for flag in ("--append-system-prompt-file", "--append-system-prompt", "--config", "--instructions"):
        if flag not in argv:
            continue
        index = argv.index(flag)
        if index + 1 >= len(argv):
            continue
        path = Path(argv[index + 1])
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        data = path.read_bytes()
        result.append(
            {
                "flag": flag,
                "path": str(path),
                "byteLength": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "base64": base64.b64encode(data).decode("ascii"),
                "mode": None
                if sys.platform == "win32"
                else f"{stat.S_IMODE(path.stat().st_mode):04o}",
            }
        )
    return result


def daemon_socket(argv: list[str]) -> dict[str, Any] | None:
    if "--daemon-socket" not in argv:
        return None
    if argv.count("--daemon-socket") != 1:
        raise ValueError("daemon socket flag must occur exactly once")
    index = argv.index("--daemon-socket")
    if index + 1 >= len(argv):
        raise ValueError("daemon socket flag requires a path")
    path = Path(argv[index + 1])
    return {
        "path": str(path),
        "absolute": path.is_absolute(),
        "existedAtHarnessStart": path.exists(),
        "parentMode": None
        if sys.platform == "win32"
        else f"{stat.S_IMODE(path.parent.stat().st_mode):04o}",
    }


def credential_config(adapter_id: str) -> dict[str, Any] | None:
    name = {
        "prime/rpc": "PRIME_AGENT_CODING_AGENT_DIR",
        "omp/rpc": "PI_CODING_AGENT_DIR",
    }.get(adapter_id)
    if name is None or name not in os.environ:
        return None
    path = Path(os.environ[name])
    return {
        "name": name,
        "path": str(path),
        "absolute": path.is_absolute(),
        "existsAtHarnessStart": path.is_dir(),
        "mode": None
        if sys.platform == "win32"
        else f"{stat.S_IMODE(path.stat().st_mode):04o}",
    }


def spawn_owned_prime_daemon(path: Path, specification: dict[str, Any]) -> None:
    """Fork one detached, exact-path fake service for wrapper settlement tests."""
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - asserted through the product boundary
        try:
            os.close(ready_read)
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(devnull, descriptor)
            if devnull > 2:
                os.close(devnull)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(8)
            ready_path = specification.get("readyPath")
            if isinstance(ready_path, str):
                Path(ready_path).write_text(str(path), encoding="utf-8")
            os.write(ready_write, b"1")
            os.close(ready_write)
            connection, _ = server.accept()
            hello = {
                "type": "daemon_hello",
                "socketPath": str(path),
                "protocol": {
                    "name": "prime-agent.daemon",
                    "version": 7,
                },
                "appVersion": VERSIONS["prime-agent"],
                "clientId": "fixture-daemon",
                "serverCapabilities": [],
            }
            if specification.get("hello") == "wrong-protocol":
                hello["protocol"]["name"] = "not-prime-agent.daemon"
            connection.sendall(compact(hello).encode("utf-8") + b"\n")
            if specification.get("hello") == "wrong-protocol":
                connection.close()
                server.settimeout(1)
                for _ in range(3):
                    try:
                        extra_connection, _ = server.accept()
                    except TimeoutError:
                        break
                    extra_connection.sendall(
                        compact(hello).encode("utf-8") + b"\n"
                    )
                    extra_connection.close()
                server.close()
                os._exit(0)
            command_wire = b""
            while b"\n" not in command_wire and len(command_wire) <= 65536:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                command_wire += chunk
            command = json.loads(command_wire.split(b"\n", 1)[0])
            response = {
                "type": "response",
                "id": command.get("id"),
                "command": "shutdown",
                "success": True,
            }
            connection.sendall(compact(response).encode("utf-8") + b"\n")
            connection.close()
            if specification.get("keepListening"):
                time.sleep(5)
            server.close()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        finally:
            os._exit(0)
    os.close(ready_write)
    readable, _, _ = select.select([ready_read], [], [], 2)
    if not readable or os.read(ready_read, 1) != b"1":
        os.close(ready_read)
        raise RuntimeError("owned Prime fixture daemon did not bind")
    os.close(ready_read)


def terminal_for_image(
    argv: list[str], stdin: bytes, files: list[dict[str, Any]], task: dict[str, Any]
) -> str:
    sentinel_marker = b"OPENPROSE_SENTINEL_IMAGE_V1"
    image_candidates = [stdin, *(base64.b64decode(entry["base64"]) for entry in files)]
    image_candidates.extend(value.encode("utf-8") for value in argv)
    if any(sentinel_marker in candidate for candidate in image_candidates):
        return compact(
            {
                "schema": "openprose.sentinel-terminal-envelope/1",
                "semanticStatus": "not-applicable",
                "marker": "OPENPROSE_SENTINEL_TERMINAL_V1",
            }
        )
    return compact(
        {
            "schema": "openprose.echo-terminal/1",
            "semanticStatus": "not-applicable",
            "placeholder": True,
            "marker": "OPENPROSE_ECHO_TERMINAL_V0",
            "task": {"argv": task["argv"]},
        }
    )


def sdk_emit(record: dict[str, Any], lexemes: dict[str, str]) -> None:
    # Closed provider-free fixtures exercise equivalent JSON numeric encodings.
    encoded = compact(record)
    for key, lexeme in lexemes.items():
        value = json.loads(lexeme)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('SDK fixture numeric lexeme must be a JSON number')
        token = compact(key) + ':'
        start = encoded.find(token)
        if start < 0:
            continue
        start += len(token)
        end = start
        while end < len(encoded) and encoded[end] not in ',}':
            end += 1
        if json.loads(encoded[start:end]) != value:
            raise ValueError('SDK fixture numeric lexeme changed its value')
        encoded = encoded[:start] + lexeme + encoded[end:]
    if json.loads(encoded) != record:
        raise ValueError('SDK fixture wire encoding changed its record')
    sys.stdout.write(encoded + '\n')
    sys.stdout.flush()


def sdk_main(argv: list[str]) -> int:
    """No provider dependencies: closed records supplied by the shared SDK oracle."""
    workspace = Path.cwd()
    control = json.loads((workspace / ".sdk-compatibility-fixture.json").read_text("utf-8"))
    if Path(sys.argv[0]).resolve() != Path(control["expectedHelper"]):
        (workspace / ".sdk-wrong-helper-used").write_text("PATH helper used", encoding="utf-8")
        return 88
    if argv == ["--version"]:
        (workspace / ".sdk-harness-probed").write_text("version", encoding="utf-8")
        print(VERSIONS['prose-agents-sdk'])
        return 0
    (workspace / ".sdk-harness-started").write_text("started", encoding="utf-8")
    if control["scenario"] == "trap":
        return 88
    task = task_from_launch(argv, b'')
    files = prompt_files(argv)
    model = argv[argv.index('--model') + 1]
    if model != control["model"] or '--cwd' not in argv or Path(argv[argv.index('--cwd') + 1]).resolve() != workspace.resolve():
        return 89
    terminal = terminal_for_image(argv, b'', files, task)
    common = dict(usageObservation=control['usageObservation'], modelIdentity=control['modelIdentity'])
    sdk_emit(dict(type='start', model=model, cwd=str(workspace), limits=control['limits']), control['wireNumberLexemes'])
    if control['scenario'] == 'failure':
        sdk_emit(dict(type='error', error_type='ExecutionError', message='safe fixture failure', limits=control['limits'], **common), control['wireNumberLexemes'])
        return 1
    sdk_emit(dict(type='final', output=terminal, usage={'requests':3,'input_tokens':9,'output_tokens':6,'total_tokens':15}, **common), control['wireNumberLexemes'])
    return 0


def main() -> int:
    executable = Path(sys.argv[0]).name
    argv = sys.argv[1:]
    if executable not in ADAPTERS:
        raise ValueError(f"unexpected executable identity: {executable}")
    if executable == "prose-agents-sdk":
        return sdk_main(argv)
    compatibility_path = Path.cwd() / ".claude-compatibility-fixture.json"
    compatibility = json.loads(compatibility_path.read_text("utf-8")) if executable == "claude" and compatibility_path.is_file() else {}
    if executable == "codex":
        path = Path.cwd() / ".codex-compatibility-fixture.json"
        compatibility = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        if "version" in compatibility:
            compatibility["version"] = "codex-cli " + compatibility["version"]
        if argv == ["exec", "--help"]:
            scenario = compatibility.get("capabilityScenario")
            options = "--skip-git-repo-check --json --ephemeral --ignore-user-config --ignore-rules --cd --model --sandbox --config"
            if scenario == "missing-json":
                options = options.replace("--json", "")
            if scenario == "missing-sandbox":
                options = options.replace("--sandbox", "")
            if scenario == "prefix-json":
                options = options.replace("--json", "--json-future")
            if scenario == "descriptive-json":
                options = options.replace("--json", "")
            if scenario == "oversized":
                options += " " * 32768
            options = "\n".join(options.split()) if scenario != "oversized" else options
            if scenario == "descriptive-json":
                options += "\nThis describes --json"
            print(options, file=sys.stderr if scenario == "wrong-stream" else sys.stdout)
            return 2 if scenario == "nonzero" else 0
    if argv == ["--version"]:
        stream = sys.stderr if executable == "prime-agent" else sys.stdout
        print(compatibility.get("version", VERSIONS[executable]), file=stream)
        return 0
    if executable == "codex" and argv == ["login", "status"]:
        print("Logged in using ChatGPT")
        return 0
    if executable == "claude" and argv == ["auth", "status", "--json"]:
        print('{"loggedIn":true}')
        return 0
    if executable == "prime-agent" and argv == ["model", "list"]:
        record_forbidden_auth_probe()
        print("fixture/model")
        return 0
    if executable == "omp" and argv == ["--help"]:
        record_forbidden_auth_probe()
        print("Usage: omp")
        return 0

    adapter_id = ADAPTERS[executable]
    terminal_close = adapter_id in {"prime/rpc", "omp/rpc"}
    if adapter_id == "omp/rpc":
        control_fixture_path = Path.cwd() / ".omp-control-fixture.json"
        control_mode = None
        if control_fixture_path.is_file():
            control_mode = json.loads(control_fixture_path.read_text("utf-8")).get("mode")
        emit(
            {
                "type": "ready",
                "protocolVersion": 1,
                "supportedProtocolVersions": [1, 2],
                "maxFrameBytes": 1048576,
                "maxReassembledFrameBytes": 67108864,
            },
            {"type": "available_commands_update", "commands": []},
        )
        state_bytes = sys.stdin.buffer.readline()
        state_request = json.loads(state_bytes)
        if set(state_request) != {"id", "type"} or state_request.get("type") != "get_state":
            raise ValueError("OMP staged controller did not send the exact get_state request")
        if control_mode == "reordered-lifecycle":
            emit({"type": "agent_start"})
            return 0
        if control_mode == "interactive-ui":
            emit({"type": "extension_ui_request", "id": "fixture-ui", "method": "confirm"})
            return 0
        state_response = {
            "id": "uncorrelated.omp.state.1"
            if control_mode == "uncorrelated-state"
            else state_request["id"],
            "type": "response",
            "command": "get_state",
            "success": control_mode != "failed-state",
            "data": {
                "dumpTools": [{"name": "hostile-tool"}]
                if control_mode == "nonempty-tools"
                else [],
                **(
                    {"candidateSecret": "OPENPROSE-CANDIDATE-CANARY::omp-state"}
                    if control_mode == "candidate-canary"
                    else {}
                ),
            },
        }
        emit(state_response)
        if control_mode == "duplicate-state":
            emit(state_response)
            return 0
        if control_mode == "accepted-input-eof":
            # Use an admitted empty inventory, then accept the queued prompt.
            # A raced BrokenPipe must not hide a retained-stdin leak.
            if not sys.stdin.buffer.readline():
                raise RuntimeError("expected the queued prompt before fixture exit")
            return 0
        if control_mode in {"uncorrelated-state", "failed-state", "nonempty-tools"}:
            return 0
        prompt_bytes = sys.stdin.buffer.readline()
        stdin = state_bytes + prompt_bytes
    else:
        stdin = sys.stdin.buffer.readline() if terminal_close else sys.stdin.buffer.read()
    if terminal_close and stdin_eof_is_ready():
        print("functional-alpha probe observed stdin EOF before agent_end", file=sys.stderr)
        return 91
    files = prompt_files(argv)
    observation = {
        "adapterId": adapter_id,
        "argv": [sys.argv[0], *argv],
        "environmentNames": sorted(os.environ),
        "stdin": {
            "base64": base64.b64encode(stdin).decode("ascii"),
            "byteLength": len(stdin),
            "sha256": hashlib.sha256(stdin).hexdigest(),
        },
        "files": files,
        "daemonSocket": daemon_socket(argv),
        "credentialConfig": credential_config(adapter_id),
        "adapterControls": (
            {"PRIME_AGENT_TELEMETRY": os.environ.get("PRIME_AGENT_TELEMETRY")}
            if adapter_id == "prime/rpc"
            else {}
        ),
    }
    observation_path = Path(sys.argv[0]).parent.parent / "observation.json"

    service_specification_path = Path.cwd() / ".prime-owned-service-fixture.json"
    service_specification = None
    if adapter_id == "prime/rpc" and service_specification_path.is_file():
        service_specification = json.loads(service_specification_path.read_text("utf-8"))
        socket_observation = observation["daemonSocket"]
        if not isinstance(socket_observation, dict):
            raise ValueError("owned Prime fixture requires the exact daemon socket")
        spawn_owned_prime_daemon(Path(socket_observation["path"]), service_specification)
        exit_mode = service_specification.get("exitMode")
        if exit_mode == "protocol":
            sys.stdout.write("{malformed\n")
            sys.stdout.flush()
            return 0
        if exit_mode == "child-failure":
            return 9
        if exit_mode in {"timeout", "cancellation"}:
            time.sleep(10)

    if adapter_id == "codex/exec-json" and (Path.cwd() / ".native-capture-secrets-fixture").is_file():
        text = "C command_execution mm " + " ".join(os.environ[name] for name in (
            "OPENAI_API_KEY", "OPENPROSE_RECURSION_TOKEN", "OPENPROSE_RUN_NONCE"
        ))
        emit(
            {"type": "thread.started", "thread_id": "command_execution"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "item-1", "type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": {}},
        )
        return 0

    output_fixture = Path.cwd() / ".native-output-limit-fixture.json"
    if adapter_id == "codex/exec-json" and output_fixture.is_file():
        mode = json.loads(output_fixture.read_text("utf-8"))["mode"]
        if mode not in {"capture", "stdout"}:
            raise ValueError("unknown output-limit fixture")
        (Path.cwd() / "harness.pid").write_text(str(os.getpid()), encoding="ascii")
        emit({"type": "thread.started", "thread_id": "fixture-thread"}, {"type": "turn.started"})
        text = "é" + ("XY" * 2048 if mode == "capture" else "a" * 16384)
        try:
            for index in range(100):
                emit({"type": "item.completed", "item": {"id": f"item-{index}", "type": "agent_message", "text": text}})
        except BrokenPipeError:
            # The runner has closed its bounded reader. Avoid fixture shutdown noise.
            os._exit(0)
        # A limit failure must settle this still-running process before returning.
        time.sleep(30)
        return 0

    task = task_from_launch(argv, stdin)
    visible = ["Echoed task argv:", compact(task["argv"])]
    terminal = terminal_for_image(argv, stdin, files, task)
    if service_specification and service_specification.get("exitMode") == "postprocess":
        terminal = compact({"not": "the image-declared terminal envelope"})
    if adapter_id == "codex/exec-json":
        if compatibility.get("protocolScenario") is not None:
            emit({"type":"thread.started","thread_id":"fixture-thread"},{"type":"turn.started"})
            if compatibility["protocolScenario"] == "unknown-type":
                emit({"type":"future.unsupported"})
            return 0
        emit(
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.started"},
            *(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"item-{index}",
                        "type": "agent_message",
                        "text": text,
                    },
                }
                for index, text in enumerate([*visible, terminal], start=1)
            ),
            {"type": "turn.completed", "usage": {}},
        )
    elif adapter_id == "claude/print-stream-json":
        telemetry = []
        scenario = compatibility.get("telemetryScenario")
        if scenario is not None:
            record = {"type": "rate_limit_event", "session_id": "fixture-session"}
            if scenario == "unknown-subtype":
                record.update(type="system", subtype="future_telemetry")
            telemetry.append(record)
        emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "fixture-session",
                "model": "fixture/model",
                "tools": [],
                "mcp_servers": [],
                "plugins": [],
            },
            *telemetry,
            *(
                {
                    "type": "assistant",
                    "session_id": "fixture-session",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
                for text in [*visible, terminal]
            ),
            {
                "type": "result",
                "subtype": "success",
                "session_id": "fixture-session",
                "is_error": False,
                "result": terminal,
            },
        )
    elif adapter_id == "prime/rpc":
        response_id = prompt_id(stdin)
        assistant_text = "\n".join([*visible, terminal])
        user_message = {
            "role": "user",
            "content": [{"type": "text", "text": compact(task)}],
            "timestamp": 1_700_000_000_000,
        }
        usage = {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "total": 0,
            },
        }

        def assistant_message(
            content: list[dict[str, Any]], *, response_id: bool = False
        ) -> dict[str, Any]:
            message = {
                "role": "assistant",
                "content": content,
                "api": "fixture-jsonl",
                "provider": "fixture",
                "model": "fixture/model",
                "usage": usage,
                "stopReason": "stop",
                "timestamp": 1_700_000_000_001,
            }
            if response_id:
                message["responseId"] = "fixture-response-id"
            return message

        thinking_text = "synthetic reasoning summary"
        thinking = {
            "type": "thinking",
            "thinking": thinking_text,
            "thinkingSignature": "fixture-thinking-signature",
        }
        thinking_started = assistant_message(
            [{"type": "thinking", "thinking": ""}], response_id=True
        )
        thinking_ended = assistant_message([thinking], response_id=True)
        thinking_break_one = len(thinking_text) // 3
        thinking_break_two = (len(thinking_text) * 2) // 3
        streamed_thinking = ""
        thinking_updates = []
        for delta in [
            thinking_text[:thinking_break_one],
            thinking_text[thinking_break_one:thinking_break_two],
            thinking_text[thinking_break_two:] + "\n\n",
        ]:
            streamed_thinking += delta
            thinking_updates.append(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "thinking_delta",
                        "contentIndex": 0,
                        "delta": delta,
                    },
                    "message": assistant_message(
                        [{"type": "thinking", "thinking": streamed_thinking}],
                        response_id=True,
                    ),
                }
            )
        text_started = assistant_message(
            [thinking, {"type": "text", "text": ""}], response_id=True
        )
        first_break = len(assistant_text) // 3
        second_break = (len(assistant_text) * 2) // 3
        streamed_text = ""
        text_updates = []
        for delta in [
            assistant_text[:first_break],
            assistant_text[first_break:second_break],
            assistant_text[second_break:],
        ]:
            streamed_text += delta
            message = assistant_message(
                [thinking, {"type": "text", "text": streamed_text}],
                response_id=True,
            )
            text_updates.append(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "contentIndex": 1,
                        "delta": delta,
                    },
                    "message": message,
                }
            )
        text_complete = assistant_message(
            [
                thinking,
                {
                    "type": "text",
                    "text": assistant_text,
                    "textSignature": "fixture-text-signature",
                },
            ],
            response_id=True,
        )
        records = (
            {
                "id": response_id,
                "type": "response",
                "command": "prompt",
                "success": True,
            },
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "message_start", "message": user_message},
            {"type": "message_end", "message": user_message},
            {"type": "message_start", "message": assistant_message([])},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_start",
                    "contentIndex": 0,
                },
                "message": thinking_started,
            },
            *thinking_updates,
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_end",
                    "contentIndex": 0,
                    "content": thinking_text,
                },
                "message": thinking_ended,
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_start",
                    "contentIndex": 1,
                },
                "message": text_started,
            },
            *text_updates,
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "contentIndex": 1,
                    "content": assistant_text,
                },
                "message": text_complete,
            },
            {"type": "message_end", "message": text_complete},
            {"type": "turn_end", "message": text_complete, "toolResults": []},
            {"type": "agent_end", "messages": [user_message, text_complete]},
        )
        lifecycle_fixture_path = Path.cwd() / ".prime-lifecycle-fixture.json"
        if lifecycle_fixture_path.is_file():
            lifecycle_fixture = json.loads(lifecycle_fixture_path.read_text("utf-8"))
            if lifecycle_fixture != {"mode": "text-only-index-zero"}:
                raise ValueError("Prime lifecycle fixture mode is invalid")
            text_only_records = json.loads(
                json.dumps([*records[:6], *records[11:]], separators=(",", ":"))
            )

            def strip_thinking(message: Any) -> None:
                if (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and isinstance(message.get("content"), list)
                    and len(message["content"]) == 2
                ):
                    message["content"] = [message["content"][1]]

            for record in text_only_records:
                event = record.get("assistantMessageEvent")
                if isinstance(event, dict) and event.get("contentIndex") == 1:
                    event["contentIndex"] = 0
                strip_thinking(record.get("message"))
                messages = record.get("messages")
                if isinstance(messages, list):
                    for message in messages:
                        strip_thinking(message)
            records = tuple(text_only_records)
        parser_fixture_path = Path.cwd() / ".prime-parser-diagnostic-fixture.json"
        if parser_fixture_path.is_file():
            parser_fixture = json.loads(parser_fixture_path.read_text("utf-8"))
            accepted_records = parser_fixture.get("acceptedRecords")
            if (
                not isinstance(accepted_records, int)
                or isinstance(accepted_records, bool)
                or not 0 <= accepted_records < len(records)
            ):
                raise ValueError("Prime parser fixture acceptedRecords is invalid")
            emit(*records[:accepted_records])
            fault = parser_fixture.get("fault")
            if fault == "malformed-record":
                sys.stdout.write("OPENPROSE-CANDIDATE-CANARY::{malformed\n")
                sys.stdout.flush()
            elif fault == "partial-record":
                sys.stdout.write(
                    '{"candidateSecret":"OPENPROSE-CANDIDATE-CANARY::partial"'
                )
                sys.stdout.flush()
            elif fault in {
                "first-content-record",
                "lifecycle-record",
                "lifecycle-then-malformed",
            }:
                prior_message = records[accepted_records - 1].get("message")
                if not isinstance(prior_message, dict):
                    raise ValueError(
                        "Prime lifecycle fixture requires a preceding current message"
                    )
                invalid_lifecycle = {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "contentIndex": 1,
                        "delta": "OPENPROSE-CANDIDATE-CANARY::lifecycle",
                    },
                    "message": prior_message,
                }
                if fault == "lifecycle-then-malformed":
                    emit(invalid_lifecycle)
                    sys.stdout.write("OPENPROSE-CANDIDATE-CANARY::{later-malformed\n")
                    sys.stdout.flush()
                else:
                    emit(invalid_lifecycle, *records[accepted_records:])
                    if sys.stdin.buffer.read() != b"":
                        return 92
            else:
                raise ValueError("Prime parser fixture fault is invalid")
            return 0
        emit(*records)
        if sys.stdin.buffer.read() != b"":
            return 92
    else:
        response_id = prompt_id(stdin)
        assistant_text = "\n".join([*visible, terminal])
        user_message = {"role": "user", "content": task}
        assistant_message = {
            "role": "assistant",
            "content": [{"type": "text", "text": assistant_text}],
        }
        emit(
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "message_start", "message": user_message},
            {"type": "message_end", "message": user_message},
            {"type": "message_start", "message": {"role": "assistant", "content": []}},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": assistant_text},
                "message": {"role": "assistant", "content": []},
            },
            {"type": "message_end", "message": assistant_message},
            {"type": "turn_end", "message": assistant_message, "toolResults": []},
            {
                "type": "agent_end",
                "messages": [user_message, assistant_message],
                "isTerminal": control_mode != "nonterminal-settlement",
            },
        )
        if sys.stdin.buffer.read() != b"":
            return 92
        emit({"id": response_id, "type": "response", "command": "prompt", "success": True})
    observation_path.write_text(compact(observation) + "\n", encoding="utf-8")
    return 0


def stdin_eof_is_ready() -> bool:
    if os.name == "nt":
        return False
    readable, _, _ = select.select([sys.stdin.buffer], [], [], 0)
    if not readable:
        return False
    extra = os.read(sys.stdin.buffer.fileno(), 1)
    if extra:
        raise ValueError("functional-alpha probe received multiple RPC requests")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
