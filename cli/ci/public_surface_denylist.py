"""Denylist for the public-surface leak gate (cli/ci/check_public_surface.py).

prose-cli is a public user client. Developer tooling and internal service
detail belong in the private service repository. Every pattern below names
something that must never reach this repository or the binaries it builds.

Only generic patterns (hostname shapes, home directories, long run ids and
names that were already public) are written out, plus salted digests of
random identifiers captured from service data. Named internal terms are not
listed here in any form, since a digest of a short or guessable word can be
reversed by trying candidates: the service repository checks them privately
before a release is published.
"""

from __future__ import annotations

import hashlib
import re
from typing import NamedTuple, Optional, Protocol


class _Matcher(Protocol):
    def search(self, text: str) -> Optional["re.Match[str]"]: ...


class Pattern(NamedTuple):
    name: str
    regex: _Matcher


# Identifiers copied from real service data (trigger UUIDs, the 16-hex
# prefix of a run id) are matched by salted SHA-256, so this public file does
# not republish them. They are random, high-entropy values, so a digest does
# not reveal them. Fixtures use made-up identifiers instead.
DIGEST_SALT = "openprose-public-surface:"


def identifier_digest(identifier: str) -> str:
    return hashlib.sha256((DIGEST_SALT + identifier).encode("utf-8")).hexdigest()


class DigestMatcher:
    """Finds a token matching ``token`` whose salted SHA-256 is in ``digests``."""

    def __init__(self, digests: frozenset[str], token: "re.Pattern[str]") -> None:
        self.digests = digests
        self.token = token

    def search(self, text: str) -> Optional["re.Match[str]"]:
        for match in self.token.finditer(text):
            if identifier_digest(match.group()) in self.digests:
                return match
        return None


_CAPTURED_ID = re.compile(
    r"(?<![0-9A-Za-z_-])(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z-])"
    r"|run_[0-9a-f]{16})"
)
CAPTURED_ID_DIGESTS: frozenset[str] = frozenset(
    {
    "b839bdceccffdaf5f6e4f390022ba3be3573e6013d58809a3e2421ae06669d6f",
    "018cdfba0ae4366d454ddf3345a705321a55125123501d20c3f17bc23deb26ee",
    "191af5df5de3d9a621e7fb21814726bc3427020b325a22f1b7f4f037a7f93b7e",
    }
)


def _literal(name: str, *parts: str, flags: int = 0) -> Pattern:
    return Pattern(name, re.compile(re.escape("".join(parts)), flags))


def _regex(name: str, *parts: str, flags: int = 0) -> Pattern:
    return Pattern(name, re.compile("".join(parts), flags))


_STAGE = "stag" + "ing"
_SERVICE = "run-" + "prose"
_WORKERS = r"\.openprose\.workers\." + "dev"

# The retired alternate service environment's credential variable and
# option: public before, removed now. Only the changelog may name them.
RETIRED: tuple[Pattern, ...] = (
    _literal("retired credential variable", "OPENPROSE_", _STAGE.upper(), "_API_KEY"),
    _literal("retired service environment option", "--service-", "environment"),
)

# Patterns that must not appear in any tracked file, in the help, guide or
# manifest output of either port, or in the strings of either release binary.
FORBIDDEN: tuple[Pattern, ...] = (
    *RETIRED,
    _literal("retired service hostname", _SERVICE, "-", _STAGE),
    # Any service worker hostname other than production.
    _regex(
        "non-production service hostname",
        r"(?<![a-z0-9-])(?!",
        _SERVICE,
        r"-production\.)[a-z0-9-]+",
        _WORKERS,
    ),
    Pattern("identifier captured from service data", DigestMatcher(CAPTURED_ID_DIGESTS, _CAPTURED_ID)),
    # A developer's home directory. Tests use the placeholder users alice and
    # private.
    _regex(
        "developer home directory",
        r"/", "Users", r"/(?!alice/|private/)[A-Za-z_][A-Za-z0-9_.-]*/",
    ),
    _regex("developer home directory", r"/", "home", r"/(?!runner/)[a-z_][a-z0-9_.-]*/"),
    # A developer endpoint selector such as dev:<name>.
    _regex("developer endpoint selector", r"(?<![A-Za-z0-9_])", "dev", r":(?:<[a-z]+>|[a-z][a-z0-9-]*\b)(?![./:])"),
    # Service run ids are run_ plus 64 hex digits. Fixtures use synthetic ids
    # that repeat a short unit (run_ plus 4f1c2d3e4f5a6b7c four times, or one
    # digit 64 times); a real id is random and never does.
    _regex(
        "real-looking run id",
        r"(?<![0-9a-f])run_",
        r"(?!([0-9a-f]{1,16})\1+[0-9a-f]{0,15}(?![0-9a-f]))",
        r"[0-9a-f]{32,}(?![0-9a-f])",
    ),
)

# Internal ticket ids. Forbidden in user-facing help, guide and manifest
# output, in user documentation, in the published contract (cli/shared) and
# conformance corpus (cli/conformance), in product source and its tests
# (cli/bun, cli/rust/crates), and in the release binaries; the protocol
# records may use them.
# Ticket ids are matched in any case, with or without a separator, and inside
# identifiers (snake_case, camelCase and kebab-case names).
JARGON: tuple[Pattern, ...] = (
    _regex(
        "internal ticket id",
        r"(?:(?<![A-Za-z0-9])(?i:", "imp", r")|(?<=[a-z0-9])", "Imp", r")[-_]?\d{3}(?![0-9])",
    ),
    # The service's internal name, in any case and separator. Only the
    # production origin, which the products must reach, may carry it.
    _regex(
        "internal service name",
        r"(?i:", _SERVICE.replace("-", "[-_]"), r")(?!-production", _WORKERS, r")",
    ),
)

# Additional patterns for user-facing help output only. Tracked files may use
# the word for unrelated meanings (release staging directories, npm staging).
HELP_ONLY: tuple[Pattern, ...] = (
    _regex("staging in user-facing help", r"\b", _STAGE, r"\b", flags=re.IGNORECASE),
)

# Patterns for the public release binaries only. Their source legitimately
# names the developer endpoint override (behind the dev-endpoint build switch)
# and the test seams (behind the test-seam build switch); a public binary
# must contain neither, nor a builder's home directory.
BINARY_ONLY: tuple[Pattern, ...] = (
    _literal("developer endpoint override", "OPENPROSE_", "API_URL"),
    _literal("developer endpoint label", "custom ", "endpoint"),
    _literal("developer endpoint credential entry", "org.openprose.cli.", "custom"),
    _regex("test seam variable", r"\bOPENPROSE_", r"(?:TEST|CONFORMANCE)_[A-Z0-9_]+"),
    _regex("test seam variable", r"\bPROSE_", r"TEST_SERVICE_[A-Z0-9_]+"),
    _regex("builder home directory", r"/", "Users", r"/[^/\s]+/"),
    _regex("builder home directory", r"/", "home", r"/[a-z_][a-z0-9_.-]*/"),
)
