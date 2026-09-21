from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli"
GUIDE = CLI / "CONTRIBUTING.md"
HARNESS_TEMPLATE = ROOT / ".github/ISSUE_TEMPLATE/openprose-cli-harness-model.yml"
BENCHMARK_TEMPLATE = ROOT / ".github/ISSUE_TEMPLATE/openprose-cli-benchmark-profile.yml"
BUG_TEMPLATE = ROOT / ".github/ISSUE_TEMPLATE/openprose-cli-bug.yml"
SUPPORT = CLI / "SUPPORT.md"
RELEASE_GUIDE = CLI / "release" / "README.md"
READINESS = CLI / "release" / "ALPHA_READINESS.md"
MIGRATION = CLI / "release" / "MIGRATION_AND_ROLLBACK.md"
CHANGELOG = CLI / "CHANGELOG.md"


class CliContributorDocumentationTests(unittest.TestCase):
    def test_cli_readme_links_first_five_minutes_and_support(self) -> None:
        readme = (CLI / "README.md").read_text(encoding="utf-8")
        first_five_link = "[First five minutes](#first-five-minutes)"
        support_link = "[Support and report a CLI problem](SUPPORT.md)"
        self.assertIn(first_five_link, readme)
        self.assertIn(support_link, readme)
        self.assertLess(
            readme.index(first_five_link), readme.index("## Current status")
        )
        first_five = readme.split("## First five minutes", maxsplit=1)[1].split(
            "\n## ", maxsplit=1
        )[0]
        first_five_flat = " ".join(first_five.split())
        self.assertIn(
            "[Install the functional alpha](#install-the-functional-alpha)",
            first_five_flat,
        )
        self.assertIn(
            "[Try the functional-alpha transport "
            "smoke](#try-the-functional-alpha-transport-smoke)",
            first_five_flat,
        )
        self.assertIn(support_link, first_five_flat)

    def test_cli_readme_has_an_honest_first_install_path(self) -> None:
        readme = (CLI / "README.md").read_text(encoding="utf-8")
        self.assertTrue(readme.startswith("# OpenProse CLI\n"))
        self.assertNotIn("# OpenProse CLI bake-off", readme)
        install = readme.split("## Install the functional alpha", maxsplit=1)[1].split(
            "\n## ", maxsplit=1
        )[0]
        install_flat = " ".join(install.split())
        for marker in (
            "Do not infer availability from this source tree.",
            "A GitHub prerelease in this repository that lists the exact artifact "
            "and `SHA256SUMS` establishes the standalone route.",
            "The public npm registry's exact version and `alpha` dist-tag "
            "establish the registry routes.",
            "When an authorized functional alpha is available",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, install_flat)
        for marker in (
            "GitHub Release",
            "`SHA256SUMS`",
            "@openprose/prose-cli@alpha",
            "read -r ALPHA_VERSION",
            "invalid functional-alpha version",
            "Node.js 22.22.3 or newer",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, install)
        for prohibited in (
            "No artifact produced by this new CLI implementation has been published",
            "No artifact produced by this implementation is public yet",
            "a GitHub Release, npm package, or npm distribution tag is available now",
            "It has not published an artifact from this implementation",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, " ".join(readme.split()))
        self.assertLess(
            readme.index("## Install the functional alpha"),
            readme.index("## Try the functional-alpha transport smoke"),
        )
        self.assertNotIn("replace-with-an-exact-published-alpha-version", install)
        validation = "grep -Eq '^(0|[1-9][0-9]*)"
        exact_install = '"@openprose/prose-cli@$ALPHA_VERSION"'
        self.assertIn(validation, install)
        self.assertLess(install.index(validation), install.index(exact_install))

    def test_install_routes_carry_one_prefix_into_a_version_neutral_first_run(
        self,
    ) -> None:
        readme = (CLI / "README.md").read_text(encoding="utf-8")
        install = readme.split("## Install the functional alpha", maxsplit=1)[1].split(
            "\n## ", maxsplit=1
        )[0]
        first_run = readme.split(
            "## Try the functional-alpha transport smoke", maxsplit=1
        )[1].split("\n## ", maxsplit=1)[0]
        self.assertIn(
            "ALPHA_VERSION=$(npm view --silent '@openprose/prose-cli@alpha' version)",
            install,
        )
        self.assertIn(
            'ALPHA_PREFIX="$HOME/.local/openprose-cli-$ALPHA_VERSION"', install
        )
        self.assertIn('--prefix "$ALPHA_PREFIX"', install)
        self.assertNotIn('@openprose/prose-cli@alpha\n', install)
        self.assertIn('PROSE="$ALPHA_PREFIX/bin/prose"', first_run)
        self.assertIn(
            'EXAMPLE="$ALPHA_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
            first_run,
        )
        self.assertNotIn("0.15.0-alpha.1", first_run)

        exact_shell = (
            install.split("For a reproducible installation", maxsplit=1)[1]
            .split("```sh", maxsplit=1)[1]
            .split("```", maxsplit=1)[0]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            capture = root / "npm-argv"
            npm = fake_bin / "npm"
            npm.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURE"\n', encoding="utf-8"
            )
            npm.chmod(0o755)
            environment = {
                "HOME": str(root / "home"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "CAPTURE": str(capture),
            }
            script = root / "install-exact-alpha.sh"
            script.write_text(
                exact_shell + "\nprintf 'PREFIX=%s\\n' \"$ALPHA_PREFIX\"\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/bin/sh", str(script)],
                input="23.4.5-alpha.87\n",
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                f"PREFIX={root / 'home' / '.local/openprose-cli-23.4.5-alpha.87'}",
                result.stdout,
            )
            self.assertEqual(
                capture.read_text("utf-8").splitlines(),
                [
                    "install",
                    "--global",
                    "--ignore-scripts",
                    "--prefix",
                    str(root / "home" / ".local/openprose-cli-23.4.5-alpha.87"),
                    "@openprose/prose-cli@23.4.5-alpha.87",
                ],
            )

        moving_shell = (
            install.split("To follow an available moving alpha channel", maxsplit=1)[1]
            .split("```sh", maxsplit=1)[1]
            .split("```", maxsplit=1)[0]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            capture = root / "npm-install-argv"
            npm = fake_bin / "npm"
            npm.write_text(
                "#!/bin/sh\n"
                "if test \"$1\" = view; then\n"
                "  printf '%s\\n' \"$ALPHA_VIEW_VERSION\"\n"
                "  exit 0\n"
                "fi\n"
                "printf '%s\\n' \"$@\" > \"$CAPTURE\"\n",
                encoding="utf-8",
            )
            npm.chmod(0o755)
            environment = {
                "HOME": str(root / "home"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "CAPTURE": str(capture),
                "ALPHA_VIEW_VERSION": "23.4.5-alpha.88",
            }
            script = root / "install-moving-alpha.sh"
            script.write_text(
                moving_shell + "\nprintf 'PREFIX=%s\\n' \"$ALPHA_PREFIX\"\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/bin/sh", str(script)],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                f"PREFIX={root / 'home' / '.local/openprose-cli-23.4.5-alpha.88'}",
                result.stdout,
            )
            self.assertEqual(
                capture.read_text("utf-8").splitlines(),
                [
                    "install",
                    "--global",
                    "--ignore-scripts",
                    "--prefix",
                    str(root / "home" / ".local/openprose-cli-23.4.5-alpha.88"),
                    "@openprose/prose-cli@23.4.5-alpha.88",
                ],
            )

            capture.unlink()
            for invalid_version in (
                "23.4.5",
                "23.4.5-alpha.88\n23.4.5-alpha.89",
                "23.4.5-alpha.88;touch unexpected",
            ):
                with self.subTest(invalid_version=invalid_version):
                    environment["ALPHA_VIEW_VERSION"] = invalid_version
                    invalid = subprocess.run(
                        ["/bin/sh", str(script)],
                        text=True,
                        capture_output=True,
                        env=environment,
                        check=False,
                    )
                    self.assertNotEqual(invalid.returncode, 0)
                    self.assertIn("invalid functional-alpha version", invalid.stderr)
                    self.assertFalse(capture.exists())

    def test_clean_clone_setup_selects_rust_1_87_for_root_run_checks(self) -> None:
        guide = GUIDE.read_text(encoding="utf-8")
        start = guide.split("## Start here", maxsplit=1)[1].split(
            "\n## ", maxsplit=1
        )[0]
        install = (
            "rustup toolchain install 1.87.0 --profile minimal "
            "--component clippy,rustfmt"
        )
        selector = "export RUSTUP_TOOLCHAIN=1.87.0"
        dependency_check = "python3 cli/ci/check_dependencies.py"
        quick = "python3 cli/ci/run_local.py --quick"
        for marker in (install, selector, dependency_check, quick):
            self.assertIn(marker, start)
        self.assertLess(start.index(install), start.index(selector))
        self.assertLess(start.index(selector), start.index(dependency_check))
        self.assertLess(start.index(selector), start.index(quick))
        self.assertNotIn("rustup override set", start)

    def test_public_guidance_has_actionable_harness_sign_in_boundaries(self) -> None:
        readme = " ".join((CLI / "README.md").read_text("utf-8").split())
        for marker in (
            "`codex login`",
            "`claude auth login`",
            "start the installed `prime-agent` harness separately",
            "start the installed `omp` harness separately",
            "OpenProse CLI never invokes or controls that TUI",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, readme)

    def test_changelog_requires_an_exact_dated_section_before_tagging(self) -> None:
        changelog = " ".join(CHANGELOG.read_text("utf-8").split())
        for marker in (
            "Before creating `cli-vX.Y.Z-alpha.N`",
            "`## [X.Y.Z-alpha.N] — YYYY-MM-DD`",
            "must not remain only under `[Unreleased]`",
            "This changelog entry does not claim that an artifact is published",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, changelog)
        self.assertNotIn("This unreleased entry", changelog)

    def test_cli_readme_places_platform_support_beside_harness_switching(self) -> None:
        readme = (CLI / "README.md").read_text(encoding="utf-8")
        first_use = readme.split(
            "## Try the functional-alpha transport smoke", maxsplit=1
        )[1].split("\n## ", maxsplit=1)[0]
        table = (
            "| macOS Apple silicon | Prime Agent, OMP, Codex CLI, Claude Code |",
            "| macOS Intel | Codex CLI |",
            "| Linux x64 with glibc 2.34 or newer | Codex CLI, OMP |",
            "| Linux ARM64 with glibc 2.34 or newer | Codex CLI |",
            "| Windows, Linux with musl, and other platforms | Not supported |",
        )
        for row in table:
            with self.subTest(row=row):
                self.assertIn(row, first_use)
        guard = "Choose only a harness listed for your platform."
        switch = "To use another admitted harness"
        self.assertIn(guard, first_use)
        self.assertIn(switch, first_use)
        self.assertLess(first_use.index(guard), first_use.index(switch))

    def test_cli_readme_routes_contributors_to_the_cli_guide(self) -> None:
        readme = (CLI / "README.md").read_text(encoding="utf-8")
        self.assertIn("[Contributing to the CLI](CONTRIBUTING.md)", readme)

    def test_public_guides_link_the_alpha_readiness_authority(self) -> None:
        readme = (CLI / "README.md").read_text(encoding="utf-8")
        release_guide = RELEASE_GUIDE.read_text(encoding="utf-8")
        self.assertIn(
            "[functional-alpha readiness contract](release/ALPHA_READINESS.md)",
            readme,
        )
        self.assertIn(
            "[functional-alpha readiness contract](ALPHA_READINESS.md)",
            release_guide,
        )

    def test_alpha_readiness_orders_authorities_and_nonclaims(self) -> None:
        readiness = READINESS.read_text(encoding="utf-8")
        ordered_headings = (
            "## Candidate-ready requirements",
            "## Draft-ready requirements",
            "## Public-alpha-ready requirements",
            "## Post-publication verification",
            "## Claims the functional alpha does not make",
        )
        positions = [readiness.index(heading) for heading in ordered_headings]
        self.assertEqual(positions, sorted(positions))

        readiness_flat = " ".join(readiness.split())
        for marker in (
            "| Candidate-ready |",
            "| Draft-ready |",
            "| Public-alpha-ready |",
            "| Post-publication verified |",
            "Repository-level README, release, contribution, terms, and privacy "
            "guidance",
            "repository Terms apply to downloadable MIT packages",
            "privacy disclosure",
            "dependency-license review",
            "Vulnerability review",
            "ad-hoc code signatures",
            "no Apple notarization",
            "contributor license agreement, Developer Certificate of Origin, or "
            "neither",
            "GitHub artifact attestations",
            "registry provenance",
            "Redownload every GitHub asset",
            "withdraw and supersede",
            "does not establish OpenProse execution, semantic conformance, program "
            "portability",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, readiness_flat)

    def test_promotion_runbook_requires_public_alpha_readiness_authority(self) -> None:
        migration = " ".join(MIGRATION.read_text(encoding="utf-8").split())
        for marker in (
            "ALPHA_READINESS.md",
            "Public-alpha-ready",
            "root README, release, contribution, Terms, and privacy guidance",
            "dependency-license review",
            "vulnerability review",
            "macOS signing and notarization posture",
            "CLA, DCO, or neither",
            "GitHub artifact attestations",
            "npm registry provenance",
            "Source code and workflow tests do not prove these external controls",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, migration)

    def test_cli_readme_warns_before_the_first_real_harness_run(self) -> None:
        readme = (CLI / "README.md").read_text(encoding="utf-8")
        section = readme.split(
            "## Try the functional-alpha transport smoke", maxsplit=1
        )[1].split("\n## ", maxsplit=1)[0]
        warning = (
            "The run command contacts the selected provider and may incur charges "
            "under the\nsigned-in account. The CLI cannot determine the account or "
            "billing route."
        )
        self.assertIn(warning, section)
        self.assertLess(section.index(warning), section.index('"$PROSE" run'))

    def test_guide_preserves_language_and_provider_safety_boundaries(self) -> None:
        guide = GUIDE.read_text(encoding="utf-8")
        required = (
            "must not parse OpenProse or introduce language semantics",
            "python3 cli/ci/run_local.py --quick",
            "Node.js 22.22.3 or newer",
            "The local admission commands below currently require macOS or Linux.",
            "Native\nWindows admission fails before it starts a child process",
            "bun install --cwd cli/bun --frozen-lockfile",
            "cargo fetch --manifest-path cli/rust/Cargo.toml --locked",
            "cli/protocol/OWNERSHIP.md",
            "If you work in your own\n   fork or branch, do not edit `OWNERSHIP.md`",
            "Provider-free tests make no network or model calls.",
            "No secret, private path, or raw provider response",
            "https://github.com/openprose/prose-cli/security/advisories/new",
            "Do not file a public issue or harness/model request",
            "Acceptance into the adapter inventory is not acceptance into a benchmark.",
            "## Implement a harness adapter",
            "fixed seed",
            "Do not skip a failed gate",
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, guide)

    def test_harness_request_template_is_lightweight_and_exact(self) -> None:
        template = json.loads(HARNESS_TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(template["name"], "OpenProse CLI harness or model request")
        self.assertNotIn("labels", template)
        fields = {item.get("id"): item for item in template["body"] if "id" in item}
        required_fields = (
            "request_type",
            "requested_identity",
            "user_journey",
            "distribution_platform",
            "protocol_isolation",
            "authentication_billing",
            "reproduction",
            "upstream_evidence",
            "affiliation",
            "disclosure",
        )
        self.assertEqual(set(fields), set(required_fields))
        for field in required_fields:
            with self.subTest(field=field):
                self.assertIs(fields[field]["validations"]["required"], True)
        disclosure_options = fields["disclosure"]["attributes"]["options"]
        self.assertTrue(disclosure_options)
        self.assertTrue(
            all(option["required"] is True for option in disclosure_options)
        )
        encoded = json.dumps(template, sort_keys=True)
        self.assertEqual(
            fields["request_type"]["attributes"]["options"],
            [
                "New harness adapter",
                "New admitted harness version",
                "Model or authentication route",
            ],
        )
        self.assertLessEqual(len(fields), 10)
        for forbidden in ("YOUR_API_KEY=", "sk-ant-", "sk-proj-"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded)
        for marker in (
            "exact harness version",
            "exact model ID",
            "version command and expected output",
            "suspected security vulnerability",
            "https://github.com/openprose/prose-cli/security/advisories/new",
            "Do not use this form for a benchmark profile or cell",
            "adapter admission, benchmark inclusion, semantic conformance, "
            "portability, and public ranking are separate decisions",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, encoded)
        for benchmark_only_field in (
            "benchmark_design",
            "benchmark_accounting",
            "profile_identity",
            "cell_identity",
        ):
            self.assertNotIn(benchmark_only_field, fields)
        self.assertFalse(HARNESS_TEMPLATE.with_suffix(".md").exists())

    def test_benchmark_request_template_freezes_profile_and_cell_controls(
        self,
    ) -> None:
        template = json.loads(BENCHMARK_TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(
            template["name"], "OpenProse CLI benchmark profile or cell proposal"
        )
        self.assertNotIn("labels", template)
        fields = {item.get("id"): item for item in template["body"] if "id" in item}
        required_fields = (
            "proposal_type",
            "research_question",
            "profile_identity",
            "cell_identity",
            "semantic_contract",
            "execution_controls",
            "stops_retries_cost",
            "evidence_authority",
            "reproduction",
            "upstream_evidence",
            "affiliation",
            "disclosure",
        )
        self.assertEqual(set(fields), set(required_fields))
        for field in required_fields:
            with self.subTest(field=field):
                self.assertIs(fields[field]["validations"]["required"], True)
        self.assertEqual(
            fields["proposal_type"]["attributes"]["options"],
            [
                "New benchmark profile",
                "New cell in a frozen profile",
                "Profile or cell amendment",
            ],
        )
        disclosure_options = fields["disclosure"]["attributes"]["options"]
        self.assertTrue(disclosure_options)
        self.assertTrue(
            all(option["required"] is True for option in disclosure_options)
        )
        encoded = json.dumps(template, sort_keys=True)
        for forbidden in ("YOUR_API_KEY=", "sk-ant-", "sk-proj-"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded)
        for marker in (
            "frozen benchmark profile",
            "exact admitted adapter",
            "exact target artifact digest",
            "fixed seed",
            "including failed attempts",
            "cost observation channel",
            "does not authorize live collection",
            "suspected security vulnerability",
            "https://github.com/openprose/prose-cli/security/advisories/new",
            "benchmark inclusion does not establish semantic conformance, "
            "portability, or public ranking",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, encoded)
        self.assertFalse(BENCHMARK_TEMPLATE.with_suffix(".md").exists())

    def test_contributor_guide_routes_harness_and_benchmark_intake_separately(
        self,
    ) -> None:
        guide = GUIDE.read_text(encoding="utf-8")
        harness_heading = "## Propose a harness, model, or admitted version"
        benchmark_heading = "## Propose a benchmark profile or cell"
        authority_heading = "## Keep acceptance decisions separate"
        self.assertIn(harness_heading, guide)
        self.assertIn(benchmark_heading, guide)
        self.assertIn(authority_heading, guide)
        self.assertLess(guide.index(harness_heading), guide.index(benchmark_heading))
        self.assertLess(guide.index(benchmark_heading), guide.index(authority_heading))
        harness_section = guide.split(harness_heading, maxsplit=1)[1].split(
            "\n## ", maxsplit=1
        )[0]
        benchmark_section = guide.split(benchmark_heading, maxsplit=1)[1].split(
            "\n## ", maxsplit=1
        )[0]
        harness_flat = " ".join(harness_section.split())
        benchmark_flat = " ".join(benchmark_section.split())
        self.assertIn("openprose-cli-harness-model.yml", harness_section)
        self.assertIn(
            "Do not use this form to propose a benchmark profile or cell.",
            harness_flat,
        )
        self.assertIn("openprose-cli-benchmark-profile.yml", benchmark_section)
        self.assertIn(
            "Do not use this form to request adapter admission or a new admitted "
            "version.",
            benchmark_flat,
        )
        guide_flat = " ".join(guide.split())
        for marker in (
            "Adapter admission establishes only",
            "Benchmark inclusion establishes only",
            "Semantic conformance requires",
            "Portability requires",
            "Public ranking requires",
            "A benchmark proposal does not authorize live collection.",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, guide_flat)

    def test_general_cli_bug_template_is_distinct_and_privacy_safe(self) -> None:
        harness_template = json.loads(HARNESS_TEMPLATE.read_text(encoding="utf-8"))
        template = json.loads(BUG_TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(template["name"], "OpenProse CLI bug or install problem")
        self.assertNotEqual(template["name"], harness_template["name"])
        self.assertNotEqual(template["title"], harness_template["title"])
        self.assertNotIn("labels", template)
        fields = {item.get("id"): item for item in template["body"] if "id" in item}
        required_fields = {
            "problem_area",
            "environment",
            "cli_version",
            "reproduction",
            "expected",
            "actual",
            "diagnostics",
            "disclosure",
        }
        self.assertEqual(set(fields), required_fields)
        for field in required_fields:
            with self.subTest(field=field):
                self.assertIs(fields[field]["validations"]["required"], True)
        disclosure = fields["disclosure"]["attributes"]["options"]
        self.assertTrue(disclosure)
        self.assertTrue(all(option["required"] is True for option in disclosure))
        encoded = json.dumps(template, sort_keys=True)
        for marker in (
            "Do not include credentials, tokens, account identifiers, private paths, "
            "or raw provider output.",
            "Use the literal word REDACTED",
            "https://github.com/openprose/prose-cli/security/advisories/new",
            "not a suspected security vulnerability",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, encoded)
        for forbidden in ("YOUR_API_KEY=", "sk-ant-", "sk-proj-", "<private-path>"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded)

    def test_support_policy_states_alpha_compatibility_and_triage_boundaries(
        self,
    ) -> None:
        support = SUPPORT.read_text(encoding="utf-8")
        support_flat = " ".join(support.split())
        required = (
            "# OpenProse CLI support",
            "OpenProse CLI bug or install problem",
            "openprose-cli-bug.yml",
            "OpenProse CLI harness or model request",
            "openprose-cli-harness-model.yml",
            "OpenProse CLI benchmark profile or cell proposal",
            "openprose-cli-benchmark-profile.yml",
            "https://github.com/openprose/prose-cli/security/advisories/new",
            "Do not include credentials, tokens, account identifiers, private paths, "
            "or raw provider output",
            "No response or resolution service-level agreement is promised",
            "## Functional-alpha compatibility",
            "It does not claim that an alpha artifact or package is currently "
            "published",
            "Supported package and harness combinations",
            "Best-effort environments",
            "Unsupported environments and versions",
            "macOS Apple silicon",
            "Linux x64 with glibc 2.34 or newer",
            "Bun standalone and npm packages require macOS 13 or newer",
            "npm launcher requires Node.js 22.22.3 or newer",
            "Prime `0.7.0` and `0.8.1`",
            "OMP `18.0.9` with Bun `1.3.14` or newer",
            "Codex `0.149.0-alpha.4.1`",
            "Claude `2.1.243`",
            "may be withdrawn",
            "superseded",
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, support_flat)
        for unsupported in (
            "Windows",
            "Linux with musl",
            "Nearby harness versions are not inferred compatible",
        ):
            self.assertIn(unsupported, support_flat)
        for unsupported_promise in (
            "within one business day",
            "within two business days",
            "guaranteed response",
        ):
            self.assertNotIn(unsupported_promise, support.lower())

        intake = support.split("Do not include credentials", maxsplit=1)[0]
        self.assertIn(
            "new adapter, admitted harness version, or model route",
            intake,
        )
        self.assertIn("benchmark profile or cell", intake)
        self.assertNotIn(
            "new adapter, admitted harness version, model route, or benchmark cell",
            intake,
        )

    def test_support_has_a_safe_actionable_alpha_troubleshooting_playbook(
        self,
    ) -> None:
        support = SUPPORT.read_text(encoding="utf-8")
        after_heading = support.split(
            "## Troubleshoot the functional alpha", maxsplit=1
        )[1]
        playbook = after_heading.split("\n## ", maxsplit=1)[0]
        playbook_flat = " ".join(playbook.split())

        required = (
            "### Start with the exact executable and machine output",
            "Do not use an ambient `prose` executable",
            "These commands do not start a model run",
            "PROSE='/absolute/path/to/the/selected/prose'",
            '"$PROSE" --version',
            '"$PROSE" --output json cli config explain',
            '"$PROSE" --output json cli harness list',
            '"$PROSE" --output json cli doctor',
            "A nonzero exit can still accompany a schema-valid JSON report",
            "Do not publish these reports unchanged",
            "`HARNESS_UNAVAILABLE`",
            "`HARNESS_INCOMPATIBLE`",
            "exact `Repair` command",
            "packaged README",
            "`codex login`",
            "`claude auth login`",
            "`prime-agent`",
            "`omp`",
            "OpenProse CLI never invokes or controls either TUI",
            "`prime-harness-login`",
            "`omp-harness-login`",
            "fully qualified model",
            "`cli config explain`",
            "`PROSE_HARNESS`",
            "`PROSE_MODEL`",
            "`PROSE_AUTH_PROFILE`",
            "project or environment override",
            "Node.js 22.22.3",
            "glibc 2.34",
            "Windows and Linux with musl are unsupported",
            "Do not bypass a package-integrity refusal",
            "same exact version",
            "`PROCESS_CLEANUP_FAILED`",
            "same `TMPDIR`",
            "CLEANUP_HANDLE='paste-the-opaque-handle-from-the-error'",
            '"$PROSE" --output json cli cleanup prime "$CLEANUP_HANDLE"',
            "Do not stop an unrelated Prime service",
            "`PROTOCOL_MALFORMED`",
            "`PROTOCOL_TRUNCATED`",
            "The wrapper does not perform a hidden retry",
            "retry the latter once",
            "### Decide whether to repair or report",
            "Use the bug form",
            "Use the harness or model request",
            "credentials, tokens, account identifiers, private paths, "
            "raw provider output",
            "stable error code and exit code",
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, playbook_flat)

        after_fence = playbook.split("```sh", maxsplit=1)[1]
        diagnostics = after_fence.split("```", maxsplit=1)[0]
        self.assertNotIn("\nprose ", diagnostics)
        self.assertLess(
            playbook.index("### Start with"),
            playbook.index("`HARNESS_UNAVAILABLE`"),
        )


if __name__ == "__main__":
    unittest.main()
