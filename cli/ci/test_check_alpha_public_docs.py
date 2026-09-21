from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import check_alpha_public_docs as docs


VERSION = "0.15.0-alpha.1"


def write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8", newline="\n")


def valid_documents(root: Path, version: str = VERSION) -> None:
    write(
        root / "README.md",
        """# OpenProse

The interactive OpenProse skill path remains independent of the optional
downloadable CLI. See the [OpenProse CLI](cli/README.md) for installation and
noninteractive harness execution.
""",
    )
    write(
        root / "RELEASE.md",
        """# Release process

The downloadable CLI has an independent functional-alpha release train using
`cli-vX.Y.Z-alpha.N`. See [CLI release operations](cli/release/README.md).
""",
    )
    write(
        root / "CONTRIBUTING.md",
        """# Contributing

Route CLI changes to the [CLI contribution guide](cli/CONTRIBUTING.md).

Contribution governance: neither CLA nor DCO.
""",
    )
    write(
        root / "PRIVACY.md",
        """# Privacy

The CLI adds no OpenProse-operated telemetry. Task data is sent to the
user-selected harness and provider when a run is started.
""",
    )
    write(
        root / "TERMS.md",
        """# Terms

These terms apply to the downloadable OpenProse CLI packages.
        """,
    )
    write(
        root / "LICENSE",
        """MIT License

Copyright (c) 2025 OpenProse

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction.

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
""",
    )
    write(root / "cli" / "README.md", "# OpenProse CLI\n")
    write(root / "cli" / "release" / "README.md", "# CLI release operations\n")
    write(root / "cli" / "CONTRIBUTING.md", "# Contributing to the CLI\n")
    write(
        root / "cli" / "SUPPORT.md",
        """# OpenProse CLI support

- Use the [OpenProse CLI harness or model request](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-harness-model.yml)
  form for a new adapter, admitted harness version, model route, or authentication
  route.
- Use the [OpenProse CLI benchmark profile or cell proposal](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-benchmark-profile.yml)
  form for a benchmark profile or cell.
- Report a suspected vulnerability only through [private vulnerability
  reporting](https://github.com/openprose/prose-cli/security/advisories/new).

Do not include credentials, tokens, account identifiers, private paths, or raw
provider output in a public issue.
""",
    )
    write(
        root / ".github" / "ISSUE_TEMPLATE" / "openprose-cli-harness-model.yml",
        json.dumps(
            {
                "name": "OpenProse CLI harness or model request",
                "description": (
                    "Request a new harness adapter, admitted harness version, "
                    "or model or authentication route"
                ),
                "title": "[CLI harness/model request]: ",
                "body": [
                    {
                        "type": "markdown",
                        "attributes": {
                            "value": (
                                "Use this form for adapter admission, an exact "
                                "admitted harness version, or a model/authentication "
                                "route. Do not "
                                "use this form for a benchmark profile or cell. Do not "
                                "include credentials, tokens, account identifiers, "
                                "private "
                                "paths, or raw provider transcripts. Do not report a "
                                "suspected security vulnerability here; use the "
                                "private "
                                "vulnerability report at https://github.com/openprose/prose-cli/"
                                "security/advisories/new."
                            )
                        },
                    },
                    {
                        "type": "dropdown",
                        "id": "request_type",
                        "attributes": {
                            "label": "Request type",
                            "options": [
                                "New harness adapter",
                                "New admitted harness version",
                                "Model or authentication route",
                            ],
                        },
                        "validations": {"required": True},
                    },
                    {
                        "type": "checkboxes",
                        "id": "disclosure",
                        "attributes": {
                            "label": "Disclosure",
                            "options": [
                                {
                                    "label": (
                                        "I removed credentials, account identifiers, "
                                        "private paths, and raw provider output."
                                    ),
                                    "required": True,
                                }
                            ],
                        },
                        "validations": {"required": True},
                    },
                ],
            },
            indent=2,
        )
        + "\n",
    )
    write(
        root / ".github" / "ISSUE_TEMPLATE" / "openprose-cli-benchmark-profile.yml",
        json.dumps(
            {
                "name": "OpenProse CLI benchmark profile or cell proposal",
                "description": (
                    "Propose a frozen benchmark profile or one exact cell for "
                    "separate review"
                ),
                "title": "[CLI benchmark proposal]: ",
                "body": [
                    {
                        "type": "markdown",
                        "attributes": {
                            "value": (
                                "Use this form only for a rigorous benchmark profile "
                                "or cell proposal. A benchmark proposal does not "
                                "authorize "
                                "live collection. Do not include credentials, tokens, "
                                "account identifiers, private paths, or raw provider "
                                "transcripts. Do not report a suspected security "
                                "vulnerability here; use the private vulnerability "
                                "report "
                                "at https://github.com/openprose/prose-cli/security/advisories/"
                                "new."
                            )
                        },
                    },
                    {
                        "type": "dropdown",
                        "id": "proposal_type",
                        "attributes": {
                            "label": "Proposal type",
                            "options": [
                                "New benchmark profile",
                                "New cell in a frozen profile",
                                "Profile or cell amendment",
                            ],
                        },
                        "validations": {"required": True},
                    },
                    {
                        "type": "checkboxes",
                        "id": "disclosure",
                        "attributes": {
                            "label": "Disclosure",
                            "options": [
                                {
                                    "label": (
                                        "I removed credentials, account identifiers, "
                                        "private paths, and raw provider output."
                                    ),
                                    "required": True,
                                }
                            ],
                        },
                        "validations": {"required": True},
                    },
                ],
            },
            indent=2,
        )
        + "\n",
    )
    write(
        root / "cli" / "CHANGELOG.md",
        f"""# OpenProse CLI changelog

## [Unreleased]

## [{version}] — 2026-09-01

- First functional alpha.
""",
    )


