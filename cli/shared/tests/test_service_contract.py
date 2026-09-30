#!/usr/bin/env python3
"""Service contract checks, independent of either product.

Covers the operation manifest (A2), the vendored interaction export (A1), the
response acceptance schemas (A3), the service envelope and event schemas, the
per-feature result schemas (including the no-/cost/i lint), taxonomy parity
and the coverage gate's ability to catch drift (mutation tests).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import unittest

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError as error:  # pragma: no cover - actionable bootstrap failure
    raise SystemExit("Install the pinned test dependency: python3 -m pip install -r cli/shared/requirements-test.txt") from error

SHARED = Path(__file__).resolve().parents[1]
CLI = SHARED.parent
SCHEMAS = SHARED / "schemas"
SERVICE = SHARED / "service"
RUNNER = CLI / "conformance" / "runner"
BASE = "https://schemas.openprose.org/cli/v1/"
COST = re.compile("cost", re.IGNORECASE)
TOKEN = "rr_test_" + "1" * 32
RUN_ID = "run_" + "a" * 64
SESSION = "11111111-2222-4333-8444-555555555555"

NEW_CODES = {
    "CONFIRMATION_REQUIRED": 2, "SERVICE_REQUEST_REJECTED": 10, "SERVICE_RESOURCE_NOT_FOUND": 10,
    "SERVICE_FEATURE_DISABLED": 10, "SERVICE_BALANCE_INSUFFICIENT": 10, "SERVICE_PREMIUM_MODEL_LOCKED": 10,
    "SERVICE_ACCOUNT_SUSPENDED": 10,
    "SERVICE_WRITE_CONFLICT": 10, "GITHUB_LINK_REQUIRED": 10, "SERVICE_RESPONSE_TOO_LARGE": 10,
    "SERVICE_WATCH_DEADLINE": 21, "HOSTED_RUN_FAILED": 22, "RUN_SUBMISSION_AMBIGUOUS": 22,
    "HOSTED_RUN_DETACHED": 21, "HOSTED_RUN_CANCELLED": 24, "EXAMPLE_NOT_VIEWABLE": 2,
}
# Pinned in 11 corpus, fixture and test files; the service operations must not change it.
HOSTED_UNAVAILABLE = {
    "code": "HOSTED_UNAVAILABLE", "boundary": "hosted-service", "exitCode": 10, "retryable": False,
    "message": "Programs run here only with a local harness; hosted runs use `prose cli run submit`.",
    "action": "To use the hosted service, run `cli run submit FILE --preview`; running programs on this machine needs a local harness (`cli harness list`).",
}


def load_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def project_fields(value, fields):
    """The public projection of the manifest: `true` copies a member, an
    object recurses (into each element of an array)."""
    if fields is True:
        return value
    if isinstance(value, list):
        return [project_fields(item, fields) for item in value]
    if isinstance(value, dict) and isinstance(fields, dict):
        return {key: project_fields(value[key], sub) for key, sub in fields.items() if key in value}
    return value


def walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}/{index}")


def renderer_help_text() -> str:
    return (CLI / "conformance" / "cases" / "fixtures" / "runner-help.txt").read_text()


def command_path(operation: dict) -> list[str]:
    """An operation's command path: its manifest `command` argv without the
    leading `cli`."""
    command = list(operation["command"])
    return command[1:] if command[:1] == ["cli"] else command


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = [*sorted(SCHEMAS.glob("*.schema.json")), *sorted((SCHEMAS / "service").glob("*.schema.json")),
                     *sorted((SERVICE / "responses").glob("*.schema.json")), SERVICE / "operations.schema.json",
                     RUNNER / "service-fixture.schema.json"]
        cls.documents = {path: load_json(path) for path in cls.paths}
        registry = Registry()
        for document in cls.documents.values():
            registry = registry.with_resource(document["$id"], Resource.from_contents(document))
        cls.registry = registry
        cls.manifest_bytes = (SERVICE / "operations.v1.json").read_bytes()
        cls.manifest = json.loads(cls.manifest_bytes)
        cls.taxonomy = {e["code"]: e for e in load_json(SHARED / "errors" / "taxonomy.v1.json")["errors"]}
        cls.operations = {o["id"]: o for o in cls.manifest["operations"]}

    def errors(self, schema, instance):
        validator = jsonschema.Draft202012Validator(schema, registry=self.registry, format_checker=jsonschema.FormatChecker())
        return [f"{list(e.absolute_path)}: {e.message[:160]}" for e in validator.iter_errors(instance)]

    def assert_valid(self, target: str, instance):
        self.assertEqual([], self.errors({"$ref": BASE + target}, instance))

    def assert_invalid(self, target: str, instance):
        self.assertTrue(self.errors({"$ref": BASE + target}, instance), f"expected {target} to reject {instance!r:.200}")

    def result_ref(self, operation_id: str) -> str:
        return self.operations[operation_id]["output"]["schema"]

    def failure(self, code: str, **details):
        record = {"schema": "openprose.runner-error/1", **self.taxonomy[code]}
        if details:
            record["details"] = details
        return record

    # ------------------------------------------------------------------ schemas
    def test_every_service_schema_is_valid_draft_2020_12_with_unique_id(self):
        ids = set()
        for path, document in self.documents.items():
            jsonschema.Draft202012Validator.check_schema(document)
            self.assertNotIn(document["$id"], ids, path)
            ids.add(document["$id"])

    def test_no_property_name_matches_cost_in_service_schemas(self):
        targets = [p for p in self.paths if p.parent in (SCHEMAS / "service", SERVICE / "responses")]
        targets += [SCHEMAS / "service-operation.schema.json", SCHEMAS / "service-event.schema.json"]
        for path in targets:
            for where, node in walk(self.documents[path]):
                if isinstance(node, dict):
                    for key in ("properties", "$defs", "patternProperties"):
                        for name in node.get(key, {}) if isinstance(node.get(key), dict) else ():
                            self.assertIsNone(COST.search(name), f"{path.name} {where}/{key}/{name}")
                    for name in node.get("required", []) if isinstance(node.get("required"), list) else ():
                        self.assertIsNone(COST.search(name), f"{path.name} {where} required {name}")
        self.assertIsNone(COST.search(json.dumps(self.manifest["operations"])), "manifest operations mention cost")

    def test_result_objects_are_closed_or_cost_free_maps(self):
        for path in sorted((SCHEMAS / "service").glob("*.schema.json")):
            for where, node in walk(self.documents[path]):
                if not (isinstance(node, dict) and node.get("type") == "object"):
                    continue
                extra = node.get("additionalProperties", True)
                if extra is False:
                    continue
                if where.endswith("/json") or where.endswith("/data"):
                    continue
                names = node.get("propertyNames", {})
                self.assertIn("not", names, f"{path.name} {where}: open object needs propertyNames excluding cost")
                self.assertEqual(names["not"], {"pattern": "[Cc][Oo][Ss][Tt]"})

    def test_service_status_projects_only_health_and_models(self):
        """`cli service status` is user-facing: whether the service answers and
        which models the account may run; nothing about the deployment."""
        status = self.service_status_result()
        self.assert_valid(self.result_ref("service.status"), status)
        for extra in ({"deployment": "x"}, {"features": {}}, {"version": "1"}, {"runtimes": {}}, {"wallet": {}}):
            self.assert_invalid(self.result_ref("service.status"), {**status, **extra})
        self.assert_invalid(self.result_ref("model.list"), {"models": [], "default_model": "model-sol", "hidden": []})

    # ----------------------------------------------------------------- taxonomy
    def test_taxonomy_grows_additively_to_43_codes_with_parity(self):
        self.assertEqual(43, len(self.taxonomy))
        for code, exit_code in NEW_CODES.items():
            self.assertEqual(exit_code, self.taxonomy[code]["exitCode"], code)
        schema = load_json(SCHEMAS / "runner-error.schema.json")
        self.assertEqual(set(schema["properties"]["code"]["enum"]), set(self.taxonomy))
        conditional = {c["if"]["properties"]["code"]["const"]: c["then"]["properties"] for c in schema["allOf"]}
        for code in NEW_CODES:
            for field in ("boundary", "exitCode", "retryable", "message", "action"):
                rule = conditional[code][field]
                # An Action may widen to anyOf, whose first branch stays the taxonomy text.
                value = rule["const"] if "const" in rule else rule["anyOf"][0]["const"]
                self.assertEqual(value, self.taxonomy[code][field], (code, field))
        for record in self.taxonomy.values():
            self.assertNotIn("prose cli", record["action"])
            self.assert_valid("runner-error.schema.json", {"schema": "openprose.runner-error/1", **record})

    def test_hosted_unavailable_text_is_unchanged(self):
        self.assertEqual(HOSTED_UNAVAILABLE, self.taxonomy["HOSTED_UNAVAILABLE"])

    def test_error_classification_uses_known_codes_and_allowlisted_service_codes(self):
        classification = self.manifest["errorClassification"]
        allowlist = set(load_json(SCHEMAS / "runner-error.schema.json")["properties"]["details"]["properties"]["serviceCode"]["enum"])
        for body_code, code in classification["bodyCodes"].items():
            self.assertIn(code, self.taxonomy)
            self.assertIn(body_code, allowlist)
        for row in classification["routeOverrides"]:
            self.assertIn(row["code"], self.taxonomy)
        for code in classification["statuses"].values():
            self.assertIn(code, self.taxonomy)
        self.assertEqual(["bodyCode", "routeOverride", "status"], classification["order"])
        self.assertEqual("SERVICE_FEATURE_DISABLED", classification["bodyCodes"]["feature_disabled"])
        self.assertEqual("SERVICE_AUTH_REQUIRED", classification["statuses"]["403"])

    def test_runner_error_details_carry_service_fields(self):
        planned = {"operation": "run.submit", "method": "POST", "description": "Submit a hosted run.",
                   "bodySha256": "0" * 64, "bodyBytes": 120, "effect": "money",
                   "quote": {"hold": {"hold_usd": "1.02", "hold_cents": 102, "ttl_seconds": 900}}}
        self.assert_valid("runner-error.schema.json", self.failure("CONFIRMATION_REQUIRED", plannedRequest=planned))
        # A plan never names the service route, its query or the price policy.
        for internal in ({"path": "/run"}, {"query": {"live": "1"}}, {"interaction": "run.submit"},
                         {"quote": {**planned["quote"], "pricing_policy_id": "pricing-policy.example"}}):
            self.assert_invalid("runner-error.schema.json", self.failure("CONFIRMATION_REQUIRED", plannedRequest={**planned, **internal}))
        self.assert_valid("runner-error.schema.json", self.failure(
            "SERVICE_FEATURE_DISABLED", serviceStatus=404, serviceCode="feature_disabled"))
        # A plan never names service feature flags.
        self.assert_invalid("runner-error.schema.json", self.failure("CONFIRMATION_REQUIRED", plannedRequest={**planned, "flags": ["x"]}))
        self.assert_valid("runner-error.schema.json", self.failure(
            "SERVICE_REQUEST_REJECTED", serviceStatus=400, serviceCode="model_not_found", serviceMessage="model is not available"))
        self.assert_valid("runner-error.schema.json", self.failure(
            "SERVICE_WATCH_DEADLINE", runId=RUN_ID, afterSequence=12, resumable=True))
        self.assert_invalid("runner-error.schema.json", self.failure(
            "SERVICE_WATCH_DEADLINE", runId=RUN_ID, afterSequence=12, resumable=False))
        self.assert_valid("runner-error.schema.json", self.failure("RUN_SUBMISSION_AMBIGUOUS", session=SESSION))
        for bad in ({"serviceCode": "not_allowlisted"}, {"serviceMessage": "line\nbreak"}, {"serviceMessage": "x" * 513},
                    {"serviceStatus": 99}, {"plannedRequest": {**planned, "headers": {"Authorization": TOKEN}}},
                    {"runId": "../etc"}, {"session": "not-a-uuid"}):
            self.assert_invalid("runner-error.schema.json", self.failure("SERVICE_REQUEST_REJECTED", **bad))

    # ------------------------------------------------------------- environments
    def test_account_results_are_envelope_results(self):
        """`cli auth ...` and `cli org list` print the service-operation/1
        envelope; their results are closed and name no service environment."""
        account = {"authenticated": True, "credentialSource": "environment"}
        self.assert_valid("service-account.schema.json", account)
        for source in ("store", "none"):
            self.assert_valid("service-account.schema.json", {**account, "credentialSource": source})
        self.assert_invalid("service-account.schema.json", {**account, "credentialSource": "os-credential-store"})
        for extra in ({"environment": "production"}, {"region": "a1"}, {"schema": "openprose.service-account/1"}):
            self.assert_invalid("service-account.schema.json", {**account, **extra})
        self.assert_valid("service-operation.schema.json", self.envelope("auth.status", account))
        orgs = {"organizations": [{"id": "org-1", "slug": "acme", "name": "Acme", "role": "admin"}]}
        self.assert_valid("organization-list.schema.json", orgs)
        self.assert_invalid("organization-list.schema.json", {**orgs, "environment": "production"})
        self.assert_valid("service-operation.schema.json", self.envelope("org.list", orgs))
        self.assertEqual(["production"], list(self.manifest["environments"]))
        self.assertEqual(["--output"], self.manifest["grammar"]["globalOptions"])

    # ---------------------------------------------------------------- envelopes
    def service_status_result(self):
        return {"status": "ok", "models": ["model-sol", "model-luna"], "default_model": "model-sol"}

    def envelope(self, operation, result, problem=None, **extra):
        return {"schema": "openprose.service-operation/1", "operation": operation,
                "interaction": self.operations[operation]["interaction"], "result": result, "problem": problem, **extra}

    def test_envelope_success_failure_and_environment_rules(self):
        result = self.service_status_result()
        self.assert_valid("service-operation.schema.json", self.envelope("service.status", result))
        self.assert_valid(self.result_ref("service.status"), result)
        failure = self.envelope("run.submit", None, self.failure("CONFIRMATION_REQUIRED"))
        self.assert_valid("service-operation.schema.json", failure)
        self.assert_invalid("service-operation.schema.json", {**failure, "result": {}})
        self.assert_invalid("service-operation.schema.json", self.envelope("service.status", None))
        # The envelope names no service environment.
        for environment in ("production", "custom", None):
            self.assert_invalid("service-operation.schema.json", {**self.envelope("service.status", result), "environment": environment})
        self.assert_invalid("service-operation.schema.json", {**self.envelope("service.status", result), "region": "x9"})
        self.assert_invalid("service-operation.schema.json", {**self.envelope("service.status", result), "token": TOKEN})
        # A paged result carries its cursor; the envelope does not.
        paged = self.envelope("run.list", {"runs": [], "nextBefore": "8210005908474_" + RUN_ID})
        self.assert_valid("service-operation.schema.json", paged)
        self.assert_valid(self.result_ref("run.list"), paged["result"])
        self.assert_invalid("service-operation.schema.json", {**paged, "nextBefore": None})

    def test_invocation_error_envelope_rules(self):
        """An argv rejected before an operation ran is still the
        envelope. `operation: "cli"` (no operation named) and a null
        environment (the environment itself was invalid) are only for a problem."""
        invalid = self.failure("INVOCATION_INVALID")
        unnamed = {"schema": "openprose.service-operation/1", "operation": "cli",
                   "interaction": None, "result": None, "problem": invalid}
        self.assert_valid("service-operation.schema.json", unnamed)
        # A service command's invocation error says what was wrong.
        for message in ("Unknown command.", "Unknown option.", "That command isn't quite right."):
            self.assert_valid("service-operation.schema.json", {**unnamed, "problem": {**invalid, "message": message}})
        self.assert_invalid("service-operation.schema.json", {**unnamed, "problem": {**invalid, "message": "Something else."}})
        self.assert_invalid("service-operation.schema.json", {**unnamed, "problem": None, "result": {}})
        self.assert_invalid("service-operation.schema.json", {**unnamed, "interaction": "runs.list"})
        self.assert_invalid("service-operation.schema.json", {**unnamed, "problem": self.failure("SERVICE_UNAVAILABLE")})
        self.assert_invalid("service-operation.schema.json", {**unnamed, "region": "x9"})
        named = self.envelope("run.list", None, invalid)
        self.assert_valid("service-operation.schema.json", named)
        self.assert_invalid("service-operation.schema.json", {**named, "nextBefore": None})

    def run_terminal(self, **changes):
        terminal = {"run_id": RUN_ID, "status": "completed", "cancelled": False,
                    "environment": "builtin",
                    "response": "{\"text\":\"ok\"}", "files": ["outputs/result.json"], "price_cents": 2,
                    "environment_price_cents": 1, "billing_status": "settled",
                    "usage": {"input_tokens": 10, "output_tokens": 2}}
        terminal.update(changes)
        return terminal

    def test_run_stream_result_and_signed_urls_are_closed(self):
        ref = self.result_ref("run.submit")
        stream = {"runId": RUN_ID, "run_id": RUN_ID, "session": SESSION, "detached": False, "afterSequence": 6, "run": self.run_terminal()}
        self.assert_valid(ref, stream)
        self.assert_valid(ref, {**stream, "detached": True, "run": None})
        # A detached run is {run_id, status}; nothing else of the record.
        self.assert_valid(ref, {**stream, "detached": True, "run": {"run_id": RUN_ID, "status": "running"}})
        self.assert_invalid(ref, {**stream, "detached": True, "run": {"run_id": RUN_ID, "status": "running", "files": []}})
        self.assert_invalid(ref, {key: value for key, value in stream.items() if key != "run_id"})
        self.assert_invalid(ref, {**stream, "run": self.run_terminal(file_urls={"a": "https://x/?tok=1"})})
        self.assert_invalid(ref, {**stream, "run": self.run_terminal(files={"outputs/result.json": "content"})})
        # The service's runtime name and environment version are not part of the record.
        self.assert_invalid(ref, {**stream, "run": self.run_terminal(runtime="x")})
        self.assert_invalid(ref, {**stream, "run": self.run_terminal(environment={"id": "builtin", "version": "1.0.0", "runtime_contract": 1})})
        detached = {**stream, "detached": True, "run": None, "resumeArgv": ["cli", "run", "watch", RUN_ID, "--after", "6", "--session", SESSION],
                    "cancelArgv": ["cli", "run", "cancel", RUN_ID, "--session", SESSION, "--yes"]}
        self.assert_valid(ref, detached)
        self.assert_valid(ref, {**detached, "reused": True})
        manifest = {"run_id": RUN_ID, "created_at": "2026-09-23T20:29:47.788Z", "status": "completed", "model": "model-sol",
                    "files": ["outputs/result.json"], "has_patch": False}
        # `run show` puts the record at result.run, as submit and watch do.
        shown = {"runId": RUN_ID, "run": manifest}
        self.assert_valid(self.result_ref("run.show"), shown)
        self.assert_invalid(self.result_ref("run.show"), manifest)
        self.assert_invalid(self.result_ref("run.show"), {**shown, "run": {**manifest, "file_urls": {}}})
        self.assert_invalid(self.result_ref("run.show"), {**shown, "run": {**manifest, "customer_id": "cus_x"}})
        self.assert_invalid(self.result_ref("run.show"), {**shown, "run": {**manifest, "files": ["../escape"]}})

    def test_event_projection_and_single_terminal_shape(self):
        base = {"schema": "openprose.service-event/1", "runId": RUN_ID, "sequence": 1,
                "at": "2026-09-24T01:11:48.833Z"}
        valid = [
            ("status", {"status": "running", "message": "Running on model-luna", "controls": {"steer": True, "stop": True}}),
            ("agent_activity", {"kind": "tool_end", "message": "Wrote file", "target": "scratch/a.txt", "outcome": "success",
                                "details": [{"label": "Observed file change", "text": "+1", "format": "diff"}],
                                "tool": "fs_write", "agent": "Agent"}),
            ("text_chunk", {"text": "{\"text\":\"ok\"}"}),
            ("browser_live_view_changed", {"status": "live", "started_at": "2026-09-24T01:11:48.833Z"}),
            ("history_truncated", {"message": "Older activity is no longer retained."}),
            ("error", {"message": "Stopped by the user."}),
            ("unrecognized", {"name": "future_event"}),
        ]
        for kind, data in valid:
            self.assert_valid("service-event.schema.json", {**base, "type": kind, "data": data})
        self.assert_invalid("service-event.schema.json", {**base, "type": "status", "data": {"status": "running", "session": SESSION}})
        self.assert_invalid("service-event.schema.json", {**base, "type": "run_complete", "data": {}})
        self.assert_invalid("service-event.schema.json", {**base, "type": "unrecognized", "data": {"name": "Bad Name!"}})
        terminal = {**base, "sequence": None, "at": None, "type": "service.completed",
                    "data": self.envelope("run.submit", {"runId": RUN_ID, "run_id": RUN_ID, "session": SESSION, "detached": False,
                                                         "afterSequence": 7, "run": self.run_terminal()}),
                    "exitCode": 0}
        self.assert_valid("service-event.schema.json", terminal)
        # The terminal line carries the exit code; no other line does.
        self.assert_invalid("service-event.schema.json", {key: value for key, value in terminal.items() if key != "exitCode"})
        self.assert_invalid("service-event.schema.json", {**base, "type": "status", "data": {"status": "running"}, "exitCode": 0})
        self.assert_invalid("service-event.schema.json", {**terminal, "sequence": 7})
        self.assert_invalid("service-event.schema.json", {**terminal, "type": "service.failed"})
        failed = {**terminal, "type": "service.failed", "runId": None, "exitCode": 22,
                  "data": self.envelope("run.submit", None, self.failure("RUN_SUBMISSION_AMBIGUOUS", session=SESSION))}
        self.assert_valid("service-event.schema.json", failed)
        # An exit-21 end (the run continues) is service.detached, never service.failed.
        event_types = load_json(SCHEMAS / "service-event.schema.json")["properties"]["type"]["enum"]
        self.assertEqual(self.manifest["stream"]["terminalEvents"], [t for t in event_types if t.startswith("service.")])
        detached = {**terminal, "type": "service.detached", "exitCode": 21,
                    "data": self.envelope("run.submit", None, self.failure("HOSTED_RUN_DETACHED", session=SESSION))}
        self.assert_valid("service-event.schema.json", detached)
        self.assert_invalid("service-event.schema.json", {**detached, "type": "service.failed"})
        self.assert_invalid("service-event.schema.json", {**failed, "type": "service.detached"})
        self.assert_invalid("service-event.schema.json", {**terminal, "type": "service.detached"})
        # `run watch` of an ended run without a live session reads its record.
        self.assert_valid(self.result_ref("run.watch"), {"runId": RUN_ID, "run_id": RUN_ID, "session": None, "detached": False,
                                                          "afterSequence": 0, "run": self.run_terminal(), "source": "record"})
        # Cancelling an ended run is already_ended, without a wallet read.
        self.assert_valid(self.result_ref("run.cancel"), {"runId": RUN_ID, "status": "already_ended", "runStatus": "completed"})
        self.assert_valid(self.result_ref("run.cancel"), {"runId": RUN_ID, "status": "already_ended", "runStatus": None})
        self.assert_invalid(self.result_ref("run.cancel"), {"runId": RUN_ID, "status": "already_ended", "balance": {}})

    def test_secret_bearing_results_are_confined_to_their_creating_operation(self):
        show = {"job": {"id": "3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10", "type": "webhook", "created_at": 1}}
        self.assert_valid(self.result_ref("job.show"), show)
        self.assert_invalid(self.result_ref("job.show"), {**show, "signing_secret": "s"})
        self.assert_invalid(self.result_ref("job.show"), {**show, "endpoint": "/webhooks/triggers/x"})
        self.assert_valid(self.result_ref("job.create"), {**show, "endpoint": "/webhooks/triggers/x", "signing_secret": "s"})
        # The absolute endpoint_url is additive and https-only; it
        # never carries the secret.
        url = "https://run-prose-production.openprose.workers.dev/webhooks/triggers/3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10"
        self.assert_valid(self.result_ref("job.show"), {**show, "endpoint_url": url})
        self.assert_valid(self.result_ref("job.create"), {**show, "endpoint": "/webhooks/triggers/x", "endpoint_url": url, "signing_secret": "s"})
        self.assert_valid(self.result_ref("job.rotate-secret"), {"id": show["job"]["id"], "endpoint": "/webhooks/triggers/x", "endpoint_url": url, "signing_secret": "s"})
        self.assert_invalid(self.result_ref("job.show"), {**show, "endpoint_url": "http://example.invalid/webhooks/triggers/x"})
        self.assert_valid(self.result_ref("job.list"), {"jobs": [show["job"]], "max_jobs": 5, "job_limit": {"kind": "unlimited"}, "types": []})
        # Job output uses the user noun and snake_case; service internals never appear.
        for internal in ({"internalRef": "opaque-1"}, {"adopted": True}, {"context_repository_id": 7}, {"createdAt": 1}):
            self.assert_invalid(self.result_ref("job.list"), {"jobs": [{**show["job"], **internal}],
                                                             "max_jobs": 5, "job_limit": {"kind": "unlimited"}, "types": []})
        self.assert_invalid(self.result_ref("job.list"), {"triggers": [show["job"]], "max_triggers": 5,
                                                         "trigger_limit": {"kind": "unlimited"}, "types": []})

    def test_every_epoch_ms_time_has_an_iso_companion(self):
        # Every epoch-ms result field X carries an additive
        # X_iso (RFC 3339 UTC or null) next to it, so no agent parses epochs.
        epoch = "common.schema.json#/$defs/epochMs"

        def is_epoch(schema) -> bool:
            return isinstance(schema, dict) and (schema.get("$ref") == epoch or any(
                isinstance(option, dict) and option.get("$ref") == epoch for option in schema.get("oneOf", [])))

        found = 0
        for path in sorted((SCHEMAS / "service").glob("*.schema.json")):
            for where, node in walk(self.documents[path]):
                properties = node.get("properties") if isinstance(node, dict) else None
                if not isinstance(properties, dict):
                    continue
                for name, schema in properties.items():
                    if not is_epoch(schema):
                        continue
                    found += 1
                    companion = properties.get(f"{name}_iso")
                    self.assertIsNotNone(companion, f"{path.name} {where}/{name} lacks {name}_iso")
                    self.assertEqual([{"type": "null"}, {"$ref": "common.schema.json#/$defs/timestamp"}],
                                     companion.get("oneOf"), f"{path.name} {where}/{name}_iso")
        self.assertGreaterEqual(found, 19)

    def test_list_operations_declare_records_and_jsonl_lines_validate(self):
        # Every list operation names
        # its item array in output.records; paged operations always do.
        expected = {"model.list": "models", "example.list": "examples", "repo.list": "repositories",
                    "run.list": "runs", "program.list": "programs", "program.revisions": "revisions",
                    "result.list": "results", "job.list": "jobs", "job.deliveries": "deliveries",
                    "job.contract.list": "contracts", "wallet.events": "events", "wallet.usage": "daily",
                    "org.member.list": "members", "org.list": "organizations", "service.operations": "operations"}
        declared = {o["id"]: o["output"]["records"] for o in self.manifest["operations"] if "records" in o["output"]}
        self.assertEqual(expected, declared)
        for operation in self.manifest["operations"]:
            if operation["output"]["paged"]:
                self.assertIn(operation["id"], declared, "a paged operation must declare output.records")
            if operation["contract"] == "service/1" and operation["id"].endswith(".list"):
                self.assertIn(operation["id"], declared, f"{operation['id']} is a list operation without output.records")
        for operation_id, collection in declared.items():
            reference = self.operations[operation_id]["output"]["schema"]
            document, _, pointer = reference.partition("#")
            if document == "operations.v1.json":
                # The published manifest: its operations are the records.
                self.assertIsInstance(self.manifest[collection], list)
                continue
            node = self.documents[SCHEMAS / document]
            for part in filter(None, pointer.strip("/").split("/")):
                node = node[part]
            self.assertEqual("array", node["properties"][collection]["type"], operation_id)
            self.assertIn(collection, node["required"], operation_id)
        record = {"schema": "openprose.service-record/1", "operation": "run.list",
                  "collection": "runs", "index": 0, "record": {"run_id": RUN_ID}}
        page = {"schema": "openprose.service-page/1", "operation": "run.list",
                "interaction": "runs.list", "collection": "runs", "count": 1, "nextBefore": "c1", "meta": {}, "exitCode": 0}
        self.assert_valid("service-record.schema.json", record)
        self.assert_valid("service-page.schema.json", page)
        self.assert_valid("service-page.schema.json", {**page, "nextBefore": None})
        self.assert_invalid("service-page.schema.json", {**page, "region": "alpha"})
        self.assert_invalid("service-page.schema.json", {**page, "environment": "production"})
        self.assert_invalid("service-record.schema.json", {**record, "environment": "production"})
        self.assert_invalid("service-page.schema.json", {**page, "count": -1})
        self.assert_invalid("service-page.schema.json", {k: v for k, v in page.items() if k != "nextBefore"})
        self.assert_invalid("service-record.schema.json", {**record, "index": -1})
        self.assert_invalid("service-record.schema.json", {**record, "problem": None})

    def test_human_goldens_use_plain_words(self):
        """Human output of a service command names no HTTP status, runtime
        name or internal wording: the corpus goldens (stdout and stderr text)
        never contain these strings."""
        retired = ("Runner invocation is invalid", "Service status:", "  patch: ", "configuration revision",
                   "Contract run", "not available in this build", "reactor", "semantic_diff")
        runner_words = {"doctor", "harness", "cleanup", "config"}
        corpus = CLI / "conformance" / "cases" / "service"
        checked = 0
        for path in sorted(corpus.glob("*/*.json")):
            case = load_json(path)
            argv = case["argv"]
            if "cli" in argv and argv.index("cli") + 1 < len(argv) and argv[argv.index("cli") + 1] in runner_words:
                continue
            if case["id"].startswith("service-capabilities"):
                continue  # the exit-code dictionary quotes the taxonomy messages
            texts = [case.get("stdout", {}).get("text", ""), case.get("stderr", {}).get("text", "")]
            texts += case.get("stderr", {}).get("contains", [])
            for text in texts:
                for word in retired:
                    self.assertNotIn(word, text, f"{path.name} human output says {word!r}")
            checked += 1
        self.assertGreater(checked, 500)

    # ---------------------------------------------------------------- manifest
    def test_manifest_schema_identity_and_help_freshness(self):
        self.assertEqual([], self.errors(load_json(SERVICE / "operations.schema.json"), self.manifest))
        self.assertTrue(self.manifest_bytes.endswith(b"\n") and b"\r" not in self.manifest_bytes)
        self.assertEqual(self.manifest_bytes.decode("ascii"), json.dumps(self.manifest, indent=2, ensure_ascii=True) + "\n")
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        self.assertEqual((SERVICE / "help.v1.json").read_text(), renderer.render(self.manifest_bytes))
        topics = load_json(SERVICE / "help.v1.json")["topics"]
        for operation in self.manifest["operations"]:
            if operation["contract"] == "service/1":
                self.assertIn("cli " + " ".join(command_path(operation)), topics)

    def test_runner_help_lists_every_command_exit_code_and_agent_entry(self):
        """`prose --help` names every command (and only real ones),
        every exit code of the error taxonomy, the environment variables and the
        agent entry points; the check itself rejects drift."""
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        text = renderer.RUNNER_HELP.read_text()
        self.assertEqual([], renderer.runner_help_problems(self.manifest, text))
        self.assertTrue(renderer.runner_help_problems(self.manifest, text + "\n  prose cli service nonexistent\n"))
        self.assertTrue(renderer.runner_help_problems(self.manifest, text.replace("\n  25  ", "\n  ")))
        self.assertTrue(renderer.runner_help_problems(self.manifest, text.replace("For agents:", "Agents")))

    def test_help_topics_print_manifest_examples_and_agents_first(self):
        """Every help topic prints an Examples section taken from
        the manifest (removing one fails the check), and `prose --help` starts
        "For agents" within its first 25 lines."""
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        document = load_json(SERVICE / "help.v1.json")
        topics = document["topics"]
        self.assertGreaterEqual(len(topics), 79)
        self.assertEqual([], renderer.examples_problems(self.manifest, topics))
        for operation in self.manifest["operations"]:
            text = topics["cli " + " ".join(command_path(operation))]
            for example in operation["examples"]:
                self.assertIn("\n  " + example + "\n", text)
        broken = dict(topics)
        broken["cli run submit"] = topics["cli run submit"].replace(
            "\n  " + next(o for o in self.manifest["operations"] if o["id"] == "run.submit")["examples"][0], "")
        self.assertTrue(renderer.examples_problems(self.manifest, broken))
        broken["cli wallet"] = topics["cli wallet"].replace("\nExamples:\n", "\n")
        self.assertIn("`cli wallet` has no Examples section", renderer.examples_problems(self.manifest, broken))
        manifest = deepcopy(self.manifest)
        next(o for o in manifest["operations"] if o["id"] == "wallet.usage")["examples"].append(
            "prose cli wallet usage --json")
        self.assertTrue(renderer.examples_problems(manifest, topics))
        self.assertIn("Default: 30 days ago (UTC).", topics["cli wallet usage"])
        self.assertIn("a new program is private", topics["cli program save"])
        self.assertNotIn("cli api", topics)
        self.assertNotIn("cli environment", topics)
        self.assertEqual([], self.errors(load_json(SCHEMAS / "service-capabilities.schema.json"), document["capabilities"]))
        text = renderer.RUNNER_HELP.read_text()
        self.assertIn("For agents:", text.splitlines()[:25])
        moved = text.replace("For agents:", "Agent notes:", 1) + "\nFor agents:\n"
        self.assertTrue(any("first 25 lines" in problem
                            for problem in renderer.runner_help_problems(self.manifest, moved)))

    def test_runner_help_leads_with_the_service_commands(self):
        """`prose --help` opens with the hosted service commands (run,
        program, job, wallet, auth); the language and harness usage and the
        runner options come later, under "Advanced / local harness"."""
        lines = renderer_help_text().splitlines()
        first = lines.index("Hosted OpenProse service commands (never prompt):")
        advanced = lines.index("Advanced / local harness:")
        self.assertLess(first, 10)
        self.assertEqual(["run", "program", "job", "wallet", "auth"],
                         [line.split()[2] for line in lines[first + 1:first + 6]])
        for later in ("Runner commands:", "Runner options:"):
            self.assertLess(advanced, lines.index(later))
        self.assertLess(first, advanced)
        self.assertFalse(any("<LANGUAGE_COMMAND>" in line for line in lines[:advanced]))

    def test_help_exit_codes_come_from_the_manifest_and_match_emitted_codes(self):
        """Every help "Exit codes:" line is rendered from the
        operation's manifest exitCodes, and the check rejects an exit list that
        disagrees with the taxonomy or with the codes the corpora emit."""
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        self.assertEqual([], renderer.exit_code_problems(self.manifest))
        topics = load_json(SERVICE / "help.v1.json")["topics"]
        self.assertEqual([], renderer.topic_problems(self.manifest, topics))
        self.assertIn("21 deadline or detached: the run id is known and the run continues (resume with details.resumeArgv)", topics["cli run submit"])
        self.assertNotIn("submission ambiguous", topics["cli run watch"])

        def mutated(operation_id, change):
            manifest = deepcopy(self.manifest)
            change(next(o for o in manifest["operations"] if o["id"] == operation_id)["exitCodes"])
            return renderer.exit_code_problems(manifest)

        # The former watch line: `22 run failed or submission ambiguous`.
        def ambiguous_watch(entries):
            next(e for e in entries if e["exit"] == 22)["codes"].append("RUN_SUBMISSION_AMBIGUOUS")
        self.assertTrue(any("RUN_SUBMISSION_AMBIGUOUS" in p for p in mutated("run.watch", ambiguous_watch)))
        # The former draft line listed only 0, 2 and 10; its corpus emits 22 and 24.
        def pre_draft_failure(entries):
            del entries[3:]
        self.assertTrue(any("program-draft-run-failed" in p for p in mutated("program.draft", pre_draft_failure)))
        # A code under the wrong exit, an unlisted lifecycle code, a missing 0.
        self.assertTrue(mutated("run.submit", lambda e: next(x for x in e if x["exit"] == 24)["codes"].append("HOSTED_RUN_DETACHED")))
        self.assertTrue(mutated("run.watch", lambda e: next(x for x in e if x["exit"] == 21).pop("codes")))
        self.assertTrue(mutated("run.show", lambda e: e.pop(0)))
        # Topics: a missing verb topic, the runner help, a missing device-flow note.
        self.assertTrue(renderer.topic_problems(self.manifest, {k: v for k, v in topics.items() if k != "cli auth logout"}))
        self.assertTrue(renderer.topic_problems(self.manifest, {**topics, "cli org list": "OpenProse outer runner\n"}))
        self.assertTrue(renderer.topic_problems(self.manifest, {
            **topics, "cli auth login": topics["cli auth login"].replace("Device flow: ", "")}))

    def test_manifest_exit_dictionary_matches_taxonomy_and_every_emitted_code(self):
        """The manifest-level exitCodes dictionary lists every
        problem code any shared corpus case emits, with the exit that case
        expects, and each entry equals the taxonomy; the check rejects drift."""
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        taxonomy = load_json(SHARED / "errors" / "taxonomy.v1.json")
        emitted = renderer.emitted_exit_codes(self.manifest, taxonomy)
        self.assertEqual([], renderer.exit_dictionary_problems(self.manifest, taxonomy, emitted))
        dictionary = self.manifest["exitCodes"]
        codes = {code for _, _, code, _ in emitted if code}
        self.assertGreaterEqual(len(codes), 15)
        for operation_id, exit_code, code, case in emitted:
            if code:
                self.assertEqual(exit_code, dictionary[code]["exit"], f"{case} ({operation_id})")
        for code, entry in dictionary.items():
            self.assertEqual((entry["exit"], entry["retryable"], entry["meaning"]),
                             (self.taxonomy[code]["exitCode"], self.taxonomy[code]["retryable"], self.taxonomy[code]["message"]))

        def mutated(change):
            manifest = deepcopy(self.manifest)
            change(manifest["exitCodes"])
            return renderer.exit_dictionary_problems(manifest, taxonomy, emitted)

        self.assertTrue(any("HOSTED_RUN_DETACHED" in p for p in mutated(lambda d: d.pop("HOSTED_RUN_DETACHED"))))
        self.assertTrue(mutated(lambda d: d["SERVICE_WATCH_DEADLINE"].update(exit=22)))
        self.assertTrue(mutated(lambda d: d["SERVICE_UNAVAILABLE"].update(retryable=False)))
        self.assertTrue(mutated(lambda d: d["INVOCATION_INVALID"].update(meaning="Bad.")))
        self.assertTrue(mutated(lambda d: d.update(HARNESS_FAILED={"exit": 22, "retryable": False,
                                                                     "meaning": self.taxonomy["HARNESS_FAILED"]["message"]})))
        self.assertTrue(mutated(lambda d: d.pop("CONFIRMATION_REQUIRED")))

    def test_manifest_env_vars_and_examples(self):
        """`envVars` names every credential variable and
        PROSE_OUTPUT; every operation has examples that name its command, use
        only its options and never stop at CONFIRMATION_REQUIRED."""
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        names = [variable["name"] for variable in self.manifest["envVars"]]
        for environment in self.manifest["environments"].values():
            self.assertIn(environment["credentialEnv"], names)
        self.assertIn("PROSE_OUTPUT", names)
        self.assertEqual([], renderer.example_problems(self.manifest))

        def mutated(operation_id, examples):
            manifest = deepcopy(self.manifest)
            next(o for o in manifest["operations"] if o["id"] == operation_id)["examples"] = examples
            return renderer.example_problems(manifest)

        self.assertTrue(mutated("model.list", ["prose cli model list --bogus"]))
        self.assertTrue(mutated("model.list", ["prose cli model lst"]))
        self.assertTrue(mutated("wallet.topup", ["prose cli wallet topup --yes"]))
        self.assertTrue(mutated("wallet.topup", ["prose cli wallet topup --amount-cents 500"]))
        self.assertTrue(mutated("run.show", ["prose cli run show"]))
        self.assertTrue(mutated("run.show", ["prose cli run show a b"]))
        self.assertTrue(mutated("run.show", ["cli run show run_1"]))

    def test_capabilities_document_and_operation_views(self):
        """`cli service capabilities` (JSON and page) and the
        human `cli service operations` table are rendered from the manifest."""
        help_document = load_json(SERVICE / "help.v1.json")
        capabilities = help_document["capabilities"]
        self.assert_valid("service-capabilities.schema.json", capabilities)
        self.assertEqual(capabilities["exitCodes"], self.manifest["exitCodes"])
        self.assertEqual(capabilities["envVars"], self.manifest["envVars"])
        # The digest of the published manifest (`cli service operations --json`
        # result): canonical JSON of its public projection.
        published = project_fields(self.manifest, load_json(SERVICE / "operations-public.v1.json")["fields"])
        text = json.dumps(published, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(capabilities["manifest"]["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual([o["id"] for o in capabilities["operations"]], [o["id"] for o in self.manifest["operations"]])
        self.assert_invalid("service-capabilities.schema.json", {**capabilities, "extra": 1})
        page = help_document["views"]["cli service capabilities"]
        for code, entry in self.manifest["exitCodes"].items():
            self.assertRegex(page, rf"\n  {entry['exit']} +{code} ")
        for variable in self.manifest["envVars"]:
            self.assertIn(f"\n  {variable['name']} ", page)
        table = help_document["views"]["cli service operations"].split("\n")
        self.assertEqual(table[0].split(), ["OPERATION", "EFFECT", "CONFIRM", "USAGE"])
        rows = [line.split()[:3] for line in table[1:] if line and not line.startswith("Details:")]
        self.assertEqual(sorted(rows), sorted([o["id"], o["effect"], "yes" if o["confirm"] else "no"]
                                              for o in self.manifest["operations"]))
        self.assertIn("service.capabilities", self.operations)
        self.assertEqual(self.operations["service.capabilities"]["transport"], "none")

    def test_service_triage_composes_six_reads_with_price_only_sections(self):
        """`cli service triage` is one read composing health,
        balance, default organization, recent runs and jobs; each
        section carries its own problem; only price fields; the corpus pins
        the authenticated, anonymous, partial-failure and no-cost cases."""
        operation = self.operations["service.triage"]
        self.assertEqual((operation["effect"], operation["confirm"], operation["preview"]), ("read", False, False))
        self.assertEqual([(r["method"], r["path"], r["auth"]) for r in operation["requests"]], [
            ("GET", "/health", "none"), ("GET", "/wallet/balance", "bearer"),
            ("GET", "/organizations/{name}", "bearer"), ("GET", "/runs", "bearer"), ("GET", "/triggers", "bearer")])
        self.assertEqual(operation["requests"][3]["query"], {"limit": "5"})
        self.assertEqual([entry["exit"] for entry in operation["exitCodes"]], [0, 2, 24])
        cases = RUNNER.parent / "cases" / "service" / "discovery"
        for name in ("triage-authenticated", "triage-anonymous", "triage-partial-failure", "triage-no-cost-fields"):
            self.assertTrue((cases / f"{name}.json").is_file(), name)
        ref = self.result_ref("service.triage")
        result = load_json(cases / "triage-authenticated.json")["stdout"]["json"]["result"]
        self.assert_valid(ref, result)
        self.assertIsNone(COST.search(json.dumps(result)))
        self.assert_invalid(ref, {**result, "wallet": {**result["wallet"], "balance": {
            **result["wallet"]["balance"], "available_cost_cents": 1}}})
        self.assert_invalid(ref, {**result, "flags": {"disabled": [], "problem": None}})
        self.assert_invalid(ref, {**result, "health": {**result["health"], "version": "x"}})
        self.assert_invalid(ref, {**result, "runs": {**result["runs"], "total_cost_cents": 1}})
        self.assert_invalid(ref, {**result, "nextCommands": result["nextCommands"] * 3})
        failing = {"problem": self.failure("SERVICE_UNAVAILABLE", serviceStatus=503)}
        self.assert_valid(ref, {**result, "wallet": failing, "organization": failing, "jobs": None})
        self.assert_invalid(ref, {**result, "health": None})
        anonymous = load_json(cases / "triage-anonymous.json")
        self.assertEqual(anonymous["exitCode"], 0)
        self.assertEqual([e["path"] for e in anonymous["fixture"]["exchanges"]], ["/health"])
        self.assertEqual(anonymous["stdout"]["json"]["result"]["nextCommands"][0]["env"], "OPENPROSE_API_KEY")
        human = load_json(cases / "triage-authenticated-human.json")["stdout"]["text"]
        self.assertLessEqual(len(human.splitlines()), 25)

    def test_service_guide_is_checked_against_the_manifest(self):
        """`cli service guide` is a no-request framework
        operation; guide.v1.md keeps its required sections and recipes, every
        command in it parses against the manifest, and the generated corpus
        cases pin both views to the file."""
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        guide = (SERVICE / "guide.v1.md").read_text()
        client_doc = (CLI.parent / "docs" / "hosted-service-client.md").read_text()
        operation = self.operations["service.guide"]
        self.assertEqual((operation["transport"], operation["confirm"], operation["requests"]), ("none", False, []))
        self.assertEqual(self.manifest["grammar"]["intentInference"]["nounSynonyms"]["guide"], ["service", "guide"])
        self.assertEqual([], renderer.guide_problems(self.manifest, guide, client_doc))
        sections = renderer.guide_sections(guide)
        self.assert_valid(self.result_ref("service.guide"), {"sections": sections})
        self.assert_invalid(self.result_ref("service.guide"), {"sections": [{**sections[0], "id": "Start Here"}]})
        self.assertEqual(renderer.guide_slug("Get a run's answer"), "get-a-runs-answer")
        rebuilt = "\n".join(f"## {s['title']}\n\n{s['body']}\n" for s in sections)
        self.assertEqual(renderer.GUIDE_TITLE + rebuilt, guide)
        topics = load_json(SERVICE / "help.v1.json")["topics"]
        for path, content in renderer.guide_cases(topics, guide).items():
            self.assertEqual(path.read_text(), content, f"{path.name} is stale; run render_service_help.py --write")
        self.assertEqual(load_json(RUNNER.parent / "cases" / "service" / "framework" / "service-guide-human.json")
                         ["stdout"]["text"], guide)

        def drift(old, new, document=client_doc):
            self.assertIn(old, guide)
            return renderer.guide_problems(self.manifest, guide.replace(old, new), document)

        self.assertTrue(drift("prose cli run list --limit 50 --json", "prose cli run lst --limit 50 --json"))
        self.assertTrue(drift("--spec-file hook.json --yes", "--spec hook.json --yes"))
        self.assertTrue(drift("result publish haiku --run RUN_ID --yes", "result publish haiku --run RUN_ID"))
        self.assertTrue(drift("prose cli run show RUN_ID --json", "prose cli run show --json"))
        self.assertTrue(drift("`cli wallet events`", "`cli wallet event`"))
        self.assertTrue(drift("prose cli run quote --json", "prose --bogus-global cli run quote --json"))
        self.assertTrue(drift("## Paging", "## Pages"))
        self.assertTrue(drift("### Create a webhook job", "### Make a hook"))
        self.assertTrue(drift("## Paging", "## Paging", client_doc.replace("guide.v1.md", "guide.md")))

    def test_global_aliases_before_cli_resolve_and_never_shadow(self):
        """An option alias whose target is a global option is
        suggested before `cli` too, so it must never be a runner global itself."""
        aliases = self.manifest["grammar"]["intentInference"]["optionAliases"]
        globals_ = set(self.manifest["grammar"]["globalOptions"])
        targeted = {name for name, targets in aliases.items() if set(targets) & globals_}
        self.assertTrue({"--format"} <= targeted)
        self.assertFalse({"--environment", "--production"} & set(aliases))
        # `--env` means a run input or environment, never a service selection.
        self.assertFalse(set(aliases.get("--env", [])) & globals_)
        runner_globals = set(re.findall(r"^  (--[a-z-]+)", renderer_help_text(), re.MULTILINE))
        self.assertFalse(targeted & runner_globals, "a global alias shadows a runner option")

    def test_language_commands_match_spec_and_lone_synonyms_are_exact(self):
        """`languageCommands` is the SPEC 7.1 list, so a lone
        language command before `cli` is never intercepted; the lone
        nounSynonyms words the parser intercepts each name exactly one command."""
        tables = self.manifest["grammar"]["intentInference"]
        spec = (CLI / "SPEC.md").read_text()
        block = spec.split("### 7.1 Forwarded language commands", 1)[1].split("```text", 1)[1].split("```", 1)[0]
        self.assertEqual(sorted(block.split()), tables["languageCommands"])
        runner = [["doctor"], ["harness", "list"], ["harness", "use"], ["cleanup", "prime"], ["config", "explain"]]
        paths = {tuple(command_path(operation)) for operation in self.manifest["operations"]} | {tuple(p) for p in runner}
        for word in ("login", "logout", "whoami", "models", "topup", "operations", "status"):
            target = tuple(tables["nounSynonyms"][word])
            groups = [p for p in paths if p[:len(target)] == target and len(p) == len(target) + 1]
            self.assertTrue(target in paths or len(groups) == 1, f"lone synonym {word} -> {target} is ambiguous")

    def test_intent_inference_tables_resolve_and_never_shadow(self):
        """Every synonym names real commands or options, and no synonym is itself one."""
        tables = self.manifest["grammar"]["intentInference"]
        runner = [["doctor"], ["harness", "list"], ["harness", "use"], ["cleanup", "prime"], ["config", "explain"]]
        paths = [command_path(operation) for operation in self.manifest["operations"]] + runner
        prefixes = {tuple(path[:length]) for path in paths for length in range(1, len(path) + 1)}
        nouns = {path[0] for path in paths}
        verbs = {path[index] for path in paths for index in range(1, len(path))}
        for word, target in tables["nounSynonyms"].items():
            self.assertNotIn(word, nouns, f"noun synonym {word} shadows a command")
            self.assertIn(tuple(target), prefixes, f"noun synonym {word} -> {target} is not a command path")
        for word, candidates in tables["verbSynonyms"].items():
            self.assertTrue(set(candidates) <= verbs, f"verb synonym {word} -> {candidates} names an unknown verb")
            # A synonym applies only where the word is not a verb; it must apply somewhere.
            groups = {prefix for prefix in prefixes if any(len(p) > len(prefix) and tuple(p[:len(prefix)]) == prefix for p in paths)}
            reachable = [g for g in groups
                         if word not in {p[len(g)] for p in paths if len(p) > len(g) and tuple(p[:len(g)]) == g}
                         and any(c in {p[len(g)] for p in paths if len(p) > len(g) and tuple(p[:len(g)]) == g} for c in candidates)]
            self.assertTrue(reachable, f"verb synonym {word} never resolves in any group")
        options = {"--json", "--yes", "--preview", "--help", "--output"}
        options |= {option["name"] for operation in self.manifest["operations"] for option in operation["options"]}
        for alias, candidates in tables["optionAliases"].items():
            self.assertTrue(set(candidates) <= options, f"option alias {alias} -> {candidates} names an unknown option")
        # The unit, rewrite and secret tables name real things
        # and never shadow a real option or command path.
        for source, conversion in tables["optionConversions"].items():
            self.assertNotIn(source, options, f"option conversion {source} shadows an option")
            self.assertNotIn(source, tables["optionAliases"], f"{source} is both an alias and a conversion")
            self.assertIn(conversion["option"], options, f"option conversion {source} names an unknown option")
        by_command = {tuple(command_path(operation)): operation for operation in self.manifest["operations"]}
        for phrase, rewrite in tables["commandRewrites"].items():
            words = tuple(phrase.split(" "))
            self.assertNotIn(words, prefixes, f"command rewrite {phrase!r} shadows a command path")
            # A phrase is a group plus a word it lacks, or starts with a word
            # that is no command at all (`environment list`).
            self.assertTrue(words[:-1] in prefixes or words[0] not in nouns,
                            f"command rewrite {phrase!r} is not in a command group")
            self.assertNotIn(words[0], tables["nounSynonyms"], f"command rewrite {phrase!r} is shadowed by a noun synonym")
            target = by_command.get(tuple(rewrite["command"]))
            self.assertIsNotNone(target, f"command rewrite {phrase!r} names no operation")
            names = {option["name"] for option in target["options"]}
            appended = [token for token in rewrite["append"] if token.startswith("--")]
            common = {option["name"] for option in self.manifest["grammar"]["commonOptions"]}
            if "--preview" in appended:
                self.assertTrue(target["preview"], f"command rewrite {phrase!r} previews {target['id']}, which has no preview")
            self.assertTrue(set(appended) <= names | common, f"command rewrite {phrase!r} appends an option {target['id']} lacks")
            if "argumentCommand" in rewrite:
                self.assertIn(tuple(rewrite["argumentCommand"]), by_command, f"command rewrite {phrase!r} names no argument command")
            if "argumentOption" in rewrite:
                self.assertIn(rewrite["argumentOption"], names, f"command rewrite {phrase!r} names an unknown option")
        # Aliases are exact spellings of real commands, in a real group, and
        # never shadow one; they run the target, so they are no suggestion.
        for phrase, target in tables["commandAliases"].items():
            words = tuple(phrase.split(" "))
            self.assertNotIn(words, prefixes, f"alias {phrase!r} shadows a command path")
            self.assertIn(words[:-1], prefixes, f"alias {phrase!r} is not in a command group")
            self.assertEqual(words[:-1], tuple(target[:-1]), f"alias {phrase!r} leaves its group")
            self.assertIn(tuple(target), by_command, f"alias {phrase!r} names no operation")
            self.assertNotIn(phrase, tables["commandRewrites"], f"{phrase!r} is both an alias and a rewrite")
        ids = {operation["id"] for operation in self.manifest["operations"]}
        for name, removal in tables["optionRemovals"].items():
            self.assertNotIn(name, options, f"option removal {name} drops a real option")
            self.assertNotIn(name, tables["optionAliases"], f"{name} is both an alias and a removal")
            self.assertTrue(set(removal["operations"]) <= ids, f"option removal {name} names an unknown operation")
        for name in tables["secretFileOptions"]:
            owners = [operation["id"] for operation in self.manifest["operations"]
                      if any(option["name"] == name and option["required"] for option in operation["options"])]
            self.assertTrue(owners, f"secret file option {name} is no operation's required option")
            for owner in owners:
                self.assertEqual([], self.operations[owner]["arguments"], f"{owner} takes positional arguments")
        limits = tables["distance"]
        self.assertEqual(("optimal-string-alignment", 4, 1, 2),
                         (limits["metric"], limits["shortWordLength"], limits["shortWordMax"], limits["max"]))

    def test_manifest_policy_matches_the_decision_record(self):
        manifest = self.manifest
        self.assertEqual("refuse", manifest["policy"]["redirects"])
        self.assertEqual("none", manifest["policy"]["retries"])
        self.assertEqual(65536, manifest["transportClasses"]["account"]["maxResponseBytes"])
        self.assertEqual(45000, manifest["transportClasses"]["stream"]["idleTimeoutMs"])
        self.assertEqual({"production": {"origin": "https://run-prose-production.openprose.workers.dev",
                                         "credentialEnv": "OPENPROSE_API_KEY", "credentialStore": True}},
                         manifest["environments"])
        self.assertEqual(["--model", "--harness", "--dry-run"], manifest["grammar"]["rejectedGlobalOptions"])
        for operation_id in ("run.submit", "program.draft", "job.create", "wallet.topup", "wallet.redeem", "run.share",
                             "program.delete", "program.visibility", "result.publish", "org.create", "org.invite"):
            self.assertTrue(self.operations[operation_id]["confirm"], operation_id)
        for operation_id in ("service.status", "run.quote", "run.list", "run.show", "wallet.balance", "org.list", "job.contract.list"):
            self.assertFalse(self.operations[operation_id]["confirm"], operation_id)
        submit = self.operations["run.submit"]
        post = next(r for r in submit["requests"] if r["method"] == "POST")
        # Always live; placement and repository intent are optional query
        # templates because the service reads them from the query.
        self.assertEqual({"live": "1", "session": "{session}", "environment": "{environment}", "runtime": "{runtime}",
                          "repositories": "{repositories}", "output_repository": "{output_repository}"}, post["query"])
        self.assertEqual("text/event-stream", post["headers"]["Accept"])
        self.assertEqual("{session}", post["headers"]["X-Session-Id"])
        self.assertNotIn("org.delete", self.operations)
        # The shipped manifest carries no inventory of service surface the client does not use.
        self.assertNotIn("exclusions", manifest)
        self.assertNotIn("routeExclusions", manifest)

    def test_confirm_reasons_name_consequences(self):
        """Every confirm-class operation carries a manifest
        confirmReason naming its consequence; the help gate and the manifest
        schema both refuse a circular reason and a reason on other operations."""
        renderer = load_module("openprose_render_service_help", CLI / "ci" / "render_service_help.py")
        self.assertEqual([], renderer.confirm_reason_problems(self.manifest))
        manifest_schema = self.documents[SERVICE / "operations.schema.json"]
        self.assertEqual([], self.errors(manifest_schema, self.manifest))
        for operation in self.manifest["operations"]:
            self.assertEqual(operation["confirm"], "confirmReason" in operation, operation["id"])
        for mutate, expected in (
            (lambda ops: ops["job.rotate-secret"].__setitem__("confirmReason", "the service requires confirmation for it"), "circular"),
            (lambda ops: ops["result.unpublish"].pop("confirmReason"), "needs a confirmReason"),
            (lambda ops: ops["run.quote"].__setitem__("confirmReason", "it reads the quote from the service"), "does not confirm"),
        ):
            manifest = deepcopy(self.manifest)
            mutate({o["id"]: o for o in manifest["operations"]})
            problems = renderer.confirm_reason_problems(manifest)
            self.assertTrue(any(expected in problem for problem in problems), (expected, problems))
            self.assertNotEqual([], self.errors(manifest_schema, manifest), expected)
        help_topics = json.loads((SERVICE / "help.v1.json").read_text())["topics"]
        for operation in self.manifest["operations"]:
            if operation["confirm"] and operation["contract"] == "service/1":
                topic = help_topics["cli " + " ".join(command_path(operation))]
                self.assertIn(f"required because {operation['confirmReason']}.", topic, operation["id"])
                self.assertNotIn("requires confirmation for it", topic, operation["id"])

    def test_planned_summary_fields_match_the_schema(self):
        """`plannedRequest.summary` admits exactly the fields the
        manifest's confirmation.summaryFields lists, and none is a secret."""
        fields = self.manifest["confirmation"]["summaryFields"]
        schema = self.documents[SCHEMAS / "service-operation.schema.json"]["$defs"]["plannedRequest"]["properties"]["summary"]
        self.assertEqual({f["field"] for f in fields}, set(schema["properties"]))
        secret = re.compile(r"secret|token|code|key|content|current_program|request", re.IGNORECASE)
        for field in fields:
            self.assertIsNone(secret.search(field["body"]), field)
        self.assertNotIn("inputs", {f["field"] for f in fields})

    def test_job_spec_schemas_are_closed_and_consistent(self):
        """`job create/update/configure` publish a closed spec
        schema; every required key is an accepted key and the unpaid rule
        names a real variant."""
        def check(obj, where):
            for key in obj["required"]:
                self.assertIn(key, obj["keys"], where)
            for name, field in obj["keys"].items():
                if isinstance(field, dict) and ("object" in field or "array" in field):
                    check(field.get("object") or field.get("array"), f"{where}.{name}")
        create = self.operations["job.create"]["spec"]
        self.assertTrue({"schedule", "webhook"} <= set(create["variants"]))
        self.assertIn(create["unpaidWhen"]["type"], create["variants"])
        self.assertIn(create["unpaidWhen"]["absent"], create["variants"][create["unpaidWhen"]["type"]]["keys"])
        for name, variant in create["variants"].items():
            check({"required": variant["required"], "keys": {**create["common"], **variant["keys"]}}, name)
        for operation_id in ("job.update", "job.configure"):
            spec = self.operations[operation_id]["spec"]
            check(spec, operation_id)
            self.assertIn(spec["option"], {o["name"] for o in self.operations[operation_id]["options"]})
        self.assertEqual({"job.create", "job.update", "job.configure"}, {o["id"] for o in self.manifest["operations"] if "spec" in o})

    def test_not_found_entries_name_real_arguments_and_listing_commands(self):
        """Every operation that addresses a resource by an
        argument has a manifest `notFound` entry; its placeholders name the
        operation's own arguments and its `list` is a real command."""
        commands = {tuple(command_path(o)) for o in self.manifest["operations"]}
        addressed = {"RUN_ID", "JOB_ID", "SLUG", "ORG", "NAME", "OWNER/SLUG", "OWNER/SLUG[@REV]", "PUBLICATION_ID",
                     "ACCOUNT_ID", "INVITATION_ID"}
        creators = {"program.save", "org.create", "org.rename"}
        placeholder = re.compile(r"\{([^{}]+)\}")

        def check(operation, entry, where):
            names = {a["name"] for a in operation["arguments"]}
            self.assertRegex(entry["resource"], r"^[a-z]+$", where)
            for word in [entry["id"], *entry.get("list", [])]:
                for name in placeholder.findall(word):
                    self.assertIn(name, names, f"{where}: {{{name}}} is not an argument")
            if "list" in entry:
                words = [w for w in entry["list"] if not placeholder.search(w)]
                self.assertTrue(any(tuple(words[:len(c)]) == c for c in commands), f"{where}: {entry['list']} is not a command")
            for code, override in entry.get("serviceCodes", {}).items():
                self.assertIn(code, self.manifest["errorClassification"]["bodyCodes"], where)
                check(operation, override, f"{where}/{code}")

        for operation in self.manifest["operations"]:
            names = {a["name"] for a in operation["arguments"]}
            entry = operation.get("notFound")
            if entry is None:
                self.assertFalse(names & addressed and operation["id"] not in creators and operation["contract"] == "service/1"
                                 and operation["id"] not in ("program.draft", "run.submit"),
                                 f"{operation['id']} addresses {names & addressed} without a notFound entry")
                continue
            check(operation, entry, operation["id"])
        for operation_id in ("job.show", "job.delete", "run.show", "program.delete", "org.show", "result.list"):
            self.assertIn("notFound", self.operations[operation_id], operation_id)
        self.assertEqual({"resource": "job", "id": "{JOB_ID}", "list": ["job", "list"]}, self.operations["job.show"]["notFound"])
        # The two deletes are idempotent: their results admit already_absent
        # (jobs, snake_case) and alreadyAbsent (programs).
        self.assert_valid(self.result_ref("job.delete"), {"id": "3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10", "deleted": True, "already_absent": True})
        self.assert_valid(self.result_ref("program.delete"), {"slug": "hello", "deleted": True, "alreadyAbsent": True})
        self.assert_invalid(self.result_ref("program.delete"), {"slug": "hello", "deleted": True, "alreadyAbsent": False})

    def test_vendored_export_provenance(self):
        data = (SERVICE / "service-interactions.v1.json").read_bytes()
        source = load_json(SERVICE / "service-interactions.source.json")
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(digest, source["sha256"])
        self.assertEqual(digest, self.manifest["interactionsSource"]["sha256"])
        sync = load_module("openprose_sync_interactions", CLI / "ci" / "sync_service_interactions.py")
        exported = sync.validate_projection(data)
        # The projection holds exactly the mapped interactions and the routes the manifest sends.
        mapped, used = sync.manifest_usage(self.manifest)
        self.assertEqual(mapped, {item["id"] for item in exported["interactions"]})
        self.assertEqual(used, {(item["id"], r["method"], r["path"], r["auth"])
                                for item in exported["interactions"] for r in item["routes"]})
        self.assertEqual({"schema", "upstreamSha256", "sha256"}, set(source))
        # Exactly the public fields: nothing else an export may carry is vendored.
        self.assertEqual({"schema", "principals", "interactions"}, set(exported))
        self.assertTrue(all(set(item) == {"id", "principal", "effect", "reversible", "agent", "routes"}
                            for item in exported["interactions"]))
        self.assertTrue(all(set(route) == {"method", "path", "auth"}
                            for item in exported["interactions"] for route in item["routes"]))
        leaky = json.loads(data)
        leaky["interactions"][0]["description"] = "private text"
        with self.assertRaises(sync.SyncError):
            sync.validate_projection((json.dumps(leaky, indent=2) + "\n").encode())
        extra = json.loads(data)
        extra["notes"] = "extra"
        with self.assertRaises(sync.SyncError):
            sync.validate_projection((json.dumps(extra, indent=2) + "\n").encode())
        # An export is checked against the public format, then projected to exactly
        # the vendored bytes: unmapped interactions, unused routes and any field
        # outside the public format are dropped.
        upstream = json.loads(data)
        upstream["notes"] = "extra top-level field"
        for item in upstream["interactions"]:
            item["notes"] = "extra interaction field"
            for route in item["routes"]:
                route["notes"] = "extra route field"
        upstream["interactions"][0]["routes"].append({"method": "HEAD", "path": "/unused", "auth": "none"})
        upstream["interactions"].append({"id": "zzz.unmapped", "principal": "customer",
                                         "effect": "read", "reversible": True, "agent": "auto",
                                         "routes": [{"method": "GET", "path": "/zzz", "auth": "api_key"}]})
        doc = sync.validate_export((json.dumps(upstream, indent=2) + "\n").encode())
        self.assertEqual(data, sync.project(doc, self.manifest))
        self.assertEqual(["zzz.unmapped"], sync.unmapped(doc, self.manifest))
        # An export names its own interactions schema; the projection always
        # carries the vendored one. A schema of another kind is refused.
        renamed = sync.validate_export((json.dumps(dict(upstream, schema="openprose.example-interactions/1"),
                                                   indent=2) + "\n").encode())
        self.assertEqual(data, sync.project(renamed, self.manifest))
        with self.assertRaises(sync.SyncError):
            sync.validate_export((json.dumps(dict(upstream, schema="openprose.service-operations/1"),
                                             indent=2) + "\n").encode())
        # An export missing a public field is refused.
        missing = json.loads(data)
        del missing["interactions"][0]["agent"]
        with self.assertRaises(sync.SyncError):
            sync.validate_export((json.dumps(missing, indent=2) + "\n").encode())
        # A mapped interaction missing from the export fails the sync.
        doc["interactions"] = [item for item in doc["interactions"] if item["id"] != "wallet.balance"]
        with self.assertRaises(sync.SyncError):
            sync.project(doc, self.manifest)

    # ------------------------------------------------------------ A3 responses
    def test_response_index_and_examples(self):
        index = load_json(SERVICE / "responses" / "index.json")
        names = {row["name"] for row in index["responses"]}
        for row in index["responses"]:
            self.assertTrue((SERVICE / "responses" / row["schema"]).is_file(), row)
        for operation in self.manifest["operations"]:
            for request in operation["requests"]:
                if request["response"] is not None:
                    self.assertIn(request["response"], names, (operation["id"], request["path"]))
        examples = {
            "wallet-balance": {"customer_id": "cus_example", "balance": {
                "available_cents": 3337, "available_dollars": "33.37", "available_nanos": 1, "posted_cents": 3337,
                "posted_dollars": "33.37", "reserved_cents": 0, "reserved_dollars": "0.00", "reserved_nanos": 0}},
            "runs-list": {"runs": [{"run_id": RUN_ID, "created_at": "2026-09-23T20:29:47.788Z", "status": "completed",
                                    "model": "model-sol", "program_ref": "owner/slug@0123456789abcdef"}], "next_before": None},
            "run-quote": {"hold": {"hold_usd": "1.02", "ttl_seconds": 900}, "pricing_policy_id": "p", "note": "n"},
            "error": {"error": "Not allowed", "code": "feature_disabled", "feature": "example_feature"},
            "run-complete": {"type": "run_complete", "run_id": RUN_ID, "status": "error", "cancelled": True, "files": []},
        }
        for name, body in examples.items():
            self.assert_valid(f"service/responses/{name}.schema.json", body)
        self.assert_invalid("service/responses/run-quote.schema.json", {"hold": {"hold_usd": 1.02, "ttl_seconds": 900}})

    # ------------------------------------------------------------- fixtures
    def test_fixture_schema_admits_production_transcripts_only(self):
        fixture = {"$ref": BASE + "conformance/service-fixture.schema.json"}
        for path in sorted((RUNNER.parent / "cases" / "service" / "account").glob("*.json")):
            self.assertEqual([], self.errors(fixture, load_json(path)["fixture"]), path.name)
        self.assertTrue(self.errors(fixture, {"environment": "other", "exchanges": []}))
        self.assertTrue(self.errors(fixture, {"credentials": {"other": TOKEN}, "exchanges": []}))
        self.assertTrue(self.errors(fixture, {"region": "a1", "exchanges": []}))
        sse = {"environment": "production", "credentials": {"production": TOKEN}, "storeAvailable": True,
               "clock": {"start": "2026-09-24T00:00:00Z", "stepMs": 1}, "ids": [SESSION],
               "exchanges": [{"method": "POST", "path": "/run", "query": {"live": "1", "session": SESSION},
                              "requestHeaders": {"X-Session-Id": SESSION, "Authorization": {"present": True}},
                              "status": 200, "responseHeaders": {"Content-Type": "text/event-stream"},
                              "sse": {"frames": [{"comment": "heartbeat"}, {"id": "1", "event": "status",
                                                                            "data": {"type": "status", "status": "running"}}],
                                      "end": "disconnect"}}]}
        self.assertEqual([], self.errors(fixture, sse))
        self.assertTrue(self.errors(fixture, {**sse, "exchanges": [{**sse["exchanges"][0], "bodyText": "x"}]}))

    # ------------------------------------------------------------ coverage drift
    def coverage(self, manifest=None, a1=None, strict=True):
        module = load_module("openprose_service_coverage", RUNNER / "service_coverage.py")
        a1 = a1 if a1 is not None else json.loads((SERVICE / "service-interactions.v1.json").read_bytes())
        digest = self.manifest["interactionsSource"]["sha256"]  # digest drift is covered by test_vendored_export_provenance
        manifest = deepcopy(manifest if manifest is not None else self.manifest)
        errors, _, _ = module.audit(manifest, a1, digest, strict)
        return errors

    def test_coverage_is_clean(self):
        self.assertEqual([], self.coverage())

    def test_coverage_mutations_are_caught(self):
        def mutated(change):
            manifest = deepcopy(self.manifest)
            change({o["id"]: o for o in manifest["operations"]}, manifest)
            return self.coverage(manifest)

        def delete_route(ops, _):
            ops["wallet.balance"]["requests"][0]["catalogRoute"]["path"] = "/wallet/balances"

        def change_flag(ops, _):
            ops["wallet.topup"]["requests"][0]["catalogRoute"]["auth"] = "none"

        def unconfirm(ops, _):
            ops["run.submit"]["confirm"] = False

        def weaken_effect(ops, _):
            ops["program.delete"]["effect"] = "write"

        def anonymous_key_route(ops, _):
            ops["model.list"]["requests"][0]["auth"] = "none"

        def unknown_request(ops, _):
            ops["wallet.balance"]["requests"][0]["interaction"] = "zzz.unknown"

        for change in (delete_route, change_flag, unconfirm, weaken_effect, anonymous_key_route, unknown_request):
            self.assertTrue(mutated(change), change.__name__)

    def test_new_catalog_interaction_fails_strict_coverage(self):
        a1 = json.loads((SERVICE / "service-interactions.v1.json").read_bytes())
        a1["interactions"].append({"id": "zeta.new", "principal": "customer", "effect": "read",
                                   "reversible": True, "agent": "auto",
                                   "routes": [{"method": "GET", "path": "/zeta", "auth": "api_key"}]})
        self.assertTrue(any("zeta.new" in e for e in self.coverage(a1=a1)))
        self.assertFalse(any("zeta.new" in e for e in self.coverage(a1=a1, strict=False)))

    def test_strict_case_coverage_floor(self):
        """`--strict` (the service-coverage gate) needs one success and one failure
        case per service operation; the account operations are frozen elsewhere."""
        module = load_module("openprose_service_coverage", RUNNER / "service_coverage.py")
        cases = RUNNER.parent / "cases" / "service"
        errors, summary = module.case_coverage(self.manifest, cases)
        self.assertEqual([], errors)
        self.assertIn("auth.login", summary["frozenAccount"])
        manifest = deepcopy(self.manifest)
        manifest["operations"].append({**deepcopy(manifest["operations"][0]), "id": "zeta.uncovered",
                                       "contract": "service/1"})
        errors, _ = module.case_coverage(manifest, cases)
        self.assertIn("zeta.uncovered: no success case in cli/conformance/cases/service/", errors)
        self.assertIn("zeta.uncovered: no failure case in cli/conformance/cases/service/", errors)


