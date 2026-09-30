#!/usr/bin/env python3
"""Public-surface leak gate.

prose-cli is a public user client for OpenProse. This gate fails when
internal service detail or developer-only surface leaks into it:

* every tracked (and untracked, not ignored) file in the repository;
* the printable strings of any ``--binary`` given;
* the output of any ``--help-command`` given, and for every ``--help-binary``
  the whole user-facing surface of that binary: ``--help``, ``cli --help``,
  ``cli service operations`` and ``cli service capabilities`` (human and
  ``--json``), ``cli service guide``, and the ``--help`` of every command
  group and command in the tree: those the operation manifest and the help
  topics name, then recursively every ``cli <group> [<command>...]`` any help
  text names (runner commands such as ``cli harness use`` included).

``--build-release`` builds both ports' public release binaries first (the
Rust one with the source root and Cargo home remapped, as a distributable
build is), then scans both binaries' strings and their whole user-facing
surface, which is what the ``public-surface`` gate in cli/ci/run_local.py
does. The Bun binary is scanned against a baseline: strings that the Bun
runtime executable itself contains are the runtime's, not this client's.

The denylist lives in cli/ci/public_surface_denylist.py, which is the only
file excluded from the scan.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, Iterator, NamedTuple, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from public_surface_denylist import (  # noqa: E402
    BINARY_ONLY,
    FORBIDDEN,
    HELP_ONLY,
    JARGON,
    RETIRED,
    Pattern,
)


CLI_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CLI_ROOT.parent
DENYLIST_PATH = "cli/ci/public_surface_denylist.py"
EXCLUDED_PATHS = frozenset({DENYLIST_PATH})
MANIFEST_PATH = "cli/shared/service/operations.v1.json"
HELP_PATH = "cli/shared/service/help.v1.json"
# Tracked files that both ports print verbatim as help, guide or manifest
# text. They are user-facing help, so the help-only patterns apply too.
HELP_SOURCE_PATHS = frozenset(
    {
        "cli/conformance/cases/fixtures/runner-help.txt",
        "cli/shared/errors/taxonomy.v1.json",
        "cli/shared/service/guide.v1.md",
        "cli/shared/service/help.v1.json",
        MANIFEST_PATH,
    }
)
# User documentation, the published contract and the conformance corpus: no
# internal process vocabulary.
USER_DOC_PATHS = frozenset(
    {"README.md", "cli/README.md", "cli/protocol/STATUS.md", "docs/hosted-service-client.md"}
)
USER_DOC_PREFIXES = (
    "docs/service/",
    "cli/shared/",
    "cli/conformance/",
    # Product source and its tests ship publicly too; their comments and test
    # names carry no internal process vocabulary either.
    "cli/bun/src/",
    "cli/bun/test/",
    "cli/bun/scripts/",
    "cli/rust/crates/",
    # Build, release and gate tooling is public source as well.
    "cli/ci/",
)

# A documented or scripted release build that remaps the source root but not
# Cargo's home embeds the builder's home directory in the binary through the
# panic locations of its dependencies. Build recipes must remap both.
RECIPE_SUFFIXES = (".md", ".yml", ".yaml", ".sh")
RECIPE_PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "release build without a Cargo home remap",
        re.compile(r"^(?!.*=/cargo-home).*--remap-path-prefix=\S*=/openprose-source"),
    ),
)

HELP_PATTERNS: tuple[Pattern, ...] = (*FORBIDDEN, *JARGON, *HELP_ONLY)
BINARY_PATTERNS: tuple[Pattern, ...] = (*FORBIDDEN, *JARGON, *BINARY_ONLY)

MIN_STRING_LENGTH = 4
MAX_EXCERPT = 120
RUST_TARGET_DIR = "target/public-surface"
BUN_OUTFILE = "dist/prose-public-surface"


def command_path(operation: dict) -> list[str]:
    """An operation's command path: its manifest `command` argv without the
    leading `cli`."""
    command = list(operation["command"])
    return command[1:] if command[:1] == ["cli"] else command


class Finding(NamedTuple):
    source: str
    line: int
    pattern: str
    excerpt: str

    def render(self) -> str:
        location = f"{self.source}:{self.line}" if self.line else self.source
        return f"{location}: {self.pattern}: {self.excerpt}"


def scan_text(
    source: str, text: str, patterns: Sequence[Pattern] = FORBIDDEN
) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern in patterns:
            match = pattern.regex.search(line)
            if match is None:
                continue
            start = max(0, match.start() - 40)
            excerpt = line[start : start + MAX_EXCERPT].strip()
            findings.append(Finding(source, number, pattern.name, excerpt))
    return findings


def repository_files(root: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored."""
    output = subprocess.run(
        ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    names = sorted({name for name in output.decode("utf-8").split("\0") if name})
    return [name for name in names if name not in EXCLUDED_PATHS]


# The changelog must name what a release removed, so the retired (formerly
# public) service environment names are allowed there and nowhere else.
RETIRED_NAME_PATHS = frozenset({"cli/CHANGELOG.md"})


def file_patterns(name: str) -> tuple[Pattern, ...]:
    recipe = RECIPE_PATTERNS if name.endswith(RECIPE_SUFFIXES) else ()
    if name in HELP_SOURCE_PATHS:
        return (*HELP_PATTERNS, *recipe)
    forbidden = FORBIDDEN
    if name in RETIRED_NAME_PATHS:
        forbidden = tuple(pattern for pattern in FORBIDDEN if pattern not in RETIRED)
    if name in USER_DOC_PATHS or name.startswith(USER_DOC_PREFIXES):
        return (*forbidden, *JARGON, *recipe)
    return (*forbidden, *recipe)


def scan_repository(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for name in repository_files(root):
        path = root / name
        if path.is_symlink() or not path.is_file():
            # Deleted in the working tree but still in the index.
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        findings.extend(scan_text(name, text, file_patterns(name)))
    return findings


def printable_strings(data: bytes, minimum: int = MIN_STRING_LENGTH) -> Iterator[str]:
    """Like ``strings(1)``: runs of printable ASCII of at least ``minimum``."""
    for match in re.finditer(rb"[\x20-\x7e\t]{%d,}" % minimum, data):
        yield match.group().decode("ascii")


def binary_strings(path: Path) -> set[str]:
    return set(printable_strings(path.read_bytes()))


def scan_binary(path: Path, baseline: Path | None = None) -> list[Finding]:
    """Scan a binary's strings; strings the ``baseline`` executable also
    contains (the Bun runtime inside a Bun standalone binary) are skipped."""
    try:
        data = path.read_bytes()
        skip = binary_strings(baseline) if baseline is not None else set()
    except OSError as error:
        return [Finding(f"strings {path}", 0, "binary unreadable", str(error))]
    text = "\n".join(value for value in printable_strings(data) if value not in skip)
    return [
        finding._replace(line=0)
        for finding in scan_text(f"strings {path}", text, BINARY_PATTERNS)
    ]


class HelpRun(NamedTuple):
    text: str
    returncode: int | None
    error: str | None


def run_help(command: Sequence[str], cwd: Path | None = None) -> HelpRun:
    """Run one help command in an empty home, with no OpenProse settings."""
    with tempfile.TemporaryDirectory(prefix="prose-public-surface-") as home:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                capture_output=True,
                env=_help_environment(Path(home)),
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return HelpRun("", None, str(error))
    text = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace")
    return HelpRun(text, completed.returncode, None)


def help_findings(
    command: Sequence[str], run: HelpRun, *, require_success: bool = True
) -> list[Finding]:
    source = " ".join(shlex.quote(part) for part in command)
    if run.error is not None:
        return [Finding(source, 0, "help command failed", run.error)]
    findings = scan_text(source, run.text, HELP_PATTERNS)
    if require_success and run.returncode != 0:
        findings.append(Finding(source, 0, "help command failed", f"exit {run.returncode}"))
    return findings


def scan_help(command: Sequence[str], cwd: Path | None = None) -> list[Finding]:
    return help_findings(command, run_help(command, cwd))


def _help_environment(home: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("OPENPROSE_", "PROSE_", "XDG_"))
    }
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "NO_COLOR": "1",
        }
    )
    return environment