def failures(report: dict[str, object]) -> set[tuple[str, str, str]]:
    return {
        (item["code"], item["document"], item["check"])
        for item in report["failures"]  # type: ignore[index,union-attr]
    }


class AlphaPublicDocsTest(unittest.TestCase):
    def test_exact_public_documents_pass_with_closed_sanitized_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            report = docs.assess(version=VERSION, repository_root=root)
            encoded = json.dumps(report, sort_keys=True)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            set(report),
            {
                "schema",
                "status",
                "version",
                "releaseOnly",
                "publicationAuthorized",
                "documents",
                "checks",
                "failures",
            },
        )
        self.assertEqual(set(report["documents"]), set(docs.DOCUMENTS))
        self.assertEqual(set(report["checks"]), set(docs.CHECKS))
        self.assertEqual(report["failures"], [])
        self.assertTrue(report["releaseOnly"])
        self.assertFalse(report["publicationAuthorized"])
        self.assertNotIn(str(root), encoded)
        self.assertNotIn("First functional alpha", encoded)
        self.assertNotIn("openprose-cli-harness-model.yml", encoded)
        self.assertNotIn("security/advisories/new", encoded)
        for value in report["documents"].values():
            self.assertEqual(set(value), {"status", "byteLength", "sha256"})
            self.assertEqual(value["status"], "authenticated")
            self.assertGreater(value["byteLength"], 0)
            self.assertRegex(value["sha256"], r"^[0-9a-f]{64}$")

    def test_cli_emits_only_one_closed_json_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            output: list[str] = []
            status = docs.main(
                ["--version", VERSION, "--repository-root", str(root)],
                emit=output.append,
            )
        self.assertEqual(status, 0)
        self.assertEqual(len(output), 1)
        self.assertEqual(json.loads(output[0])["status"], "pass")

    def test_changelog_requires_one_exact_real_dated_version_section(self) -> None:
        mutations = {
            "missing": "# Changelog\n\n## [Unreleased]\n",
            "duplicate": (
                f"# Changelog\n\n## [{VERSION}] — 2026-09-01\n"
                f"\n## [{VERSION}] — 2026-09-02\n"
            ),
            "exact-plus-malformed-duplicate": (
                f"# Changelog\n\n## [{VERSION}] — 2026-09-01\n"
                f"\n## [{VERSION}] - 2026-09-02\n"
            ),
            "wrong-dash": f"# Changelog\n\n## [{VERSION}] - 2026-09-01\n",
            "invalid-date": f"# Changelog\n\n## [{VERSION}] — 2026-02-30\n",
            "wrong-version": "# Changelog\n\n## [0.15.0-alpha.2] — 2026-09-01\n",
        }
        for name, changelog in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                write(root / "cli" / "CHANGELOG.md", changelog)
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                ("CHANGELOG_SECTION_INVALID", "cli-changelog", "changelog-section"),
                failures(report),
            )

    def test_each_public_contract_fails_closed_when_missing_or_ambiguous(self) -> None:
        mutations = {
            "readme-missing": (
                "README.md",
                "# OpenProse\n\nUse the skill.\n",
                ("README_SURFACES_MISSING", "root-readme", "readme-surfaces"),
            ),
            "readme-duplicate": (
                "README.md",
                "# OpenProse\n\n"
                "The interactive skill and optional downloadable CLI are independent. "
                "See [CLI](cli/README.md).\n\n"
                "The interactive skill and optional downloadable CLI are independent. "
                "See [CLI](cli/README.md).\n",
                ("README_SURFACES_MISSING", "root-readme", "readme-surfaces"),
            ),
            "release": (
                "RELEASE.md",
                "# Release process\n\nOnly the skill is released.\n",
                ("RELEASE_TRAIN_MISSING", "root-release", "release-train"),
            ),
            "contributing-route": (
                "CONTRIBUTING.md",
                "# Contributing\n\nContribution governance: DCO.\n",
                (
                    "CONTRIBUTION_ROUTE_MISSING",
                    "root-contributing",
                    "contribution-route",
                ),
            ),
            "privacy": (
                "PRIVACY.md",
                "# Privacy\n\nNo telemetry.\n",
                ("PRIVACY_BOUNDARY_MISSING", "root-privacy", "privacy-boundary"),
            ),
            "terms": (
                "TERMS.md",
                "# Terms\n\nThese terms cover OpenProse.\n",
                ("TERMS_SCOPE_MISSING", "root-terms", "terms-scope"),
            ),
            "governance": (
                "CONTRIBUTING.md",
                "# Contributing\n\nRoute CLI changes to the "
                "[CLI contribution guide](cli/CONTRIBUTING.md).\n\n"
                "Maintainers will decide whether a CLA or DCO applies.\n",
                (
                    "GOVERNANCE_DECLARATION_MISSING",
                    "root-contributing",
                    "contribution-governance",
                ),
            ),
            "governance-ambiguous": (
                "CONTRIBUTING.md",
                "# Contributing\n\nRoute CLI changes to the "
                "[CLI contribution guide](cli/CONTRIBUTING.md).\n\n"
                "Contribution governance: CLA.\n"
                "Contribution governance: DCO.\n",
                (
                    "GOVERNANCE_DECLARATION_MISSING",
                    "root-contributing",
                    "contribution-governance",
                ),
            ),
        }
        for name, (relative, value, expected) in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                write(root / relative, value)
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(expected, failures(report))

    def test_known_stale_claims_are_rejected(self) -> None:
        mutations = {
            "README.md": "There is no separate binary.",
            "RELEASE.md": "There is no prose-cli release train.",
            "CONTRIBUTING.md": "Harness implementations live outside this repo.",
            "PRIVACY.md": (
                "No usage data, error reports, or environment information "
                "is sent anywhere."
            ),
        }
        for relative, stale in mutations.items():
            with self.subTest(
                relative=relative
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                path = root / relative
                write(path, path.read_text("utf-8") + "\n" + stale + "\n")
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                ("STALE_CLAIM", docs.ROOT_DOCUMENT_IDS[relative], "stale-claims"),
                failures(report),
            )

    def test_each_explicit_terms_and_governance_choice_is_accepted(self) -> None:
        governance = (
            "Contribution governance: CLA.",
            "Contribution governance: DCO.",
            "Contribution governance: neither CLA nor DCO.",
        )
        terms = (
            "These terms apply to the downloadable OpenProse CLI packages.",
            "These terms do not apply to the downloadable OpenProse CLI packages.",
        )
        for governance_value in governance:
            for terms_value in terms:
                with (
                    self.subTest(
                        governance=governance_value,
                        terms=terms_value,
                    ),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    valid_documents(root)
                    write(
                        root / "CONTRIBUTING.md",
                        "# Contributing\n\nRoute CLI changes to the "
                        "[CLI contribution guide](cli/CONTRIBUTING.md).\n\n"
                        f"{governance_value}\n",
                    )
                    write(root / "TERMS.md", f"# Terms\n\n{terms_value}\n")
                    report = docs.assess(version=VERSION, repository_root=root)
                self.assertEqual(report["status"], "pass")

    def test_duplicate_privacy_and_terms_declarations_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            privacy = (root / "PRIVACY.md").read_text("utf-8")
            write(root / "PRIVACY.md", privacy + "\n" + privacy)
            write(
                root / "TERMS.md",
                "# Terms\n\n"
                "These terms apply to the downloadable OpenProse CLI packages.\n\n"
                "These terms do not apply to the downloadable OpenProse CLI "
                "packages.\n",
            )
            report = docs.assess(version=VERSION, repository_root=root)
        observed = failures(report)
        self.assertIn(
            ("PRIVACY_BOUNDARY_MISSING", "root-privacy", "privacy-boundary"),
            observed,
        )
        self.assertIn(("TERMS_SCOPE_MISSING", "root-terms", "terms-scope"), observed)

    def test_license_requires_the_mit_identity_and_material_terms(self) -> None:
        mutations = {
            "wrong-license": "Apache License 2.0\n",
            "missing-grant": (
                "MIT License\n\nCopyright (c) 2025 OpenProse\n\n"
                'THE SOFTWARE IS PROVIDED "AS IS".\n'
            ),
            "missing-notice-condition": (
                "MIT License\n\nCopyright (c) 2025 OpenProse\n\n"
                "Permission is hereby granted, free of charge.\n\n"
                'THE SOFTWARE IS PROVIDED "AS IS".\n'
            ),
            "missing-disclaimer": (
                "MIT License\n\nCopyright (c) 2025 OpenProse\n\n"
                "Permission is hereby granted, free of charge.\n\n"
                "copies or substantial portions of the Software.\n"
            ),
        }
        for name, license_text in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                write(root / "LICENSE", license_text)
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                ("LICENSE_INVALID", "root-license", "license-identity"),
                failures(report),
            )

    def test_support_requires_distinct_intake_and_private_security_routes(self) -> None:
        support = """# Support

- Use the [harness request](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-harness-model.yml)
  for a harness, model, or authentication route.
- Use the [benchmark proposal](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-benchmark-profile.yml)
  for a benchmark profile or cell.
- Report a suspected vulnerability through [private reporting](https://github.com/openprose/prose-cli/security/advisories/new).

Do not include credentials, tokens, account identifiers, private paths, or raw
provider output in a public issue.
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            write(root / "cli" / "SUPPORT.md", support)
            report = docs.assess(version=VERSION, repository_root=root)
        self.assertNotIn(
            ("SUPPORT_ROUTING_INVALID", "cli-support", "support-routing"),
            failures(report),
        )

        mutations = {
            "missing-benchmark-route": support.replace(
                "https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-benchmark-profile.yml",
                "https://github.com/openprose/prose-cli/issues/new",
            ),
            "benchmark-routed-to-harness": support.replace(
                "https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-benchmark-profile.yml",
                "https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-harness-model.yml",
            ),
            "missing-private-security": support.replace(
                "https://github.com/openprose/prose-cli/security/advisories/new",
                "https://github.com/openprose/prose-cli/issues/new",
            ),
            "missing-public-redaction": support.replace(
                "credentials, tokens, account identifiers, private paths, or raw\n"
                "provider output",
                "sensitive values",
            ),
        }
        for name, support_text in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                write(root / "cli" / "SUPPORT.md", support_text)
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                ("SUPPORT_ROUTING_INVALID", "cli-support", "support-routing"),
                failures(report),
            )

    def test_issue_forms_keep_separate_purposes_and_private_reporting(self) -> None:
        mutations = {
            "harness-accepts-benchmarks": (
                "openprose-cli-harness-model.yml",
                "Do not use this form for a benchmark profile or cell.",
                "Use this form for a benchmark profile or cell.",
                (
                    "ISSUE_FORM_ROUTING_INVALID",
                    "cli-harness-model-issue-form",
                    "issue-form-routing",
                ),
            ),
            "benchmark-purpose-removed": (
                "openprose-cli-benchmark-profile.yml",
                "Use this form only for a rigorous benchmark profile or cell proposal.",
                "Use this form for a proposal.",
                (
                    "ISSUE_FORM_ROUTING_INVALID",
                    "cli-benchmark-issue-form",
                    "issue-form-routing",
                ),
            ),
            "harness-security-public": (
                "openprose-cli-harness-model.yml",
                "https://github.com/openprose/prose-cli/security/advisories/new",
                "https://github.com/openprose/prose-cli/issues/new",
                (
                    "ISSUE_FORM_PRIVACY_INVALID",
                    "cli-harness-model-issue-form",
                    "issue-form-privacy",
                ),
            ),
            "benchmark-redaction-removed": (
                "openprose-cli-benchmark-profile.yml",
                "credentials, tokens, account identifiers, private paths, or raw "
                "provider transcripts",
                "sensitive values",
                (
                    "ISSUE_FORM_PRIVACY_INVALID",
                    "cli-benchmark-issue-form",
                    "issue-form-privacy",
                ),
            ),
        }
        for name, (filename, before, after, expected) in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                path = root / ".github" / "ISSUE_TEMPLATE" / filename
                write(path, path.read_text("utf-8").replace(before, after))
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(expected, failures(report))

    def test_issue_forms_must_be_json_objects_with_closed_expected_controls(
        self,
    ) -> None:
        mutations = {
            "malformed": "not-json\n",
            "wrong-title": json.dumps(
                {
                    "name": "OpenProse CLI harness or model request",
                    "description": "Request a route",
                    "title": "[wrong]: ",
                    "body": [],
                }
            ),
        }
        for name, value in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                write(
                    root
                    / ".github"
                    / "ISSUE_TEMPLATE"
                    / "openprose-cli-harness-model.yml",
                    value,
                )
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                (
                    "ISSUE_FORM_ROUTING_INVALID",
                    "cli-harness-model-issue-form",
                    "issue-form-routing",
                ),
                failures(report),
            )

    def test_files_and_internal_link_targets_must_be_safe_regular_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            target = root / "outside.md"
            write(target, "# Outside\n")
            (root / "cli" / "README.md").unlink()
            os.symlink(target, root / "cli" / "README.md")
            report = docs.assess(version=VERSION, repository_root=root)
        self.assertIn(
            ("DOCUMENT_UNSAFE", "cli-readme", "document-authentication"),
            failures(report),
        )
        self.assertEqual(report["checks"]["readme-surfaces"], "fail")

    def test_expanded_release_documents_are_required_exact_regular_files(self) -> None:
        documents = {
            "root-license": "LICENSE",
            "cli-support": "cli/SUPPORT.md",
            "cli-harness-model-issue-form": (
                ".github/ISSUE_TEMPLATE/openprose-cli-harness-model.yml"
            ),
            "cli-benchmark-issue-form": (
                ".github/ISSUE_TEMPLATE/openprose-cli-benchmark-profile.yml"
            ),
        }
        for document, relative in documents.items():
            with (
                self.subTest(document=document),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                valid_documents(root)
                (root / relative).unlink()
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                ("DOCUMENT_UNAVAILABLE", document, "document-authentication"),
                failures(report),
            )
            self.assertEqual(report["documents"][document]["status"], "unavailable")

    def test_symlinked_internal_document_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repository"
            root.mkdir()
            valid_documents(root)
            moved = base / "moved-cli"
            (root / "cli").rename(moved)
            os.symlink(moved, root / "cli")
            report = docs.assess(version=VERSION, repository_root=root)
        self.assertIn(
            ("DOCUMENT_UNSAFE", "cli-readme", "document-authentication"),
            failures(report),
        )

    def test_symlinked_root_is_rejected_without_exposing_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real"
            real.mkdir()
            valid_documents(real)
            link = base / "repository-link"
            os.symlink(real, link)
            report = docs.assess(version=VERSION, repository_root=link)
        encoded = json.dumps(report, sort_keys=True)
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            ("ROOT_UNSAFE", "release-set", "repository-root"), failures(report)
        )
        self.assertNotIn(str(base), encoded)

    def test_malformed_control_non_utf8_and_oversized_text_are_rejected(self) -> None:
        mutations = (
            b"# Privacy\n\ntext\x1b[31m\n",
            b"# Privacy\n\n\xff\n",
            b"# Privacy\r\n\r\ntext\r\n",
            "# Privacy\n\ntext\u202e\n".encode("utf-8"),
            b"# Privacy\n\n" + b"a" * (docs.MAX_DOCUMENT_BYTES + 1),
        )
        for value in mutations:
            with self.subTest(
                length=len(value)
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                valid_documents(root)
                write(root / "PRIVACY.md", value)
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                ("DOCUMENT_MALFORMED", "root-privacy", "document-authentication"),
                failures(report),
            )

    def test_document_read_error_becomes_sanitized_unsafe_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            with mock.patch.object(docs.os, "read", side_effect=OSError("private")):
                report = docs.assess(version=VERSION, repository_root=root)
        encoded = json.dumps(report, sort_keys=True)
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            ("DOCUMENT_UNSAFE", "root-readme", "document-authentication"),
            failures(report),
        )
        self.assertNotIn("private", encoded)

    def test_document_path_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            readme = root / "README.md"
            original_read = docs.os.read
            replaced = False

            def replace_after_read(descriptor: int, maximum: int) -> bytes:
                nonlocal replaced
                result = original_read(descriptor, maximum)
                if result and not replaced:
                    replaced = True
                    readme.unlink()
                    write(readme, "# Replacement\n")
                return result

            with mock.patch.object(docs.os, "read", side_effect=replace_after_read):
                report = docs.assess(version=VERSION, repository_root=root)
        self.assertIn(
            ("DOCUMENT_UNSAFE", "root-readme", "document-authentication"),
            failures(report),
        )

    def test_invalid_input_is_sanitized_and_does_not_read_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            report = docs.assess(version="../../private\nvalue", repository_root=root)
        encoded = json.dumps(report, sort_keys=True)
        self.assertIsNone(report["version"])
        self.assertIn(("INPUT_INVALID", "release-set", "version"), failures(report))
        self.assertNotIn("private", encoded)
        self.assertTrue(
            all(value["status"] == "not-read" for value in report["documents"].values())
        )

    def test_relative_and_parent_traversing_roots_fail_closed(self) -> None:
        for root in (Path("repository"), Path("/tmp/../private/repository")):
            with self.subTest(root=str(root)):
                report = docs.assess(version=VERSION, repository_root=root)
            self.assertIn(
                ("ROOT_UNSAFE", "release-set", "repository-root"),
                failures(report),
            )

    def test_cli_rejects_duplicate_input_flags_as_ambiguous_json(self) -> None:
        output: list[str] = []
        status = docs.main(
            [
                "--version",
                VERSION,
                "--version",
                "0.15.0-alpha.2",
                "--repository-root",
                "/private/repository",
            ],
            emit=output.append,
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(output), 1)
        report = json.loads(output[0])
        self.assertEqual(report["status"], "fail")
        self.assertIsNone(report["version"])
        self.assertNotIn("private", output[0])

    def test_incomplete_release_documents_report_all_owner_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_documents(root)
            incomplete = {
                "README.md": "# OpenProse\n\nThere is no separate binary.\n",
                "RELEASE.md": "# Release process\n",
                "CONTRIBUTING.md": "# Contributing\n",
                "PRIVACY.md": "# Privacy\n",
                "TERMS.md": "# Terms\n",
                "cli/CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n",
            }
            for relative, value in incomplete.items():
                write(root / relative, value)
            report = docs.assess(version=VERSION, repository_root=root)
        observed_checks = {item[2] for item in failures(report)}
        self.assertEqual(report["status"], "fail")
        self.assertEqual(
            observed_checks,
            {
                "changelog-section",
                "readme-surfaces",
                "release-train",
                "contribution-route",
                "privacy-boundary",
                "terms-scope",
                "contribution-governance",
                "stale-claims",
            },
        )


if __name__ == "__main__":
    unittest.main()
