#!/usr/bin/env python3
"""Shared black-box corpus for service operations.

    python3 service_operations.py --validate
    python3 service_operations.py --list-features
    python3 service_operations.py --feature run -- /abs/path/to/prose
    python3 service_operations.py --all -- /abs/path/to/prose-test

`--validate` checks the operation manifest against its schema and every case
under `cases/service/<feature>/` against `service-fixture.schema.json`, the
result schemas, the event schema and the error taxonomy. It needs no product.

Running cases requires a test-seam product (`--features test-seams` or
`dist/prose-test`). Each case gets a fresh HOME/XDG root, the closed HTTP
transcript in PROSE_TEST_SERVICE_FIXTURE, dead proxies and no credentials
except those the case declares. stdout must equal the expected document
exactly (the fixture fixes clocks and ids, so nothing is normalized), and a
JSON or JSONL golden pins the stdout bytes: compact, object keys sorted
recursively, UTF-8, one line per document (`canonical_json.canonical_line`). `--all`
also runs the manifest identity probe: `cli service operations --json` must
print one envelope line whose result is `shared/service/operations.v1.json`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

from canonical_json import canonical_line

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError as error:  # pragma: no cover - actionable bootstrap failure
    raise SystemExit("Install the pinned test dependencies: python3 -m pip install -r cli/shared/requirements-test.txt") from error

RUNNER = Path(__file__).resolve().parent
CLI = RUNNER.parents[1]
SHARED = CLI / "shared"
MANIFEST = SHARED / "service" / "operations.v1.json"
CASES = CLI / "conformance" / "cases" / "service"
FIXTURE_SCHEMA = RUNNER / "service-fixture.schema.json"
FEATURES = ("framework", "discovery", "runs", "run-records", "programs", "results", "jobs", "wallet", "organizations", "account")
SAFE_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
TIMEOUT_SECONDS = 20
# Bare documents the corpus may expect, by `schema` value: only the runner
# error of a runner command (`cli doctor`, `cli harness ...`).
ACCOUNT_SCHEMAS = {
    "openprose.runner-error/1": "runner-error.schema.json",
}


# Every JSON or JSONL document a service argv prints is one of
# these service documents, which carry a `problem` member on failure.
# A bare `openprose.runner-error/1` is only for the language and the runner
# commands (`cli doctor`, `cli harness ...`, `cli config ...`, `cli cleanup ...`).
SERVICE_DOCUMENTS = ("openprose.service-operation/1", "openprose.service-event/1",
                     "openprose.service-record/1", "openprose.service-page/1")
REPORT_DOCUMENTS: tuple[str, ...] = ()
RUNNER_COMMAND_WORDS = ("doctor", "harness", "cleanup", "config")


def runner_argv(argv: list[str]) -> bool:
    """The command word after the first `cli` (before `--`) is a runner command."""
    head = argv[:argv.index("--")] if "--" in argv else argv
    if "cli" not in head:
        return False
    at = head.index("cli")
    return at + 1 < len(argv) and argv[at + 1] in RUNNER_COMMAND_WORDS


def one_shape_problems(argv: list[str], exit_code: int, documents: list) -> list[str]:
    """Invariant over a service argv's structured stdout: every
    document is a service document or account report, never a bare runner
    error, and on a non-zero exit the last document carries
    `.problem.details` (a terminal event: `.data.problem.details`)."""
    if runner_argv(argv) or not documents:
        return []
    problems = []
    for document in documents:
        schema = document.get("schema") if isinstance(document, dict) else None
        if schema not in SERVICE_DOCUMENTS + REPORT_DOCUMENTS:
            problems.append(f"one JSON shape: a service argv printed {schema!r}, not a service document")
    last = documents[-1] if isinstance(documents[-1], dict) else {}
    if exit_code != 0 and not problems:
        holder = last.get("data") if last.get("schema") == "openprose.service-event/1" else last
        problem = holder.get("problem") if isinstance(holder, dict) else None
        if not isinstance(problem, dict) or not isinstance(problem.get("details"), dict):
            problems.append(f"one JSON shape: exit {exit_code} without .problem.details")
    return problems


def load_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def schema_registry() -> Registry:
    registry = Registry()
    paths = [*sorted((SHARED / "schemas").glob("*.schema.json")),
             *sorted((SHARED / "schemas" / "service").glob("*.schema.json")),
             SHARED / "service" / "operations.schema.json", FIXTURE_SCHEMA]
    for path in paths:
        document = load_json(path)
        registry = registry.with_resource(document["$id"], Resource.from_contents(document))
    return registry


REGISTRY = None


def validator(schema: dict):
    global REGISTRY
    if REGISTRY is None:
        REGISTRY = schema_registry()
    return jsonschema.Draft202012Validator(schema, registry=REGISTRY, format_checker=jsonschema.FormatChecker())


def errors_of(schema: dict, instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message[:200]}"
            for e in sorted(validator(schema).iter_errors(instance), key=lambda e: list(e.absolute_path))]


SCHEMA_BASE = "https://schemas.openprose.org/cli/v1/"


def result_schema(operation: dict) -> dict | None:
    target = operation["output"]["schema"]
    if target is None or target == "operations.v1.json":
        return None
    return {"$ref": SCHEMA_BASE + target}


def segments(path: str) -> list[str]:
    return [p for p in path.split("?")[0].split("/") if p]


def path_matches(actual: str, template: str) -> bool:
    got, want = segments(actual), segments(template)
    if len(got) != len(want):
        return False
    return all(w.startswith("{") or g == w for g, w in zip(got, want))


def api_path_matches(actual: str, template: str) -> bool:
    """Like path_matches, and a trailing `{path}` matches zero or more segments (run files)."""
    want = segments(template)
    if want and want[-1] == "{path}":
        got = segments(actual)
        return len(got) >= len(want) - 1 and path_matches("/".join(got[:len(want) - 1]) or "/", "/".join(want[:-1]) or "/")
    return path_matches(actual, template)


def taxonomy() -> dict:
    return {e["code"]: e for e in load_json(SHARED / "errors" / "taxonomy.v1.json")["errors"]}


def iter_cases(features=None):
    if not CASES.is_dir():
        return
    for directory in sorted(p for p in CASES.iterdir() if p.is_dir()):
        if features and directory.name not in features:
            continue
        for path in sorted(directory.glob("*.json")):
            yield directory.name, path


def validate_manifest(manifest: dict) -> list[str]:
    problems = [f"manifest {e}" for e in errors_of(load_json(SHARED / "service" / "operations.schema.json"), manifest)]
    seen_ids = set()
    for operation in manifest["operations"]:
        oid = operation["id"]
        if oid in seen_ids:
            problems.append(f"duplicate operation {oid}")
        seen_ids.add(oid)
        if operation["feature"] not in FEATURES:
            problems.append(f"{oid}: unknown feature {operation['feature']}")
        names = [o["name"] for o in operation["options"]]
        if len(names) != len(set(names)):
            problems.append(f"{oid}: duplicate option names")
        # Rejected global options are rejected only before `cli`; an operation
        # may define its own option of the same name (run submit --model).
        reserved = {o["name"] for o in manifest["grammar"]["commonOptions"]}
        if reserved & set(names):
            problems.append(f"{oid}: redefines a common option {sorted(reserved & set(names))}")
        target = operation["output"]["schema"]
        if target and target != "operations.v1.json":
            file_part, _, fragment = target.partition("#")
            path = SHARED / "schemas" / file_part
            if not path.is_file():
                problems.append(f"{oid}: result schema file {file_part} missing")
            elif fragment:
                name = fragment.rsplit("/", 1)[-1]
                if name not in load_json(path).get("$defs", {}):
                    problems.append(f"{oid}: result schema {target} missing")
        if operation["confirm"] and operation["transport"] == "none":
            problems.append(f"{oid}: confirm-class operations must send a request")
        if operation["output"]["stream"] and operation["transport"] != "stream":
            problems.append(f"{oid}: streamed output requires the stream transport class")
    return problems


def validate_record_lines(where: str, operation: dict, case: dict, lines: list) -> list[str]:
    """A list operation's JSONL: one service-record/1 line per
    item of `output.records`, then one service-page/1 trailer whose `count`
    matches; the reassembled result must satisfy the operation's result schema."""
    problems = []
    collection = operation["output"]["records"]
    if not lines:
        return [f"{where}: list jsonl must end with a service-page/1 trailer"]
    *records, page = lines
    problems += [f"{where} page {e}" for e in errors_of({"$ref": SCHEMA_BASE + "service-page.schema.json"}, page)]
    for index, line in enumerate(records):
        problems += [f"{where} record {e}" for e in errors_of({"$ref": SCHEMA_BASE + "service-record.schema.json"}, line)]
        if line.get("index") != index or line.get("collection") != collection or line.get("operation") != operation["id"]:
            problems.append(f"{where}: record line {index} has the wrong index, collection or operation")
    if page.get("count") != len(records) or page.get("collection") != collection or page.get("operation") != operation["id"]:
        problems.append(f"{where}: the page trailer's count, collection or operation disagrees with its records")
    if not operation["output"]["paged"] and page.get("nextBefore") is not None:
        problems.append(f"{where}: an unpaged operation's trailer carries nextBefore null")
    if case["exitCode"] != 0:
        problems.append(f"{where}: a record listing must exit 0")
    meta = page.get("meta") if isinstance(page.get("meta"), dict) else {}
    result = {**meta, collection: [line.get("record") for line in records]}
    schema = result_schema(operation)
    if schema:
        problems += [f"{where} result {e}" for e in errors_of(schema, result)]
    return problems


