#!/usr/bin/env python3
"""Deterministic structural guards for the outer-runner/language boundary.

This checker proves only the declared source/dependency shape. It is not a
semantic parser, runtime monitor, or general security authority.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Iterator


SOURCE_SUFFIXES = frozenset({".rs", ".ts", ".tsx", ".js", ".mjs", ".cjs"})
IGNORED_PARTS = frozenset({"node_modules", "target", "dist", "__pycache__"})
PRODUCT_ROOTS = (
    Path("cli/rust/crates"),
    Path("cli/bun/src"),
    Path("cli/bun/scripts"),
    Path("cli/bun/npm"),
)

ALLOWED_BUN_MODULES = frozenset(
    {
        "ajv/dist/2020",
        "node:crypto",
        "node:fs",
        "node:fs/promises",
        "node:os",
        "node:path",
    }
)
ALLOWED_BUN_SOURCE_MODULES = frozenset(
    {
        ("cli/bun/src/adapters/prime-owned-service.ts", "node:net"),
        ("cli/bun/src/adapters/native-tool-lifecycle.ts", "node:util"),
        ("cli/bun/src/adapters/prime-drain.ts", "node:util"),
        ("cli/bun/src/adapters/protocols.ts", "node:util"),
    }
)
NPM_LAUNCHER = "cli/bun/npm/bin/prose.js"
ALLOWED_BUN_SCRIPTS = {
    "test": "bun --no-env-file --config=./config/empty-bunfig.toml test ./test",
    "typecheck": "tsc --noEmit -p tsconfig.json",
    "image:check": "bun --no-env-file --config=./config/empty-bunfig.toml run ./scripts/image-bundle.ts check",
    "build": "bun --no-env-file --config=./config/empty-bunfig.toml run ./scripts/image-bundle.ts build",
    "build:test": "bun --no-env-file --config=./config/empty-bunfig.toml run ./scripts/image-bundle.ts build-test",
    "build:release": "bun --no-env-file --config=./config/empty-bunfig.toml run ./scripts/image-bundle.ts build --require-release-eligible",
    "check": "bun run typecheck && bun run test && bun run build",
}
ALLOWED_BUN_DEPENDENCIES = {
    "dependencies": {"ajv": "8.20.0"},
    "devDependencies": {
        "@types/bun": "1.3.5",
        "ajv-formats": "3.0.1",
        "typescript": "5.9.3",
    },
    "optionalDependencies": {},
    "peerDependencies": {},
}
ALLOWED_RUST_EXTERNAL_DEPENDENCIES = frozenset(
    {
        "chrono",
        "rustix",
        "serde",
        "serde_json",
        "sha2",
        "signal-hook",
        "tempfile",
        "toml",
        "uuid",
    }
)
ALLOWED_RUST_LOCAL_DEPENDENCIES = {
    "prose-process-supervisor": Path("cli/rust/crates/prose-process-supervisor"),
    "prose-runner-core": Path("cli/rust/crates/prose-runner-core"),
}
ALLOWED_RUST_CRATES = frozenset(
    {"prose-cli", "prose-process-supervisor", "prose-runner-core"}
)
ALLOWED_SHARED_REFERENCE_FILES = frozenset(
    {
        "cli/conformance/cases/fixtures/runner-help.txt",
        "cli/shared/capabilities/adapters/codex-env-route.v1.json",
        "cli/shared/fixtures/adapters/claude-background-tasks.json",
        "cli/shared/fixtures/adapters/claude-native-turns.json",
        "cli/shared/fixtures/adapters/claude-shutdown.json",
        "cli/shared/fixtures/adapters/claude-task-lifecycle.json",
        "cli/shared/fixtures/adapters/claude-thinking-tokens.json",
        "cli/shared/fixtures/adapters/native-output.v1.json",
        "cli/shared/fixtures/adapters/native-profile.json",
        "cli/shared/fixtures/adapters/sdk-native-limits.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/omp-custom.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/omp-late-progress.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/omp-task-defaults.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/omp.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/prime-child-telemetry.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/prime-drain.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/prime-implicit-turn.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/prime-queue-telemetry.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/prime-turn-transition.json",
        "cli/shared/fixtures/adapters/tool-lifecycle/prime.json",
        "cli/shared/fixtures/config/optional-reporting.json",
        "cli/shared/fixtures/registry/hash-vectors.json",
        "cli/shared/fixtures/registry/single-file.json",
        "cli/shared/fixtures/registry/single-file.canonical.json",
        "cli/shared/fixtures/registry/single-file.receipt.json",
        "cli/shared/fixtures/registry/directory.json",
        "cli/shared/fixtures/registry/directory.canonical.json",
        "cli/shared/fixtures/registry/directory.receipt.json",
        "cli/shared/fixtures/kernel-startup/release.json",
        "cli/shared/fixtures/native-output-budget.json",
        "cli/shared/fixtures/transport-diagnostics.json",
        "cli/conformance/fake-harness/fake_harness.py",
        "cli/shared/capabilities/transport-limits.v1.json",
        "cli/shared/capabilities/adapters/oracle.v1.json",
        "cli/shared/errors/taxonomy.v1.json",
        "cli/shared/fixtures/adapters/bin/adapter_probe.py",
        "cli/shared/fixtures/config/flat-toml-v1.json",
        "cli/shared/fixtures/human/human-safe-scalars.json",
        "cli/shared/fixtures/transport/deterministic-mock-adapter.json",
        "cli/shared/fixtures/transport/mock-adapter.json",
    }
)
ALLOWED_SHARED_REFERENCE_PREFIXES = (
    "cli/shared/capabilities/adapters/recipes/",
    "cli/shared/fixtures/adapters/scenarios/",
    "cli/shared/fixtures/adapters/wire/",
    "cli/shared/fixtures/operations/",
    "cli/shared/image/",
)
ALLOWED_MARKDOWN_REFERENCES = frozenset(
    {
        "cli/shared/image/sentinel-v1/payload/00-sentinel.md",
        "cli/shared/image/sentinel-v1/payload/10-byte-canary.md",
    }
)
LANGUAGE_LITERAL_TERMS = {
    "Contract Markdown": "language-owned Contract Markdown",
    "ProseScript": "language-owned ProseScript",
    "Forme": "language-owned Forme",
}
SKILL_LITERAL_TERMS = ("SKILL.md", ".claude/skills", ".agents/skills")

TS_FROM_IMPORT = re.compile(
    r"\b(?:import|export)\s+(?:type\s+)?[^;\n]*?\bfrom\s*([\"'])([^\"']+)\1"
)
TS_SIDE_EFFECT_IMPORT = re.compile(r"\bimport\s*([\"'])([^\"']+)\1")
TS_LITERAL_CALL = re.compile(r"\b(?:import|require)\s*\(\s*([\"'])([^\"']+)\1\s*\)")
TS_SOURCE_CALL = re.compile(r"\b(?:import|require)\s*\(")
TS_URL_REFERENCE = re.compile(
    r"\bnew\s+URL\s*\(\s*([\"'])([^\"']+)\1\s*,\s*import\.meta\.url\s*\)"
)
TS_REFERENCE_DIRECTIVE = re.compile(
    r"(?m)^\s*///\s*<reference\s+(path|types)\s*=\s*([\"'])(.*?)\2(?:\s*/?)?>"
)
RUST_PATH_ATTRIBUTE = re.compile(r"#\s*\[\s*path\s*=\s*\"([^\"]+)\"\s*\]")
RUST_PATH_CONSTRUCTOR = re.compile(
    r"(?:\.join|PathBuf::from|Path::new)\s*\(\s*\"([^\"]+)\"\s*\)"
)
RUST_LITERAL_FILE_REFERENCE = re.compile(
    r"\b(?:(?:std::)?fs::(?:read|read_to_string|read_dir|copy|metadata|symlink_metadata|canonicalize)|File::open|OpenOptions::open)"
    r"\s*\(\s*\"([^\"]+)\""
)
RUST_COMMAND_IMPORT = re.compile(
    r"\buse\s+std::process::Command(?:\s+as\s+([A-Za-z_][A-Za-z0-9_]*))?"
)
SHELL_EXECUTABLE = (
    r"(?:sh|bash|zsh|dash|ksh|fish|cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh(?:\.exe)?)"
)


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    rule: str
    detail: str

    def render(self) -> str:
        return f"{self.path}: {self.rule}: {self.detail}"


@dataclass(frozen=True)
class TomlAssignment:
    table: str
    key: str
    value: str
    line: int


class DuplicateJsonKey(ValueError):
    pass


def _files(root: Path, relative_root: Path, suffixes: Iterable[str]) -> Iterator[Path]:
    base = root / relative_root
    if not base.exists():
        return
    accepted = frozenset(suffixes)
    for path in sorted(base.rglob("*")):
        if set(path.parts) & IGNORED_PARTS:
            continue
        if path.is_file() and path.suffix in accepted:
            yield path


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _rust_raw_literal_end(text: str, opening: int) -> int | None:
    """Return the exclusive end of a Rust raw string/byte/C-string literal."""
    cursor = opening
    if text.startswith(("br", "cr"), cursor):
        cursor += 2
    elif cursor < len(text) and text[cursor] == "r":
        cursor += 1
    else:
        return None
    hashes = cursor
    while cursor < len(text) and text[cursor] == "#":
        cursor += 1
    if cursor >= len(text) or text[cursor] != '"':
        return None
    terminator = '"' + text[hashes:cursor]
    closing = text.find(terminator, cursor + 1)
    return None if closing < 0 else closing + len(terminator)


def _without_comments(text: str, *, single_quoted_strings: bool) -> str:
    result: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    block_depth = 0
    quotes = {'"', "`"} | ({"'"} if single_quoted_strings else set())
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if block_depth:
            if char == "/" and following == "*":
                block_depth += 1
                result.extend("  ")
                index += 2
            elif char == "*" and following == "/":
                block_depth -= 1
                result.extend("  ")
                index += 2
            else:
                result.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if quote is not None:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if not single_quoted_strings and char in {"b", "c", "r"}:
            raw_end = _rust_raw_literal_end(text, index)
            if raw_end is not None:
                result.extend(text[index:raw_end])
                index = raw_end
                continue
        if char == "/" and following == "/":
            while index < len(text) and text[index] != "\n":
                result.append(" ")
                index += 1
            continue
        if char == "/" and following == "*":
            block_depth = 1
            result.extend("  ")
            index += 2
            continue
        if char in quotes:
            quote = char
        result.append(char)
        index += 1
    return "".join(result)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, root: Path) -> tuple[Any | None, list[Violation]]:
    try:
        return json.loads(path.read_text("utf-8"), object_pairs_hook=_json_object), []
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey) as error:
        return None, [Violation(_relative(path, root), "manifest-invalid", str(error))]


def _strip_toml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    result: list[str] = []
    for char in line:
        if quote is not None:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
            result.append(char)
        elif char == "#":
            break
        else:
            result.append(char)
    return "".join(result)


def _balanced_toml(value: str) -> bool:
    square = curly = 0
    quote: str | None = None
    escaped = False
    for char in value:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
    return quote is None and square == 0 and curly == 0


def _toml_assignments(text: str) -> list[TomlAssignment]:
    result: list[TomlAssignment] = []
    table = ""
    pending = ""
    pending_line = 0
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if not pending and line.startswith("[") and line.endswith("]"):
            table = line.strip("[]").strip().strip('"').strip("'")
            continue
        if not pending:
            pending = line
            pending_line = number
        else:
            pending += " " + line
        if not _balanced_toml(pending):
            continue
        if "=" in pending:
            key, value = pending.split("=", 1)
            result.append(
                TomlAssignment(
                    table,
                    key.strip().strip('"').strip("'"),
                    value.strip(),
                    pending_line,
                )
            )
        pending = ""
    return result


def _quoted_values(value: str) -> list[str]:
    return [match.group(2) for match in re.finditer(r'(["\'])(.*?)\1', value)]


def _inline_value(value: str, key: str) -> str | None:
    match = re.search(rf"(?:^|[,{{])\s*{re.escape(key)}\s*=\s*([\"'])(.*?)\1", value)
    return None if match is None else match.group(2)


def _cargo_dependency_table(table: str) -> bool:
    return bool(
        re.search(
            r"(?:^|\.)(?:dependencies|dev-dependencies|build-dependencies)$", table
        )
    )


def _cargo_crate_root(manifest: Path, root: Path) -> Path | None:
    crates = (root / "cli/rust/crates").resolve()
    resolved = manifest.resolve()
    if not _inside(resolved, crates):
        return None
    relative = resolved.relative_to(crates)
    return crates / relative.parts[0] if relative.parts else None


def _allowed_shared_reference(relative: str, target: Path) -> bool:
    if target.suffix.lower() == ".md" and relative not in ALLOWED_MARKDOWN_REFERENCES:
        return False
    return relative in ALLOWED_SHARED_REFERENCE_FILES or any(
        relative == prefix.rstrip("/") or relative.startswith(prefix)
        for prefix in ALLOWED_SHARED_REFERENCE_PREFIXES
    )


def _allowed_source_reference(source: Path, target: Path, root: Path) -> bool:
    resolved_root = root.resolve()
    if not _inside(target, resolved_root):
        return False
    relative = _relative(target, resolved_root)
    source_relative = _relative(source.resolve(), resolved_root)
    if source_relative.startswith("cli/rust/crates/"):
        crate = _cargo_crate_root(source, root)
        if crate is not None and _inside(target, crate):
            return True
    elif source_relative.startswith("cli/bun/"):
        bun = (root / "cli/bun").resolve()
        if _inside(target, bun) and not (
            set(target.relative_to(bun).parts) & {"test", "node_modules", "dist"}
        ):
            return True
    return _allowed_shared_reference(relative, target)


def _reference_violation(source: Path, raw: str, root: Path) -> Violation | None:
    target = (source.parent / raw).resolve()
    if _allowed_source_reference(source, target, root):
        return None
    return Violation(
        _relative(source, root),
        "source-import-escape",
        f"source reference is outside the explicit product/shared allowlist: {raw}",
    )


def _check_package_json(root: Path, manifest: Path) -> list[Violation]:
    package, violations = _load_json(manifest, root)
    if package is None:
        return violations
    relative = _relative(manifest, root)
    if not isinstance(package, dict):
        return [
            Violation(relative, "manifest-invalid", "package.json must be an object")
        ]
    for group, allowed in ALLOWED_BUN_DEPENDENCIES.items():
        entries = package.get(group, {})
        if not isinstance(entries, dict):
            violations.append(
                Violation(relative, "manifest-invalid", f"{group} must be an object")
            )
            continue
        for name, value in entries.items():
            if not isinstance(value, str):
                violations.append(
                    Violation(
                        relative, "manifest-invalid", f"{group}.{name} must be a string"
                    )
                )
                continue
            prefix = next(
                (item for item in ("file:", "link:") if value.startswith(item)), None
            )
            if prefix is not None:
                target = (manifest.parent / value[len(prefix) :]).resolve()
                if not _inside(target, (root / "cli/bun").resolve()):
                    violations.append(
                        Violation(
                            relative,
                            "dependency-escape",
                            f"{group}.{name} leaves cli/bun: {value}",
                        )
                    )
                elif name not in allowed:
                    violations.append(
                        Violation(
                            relative,
                            "dependency-not-allowed",
                            f"{group}.{name} is not explicitly allowed",
                        )
                    )
            elif name not in allowed:
                violations.append(
                    Violation(
                        relative,
                        "dependency-not-allowed",
                        f"{group}.{name} is not explicitly allowed",
                    )
                )
            elif value != allowed[name]:
                violations.append(
                    Violation(
                        relative,
                        "dependency-not-allowed",
                        f"{group}.{name} differs from its exact admitted version",
                    )
                )
        if package.get("name") == "@openprose/prose-cli-bun":
            for name in allowed.keys() - entries.keys():
                violations.append(
                    Violation(
                        relative,
                        "dependency-not-allowed",
                        f"required {group}.{name} is missing",
                    )
                )
    for group in (
        "bundledDependencies",
        "bundleDependencies",
        "overrides",
        "resolutions",
    ):
        if group in package:
            violations.append(
                Violation(
                    relative,
                    "dependency-not-allowed",
                    f"{group} is not an admitted dependency declaration",
                )
            )
    for alias_key in ("imports", "workspaces"):
        if alias_key in package:
            violations.append(
                Violation(
                    relative,
                    "source-alias",
                    f"package {alias_key} aliases are not admitted",
                )
            )
    scripts = package.get("scripts")
    if scripts is not None:
        if not isinstance(scripts, dict):
            violations.append(
                Violation(relative, "manifest-invalid", "scripts must be an object")
            )
        else:
            for name, value in scripts.items():
                if ALLOWED_BUN_SCRIPTS.get(name) != value:
                    violations.append(
                        Violation(
                            relative,
                            "build-script-declaration",
                            f"scripts.{name} differs from the explicit build allowlist",
                        )
                    )
            if package.get("name") == "@openprose/prose-cli-bun":
                for name in ALLOWED_BUN_SCRIPTS.keys() - scripts.keys():
                    violations.append(
                        Violation(
                            relative,
                            "build-script-declaration",
                            f"required scripts.{name} is missing",
                        )
                    )
    for key in ("main", "module", "browser", "bin", "exports", "files"):
        if key not in package:
            continue
        pending: list[Any] = [package[key]]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                target = (manifest.parent / value).resolve()
                if not value.startswith(".") or not _inside(
                    target, (root / "cli/bun").resolve()
                ):
                    violations.append(
                        Violation(
                            relative,
                            "source-declaration-escape",
                            f"package {key} entry leaves cli/bun: {value}",
                        )
                    )
    return violations


def _check_tsconfig(root: Path, path: Path) -> list[Violation]:
    document, violations = _load_json(path, root)
    if document is None:
        return violations
    relative = _relative(path, root)
    if not isinstance(document, dict):
        return [Violation(relative, "manifest-invalid", "tsconfig must be an object")]
    compiler = document.get("compilerOptions", {})
    if not isinstance(compiler, dict):
        return [
            Violation(relative, "manifest-invalid", "compilerOptions must be an object")
        ]
    for key in ("baseUrl", "paths", "rootDirs", "typeRoots", "plugins"):
        if key in compiler:
            violations.append(
                Violation(relative, "source-alias", f"tsconfig {key} is not admitted")
            )
    if compiler.get("types") != ["bun"]:
        violations.append(
            Violation(
                relative,
                "source-alias",
                "tsconfig types must be the exact admitted Bun runtime declaration",
            )
        )
    for key in ("include", "exclude", "files"):
        values = document.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            violations.append(
                Violation(
                    relative, "manifest-invalid", f"tsconfig {key} must be strings"
                )
            )
            continue
        for value in values:
            target = (path.parent / value).resolve()
            if not _inside(target, (root / "cli/bun").resolve()):
                violations.append(
                    Violation(
                        relative,
                        "source-declaration-escape",
                        f"tsconfig {key} entry leaves cli/bun: {value}",
                    )
                )
    for key in ("extends", "references"):
        if key in document:
            violations.append(
                Violation(relative, "source-alias", f"tsconfig {key} is not admitted")
            )
    return violations


def _check_cargo_manifest(root: Path, manifest: Path) -> list[Violation]:
    relative = _relative(manifest, root)
    try:
        assignments = _toml_assignments(manifest.read_text("utf-8"))
    except UnicodeDecodeError:
        return [Violation(relative, "manifest-invalid", "Cargo manifest is not UTF-8")]
    violations: list[Violation] = []
    rust = (root / "cli/rust").resolve()
    crates = (root / "cli/rust/crates").resolve()
    crate_root = _cargo_crate_root(manifest, root)
    if crate_root is not None and crate_root.name not in ALLOWED_RUST_CRATES:
        violations.append(
            Violation(
                relative,
                "dependency-not-allowed",
                f"Rust crate is not explicitly allowed: {crate_root.name}",
            )
        )
    for assignment in assignments:
        if assignment.table == "replace" or assignment.table.startswith("patch."):
            violations.append(
                Violation(
                    relative,
                    "dependency-source",
                    f"Cargo {assignment.table} source override is not admitted: {assignment.key}",
                )
            )
            continue
        if assignment.table == "workspace" and assignment.key in {
            "members",
            "default-members",
        }:
            for raw in _quoted_values(assignment.value):
                target = (manifest.parent / raw).resolve()
                admitted = (
                    _inside(target, crates)
                    and target.parent == crates
                    and target.name in ALLOWED_RUST_CRATES
                )
                if not admitted:
                    violations.append(
                        Violation(
                            relative,
                            "workspace-member-escape",
                            f"workspace member is outside the closed crate set: {raw}",
                        )
                    )
        if assignment.table == "workspace" and assignment.key == "exclude":
            violations.append(
                Violation(
                    relative,
                    "workspace-member-escape",
                    "workspace exclude is not admitted",
                )
            )
        if assignment.table in {
            "package",
            "lib",
            "bin",
            "test",
            "bench",
            "example",
        } and assignment.key in {"build", "path"}:
            values = _quoted_values(assignment.value)
            if len(values) != 1:
                violations.append(
                    Violation(
                        relative,
                        "manifest-invalid",
                        f"Cargo {assignment.table}.{assignment.key} must be one literal path",
                    )
                )
            else:
                target = (manifest.parent / values[0]).resolve()
                parent = crate_root or rust
                if not _inside(target, parent) or target.suffix != ".rs":
                    violations.append(
                        Violation(
                            relative,
                            "source-declaration-escape",
                            f"Cargo source declaration is not local Rust source: {values[0]}",
                        )
                    )
        if not _cargo_dependency_table(assignment.table):
            continue
        dependency_key = assignment.key.split(".", 1)[0]
        package_name = _inline_value(assignment.value, "package") or dependency_key
        path_value = _inline_value(assignment.value, "path")
        if (
            _inline_value(assignment.value, "git") is not None
            or _inline_value(assignment.value, "registry") is not None
        ):
            violations.append(
                Violation(
                    relative,
                    "dependency-source",
                    f"Cargo dependency {dependency_key} uses a non-default source",
                )
            )
        if path_value is not None:
            target = (manifest.parent / path_value).resolve()
            expected_relative = ALLOWED_RUST_LOCAL_DEPENDENCIES.get(package_name)
            expected = (
                (root / expected_relative).resolve()
                if expected_relative is not None
                else None
            )
            if not _inside(target, rust):
                violations.append(
                    Violation(
                        relative,
                        "dependency-escape",
                        f"Cargo path dependency leaves cli/rust: {path_value}",
                    )
                )
            elif (
                expected is None or target != expected or dependency_key != package_name
            ):
                violations.append(
                    Violation(
                        relative,
                        "dependency-not-allowed",
                        f"Cargo local dependency is not explicitly allowed: {dependency_key}",
                    )
                )
        elif (
            relative == "cli/rust/crates/prose-runner-core/Cargo.toml"
            and assignment.table == "dependencies"
            and assignment.key == "ureq"
            and re.fullmatch(
                r'\{\s*version\s*=\s*"=2\.12\.1"\s*,\s*'
                r'default-features\s*=\s*false\s*,\s*'
                r'features\s*=\s*\[\s*"tls"\s*\]\s*\}',
                assignment.value,
            )
        ):
            # Only the reviewed, pinned published-kernel acquisition dependency.
            pass
        elif package_name not in ALLOWED_RUST_EXTERNAL_DEPENDENCIES:
            violations.append(
                Violation(
                    relative,
                    "dependency-not-allowed",
                    f"Cargo dependency is not explicitly allowed: {package_name}",
                )
            )
        elif dependency_key != package_name:
            violations.append(
                Violation(
                    relative,
                    "dependency-not-allowed",
                    f"Cargo dependency aliases are not admitted: {dependency_key}",
                )
            )
    return violations


def _check_cargo_config(root: Path) -> list[Violation]:
    path = root / "cli/rust/.cargo/config.toml"
    if not path.exists():
        return []
    relative = _relative(path, root)
    violations: list[Violation] = []
    for assignment in _toml_assignments(path.read_text("utf-8")):
        if assignment.table == "build" and assignment.key == "rustc-wrapper":
            violations.append(
                Violation(
                    relative,
                    "build-script-declaration",
                    "rustc-wrapper is not admitted",
                )
            )
        if assignment.table.startswith("target.") and assignment.key == "runner":
            violations.append(
                Violation(
                    relative,
                    "build-script-declaration",
                    "Cargo target runner is not admitted",
                )
            )
        if assignment.table.startswith("source") or assignment.table == "patch":
            violations.append(
                Violation(
                    relative,
                    "dependency-source",
                    "Cargo source replacement is not admitted",
                )
            )
        if assignment.table == "env" and assignment.key != "OPENPROSE_BUILD_COMMIT":
            violations.append(
                Violation(
                    relative,
                    "build-script-declaration",
                    f"Cargo build environment entry is not admitted: {assignment.key}",
                )
            )
    return violations


def check_manifest_paths(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for manifest in _files(root, Path("cli/rust"), {".toml"}):
        if manifest.name == "Cargo.toml":
            violations.extend(_check_cargo_manifest(root, manifest))
    violations.extend(_check_cargo_config(root))
    for manifest in _files(root, Path("cli/bun"), {".json"}):
        if manifest.name == "package.json":
            violations.extend(_check_package_json(root, manifest))
        elif manifest.name.startswith("tsconfig"):
            violations.extend(_check_tsconfig(root, manifest))
    return violations


def _escaped_literal(code: str, opening: int, delimiter: str) -> tuple[str, int] | None:
    """Return one escaped literal body and its exclusive end in linear time."""
    cursor = opening + 1
    while cursor < len(code):
        character = code[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == delimiter:
            return code[opening + 1 : cursor], cursor + 1
        cursor += 1
    return None


def _typescript_string_literals(code: str) -> list[str]:
    values: list[str] = []
    cursor = 0
    delimiters = {'"', "'", "`"}
    while cursor < len(code):
        delimiter = code[cursor]
        if delimiter not in delimiters:
            cursor += 1
            continue
        literal = _escaped_literal(code, cursor, delimiter)
        if literal is None:
            break
        value, cursor = literal
        values.append(value)
    return values


def _rust_char_literal_end(code: str, opening: int) -> int | None:
    """Recognize a Rust character literal so embedded quotes stay masked."""
    body = opening + 1
    if body >= len(code) or code[body] in {"'", "\r", "\n"}:
        return None
    if code[body] != "\\":
        closing = body + 1
    elif body + 1 >= len(code):
        return None
    elif code[body + 1] == "x":
        closing = body + 4
    elif code[body + 1] == "u" and body + 2 < len(code) and code[body + 2] == "{":
        closing = body + 3
        digits = 0
        while closing < len(code) and code[closing] in "0123456789abcdefABCDEF_":
            digits += code[closing] != "_"
            closing += 1
        if digits == 0 or closing >= len(code) or code[closing] != "}":
            return None
        closing += 1
    else:
        closing = body + 2
    if closing < len(code) and code[closing] == "'":
        return closing + 1
    return None


def _rust_string_literals(code: str) -> list[str]:
    values: list[str] = []
    cursor = 0
    while cursor < len(code):
        character = code[cursor]
        if character == "'":
            char_end = _rust_char_literal_end(code, cursor)
            cursor = char_end if char_end is not None else cursor + 1
            continue
        if character == "r":
            hashes_end = cursor + 1
            while hashes_end < len(code) and code[hashes_end] == "#":
                hashes_end += 1
            hash_count = hashes_end - cursor - 1
            if hashes_end < len(code) and code[hashes_end] == '"':
                body = hashes_end + 1
                terminator = '"' + ("#" * hash_count)
                closing = code.find(terminator, body)
                if closing < 0:
                    break
                values.append(code[body:closing])
                cursor = closing + len(terminator)
                continue
        if character != '"':
            cursor += 1
            continue
        literal = _escaped_literal(code, cursor, '"')
        if literal is None:
            break
        value, cursor = literal
        values.append(value)
    return values


def _string_literals(code: str, *, rust: bool) -> list[str]:
    return _rust_string_literals(code) if rust else _typescript_string_literals(code)


def _check_language_literals(source: Path, code: str, root: Path) -> list[Violation]:
    violations: list[Violation] = []
    relative = _relative(source, root)
    literals = _string_literals(code, rust=source.suffix == ".rs")
    for value in literals:
        for needle, detail in LANGUAGE_LITERAL_TERMS.items():
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", value
            ):
                violations.append(Violation(relative, "language-boundary", detail))
        if any(term in value for term in SKILL_LITERAL_TERMS):
            violations.append(
                Violation(relative, "language-boundary", "skill-file discovery")
            )
    if ({".claude", "skills"} <= set(literals)) or (
        {".agents", "skills"} <= set(literals)
    ):
        violations.append(
            Violation(relative, "language-boundary", "ambient skill path construction")
        )
    return violations


def _ts_module_references(code: str) -> tuple[list[str], int]:
    references: list[str] = []
    literal_call_starts: set[int] = set()
    for pattern in (
        TS_FROM_IMPORT,
        TS_SIDE_EFFECT_IMPORT,
        TS_LITERAL_CALL,
        TS_URL_REFERENCE,
    ):
        for match in pattern.finditer(code):
            references.append(match.group(2))
            if pattern is TS_LITERAL_CALL:
                literal_call_starts.add(match.start())
    dynamic = sum(
        1
        for match in TS_SOURCE_CALL.finditer(code)
        if match.start() not in literal_call_starts
    )
    return references, dynamic


def _check_ts_references(source: Path, code: str, root: Path) -> list[Violation]:
    relative = _relative(source, root)
    references, dynamic_count = _ts_module_references(code)
    violations = [
        Violation(
            relative,
            "dynamic-source-reference",
            "non-literal import/require cannot be admitted structurally",
        )
        for _ in range(dynamic_count)
    ]
    for raw in references:
        if raw.startswith(".") or raw.startswith("/"):
            violation = _reference_violation(source, raw, root)
            if violation is not None:
                violations.append(violation)
        elif raw == "node:child_process":
            if relative != NPM_LAUNCHER:
                violations.append(
                    Violation(
                        relative,
                        "process-boundary",
                        "child_process is allowed only for the audited npm launcher",
                    )
                )
        elif (
            raw not in ALLOWED_BUN_MODULES
            and (
                relative,
                raw,
            )
            not in ALLOWED_BUN_SOURCE_MODULES
        ):
            violations.append(
                Violation(
                    relative,
                    "dependency-not-allowed",
                    f"Bun source imports a module outside the explicit allowlist: {raw}",
                )
            )
    return violations


def _check_ts_reference_directives(
    source: Path, text: str, root: Path
) -> list[Violation]:
    violations: list[Violation] = []
    for kind, _, raw in TS_REFERENCE_DIRECTIVE.findall(text):
        if kind == "path":
            violation = _reference_violation(source, raw, root)
            if violation is not None:
                violations.append(violation)
        elif raw != "bun":
            violations.append(
                Violation(
                    _relative(source, root),
                    "dependency-not-allowed",
                    f"TypeScript reference types is outside the explicit allowlist: {raw}",
                )
            )
    return violations


def _balanced_macro_argument(code: str, opening: int) -> str | None:
    depth = 1
    quote: str | None = None
    escaped = False
    index = opening + 1
    while index < len(code):
        char = code[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char == '"':
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return code[opening + 1 : index].strip()
        index += 1
    return None


def _rust_crate_dir(source: Path, root: Path) -> Path:
    return _cargo_crate_root(source, root) or source.parent


def _check_rust_references(source: Path, code: str, root: Path) -> list[Violation]:
    relative = _relative(source, root)
    violations: list[Violation] = []
    for raw in RUST_PATH_ATTRIBUTE.findall(code):
        violation = _reference_violation(source, raw, root)
        if violation is not None:
            violations.append(violation)
    for match in re.finditer(r"\binclude(?:_bytes|_str)?!\s*\(", code):
        argument = _balanced_macro_argument(code, match.end() - 1)
        literal = (
            re.fullmatch(r'"([^\"]+)"', argument, re.DOTALL)
            if argument is not None
            else None
        )
        if literal is not None:
            violation = _reference_violation(source, literal.group(1), root)
            if violation is not None:
                violations.append(violation)
            continue
        normalized = re.sub(r"\s+", "", argument or "")
        if (
            relative == "cli/rust/crates/prose-cli/src/main.rs"
            and normalized == 'concat!(env!("OUT_DIR"),"/current.bundle.bin")'
        ):
            continue
        violations.append(
            Violation(
                relative,
                "dynamic-source-reference",
                "non-literal Rust include cannot be admitted structurally",
            )
        )
    crate_dir = _rust_crate_dir(source, root)
    inspect_constructed_paths = source.name == "build.rs" or "/src/" in relative
    for raw in RUST_PATH_CONSTRUCTOR.findall(code):
        if inspect_constructed_paths and raw.startswith(("./", "../")):
            target = (crate_dir / raw).resolve()
            if not _allowed_source_reference(source, target, root):
                violations.append(
                    Violation(
                        relative,
                        "source-import-escape",
                        f"Rust path construction leaves the explicit product/shared allowlist: {raw}",
                    )
                )
    for raw in RUST_LITERAL_FILE_REFERENCE.findall(code):
        if not raw.startswith(("./", "../")):
            continue
        target = (crate_dir / raw).resolve()
        if not _allowed_source_reference(source, target, root):
            violations.append(
                Violation(
                    relative,
                    "source-import-escape",
                    f"Rust literal file reference leaves the explicit product/shared allowlist: {raw}",
                )
            )
    return violations


def _tainted_names(code: str, *, rust: bool) -> set[str]:
    tainted = {"args", "argv", "opaque", "forwarded", "task_argv", "taskArgv"}
    if rust:
        tainted.update(
            match.group(1)
            for match in re.finditer(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:Vec\s*<\s*String\s*>|&\s*\[\s*String\s*\])",
                code,
            )
        )
        assignments = re.findall(
            r"\blet(?:\s+mut)?\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);", code
        )
        seed = "std::env::args"
    else:
        tainted.update(
            match.group(1)
            for match in re.finditer(
                r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*(?:readonly\s+)?string\s*\[\s*\]",
                code,
            )
        )
        assignments = re.findall(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([^;\n]+)", code
        )
        seed = "process.argv"
    changed = True
    while changed:
        changed = False
        for name, expression in assignments:
            if name not in tainted and (
                seed in expression
                or ".argv" in expression
                or any(
                    re.search(rf"\b{re.escape(item)}\b", expression) for item in tainted
                )
            ):
                tainted.add(name)
                changed = True
    return tainted


def _read_aliases(code: str, *, rust: bool) -> set[str]:
    aliases = {
        "createReadStream",
        "open",
        "read",
        "readFile",
        "readFileSync",
        "read_to_string",
    }
    if rust:
        for original, alias in re.findall(
            r"\buse\s+(?:std::fs|fs)::(read_to_string|read|File::open)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)",
            code,
        ):
            aliases.add(alias or original.rsplit("::", 1)[-1])
        return aliases
    for bindings in re.findall(
        r"import\s*\{([^}]*)\}\s*from\s*[\"']node:fs(?:/promises)?[\"']", code
    ):
        for binding in bindings.split(","):
            match = re.match(
                r"\s*(readFile(?:Sync)?)\s*(?:as\s+([A-Za-z_$][A-Za-z0-9_$]*))?",
                binding,
            )
            if match:
                aliases.add(match.group(2) or match.group(1))
    for bindings in re.findall(
        r"\{([^}]*)\}\s*=\s*require\s*\(\s*[\"']node:fs[\"']\s*\)", code
    ):
        for binding in bindings.split(","):
            match = re.match(
                r"\s*(readFile(?:Sync)?)\s*(?::\s*([A-Za-z_$][A-Za-z0-9_$]*))?", binding
            )
            if match:
                aliases.add(match.group(2) or match.group(1))
    changed = True
    while changed:
        changed = False
        for alias, original in re.findall(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*;",
            code,
        ):
            if original in aliases and alias not in aliases:
                aliases.add(alias)
                changed = True
    return aliases


def _opaque_read_scopes(code: str, *, rust: bool) -> tuple[str, ...]:
    """Keep Rust argv taint local to a function instead of the whole file.

    The checker is deliberately structural, but Rust local names are scoped to
    a function. Treating every repeated local name in a module as one alias can
    connect an argv-derived value in production code to an unrelated fixture
    read in a test function and report a boundary that does not exist.
    """
    if not rust:
        return (code,)
    starts = [
        match.start()
        for match in re.finditer(
            r"(?m)^[ \t]*(?:(?:pub(?:\([^\n)]*\))?|async|unsafe)\s+)*fn\s+[A-Za-z_][A-Za-z0-9_]*\b",
            code,
        )
    ]
    if not starts:
        return (code,)
    return tuple(code[start:end] for start, end in zip(starts, (*starts[1:], len(code))))


def _first_call_argument(code: str, opening: int, *, rust: bool) -> str:
    """Extract only the path argument, respecting nested expressions/literals.

    A greedy line match can mistake a later redaction argument or even a struct
    field after the call for its filesystem path. Keep conservative call-name
    detection, but do not propagate taint from those unrelated expressions.
    """
    cursor = opening + 1
    start = cursor
    depth = 0
    while cursor < len(code):
        char = code[cursor]
        if rust and char in {"b", "c", "r"}:
            end = _rust_raw_literal_end(code, cursor)
            if end is not None:
                cursor = end
                continue
        if not rust and char == "`":
            # Nested template interpolation requires a full JS parser.
            return code[start:]
        if char in ({'"', "'"} if not rust else {'"'}):
            literal = _escaped_literal(code, cursor, char)
            if literal is None:
                return code[start:]
            cursor = literal[1]
            continue
        if rust and char == "'":
            # Character literals, not Rust lifetimes.
            character = re.match(r"'(?:\\.|[^'\\])'", code[cursor:])
            if character:
                cursor += character.end()
                continue
        if char == "<" or (not rust and char == "/"):
            # Generic/type arguments and JS regex literals have delimiters this
            # intentionally small scanner cannot distinguish from expressions.
            # Never truncate at a comma or parenthesis inside ambiguous syntax:
            # conservatively retain the rest of this scope for taint checking.
            # Simple paths still exclude unrelated later arguments/struct fields.
            return code[start:]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return code[start:cursor]
            depth -= 1
        elif char == "," and depth == 0:
            return code[start:cursor]
        cursor += 1
    return code[start:]


def _check_opaque_reads(source: Path, code: str, root: Path) -> list[Violation]:
    rust = source.suffix == ".rs"
    read_names = _read_aliases(code, rust=rust)
    call_names = "|".join(
        re.escape(name) for name in sorted(read_names, key=len, reverse=True)
    )
    for scope in _opaque_read_scopes(code, rust=rust):
        tainted = _tainted_names(scope, rust=rust)
        expressions: list[str] = []
        calls = re.finditer(
            rf"\b(?:{call_names}|Bun\.file)\s*\(", scope
        )
        for call in calls:
            # Function declarations are not reads (notably `fn open(...)`).
            if rust and re.search(r"\bfn\s+$", scope[:call.start()]):
                continue
            expressions.append(
                _first_call_argument(scope, call.end() - 1, rust=rust)
            )
        for expression in expressions:
            if (
                "process.argv" in expression
                or ".argv" in expression
                or any(
                    re.search(rf"\b{re.escape(name)}\b", expression)
                    for name in tainted
                )
            ):
                return [
                    Violation(
                        _relative(source, root),
                        "opaque-program-boundary",
                        "file-content read is derived from forwarded program argv",
                    )
                ]
    return []


def _check_process_boundary(source: Path, code: str, root: Path) -> list[Violation]:
    relative = _relative(source, root)
    details: set[str] = set()
    if re.search(
        r"\bshell\s*:\s*true\b|\bBun\.\$|\b(?:openpty|forkpty|pty\.spawn|Bun\.dlopen|Deno\.dlopen|node-pty|portable-pty)\b",
        code,
    ):
        details.add("shell/PTY/native-FFI facility")
    if re.search(r"(?<!\.)\bexec(?:Sync)?\s*\(", code):
        details.add("shell-string execution API")
    command_names = {"Command", "std::process::Command"}
    command_names.update(
        match.group(1) for match in RUST_COMMAND_IMPORT.finditer(code) if match.group(1)
    )
    shell_constants = {
        match.group(1)
        for match in re.finditer(
            rf"\b(?:const|let|var)(?:\s+mut)?\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=;]+)?=\s*[\"']{SHELL_EXECUTABLE}[\"']",
            code,
            re.IGNORECASE,
        )
    }
    for name in command_names:
        if re.search(
            rf"\b{re.escape(name)}::new\s*\(\s*[\"']{SHELL_EXECUTABLE}[\"']",
            code,
            re.IGNORECASE,
        ):
            details.add("literal shell process launch")
        for constant in shell_constants:
            if re.search(
                rf"\b{re.escape(name)}::new\s*\(\s*{re.escape(constant)}\s*\)",
                code,
            ):
                details.add("aliased literal shell process launch")
    if re.search(
        rf"\b(?:Bun\.spawn|spawn|spawnSync)\s*\(\s*(?:\[\s*)?[\"']{SHELL_EXECUTABLE}[\"']",
        code,
        re.IGNORECASE,
    ):
        details.add("literal shell process launch")
    for constant in shell_constants:
        if re.search(
            rf"\b(?:Bun\.spawn|spawn|spawnSync)\s*\(\s*(?:\[\s*)?{re.escape(constant)}\b",
            code,
        ):
            details.add("aliased literal shell process launch")
    if "node:child_process" in code and relative == NPM_LAUNCHER:
        statements = [
            line for line in code.splitlines() if "node:child_process" in line
        ]
        if statements != ['const { spawn } = require("node:child_process");']:
            details.add("npm launcher child_process binding is not exact spawn-only")
    return [
        Violation(relative, "process-boundary", detail) for detail in sorted(details)
    ]


def _check_ts_resolved_paths(source: Path, code: str, root: Path) -> list[Violation]:
    relative = _relative(source, root)
    values: dict[str, Path] = {"__dirname": source.parent.resolve()}
    violations: list[Violation] = []
    assignments = re.findall(
        r"\bconst\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*resolve\s*\(([^;]+)\)\s*;", code
    )
    for _ in range(len(assignments) + 1):
        changed = False
        for name, arguments in assignments:
            if name in values:
                continue
            parts = [part.strip() for part in arguments.split(",")]
            target = (
                source.parent.resolve()
                if parts and parts[0] == "import.meta.dir"
                else values.get(parts[0])
                if parts
                else None
            )
            if target is None:
                continue
            literals: list[str] = []
            for part in parts[1:]:
                match = re.fullmatch(r'(["\'])(.*?)\1', part)
                if match is None:
                    literals = []
                    break
                literals.append(match.group(2))
            if len(literals) != max(0, len(parts) - 1):
                continue
            for item in literals:
                target = (target / item).resolve()
            values[name] = target
            changed = True
            if not _allowed_source_reference(source, target, root):
                violations.append(
                    Violation(
                        relative,
                        "source-import-escape",
                        f"resolved build/source path leaves the explicit allowlist: {name}",
                    )
                )
        if not changed:
            break
    return violations


def check_source_boundaries(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for product_root in PRODUCT_ROOTS:
        for source in _files(root, product_root, SOURCE_SUFFIXES):
            relative = _relative(source, root)
            if source.suffix == ".rs" and "/tests/" in relative:
                # Cargo integration tests are not linked into stable products.
                continue
            if source.is_symlink():
                violations.append(
                    Violation(relative, "source-symlink", "stable source is a symlink")
                )
                continue
            try:
                text = source.read_text("utf-8")
            except UnicodeDecodeError:
                violations.append(
                    Violation(relative, "source-encoding", "stable source is not UTF-8")
                )
                continue
            rust = source.suffix == ".rs"
            code = _without_comments(text, single_quoted_strings=not rust)
            violations.extend(_check_language_literals(source, code, root))
            violations.extend(_check_process_boundary(source, code, root))
            violations.extend(_check_opaque_reads(source, code, root))
            if rust:
                violations.extend(_check_rust_references(source, code, root))
            else:
                violations.extend(_check_ts_reference_directives(source, text, root))
                violations.extend(_check_ts_references(source, code, root))
                violations.extend(_check_ts_resolved_paths(source, code, root))
    return sorted(set(violations))


def check_repository(root: Path) -> list[Violation]:
    return sorted(set(check_manifest_paths(root) + check_source_boundaries(root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Structural (not semantic/security) outer-runner boundary guard"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args(argv)
    violations = check_repository(args.root.resolve())
    if violations:
        print(
            f"FAIL: {len(violations)} architecture-boundary violation(s)",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"- {violation.render()}", file=sys.stderr)
        return 1
    print(
        "PASS: stable CLIs remain structurally outside language/ambient-skill dependencies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
