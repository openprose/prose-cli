#!/usr/bin/env python3
"""Generate adapter wire/scenario evidence from shared image/task authorities."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ADAPTERS = Path(__file__).resolve().parent
SHARED = ADAPTERS.parents[1]
CLI = SHARED.parent
ROOT = CLI.parent
IMAGE = SHARED / "image" / "echo-v0"
TASK = SHARED / "fixtures" / "transport" / "sentinel-task.json"
FRAMING = IMAGE / "contracts" / "one-field-framing.txt"
SCENARIOS = ADAPTERS / "scenarios"
WIRE = ADAPTERS / "wire"


def load_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def canonical_json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expected_outputs() -> dict[Path, bytes]:
    manifest = load_json(IMAGE / "manifest.json")
    image = b"".join(
        (IMAGE / entry["path"]).read_bytes() for entry in manifest["payload"]
    )
    declared = manifest["modelVisibleBytes"]
    if declared != {
        "serialization": "ordered-raw-concatenation-v1",
        "byteLength": len(image),
        "sha256": sha256(image),
    }:
        raise ValueError(
            "manifest modelVisibleBytes does not match ordered payload bytes"
        )

    task = canonical_json(load_json(TASK))
    task_value = json.loads(task)
    terminal = {
        "schema": "openprose.echo-terminal/1",
        "semanticStatus": "not-applicable",
        "placeholder": True,
        "marker": "OPENPROSE_ECHO_TERMINAL_V0",
        "task": {"argv": task_value["argv"]},
    }
    terminal_text = json.dumps(terminal, ensure_ascii=False, separators=(",", ":"))
    assistant_text = (
        "Echoed task argv: "
        + canonical_json(task_value["argv"]).decode("utf-8")
        + "\n"
        + terminal_text
    )
    framed = (
        FRAMING.read_text("utf-8")
        .replace("{{IMAGE_SHA256}}", sha256(image))
        .replace("{{IMAGE_BYTES}}", image.decode("utf-8"))
        .replace("{{TASK_SHA256}}", sha256(task))
        .replace("{{TASK_JSON}}", task.decode("utf-8"))
        .encode("utf-8")
    )
    prime = (
        canonical_json(
            {
                "id": "fixture-invocation-0001",
                "type": "prompt",
                "message": task.decode("utf-8"),
            }
        )
        + b"\n"
    )
    omp_state_id = "fixture-invocation-0001.omp.state.1"
    omp_prompt_id = "fixture-invocation-0001.omp.prompt.1"
    omp_state = canonical_json({"id": omp_state_id, "type": "get_state"}) + b"\n"
    omp_prompt = (
        canonical_json(
            {
                "id": omp_prompt_id,
                "type": "prompt",
                "message": task.decode("utf-8"),
            }
        )
        + b"\n"
    )
    omp = omp_state + omp_prompt
    omp_overlay = (
        b"retry:\n"
        b"  enabled: false\n"
        b"disabledProviders:\n"
        b"  - native\n"
        b"  - omp-plugins\n"
        b"  - claude\n"
        b"  - agent-plugins\n"
        b"  - claude-plugins\n"
        b"  - codex\n"
        b"  - gemini\n"
        b"  - opencode\n"
        b"  - cursor\n"
        b"  - windsurf\n"
        b"  - vscode\n"
        b"  - mcp-json\n"
    )

    outputs = {
        WIRE / "codex-stdin.txt": framed,
        WIRE / "prime-stdin.jsonl": prime,
        WIRE / "omp-stdin.jsonl": omp,
    }
    for path in sorted(SCENARIOS.glob("*.json")):
        scenario = load_json(path)
        adapter_id = scenario["adapterId"]
        scenario["stdinLifecycle"] = (
            "close-after-terminal-event"
            if adapter_id in {"prime/rpc", "omp/rpc"}
            else "close-after-write"
        )
        if adapter_id == "codex/exec-json":
            scenario["stdin"].update(
                byteLength=len(framed),
                sha256=sha256(framed),
                decodedImageSha256=sha256(image),
            )
            scenario["fakeStdout"] = [
                {"type": "thread.started", "thread_id": "fake-thread-0001"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "agent_message",
                        "text": assistant_text,
                    },
                },
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
        elif adapter_id == "prime/rpc":
            scenario["stdin"].update(
                byteLength=len(prime),
                sha256=sha256(prime),
                decodedMessageSha256=sha256(task),
            )
            scenario["stdin"].pop("decodedImageSha256", None)
            scenario["inlineImage"] = {
                "argument": "{{IMAGE_UTF8}}",
                "source": "cli/shared/image/echo-v0/manifest.json#modelVisibleBytes",
                "byteLength": len(image),
                "sha256": sha256(image),
            }
            scenario["fakeStdout"] = _rpc_stdout(assistant_text, task_value)
        elif adapter_id == "agents-sdk/jsonl":
            scenario["files"] = [{
                "argument": "{{IMAGE_PATH}}",
                "source": "cli/shared/image/echo-v0/manifest.json#modelVisibleBytes",
                "mode": "0600", "byteLength": len(image), "sha256": sha256(image),
            }]
            sdk_observation = load_json(ADAPTERS / "sdk-production.json")["observation"]
            scenario["fakeStdout"] = [
                {"type": "start", "model": "fixture", "cwd": "fixture"},
                {"type": "final", "output": assistant_text,
                 "usageObservation": sdk_observation["completedUsage"],
                 "modelIdentity": sdk_observation["modelIdentity"]},
            ]
        elif adapter_id in {"claude/print-stream-json", "omp/rpc"}:
            scenario["files"] = [
                {
                    "argument": "{{IMAGE_PATH}}",
                    "source": "cli/shared/image/echo-v0/manifest.json#modelVisibleBytes",
                    "mode": "0600",
                    "byteLength": len(image),
                    "sha256": sha256(image),
                }
            ]
            if adapter_id == "omp/rpc":
                scenario["files"].append(
                    {
                        "argument": "{{RENDERED_CONFIG_PATH}}",
                        "source": "cli/shared/capabilities/adapters/recipes/omp-rpc.v1.json#launch.controls.ownedConfigOverlay.bytes",
                        "mode": "0600",
                        "byteLength": len(omp_overlay),
                        "sha256": sha256(omp_overlay),
                    }
                )
                scenario["stdin"].update(
                    byteLength=len(omp),
                    sha256=sha256(omp),
                    decodedMessageSha256=sha256(task),
                    stages=[
                        {
                            "after": "available_commands_update",
                            "requestId": omp_state_id,
                            "byteLength": len(omp_state),
                            "sha256": sha256(omp_state),
                        },
                        {
                            "after": "get_state-response-empty-dumpTools",
                            "requestId": omp_prompt_id,
                            "byteLength": len(omp_prompt),
                            "sha256": sha256(omp_prompt),
                        },
                    ],
                )
                scenario["fakeStdout"] = _omp_stdout(
                    assistant_text, task_value, omp_state_id, omp_prompt_id
                )
            else:
                scenario["fakeStdout"] = [
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
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": assistant_text}],
                        },
                    },
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": "fake-session-0001",
                        "is_error": False,
                        "result": assistant_text,
                    },
                ]
        scenario["expectedLanguageTerminal"] = terminal
        # Preserve the reviewed SDK scenario's ASCII-escaped byte serialization.
        # Other adapters retain their existing UTF-8 serialization.
        outputs[path] = (
            json.dumps(scenario, ensure_ascii=(adapter_id == "agents-sdk/jsonl"), indent=2).encode("utf-8") + b"\n"
        )
    return outputs


def _rpc_stdout(
    assistant_text: str, task: dict[str, object]
) -> list[dict[str, object]]:
    user_message = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": json.dumps(task, ensure_ascii=False, separators=(",", ":")),
            }
        ],
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
        content: list[dict[str, object]], *, response_id: bool = False
    ) -> dict[str, object]:
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
    thinking_started_block = {"type": "thinking", "thinking": ""}
    thinking = {
        "type": "thinking",
        "thinking": thinking_text,
        "thinkingSignature": "fixture-thinking-signature",
    }
    thinking_started = assistant_message([thinking_started_block], response_id=True)
    thinking_ended = assistant_message([thinking], response_id=True)
    thinking_break_one = len(thinking_text) // 3
    thinking_break_two = (len(thinking_text) * 2) // 3
    thinking_deltas = [
        thinking_text[:thinking_break_one],
        thinking_text[thinking_break_one:thinking_break_two],
        thinking_text[thinking_break_two:] + "\n\n",
    ]
    if any(not delta for delta in thinking_deltas):
        raise ValueError("Prime fixture requires three nonempty thinking deltas")
    streamed_thinking = ""
    thinking_updates = []
    for delta in thinking_deltas:
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
    text_deltas = [
        assistant_text[:first_break],
        assistant_text[first_break:second_break],
        assistant_text[second_break:],
    ]
    if any(not delta for delta in text_deltas):
        raise ValueError("Prime fixture requires three nonempty text deltas")
    streamed_text = ""
    text_updates = []
    for delta in text_deltas:
        streamed_text += delta
        message = assistant_message(
            [thinking, {"type": "text", "text": streamed_text}], response_id=True
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
    return [
        {
            "id": "fixture-invocation-0001",
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
        {
            "type": "message_end",
            "message": text_complete,
        },
        {
            "type": "turn_end",
            "message": text_complete,
            "toolResults": [],
        },
        {"type": "agent_end", "messages": [user_message, text_complete]},
    ]


def _omp_stdout(
    assistant_text: str,
    task: dict[str, object],
    state_request_id: str,
    prompt_request_id: str,
) -> list[dict[str, object]]:
    user_message = {"role": "user", "content": task}
    assistant_message = {
        "role": "assistant",
        "content": [{"type": "text", "text": assistant_text}],
    }
    return [
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
        {
            "id": state_request_id,
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"dumpTools": []},
        },
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
            "telemetry": _omp_telemetry(),
            "coverage": _omp_coverage(),
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
        {
            "id": prompt_request_id,
            "type": "response",
            "command": "prompt",
            "success": True,
        },
    ]


def _omp_telemetry() -> dict[str, object]:
    return {
        "chats": {
            "total": 1,
            "byStopReason": {"stop": 1},
            "totalLatencyMs": 12.5,
        },
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
        "cost": {
            "estimatedUsd": 0,
            "unavailableReasons": ["fixture-provider"],
        },
        "errors": {"total": 0, "byType": {}},
        "stepCount": 1,
    }


def _omp_coverage() -> dict[str, object]:
    return {
        "toolsAvailable": [],
        "toolsInvoked": [],
        "toolsUnused": [],
        "modelsUsed": ["fixture-model"],
        "providersUsed": ["openrouter"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    drift = []
    for path, expected in expected_outputs().items():
        actual = path.read_bytes() if path.is_file() else None
        if actual == expected:
            continue
        if args.write:
            path.write_bytes(expected)
        else:
            drift.append(str(path.relative_to(ROOT)))
    if drift:
        print("adapter fixture drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