def validate_case(manifest: dict, feature: str, path: Path, case: dict, codes: dict) -> list[str]:
    where = f"{feature}/{path.name}"
    fixture_doc = load_json(FIXTURE_SCHEMA)
    problems = [f"{where} {e}" for e in errors_of({"$ref": fixture_doc["$id"] + "#/$defs/case"}, case)]
    if problems:
        return problems
    if case["feature"] != feature:
        problems.append(f"{where}: feature {case['feature']} does not match its directory")
    if path.stem != case["id"]:
        problems.append(f"{where}: file name must be <id>.json")
    if case.get("languageForwarded") and (case["exitCode"] != 10 or "HOSTED_UNAVAILABLE" not in
                                          json.dumps(case.get("stderr", {}))):
        problems.append(f"{where}: languageForwarded is only for a HOSTED_UNAVAILABLE refusal (exit 10) of a forwarded argv")
    operations = {o["id"]: o for o in manifest["operations"]}
    operation = operations.get(case["operation"])
    if operation is None:
        return problems + [f"{where}: unknown operation {case['operation']}"]
    if feature != "framework" and operation["feature"] != feature:
        problems.append(f"{where}: operation {operation['id']} belongs to feature {operation['feature']}")
    for exchange in case["fixture"].get("exchanges", []):
        if isinstance(exchange.get("body"), str) or isinstance(exchange.get("expectedBody"), str):
            problems.append(f"{where}: service cases use bodyText/expectedBodyText for raw text, not a string body")
        if feature == "framework":
            continue
        # A trailing `{path}` (run files) spans segments.
        templates = [r for r in operation["requests"] if r["method"] == exchange["method"]
                     and api_path_matches(exchange["path"], r["path"])]
        if not templates:
            problems.append(f"{where}: exchange {exchange['method']} {exchange['path']} is not a request of {operation['id']}")
        elif "query" in exchange:
            allowed = set().union(*(set(r.get("query", {})) for r in templates))
            extra = set(exchange["query"]) - allowed
            if extra:
                problems.append(f"{where}: query parameters {sorted(extra)} are not declared by {operation['id']}")
    stdout = case["stdout"]
    structured = [stdout["json"]] if "json" in stdout else stdout.get("jsonl", [])
    problems += [f"{where}: {problem}" for problem in
                 one_shape_problems(case["argv"], case["exitCode"], structured)]
    envelope = {"$ref": SCHEMA_BASE + "service-operation.schema.json"}
    documents = []
    if "json" in stdout:
        documents = [("json", stdout["json"])]
    elif "jsonl" in stdout and not operation["output"]["stream"]:
        lines = stdout["jsonl"]
        if len(lines) == 1 and isinstance(lines[0], dict) and lines[0].get("schema") == "openprose.service-operation/1":
            documents = [("json", lines[0])]
        elif operation["output"].get("records"):
            problems += validate_record_lines(where, operation, case, lines)
        else:
            problems.append(f"{where}: jsonl of a non-stream operation without output.records must be one envelope line")
    elif "jsonl" in stdout:
        documents = [("jsonl", line) for line in stdout["jsonl"]]
        terminal = [line for line in stdout["jsonl"] if line.get("type") in ("service.completed", "service.failed", "service.detached")]
        if len(terminal) != 1 or stdout["jsonl"][-1] is not terminal[0]:
            problems.append(f"{where}: jsonl must end with exactly one terminal line")
    for kind, document in documents:
        account_schema = ACCOUNT_SCHEMAS.get(document.get("schema")) if kind == "json" else None
        if account_schema is not None:
            # Account reports (auth, org list, package) validate against their
            # own schemas, including a problem's taxonomy Action.
            problems += [f"{where} {document['schema']} {e}" for e in
                         errors_of({"$ref": SCHEMA_BASE + account_schema}, document)]
            problem = document.get("problem")
            if problem and codes.get(problem.get("code"), {}).get("exitCode") != case["exitCode"]:
                problems.append(f"{where}: exit {case['exitCode']} differs from the taxonomy exit for {problem.get('code')}")
            continue
        is_envelope = kind == "json" and document.get("schema") == "openprose.service-operation/1"
        if kind == "jsonl":
            problems += [f"{where} jsonl {e}" for e in errors_of({"$ref": SCHEMA_BASE + "service-event.schema.json"}, document)]
            if document.get("type") in ("service.completed", "service.failed", "service.detached"):
                document, is_envelope = document["data"], True
            else:
                continue
        if not is_envelope:
            continue
        problems += [f"{where} envelope {e}" for e in errors_of(envelope, document)]
        # `cli`: an invocation error for an argv that names no operation.
        if document.get("operation") not in (operation["id"], "cli") and feature != "framework":
            problems.append(f"{where}: envelope operation differs from the case operation")
        problem = document.get("problem")
        if problem:
            code = problem.get("code")
            if code not in codes:
                problems.append(f"{where}: unknown error code {code}")
            elif codes[code]["exitCode"] != case["exitCode"]:
                problems.append(f"{where}: exit {case['exitCode']} differs from taxonomy exit {codes[code]['exitCode']} for {code}")
        else:
            if case["exitCode"] != 0:
                problems.append(f"{where}: a successful envelope must exit 0")
            schema = result_schema(operation)
            result = document.get("result")
            if isinstance(result, dict) and set(result) == {"help", "operations"}:
                # A --help in a JSON mode: the help text and command records.
                help_schema = {"$ref": SCHEMA_BASE + "service/framework.schema.json#/$defs/help"}
                problems += [f"{where} help {e}" for e in errors_of(help_schema, result)]
            elif isinstance(result, dict) and result.get("preview") is True:
                # A --preview result is the closed framework preview
                # (plannedRequest.owner is part of it), not the verb's result.
                preview = {"$ref": SCHEMA_BASE + "service/framework.schema.json#/$defs/preview"}
                problems += [f"{where} preview {e}" for e in errors_of(preview, result)]
            elif schema and isinstance(result, dict):
                problems += [f"{where} result {e}" for e in errors_of(schema, result)]
    return problems


