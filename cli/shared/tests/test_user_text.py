#!/usr/bin/env python3
"""Plain-English check of the text a hosted-service user reads.

The service help (`shared/service/help.v1.json`: every help topic and human
view), the agent guide (`shared/service/guide.v1.md`) and every human
expectation of the service corpus (`stdout`/`stderr` text and `contains`
fragments under `conformance/cases/service/`) must not use the service's
internal nouns. JSON field names are data and are not checked; the runner
help (`prose --help`), which documents local harnesses, is not service text.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

SHARED = Path(__file__).resolve().parents[1]
CLI = SHARED.parent
SERVICE = SHARED / "service"
SERVICE_CASES = CLI / "conformance" / "cases" / "service"
RUNNER_HELP = CLI / "conformance" / "cases" / "fixtures" / "runner-help.txt"

# Internal nouns and retired labels, with the plain words used instead.
BANNED = {
    r"\btrigger\w*": "job (a job runs; it is not a trigger)",
    r"\breactor\b": "the service's runtime name is not user text",
    r"\badmitted budget\b": "run budget",
    r"\brender\w*": "print or show",
    r"\bparked\b": "settling",
    r"\bshowable\b": "can be shown",
    r"\bposted\b": "balance",
    r"\bpending settlement\b": "settling (the final price is being confirmed)",
    r"\bharness\w*": "the hosted service picks how a run executes",
}
# `harness` may appear only where the text is about running on this machine:
# the local-harness pointer of a forwarded language command, a `--harness`
# option name the user typed, or the command name `harness` in a command list.
HARNESS_ALLOWED = re.compile(
    r"local harness|--harness\b|cli harness\b|, harness,")
URL = re.compile(r"https?://\S+")


def user_texts() -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    document = json.loads((SERVICE / "help.v1.json").read_text())
    texts += [(f"help topic {topic!r}", text) for topic, text in document["topics"].items()]
    texts += [(f"help view {view!r}", text) for view, text in document["views"].items()]
    texts.append(("guide.v1.md", (SERVICE / "guide.v1.md").read_text()))
    runner_help = RUNNER_HELP.read_text()
    for path in sorted(SERVICE_CASES.glob("*/*.json")):
        case = json.loads(path.read_text())
        for stream in ("stdout", "stderr"):
            expected = case.get(stream)
            if not isinstance(expected, dict):
                continue
            name = f"{path.parent.name}/{path.name} {stream}"
            text = expected.get("text")
            if isinstance(text, str) and text != runner_help:
                texts.append((name, text))
            texts += [(name, fragment) for fragment in expected.get("contains", []) if isinstance(fragment, str)]
    return texts


def jargon(text: str) -> list[str]:
    """Every banned word in `text`, outside URLs and the allowed harness uses."""
    text = URL.sub("", text)
    found = []
    for pattern in BANNED:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if pattern == r"\bharness\w*":
                window = text[max(0, match.start() - 6):match.end() + 1]
                if HARNESS_ALLOWED.search(window):
                    continue
            found.append(text[max(0, match.start() - 40):match.end() + 20].replace("\n", " "))
    return found


class UserTextTests(unittest.TestCase):
    def test_no_internal_nouns_in_help_guide_or_human_output(self) -> None:
        problems = [f"{where}: ...{snippet}..." for where, text in user_texts() for snippet in jargon(text)]
        self.assertEqual(problems, [], "internal nouns in user text:\n" + "\n".join(problems))

    def test_the_check_catches_each_banned_word(self) -> None:
        for sample in ("List jobs (triggers).", "reactor model-luna", "the admitted budget ran out",
                       "rendered from", "billing: parked", "showable: false", "Posted: $1.00",
                       "billing: pending settlement", "the hosted service picks the harness"):
            self.assertTrue(jargon(sample), sample)
        for sample in ("needs a local harness (`prose cli harness list`)", "Drop --harness here",
                       "Commands: doctor, example, harness, job",
                       "https://example.invalid/webhooks/triggers/1", "Balance: $1.00"):
            self.assertEqual(jargon(sample), [], sample)


if __name__ == "__main__":
    unittest.main()