def schema_ref_closure_problems(registered: list[str]) -> list[str]:
    """Every cross-file `$ref` reachable from `registered` must name a registered schema.

    `registered` holds schema stems under cli/shared/schemas (`runner-error`,
    `service/jobs`). A missing target is what makes Ajv throw "can't resolve
    reference" when the Bun suite compiles the set.
    """
    names = set(registered)
    problems = []

    def refs(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    yield value
                else:
                    yield from refs(value)
        elif isinstance(node, list):
            for value in node:
                yield from refs(value)

    for name in sorted(names):
        document = load_json(SCHEMAS / f"{name}.schema.json")
        for ref in refs(document):
            target = ref.split("#", 1)[0]
            if not target:
                continue
            absolute = (Path(name).parent / target).as_posix()
            stem = absolute.removesuffix(".schema.json").removeprefix("./")
            if stem not in names:
                problems.append(f"{name}.schema.json $ref {ref!r} needs {stem!r} registered")
    return problems


class BunSchemaRegistrationTest(unittest.TestCase):
    """cli/bun/test/shared-contract.test.ts registers schemas by name for Ajv.

    The `runner-error` -> `service-operation` reference went unregistered
    and the whole Bun file errored at load; the bun-tests gate was already red on
    this host, so nothing noticed. This keeps the list closed under `$ref` in the
    green shared-contracts gate.
    """

    SOURCE = CLI / "bun" / "test" / "shared-contract.test.ts"

    def registered(self) -> list[str]:
        text = self.SOURCE.read_text("utf-8")
        block = re.search(r"for \(const name of \[(.*?)\]\) \{\s*ajv\.addSchema", text, re.S)
        self.assertIsNotNone(block, "shared-contract.test.ts no longer registers schemas in one name loop")
        return re.findall(r'"([a-z0-9/-]+)"', block.group(1))

    def test_registered_schemas_are_closed_under_ref(self):
        registered = self.registered()
        self.assertIn("runner-error", registered)
        self.assertEqual([], schema_ref_closure_problems(registered))

    def test_closure_check_catches_a_dropped_schema(self):
        registered = [name for name in self.registered() if name != "service-operation"]
        problems = schema_ref_closure_problems(registered)
        self.assertTrue(any("service-operation" in problem for problem in problems), problems)


if __name__ == "__main__":
    unittest.main()
