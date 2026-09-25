#!/usr/bin/env python3
"""Unit tests for the public-surface leak gate (check_public_surface.py).

Every forbidden sample below is assembled from fragments so this file never
trips the scan it tests. Identifiers captured from service data are matched
by digest; the mechanics are tested with made-up identifiers.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_public_surface as checker  # noqa: E402
import public_surface_denylist  # noqa: E402
from public_surface_denylist import (  # noqa: E402
    BINARY_ONLY,
    FORBIDDEN,
    HELP_ONLY,
    JARGON,
    CAPTURED_ID_DIGESTS,
    RETIRED,
    DigestMatcher,
    Pattern,
    identifier_digest,
)


STAGE = "stag" + "ing"
# Random-looking (a real id never repeats a short unit); computed, so this
# file holds no literal id.
HEX64 = hashlib.sha256(b"public surface sample").hexdigest()
HEX32 = HEX64[:32]
WORKERS = ".openprose.workers." + "dev"
SERVICE = "run-" + "prose"

# One sample per generic forbidden pattern family, each built from fragments.
LEAKS = {
    "retired hostname": "https://" + SERVICE + "-" + STAGE + WORKERS,
    "other worker hostname": "https://preview-" + "thing.openprose.workers." + "dev/x",
    "endpoint selector placeholder": "dev" + ":<name>",
    "endpoint selector": "use dev" + ":sample here",
    "retired key": "OPENPROSE_" + STAGE.upper() + "_API_KEY",
    "environment option": "--service-" + "environment",
    "run id": "run_" + HEX64,
    "32-hex run id": "run_" + HEX32,
    "developer mac home": "/" + "Users/someone/code/openprose/.env",
    "developer linux home": "/" + "home/someone/work/x",
}

# Internal process vocabulary: forbidden in help, user docs and binaries only.
JARGON_LEAKS = {
    "ticket": "Account credentials (" + "IMP" + "-034)",
    "ticket in a camelCase name": "function is" + "Imp" + "035(candidate)",
    "ticket in a snake_case name": "fn is_" + "imp" + "034_verb(noun)",
    "lowercase ticket": '"id": "' + "imp" + '-008-unused-framing/1"',
    "ticket in a path": "decisions/" + "imp" + "026-weave-host.md",
    "internal service name": "the " + SERVICE + " catalog id",
    "internal service header": "X-" + SERVICE.title() + "-Client",
    "internal service name in a snake_case name": "sync_" + SERVICE.replace("-", "_") + "_interactions.py",
}

# Forbidden only in the strings of a public release binary.
BINARY_LEAKS = {
    "override": "OPENPROSE_" + "API_URL",
    "label": "OpenProse (custom " + "endpoint https://x.example)",
    "credential entry": "org.openprose.cli." + "custom-x",
    "credential service": "org.openprose.cli." + "custom",
    "fake harness": "OPENPROSE_" + "CONFORMANCE_FAKE_HARNESS",
    "test scenario": "OPENPROSE_" + "TEST_SCENARIO",
    "service fixture": "PROSE_" + "TEST_SERVICE_FIXTURE",
    "mac home": "/" + "Users/someone/.cargo/registry/src/x.rs",
    "linux home": "/" + "home/someone/.cargo/registry/src/x.rs",
}

CLEAN = [
    "https://run-prose-production.openprose.workers.dev/health",
    "prose --output json cli run list",
    "OPENPROSE_API_KEY and OPENPROSE_API_URL",
    "run_aaaaaaaaaaaaaaaa is running",
    "dev: info.dev, devDependencies, build:dev, dev:./path",
    "release " + STAGE + " directory",  # tracked files may use the word
    "run_" + "0123456789abcdef",  # a short synthetic fixture id
    "run_" + "4f1c2d3e4f5a6b7c" * 4,  # a full-length synthetic fixture id (a repeated unit)
    "run_" + "abc123" * 10 + "abc1",  # a repeated unit cut to 64 digits
    "https://example.invalid",
    "example_feature is disabled",
    "/Users/alice/.config/openprose/cli.toml",  # test placeholder users
    "/Users/private/.env and /home/runner/work/x",
    "gpt-5.4-mini, gpt-5.5 and model-luna",
    "Import123, simple042, swp12 and wpa12",
]


class ScanTextTests(unittest.TestCase):
    def test_every_leak_family_is_found(self) -> None:
        for label, sample in LEAKS.items():
            with self.subTest(label=label):
                findings = checker.scan_text("sample", f"prefix {sample} suffix\n")
                self.assertTrue(findings, f"{label} was not flagged: {sample}")
                self.assertEqual(findings[0].line, 1)

    def test_clean_text_passes(self) -> None:
        for sample in CLEAN:
            with self.subTest(sample=sample):
                self.assertEqual(checker.scan_text("sample", sample), [])

    def test_the_word_is_forbidden_only_in_help(self) -> None:
        text = "Select the " + STAGE.capitalize() + " service"
        self.assertEqual(checker.scan_text("file", text), [])
        findings = checker.scan_text("help", text, (*FORBIDDEN, *HELP_ONLY))
        self.assertEqual([finding.pattern for finding in findings], [HELP_ONLY[0].name])

    def test_jargon_is_forbidden_only_in_user_facing_text(self) -> None:
        for label, sample in JARGON_LEAKS.items():
            with self.subTest(label=label):
                self.assertEqual(checker.scan_text("source.rs", sample), [])
                self.assertTrue(checker.scan_text("help", sample, checker.HELP_PATTERNS))
                self.assertTrue(checker.scan_text("strings", sample, checker.BINARY_PATTERNS))
        self.assertEqual(checker.scan_text("help", "IMP-" + "0351 and xIMP" + "123", JARGON), [])
        # The production origin carries the service name; the products must reach it.
        production = "https://" + SERVICE + "-production" + WORKERS + "/health"
        self.assertEqual(checker.scan_text("strings", production, checker.BINARY_PATTERNS), [])
        self.assertEqual(checker.scan_text("help", "prose --json run.prose", checker.HELP_PATTERNS), [])

    def test_only_high_entropy_identifiers_are_matched_by_digest(self) -> None:
        # A digest of a short or guessable term can be reversed by trying
        # candidates, so the public denylist holds no such digests: the only
        # digest-matched pattern is for random identifiers captured from
        # service data.
        digest_patterns = [
            pattern for pattern in (*FORBIDDEN, *JARGON, *HELP_ONLY, *BINARY_ONLY)
            if isinstance(pattern.regex, DigestMatcher)
        ]
        self.assertEqual(["identifier captured from service data"], [p.name for p in digest_patterns])
        exported = [name for name in dir(public_surface_denylist) if name.endswith("_DIGESTS")]
        self.assertEqual(["CAPTURED_ID_DIGESTS"], exported)

    def test_binary_only_patterns(self) -> None:
        for label, sample in BINARY_LEAKS.items():
            with self.subTest(label=label):
                if not label.endswith("home"):
                    # A developer's home directory is forbidden everywhere.
                    self.assertEqual(checker.scan_text("source.ts", sample), [])
                    self.assertEqual(checker.scan_text("help", sample, checker.HELP_PATTERNS), [])
                self.assertTrue(checker.scan_text("strings", sample, BINARY_ONLY), label)
        for clean in ("OPENPROSE_API_KEY", "/openprose-source/cli/rust/x.rs", "/cargo-home/registry/x.rs"):
            self.assertEqual(checker.scan_text("strings", clean, BINARY_ONLY), [], clean)

    def test_captured_service_ids_are_matched_by_digest_only(self) -> None:
        self.assertEqual(3, len(CAPTURED_ID_DIGESTS))
        uuid = "0a1b2c3d-" + "0000-4000-8000-" + "00000000abcd"
        run = "run_" + "00112233" + "44556677"
        matcher = DigestMatcher(
            frozenset({identifier_digest(uuid), identifier_digest(run)}),
            public_surface_denylist._CAPTURED_ID,
        )
        patterns = (Pattern("captured", matcher),)
        for text in (f"/triggers/{uuid}", f'"id": "{uuid}"', f"/runs/{run}/files", f"{run}.", run + "89abcdef"):
            with self.subTest(text=text):
                self.assertTrue(checker.scan_text("sample", text, patterns), text)
        for text in (uuid.upper(), f"{uuid}0", "x" + run, "run_" + "ffffffff" * 2):
            with self.subTest(text=text):
                self.assertEqual(checker.scan_text("sample", text, patterns), [])

    def test_findings_report_line_numbers(self) -> None:
        text = "clean\nclean\n" + LEAKS["run id"] + "\n"
        (finding,) = checker.scan_text("f.md", text)
        self.assertEqual(finding.line, 3)
        self.assertTrue(finding.render().startswith("f.md:3: "))


class RepositoryScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.git("init", "-q")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def git(self, *arguments: str) -> None:
        subprocess.run(
            ("git", *arguments),
            cwd=self.root,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        )

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_clean_repository_passes(self) -> None:
        self.write("README.md", "\n".join(CLEAN))
        self.git("add", "README.md")
        self.assertEqual(checker.scan_repository(self.root), [])

    def test_tracked_and_untracked_files_are_scanned(self) -> None:
        self.write("docs/a.md", "see " + LEAKS["retired key"] + "\n")
        self.git("add", "docs/a.md")
        self.write("new.txt", LEAKS["run id"] + "\n")
        sources = {finding.source for finding in checker.scan_repository(self.root)}
        self.assertEqual(sources, {"docs/a.md", "new.txt"})

    def test_ignored_and_deleted_files_are_skipped(self) -> None:
        self.write(".gitignore", "build/\n")
        self.write("build/out.txt", LEAKS["environment option"])
        self.write("gone.md", LEAKS["environment option"])
        self.git("add", ".gitignore", "gone.md")
        (self.root / "gone.md").unlink()
        self.assertEqual(checker.scan_repository(self.root), [])

    def test_help_sources_get_the_help_only_patterns(self) -> None:
        text = "Select the " + STAGE + " service\n"
        self.write("docs/notes.md", text)
        self.write("cli/shared/service/guide.v1.md", text)
        self.git("add", "docs/notes.md", "cli/shared/service/guide.v1.md")
        sources = {finding.source for finding in checker.scan_repository(self.root)}
        self.assertEqual(sources, {"cli/shared/service/guide.v1.md"})

    def test_user_docs_and_contract_get_the_jargon_patterns(self) -> None:
        text = JARGON_LEAKS["ticket"] + "\n"
        names = ("README.md", "docs/service/runs.md", "docs/hosted-service-client.md", "cli/protocol/STATUS.md",
                 "cli/shared/schemas/x.schema.json", "cli/conformance/cases/service/runs/x.json",
                 "cli/bun/src/core/x.ts", "cli/bun/test/x.test.ts",
                 "cli/rust/crates/prose-runner-core/src/x.rs", "cli/ci/x.py",
                 "cli/src/lib.rs", "cli/protocol/x.md")
        for name in names:
            self.write(name, text)
            self.git("add", name)
        sources = {finding.source for finding in checker.scan_repository(self.root)}
        self.assertEqual(sources, set(names[:10]))

    def test_release_recipes_must_remap_cargo_home(self) -> None:
        source_only = 'RUSTFLAGS="--remap-path-prefix=$SOURCE_ROOT=/openprose-source" \\\n'
        both = (
            'RUSTFLAGS="--remap-path-prefix=$SOURCE_ROOT=/openprose-source'
            ' --remap-path-prefix=$CARGO_HOME_ROOT=/cargo-home" \\\n'
        )
        self.write("cli/release/README.md", source_only)
        self.write(".github/workflows/release.yml", source_only)
        self.write("cli/ci/check_release.py", source_only)
        self.write("docs/build.md", both)
        self.git("add", "cli/release/README.md", ".github/workflows/release.yml",
                 "cli/ci/check_release.py", "docs/build.md")
        findings = checker.scan_repository(self.root)
        self.assertEqual(
            {finding.source for finding in findings},
            {"cli/release/README.md", ".github/workflows/release.yml"},
        )
        self.assertEqual({finding.pattern for finding in findings},
                         {"release build without a Cargo home remap"})

    def test_changelog_may_name_retired_names_only(self) -> None:
        text = "\n".join((LEAKS["retired key"], LEAKS["environment option"])) + "\n"
        self.write("cli/CHANGELOG.md", text + LEAKS["run id"] + "\n")
        self.write("docs/notes.md", text)
        self.git("add", "cli/CHANGELOG.md", "docs/notes.md")
        findings = checker.scan_repository(self.root)
        self.assertEqual(
            [(finding.source, finding.pattern) for finding in findings if finding.source == "cli/CHANGELOG.md"],
            [("cli/CHANGELOG.md", "real-looking run id")],
        )
        self.assertEqual(
            {finding.pattern for finding in findings if finding.source == "docs/notes.md"},
            {pattern.name for pattern in RETIRED},
        )

    def test_denylist_file_is_excluded(self) -> None:
        self.write(checker.DENYLIST_PATH, LEAKS["retired key"])
        self.git("add", checker.DENYLIST_PATH)
        self.assertEqual(checker.scan_repository(self.root), [])

    def test_real_denylist_and_checker_do_not_trip_the_scan(self) -> None:
        here = Path(__file__).resolve().parent
        for name in ("public_surface_denylist.py", "check_public_surface.py", Path(__file__).name):
            with self.subTest(name=name):
                text = (here / name).read_text(encoding="utf-8")
                self.assertEqual(checker.scan_text(name, text), [])

    def test_main_exit_codes(self) -> None:
        self.write("ok.md", "fine\n")
        self.git("add", "ok.md")
        self.assertEqual(checker.main(["--root", str(self.root)]), 0)
        self.write("bad.md", LEAKS["environment option"] + "\n")
        self.git("add", "bad.md")
        self.assertEqual(checker.main(["--root", str(self.root)]), 1)


class BinaryAndHelpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_printable_strings_like_strings_1(self) -> None:
        data = b"\x00\x01abc\x00abcd\xffhello world\x00"
        self.assertEqual(list(checker.printable_strings(data)), ["abcd", "hello world"])

    def test_binary_strings_are_scanned(self) -> None:
        binary = self.root / "prose"
        binary.write_bytes(b"\x7fELF\x00\x00" + LEAKS["retired key"].encode() + b"\x00\x01ok text\x00")
        (finding,) = checker.scan_binary(binary)
        self.assertEqual(finding.line, 0)
        self.assertIn("strings", finding.source)
        clean = self.root / "clean"
        clean.write_bytes(b"\x00OpenProse\x00run_aaaaaaaaaaaaaaaa\x00")
        self.assertEqual(checker.scan_binary(clean), [])

    def test_binary_only_patterns_apply_to_binaries(self) -> None:
        binary = self.root / "prose"
        binary.write_bytes(b"\x00" + BINARY_LEAKS["override"].encode() + b"\x00")
        (finding,) = checker.scan_binary(binary)
        self.assertEqual(finding.pattern, BINARY_ONLY[0].name)

    def test_bun_runtime_strings_are_a_baseline(self) -> None:
        runtime = self.root / "bun"
        runtime.write_bytes(b"\x00" + LEAKS["environment option"].encode() + b"\x00runtime\x00")
        compiled = self.root / "prose-bun"
        compiled.write_bytes(runtime.read_bytes() + b"\x00" + LEAKS["run id"].encode() + b"\x00")
        (finding,) = checker.scan_binary(compiled, runtime)
        self.assertIn("run_", finding.excerpt)
        self.assertEqual(len(checker.scan_binary(compiled)), 2)

    def test_help_walk_covers_every_command_path(self) -> None:
        manifest = {"operations": [{"command": ["run", "submit"]}, {"command": ["org", "member", "list"]},
                                   {"command": ["run", "list"]}]}
        commands = [command[1:] for command in checker.help_commands(Path("prose"), manifest)]
        for expected in (["--help"], ["cli", "--help"], ["cli", "service", "operations", "--json"],
                         ["cli", "service", "capabilities", "--json"], ["cli", "service", "guide"],
                         ["cli", "run", "--help"], ["cli", "run", "submit", "--help"], ["cli", "run", "list", "--help"],
                         ["cli", "org", "member", "--help"], ["cli", "org", "member", "list", "--help"]):
            self.assertIn(expected, commands)
        self.assertEqual(1, commands.count(["cli", "run", "--help"]))
        real = checker.load_manifest(checker.REPOSITORY_ROOT)
        self.assertGreater(len(checker.help_commands(Path("prose"), real)), 60)

    def test_help_topics_add_command_paths(self) -> None:
        help_data = {"topics": {"cli": "", "cli auth": "", "cli auth login": "", "cli run submit": ""}}
        commands = [command[1:] for command in checker.help_commands(Path("prose"), {}, help_data)]
        for expected in (["cli", "auth", "--help"], ["cli", "auth", "login", "--help"],
                         ["cli", "run", "submit", "--help"]):
            self.assertIn(expected, commands)
        real = checker.load_help(checker.REPOSITORY_ROOT)
        self.assertGreater(len(checker.topic_paths(real)), 60)

    def test_discovered_paths_parse_every_help_form(self) -> None:
        text = "\n".join([
            "Runner commands:",
            "  cli doctor                 Report readiness",
            "  cli harness use <ID>       Persist a default harness",
            "Hosted OpenProse service commands:",
            "  prose cli org member list|role|remove [--json]",
            "Commands:",
            "  member                      Commands: list, remove.",
            "  invitation                  Commands: accept.",
            "",
            "Examples:",
            "  prose cli result list exowner1/hello --json",
        ])
        paths = checker.discovered_paths(text, ("org",))
        for expected in (("doctor",), ("harness",), ("harness", "use"), ("org",), ("org", "member"),
                         ("org", "member", "list"), ("org", "member", "role"), ("org", "member", "remove"),
                         ("org", "invitation"), ("result", "list")):
            self.assertIn(expected, paths)
        self.assertNotIn(("result", "list", "exowner1/hello"), paths)

    def test_walk_follows_every_named_command(self) -> None:
        # A fake binary whose deepest help (reachable only from another help
        # text, never from the manifest) leaks.
        script = self.root / "fake-prose"
        script.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  'cli --help') printf 'Commands:\\n  vault                Commands: seal.\\n' ;;\n"
            "  'cli vault --help') printf 'Commands:\\n  seal                 Seal it.\\n' ;;\n"
            "  'cli vault seal --help') printf 'Usage: cli vault seal\\n  cli hidden deep\\n' ;;\n"
            "  'cli hidden deep --help') printf 'see " + LEAKS["run id"] + "\\n' ;;\n"
            "  *) printf 'Usage: prose\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        findings, count = checker.walk_help(script, {}, {})
        self.assertGreaterEqual(count, 10)
        self.assertEqual(["real-looking run id"], [finding.pattern for finding in findings])
        self.assertIn("cli hidden deep --help", findings[0].source)

    def test_release_rustflags_remap_source_and_cargo_home(self) -> None:
        flags = checker.release_rustflags(self.root)
        self.assertIn(f"--remap-path-prefix={self.root.resolve()}=/openprose-source", flags)
        self.assertIn("=/cargo-home", flags)

    def test_missing_binary_and_help_command_are_findings(self) -> None:
        missing = self.root / "absent"
        (finding,) = checker.scan_binary(missing)
        self.assertEqual(finding.pattern, "binary unreadable")
        (finding,) = checker.scan_help([str(missing), "--help"])
        self.assertEqual(finding.pattern, "help command failed")

    def fake_help(self, text: str, exit_code: int = 0) -> Path:
        script = self.root / "fake-prose"
        script.write_text(
            "#!/bin/sh\ncat <<'EOF'\n" + text + "\nEOF\nexit " + str(exit_code) + "\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_help_output_is_scanned_with_the_help_only_patterns(self) -> None:
        script = self.fake_help("Usage: prose cli\nSelect " + STAGE + " here")
        findings = checker.scan_help([str(script), "--help"])
        self.assertEqual([finding.pattern for finding in findings], [HELP_ONLY[0].name])

    def test_clean_help_passes_and_failing_help_is_reported(self) -> None:
        clean = self.fake_help("Usage: prose [--output MODE] cli <COMMAND>")
        self.assertEqual(checker.scan_help([str(clean), "--help"]), [])
        failing = self.fake_help("Usage", exit_code=3)
        (finding,) = checker.scan_help([str(failing), "--help"])
        self.assertEqual(finding.pattern, "help command failed")

    def test_main_scans_help_binaries_without_files(self) -> None:
        script = self.fake_help("Usage: prose cli " + LEAKS["environment option"])
        self.assertEqual(
            checker.main(["--root", str(self.root), "--no-files", "--help-binary", str(script)]),
            1,
        )
        clean = self.fake_help("Usage: prose cli")
        self.assertEqual(
            checker.main(["--root", str(self.root), "--no-files", "--help-binary", str(clean)]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
