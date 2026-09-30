#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import unittest

try:
    import jsonschema
except ImportError as error:  # pragma: no cover - actionable bootstrap failure
    raise SystemExit(
        "Install the pinned test dependency: "
        "python3 -m pip install -r cli/ci/requirements-test.txt"
    ) from error


HERE = Path(__file__).resolve().parent
# Synthetic source records exercise the current aggregator, not a live-run claim.
from test_matrix import MATRIX, complete_records

EXPECTED_COMMIT = "0123456789abcdef0123456789abcdef01234567"
SCHEMA = HERE / "matrix.schema.json"
EXPECTED_SURFACES = ("rust", "bun", "npm")
EXPECTED_HARNESSES = ("prime", "omp", "codex", "claude")

FORBIDDEN_PATH_KEYS = {"path", "commandpath"}
FORBIDDEN_CREDENTIAL_KEYS = {
    "apikey",
    "authorization",
    "clientsecret",
    "credential",
    "credentials",
    "idtoken",
    "oauthtoken",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
    "accesstoken",
}

# Search within every string, rather than only rejecting strings whose first
# character happens to begin a path. The delimiters avoid treating URLs and
# public identifiers such as ``prime/rpc`` as filesystem paths.
ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?:^|[\s\"'=,(])/(?!/)(?:[^\s\"']*)"),
    re.compile(r"(?:^|[\s\"'=,(])[A-Za-z]:[\\/]"),
    re.compile(r"(?:^|[\s\"'=,(])(?:\\\\|//)[^\\/\s]+[\\/]"),
    re.compile(r"\bfile:/+", re.IGNORECASE),
)

# These patterns target values, not public metadata labels. Consequently
# ``provider-api-key``, ``cached-chatgpt-login``, model IDs, and dependency
# names containing words such as "credential" or "token" remain valid.
CREDENTIAL_VALUE_PATTERNS = (
    re.compile(
        r"\b(?:api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|"
        r"oauth[-_ ]?token|id[-_ ]?token|client[-_ ]?secret|private[-_ ]?key|"
        r"password|passwd|secret|authorization)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/]{12,}={0,2}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
)


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def walk(value: object, location: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            yield location, key, child
            yield from walk(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{location}[{index}]")


def normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


class PublicFunctionalAlphaEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = MATRIX.build_report(complete_records(), MATRIX.SURFACES)
        cls.raw = canonical_json(cls.report)
        cls.schema = json.loads(SCHEMA.read_text("utf-8"))

    def test_matrix_is_canonical_and_schema_valid(self) -> None:
        self.assertEqual(canonical_json(self.report), self.raw)
        jsonschema.Draft202012Validator.check_schema(self.schema)
        errors = sorted(
            jsonschema.Draft202012Validator(
                self.schema,
                format_checker=jsonschema.FormatChecker(),
            ).iter_errors(self.report),
            key=lambda error: list(error.absolute_path),
        )
        self.assertEqual(
            [],
            [f"{list(error.absolute_path)}: {error.message}" for error in errors],
        )

    def test_generated_matrix_binds_the_complete_synthetic_cohort(self) -> None:
        self.assertEqual(
            "openprose.functional-alpha-live-matrix/4",
            self.report["schema"],
        )
        self.assertEqual("pass", self.report["status"])
        self.assertEqual(list(EXPECTED_SURFACES), self.report["requiredSurfaces"])
        self.assertEqual(list(EXPECTED_HARNESSES), self.report["requiredHarnesses"])
        self.assertEqual(12, self.report["cellCount"])
        self.assertEqual(12, len(self.report["cells"]))
        self.assertEqual(
            EXPECTED_COMMIT,
            self.report["common"]["runnerBuild"]["commit"],
        )

        surface_entries = self.report["surfaces"]
        self.assertEqual(
            set(EXPECTED_SURFACES),
            {entry["surface"] for entry in surface_entries},
        )
        self.assertEqual(len(EXPECTED_SURFACES), len(surface_entries))
        self.assertTrue(
            all(
                entry["runner"]["commit"] == EXPECTED_COMMIT
                for entry in surface_entries
            )
        )

        actual_cells = [
            (cell["surface"], cell["harness"]) for cell in self.report["cells"]
        ]
        expected_cells = {
            (surface, harness)
            for surface in EXPECTED_SURFACES
            for harness in EXPECTED_HARNESSES
        }
        self.assertEqual(len(actual_cells), len(set(actual_cells)))
        self.assertEqual(expected_cells, set(actual_cells))

    def assert_public(self, report: object) -> None:
        for location, key, value in walk(report):
            normalized = normalized_key(key)
            self.assertNotIn(normalized, FORBIDDEN_PATH_KEYS, location)
            self.assertNotIn(normalized, FORBIDDEN_CREDENTIAL_KEYS, location)
            if not isinstance(value, str):
                continue
            self.assertFalse(
                any(pattern.search(value) for pattern in ABSOLUTE_PATH_PATTERNS),
                f"absolute path at {location}.{key}",
            )
            self.assertFalse(
                any(pattern.search(value) for pattern in CREDENTIAL_VALUE_PATTERNS),
                f"credential-bearing string at {location}.{key}",
            )

    def test_generated_public_matrix_contains_no_paths_or_credentials(self) -> None:
        self.assert_public(self.report)

    def test_privacy_checks_reject_nested_paths_and_credentials(self) -> None:
        values = (
            ("note", "failed at /private/build/source"),
            ("note", r"failed at C:\build\source"),
            ("note", r"failed at \\server\share\source"),
            ("note", "read file:///private/build/source"),
            ("note", "Authorization: Bearer synthetic-test-credential"),
            ("note", "https://fixture:synthetic-password@example.invalid"),
            ("commandPath", "prose"),
            ("api_key", "synthetic-test-credential"),
        )
        for key, value in values:
            with self.subTest(key=key, value=value):
                report = deepcopy(self.report)
                report["syntheticNestedMetadata"] = [{key: value}]
                with self.assertRaises(AssertionError):
                    self.assert_public(report)


if __name__ == "__main__":
    unittest.main()