def open_schema_objects(node, where: str) -> list[str]:
    """Every object a result schema describes is closed: `additionalProperties`
    is false, or it is a declared map whose keys are constrained by
    `propertyNames` and whose values by an `additionalProperties` schema."""
    problems = []
    if isinstance(node, dict):
        kind = node.get("type")
        is_object = kind == "object" or (isinstance(kind, list) and "object" in kind) or "properties" in node
        if is_object:
            extra = node.get("additionalProperties")
            if extra is not False and not (isinstance(extra, dict) and "propertyNames" in node):
                problems.append(f"{where}: object schema is not closed (additionalProperties: false)")
        for key, value in node.items():
            problems += open_schema_objects(value, f"{where}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems += open_schema_objects(value, f"{where}/{index}")
    return problems


def validate() -> int:
    manifest = load_json(MANIFEST)
    problems = validate_manifest(manifest)
    problems += public_manifest_problems()
    for path in sorted((SHARED / "schemas" / "service").glob("*.schema.json")):
        problems += open_schema_objects(load_json(path), f"schemas/service/{path.name}#")
    codes = taxonomy()
    count = 0
    ids = set()
    for feature, path in iter_cases():
        if feature not in FEATURES:
            problems.append(f"unknown feature directory {feature}")
            continue
        try:
            case = load_json(path)
        except json.JSONDecodeError as error:
            problems.append(f"{feature}/{path.name}: invalid JSON ({error})")
            continue
        if case.get("id") in ids:
            problems.append(f"duplicate case id {case.get('id')}")
        ids.add(case.get("id"))
        problems += validate_case(manifest, feature, path, case, codes)
        count += 1
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"{'PASS' if not problems else 'FAIL'}: manifest ({len(manifest['operations'])} operations) and {count} service corpus cases validated")
    return 1 if problems else 0


def fixture_tokens(fixture: dict) -> list[bytes]:
    values = [fixture.get("credential")] + list((fixture.get("credentials") or {}).values())
    return [v.encode() for v in values if isinstance(v, str) and v]


def fresh_environment(root: Path, fixture_path: Path, extra: dict) -> dict:
    environment = dict(SAFE_ENV)
    environment.update({"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
                        "TMPDIR": str(root), "PROSE_TEST_SERVICE_FIXTURE": str(fixture_path)})
    environment.update(extra)
    return environment


