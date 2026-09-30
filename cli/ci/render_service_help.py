#!/usr/bin/env python3
"""Render service help from the operation manifest.

    python3 cli/ci/render_service_help.py --write   # regenerate
    python3 cli/ci/render_service_help.py --check   # fail if stale

Output: `cli/shared/service/help.v1.json` (`openprose.service-help/1`), a map
from command topic (`cli`, `cli run`, `cli run submit`, ...) to the exact help
text both products print for `--help`. Topics exist for every manifest
operation (service ones generated, the frozen account ones hand-written here) and
for every command group, so no `cli <noun> [verb] --help` falls back to the
runner help. Every topic's "Exit codes:" line is rendered from
the operation's manifest `exitCodes`, and `--check` fails when those disagree
with the error taxonomy or with the codes the shared corpora show the
operation emitting. It also renders the `cli service capabilities` document
and page and the `cli service operations` table, and `--check`
fails when the manifest exit dictionary or an operation's examples drift.
It also generates the corpus cases that pin `cli service guide`
(`shared/service/guide.v1.md`), and `--check` fails when the
guide names a command that does not parse against the manifest.
Every topic prints its manifest examples in an `Examples:` section, and
`prose --help` starts its "For agents" section within its first 25 lines.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
from pathlib import Path
import sys

CLI = Path(__file__).resolve().parents[1]
SERVICE = CLI / "shared" / "service"
MANIFEST = SERVICE / "operations.v1.json"
HELP = SERVICE / "help.v1.json"
WIDTH = 28

RUNNER_HELP = CLI / "conformance" / "cases" / "fixtures" / "runner-help.txt"

# One-line summaries of the top-level command groups in `cli --help`; the
# group's commands follow in the order `command_order` gives.
GROUP_SUMMARY = {
    "run": "Run a program on the hosted service, then follow, read or cancel it",
    "program": "Save your programs and manage their revisions",
    "example": "Working example programs to read and copy",
    "job": "Run a saved program on a schedule or from a webhook",
    "wallet": "Balance, usage and credit",
    "auth": "Sign in, check or remove the stored key",
    "model": "Hosted models",
    "result": "Published results of public programs (OWNER/SLUG); a run's own output is `cli run show` or `cli run download`",
    "org": "Organizations, members and invitations",
    "repo": "Repositories the service can read",
    "package": "Registry packages (no source execution)",
    "service": "Service status, triage, the guide and the command list",
}
# Help lists task commands first: the groups a person runs every day come
# before the account and administration ones, and within a group the most
# common command comes first. Anything not named here follows in
# alphabetical order.
COMMAND_ORDER = {
    (): ("run", "program", "example", "job", "wallet", "auth", "model", "result", "org", "repo", "package",
         "service"),
    ("run",): ("submit", "watch", "show", "list"),
    ("program",): ("save", "list", "show"),
    ("example",): ("list", "show"),
    ("job",): ("create", "list", "show", "delete"),
    ("wallet",): ("balance", "usage", "events", "topup", "redeem"),
    ("auth",): ("login", "status", "logout"),
    ("org",): ("list", "show"),
    ("result",): ("show", "list", "publish"),
    ("service",): ("triage", "status", "guide", "capabilities", "operations"),
}


def command_order(prefix: tuple[str, ...], names) -> list[str]:
    """The children of a command group in help order (see COMMAND_ORDER)."""
    first = COMMAND_ORDER.get(prefix, ())
    return [name for name in first if name in names] + sorted(name for name in names if name not in first)
GROUP_INTRO = {
    ("result",): ["Published results: outputs an owner chose to publish from a public program OWNER/SLUG.",
                  "They are not run records. Read a run's own output with `cli run show RUN_ID`,",
                  "`cli run show RUN_ID --file outputs/result.json` or `cli run download RUN_ID --output-dir DIR`.", ""],
    ("auth",): ["Account credentials for the OpenProse service. Every other service command",
                "reads the key these commands manage, or OPENPROSE_API_KEY.", ""],
}

# Credential variables and the device-flow note every account topic carries
# `--check` requires them in each account topic.
CREDENTIAL_VARIABLES = ("OPENPROSE_API_KEY",)
CREDENTIALS = """Credentials:
  OPENPROSE_API_KEY           API key; a non-empty value wins over the stored key.
"""
DEVICE_FLOW = """Device flow: `cli auth login` prints a one-time code and https://github.com/login/device
on stderr, never the key, and never opens a browser. A person approves the code
there within 15 minutes. An agent without a person sets the variable instead.
"""
ACCOUNT_GLOBALS = """Global options:
  --output human|json|jsonl   Output mode; the same as the PROSE_OUTPUT setting.
"""
JSON_HELP_OPTIONS = """Options:
  --json                      Print one JSON result; the same as the global --output json.
  --help                      Show help for this command.
"""

# The frozen account and `org list` commands keep their own
# parser. Each body ends before its "Output:" line, which is
# rendered from the manifest together with the "Exit codes:" line.
ACCOUNT_TOPICS = {
    "cli auth status": """Usage: prose [GLOBAL OPTIONS] cli auth status [--json]

Report whether a key is available and, when there is one, verify it with
GET /organizations. Signed out is success
(authenticated false, exit 0). A malformed or rejected key is
SERVICE_AUTH_REQUIRED naming details.credentialVariable, credentialSource and
credentialProblem; its Action follows the key's source (replace or unset the
variable, or log in again). Global options may also follow the command.

Arguments: none.

""" + JSON_HELP_OPTIONS + "\n" + ACCOUNT_GLOBALS + "\n" + CREDENTIALS + "\n" + DEVICE_FLOW,
    "cli auth login": """Usage: prose [GLOBAL OPTIONS] cli auth login [--json]

Sign in with the GitHub device flow and store the new key in the operating
system credential store. There is no plaintext fallback. Login is refused
(INVOCATION_INVALID naming the variable) while OPENPROSE_API_KEY is set,
because the variable would still win.

Arguments: none.

""" + JSON_HELP_OPTIONS + "\n" + ACCOUNT_GLOBALS + "\n" + CREDENTIALS + "\n" + DEVICE_FLOW,
    "cli auth logout": """Usage: prose [GLOBAL OPTIONS] cli auth logout [--json]

Remove the stored key from the credential store. The server key is not
revoked. Logging out while signed out succeeds. Logout is refused
(INVOCATION_INVALID naming the variable) while OPENPROSE_API_KEY is set; unset
it yourself.

Arguments: none.

""" + JSON_HELP_OPTIONS + "\n" + ACCOUNT_GLOBALS + "\n" + CREDENTIALS + "\n" + DEVICE_FLOW,
    "cli org list": """Usage: prose [GLOBAL OPTIONS] cli org list [--json]

List the organizations this account belongs to: id, slug, name and role. The
first call can create the account's default organization. Human output is one
`SLUG  ROLE  NAME` line per organization (`-` without a role). Global options
may also follow the command. Other organization commands: `prose cli org --help`.

Arguments: none.

