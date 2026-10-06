#!/usr/bin/env python3
"""Explicit provider-free native Codex option observation, never model qualification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import importlib.util
import sys

CLI = Path(__file__).resolve().parents[2]
CONTRACT = CLI / "shared/capabilities/adapters/codex-compatibility.v1.json"
SPEC = importlib.util.spec_from_file_location("codex_observation_process_host", CLI / "conformance/runner/run.py")
PROCESS_HOST = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROCESS_HOST
SPEC.loader.exec_module(PROCESS_HOST)
run_owned_process = PROCESS_HOST.run_owned_process


def bounded_probe(argv: list[str], environment: dict[str, str], limit: int):
    result = run_owned_process(argv, cwd=CLI, environment=environment,
                               timeout_seconds=5, max_capture_bytes=limit)
    if result.stdout_truncated or result.stderr_truncated:
        raise ValueError("Native probe output exceeds the observation bound")
    if result.exit_code != 0 or result.timed_out or not result.settled:
        raise ValueError("Native probe failed or its process boundary did not settle")
    return result


def observe(executable: Path) -> dict:
    executable = executable.resolve(strict=True)
    contract = json.loads(CONTRACT.read_text("utf-8"))
    environment = {name: os.environ[name] for name in ("PATH", "SystemRoot", "WINDIR") if name in os.environ}
    version = bounded_probe([str(executable), "--version"], environment, contract["maxOutputBytes"]).stdout.decode("utf-8").strip()
    if not re.fullmatch(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Native version identity is malformed")
    help_result = bounded_probe([str(executable), *contract["probeArgv"]], environment, contract["maxOutputBytes"])
    if max(len(help_result.stdout), len(help_result.stderr)) > contract["maxOutputBytes"]:
        raise ValueError("Native help output exceeds the retained observation bound")
    text = help_result.stdout.decode("utf-8")
    options = set(re.findall(r"^\s*(?:-[A-Za-z],\s*)?(--[a-z][a-z0-9-]*)(?=\s|=|$)", text, re.M))
    missing = [option for option in contract["requiredExecOptions"] if option not in options]
    return {
        "schema": "openprose.codex-native-options-observation/1",
        "nativeVersion": version,
        "nativeExecutableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "compatibilityContractSha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        "nativeHelpSha256": hashlib.sha256(help_result.stdout).hexdigest(),
        "requiredOptions": contract["requiredExecOptions"],
        "missingOptions": missing,
        "providerCallsAttempted": 0,
        "qualification": "unqualified",
        "limitations": ["Native advertised options only; no exec-json event stream, model, tool, permission enforcement, developer/base instruction config semantics, cancellation or semantic contract qualification."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    args = parser.parse_args()
    result = observe(args.codex)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if not result["missingOptions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
