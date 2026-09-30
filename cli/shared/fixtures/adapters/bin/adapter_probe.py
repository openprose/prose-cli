#!/usr/bin/env python3
"""Provider-free executable that records an adapter launch without a shell."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import sys
from typing import Any


CONTROL_NAMES = {
    "OPENPROSE_ADAPTER_OBSERVATION_PATH",
    "OPENPROSE_ADAPTER_EXPECTED_ID",
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def infer_adapter(argv: list[str]) -> str:
    expected = os.environ.get("OPENPROSE_ADAPTER_EXPECTED_ID")
    if expected:
        return expected
    if argv and argv[0] == "exec":
        return "codex/exec-json"
    if "--safe-mode" in argv:
        return "claude/print-stream-json"
    if "--append-system-prompt" in argv:
        return "omp/rpc"
    return "prime/rpc"


def read_prompt_files(argv: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for flag in ("--instructions", "--append-system-prompt-file", "--append-system-prompt", "--config"):
        if flag not in argv:
            continue
        index = argv.index(flag)
        if index + 1 >= len(argv):
            continue
        path = Path(argv[index + 1])
        try:
            is_file = path.is_file()
        except OSError:
            # Prime carries the image itself in --append-system-prompt. Long
            # UTF-8 prompt bytes are not a path and may exceed NAME_MAX.
            is_file = False
        if not is_file:
            continue
        data = path.read_bytes()
        records.append(
            {
                "flag": flag,
                "path": str(path),
                "byteLength": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "base64": base64.b64encode(data).decode("ascii"),
                "mode": None if os.name == "nt" else f"{stat.S_IMODE(path.stat().st_mode):04o}",
            }
        )
    return records


def read_daemon_socket(argv: list[str]) -> dict[str, Any] | None:
    if "--daemon-socket" not in argv:
        return None
    if argv.count("--daemon-socket") != 1:
        raise ValueError("daemon socket flag must occur exactly once")
    index = argv.index("--daemon-socket")
    if index + 1 >= len(argv):
        raise ValueError("daemon socket flag requires a path")
    path = Path(argv[index + 1])
    parent = path.parent
    return {
        "path": str(path),
        "absolute": path.is_absolute(),
        "existedAtHarnessStart": path.exists(),
        "parentMode": None if os.name == "nt" else f"{stat.S_IMODE(parent.stat().st_mode):04o}",
    }


def read_credential_config(adapter_id: str) -> dict[str, Any] | None:
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


def emit(records: list[dict[str, Any]]) -> None:
    for record in records:
        sys.stdout.write(canonical(record) + "\n")
    sys.stdout.flush()


def rpc_prompt_id(stdin: bytes) -> str | None:
    for line in stdin.splitlines():
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(record, dict)
            and record.get("type") == "prompt"
            and isinstance(record.get("id"), str)
        ):
            return record["id"]
    return None


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
            if message:
                return {"argv": [message]}

    text = stdin.decode("utf-8", errors="strict")
    task_start = text.find("<task sha256=\"")
    if task_start >= 0:
        body_start = text.find(">\n", task_start)
        body_end = text.find("\n</task>", body_start + 2)
        if body_start >= 0 and body_end >= 0:
            candidate = json.loads(text[body_start + 2 : body_end])
            if isinstance(candidate, dict) and isinstance(candidate.get("argv"), list):
                return candidate
    raise ValueError("adapter probe did not receive a task envelope")


def echo_assistant_text(task: dict[str, Any]) -> str:
    terminal = {
        "schema": "openprose.echo-terminal/1",
        "semanticStatus": "not-applicable",
        "placeholder": True,
        "marker": "OPENPROSE_ECHO_TERMINAL_V0",
        "task": {"argv": task["argv"]},
    }
    terminal_text = json.dumps(
        terminal, ensure_ascii=False, separators=(",", ":")
    )
    argv_text = json.dumps(
        task["argv"], ensure_ascii=False, separators=(",", ":")
    )
    return f"Echoed task argv: {argv_text}\n{terminal_text}"


def main() -> int:
    argv = sys.argv[1:]
    adapter_id = infer_adapter(argv)
    terminal_close = adapter_id in {"prime/rpc", "omp/rpc"}
    if adapter_id == "omp/rpc":
        emit(
            [
                {
                    "type": "ready",
                    "protocolVersion": 1,
                    "supportedProtocolVersions": [1, 2],
                    "maxFrameBytes": 1048576,
                    "maxReassembledFrameBytes": 67108864,
                },
                {
                    "type": "extension_ui_request",
                    "id": "widget-request-startup",
                    "method": "setWidget",
                    "widgetKey": "autoresearch",
                },
                {"type": "available_commands_update", "commands": []},
            ]
        )
        state_bytes = sys.stdin.buffer.readline()
        state = json.loads(state_bytes)
        if set(state) != {"id", "type"} or state.get("type") != "get_state" or not isinstance(state.get("id"), str):
            print("adapter probe did not receive the exact OMP state request", file=sys.stderr)
            return 90
        emit([{"id": state["id"], "type": "response", "command": "get_state", "success": True, "data": {"dumpTools": []}}])
        prompt_bytes = sys.stdin.buffer.readline()
        stdin = state_bytes + prompt_bytes
    else:
        stdin = sys.stdin.buffer.readline() if terminal_close else sys.stdin.buffer.read()
    if terminal_close and stdin_eof_is_ready():
        print("adapter probe observed stdin EOF before the RPC terminal", file=sys.stderr)
        return 91
    observation = {
        "schema": "openprose.adapter-probe-observation/1",
        "adapterId": adapter_id,
        "argv": [sys.argv[0], *argv],
        "cwd": os.getcwd(),
        "environmentNames": sorted(name for name in os.environ if name not in CONTROL_NAMES),
        "stdin": {
            "byteLength": len(stdin),
            "sha256": hashlib.sha256(stdin).hexdigest(),
            "base64": base64.b64encode(stdin).decode("ascii"),
        },
        "files": read_prompt_files(argv),
        "daemonSocket": read_daemon_socket(argv),
        "credentialConfig": read_credential_config(adapter_id),
        "adapterControls": (
            {"PRIME_AGENT_TELEMETRY": os.environ.get("PRIME_AGENT_TELEMETRY")}
            if adapter_id == "prime/rpc"
            else {}
        ),
        "shell": False,
        "outerPty": False,
    }
    task = task_from_launch(argv, stdin)
    assistant_text = echo_assistant_text(task)
    if adapter_id == "codex/exec-json":
        emit(
            [
                {"type": "thread.started", "thread_id": "fake-thread-0001"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"id": "item-1", "type": "agent_message", "text": assistant_text}},
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1,
                        "cached_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 0,
                    },
                },
            ]
        )
    elif adapter_id == "agents-sdk/jsonl":
        emit([
            {"type": "start", "model": "fixture", "cwd": "fixture"},
            {"type": "final", "output": assistant_text},
        ])
    elif adapter_id == "claude/print-stream-json":
        emit(
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "fake-session-0001",
                    "model": "fake",
                    "tools": [],
                    "mcp_servers": [],
                    "plugins": [],
                },
                {
                    "type": "assistant",
                    "session_id": "fake-session-0001",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "fake-session-0001",
                    "is_error": False,
                    "result": assistant_text,
                },
            ]
        )
    elif adapter_id == "prime/rpc":
        response_id = rpc_prompt_id(stdin)
        user_message = {
            "role": "user",
            "content": [{"type": "text", "text": canonical(task)}],
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
        emit(
            [
                {"id": response_id, "type": "response", "command": "prompt", "success": True},
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
            ]
        )
        if sys.stdin.buffer.read() != b"":
            print("adapter probe received bytes after its RPC prompt", file=sys.stderr)
            return 92
    else:
        response_id = rpc_prompt_id(stdin)
        user_message = {"role": "user", "content": task}
        assistant_message = {
            "role": "assistant",
            "content": [{"type": "text", "text": assistant_text}],
        }
        emit(
            [
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
                    "type": "extension_ui_request",
                    "id": "widget-request-turn",
                    "method": "setWidget",
                    "widgetKey": "autoresearch",
                    "widgetLines": ["status", "running"],
                    "widgetPlacement": "belowEditor",
                },
                {
                    "type": "agent_end",
                    "messages": [user_message, assistant_message],
                    "telemetry": {
                        "chats": {"total": 1, "byStopReason": {"stop": 1}, "totalLatencyMs": 12.5},
                        "tools": {
                            "total": 0,
                            "ok": 0,
                            "error": 0,
                            "skipped": 0,
                            "blocked": 0,
                            "timeout": 0,
                            "aborted": 0,
                            "totalLatencyMs": 0,
                            "byName": {},
                        },
                        "usage": {
                            "inputTokens": 4,
                            "outputTokens": 2,
                            "cachedInputTokens": 0,
                            "cacheWriteTokens": 0,
                            "reasoningOutputTokens": 0,
                            "totalTokens": 6,
                        },
                        "cost": {"estimatedUsd": 0, "unavailableReasons": ["fixture-provider"]},
                        "errors": {"total": 0, "byType": {}},
                        "stepCount": 1,
                    },
                    "coverage": {
                        "toolsAvailable": [],
                        "toolsInvoked": [],
                        "toolsUnused": [],
                        "modelsUsed": ["fixture-model"],
                        "providersUsed": ["openrouter"],
                    },
                    "isTerminal": True,
                },
                {
                    "type": "extension_ui_request",
                    "id": "widget-request-settled",
                    "method": "setWidget",
                    "widgetKey": "autoresearch",
                    "widgetLines": [],
                    "widgetPlacement": "aboveEditor",
                },
            ]
        )
        if sys.stdin.buffer.read() != b"":
            print("adapter probe received bytes after its RPC prompt", file=sys.stderr)
            return 92
        emit([{"id": response_id, "type": "response", "command": "prompt", "success": True}])

    observation_path = os.environ.get("OPENPROSE_ADAPTER_OBSERVATION_PATH")
    if observation_path:
        Path(observation_path).write_text(canonical(observation) + "\n", encoding="utf-8")
    return 0


def stdin_eof_is_ready() -> bool:
    if os.name == "nt":
        return False
    readable, _, _ = select.select([sys.stdin.buffer], [], [], 0)
    if not readable:
        return False
    extra = os.read(sys.stdin.buffer.fileno(), 1)
    if extra:
        raise ValueError("adapter probe received more than one RPC request")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
