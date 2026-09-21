#!/usr/bin/env python3
"""Check public-document readiness for one functional-alpha CLI release.

This is a release-only, provider-free preflight. It authenticates a fixed set
of repository documents and emits a closed, sanitized JSON result. It neither
changes a document nor authorizes publication.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
from typing import Callable, Sequence


SCHEMA = "openprose.alpha-public-docs-preflight/1"
MAX_DOCUMENT_BYTES = 1024 * 1024
ALPHA = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)" r"-alpha\.(0|[1-9][0-9]*)$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")

DOCUMENTS = {
    "root-readme": "README.md",
    "root-release": "RELEASE.md",
    "root-contributing": "CONTRIBUTING.md",
    "root-privacy": "PRIVACY.md",
    "root-terms": "TERMS.md",
    "root-license": "LICENSE",
    "cli-readme": "cli/README.md",
    "cli-release-readme": "cli/release/README.md",
    "cli-contributing": "cli/CONTRIBUTING.md",
    "cli-support": "cli/SUPPORT.md",
    "cli-changelog": "cli/CHANGELOG.md",
    "cli-harness-model-issue-form": (
        ".github/ISSUE_TEMPLATE/openprose-cli-harness-model.yml"
    ),
    "cli-benchmark-issue-form": (
        ".github/ISSUE_TEMPLATE/openprose-cli-benchmark-profile.yml"
    ),
}
ROOT_DOCUMENT_IDS = {
    "README.md": "root-readme",
    "RELEASE.md": "root-release",
    "CONTRIBUTING.md": "root-contributing",
    "PRIVACY.md": "root-privacy",
    "TERMS.md": "root-terms",
    "LICENSE": "root-license",
}
CHECKS = (
    "repository-root",
    "version",
    "document-authentication",
    "changelog-section",
    "readme-surfaces",
    "release-train",
    "contribution-route",
    "privacy-boundary",
    "terms-scope",
    "contribution-governance",
    "license-identity",
    "support-routing",
    "issue-form-routing",
    "issue-form-privacy",
    "stale-claims",
)
FAILURE_CODES = frozenset(
    {
        "INPUT_INVALID",
        "ROOT_UNSAFE",
        "DOCUMENT_UNAVAILABLE",
        "DOCUMENT_UNSAFE",
        "DOCUMENT_MALFORMED",
        "CHANGELOG_SECTION_INVALID",
        "README_SURFACES_MISSING",
        "RELEASE_TRAIN_MISSING",
        "CONTRIBUTION_ROUTE_MISSING",
        "PRIVACY_BOUNDARY_MISSING",
        "TERMS_SCOPE_MISSING",
        "GOVERNANCE_DECLARATION_MISSING",
        "LICENSE_INVALID",
        "SUPPORT_ROUTING_INVALID",
        "ISSUE_FORM_ROUTING_INVALID",
        "ISSUE_FORM_PRIVACY_INVALID",
        "STALE_CLAIM",
    }
)

_README_LINK = re.compile(r"(?<!!)\[[^\]\r\n]+\]\(cli/README\.md\)")
_RELEASE_LINK = re.compile(r"(?<!!)\[[^\]\r\n]+\]\(cli/release/README\.md\)")
_CONTRIBUTING_LINK = re.compile(r"(?<!!)\[[^\]\r\n]+\]\(cli/CONTRIBUTING\.md\)")
HARNESS_MODEL_FORM_URL = (
    "https://github.com/openprose/prose-cli/issues/new?"
    "template=openprose-cli-harness-model.yml"
)
BENCHMARK_FORM_URL = (
    "https://github.com/openprose/prose-cli/issues/new?"
    "template=openprose-cli-benchmark-profile.yml"
)
PRIVATE_VULNERABILITY_URL = "https://github.com/openprose/prose-cli/security/advisories/new"
_CLI_ALPHA_TAG = re.compile(
    r"\bcli-v(?:x\.y\.z|[0-9]+\.[0-9]+\.[0-9]+)-alpha\." r"(?:n|[0-9]+)\b",
    re.IGNORECASE,
)
_GOVERNANCE_DECLARATIONS = frozenset(
    {
        "contribution governance: cla.",
        "contribution governance: dco.",
        "contribution governance: neither cla nor dco.",
    }
)
_TERMS_DECLARATIONS = (
    "these terms apply to the downloadable openprose cli packages.",
    "these terms do not apply to the downloadable openprose cli packages.",
)
_STALE_CLAIMS = {
    "root-readme": ("there is no separate binary",),
    "root-release": (
        "@openprose/prose-cli has been removed from the repo",
        "there is no prose-cli release train",
    ),
    "root-contributing": (
        "harness implementations live outside this repo",
        "cli implementations live outside this repo",
        "cli code lives outside this repo",
        "cli code is external",
        "there is nothing to build",
    ),
    "root-privacy": (
        "no usage data, error reports, or environment information is sent anywhere",
    ),
}


class PublicDocsError(ValueError):
    """A public-document input is unsafe or malformed."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ClosedArgumentParser(argparse.ArgumentParser):
    """Convert parser diagnostics into the same sanitized result boundary."""

    def error(self, message: str) -> None:
        del message
        raise PublicDocsError("input")