""" + JSON_HELP_OPTIONS + "\n" + ACCOUNT_GLOBALS + "\n" + CREDENTIALS + "\n" + DEVICE_FLOW,
}
# What every account verb topic must say.
ACCOUNT_TOPIC_REQUIRED = ("Usage: ", "\nArguments", "\nOptions:\n", "\nOutput: ", "\nExit codes: ",
                          *CREDENTIAL_VARIABLES, "Device flow: ")

# The frozen registry package commands keep their own parser; their help
# topics are hand-written here because the manifest records only their routes.
# Every syntax line matches runner-help.txt. The "Output:" and
# "Exit codes:" lines are rendered from the manifest.
PACKAGE_TOPICS = {
    "cli package": """Usage: prose [GLOBAL OPTIONS] cli package <COMMAND> [ARGUMENTS] [OPTIONS]

Registry package commands (no source execution). Packages are files published
to an organization's registry namespace; they are not OpenProse service programs
(see `cli program`).

Commands:
  fetch                       Download one exact package version into a fresh directory, verifying its hashes.
  list                        List an organization's public packages, one page at a time.
  publish                     Publish a file or a package directory as ORG/NAME@VERSION (private by default).
  withdraw                    Remove a version from discovery; pinned retrieval keeps working.

Global options:
  --output human|json|jsonl   Output mode; the same as the PROSE_OUTPUT setting.

Run `prose cli package <COMMAND> --help` for details. Output: openprose.service-operation/1 (--json).
""",
    "cli package publish": """Usage: prose [GLOBAL OPTIONS] cli package publish <FILE|DIR> --organization <ORG> --name <NAME> --version <VERSION> [--public] [--json]

Publish one file, or a directory with prose-package.json listing its files, exports
and pinned dependencies. Private is the default.

Arguments:
  FILE|DIR                    The file or package directory to publish.

Options:
  --organization ORG          Organization that owns the package. Required.
  --name NAME                 Package name. Required.
  --version VERSION           Exact version to publish. Required.
  --public                    Make the version publicly discoverable.
  --json                      Print one JSON result; the same as the global --output json.
  --help                      Show help for this command.
""",
    "cli package fetch": """Usage: prose [GLOBAL OPTIONS] cli package fetch <ORG>/<NAME>@<VERSION> --output-dir <FRESH_DIR> [--sha256 <DIGEST>] [--json]

Fetch one exact version, verify canonical bytes and receipt hashes, then create a
fresh directory. It never overwrites an existing directory. A target without its
receipt after an interruption is incomplete; there is no automatic resume.

Arguments:
  ORG/NAME@VERSION            Exact package version.

Options:
  --output-dir FRESH_DIR      Directory to create; it must not exist. Required.
  --sha256 DIGEST             Expected package digest.
  --json                      Print one JSON result; the same as the global --output json.
  --help                      Show help for this command.
""",
    "cli package list": """Usage: prose [GLOBAL OPTIONS] cli package list <ORG> [--cursor <CURSOR>] [--json]

List an organization's public packages, one page at a time. An organization with
no public packages prints `No public packages in ORG.` --output may also follow
the command.

Arguments:
  ORG                         Organization slug (see `cli org list`).

Options:
  --cursor CURSOR             The next-page cursor printed by the previous page.
  --json                      Print one JSON result; the same as the global --output json.
  --help                      Show help for this command.
""",
    "cli package withdraw": """Usage: prose [GLOBAL OPTIONS] cli package withdraw <ORG>/<NAME>@<VERSION> [--json]

Withdraw one version from discovery. Pinned retrieval of that exact version keeps
working.

Arguments:
  ORG/NAME@VERSION            Exact package version.

Options:
  --json                      Print one JSON result; the same as the global --output json.
  --help                      Show help for this command.
""",
}

# The `--yes` line renders the operation's manifest
# `confirmReason` (its consequence). A reason that only restates that
# confirmation is needed is circular and fails `--check`.
CIRCULAR_REASON = re.compile(r"confirm|requires? (?:explicit )?approval|service requires", re.IGNORECASE)


def command_path(operation: dict) -> list[str]:
    """An operation's command path: its manifest `command` argv without the
    leading `cli`."""
    command = list(operation["command"])
    return command[1:] if command[:1] == ["cli"] else command


def option_label(option: dict) -> str:
    return option["name"] + (f" {option['value']}" if option["value"] else "")


def argument_label(argument: dict) -> str:
    name = argument["name"]
    return f"<{name}>" if argument["required"] else f"[{name}]"


def rows(pairs: list[tuple[str, str]]) -> list[str]:
    lines = []
    for label, text in pairs:
        if len(label) + 2 > WIDTH:
            lines.append(f"  {label}")
            lines.append(" " * (WIDTH + 2) + text)
        else:
            lines.append(f"  {label.ljust(WIDTH)}{text}")
    return lines


def examples_lines(examples: list[str]) -> list[str]:
    """The `Examples:` section of a help topic: the manifest's
    runnable examples, one per line, followed by a blank line."""
    return ["Examples:", *(f"  {example}" for example in examples), ""]


def group_examples(manifest: dict, prefix: tuple[str, ...]) -> list[str]:
    """One example per child of a command group, in command order: the child
    operation's first manifest example, or for a child group the first example
    of its first operation in manifest order."""
    examples: list[str] = []
    children: dict[str, list[dict]] = {}
    for operation in manifest["operations"]:
        command = tuple(command_path(operation))
        if command[:len(prefix)] == prefix and len(command) > len(prefix):
            children.setdefault(command[len(prefix)], []).append(operation)
    for name in command_order(prefix, children):
        examples.append(first_operation(prefix + (name,), children[name])["examples"][0])
    return examples


def first_operation(prefix: tuple[str, ...], operations: list[dict]) -> dict:
    """The operation a group's help shows first: the group's own operation,
    else the first child in help order (see COMMAND_ORDER)."""
    exact = [o for o in operations if len(command_path(o)) == len(prefix)]
    if exact or prefix not in COMMAND_ORDER:
        return (exact or operations)[0]
    children: dict[str, list[dict]] = {}
    for operation in operations:
        children.setdefault(command_path(operation)[len(prefix)], []).append(operation)
    name = command_order(prefix, children)[0]
    return first_operation(prefix + (name,), children[name])


def topic_examples(manifest: dict, topic: str) -> list[str]:
    """The examples a help topic prints: an operation's own, or its group's."""
    words = tuple(topic.split(" ")[1:])
    for operation in manifest["operations"]:
        if tuple(command_path(operation)) == words:
            return operation["examples"]
    return group_examples(manifest, words)


def with_examples(text: str, examples: list[str]) -> str:
    """Insert the Examples section into a hand-written topic: before its
    global options, or before its Output line."""
    block = "\n".join(examples_lines(examples)) + "\n"
    for marker in ("\nGlobal options:\n", "\nOutput: "):
        if marker in text:
            head, tail = text.split(marker, 1)
            return head + "\n" + block + marker.lstrip("\n") + tail
    return text.rstrip("\n") + "\n\n" + block


def operation_help(manifest: dict, operation: dict) -> str:
    command = " ".join(command_path(operation))
    usage_parts = ["prose [GLOBAL OPTIONS] cli", command]
    usage_parts += [argument_label(a) for a in operation["arguments"]]
    usage_parts.append("[OPTIONS]")
    lines = ["Usage: " + " ".join(usage_parts), "", operation["summary"], ""]
    if operation["arguments"]:
        lines.append("Arguments:")
        lines += rows([(a["name"], a["description"]) for a in operation["arguments"]])
        lines.append("")
    options = []
    for option in operation["options"]:
        text = option["description"]
        if option.get("choices"):
            text += " One of: " + ", ".join(option["choices"]) + "."
        if option.get("default"):
            text += f" Default: {option['default']}."
        if option["required"]:
            text += " Required."
        if option["repeatable"]:
            text += " Repeatable."
        options.append((option_label(option), text))
    common = {o["name"]: o for o in manifest["grammar"]["commonOptions"]}
    if operation["confirm"]:
        options.append(("--yes", f"Confirm this operation; required because {operation['confirmReason']}."))
    if operation["preview"]:
        options.append(("--preview", common["--preview"]["description"]))
    options.append(("--json", common["--json"]["description"]))
    options.append(("--help", common["--help"]["description"]))
    lines.append("Options:")
    lines += rows(options)
    lines.append("")
    lines += examples_lines(operation["examples"])
    # The service requests and interactions an operation makes are contract
    # data for agents: `cli service operations --json` carries them, human
    # help does not.
    output = operation["output"]
    if output["stream"]:
        lines.append("Output: openprose.service-operation/1 (--json) or openprose.service-event/1 lines (--output jsonl)."
                     + OUTPUT_NOTES.get(operation["id"], ""))
    elif operation["id"] == "service.operations":
        lines.append("Output: a summary table; the manifest (openprose.service-operations/1) byte for byte with --json;"
                     " one compact operation per line with --output jsonl.")
    elif operation["id"] == "service.capabilities":
        lines.append("Output: openprose.service-capabilities/1 (--json).")
    elif operation["id"] == "service.guide":
        lines.append("Output: the guide as Markdown (shared/service/guide.v1.md); with --json, its sections"
                     " as openprose.service-operation/1 result {sections: [{id, title, body}]}.")
    else:
        lines.append("Output: openprose.service-operation/1 (--json).")
    if output["paged"]:
        lines.append("Paging: pass the result's nextBefore to --before for the next page.")
    lines.append(exit_line(operation))
    return "\n".join(lines) + "\n"


# What a script reads from a streaming command's result, after its Output line.
OUTPUT_NOTES = {
    "run.submit": " The run id is result.runId (and result.run.run_id once the run ends); the answer is result.run.response.",
    "run.watch": " The run id is result.runId (and result.run.run_id once the run ends); the answer is result.run.response.",
}


def confirm_reason_problems(manifest: dict) -> list[str]:
    """Every confirm-class operation states the consequence that
    needs --yes, no reason is circular ("requires confirmation"), and no other
    operation carries one."""
    problems = []
    for operation in manifest["operations"]:
        name = operation["id"]
        reason = operation.get("confirmReason")
        if not operation["confirm"]:
            if reason is not None:
                problems.append(f"{name}: confirmReason on an operation that does not confirm")
            continue
        if not isinstance(reason, str) or len(reason) < 20:
            problems.append(f"{name}: confirm-class operation needs a confirmReason naming its consequence")
        elif CIRCULAR_REASON.search(reason):
            problems.append(f"{name}: confirmReason {reason!r} is circular; name what happens, not that confirmation is needed")
    return problems


def exit_line(operation: dict) -> str:
    """The help's exit-code line, rendered from the manifest `exitCodes`."""
    return "Exit codes: " + "; ".join(f"{entry['exit']} {entry['meaning']}" for entry in operation["exitCodes"]) + "."