PUBLIC_PROJECTION = SHARED / "service" / "operations-public.v1.json"


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


def public_manifest() -> dict:
    """What `cli service operations` prints: the manifest reduced to its public
    projection (`shared/service/operations-public.v1.json`)."""
    manifest = json.loads(MANIFEST.read_bytes())
    return project_fields(manifest, json.loads(PUBLIC_PROJECTION.read_bytes())["fields"])


def public_manifest_problems() -> list[str]:
    """The printed manifest stays within its size budget and names none of the
    projection's forbidden strings (service routes, the service origin, the key
    format, service error strings, catalog detail)."""
    projection = json.loads(PUBLIC_PROJECTION.read_bytes())
    text = canonical_line(public_manifest())
    problems = []
    if len(text) > projection["maxBytes"]:
        problems.append(f"the published manifest is {len(text)} bytes, over its {projection['maxBytes']}-byte budget")
    for forbidden in projection["forbid"]:
        if forbidden.encode() in text:
            problems.append(f"the published manifest contains {forbidden!r}")
    return problems


def manifest_envelope() -> bytes:
    """The `--output json cli service operations` line: the manifest as the
    envelope's result."""
    return canonical_line({"schema": "openprose.service-operation/1", "operation": "service.operations",
                           "interaction": None, "result": public_manifest(), "problem": None})


def manifest_records() -> bytes:
    """The `--output jsonl cli service operations` lines: one service-record/1
    line per operation, then the service-page/1 trailer whose meta is the rest
    of the manifest."""
    manifest = public_manifest()
    head = {"operation": "service.operations", "collection": "operations"}
    lines = [canonical_line({"schema": "openprose.service-record/1", **head, "index": index, "record": operation})
             for index, operation in enumerate(manifest["operations"])]
    meta = {key: value for key, value in manifest.items() if key != "operations"}
    lines.append(canonical_line({"schema": "openprose.service-page/1", **head, "interaction": None,
                                 "count": len(manifest["operations"]), "nextBefore": None, "meta": meta, "exitCode": 0}))
    return b"".join(lines)


def identity_probe(command: list[str]) -> str | None:
    with tempfile.TemporaryDirectory(prefix="prose-service-identity-") as directory:
        root = Path(directory)
        fixture = root / "service.json"
        fixture.write_text(json.dumps({"storeAvailable": True, "exchanges": []}))
        observed = subprocess.run([*command, "--output", "json", "cli", "service", "operations"], cwd=root,
                                  env=fresh_environment(root, fixture, {}), capture_output=True, timeout=TIMEOUT_SECONDS)
        if observed.returncode != 0:
            return f"`cli service operations` exited {observed.returncode}"
        if observed.stdout != manifest_envelope():
            return "`cli service operations --json` is not one envelope line whose result is the published shared/service/operations.v1.json"
        return None


# Reasons the grammar parser gives (both ports, byte-identical): an example
# that draws one of these is not a valid command line. Value checks that need
# local state (an input file, a run journal entry, a key) are not grammar.
PARSER_REASON = re.compile(r"^(missing or invalid arguments|unknown command|unknown option"
                           r"|unexpected argument|missing argument|missing required option|option |invalid output mode"
                           r"|--json conflicts|`[^`]+` is not a command)")


def guide_lines() -> list[str]:
    """Every full `prose ...` command line of shared/service/guide.v1.md, as
    `ci/render_service_help.py` extracts them, except templates such as
    `prose cli <COMMAND> --help`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    return [line for line in renderer.guide_commands((SHARED / "service" / "guide.v1.md").read_text())[0]
            if "<" not in line and "..." not in line]


def examples_probe(command: list[str]) -> list[str]:
    """Every manifest example and every command line of the guide parses: run it with an empty fixture (nothing is sent) and
    fail when the grammar rejects it."""
    manifest = json.loads(MANIFEST.read_bytes())
    lines = [(operation["id"], example) for operation in manifest["operations"] for example in operation["examples"]]
    lines += [("guide", line) for line in guide_lines()]
    return parse_probe(command, lines)


def parse_probe(command: list[str], lines: list[tuple[str, str]]) -> list[str]:
    """Run each (label, `prose ...` line) with an empty fixture and report
    the lines whose argv the product's grammar parser rejects."""
    problems = []
    for label, example in lines:
        argv = shlex.split(example)[1:]
        if "--json" not in argv and "--output" not in argv:
            argv = ["--output", "json", *argv]
        with tempfile.TemporaryDirectory(prefix="prose-service-example-") as directory:
            root = Path(directory)
            fixture = root / "service.json"
            fixture.write_text(json.dumps({"storeAvailable": True, "exchanges": []}))
            (root / "work").mkdir()
            observed = subprocess.run([*command, *argv], cwd=root / "work", env=fresh_environment(root, fixture, {}),
                                      input=b"", capture_output=True, timeout=TIMEOUT_SECONDS)
        if observed.returncode != 2:
            continue
        try:
            document = json.loads(observed.stdout.decode().splitlines()[-1])
        except (ValueError, IndexError):
            problems.append(f"{label}: example {example!r} exited 2 without a JSON problem")
            continue
        problem = document.get("problem") if isinstance(document.get("problem"), dict) else document
        reason = str((problem.get("details") or {}).get("reason", ""))
        if problem.get("code") == "INVOCATION_INVALID" and PARSER_REASON.match(reason):
            problems.append(f"{label}: example {example!r} does not parse: {reason[:160]}")
    return problems


def executable_paths(command: list[str]) -> list[bytes]:
    """The product path as given and resolved: no human error line may name
    it. A bare command name is not a path."""
    if "/" not in command[0]:
        return []
    return sorted({command[0].encode(), str(Path(command[0]).resolve()).encode()})


