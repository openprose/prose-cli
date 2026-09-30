#!/usr/bin/env python3
"""Coverage gate: the operation manifest (A2) against the vendored service
interaction export (A1).

    python3 cli/conformance/runner/service_coverage.py [--strict-mapping] [--json]

Checks (all modes):
  * the manifest's interactionsSource digest equals the vendored export;
  * every request names an interaction listed by its operation, and its
    catalogRoute is a route of that interaction in A1 (method, path, auth);
  * the request path is the catalog template (parameter names may differ; a
    trailing `{path}` catalog parameter also matches zero or more segments);
  * a catalog route that requires a key is sent with the bearer credential;
  * confirm >= derived rule (agent confirm/never, DELETE, money/outward/
    destructive, not reversible; over non-GET/HEAD requests), unless an account
    operation records a confirmWaiver;
  * effect >= the catalog effect of every non-GET/HEAD request's interaction
    (a safe GET of a multi-route interaction such as organizations.manage is
    a read);
  * the vendored export holds only what the client uses (see
    cli/ci/sync_service_interactions.py).

`--strict-mapping` additionally fails when a vendored interaction is not
mapped by an operation, or when a vendored route is never sent.

`--strict` implies `--strict-mapping` and also fails unless every service
operation has at least one success case (exit 0) and at least one failure case
(non-zero exit) in the shared corpus `cli/conformance/cases/service/`. Both
products run that whole corpus (the service-operations-rust and
service-operations-bun gates), so this is the per-product floor. The account
operations are frozen; their cases live in `cases/service/account/` and the
registry-service gate; they are reported, not counted. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

CLI = Path(__file__).resolve().parents[2]
SERVICE = CLI / "shared" / "service"
KEY_AUTH = {"api_key", "api_key_or_session_token", "api_key_with_github"}
UNREACHABLE_AUTH = {"oauth_state", "signed_url"}
SAFE_METHODS = {"GET", "HEAD"}
CASES = CLI / "conformance" / "cases" / "service"


def command_path(operation: dict) -> list[str]:
    """An operation's command path: its manifest `command` argv without the
    leading `cli`."""
    command = list(operation["command"])
    return command[1:] if command[:1] == ["cli"] else command


def effect_at_least(actual: str, catalog: str) -> bool:
    if actual == catalog or catalog == "read":
        return True
    if catalog == "write":
        return actual in {"money", "outward", "destructive"}
    return False


def derived_confirm(interaction: dict, method: str) -> bool:
    if method in SAFE_METHODS:
        return False
    return (interaction["agent"] in {"confirm", "never"} or method == "DELETE"
            or interaction["effect"] in {"money", "outward", "destructive"} or not interaction["reversible"])


def segments(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def template_matches(request_path: str, catalog_path: str) -> bool:
    request, catalog = segments(request_path), segments(catalog_path)
    if catalog and catalog[-1] == "{path}":
        head = catalog[:-1]
        if len(request) < len(head):
            return False
        request = request[:len(head)]
        catalog = head
    if len(request) != len(catalog):
        return False
    for actual, expected in zip(request, catalog):
        if is_param(expected):
            if not actual or (not is_param(actual) and "/" in actual):
                return False
        elif actual != expected:
            return False
    return True


def load(manifest_path: Path, a1_path: Path):
    manifest = json.loads(manifest_path.read_text())
    a1_bytes = a1_path.read_bytes()
    a1 = json.loads(a1_bytes)
    return manifest, a1, hashlib.sha256(a1_bytes).hexdigest()


def audit(manifest: dict, a1: dict, a1_sha256: str, strict: bool) -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    catalog = {item["id"]: item for item in a1["interactions"]}
    if manifest["interactionsSource"]["sha256"] != a1_sha256:
        errors.append("interactionsSource.sha256 differs from the vendored export; review new interactions, then update it")

    mapped: set[str] = set()
    used_routes: set[tuple] = set()
    ids = set()
    commands = set()
    for operation in manifest["operations"]:
        oid = operation["id"]
        if oid in ids:
            errors.append(f"duplicate operation id {oid}")
        ids.add(oid)
        command = tuple(command_path(operation))
        if command in commands:
            errors.append(f"duplicate command {' '.join(command)}")
        commands.add(command)
        listed = set(operation["interactions"])
        for interaction_id in listed:
            if interaction_id not in catalog:
                errors.append(f"{oid}: unknown interaction {interaction_id}")
            else:
                if catalog[interaction_id]["routes"] and not any(
                        r["interaction"] == interaction_id for r in operation["requests"]):
                    errors.append(f"{oid}: lists {interaction_id} but sends none of its routes")
                mapped.add(interaction_id)
        if operation["interaction"] is not None and operation["interaction"] not in listed:
            errors.append(f"{oid}: primary interaction {operation['interaction']} is not listed")
        confirm_needed = []
        for request in operation["requests"]:
            interaction_id = request["interaction"]
            item = catalog.get(interaction_id)
            where = f"{oid} {request['method']} {request['path']}"
            if interaction_id not in listed:
                errors.append(f"{where}: interaction {interaction_id} not listed by the operation")
            if item is None:
                errors.append(f"{where}: unknown interaction {interaction_id}")
                continue
            route = request["catalogRoute"]
            if route not in item["routes"]:
                errors.append(f"{where}: catalogRoute {route} is not a route of {interaction_id}")
            if route["method"] != request["method"]:
                errors.append(f"{where}: method differs from catalogRoute")
            if not template_matches(request["path"], route["path"]):
                errors.append(f"{where}: path does not match catalog template {route['path']}")
            if route["auth"] in KEY_AUTH and request["auth"] != "bearer":
                errors.append(f"{where}: catalog requires a key ({route['auth']}) but auth is {request['auth']}")
            if route["auth"] in UNREACHABLE_AUTH:
                errors.append(f"{where}: catalog auth {route['auth']} is not reachable by the CLI")
            if route["auth"] == "device_code" and request["auth"] != "none":
                errors.append(f"{where}: device flow requests must not send a key")
            used_routes.add((interaction_id, route["method"], route["path"], route["auth"]))
            if request["method"] not in SAFE_METHODS and not effect_at_least(operation["effect"], item["effect"]):
                errors.append(f"{oid}: effect {operation['effect']} is weaker than {interaction_id} ({item['effect']})")
            if derived_confirm(item, request["method"]):
                confirm_needed.append(f"{request['method']} {request['path']} ({interaction_id})")
        if confirm_needed and not operation["confirm"]:
            waiver = operation.get("confirmWaiver")
            if waiver and operation["contract"] == "account/1":
                warnings.append(f"{oid}: confirmation waived: {waiver}")
            else:
                errors.append(f"{oid}: confirm is false but the catalog requires it for {', '.join(confirm_needed)}")
        if operation.get("confirmWaiver") and operation["contract"] != "account/1":
            errors.append(f"{oid}: confirmWaiver is admitted only for the frozen account operations")
        if operation["confirm"] and not operation["mutation"]:
            errors.append(f"{oid}: a confirm-class operation must be a mutation")

    unmapped = sorted(set(catalog) - mapped)
    unused_routes = []
    for interaction_id in sorted(mapped):
        for route in catalog[interaction_id]["routes"]:
            key = (interaction_id, route["method"], route["path"], route["auth"])
            if key not in used_routes:
                unused_routes.append(key)
    for interaction_id in unmapped:
        (errors if strict else warnings).append(f"{interaction_id}: vendored but not mapped by any operation")
    for key in unused_routes:
        (errors if strict else warnings).append(f"route {key[1]} {key[2]} ({key[3]}) of {key[0]} is vendored but never sent")
    summary = {
        "interactions": len(catalog), "mapped": len(mapped),
        "unmapped": unmapped, "operations": len(manifest["operations"]),
        "routesUsed": len(used_routes), "routesUnaccounted": len(unused_routes),
    }
    return errors, warnings, summary


def case_coverage(manifest: dict, cases_dir: Path) -> tuple[list[str], dict]:
    """Every service operation needs one success and one failure case."""
    errors: list[str] = []
    operations = {operation["id"]: operation for operation in manifest["operations"]}
    counts = {oid: {"success": 0, "failure": 0} for oid in operations}
    for path in sorted(cases_dir.glob("*/*.json")):
        case = json.loads(path.read_text())
        oid = case.get("operation")
        if oid not in counts:
            errors.append(f"{path.relative_to(cases_dir)}: names unknown operation {oid!r}")
            continue
        counts[oid]["success" if case.get("exitCode") == 0 else "failure"] += 1
    frozen = []
    for oid, operation in operations.items():
        if operation["contract"] != "service/1":
            frozen.append(oid)
            continue
        for kind in ("success", "failure"):
            if counts[oid][kind] == 0:
                errors.append(f"{oid}: no {kind} case in cli/conformance/cases/service/")
    return errors, {"cases": counts, "frozenAccount": sorted(frozen)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manifest coverage against the vendored interaction export")
    parser.add_argument("--strict-mapping", action="store_true",
                        help="fail on unmapped interactions and unaccounted routes")
    parser.add_argument("--strict", action="store_true",
                        help="--strict-mapping plus one success and one failure case per service operation")
    parser.add_argument("--cases", type=Path, default=CASES, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--manifest", type=Path, default=SERVICE / "operations.v1.json")
    parser.add_argument("--interactions", type=Path, default=SERVICE / "service-interactions.v1.json")
    args = parser.parse_args(argv)
    manifest, a1, digest = load(args.manifest, args.interactions)
    errors, warnings, summary = audit(manifest, a1, digest, args.strict_mapping or args.strict)
    if args.strict:
        case_errors, case_summary = case_coverage(manifest, args.cases)
        errors.extend(case_errors)
        summary["caseCoverage"] = case_summary
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}, indent=2))
    else:
        for warning in warnings:
            print(f"WARN {warning}")
        for error in errors:
            print(f"FAIL {error}")
        print(f"{'PASS' if not errors else 'FAIL'}: {summary['mapped']} mapped of "
              f"{summary['interactions']} vendored interactions; {summary['operations']} operations; "
              f"{summary['routesUsed']} routes used, {summary['routesUnaccounted']} unaccounted"
              + (f"; case coverage: {len(summary['caseCoverage']['cases']) - len(summary['caseCoverage']['frozenAccount'])}"
                 f" service operations checked, {len(summary['caseCoverage']['frozenAccount'])} account operations"
                 " covered by frozen corpora" if "caseCoverage" in summary else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