def output_line(operation: dict) -> str:
    """`Output: openprose.<name>/1 (--json).` for an account operation's closed schema file."""
    name = operation["output"]["schema"].removesuffix(".json").removesuffix(".schema")
    return f"Output: openprose.{name}/1 (--json)."


def account_help(operation: dict, body: str) -> str:
    return body + "\n" + output_line(operation) + "\n" + exit_line(operation) + "\n"


def group_help(manifest: dict, prefix: tuple[str, ...]) -> str:
    children: dict[str, list[dict]] = {}
    for operation in manifest["operations"]:
        command = tuple(command_path(operation))
        if command[:len(prefix)] == prefix and len(command) > len(prefix):
            children.setdefault(command[len(prefix)], []).append(operation)
    topic = " ".join(("cli",) + prefix)
    lines = [f"Usage: prose [GLOBAL OPTIONS] {topic} <COMMAND> [ARGUMENTS] [OPTIONS]", ""]
    if not prefix:
        lines += ["OpenProse service and account commands. They reach the hosted OpenProse service",
                  "and never prompt. New here? `prose cli service guide` walks through a first",
                  "program, a daily schedule, scripting and costs. Commands for this machine",
                  "(doctor, config) are listed by `prose --help`.", ""]
    lines += GROUP_INTRO.get(prefix, [])
    lines.append("Commands:")
    pairs = []
    for name in command_order(prefix, children):
        operations = children[name]
        exact = [o for o in operations if len(command_path(o)) == len(prefix) + 1]
        verbs = command_order(prefix + (name,), {command_path(o)[len(prefix) + 1] for o in operations if len(command_path(o)) > len(prefix) + 1})
        summary = exact[0]["summary"] if exact else "Commands: " + ", ".join(verbs) + "."
        if not prefix and name in GROUP_SUMMARY:
            summary = GROUP_SUMMARY[name] + ": " + ", ".join(verbs) + "."
            pairs.append((name, summary))
            continue
        pairs.append((name, summary.split("\n")[0].split(". ")[0].rstrip(".") + "."))
    lines += rows(pairs)
    lines += ["", *examples_lines(group_examples(manifest, prefix))[:-1]]
    lines += ["", *ACCOUNT_GLOBALS.rstrip("\n").split("\n")]
    if prefix == ("auth",):
        lines += ["", *(CREDENTIALS + "\n" + DEVICE_FLOW).rstrip("\n").split("\n")]
    lines += ["", f"Run `prose {topic} <COMMAND> --help` for details. "
              "`prose cli service operations --json` prints every command as JSON."]
    return "\n".join(lines) + "\n"


def render(manifest_bytes: bytes) -> str:
    manifest = json.loads(manifest_bytes)
    topics: dict[str, str] = {}
    groups = {()}
    for operation in manifest["operations"]:
        command = tuple(command_path(operation))
        for depth in range(1, len(command)):
            groups.add(command[:depth])
        if operation["contract"] == "service/1":
            topics["cli " + " ".join(command)] = operation_help(manifest, operation)
    for group in groups:
        topics[" ".join(("cli",) + group)] = group_help(manifest, group)
    by_topic = {"cli " + " ".join(command_path(operation)): operation for operation in manifest["operations"]}
    for topic, body in {**ACCOUNT_TOPICS, **PACKAGE_TOPICS}.items():
        text = account_help(by_topic[topic], body) if topic in by_topic else body
        topics[topic] = with_examples(text, topic_examples(manifest, topic))
    sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    capabilities = capabilities_document(manifest, published_manifest_sha256(manifest))
    document = {
        "schema": "openprose.service-help/1",
        "manifestSha256": sha256,
        "topics": dict(sorted(topics.items())),
        "capabilities": capabilities,
        "views": {
            "cli service capabilities": capabilities_text(manifest, capabilities),
            "cli service operations": operations_table(manifest, topics),
        },
    }
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