def suggested_argv(stdout: bytes, stderr: bytes) -> list[str] | None:
    """`details.suggestedArgv` of the JSON problem on stdout, or the last
    `prose ...` command named by the human Action line on stderr."""
    lines = stdout.decode().splitlines()
    if lines:
        try:
            document = json.loads(lines[-1])
        except ValueError:
            document = None
        if isinstance(document, dict):
            problem = document.get("problem") if isinstance(document.get("problem"), dict) else document
            argv = (problem.get("details") or {}).get("suggestedArgv")
            if isinstance(argv, list):
                return argv
    for line in reversed(stderr.decode().splitlines()):
        if line.startswith("Action:"):
            commands = re.findall(r"`prose ([^`]*)`", line)
            return shlex.split(commands[-1]) if commands else None
    return None


def problem_suggestion(stdout: bytes) -> tuple[list[str], str | None] | None:
    """`(details.suggestedArgv, details.suggestedStdin)` of the last structured
    document on stdout (a terminal event carries the problem in `data`), or
    None when it names no suggestion."""
    lines = stdout.decode(errors="replace").splitlines()
    try:
        document = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    holder = document.get("data") if document.get("schema") == "openprose.service-event/1" else document
    problem = holder.get("problem") if isinstance(holder, dict) and isinstance(holder.get("problem"), dict) else holder
    details = problem.get("details") if isinstance(problem, dict) else None
    argv = details.get("suggestedArgv") if isinstance(details, dict) else None
    if not isinstance(argv, list):
        return None
    stdin = details.get("suggestedStdin")
    return argv, stdin if isinstance(stdin, str) else None


# What a round-tripped suggestion reads on standard input when it names
# `-` for a file option and the problem gives no suggestedStdin: the secret
# the user supplies (a code or token the CLI never echoes).
STAND_IN_SECRET = b"EXAMPLE-STAND-IN-SECRET\n"
# `--round-trip-only`: check nothing but the suggestion round trip (proves the gate
# on a build whose goldens differ).
ROUND_TRIP_ONLY = False


def round_trip_problem(command: list[str], cwd: Path, env: dict, argv: list[str], stdin: str | None) -> str | None:
    """A suggestion is complete. Run `details.suggestedArgv`
    offline in the case's fixture and working directory; it must parse. An
    INVOCATION_INVALID exit 2 (a repeated rejection, a missing required
    option or argument, a bare noun that needs a verb) fails. Service,
    fixture and confirmation outcomes are the suggestion working."""
    retry = argv if ("--json" in argv or "--output" in argv) else ["--output", "json", *argv]
    feed = stdin.encode() if stdin is not None else STAND_IN_SECRET if "-" in argv else b""
    again = subprocess.run([*command, *retry], cwd=cwd, env=env, input=feed, capture_output=True, timeout=TIMEOUT_SECONDS)
    if again.returncode == 2 and b"INVOCATION_INVALID" in again.stdout + again.stderr:
        reason = ""
        try:
            document = json.loads(again.stdout.decode().splitlines()[-1])
            problem = document.get("problem") if isinstance(document.get("problem"), dict) else document
            reason = str((problem.get("details") or {}).get("reason", ""))
        except (ValueError, IndexError, AttributeError):
            reason = (again.stdout + again.stderr)[:200].decode(errors="replace")
        return f"round trip: suggestedArgv {argv!r} is itself INVOCATION_INVALID: {reason[:200]}"
    return None