def command_paths(manifest: dict) -> list[tuple[str, ...]]:
    """Every command group and command path the manifest names, in order."""
    paths: list[tuple[str, ...]] = []
    for operation in manifest.get("operations", []):
        words = tuple(command_path(operation))
        for end in range(1, len(words) + 1):
            if words[:end] not in paths:
                paths.append(words[:end])
    return paths


def topic_paths(help_data: dict) -> list[tuple[str, ...]]:
    """Every command path with a help topic (`cli run submit` -> (run, submit))."""
    paths: list[tuple[str, ...]] = []
    for topic in help_data.get("topics", {}):
        words = tuple(topic.split())
        if words[:1] == ("cli",) and len(words) > 1 and words[1:] not in paths:
            paths.append(words[1:])
    return paths


def help_commands(
    binary: Path, manifest: dict | None = None, help_data: dict | None = None
) -> list[list[str]]:
    """The known user-facing surface of one binary: the fixed entry points and
    the `--help` of every command group and command the manifest or the help
    topics name."""
    base = str(binary)
    commands = [
        [base, "--help"],
        [base, "cli", "--help"],
        [base, "cli", "service", "operations"],
        [base, "cli", "service", "operations", "--json"],
        [base, "cli", "service", "capabilities"],
        [base, "cli", "service", "capabilities", "--json"],
        [base, "cli", "service", "guide"],
    ]
    paths = command_paths(manifest or {})
    paths += [path for path in topic_paths(help_data or {}) if path not in paths]
    for path in paths:
        commands.append([base, "cli", *path, "--help"])
    return commands