# ---------------------------------------------------------------- capabilities
# `cli service capabilities` and the human `cli service operations` table are
# rendered here, once, and embedded by both ports (like the help topics), so
# their bytes cannot drift between Rust and Bun. `--output jsonl` of
# `service operations` is the only view the ports compute: one canonical line
# per manifest operation.
CAPABILITIES_SCHEMA = "openprose.service-capabilities/1"
EXIT_WIDTH = 4
CODE_WIDTH = 30


def usage_of(operation: dict, topics: dict[str, str]) -> str:
    """The operation's usage without `prose [GLOBAL OPTIONS] `: service
    operations name their required options before [OPTIONS]; the frozen account
    ones use their hand-written help topic's usage line."""
    if operation["contract"] == "service/1":
        parts = ["cli", *command_path(operation), *(argument_label(a) for a in operation["arguments"])]
        parts += [option_label(o) for o in operation["options"] if o["required"]]
        return " ".join(parts + ["[OPTIONS]"])
    first = topics["cli " + " ".join(command_path(operation))].split("\n", 1)[0]
    return first.removeprefix("Usage: prose [GLOBAL OPTIONS] ")


def noun_index(manifest: dict) -> dict[str, list[str]]:
    nouns: dict[str, set[str]] = {}
    for operation in manifest["operations"]:
        nouns.setdefault(command_path(operation)[0], set())
        if len(command_path(operation)) > 1:
            nouns[command_path(operation)[0]].add(" ".join(command_path(operation)[1:]))
    return {noun: sorted(verbs) for noun, verbs in sorted(nouns.items())}


# `cli service operations` publishes the manifest reduced to its public
# projection (`operations-public.v1.json`): an allowlist of members.
PUBLIC_PROJECTION = SERVICE / "operations-public.v1.json"


def project_fields(value, fields):
    """`value` reduced to the members `fields` lists: `true` copies a member
    whole, an object applies the same allowlist to that member (to each
    element of an array)."""
    if fields is True:
        return value
    if isinstance(value, list):
        return [project_fields(item, fields) for item in value]
    if isinstance(value, dict) and isinstance(fields, dict):
        return {key: project_fields(value[key], sub) for key, sub in fields.items() if key in value}
    return value