def run_case(command: list[str], case: dict) -> str | None:
    with tempfile.TemporaryDirectory(prefix="prose-service-oracle-") as directory:
        root = Path(directory).resolve()
        fixture = root / "service.json"
        fixture.write_text(json.dumps(case["fixture"]))
        for entry in case.get("files", []):
            target = root / "work" / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(entry["contentBase64"]) if "contentBase64" in entry
                               else entry.get("content", "").encode())
        (root / "work").mkdir(exist_ok=True)
        observed = subprocess.run([*command, *case["argv"]], cwd=root / "work",
                                  env=fresh_environment(root, fixture, case.get("environment", {})),
                                  input=case.get("stdin", "").encode(), capture_output=True, timeout=TIMEOUT_SECONDS)
        if ROUND_TRIP_ONLY:
            found = problem_suggestion(observed.stdout)
            return None if found is None else round_trip_problem(
                command, root / "work", fresh_environment(root, fixture, case.get("environment", {})), found[0], found[1])
        if observed.returncode != case["exitCode"]:
            return f"exit {observed.returncode}, expected {case['exitCode']}: {observed.stderr[:300]!r}"
        # Service errors never name the executable: a copyable command starts
        # with `prose`, and a local path is not the user's business.
        for path in [] if case.get("languageForwarded") else executable_paths(command):
            if path in observed.stderr:
                return "stderr names the product executable path; human errors must not"
        # Every JSON suggestion round-trips, after the case's
        # own checks (below); a human case opts in with suggestedArgvReaches
        # and is checked through its Action.
        suggestion = problem_suggestion(observed.stdout)
        if suggestion is None and case.get("suggestedArgvReaches"):
            retry = suggested_argv(observed.stdout, observed.stderr)
            if not retry:
                return "suggestedArgvReaches: the case output carries no suggestedArgv"
            again = subprocess.run([*command, *retry], cwd=root / "work",
                                   env=fresh_environment(root, fixture, case.get("environment", {})),
                                   input=b"", capture_output=True, timeout=TIMEOUT_SECONDS)
            if again.returncode == 2 and b"INVOCATION_INVALID" in again.stdout + again.stderr:
                return f"suggestedArgv {retry!r} is itself rejected: {(again.stdout + again.stderr)[:300]!r}"
        output = observed.stdout + observed.stderr
        # The account and command overviews never name local paths (the
        # home, state or journal directory); `cli config explain` is the
        # local report.
        if case.get("operation") in LOCAL_PATH_FREE_OPERATIONS and (
                str(root).encode() in output or b"openprose/cli" in output):
            return "the output names a local path (home, state or journal directory)"
        for secret in fixture_tokens(case["fixture"]) + [s.encode() for s in case.get("forbid", [])]:
            if secret in output:
                return "a forbidden string or fixture credential appeared in the output"
        stdout = case["stdout"]
        if "json" in stdout or "jsonl" in stdout:
            # The one-JSON-shape invariant runs on what the product printed,
            # before the golden comparison.
            try:
                observed_documents = [json.loads(line) for line in observed.stdout.decode().splitlines()]
            except ValueError:
                observed_documents = []
            shape = one_shape_problems(case["argv"], observed.returncode, observed_documents)
            if shape:
                return shape[0]
        if "manifestBytes" in stdout:
            if observed.stdout != manifest_envelope():
                return "stdout is not the envelope line whose result is the manifest"
        elif "manifestOperations" in stdout:
            if observed.stdout != manifest_records():
                return "stdout is not one record line per manifest operation, in manifest order, then the page trailer"
        elif "json" in stdout:
            if not observed.stdout.endswith(b"\n") or observed.stdout.count(b"\n") != 1:
                return "JSON mode must print exactly one line"
            if json.loads(observed.stdout) != stdout["json"]:
                return "stdout JSON differs from the corpus"
            if observed.stdout != canonical_line(stdout["json"]):
                return "stdout JSON is not canonical (compact, keys sorted recursively): " + repr(observed.stdout[:160])
        elif "jsonl" in stdout:
            lines = [json.loads(line) for line in observed.stdout.decode().splitlines()]
            if lines != stdout["jsonl"]:
                return "stdout JSONL differs from the corpus"
            if observed.stdout != b"".join(canonical_line(line) for line in stdout["jsonl"]):
                return "stdout JSONL is not canonical (compact, keys sorted recursively, one line per record)"
        elif "text" in stdout:
            if observed.stdout.decode() != stdout["text"]:
                return "stdout text differs from the corpus"
        elif observed.stdout:
            return "stdout must be empty"
        expected_stderr = case.get("stderr")
        if expected_stderr is None:
            if observed.stderr and ("json" in stdout or "jsonl" in stdout):
                return "structured modes must not write to stderr unless the case declares it"
        else:
            text = observed.stderr.decode()
            if "{{RUNNER}}" in expected_stderr.get("text", ""):
                # A language-forwarded runner error names the exact runner
                # invocation (the shell-quoted product path).
                for path in sorted(executable_paths(command), key=len, reverse=True):
                    text = text.replace("'" + path.decode() + "'", "{{RUNNER}}")
            if "text" in expected_stderr and text != expected_stderr["text"]:
                return "stderr differs from the corpus"
            if expected_stderr.get("empty") and text:
                return "stderr must be empty"
            for fragment in expected_stderr.get("contains", []):
                if fragment not in text:
                    return f"stderr lacks {fragment!r}"
        for entry in case.get("expectFiles", []):
            target = root / "work" / entry["path"]
            if entry.get("absent"):
                if target.exists():
                    return f"{entry['path']} must not exist"
                continue
            if not target.is_file():
                return f"{entry['path']} was not written"
            data = target.read_bytes()
            if "sha256" in entry and hashlib.sha256(data).hexdigest() != entry["sha256"]:
                return f"{entry['path']} digest differs"
            if "content" in entry and data != entry["content"].encode():
                return f"{entry['path']} content differs"
        expected_journal = case.get("journalAfter", [])
        if expected_journal:
            entries = journal_entries(root, case["fixture"])
            for want in expected_journal:
                if not any(all(entry.get(key) == value for key, value in want.items()) for entry in entries):
                    return f"the run journal lacks an entry matching {want}"
        if suggestion is not None:
            return round_trip_problem(command, root / "work", fresh_environment(root, fixture, case.get("environment", {})),
                                      suggestion[0], suggestion[1])
    return None


LEAK_KEY = "__leak"
# Operations whose output never names a local path.
LOCAL_PATH_FREE_OPERATIONS = ("service.capabilities", "service.triage", "service.operations", "service.status")


# Response members whose keys are the user's own names (a job's configured
# input names), echoed by design: the probe leaves those maps alone.
USER_KEYED_MAPS = ("inputs",)


def inject_leak(value, key: str | None = None):
    """`value` with a `__leak` member (whose value is also `__leak`) added to
    every JSON object, recursively, except the user-keyed maps."""
    if isinstance(value, dict):
        injected = {name: inject_leak(item, name) for name, item in value.items()}
        if key not in USER_KEYED_MAPS:
            injected[LEAK_KEY] = LEAK_KEY
        return injected
    if isinstance(value, list):
        return [inject_leak(item, key) for item in value]
    return value


def leaky_fixture(fixture: dict) -> dict | None:
    """The fixture with `__leak` injected into every object of every JSON
    response body and server-sent event payload, or None when it has none."""
    changed = False
    exchanges = []
    for exchange in fixture.get("exchanges", []):
        exchange = dict(exchange)
        if isinstance(exchange.get("body"), (dict, list)):
            exchange["body"] = inject_leak(exchange["body"])
            changed = True
        sse = exchange.get("sse")
        if isinstance(sse, dict) and isinstance(sse.get("frames"), list):
            frames = []
            for frame in sse["frames"]:
                if isinstance(frame, dict) and isinstance(frame.get("data"), (dict, list)):
                    frame = {**frame, "data": inject_leak(frame["data"])}
                    changed = True
                frames.append(frame)
            exchange["sse"] = {**sse, "frames": frames}
        exchanges.append(exchange)
    return {**fixture, "exchanges": exchanges} if changed else None


FROZEN_OPERATIONS: set[str] | None = None


def load_frozen_operations() -> None:
    global FROZEN_OPERATIONS
    FROZEN_OPERATIONS = {operation["id"] for operation in json.loads(MANIFEST.read_bytes())["operations"]
                         if operation["contract"] != "service/1"}


