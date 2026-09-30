"""The canonical JSON line both products print on stdout.

Compact separators, object keys sorted recursively, UTF-8 without ASCII
escaping and one trailing newline per document: what Rust's serde_json
emits for a `serde_json::Value` (a BTreeMap) and what Bun's
`output.canonicalJson` emits. Runners compare stdout bytes against this, so
a port that drifts to insertion order fails even though both products'
documents parse to equal values.
"""
from __future__ import annotations

import json


def canonical_line(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def canonicality_problem(stdout: bytes) -> str | None:
    """None when stdout is exactly one canonical JSON line per document."""
    lines = stdout.split(b"\n")
    if lines[-1] != b"":
        return "stdout must end with a newline"
    for line in lines[:-1]:
        try:
            value = json.loads(line)
        except ValueError:
            return f"stdout line is not JSON: {line[:120]!r}"
        if line + b"\n" != canonical_line(value):
            return f"stdout JSON is not canonical (compact, keys sorted recursively): {line[:160]!r}"
    return None
