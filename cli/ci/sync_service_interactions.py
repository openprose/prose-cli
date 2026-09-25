#!/usr/bin/env python3
"""Vendor or verify the service's interaction export (artifact A1).

    python3 cli/ci/sync_service_interactions.py --from EXPORT_FILE
    python3 cli/ci/sync_service_interactions.py --check [--from EXPORT_FILE]

EXPORT_FILE is the OpenProse service's interaction export: a JSON object
with `schema` (an `openprose.*-interactions/1` id),
`principals` and `interactions`, where each interaction has an `id`, a
`principal`, an `effect`, `reversible`, an `agent` policy and its `routes`
(`method`, `path`, `auth`). The export is checked against that public
format, then projected before it is written to
`cli/shared/service/service-interactions.v1.json`. The projection keeps
only what this client uses: the interactions the operation manifest maps,
and of those only the routes the manifest sends, each with exactly the
public fields above, under the schema `openprose.service-interactions/1`.
Any other field an export carries is ignored and never enters this
repository. Only maintainers of the OpenProse service can obtain the export,
so re-vendoring is a maintainer task; anyone can run `--check`.

`--from` prints the exported interactions the manifest does not map, so a
maintainer can decide whether the client should offer them; that list is
not written anywhere. It fails when a mapped interaction or a used route is
missing from the export.

Provenance (the SHA-256 of the export file and of the projection) is
recorded in `service-interactions.source.json`. `--check` verifies the
vendored bytes against the recorded digest, and with `--from` against the
projection of the given export. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import sys

CLI = Path(__file__).resolve().parents[1]
SERVICE = CLI / "shared" / "service"
VENDORED = SERVICE / "service-interactions.v1.json"
SOURCE = SERVICE / "service-interactions.source.json"
MANIFEST = SERVICE / "operations.v1.json"

SCHEMA = "openprose.service-interactions/1"
# An export names its own interactions schema; the projection always uses SCHEMA.
EXPORT_SCHEMA = re.compile(r"^openprose\.[a-z0-9-]+-interactions/1$")
# The public export format: the fields this client reads. An export must
# carry them; the vendored projection carries exactly them.
TOP_KEYS = {"schema", "principals", "interactions"}
INTERACTION_KEYS = {"id", "principal", "effect", "reversible", "agent", "routes"}
ROUTE_KEYS = {"method", "path", "auth"}
PRINCIPALS = {"anonymous", "customer", "customer_github"}
EFFECTS = {"read", "write", "money", "outward", "destructive"}
AGENT = {"auto", "confirm", "never"}
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}


class SyncError(Exception):
    pass


def parse(data: bytes) -> dict:
    if b"\r" in data or not data.endswith(b"\n"):
        raise SyncError("export must use LF line endings and end with a newline")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncError(f"export is not UTF-8 JSON: {error}") from None
    if not isinstance(doc, dict):
        raise SyncError("export must be a JSON object")
    return doc


def _keys(value: object, required: set, exact: bool) -> bool:
    if not isinstance(value, dict):
        return False
    return set(value) == required if exact else required <= set(value)


def validate_interactions(doc: dict, exact: bool) -> dict:
    """The public format. ``exact`` (the vendored projection) admits no other
    field; an export may carry others, which the projection drops."""
    if not _keys(doc, TOP_KEYS, exact):
        raise SyncError(f"top-level keys must {'be exactly' if exact else 'include'} {sorted(TOP_KEYS)}")
    if (doc["schema"] != SCHEMA if exact
            else not (isinstance(doc["schema"], str) and EXPORT_SCHEMA.match(doc["schema"]))):
        raise SyncError(f"unexpected export schema {doc['schema']!r}")
    if not isinstance(doc["principals"], list) or set(doc["principals"]) != PRINCIPALS:
        raise SyncError("export principals differ from the public principals")
    if not isinstance(doc["interactions"], list):
        raise SyncError("export interactions must be a list")
    ids = []
    for item in doc["interactions"]:
        if not _keys(item, INTERACTION_KEYS, exact):
            raise SyncError(f"interaction keys must {'be exactly' if exact else 'include'} "
                            f"{sorted(INTERACTION_KEYS)}: {item!r:.120}")
        if item["principal"] not in PRINCIPALS:
            raise SyncError(f"{item['id']}: non-public principal {item['principal']!r}")
        if item["effect"] not in EFFECTS or item["agent"] not in AGENT or not isinstance(item["reversible"], bool):
            raise SyncError(f"{item['id']}: invalid effect, agent or reversible value")
        if not isinstance(item["routes"], list):
            raise SyncError(f"{item['id']}: routes must be a list")
        for route in item["routes"]:
            if (not _keys(route, ROUTE_KEYS, exact) or route["method"] not in METHODS
                    or not isinstance(route["path"], str) or not route["path"].startswith("/")
                    or not isinstance(route["auth"], str)):
                raise SyncError(f"{item['id']}: malformed route {route!r:.120}")
        ids.append(item["id"])
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise SyncError("interactions must be unique and sorted by id")
    return doc


def validate_export(data: bytes) -> dict:
    """Checks an export against the public format. Raises SyncError on any violation."""
    return validate_interactions(parse(data), exact=False)


def manifest_usage(manifest: dict) -> tuple[set[str], set[tuple[str, str, str, str]]]:
    """The interactions the manifest maps and the catalog routes it sends."""
    mapped: set[str] = set()
    used: set[tuple[str, str, str, str]] = set()
    for operation in manifest["operations"]:
        mapped.update(operation["interactions"])
        for request in operation["requests"]:
            route = request["catalogRoute"]
            used.add((request["interaction"], route["method"], route["path"], route["auth"]))
    return mapped, used


def project(doc: dict, manifest: dict) -> bytes:
    """The vendored projection: only mapped interactions and used routes, public fields only."""
    mapped, used = manifest_usage(manifest)
    catalog = {item["id"]: item for item in doc["interactions"]}
    missing = sorted(mapped - set(catalog))
    if missing:
        raise SyncError(f"the manifest maps interactions the export no longer has: {', '.join(missing)}")
    interactions = []
    for item in doc["interactions"]:
        if item["id"] not in mapped:
            continue
        routes = [{key: route[key] for key in ("method", "path", "auth")} for route in item["routes"]
                  if (item["id"], route["method"], route["path"], route["auth"]) in used]
        projected = {key: item[key] for key in ("id", "principal", "effect", "reversible", "agent")}
        projected["routes"] = routes
        interactions.append(projected)
    exported = {(item["id"], r["method"], r["path"], r["auth"]) for item in interactions for r in item["routes"]}
    gone = sorted(used - exported)
    if gone:
        raise SyncError("the manifest sends routes the export no longer has: "
                        + ", ".join(f"{i} {m} {p} ({a})" for i, m, p, a in gone))
    return (json.dumps({"schema": SCHEMA, "principals": doc["principals"], "interactions": interactions},
                       indent=2) + "\n").encode()


def unmapped(doc: dict, manifest: dict) -> list[str]:
    mapped, _ = manifest_usage(manifest)
    return sorted(item["id"] for item in doc["interactions"] if item["id"] not in mapped)


def validate_projection(data: bytes) -> dict:
    return validate_interactions(parse(data), exact=True)


def provenance(data: bytes, projected: bytes) -> dict:
    return {
        "schema": "openprose.vendored-source/1",
        "upstreamSha256": hashlib.sha256(data).hexdigest(),
        "sha256": hashlib.sha256(projected).hexdigest(),
    }


def read_source(export: Path) -> bytes:
    if not export.is_file():
        raise SyncError(f"{export} does not exist; pass the service's interaction export file")
    return export.read_bytes()


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def sync(export: Path) -> int:
    data = read_source(export)
    doc = validate_export(data)
    manifest = load_manifest()
    projected = project(doc, manifest)
    validate_projection(projected)
    record = provenance(data, projected)
    VENDORED.write_bytes(projected)
    SOURCE.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Vendored {len(json.loads(projected)['interactions'])} mapped interactions; sha256 {record['sha256']}")
    for interaction_id in unmapped(doc, manifest):
        print(f"not mapped by the client: {interaction_id}")
    return 0


def check(export: Path | None) -> int:
    failures = []
    if not VENDORED.is_file() or not SOURCE.is_file():
        print("FAIL: vendored export or its source record is missing", file=sys.stderr)
        return 1
    data = VENDORED.read_bytes()
    record = json.loads(SOURCE.read_text())
    if set(record) != {"schema", "upstreamSha256", "sha256"}:
        failures.append("source record keys must be exactly schema, upstreamSha256 and sha256")
    try:
        doc = validate_projection(data)
    except SyncError as error:
        failures.append(f"vendored export violates the public projection: {error}")
        doc = {"interactions": []}
    digest = hashlib.sha256(data).hexdigest()
    if record.get("sha256") != digest:
        failures.append("vendored bytes do not match the recorded SHA-256")
    manifest = load_manifest()
    if manifest.get("interactionsSource", {}).get("sha256") != digest:
        failures.append("operations.v1.json interactionsSource.sha256 differs from the vendored export")
    if export is not None:
        try:
            source = read_source(export)
            projected = project(validate_export(source), manifest)
            if projected != data:
                failures.append(f"vendored export differs from the projection of {export}; re-run with --from")
            if hashlib.sha256(source).hexdigest() != record.get("upstreamSha256"):
                failures.append("the export changed since it was vendored; re-run with --from")
        except SyncError as error:
            failures.append(str(error))
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if not failures:
        print(f"PASS: {len(doc['interactions'])} vendored interactions match sha256 {digest}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="export", type=Path, help="the service's interaction export file")
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    try:
        if args.check:
            return check(args.export.resolve() if args.export else None)
        if args.export is None:
            parser.error("--from is required unless --check is given")
        return sync(args.export.resolve())
    except SyncError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