def published_manifest_sha256(manifest: dict) -> str:
    """SHA-256 of the published manifest as `cli service operations --json`
    prints it in `result`: canonical JSON (compact, keys sorted, UTF-8)."""
    published = project_fields(manifest, json.loads(PUBLIC_PROJECTION.read_bytes())["fields"])
    text = json.dumps(published, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def capabilities_document(manifest: dict, sha256: str) -> dict:
    grammar = manifest["grammar"]
    return {
        "schema": CAPABILITIES_SCHEMA,
        "contract": manifest["contract"],
        "manifest": {"schema": manifest["schema"], "sha256": sha256,
                     "argv": ["cli", "service", "operations", "--json"]},
        "grammar": {
            "usage": grammar["usage"],
            "placement": grammar["placement"],
            "globalOptions": grammar["globalOptions"],
            "rejectedGlobalOptions": grammar["rejectedGlobalOptions"],
            "commonOptions": [option["name"] for option in grammar["commonOptions"]],
            "outputModes": ["human", "json", "jsonl"],
            "help": "prose cli <COMMAND> --help",
        },
        "exitCodes": manifest["exitCodes"],
        "envVars": manifest["envVars"],
        "nouns": noun_index(manifest),
        "operations": [{
            "id": operation["id"],
            "command": list(operation["command"]),
            "summary": operation["summary"],
            "effect": operation["effect"],
            "confirm": operation["confirm"],
            "examples": operation["examples"],
        } for operation in manifest["operations"]],
    }


# The human view of the grammar's placement rule (the precise rule stays in
# the JSON document and the manifest).
PLACEMENT_TEXT = ("Global options such as --output go before `cli`; command options go after the command. "
                  "A mistyped or misplaced word is never guessed: the command exits 2 and its error shows "
                  "the corrected command to run instead.")


def capabilities_text(manifest: dict, document: dict) -> str:
    grammar = manifest["grammar"]
    lines = ["OpenProse service commands: capabilities. "
             "`prose cli service capabilities --json` prints this page as one JSON document.", "",
             "Usage: " + grammar["usage"], PLACEMENT_TEXT,
             "Common options: " + ", ".join(document["grammar"]["commonOptions"]) + ".", "",
             "Environment variables:"]
    lines += rows([(variable["name"], variable["description"]) for variable in manifest["envVars"]])
    lines += ["", "Exit codes (0 is success; with --json, the error's code is in problem.code):",
              "  " + "0".ljust(EXIT_WIDTH) + "success"]
    for code, entry in sorted(manifest["exitCodes"].items(), key=lambda item: (item[1]["exit"], item[0])):
        text = entry["meaning"] + (" Retryable." if entry["retryable"] else "")
        lines.append("  " + str(entry["exit"]).ljust(EXIT_WIDTH) + code.ljust(CODE_WIDTH) + text)
    lines += ["", "Commands:"]
    for noun, verbs in document["nouns"].items():
        if verbs:
            text = ", ".join(verbs) + "."
        else:
            text = next(o for o in manifest["operations"] if command_path(o) == [noun])["summary"].split(". ")[0].rstrip(".") + "."
        lines += rows([(noun, text)])
    lines += ["", "Run `prose cli <COMMAND> --help` for one command, `prose cli service operations` for every "
                  "command with its effect and usage, and `prose cli service operations --json` for every command as JSON."]
    return "\n".join(lines) + "\n"


def operations_table(manifest: dict, topics: dict[str, str]) -> str:
    operations = sorted(manifest["operations"], key=lambda operation: operation["id"])
    id_width = max(len(operation["id"]) for operation in operations) + 2
    effect_width = max(len("EFFECT"), *(len(operation["effect"]) for operation in operations)) + 2
    confirm_width = len("CONFIRM") + 2
    lines = ["OPERATION".ljust(id_width) + "EFFECT".ljust(effect_width) + "CONFIRM".ljust(confirm_width) + "USAGE"]
    for operation in operations:
        lines.append(operation["id"].ljust(id_width) + operation["effect"].ljust(effect_width)
                     + ("yes" if operation["confirm"] else "no").ljust(confirm_width) + usage_of(operation, topics))
    lines += ["", "Details: `prose cli <COMMAND> --help`. Every field as JSON: `prose cli service operations --json`; "
                  "one operation per line: `prose --output jsonl cli service operations`."]
    return "\n".join(lines) + "\n"


TAXONOMY = CLI / "shared" / "errors" / "taxonomy.v1.json"
# Runner commands outside the manifest (mirrors RUNNER_COMMANDS in both ports).
RUNNER_PATHS = (("doctor",), ("harness", "list"), ("harness", "use"), ("cleanup", "prime"), ("config", "explain"))
# What an agent needs from `prose --help` before anything else.
RUNNER_HELP_REQUIRED = (
    "For agents:",
    "prose cli service status --json",
    "prose cli service triage --json",
    "prose cli service capabilities --json",
    "prose cli service guide",
    "details.suggestedArgv",
    "OPENPROSE_API_KEY",
    "PROSE_OUTPUT",
    "not `prose run list`",
    "`prose -- <WORDS>`",
)
# An agent reads the top of `prose --help`; its entry points come first.
RUNNER_HELP_AGENTS_WITHIN = 25
HELP_COMMAND = re.compile(r"^\s*prose cli ((?:[a-z][a-z-]*(?:\|[a-z][a-z-]*)*)(?: [a-z][a-z-]*(?:\|[a-z][a-z-]*)*)*)")


def help_command_paths(text: str) -> list[tuple[str, ...]]:
    """Every command path a `prose cli a|b c|d` syntax line of the help names."""
    paths: list[tuple[str, ...]] = []
    for line in text.splitlines():
        match = HELP_COMMAND.match(line)
        if match:
            paths += list(itertools.product(*(word.split("|") for word in match.group(1).split(" "))))
    return paths


def runner_help_problems(manifest: dict, text: str, taxonomy: dict | None = None) -> list[str]:
    """`prose --help` must name every service command, only real
    commands, every exit code of the error taxonomy, the
    environment variables and the agent entry points."""
    commands = {tuple(command_path(operation)) for operation in manifest["operations"]} | set(RUNNER_PATHS)
    nouns = sorted({command_path(operation)[0] for operation in manifest["operations"]})
    problems = [f"runner-help.txt does not list `prose cli {noun}`" for noun in nouns
                if f"prose cli {noun}" not in text and f"cli {noun} " not in text]
    for pointer in ("prose cli --help", "prose cli service operations --json", *RUNNER_HELP_REQUIRED):
        if pointer not in text:
            problems.append(f"runner-help.txt does not point to `{pointer}`")
    head = text.splitlines()[:RUNNER_HELP_AGENTS_WITHIN]
    if "For agents:" not in head:
        problems.append(f"runner-help.txt: the `For agents:` section must start within the first "
                        f"{RUNNER_HELP_AGENTS_WITHIN} lines, before the runner option list")
    listed = help_command_paths(text)
    for path in listed:
        real = any(path[:len(command)] == command for command in commands) or any(
            command[:len(path)] == path for command in commands)
        if not real:
            problems.append(f"runner-help.txt names `prose cli {' '.join(path)}`, which is not a command")
    for command in sorted(commands - set(RUNNER_PATHS)):
        if not any(path[:len(command)] == command for path in listed):
            problems.append(f"runner-help.txt does not list `prose cli {' '.join(command)}`")
    taxonomy = taxonomy if taxonomy is not None else json.loads(TAXONOMY.read_bytes())
    exits = sorted({0, *(error["exitCode"] for error in taxonomy["errors"])})
    section = text.split("\nExit codes", 1)[1] if "\nExit codes" in text else ""
    for code in exits:
        if not re.search(rf"^  {code} +\S", section, re.MULTILINE):
            problems.append(f"runner-help.txt exit codes do not list {code}")
    return problems


SERVICE_CASES = CLI / "conformance" / "cases" / "service"
ERROR_SCHEMA = "openprose.runner-error/1"
# An interrupt during any service request or credential-store call is
# CANCELLED (exit 24) in both ports (http `check()`), so an operation may list
# it without a corpus case of its own; every other listed code needs one.
ANY_REQUEST_CODES = frozenset({"CANCELLED"})


def _problem_codes(value: object, codes: set[str]) -> None:
    if isinstance(value, dict):
        if value.get("schema") == ERROR_SCHEMA and isinstance(value.get("code"), str):
            codes.add(value["code"])
        for child in value.values():
            _problem_codes(child, codes)
    elif isinstance(value, list):
        for child in value:
            _problem_codes(child, codes)


def _operation_for(manifest: dict, argv: list[str]) -> str | None:
    """The manifest operation an argv names (longest command path after `cli`)."""
    if "cli" not in argv:
        return None
    words = argv[argv.index("cli") + 1:]
    best = None
    for operation in manifest["operations"]:
        command = command_path(operation)
        if words[:len(command)] == command and (best is None or len(command_path(best)) < len(command)):
            best = operation
    return best["id"] if best else None


def emitted_exit_codes(manifest: dict, taxonomy: dict) -> list[tuple[str, int, str | None, str]]:
    """(operation, exit, error code or None, case) for every shared corpus
    case under cases/service/ (service cases and the account
    cases). A text-only expectation contributes the taxonomy codes it names."""
    known = {error["code"] for error in taxonomy["errors"]}
    emitted: list[tuple[str, int, str | None, str]] = []

    def add(operation: str | None, exit_code: object, expected: object, case: str) -> None:
        if operation is None or not isinstance(exit_code, int):
            return
        codes: set[str] = set()
        _problem_codes(expected, codes)
        if not codes and exit_code != 0:
            for text in re.findall(r'"text": "([^"]*)"', json.dumps(expected)):
                codes |= {word for word in re.findall(r"\b[A-Z][A-Z0-9_]{4,}\b", text) if word in known}
        codes = {code for code in codes if next(e["exitCode"] for e in taxonomy["errors"] if e["code"] == code) == exit_code} \
            if codes else codes
        for code in sorted(codes) or [None]:
            emitted.append((operation, exit_code, code, case))

    for path in sorted(SERVICE_CASES.glob("*/*.json")):
        case = json.loads(path.read_text())
        # A language-forwarded argv is the runner's, not the operation's: its
        # runner error (HOSTED_UNAVAILABLE) is no service exit code.
        if case.get("languageForwarded"):
            continue
        operation = _operation_for(manifest, case["argv"]) or case.get("operation")
        add(operation, case.get("exitCode"), [case.get("stdout"), case.get("stderr")], path.stem)
    return emitted


def exit_code_problems(manifest: dict, taxonomy: dict | None = None,
                       emitted: list[tuple[str, int, str | None, str]] | None = None) -> list[str]:
    """Each operation's manifest `exitCodes` (the source of its help's exit
    line) must agree with the taxonomy and with what the shared corpora show
    the operation emitting:

    - entries are ascending and start with 0 success; each exit is a taxonomy exit;
    - a listed code exists and has that exit in the taxonomy; exits 20 and above
      list their codes, because those are the ones an agent acts on;
    - a code the service assigns only through a route override (for example
      RUN_SUBMISSION_AMBIGUOUS on POST /run) is listed only by an operation
      that sends that request;
    - every (exit, code) a corpus case of the operation expects is listed, and
      every listed code is expected by at least one case of the operation.
    """
    taxonomy = taxonomy if taxonomy is not None else json.loads(TAXONOMY.read_bytes())
    exits = {error["code"]: error["exitCode"] for error in taxonomy["errors"]}
    classification = manifest["errorClassification"]
    general = set(classification["bodyCodes"].values()) | set(classification["statuses"].values())
    route_only: dict[str, set[tuple[str, str]]] = {}
    for override in classification["routeOverrides"]:
        if override["code"] not in general:
            route_only.setdefault(override["code"], set()).add((override["method"], override["path"]))
    emitted = emitted if emitted is not None else emitted_exit_codes(manifest, taxonomy)
    problems: list[str] = []
    for operation in manifest["operations"]:
        name = operation["id"]
        entries = operation["exitCodes"]
        listed = [entry["exit"] for entry in entries]
        if listed != sorted(set(listed)) or listed[:1] != [0]:
            problems.append(f"{name}: exitCodes must be ascending, unique and start with 0")
        routes = {(request["method"], request["path"]) for request in operation["requests"]}
        for entry in entries:
            if entry["exit"] != 0 and entry["exit"] not in exits.values():
                problems.append(f"{name}: exit {entry['exit']} is not a taxonomy exit code")
            if entry["exit"] >= 20 and not entry.get("codes"):
                problems.append(f"{name}: exit {entry['exit']} must list its error codes")
            for code in entry.get("codes", []):
                if exits.get(code) != entry["exit"]:
                    problems.append(f"{name}: {code} is exit {exits.get(code)} in the taxonomy, not {entry['exit']}")
                if code in route_only and not route_only[code] & routes:
                    problems.append(f"{name}: lists {code}, which only "
                                    + ", ".join(sorted(f"{m} {p}" for m, p in route_only[code]))
                                    + " can produce, but sends no such request")
                if code not in ANY_REQUEST_CODES and not any(o == name and c == code for o, _, c, _ in emitted):
                    problems.append(f"{name}: lists {code}, but no shared corpus case of {name} expects it")
        by_exit = {entry["exit"]: entry for entry in entries}
        for operation_id, exit_code, code, case in emitted:
            if operation_id != name:
                continue
            entry = by_exit.get(exit_code)
            if entry is None:
                problems.append(f"{name}: case {case} exits {exit_code}"
                                + (f" ({code})" if code else "") + ", which its exitCodes and help do not list")
            elif code is not None and "codes" in entry and code not in entry["codes"]:
                problems.append(f"{name}: case {case} exits {exit_code} with {code}, which exitCodes lists only as "
                                + ", ".join(entry["codes"]))
    return problems


# Codes a service operation can end with although no shared corpus case shows
# them; everything else in the manifest exit dictionary must be emitted by a
# case, listed by an operation or be an errorClassification target.
UNCASED_DICTIONARY_CODES = {
    "CANCELLED": "an interrupt during any request (ANY_REQUEST_CODES)",
    "CONFIG_INVALID": "a malformed user cli.toml or PROSE_OUTPUT, for every command",
    "DEVICE_AUTH_EXPIRED": "`cli auth login` device flow timed out",
    "DEVICE_AUTH_FAILED": "`cli auth login` device flow refused",
    "INTERNAL_ERROR": "an internal fault in either port",
    "SERVICE_RESPONSE_TOO_LARGE": "a 2xx body over its transport class limit",
}


def exit_dictionary_problems(manifest: dict, taxonomy: dict | None = None,
                             emitted: list[tuple[str, int, str | None, str]] | None = None) -> list[str]:
    """The manifest-level `exitCodes` dictionary is what
    `cli service capabilities` prints, so an agent can `case $?` on it:

    - every entry equals the taxonomy: exit, retryable and meaning (its message);
    - every code any shared corpus case emits appears, with the exit the case expects;
    - every code an operation's exitCodes lists and every errorClassification
      target (body codes, statuses, route overrides, confirmation) appears;
    - nothing else appears unless UNCASED_DICTIONARY_CODES says why.
    """
    taxonomy = taxonomy if taxonomy is not None else json.loads(TAXONOMY.read_bytes())
    by_code = {error["code"]: error for error in taxonomy["errors"]}
    dictionary = manifest.get("exitCodes", {})
    emitted = emitted if emitted is not None else emitted_exit_codes(manifest, taxonomy)
    problems: list[str] = []
    for code, entry in dictionary.items():
        error = by_code.get(code)
        if error is None:
            problems.append(f"exitCodes: {code} is not a taxonomy code")
            continue
        for field, source in (("exit", "exitCode"), ("retryable", "retryable"), ("meaning", "message")):
            if entry.get(field) != error[source]:
                problems.append(f"exitCodes: {code}.{field} is {entry.get(field)!r}; the taxonomy {source} is {error[source]!r}")
    for operation_id, exit_code, code, case in emitted:
        if code is None:
            continue
        if code not in dictionary:
            problems.append(f"exitCodes: case {case} ({operation_id}) emits {code}, which the exit dictionary lacks")
        elif dictionary[code].get("exit") != exit_code:
            problems.append(f"exitCodes: case {case} exits {exit_code} with {code}; the dictionary says {dictionary[code].get('exit')}")
    classification = manifest["errorClassification"]
    required = {manifest["confirmation"]["code"]}
    required |= set(classification["bodyCodes"].values()) | set(classification["statuses"].values())
    required |= {override["code"] for override in classification["routeOverrides"]}
    if isinstance(classification.get("otherStatus"), str):
        required.add(classification["otherStatus"])
    for operation in manifest["operations"]:
        for entry in operation["exitCodes"]:
            required |= set(entry.get("codes", []))
    problems += [f"exitCodes: {code} (listed by an operation or the error classification) is missing"
                 for code in sorted(required - set(dictionary))]
    shown = {code for _, _, code, _ in emitted if code}
    for code in sorted(set(dictionary) - shown - required - set(UNCASED_DICTIONARY_CODES)):
        problems.append(f"exitCodes: {code} is emitted by no corpus case and no operation lists it")
    return problems


GLOBAL_VALUE_OPTIONS = {"--output"}


def argv_problems(manifest: dict, operation: dict, rest: list[str], line: str) -> list[str]:
    """Problems of one command line's words after the command path: only the
    operation's options (service operations), every required option and
    argument (unless it asks for --help), and --yes or --preview on a
    confirm-class command, so it never stops at CONFIRMATION_REQUIRED."""
    common = {option["name"] for option in manifest["grammar"]["commonOptions"]}
    options = {option["name"]: option for option in operation["options"]}
    name = operation["id"]
    problems: list[str] = []
    positional: list[str] = []
    used: set[str] = set()
    position = 0
    while position < len(rest):
        word = rest[position]
        if word.startswith("--"):
            used.add(word)
            if (word in options and options[word]["value"] is not None) or word in GLOBAL_VALUE_OPTIONS:
                position += 1
            elif operation["contract"] == "service/1" and word not in options and word not in common:
                problems.append(f"{name}: {line!r} uses {word}, which {name} does not take")
        else:
            positional.append(word)
        position += 1
    if "--help" in used:
        return problems
    if operation["contract"] == "service/1":
        for option in operation["options"]:
            if option["required"] and option["name"] not in used:
                problems.append(f"{name}: {line!r} lacks the required {option['name']}")
        required = [argument for argument in operation["arguments"] if argument["required"]]
        if len(positional) < len(required):
            problems.append(f"{name}: {line!r} lacks a required argument")
        if not any(argument["variadic"] for argument in operation["arguments"]) \
                and len(positional) > len(operation["arguments"]):
            problems.append(f"{name}: {line!r} has more arguments than {name} takes")
    if operation["confirm"] and not used & {"--yes", "--preview"}:
        problems.append(f"{name}: confirm-class {line!r} carries neither --yes nor --preview")
    if "rr_test_" in line:
        problems.append(f"{name}: {line!r} contains a key")
    return problems


def command_start(words: list[str]) -> int:
    """Index of `cli` in `prose [GLOBAL OPTIONS] cli ...` (after the global options)."""
    index = 1
    while index < len(words) and words[index] in GLOBAL_VALUE_OPTIONS:
        index += 2
    return index


def example_problems(manifest: dict) -> list[str]:
    """Every operation carries runnable `examples`: each starts
    `prose `, takes only the global options before `cli`, names exactly the
    operation's command path, uses only its options (service operations),
    passes every required option and argument, and a confirm-class example
    carries --yes or --preview, so it never stops at CONFIRMATION_REQUIRED."""
    import shlex
    problems: list[str] = []
    for operation in manifest["operations"]:
        name = operation["id"]
        if not operation.get("examples"):
            problems.append(f"{name}: no examples")
            continue
        for example in operation["examples"]:
            words = shlex.split(example)
            if words[:1] != ["prose"]:
                problems.append(f"{name}: example {example!r} does not start with prose")
                continue
            index = command_start(words)
            command = command_path(operation)
            if words[index:index + 1] != ["cli"] or words[index + 1:index + 1 + len(command)] != command:
                problems.append(f"{name}: example {example!r} does not name `cli {' '.join(command)}` after the global options")
                continue
            problems += [problem.replace(f"{name}: ", f"{name}: example ", 1)
                         for problem in argv_problems(manifest, operation, words[index + 1 + len(command):], example)]
    return problems


# ---------------------------------------------------------------- guide
# `prose cli service guide` prints `shared/service/guide.v1.md` byte for byte
# (human) or its `## ` sections as {sections: [{id, title, body}]} in the
# service-operation envelope (--json). Both ports embed the file and split it
# with the rule `guide_sections` implements; the generated corpus cases below
# pin both views, and `--check` parses every `prose cli ...` command in the
# guide against the manifest.
GUIDE = SERVICE / "guide.v1.md"
GUIDE_TITLE = "# OpenProse service guide\n\n"
GUIDE_REQUIRED_SECTIONS = (
    "start-here", "grammar", "credentials", "confirm-and-preview",
    "exit-codes-resume-and-detach", "paging", "get-a-runs-answer", "recipes",
)
GUIDE_REQUIRED_RECIPES = ("### Submit and wait", "### Publish a result", "### Create a webhook job")
GUIDE_CLIENT_DOC = CLI.parent / "docs" / "hosted-service-client.md"
GUIDE_CASES = CLI / "conformance" / "cases" / "service" / "framework"
SHELL_SEPARATOR = re.compile(r"\s(?:\|\||&&|\||;|>>|2>|>|<)\s")


def guide_slug(title: str) -> str:
    """A section id: the title lowercased, apostrophes dropped, every other
    run of characters outside [a-z0-9] one hyphen, no leading or trailing
    hyphen (`Get a run's answer` -> `get-a-runs-answer`)."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower().replace("'", "")).strip("-")


def guide_sections(text: str) -> list[dict]:
    """The reference split both ports implement: a section starts at each line
    beginning `## `; its title is the rest of that line and its body every
    following line up to the next section, without leading or trailing LF.
    Text before the first section (the `# ` title) is not a section."""
    sections: list[dict] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            sections.append({"title": line[3:], "lines": []})
        elif sections:
            sections[-1]["lines"].append(line)
    return [{"id": guide_slug(section["title"]), "title": section["title"],
             "body": "\n".join(section["lines"]).strip("\n")} for section in sections]


def guide_commands(text: str) -> tuple[list[str], list[str]]:
    """The commands the guide names: full `prose ...` command lines (from
    sh code fences, split at shell operators, and from inline code spans) and
    bare `cli ...` inline spans, which must name a command path or group."""
    full: list[str] = []
    paths: list[str] = []
    fence = None
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = line[3:].strip() if fence is None else None
            continue
        if fence is not None:
            if fence == "sh":
                for segment in SHELL_SEPARATOR.split(" " + line.split(" #", 1)[0] + " "):
                    segment = segment.strip()
                    if segment.startswith("prose "):
                        full.append(segment)
            continue
        for span in re.findall(r"`([^`]+)`", line):
            if span.startswith("prose "):
                full.append(span)
            elif span == "cli" or span.startswith("cli "):
                paths.append(span)
    return full, paths


def command_line_problems(manifest: dict, line: str) -> list[str]:
    """One `prose ...` line of the guide parses against the manifest: global
    options, then `cli`, then an operation's command path (the longest that
    matches), then only that operation's options and arguments. A `<WORD>`
    placeholder or `...` in the command path makes the line a template: its
    fixed words must still start a real command path."""
    import shlex
    try:
        words = shlex.split(line)
    except ValueError as error:
        return [f"guide: {line!r} is not a shell command line ({error})"]
    index = command_start(words)
    if words[index:index + 1] != ["cli"]:
        return [f"guide: {line!r} does not put `cli` after the global options"]
    return path_problems(manifest, words[index + 1:], line, full=True)


def path_problems(manifest: dict, words: list[str], line: str, *, full: bool) -> list[str]:
    """`words` (after `cli`) name a command. With `full`, the rest must parse
    as that operation's arguments and options; a template (`<COMMAND>`,
    `...`) or a bare `cli ...` span only has to start a real command path."""
    commands = [tuple(command_path(operation)) for operation in manifest["operations"]]
    fixed: list[str] = []
    for word in words:
        if not re.fullmatch(r"[a-z][a-z-]*", word):
            break
        fixed.append(word)
    template = len(fixed) < len(words) and (words[len(fixed)].startswith("<") or words[len(fixed)] == "...")
    matches = [command for command in commands if tuple(fixed[:len(command)]) == command]
    # An alias spelling (`cli run status`) names its target command.
    aliases = manifest["grammar"]["intentInference"].get("commandAliases", {})
    for phrase, target in aliases.items():
        alias = tuple(phrase.split(" "))
        if tuple(fixed[:len(alias)]) == alias and len(alias) > max((len(m) for m in matches), default=0):
            if not full or template:
                return []
            operation = next(o for o in manifest["operations"] if command_path(o) == list(target))
            return [f"guide: {problem}" for problem in argv_problems(manifest, operation, words[len(alias):], line)]
    if matches:
        command = max(matches, key=len)
        if not full or template:
            return []
        operation = next(o for o in manifest["operations"] if tuple(command_path(o)) == command)
        return [f"guide: {problem}" for problem in argv_problems(manifest, operation, words[len(command):], line)]
    group = any(command[:len(fixed)] == tuple(fixed) for command in commands)
    if group and (template or not full or len(fixed) == len(words)):
        return []
    return [f"guide: {line!r} names no command (`cli {' '.join(fixed)}`)"]


def guide_problems(manifest: dict, text: str, client_doc: str) -> list[str]:
    """The guide keeps its contract: ASCII, the `# ` title alone before the
    first section, unique section ids including every required one, no `## `
    line inside a code fence (the ports split without tracking fences), the
    three recipes, a link from docs/hosted-service-client.md, and every command it
    names parses against the manifest."""
    problems: list[str] = []
    if not text.isascii():
        problems.append("guide: guide.v1.md must be ASCII")
    if not text.startswith(GUIDE_TITLE) or not text[len(GUIDE_TITLE):].startswith("## "):
        problems.append(f"guide: guide.v1.md must start with {GUIDE_TITLE!r} followed by the first `## ` section")
    if not text.endswith("\n") or text.endswith("\n\n"):
        problems.append("guide: guide.v1.md must end with exactly one LF")
    sections = guide_sections(text)
    ids = [section["id"] for section in sections]
    if len(ids) != len(set(ids)):
        problems.append(f"guide: duplicate section ids {sorted({i for i in ids if ids.count(i) > 1})}")
    problems += [f"guide: no `{wanted}` section" for wanted in GUIDE_REQUIRED_SECTIONS if wanted not in ids]
    problems += [f"guide: section {s['title']!r} has an empty body" for s in sections if not s["body"]]
    problems += [f"guide: no {recipe!r} recipe" for recipe in GUIDE_REQUIRED_RECIPES if f"\n{recipe}\n" not in text]
    fence = False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
        elif fence and line.startswith("## "):
            problems.append(f"guide: `## ` line inside a code fence: {line!r}")
    if "cli/shared/service/guide.v1.md" not in client_doc:
        problems.append("guide: docs/hosted-service-client.md does not link cli/shared/service/guide.v1.md")
    full, paths = guide_commands(text)
    if not full:
        problems.append("guide: names no `prose cli ...` command")
    for line in full:
        problems += command_line_problems(manifest, line)
    for span in paths:
        problems += path_problems(manifest, span.split()[1:], span, full=False)
    return problems


def guide_cases(topics: dict[str, str], text: str) -> dict[Path, str]:
    """The generated corpus cases that pin both views of the guide in both
    ports (`--write` writes them, `--check` fails when one is stale)."""
    def envelope() -> dict:
        return {"interaction": None, "operation": "service.guide", "problem": None,
                "result": {"sections": guide_sections(text)}, "schema": "openprose.service-operation/1"}

    generated = "Generated by ci/render_service_help.py --write."
    cases = [
        {"id": "service-guide-human",
         "description": "`cli service guide` prints shared/service/guide.v1.md byte for byte; no request, no key. " + generated,
         "argv": ["cli", "service", "guide"], "fixture": {"exchanges": []}, "exitCode": 0,
         "stdout": {"text": text}, "stderr": {"text": ""}},
        {"id": "service-guide-json",
         "description": "`--json` prints the guide's `## ` sections as {sections: [{id, title, body}]} in the service-operation envelope. " + generated,
         "argv": ["cli", "service", "guide", "--json"], "fixture": {"exchanges": []}, "exitCode": 0,
         "stdout": {"json": envelope()}},
        {"id": "service-guide-jsonl",
         "description": "The guide needs no key; jsonl mode prints the same single envelope line. " + generated,
         "argv": ["--output", "jsonl", "cli", "service", "guide"],
         "fixture": {"environment": "production", "credentials": {"production": None},
                     "storeAvailable": True, "exchanges": []},
         "exitCode": 0, "stdout": {"json": envelope()}},
        {"id": "help-service-guide-topic",
         "description": "`cli service guide --help` prints its generated topic. " + generated,
         "argv": ["cli", "service", "guide", "--help"], "fixture": {"exchanges": []}, "exitCode": 0,
         "stdout": {"text": topics.get("cli service guide", "")}, "stderr": {"text": ""}},
    ]
    rendered: dict[Path, str] = {}
    for case in cases:
        ordered = {"id": case.pop("id"), "feature": "framework", "operation": "service.guide", **case}
        rendered[GUIDE_CASES / f"{ordered['id']}.json"] = json.dumps(ordered, indent=2) + "\n"
    return rendered


def topic_problems(manifest: dict, topics: dict[str, str]) -> list[str]:
    """Every operation and command group has a topic, so no `cli <noun> [verb]
    --help` falls back to the runner help; every account verb topic says
    what an agent needs."""
    problems: list[str] = []
    for operation in manifest["operations"]:
        command = command_path(operation)
        for depth in range(1, len(command) + 1):
            topic = "cli " + " ".join(command[:depth])
            if topic not in topics:
                problems.append(f"no help topic for `{topic}`")
        topic = "cli " + " ".join(command)
        text = topics.get(topic, "")
        if "OpenProse outer runner" in text:
            problems.append(f"`{topic}` prints the runner help")
        if exit_line(operation) not in text:
            problems.append(f"`{topic}` does not print the exit codes of its manifest exitCodes")
        if operation["contract"] == "account/1" and command[0] != "package":
            problems += [f"`{topic}` does not say {needed.strip()!r}" for needed in ACCOUNT_TOPIC_REQUIRED
                         if needed not in text]
    problems += examples_problems(manifest, topics)
    return sorted(set(problems))


def examples_problems(manifest: dict, topics: dict[str, str]) -> list[str]:
    """Every help topic prints an `Examples:` section holding
    exactly its manifest examples (an operation's own, one per child for a
    group), so removing one from the manifest or the help fails `--check`."""
    problems: list[str] = []
    for topic, text in topics.items():
        expected = "\n".join(examples_lines(topic_examples(manifest, topic)))
        if "\nExamples:\n" not in text:
            problems.append(f"`{topic}` has no Examples section")
        elif "\n" + expected + "\n" not in text:
            problems.append(f"`{topic}` Examples section differs from its manifest examples")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render service help from the manifest")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = render(MANIFEST.read_bytes())
    guide_text = GUIDE.read_text() if GUIDE.is_file() else ""
    cases = guide_cases(json.loads(expected)["topics"], guide_text)
    if args.write:
        HELP.write_text(expected)
        for path, content in cases.items():
            path.write_text(content)
        print(f"Wrote {HELP.relative_to(CLI.parent)} ({len(json.loads(expected)['topics'])} topics)")
        return 0
    manifest = json.loads(MANIFEST.read_bytes())
    problems = runner_help_problems(manifest, RUNNER_HELP.read_text())
    problems += exit_code_problems(manifest)
    problems += exit_dictionary_problems(manifest)
    problems += example_problems(manifest)
    problems += confirm_reason_problems(manifest)
    problems += topic_problems(manifest, json.loads(expected)["topics"])
    problems += guide_problems(manifest, guide_text, GUIDE_CLIENT_DOC.read_text() if GUIDE_CLIENT_DOC.is_file() else "")
    problems += [f"{path.relative_to(CLI.parent)} is stale; run cli/ci/render_service_help.py --write"
                 for path, content in cases.items() if not path.is_file() or path.read_text() != content]
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    if not HELP.is_file() or HELP.read_text() != expected:
        print("FAIL: cli/shared/service/help.v1.json is stale; run cli/ci/render_service_help.py --write", file=sys.stderr)
        return 1
    print(f"PASS: service help matches the manifest ({len(json.loads(expected)['topics'])} topics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