_COMMAND_WORD = re.compile(r"[a-z][a-z0-9-]*(?:\|[a-z][a-z0-9-]*)*")
_SECTION_ENTRY = re.compile(r"^  ([a-z][a-z0-9-]*)\s{2,}\S")


def _expand(words: list[str]) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = [()]
    for word in words:
        paths = [path + (choice,) for path in paths for choice in word.split("|")]
    return paths


def discovered_paths(text: str, current: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Command paths a help text names: `cli WORD ...` usage and example lines
    (alternatives such as `list|show` expand) and the entries of a `Commands:`
    section, which are subcommands of ``current``."""
    found: list[tuple[str, ...]] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("Commands:") and not stripped.startswith("Commands: "):
            in_section = True
            continue
        if in_section:
            entry = _SECTION_ENTRY.match(line)
            if entry is not None:
                found.append(current + (entry.group(1),))
                continue
            if stripped:
                in_section = line.startswith("   ")
        tokens = stripped.split()
        if tokens[:1] == ["prose"]:
            tokens = tokens[1:]
        if tokens[:1] != ["cli"]:
            continue
        words: list[str] = []
        for token in tokens[1:]:
            if not _COMMAND_WORD.fullmatch(token):
                break
            words.append(token)
        for path in _expand(words):
            for end in range(1, len(path) + 1):
                found.append(path[:end])
    unique: list[tuple[str, ...]] = []
    for path in found:
        if path and path not in unique:
            unique.append(path)
    return unique


MAX_DISCOVERED_HELP = 400
MAX_COMMAND_DEPTH = 4


def walk_help(
    binary: Path,
    manifest: dict | None = None,
    help_data: dict | None = None,
    cwd: Path | None = None,
) -> tuple[list[Finding], int]:
    """Scan the whole help tree of one binary: the known surface (which must
    succeed), then every `prose cli <group> [<command>...] --help` that any
    help text names, recursively. A discovered path that is not a real
    command (an example argument, say) is still scanned but may fail."""
    base = str(binary)
    known = help_commands(binary, manifest, help_data)
    seen = {tuple(command[1:]) for command in known}
    queue: list[tuple[list[str], bool]] = [(command, True) for command in known]
    findings: list[Finding] = []
    count = 0
    while queue and count < len(known) + MAX_DISCOVERED_HELP:
        command, required = queue.pop(0)
        run = run_help(command, cwd)
        count += 1
        findings.extend(help_findings(command, run, require_success=required))
        words = command[1:]
        current: tuple[str, ...] = ()
        if words[:1] == ["cli"] and words[-1:] == ["--help"]:
            current = tuple(words[1:-1])
        for path in discovered_paths(run.text, current):
            key = ("cli", *path, "--help")
            if len(path) > MAX_COMMAND_DEPTH or key in seen:
                continue
            seen.add(key)
            queue.append(([base, *key], False))
    return findings, count


def load_help(root: Path) -> dict:
    try:
        return json.loads((root / HELP_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_manifest(root: Path) -> dict:
    try:
        return json.loads((root / MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def release_rustflags(root: Path) -> str:
    """Remap the source root and Cargo home, as a distributable build does,
    so no builder's home directory reaches the binary."""
    cargo_home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")
    flags = [
        f"--remap-path-prefix={root.resolve()}=/openprose-source",
        f"--remap-path-prefix={cargo_home.resolve()}=/cargo-home",
    ]
    existing = os.environ.get("RUSTFLAGS", "").strip()
    return " ".join([existing, *flags] if existing else flags)


def build_release(root: Path) -> tuple[Path, Path]:
    """Build both ports' public release binaries and return their paths."""
    cli = root / "cli"
    # Dedicated outputs, so a concurrent developer or dev-endpoint build can
    # never be scanned in place of the public release build.
    subprocess.run(
        (
            "cargo", "build", "--release", "--locked", "-p", "prose-cli", "--bin", "prose",
            "--target-dir", RUST_TARGET_DIR,
        ),
        cwd=cli / "rust",
        check=True,
        env={**os.environ, "RUSTFLAGS": release_rustflags(root), "CARGO_INCREMENTAL": "0"},
    )
    subprocess.run(
        ("bun", "run", "build:release", "--outfile", BUN_OUTFILE),
        cwd=cli / "bun",
        check=True,
    )
    return (
        cli / "rust" / RUST_TARGET_DIR / "release" / "prose",
        cli / "bun" / BUN_OUTFILE,
    )


def bun_runtime() -> Path | None:
    found = shutil.which("bun")
    return Path(found).resolve() if found else None


def check(
    root: Path,
    *,
    binaries: Iterable[Path | tuple[Path, Path | None]] = (),
    help_command_lines: Iterable[Sequence[str]] = (),
    scan_files: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    if scan_files:
        findings.extend(scan_repository(root))
    for entry in binaries:
        binary, baseline = entry if isinstance(entry, tuple) else (entry, None)
        findings.extend(scan_binary(binary, baseline))
    for command in help_command_lines:
        findings.extend(scan_help(command, cwd=root))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--binary",
        action="append",
        default=[],
        type=Path,
        help="scan the printable strings of this binary (repeatable)",
    )
    parser.add_argument(
        "--bun-binary",
        action="append",
        default=[],
        type=Path,
        help="scan this Bun standalone binary's strings, less the Bun runtime's own (repeatable)",
    )
    parser.add_argument(
        "--help-command",
        action="append",
        default=[],
        help="run this shell-quoted command and scan its output as user-facing help (repeatable)",
    )
    parser.add_argument(
        "--help-binary",
        action="append",
        default=[],
        type=Path,
        help="scan the whole user-facing help, guide and manifest output of this binary (repeatable)",
    )
    parser.add_argument(
        "--build-release",
        action="store_true",
        help="build both ports' release binaries, then scan their strings and user-facing output",
    )
    parser.add_argument(
        "--no-files", action="store_true", help="skip the repository file scan"
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()

    runtime = bun_runtime()
    binaries: list[Path | tuple[Path, Path | None]] = list(arguments.binary)
    binaries.extend((path, runtime) for path in arguments.bun_binary)
    commands: list[list[str]] = [shlex.split(line) for line in arguments.help_command]
    help_binaries: list[Path] = list(arguments.help_binary)
    if arguments.build_release:
        try:
            rust, bun = build_release(root)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"public-surface: release build failed: {error}", file=sys.stderr)
            return 1
        binaries.extend((rust, (bun, runtime)))
        help_binaries.extend((rust, bun))
    manifest = load_manifest(root)
    help_data = load_help(root)

    findings = check(
        root,
        binaries=binaries,
        help_command_lines=commands,
        scan_files=not arguments.no_files,
    )
    walked = 0
    for binary in help_binaries:
        binary_findings, count = walk_help(binary, manifest, help_data, cwd=root)
        findings.extend(binary_findings)
        walked += count
    for finding in findings:
        print(finding.render())
    if findings:
        print(
            f"public-surface: {len(findings)} leak(s) of internal or developer-only "
            "surface into the public client",
            file=sys.stderr,
        )
        return 1
    scanned = ["repository files"] if not arguments.no_files else []
    scanned += [
        f"strings {entry[0] if isinstance(entry, tuple) else entry}" for entry in binaries
    ]
    scanned.append(f"{len(commands) + walked} help, guide and manifest invocations")
    print("public-surface: clean (" + "; ".join(scanned) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