def _document_record(
    *,
    status: str = "not-read",
    byte_length: int | None = None,
    sha256: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "byteLength": byte_length,
        "sha256": sha256,
    }


def _report(version: str | None) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "status": "fail",
        "version": version,
        "releaseOnly": True,
        "publicationAuthorized": False,
        "documents": {name: _document_record() for name in DOCUMENTS},
        "checks": {name: "not-evaluated" for name in CHECKS},
        "failures": [],
    }


def _failure(
    report: dict[str, object], *, code: str, document: str, check: str
) -> None:
    failures = report["failures"]
    if not isinstance(failures, list):
        raise AssertionError("closed report failures must be a list")
    item = {"code": code, "document": document, "check": check}
    if item not in failures:
        failures.append(item)


def _set_check(report: dict[str, object], name: str, passed: bool) -> None:
    checks = report["checks"]
    if not isinstance(checks, dict):
        raise AssertionError("closed report checks must be an object")
    checks[name] = "pass" if passed else "fail"


def _repository_root(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise PublicDocsError("root")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PublicDocsError("root") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PublicDocsError("root")
    return resolved


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _regular_document(root: Path, relative: str) -> bytes:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise PublicDocsError("unsafe")
    candidate = root.joinpath(*parts)
    current = root
    try:
        for part in parts[:-1]:
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise PublicDocsError("unsafe")
        before = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except PublicDocsError:
        raise
    except FileNotFoundError as error:
        raise PublicDocsError("unavailable") from error
    except OSError as error:
        raise PublicDocsError("unsafe") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise PublicDocsError("unsafe") from error
    if (
        resolved != candidate
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PublicDocsError("unsafe")
    if before.st_size <= 0 or before.st_size > MAX_DOCUMENT_BYTES:
        raise PublicDocsError("malformed")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise PublicDocsError("unsafe") from error
    try:
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before) or not stat.S_ISREG(
                opened.st_mode
            ):
                raise PublicDocsError("unsafe")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(
                    descriptor,
                    min(65_536, MAX_DOCUMENT_BYTES + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_DOCUMENT_BYTES:
                    raise PublicDocsError("malformed")
            after = os.fstat(descriptor)
        except PublicDocsError:
            raise
        except OSError as error:
            raise PublicDocsError("unsafe") from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise PublicDocsError("unsafe") from error
    if _file_identity(opened) != _file_identity(after) or total != opened.st_size:
        raise PublicDocsError("unsafe")
    try:
        final = candidate.lstat()
        final_resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PublicDocsError("unsafe") from error
    if (
        _file_identity(final) != _file_identity(after)
        or stat.S_ISLNK(final.st_mode)
        or not stat.S_ISREG(final.st_mode)
        or final_resolved != candidate
    ):
        raise PublicDocsError("unsafe")
    return b"".join(chunks)


def _text(encoded: bytes) -> str:
    try:
        value = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublicDocsError("malformed") from error
    if (
        value.startswith("\ufeff")
        or "\r" in value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise PublicDocsError("malformed")
    for character in value:
        if character in {"\n", "\t"}:
            continue
        if unicodedata.category(character) in {"Cc", "Cf"}:
            raise PublicDocsError("malformed")
    return value


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _visible(value: str) -> str:
    return _normalized(re.sub(r"[`*_~]+", "", value)).casefold()


def _paragraphs(value: str) -> tuple[str, ...]:
    return tuple(
        normalized
        for paragraph in re.split(r"\n[ \t]*\n", value)
        if (normalized := _normalized(paragraph))
    )


def _changelog_ready(value: str, version: str) -> bool:
    candidates = re.findall(
        rf"^## \[{re.escape(version)}\][^\n]*$",
        value,
        re.MULTILINE,
    )
    if len(candidates) != 1:
        return False
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\] — "
        r"([0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01]))$",
    )
    matches = pattern.findall(candidates[0])
    if len(matches) != 1:
        return False
    try:
        date.fromisoformat(matches[0])
    except ValueError:
        return False
    return True


def _readme_ready(value: str) -> bool:
    if len(_README_LINK.findall(value)) != 1:
        return False
    matches = []
    for paragraph in _paragraphs(value):
        visible = _visible(paragraph)
        if (
            _README_LINK.search(paragraph) is not None
            and "interactive" in visible
            and "skill" in visible
            and "optional" in visible
            and "downloadable" in visible
            and "cli" in visible
            and ("independent" in visible or "separate" in visible)
        ):
            matches.append(paragraph)
    return len(matches) == 1


def _release_ready(value: str) -> bool:
    if len(_RELEASE_LINK.findall(value)) != 1:
        return False
    matches = []
    for paragraph in _paragraphs(value):
        visible = _visible(paragraph)
        if (
            _RELEASE_LINK.search(paragraph) is not None
            and "independent" in visible
            and "cli" in visible
            and "alpha" in visible
            and _CLI_ALPHA_TAG.search(visible) is not None
        ):
            matches.append(paragraph)
    return len(matches) == 1


def _contribution_route_ready(value: str) -> bool:
    if len(_CONTRIBUTING_LINK.findall(value)) != 1:
        return False
    matches = []
    for paragraph in _paragraphs(value):
        visible = _visible(paragraph)
        if (
            _CONTRIBUTING_LINK.search(paragraph) is not None
            and "cli" in visible
            and ("change" in visible or "contribution" in visible)
            and ("route" in visible or "guide" in visible)
        ):
            matches.append(paragraph)
    return len(matches) == 1


def _privacy_ready(value: str) -> bool:
    matches = []
    for paragraph in _paragraphs(value):
        visible = _visible(paragraph)
        if (
            "no openprose-operated telemetry" in visible
            and "task data" in visible
            and "selected harness" in visible
            and "provider" in visible
            and ("sent" in visible or "receive" in visible)
        ):
            matches.append(paragraph)
    return len(matches) == 1


def _terms_ready(value: str) -> bool:
    visible = _visible(value)
    return sum(visible.count(declaration) for declaration in _TERMS_DECLARATIONS) == 1


def _governance_ready(value: str) -> bool:
    declarations = [
        line.strip().casefold()
        for line in value.splitlines()
        if line.strip().casefold() in _GOVERNANCE_DECLARATIONS
    ]
    return len(declarations) == 1


def _license_ready(value: str) -> bool:
    lines = value.splitlines()
    visible = _visible(value)
    return (
        bool(lines)
        and lines[0] == "MIT License"
        and "copyright (c)" in visible
        and "permission is hereby granted, free of charge" in visible
        and "copies or substantial portions of the software" in visible
        and 'the software is provided "as is"' in visible
    )


def _markdown_link(value: str, target: str) -> bool:
    pattern = re.compile(r"(?<!!)\[[^\]]+\]\(" + re.escape(target) + r"\)")
    return value.count(target) == 1 and pattern.search(value) is not None


def _support_ready(value: str) -> bool:
    if not all(
        _markdown_link(value, target)
        for target in (
            HARNESS_MODEL_FORM_URL,
            BENCHMARK_FORM_URL,
            PRIVATE_VULNERABILITY_URL,
        )
    ):
        return False

    harness_routes = []
    benchmark_routes = []
    security_routes = []
    privacy_warnings = []
    sections = (
        normalized
        for section in re.split(
            r"\n[ \t]*\n|(?=^[ \t]*[-*+] )",
            value,
            flags=re.MULTILINE,
        )
        if (normalized := _normalized(section))
    )
    for section in sections:
        visible = _visible(section)
        if HARNESS_MODEL_FORM_URL in section:
            if (
                "harness" in visible
                and "model" in visible
                and "benchmark" not in visible
                and ("adapter" in visible or "authentication" in visible)
            ):
                harness_routes.append(section)
        if BENCHMARK_FORM_URL in section:
            if "benchmark" in visible and ("profile" in visible or "cell" in visible):
                benchmark_routes.append(section)
        if PRIVATE_VULNERABILITY_URL in section:
            if "suspected vulnerability" in visible and "private" in visible:
                security_routes.append(section)
        if all(
            phrase in visible
            for phrase in (
                "credentials",
                "tokens",
                "account identifiers",
                "private paths",
                "raw provider output",
                "public issue",
            )
        ):
            privacy_warnings.append(section)
    return all(
        len(matches) == 1
        for matches in (
            harness_routes,
            benchmark_routes,
            security_routes,
            privacy_warnings,
        )
    )


def _issue_form(value: str) -> dict[str, object] | None:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate issue-form field")
            result[key] = item
        return result

    try:
        result = json.loads(value, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(result, dict) or set(result) != {
        "name",
        "description",
        "title",
        "body",
    }:
        return None
    if not isinstance(result.get("body"), list):
        return None
    return result


def _form_body_item(
    form: dict[str, object], *, item_type: str, item_id: str | None = None
) -> dict[str, object] | None:
    body = form.get("body")
    if not isinstance(body, list):
        return None
    matches = []
    for item in body:
        if not isinstance(item, dict) or item.get("type") != item_type:
            continue
        if item_id is not None and item.get("id") != item_id:
            continue
        matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _attributes(item: dict[str, object] | None) -> dict[str, object] | None:
    if item is None:
        return None
    attributes = item.get("attributes")
    return attributes if isinstance(attributes, dict) else None


def _required(item: dict[str, object] | None) -> bool:
    return item is not None and item.get("validations") == {"required": True}


def _form_intro(form: dict[str, object]) -> str | None:
    attributes = _attributes(_form_body_item(form, item_type="markdown"))
    if attributes is None:
        return None
    value = attributes.get("value")
    return value if isinstance(value, str) else None


def _dropdown_options(form: dict[str, object], item_id: str) -> list[str] | None:
    attributes = _attributes(
        _form_body_item(form, item_type="dropdown", item_id=item_id)
    )
    if attributes is None:
        return None
    options = attributes.get("options")
    if not isinstance(options, list) or not all(
        isinstance(option, str) for option in options
    ):
        return None
    return options


def _issue_form_routing_ready(value: str, *, kind: str) -> bool:
    form = _issue_form(value)
    if form is None:
        return False
    intro = _form_intro(form)
    if intro is None:
        return False
    visible = _visible(intro)
    if kind == "harness-model":
        request_type = _form_body_item(
            form, item_type="dropdown", item_id="request_type"
        )
        return (
            form.get("name") == "OpenProse CLI harness or model request"
            and form.get("description")
            == (
                "Request a new harness adapter, admitted harness version, or model "
                "or authentication route"
            )
            and form.get("title") == "[CLI harness/model request]: "
            and "adapter admission" in visible
            and "admitted harness version" in visible
            and "model" in visible
            and "authentication route" in visible
            and "do not use this form for a benchmark profile or cell" in visible
            and _required(request_type)
            and _dropdown_options(form, "request_type")
            == [
                "New harness adapter",
                "New admitted harness version",
                "Model or authentication route",
            ]
        )
    if kind == "benchmark":
        proposal_type = _form_body_item(
            form, item_type="dropdown", item_id="proposal_type"
        )
        return (
            form.get("name") == "OpenProse CLI benchmark profile or cell proposal"
            and form.get("description")
            == (
                "Propose a frozen benchmark profile or one exact cell for separate "
                "review"
            )
            and form.get("title") == "[CLI benchmark proposal]: "
            and "use this form only" in visible
            and "benchmark profile or cell proposal" in visible
            and "does not authorize live collection" in visible
            and _required(proposal_type)
            and _dropdown_options(form, "proposal_type")
            == [
                "New benchmark profile",
                "New cell in a frozen profile",
                "Profile or cell amendment",
            ]
        )
    raise AssertionError("issue-form kind is not closed")


def _issue_form_privacy_ready(value: str) -> bool:
    form = _issue_form(value)
    if form is None:
        return False
    intro = _form_intro(form)
    disclosure_item = _form_body_item(
        form, item_type="checkboxes", item_id="disclosure"
    )
    disclosure = _attributes(disclosure_item)
    if intro is None or disclosure is None or not _required(disclosure_item):
        return False
    intro_visible = _visible(intro)
    options = disclosure.get("options")
    if not isinstance(options, list):
        return False
    if not all(
        isinstance(option, dict) and option.get("required") is True
        for option in options
    ):
        return False
    labels = " ".join(
        option.get("label", "")
        for option in options
        if isinstance(option, dict) and isinstance(option.get("label"), str)
    )
    labels_visible = _visible(labels)
    return all(
        phrase in intro_visible
        for phrase in (
            "credentials",
            "tokens",
            "account identifiers",
            "private paths",
            "raw provider transcripts",
            "suspected security vulnerability",
            "private vulnerability report",
            PRIVATE_VULNERABILITY_URL,
        )
    ) and all(
        phrase in labels_visible
        for phrase in (
            "credentials",
            "account identifiers",
            "private paths",
            "raw provider output",
        )
    )


def _stale_claims(value: str, document: str) -> bool:
    visible = _visible(value)
    return not any(claim in visible for claim in _STALE_CLAIMS.get(document, ()))


def _validate_report(value: dict[str, object]) -> None:
    if set(value) != {
        "schema",
        "status",
        "version",
        "releaseOnly",
        "publicationAuthorized",
        "documents",
        "checks",
        "failures",
    }:
        raise AssertionError("public-document report is not closed")
    documents = value["documents"]
    checks = value["checks"]
    failures = value["failures"]
    if (
        value["schema"] != SCHEMA
        or value["status"] not in {"pass", "fail"}
        or value["releaseOnly"] is not True
        or value["publicationAuthorized"] is not False
        or not isinstance(documents, dict)
        or set(documents) != set(DOCUMENTS)
        or not isinstance(checks, dict)
        or set(checks) != set(CHECKS)
        or not isinstance(failures, list)
        or (value["version"] is not None and not isinstance(value["version"], str))
    ):
        raise AssertionError("public-document report is malformed")
    for record in documents.values():
        if (
            not isinstance(record, dict)
            or set(record) != {"status", "byteLength", "sha256"}
            or record["status"]
            not in {"not-read", "authenticated", "unavailable", "unsafe", "malformed"}
            or (
                record["status"] == "authenticated"
                and (
                    not isinstance(record["byteLength"], int)
                    or isinstance(record["byteLength"], bool)
                    or not 0 < record["byteLength"] <= MAX_DOCUMENT_BYTES
                    or not isinstance(record["sha256"], str)
                    or SHA256.fullmatch(record["sha256"]) is None
                )
            )
            or (
                record["status"] != "authenticated"
                and (record["byteLength"] is not None or record["sha256"] is not None)
            )
        ):
            raise AssertionError("public-document identity is malformed")
    if any(
        status not in {"pass", "fail", "not-evaluated"} for status in checks.values()
    ):
        raise AssertionError("public-document check status is malformed")
    for failure in failures:
        if (
            not isinstance(failure, dict)
            or set(failure) != {"code", "document", "check"}
            or failure["code"] not in FAILURE_CODES
            or failure["document"] not in {*DOCUMENTS, "release-set"}
            or failure["check"] not in CHECKS
        ):
            raise AssertionError("public-document failure is malformed")
    if len(failures) != len(
        {(item["code"], item["document"], item["check"]) for item in failures}
    ):
        raise AssertionError("public-document failures are duplicated")
    passed = value["status"] == "pass"
    if passed != (not failures and all(status == "pass" for status in checks.values())):
        raise AssertionError("public-document report status is inconsistent")


def assess(*, version: str, repository_root: Path) -> dict[str, object]:
    """Authenticate and assess the exact functional-alpha public documents."""

    valid_version = isinstance(version, str) and ALPHA.fullmatch(version) is not None
    report = _report(version if valid_version else None)
    if not valid_version:
        _set_check(report, "version", False)
        _failure(
            report,
            code="INPUT_INVALID",
            document="release-set",
            check="version",
        )
        _validate_report(report)
        return report
    _set_check(report, "version", True)

    try:
        root = _repository_root(repository_root)
    except (PublicDocsError, TypeError):
        _set_check(report, "repository-root", False)
        _failure(
            report,
            code="ROOT_UNSAFE",
            document="release-set",
            check="repository-root",
        )
        _validate_report(report)
        return report
    _set_check(report, "repository-root", True)

    documents = report["documents"]
    if not isinstance(documents, dict):
        raise AssertionError("closed report documents must be an object")
    text: dict[str, str] = {}
    for name, relative in DOCUMENTS.items():
        try:
            encoded = _regular_document(root, relative)
            decoded = _text(encoded)
        except PublicDocsError as error:
            status = (
                error.kind
                if error.kind in {"unavailable", "unsafe", "malformed"}
                else "unsafe"
            )
            documents[name] = _document_record(status=status)
            _failure(
                report,
                code={
                    "unavailable": "DOCUMENT_UNAVAILABLE",
                    "unsafe": "DOCUMENT_UNSAFE",
                    "malformed": "DOCUMENT_MALFORMED",
                }[status],
                document=name,
                check="document-authentication",
            )
            continue
        documents[name] = _document_record(
            status="authenticated",
            byte_length=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )
        text[name] = decoded
    documents_ready = len(text) == len(DOCUMENTS)
    _set_check(report, "document-authentication", documents_ready)

    semantic_checks: tuple[
        tuple[str, str, tuple[str, ...], str, Callable[[str], bool]], ...
    ] = (
        (
            "changelog-section",
            "cli-changelog",
            ("cli-changelog",),
            "CHANGELOG_SECTION_INVALID",
            lambda value: _changelog_ready(value, version),
        ),
        (
            "readme-surfaces",
            "root-readme",
            ("root-readme", "cli-readme"),
            "README_SURFACES_MISSING",
            _readme_ready,
        ),
        (
            "release-train",
            "root-release",
            ("root-release", "cli-release-readme"),
            "RELEASE_TRAIN_MISSING",
            _release_ready,
        ),
        (
            "contribution-route",
            "root-contributing",
            ("root-contributing", "cli-contributing"),
            "CONTRIBUTION_ROUTE_MISSING",
            _contribution_route_ready,
        ),
        (
            "privacy-boundary",
            "root-privacy",
            ("root-privacy",),
            "PRIVACY_BOUNDARY_MISSING",
            _privacy_ready,
        ),
        (
            "terms-scope",
            "root-terms",
            ("root-terms",),
            "TERMS_SCOPE_MISSING",
            _terms_ready,
        ),
        (
            "contribution-governance",
            "root-contributing",
            ("root-contributing",),
            "GOVERNANCE_DECLARATION_MISSING",
            _governance_ready,
        ),
        (
            "license-identity",
            "root-license",
            ("root-license",),
            "LICENSE_INVALID",
            _license_ready,
        ),
        (
            "support-routing",
            "cli-support",
            (
                "cli-support",
                "cli-harness-model-issue-form",
                "cli-benchmark-issue-form",
            ),
            "SUPPORT_ROUTING_INVALID",
            _support_ready,
        ),
    )
    for check, document, required_documents, code, validator in semantic_checks:
        source = text.get(document)
        passed = (
            source is not None
            and all(required in text for required in required_documents)
            and validator(source)
        )
        _set_check(report, check, passed)
        if not passed:
            _failure(report, code=code, document=document, check=check)

    form_specs = (
        ("cli-harness-model-issue-form", "harness-model"),
        ("cli-benchmark-issue-form", "benchmark"),
    )
    routing_results = {
        document: source is not None and _issue_form_routing_ready(source, kind=kind)
        for document, kind in form_specs
        for source in (text.get(document),)
    }
    _set_check(report, "issue-form-routing", all(routing_results.values()))
    for document, passed in routing_results.items():
        if not passed:
            _failure(
                report,
                code="ISSUE_FORM_ROUTING_INVALID",
                document=document,
                check="issue-form-routing",
            )

    privacy_results = {
        document: source is not None and _issue_form_privacy_ready(source)
        for document, _kind in form_specs
        for source in (text.get(document),)
    }
    _set_check(report, "issue-form-privacy", all(privacy_results.values()))
    for document, passed in privacy_results.items():
        if not passed:
            _failure(
                report,
                code="ISSUE_FORM_PRIVACY_INVALID",
                document=document,
                check="issue-form-privacy",
            )

    stale_ready = True
    for document in _STALE_CLAIMS:
        source = text.get(document)
        if source is None:
            stale_ready = False
            continue
        if not _stale_claims(source, document):
            stale_ready = False
            _failure(
                report,
                code="STALE_CLAIM",
                document=document,
                check="stale-claims",
            )
    _set_check(report, "stale-claims", stale_ready)

    failures = report["failures"]
    if not isinstance(failures, list):
        raise AssertionError("closed report failures must be a list")
    report["status"] = "pass" if not failures else "fail"
    _validate_report(report)
    return report


def parser() -> argparse.ArgumentParser:
    result = ClosedArgumentParser(add_help=False)
    result.add_argument("--version", required=True)
    result.add_argument("--repository-root", type=Path, required=True)
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    emit: Callable[[str], None] = print,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if (
            arguments.count("--version") != 1
            or arguments.count("--repository-root") != 1
        ):
            raise PublicDocsError("input")
        options = parser().parse_args(arguments)
        result = assess(
            version=options.version,
            repository_root=options.repository_root,
        )
    except (PublicDocsError, OSError, TypeError, ValueError):
        result = _report(None)
        _set_check(result, "version", False)
        _failure(
            result,
            code="INPUT_INVALID",
            document="release-set",
            check="version",
        )
        _validate_report(result)
    emit(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