def leak_probe(command: list[str], case: dict) -> str | None:
    """Reruns the case with an unknown `__leak` field on every object the
    service returns: the field must never reach stdout or stderr, whatever the
    outcome. Output is built from each record's public field list, never by
    copying service objects through."""
    fixture = leaky_fixture(case["fixture"])
    if fixture is None:
        return None
    with tempfile.TemporaryDirectory(prefix="prose-service-leak-") as directory:
        root = Path(directory).resolve()
        path = root / "service.json"
        path.write_text(json.dumps(fixture))
        for entry in case.get("files", []):
            target = root / "work" / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(entry["contentBase64"]) if "contentBase64" in entry
                               else entry.get("content", "").encode())
        (root / "work").mkdir(exist_ok=True)
        observed = subprocess.run([*command, *case["argv"]], cwd=root / "work",
                                  env=fresh_environment(root, path, case.get("environment", {})),
                                  input=case.get("stdin", "").encode(), capture_output=True, timeout=TIMEOUT_SECONDS)
        for name, stream in (("stdout", observed.stdout), ("stderr", observed.stderr)):
            if LEAK_KEY.encode() in stream:
                at = stream.index(LEAK_KEY.encode())
                return f"an unknown service field reached {name}: {stream[max(0, at - 120):at + 40]!r}"
        # An unknown field changes nothing: the same exit and the same stdout.
        # The frozen account/1 contract (registry and auth) validates its
        # documents strictly instead; it only has to keep the field out.
        if FROZEN_OPERATIONS is None:
            load_frozen_operations()
        if case.get("operation") in FROZEN_OPERATIONS:
            return None
        if observed.returncode != case["exitCode"]:
            return f"an unknown service field changed the exit to {observed.returncode}: {observed.stdout[:300]!r}"
        expected = case["stdout"]
        if "json" in expected and json.loads(observed.stdout or b"null") != expected["json"]:
            return f"an unknown service field changed stdout: {observed.stdout[:300]!r}"
        if "jsonl" in expected and [json.loads(line) for line in observed.stdout.decode().splitlines()] != expected["jsonl"]:
            return "an unknown service field changed stdout"
        if "text" in expected and observed.stdout.decode() != expected["text"]:
            return f"an unknown service field changed stdout: {observed.stdout[:300]!r}"
    return None


def observe_case(command: list[str], case: dict) -> subprocess.CompletedProcess:
    """Runs a case's argv once in a fresh root, as run_case does, and returns what the product printed."""
    with tempfile.TemporaryDirectory(prefix="prose-service-determinism-") as directory:
        root = Path(directory).resolve()
        fixture = root / "service.json"
        fixture.write_text(json.dumps(case["fixture"]))
        for entry in case.get("files", []):
            target = root / "work" / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(entry["contentBase64"]) if "contentBase64" in entry
                               else entry.get("content", "").encode())
        (root / "work").mkdir(exist_ok=True)
        return subprocess.run([*command, *case["argv"]], cwd=root / "work",
                              env=fresh_environment(root, fixture, case.get("environment", {})),
                              input=case.get("stdin", "").encode(), capture_output=True, timeout=TIMEOUT_SECONDS)


def terminal_exit_problem(stdout: bytes, exit_code: int) -> str | None:
    """`--output jsonl` ends with one terminal record that carries the exit
    code: a stream operation's `service.*` event, or a listing's page trailer
    (exit 0). Other JSONL output is the one envelope."""
    lines = stdout.decode().splitlines()
    if not lines:
        return "jsonl output is empty"
    last = json.loads(lines[-1])
    kind = last.get("schema")
    if kind == "openprose.service-event/1" or kind == "openprose.service-page/1":
        if last.get("exitCode") != exit_code:
            return f"the terminal jsonl record carries exitCode {last.get('exitCode')!r}, but the process exited {exit_code}"
    return None


def determinism_probe(command: list[str], features: list[str] | None) -> list[str]:
    """A corpus meta-test over every case of a paged operation (manifest
    `output.paged`): run it twice on its fixture and compare the bytes; every
    JSON line is canonical (one line, keys sorted); and every JSONL case ends
    with a terminal record carrying the process exit code."""
    manifest = json.loads(MANIFEST.read_bytes())
    paged = {operation["id"] for operation in manifest["operations"] if operation["output"].get("paged")}
    problems = []
    for feature, path in iter_cases(features):
        case = load_json(path)
        structured = "json" in case["stdout"] or "jsonl" in case["stdout"]
        if "jsonl" in case["stdout"]:
            first = observe_case(command, case)
            problem = terminal_exit_problem(first.stdout, first.returncode)
            if problem is not None:
                problems.append(f"{feature}/{case['id']}: {problem}")
        if case["operation"] not in paged or not structured:
            continue
        first, second = observe_case(command, case), observe_case(command, case)
        if first.stdout != second.stdout or first.returncode != second.returncode:
            problems.append(f"{feature}/{case['id']}: two runs on the same fixture printed different bytes")
            continue
        for line in first.stdout.splitlines(keepends=True):
            if canonical_line(json.loads(line)) != line:
                problems.append(f"{feature}/{case['id']}: a JSON line is not canonical (compact, keys sorted)")
                break
    return problems


def journal_entries(root: Path, fixture: dict) -> list[dict]:
    """Run journal entries after an invocation (`$XDG_STATE_HOME/openprose/cli/production/runs/`)."""
    directory = root / "state" / "openprose" / "cli" / "production" / "runs"
    entries = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            entries.append(json.loads(path.read_text("utf-8")))
        except ValueError:
            continue
    return entries


class RecordingProxy:
    """A local HTTP proxy that records each request line and closes the
    connection without answering, so a proxied request fails exactly like a
    dead network in both products and never reaches a service."""

    def __init__(self) -> None:
        import socket
        import threading
        self.lines: list[str] = []
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                try:
                    data = connection.recv(4096)
                except OSError:
                    continue
                self.lines.append(data.split(b"\r\n", 1)[0].decode("latin-1"))

    def close(self) -> None:
        self._socket.close()


# `cli service status --json` through a proxy that drops the connection.
PROXY_UNAVAILABLE = {
    "interaction": "health.read", "operation": "service.status", "result": None,
    "schema": "openprose.service-operation/1",
    "problem": {"action": "Check your network connection and retry later.", "boundary": "hosted-service",
                "code": "SERVICE_UNAVAILABLE",
                "details": {"reason": "the connection to the service failed before a complete response arrived"},
                "exitCode": 10, "message": "The OpenProse service is unavailable.", "retryable": True,
                "schema": "openprose.runner-error/1"},
}


def proxy_probe(command: list[str]) -> list[str]:
    """Real network code paths through a local proxy (no fixture): both
    products honor HTTPS_PROXY/https_proxy and NO_PROXY with the rules of
    shared/fixtures/transport/proxy-selection.json, ignore ALL_PROXY, and fail
    closed (SERVICE_UNAVAILABLE, never a direct request) when the proxy is
    dead or unusable."""
    failures: list[str] = []
    proxy = RecordingProxy()
    dead = "http://127.0.0.1:9"
    scenarios = [
        ("uppercase", {"HTTPS_PROXY": proxy.url}, True),
        ("lowercase-wins", {"https_proxy": proxy.url, "HTTPS_PROXY": dead}, True),
        ("no-proxy-other-hosts", {"HTTPS_PROXY": proxy.url, "NO_PROXY": "other.example, .invalid,"}, True),
        ("dead-proxy", {"HTTPS_PROXY": dead, "ALL_PROXY": proxy.url}, False),
        ("socks-never-direct", {"HTTPS_PROXY": "socks5://127.0.0.1:9", "ALL_PROXY": proxy.url}, False),
    ]
    try:
        for name, extra, through in scenarios:
            with tempfile.TemporaryDirectory(prefix="prose-proxy-probe-") as directory:
                root = Path(directory).resolve()
                environment = {"PATH": SAFE_ENV["PATH"], "HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                               "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
                               "TMPDIR": str(root), **extra}
                before = len(proxy.lines)
                observed = subprocess.run([*command, "cli", "service", "status", "--json"], cwd=root, env=environment,
                                          capture_output=True, timeout=TIMEOUT_SECONDS)
                seen = proxy.lines[before:]
                if observed.returncode != 10 or observed.stdout != canonical_line(PROXY_UNAVAILABLE):
                    failures.append(f"{name}: expected the SERVICE_UNAVAILABLE envelope, got exit "
                                    f"{observed.returncode}: {observed.stdout[:200]!r}")
                if through and not any(line.startswith("CONNECT ") and line.endswith(":443 HTTP/1.1") for line in seen):
                    failures.append(f"{name}: the request did not go through the proxy ({seen!r})")
                if not through and seen:
                    failures.append(f"{name}: ALL_PROXY must never be used ({seen!r})")
    finally:
        proxy.close()
    return failures


def run(command: list[str], features: list[str] | None, probe: bool, leaks: bool = True) -> int:
    failures = 0
    if probe:
        problem = identity_probe(command)
        print(f"{'PASS' if problem is None else 'FAIL'} manifest-identity{'' if problem is None else ': ' + problem}")
        failures += problem is not None
        example_failures = examples_probe(command)
        for failure in example_failures:
            print(f"FAIL manifest-examples: {failure}")
        if not example_failures:
            print("PASS manifest-examples")
        failures += len(example_failures)
        proxy_failures = proxy_probe(command)
        for failure in proxy_failures:
            print(f"FAIL proxy: {failure}")
        if not proxy_failures:
            print("PASS proxy")
        failures += len(proxy_failures)
    count = 0
    for feature, path in iter_cases(features):
        case = load_json(path)
        try:
            problem = run_case(command, case)
        except (subprocess.TimeoutExpired, ValueError, UnicodeDecodeError) as error:
            problem = f"{type(error).__name__}: {error}"
        if problem is None and leaks and not ROUND_TRIP_ONLY:
            try:
                leaked = leak_probe(command, case)
            except (subprocess.TimeoutExpired, ValueError, UnicodeDecodeError) as error:
                leaked = f"{type(error).__name__}: {error}"
            if leaked is not None:
                problem = f"leak probe: {leaked}"
        count += 1
        print(f"{'PASS' if problem is None else 'FAIL'} {feature}/{case['id']}{'' if problem is None else ': ' + problem}")
        failures += problem is not None
    if probe:
        determinism = determinism_probe(command, features)
        for failure in determinism:
            print(f"FAIL determinism: {failure}")
        if not determinism:
            print("PASS determinism")
        failures += len(determinism)
    if features and count == 0:
        print(f"FAIL: no corpus cases for feature(s) {', '.join(features)}")
        failures += 1
    print(f"{'PASS' if not failures else 'FAIL'}: {count} service cases, {failures} failures")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, command = argv[:index], argv[index + 1:]
    # Cases run in a fresh working directory, so a relative product path
    # (`cli/rust/target/debug/prose`) is resolved against the caller's cwd.
    if command and "/" in command[0] and not Path(command[0]).is_absolute():
        command[0] = str(Path(command[0]).resolve())
    parser = argparse.ArgumentParser(description="Service operation corpus")
    parser.add_argument("--validate", action="store_true", help="validate the manifest and corpus without a product")
    parser.add_argument("--list-features", action="store_true")
    parser.add_argument("--feature", action="append", choices=FEATURES, help="run one feature's cases (repeatable)")
    parser.add_argument("--all", action="store_true", help="run every case plus the manifest identity probe")
    parser.add_argument("--cases", type=Path, help="alternate corpus root (tests only)")
    parser.add_argument("--round-trip-only", action="store_true",
                        help="check only that every details.suggestedArgv round-trips")
    args = parser.parse_args(argv)
    if args.round_trip_only:
        global ROUND_TRIP_ONLY
        ROUND_TRIP_ONLY = True
    if args.cases is not None:
        global CASES
        CASES = args.cases.resolve()
    if args.validate:
        return validate()
    if args.list_features:
        for feature in FEATURES:
            print(f"{feature} {sum(1 for _ in iter_cases([feature]))}")
        return 0
    if not command or not (args.feature or args.all):
        parser.error("give --feature F or --all, then -- followed by a test-seam product command")
    return run(command, None if args.all else args.feature, probe=args.all)


if __name__ == "__main__":
    raise SystemExit(main())
