#!/usr/bin/env python3
from __future__ import annotations

import base64
import ast
from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError as error:  # pragma: no cover
    raise SystemExit(
        "Install cli/shared/requirements-test.txt before running adapter oracle tests."
    ) from error


CLI = Path(__file__).resolve().parents[3]
ROOT = CLI.parent
SHARED = CLI / "shared"
SCHEMAS = SHARED / "schemas"
CAPABILITIES = SHARED / "capabilities" / "adapters"
RECIPES = CAPABILITIES / "recipes"
FIXTURES = SHARED / "fixtures" / "adapters"
SCENARIOS = FIXTURES / "scenarios"
PROBE = FIXTURES / "bin" / "adapter_probe.py"
GENERATOR = FIXTURES / "generate.py"
ORACLE_PATH = CAPABILITIES / "oracle.v1.json"
FUNCTIONAL_ALPHA_PATH = CAPABILITIES / "functional-alpha.v1.json"
INVARIANTS_PATH = Path(__file__).with_name("invariants.v1.json")
TASK_PATH = SHARED / "fixtures" / "transport" / "sentinel-task.json"
IMAGE_ROOT = SHARED / "image" / "echo-v0"
IMAGE_MANIFEST_PATH = IMAGE_ROOT / "manifest.json"
FRAMING_PATH = IMAGE_ROOT / "contracts" / "one-field-framing.txt"


def load_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def canonical_json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AdapterOracleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            path.name: load_json(path) for path in sorted(SCHEMAS.glob("*.schema.json"))
        }
        cls.registry = Registry()
        for schema in cls.schemas.values():
            cls.registry = cls.registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )
        cls.recipe_validator = jsonschema.Draft202012Validator(
            cls.schemas["adapter-admission-recipe.schema.json"],
            registry=cls.registry,
            format_checker=jsonschema.FormatChecker(),
        )
        cls.case_validator = jsonschema.Draft202012Validator(
            load_json(CLI / "conformance" / "cases" / "case-manifest.schema.json"),
            format_checker=jsonschema.FormatChecker(),
        )
        cls.oracle = load_json(ORACLE_PATH)
        cls.functional_alpha = load_json(FUNCTIONAL_ALPHA_PATH)
        cls.oracle_adapters = {
            record["adapterId"]: record for record in cls.oracle["adapters"]
        }
        cls.recipes = {
            recipe["adapterId"]: recipe
            for recipe in (load_json(path) for path in sorted(RECIPES.glob("*.json")))
        }
        cls.scenarios = {
            scenario["adapterId"]: scenario
            for scenario in (
                load_json(path) for path in sorted(SCENARIOS.glob("*.json"))
            )
        }
        cls.task_bytes = canonical_json(load_json(TASK_PATH))
        image_manifest = load_json(IMAGE_MANIFEST_PATH)
        cls.image_bytes = b"".join(
            (IMAGE_ROOT / entry["path"]).read_bytes()
            for entry in image_manifest["payload"]
        )

    def assert_recipe_valid(self, recipe) -> None:
        errors = sorted(
            self.recipe_validator.iter_errors(recipe), key=lambda item: list(item.path)
        )
        self.assertEqual(
            [], [f"{list(error.path)}: {error.message}" for error in errors]
        )

    def test_recipes_validate_and_remain_unclaimed(self) -> None:
        expected = {
            "agents-sdk/jsonl",
            "codex/exec-json",
            "claude/print-stream-json",
            "prime/rpc",
            "omp/rpc",
        }
        self.assertEqual(expected, set(self.recipes))
        self.assertEqual(expected, set(self.scenarios))
        self.assertEqual(expected, set(self.oracle_adapters))
        for adapter_id, recipe in self.recipes.items():
            with self.subTest(adapter=adapter_id):
                self.assert_recipe_valid(recipe)
                self.assertEqual("frozen", recipe["state"])
                self.assertEqual([], recipe["admissionClaims"])
                self.assertIs(recipe["launch"]["shell"], False)
                self.assertIs(recipe["launch"]["outerPty"], False)
                self.assertEqual("non-interactive", recipe["launch"]["interactionMode"])
                self.assertEqual("diagnostics-only", recipe["launch"]["stderr"])
                self.assertEqual(
                    recipe["launch"]["stdinLifecycle"],
                    self.scenarios[adapter_id]["stdinLifecycle"],
                )
                self.assertEqual(
                    "protocol-error", recipe["requiredTerminal"]["eofWithoutEvent"]
                )
                self.assertEqual("unsupported", recipe["cancellation"]["containment"])
                self.assertEqual(
                    "blocked",
                    self.oracle_adapters[adapter_id]["strictAdmission"]["status"],
                )
                self.assertGreater(len(recipe["support"]["admittedVersions"]), 0)
                self.assertEqual(
                    len(recipe["support"]["admittedVersions"]),
                    len(set(recipe["support"]["admittedVersions"])),
                )
                self.assertNotIn("\n", recipe["support"]["repairCommand"])

        expected_versions = {
            "agents-sdk/jsonl": ["0.1.0"],
            "prime/rpc": ["0.7.0", "0.8.1"],
            "omp/rpc": ["18.0.9"],
            "codex/exec-json": ["0.149.0-alpha.4.1"],
            "claude/print-stream-json": ["2.1.243"],
        }
        self.assertEqual(
            expected_versions,
            {
                adapter_id: recipe["support"]["admittedVersions"]
                for adapter_id, recipe in self.recipes.items()
            },
        )
        functional = {
            record["adapterId"]: record for record in self.functional_alpha["adapters"]
        }
        self.assertEqual(set(self.recipes) - {"agents-sdk/jsonl"}, set(functional))
        # The generic SDK recipe is implemented but has no functional-alpha
        # measurement. Recipe validation must not silently promote it.
        self.assertNotIn("agents-sdk/jsonl", functional)
        for adapter_id in functional:
            recipe = self.recipes[adapter_id]
            self.assertEqual(
                recipe["support"]["versionRange"],
                functional[adapter_id]["versionRange"],
            )
            self.assertEqual(
                recipe["support"]["admittedVersions"],
                functional[adapter_id]["admittedVersions"],
            )
            self.assertEqual(
                recipe["support"]["repairCommand"],
                functional[adapter_id]["repairCommand"],
            )

    def test_oracle_evidence_and_blockers_are_closed(self) -> None:
        self.assertEqual("direct-argv-only", self.oracle["globalInvariants"]["spawn"])
        self.assertIs(self.oracle["globalInvariants"]["shell"], False)
        self.assertIs(self.oracle["globalInvariants"]["outerPty"], False)
        self.assertEqual("forbidden", self.oracle["globalInvariants"]["fallback"])
        source_ids = [source["id"] for source in self.oracle["sources"]]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        source_by_id = {source["id"]: source for source in self.oracle["sources"]}
        for source in self.oracle["sources"]:
            self.assertIn(source["status"], {"documented", "observed"})
            self.assertLessEqual(
                date.fromisoformat(source["retrievedAt"]),
                date.fromisoformat(self.oracle["retrievedAt"]),
            )
            self.assertTrue(source["url"])
            self.assertTrue(source["notes"])
        for adapter in self.oracle["adapters"]:
            self.assertGreater(len(adapter["strictAdmission"]["blockers"]), 0)
            fact_ids = [fact["id"] for fact in adapter["facts"]]
            self.assertEqual(len(fact_ids), len(set(fact_ids)))
            self.assertTrue(
                any(fact["status"] == "unknown" for fact in adapter["facts"])
            )
            for fact in adapter["facts"]:
                self.assertIn(fact["status"], {"documented", "observed", "unknown"})
                if fact["status"] == "unknown":
                    self.assertEqual([], fact["sources"])
                else:
                    self.assertGreater(len(fact["sources"]), 0)
                    for source_id in fact["sources"]:
                        self.assertIn(source_id, source_by_id)

    def expand_recipe_argv(self, recipe, *, with_model: bool = False) -> list[str]:
        values = {
            "executable": "{{EXECUTABLE}}",
            "image-path": "{{IMAGE_PATH}}",
            "image-utf8": "{{IMAGE_UTF8}}",
            "rendered-config-path": "{{RENDERED_CONFIG_PATH}}",
            "task-json": "{{TASK_JSON}}",
            "task-path": "{{TASK_PATH}}",
            "cwd": "{{WORKSPACE}}",
            "model": "{{MODEL}}",
            "invocation-id": "fixture-invocation-0001",
            "daemon-socket-path": "{{DAEMON_SOCKET_PATH}}",
        }
        result = []
        for token in recipe["launch"]["argv"]:
            if "literal" in token:
                result.append(token["literal"])
            else:
                result.append(values[token["value"]])
        if with_model:
            optional = recipe["launch"]["optionalArgv"]
            self.assertEqual(1, len(optional))
            addition = []
            for token in optional[0]["argv"]:
                addition.append(token.get("literal", values.get(token.get("value"))))
            self.assertTrue(all(isinstance(token, str) for token in addition))
            if optional[0]["placement"] == "append":
                result.extend(addition)
            elif optional[0]["placement"] == "before-final-pair":
                result[-2:-2] = addition
            else:
                self.assertEqual("before-final-argument", optional[0]["placement"])
                result[-1:-1] = addition
        return result

    def test_recipe_templates_equal_scenario_argv(self) -> None:
        for adapter_id, recipe in self.recipes.items():
            with self.subTest(adapter=adapter_id):
                self.assertEqual(
                    self.expand_recipe_argv(recipe),
                    self.scenarios[adapter_id]["expectedArgv"],
                )
                self.assertEqual(
                    self.expand_recipe_argv(recipe, with_model=True),
                    self.scenarios[adapter_id]["expectedArgvWithModel"],
                )

    def test_prime_v07_no_tool_lifecycle_is_closed_and_source_audited(self) -> None:
        recipe = self.recipes["prime/rpc"]
        self.assertEqual(["0.7.0", "0.8.1"], recipe["support"]["admittedVersions"])
        self.assertEqual(
            "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1",
            recipe["support"]["repairCommand"],
        )
        self.assertNotIn(
            "@earendil-works/pi-coding-agent", recipe["identity"]["packageIdentity"]
        )
        self.assertEqual(
            "none; actual run is the first authentication authority",
            recipe["auth"]["readinessProbe"],
        )
        self.assertEqual(
            "close-after-terminal-event", recipe["launch"]["stdinLifecycle"]
        )
        records = self.scenarios["prime/rpc"]["fakeStdout"]
        self.assertEqual(
            [
                "response",
                "agent_start",
                "turn_start",
                "message_start",
                "message_end",
                "message_start",
                "message_update",
                "message_update",
                "message_update",
                "message_update",
                "message_update",
                "message_update",
                "message_update",
                "message_update",
                "message_update",
                "message_update",
                "message_end",
                "turn_end",
                "agent_end",
            ],
            [record["type"] for record in records],
        )
        self.assertEqual("user", records[3]["message"]["role"])
        self.assertEqual("user", records[4]["message"]["role"])
        self.assertEqual(
            [{"type": "text", "text": self.task_bytes.decode("utf-8")}],
            records[3]["message"]["content"],
        )
        self.assertEqual(records[3]["message"], records[4]["message"])
        self.assertEqual("assistant", records[5]["message"]["role"])
        self.assertEqual(
            [
                "thinking_start",
                "thinking_delta",
                "thinking_delta",
                "thinking_delta",
                "thinking_end",
                "text_start",
                "text_delta",
                "text_delta",
                "text_delta",
                "text_end",
            ],
            [record["assistantMessageEvent"]["type"] for record in records[6:16]],
        )
        for record in records[6:16]:
            self.assertNotIn("partial", record["assistantMessageEvent"])
            self.assertEqual("fixture-response-id", record["message"]["responseId"])
        thinking = ""
        for record in records[7:10]:
            thinking += record["assistantMessageEvent"]["delta"]
            self.assertEqual(thinking, record["message"]["content"][0]["thinking"])
        self.assertEqual(
            thinking,
            records[10]["assistantMessageEvent"]["content"] + "\n\n",
        )
        self.assertEqual([], records[-2]["toolResults"])
        self.assertEqual(
            [records[3]["message"], records[-3]["message"]],
            records[-1]["messages"],
        )
        self.assertEqual(
            "fixture-thinking-signature",
            records[-3]["message"]["content"][0]["thinkingSignature"],
        )
        self.assertEqual(
            "fixture-text-signature",
            records[-3]["message"]["content"][1]["textSignature"],
        )

        sources = {source["id"]: source for source in self.oracle["sources"]}
        self.assertIn("agent/turn/message", sources["prime.rpc-reference"]["notes"])
        prime = self.oracle_adapters["prime/rpc"]
        lifecycle = next(
            fact for fact in prime["facts"] if fact["id"] == "no-tool-rpc-lifecycle"
        )
        self.assertEqual("documented", lifecycle["status"])
        self.assertIn("prime.rpc-reference", lifecycle["sources"])
        text_only = next(
            fact
            for fact in prime["facts"]
            if fact["id"] == "text-only-index-zero-no-tool-rpc-lifecycle"
        )
        self.assertEqual("observed", text_only["status"])
        self.assertEqual(
            ["prime.rpc-reference", "prime.text-only-stream-source-0.8.1"],
            text_only["sources"],
        )
        source = next(
            source
            for source in self.oracle["sources"]
            if source["id"] == "prime.text-only-stream-source-0.8.1"
        )
        self.assertEqual("installed-source", source["kind"])
        self.assertIn("contentIndex 0", source["notes"])
        self.assertIn("no model call", source["notes"])
        self.assertIn("does not establish strict admission", source["notes"])

    def test_upstream_omp_v18_identity_and_closed_rpc_fixture(self) -> None:
        recipe = self.recipes["omp/rpc"]
        self.assertEqual(["omp"], recipe["identity"]["executableNames"])
        self.assertEqual(
            "@oh-my-pi/pi-coding-agent@18.0.9", recipe["identity"]["packageIdentity"]
        )
        self.assertEqual(["18.0.9"], recipe["support"]["admittedVersions"])
        self.assertRegex("omp/18.0.10", recipe["probe"]["versionPattern"])
        self.assertEqual(
            "none; actual run is the first authentication authority",
            recipe["auth"]["readinessProbe"],
        )
        self.assertEqual(
            "close-after-terminal-event", recipe["launch"]["stdinLifecycle"]
        )
        self.assertEqual("--no-lsp", recipe["launch"]["argv"][7]["literal"])
        self.assertNotIn("--no-tools", [item.get("literal") for item in recipe["launch"]["argv"]])
        self.assertEqual(
            {
                "argvFlag": "--config",
                "value": "rendered-config-path",
                "encoding": "utf8",
                "mode": "0600",
                "bytes": "retry:\n  enabled: false\ndisabledProviders:\n  - native\n  - omp-plugins\n  - claude\n  - agent-plugins\n  - claude-plugins\n  - codex\n  - gemini\n  - opencode\n  - cursor\n  - windsurf\n  - vscode\n  - mcp-json\n",
                "byteLength": 200,
                "sha256": "47b1a27639ab8d5ecd52abbb5326354419583b7b781ceb66935c822f789d2779",
                "precedence": "final-cli-overlay",
            },
            recipe["launch"]["controls"]["ownedConfigOverlay"],
        )
        self.assertEqual(
            {
                "barrierEvent": "available_commands_update",
                "stateRequestType": "get_state",
                "stateRequestIdSuffix": "omp.state.1",
                "promptRequestIdSuffix": "omp.prompt.1",
                "requiredStateDataArrayField": "dumpTools",
            },
            recipe["launch"]["controls"]["protocolPrelude"],
        )

        records = self.scenarios["omp/rpc"]["fakeStdout"]
        self.assertEqual(16, len(records))
        self.assertEqual(
            [
                "ready",
                "extension_ui_request",
                "available_commands_update",
                "response",
                "agent_start",
                "turn_start",
                "message_start",
                "message_end",
                "message_start",
                "message_update",
                "message_end",
                "turn_end",
                "extension_ui_request",
                "agent_end",
                "extension_ui_request",
                "response",
            ],
            [record["type"] for record in records],
        )
        self.assertEqual(
            {
                "type": "ready",
                "protocolVersion": 1,
                "supportedProtocolVersions": [1, 2],
                "maxFrameBytes": 1048576,
                "maxReassembledFrameBytes": 67108864,
            },
            records[0],
        )
        self.assertEqual("setWidget", records[1]["method"])
        self.assertNotIn("widgetLines", records[1])
        self.assertEqual(
            {
                "id": "fixture-invocation-0001.omp.state.1",
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"dumpTools": []},
            },
            records[3],
        )
        self.assertEqual("belowEditor", records[-4]["widgetPlacement"])
        self.assertIs(records[-3]["isTerminal"], True)
        self.assertEqual([], records[-2]["widgetLines"])
        self.assertEqual("aboveEditor", records[-2]["widgetPlacement"])
        self.assertEqual(
            {"chats", "tools", "usage", "cost", "errors", "stepCount"},
            set(records[-3]["telemetry"]),
        )
        self.assertEqual(
            {
                "toolsAvailable",
                "toolsInvoked",
                "toolsUnused",
                "modelsUsed",
                "providersUsed",
            },
            set(records[-3]["coverage"]),
        )
        self.assertEqual("fixture-invocation-0001.omp.prompt.1", records[-1]["id"])
        self.assertIs(records[-1]["success"], True)

        sources = {source["id"]: source for source in self.oracle["sources"]}
        self.assertIn("fire-and-forget", sources["omp.rpc-source"]["notes"])
        self.assertIn("widgetLines", sources["omp.rpc-ui-types-source-18.0.9"]["notes"])
        self.assertIn(
            "PI_CONFIG_FILES",
            sources["omp.deterministic-launch-source-18.0.9"]["notes"],
        )
        self.assertIn(
            "unsupported_nonterminal_settlement",
            sources["omp.nonterminal-settlement-source-18.0.9"]["notes"],
        )
        facts = {fact["id"]: fact for fact in self.oracle_adapters["omp/rpc"]["facts"]}
        for fact_id in (
            "wrapper-owned-retry-and-mcp-provider-disabled-final-config-overlay",
            "ambient-mcp-discovery-disabled",
            "no-tools-launch-and-preprompt-empty-tool-state",
            "nonterminal-agent-end-fails-closed",
        ):
            self.assertEqual("observed", facts[fact_id]["status"])

    def test_environment_policy_selects_one_group_and_strips_others(self) -> None:
        base = set(self.oracle["baseEnvironmentAllowlist"])
        self.assertEqual(len(base), len(self.oracle["baseEnvironmentAllowlist"]))
        self.assertFalse(base & set(self.oracle["environmentRules"]["alwaysStrip"]))
        self.assertIn("USER", base)
        self.assertIn("USERPROFILE", base)
        self.assertIn(
            "non-empty", self.oracle["environmentRules"]["credentialPresence"]
        )
        self.assertIn(
            "never fall back", self.oracle["environmentRules"]["credentialPresence"]
        )
        self.assertEqual(
            {"prime/rpc": {"PRIME_AGENT_TELEMETRY": "0"}},
            self.oracle["environmentRules"]["adapterOwnedControls"],
        )
        self.assertIn(
            "every actual Prime run",
            self.oracle["environmentRules"]["adapterOwnedControlPolicy"],
        )
        for adapter_id, adapter in self.oracle_adapters.items():
            scenario = self.scenarios[adapter_id]
            groups = adapter["credentialGroups"]
            selected_name = scenario["environment"]["credentialGroup"]
            self.assertIn(selected_name, groups)
            selected = set(groups[selected_name])
            all_credentials = {name for names in groups.values() for name in names}
            forbidden = set(scenario["environment"]["forbiddenNames"])
            self.assertFalse(selected & forbidden)
            self.assertTrue((all_credentials - selected).issubset(forbidden))
            self.assertTrue(
                set(self.oracle["environmentRules"]["alwaysStrip"]).issubset(forbidden)
            )
            self.assertEqual([], scenario["environment"]["additionalNames"])

        prime = self.oracle_adapters["prime/rpc"]
        telemetry = next(
            fact
            for fact in prime["facts"]
            if fact["id"] == "pseudonymous-telemetry-disabled"
        )
        self.assertEqual("observed", telemetry["status"])
        self.assertEqual(["prime.telemetry-control-0.8.1"], telemetry["sources"])
        for adapter_id in {"omp/rpc", "codex/exec-json", "claude/print-stream-json"}:
            self.assertFalse(
                any(
                    fact["id"] == "pseudonymous-telemetry-disabled"
                    for fact in self.oracle_adapters[adapter_id]["facts"]
                )
            )

    def render_framed(self) -> bytes:
        return (
            FRAMING_PATH.read_text("utf-8")
            .replace("{{IMAGE_SHA256}}", sha256(self.image_bytes))
            .replace("{{IMAGE_BYTES}}", self.image_bytes.decode("utf-8"))
            .replace("{{TASK_SHA256}}", sha256(self.task_bytes))
            .replace("{{TASK_JSON}}", self.task_bytes.decode("utf-8"))
            .encode("utf-8")
        )

    def assert_fixture_digest(self, metadata) -> bytes:
        data = (ROOT / metadata["fixture"]).read_bytes()
        self.assertEqual(metadata["byteLength"], len(data))
        self.assertEqual(metadata["sha256"], sha256(data))
        return data

    def test_wire_fixtures_are_exact_and_decodable(self) -> None:
        image_manifest = load_json(IMAGE_MANIFEST_PATH)
        self.assertEqual(
            image_manifest["modelVisibleBytes"]["byteLength"], len(self.image_bytes)
        )
        self.assertEqual(
            image_manifest["modelVisibleBytes"]["sha256"], sha256(self.image_bytes)
        )
        self.assertEqual(183, len(self.task_bytes))
        self.assertEqual(
            "a8a31920b47485944535dc7e1a742f424884922861ec85771ab699124dfc83fc",
            sha256(self.task_bytes),
        )
        framed = self.render_framed()
        codex = self.assert_fixture_digest(self.scenarios["codex/exec-json"]["stdin"])
        self.assertEqual(framed, codex)
        prime = self.assert_fixture_digest(self.scenarios["prime/rpc"]["stdin"])
        prime_frame = json.loads(prime)
        self.assertEqual("prompt", prime_frame["type"])
        self.assertEqual(self.task_bytes, prime_frame["message"].encode("utf-8"))
        self.assertEqual(
            self.scenarios["prime/rpc"]["stdin"]["decodedMessageSha256"],
            sha256(prime_frame["message"].encode("utf-8")),
        )
        prime_image = self.scenarios["prime/rpc"]["inlineImage"]
        self.assertEqual("{{IMAGE_UTF8}}", prime_image["argument"])
        self.assertEqual(len(self.image_bytes), prime_image["byteLength"])
        self.assertEqual(sha256(self.image_bytes), prime_image["sha256"])
        omp = self.assert_fixture_digest(self.scenarios["omp/rpc"]["stdin"])
        omp_frames = [json.loads(line) for line in omp.splitlines()]
        self.assertEqual(
            {"id": "fixture-invocation-0001.omp.state.1", "type": "get_state"},
            omp_frames[0],
        )
        self.assertEqual("prompt", omp_frames[1]["type"])
        self.assertEqual("fixture-invocation-0001.omp.prompt.1", omp_frames[1]["id"])
        self.assertEqual(self.task_bytes, omp_frames[1]["message"].encode("utf-8"))
        self.assertEqual(
            self.scenarios["omp/rpc"]["stdin"]["decodedMessageSha256"],
            sha256(omp_frames[1]["message"].encode("utf-8")),
        )
        stages = self.scenarios["omp/rpc"]["stdin"]["stages"]
        self.assertEqual(
            [len(line) + 1 for line in omp.splitlines()],
            [stage["byteLength"] for stage in stages],
        )

    def test_generated_adapter_fixtures_have_no_drift(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def concrete_argv(self, scenario, workspace: Path, image_path: Path) -> list[str]:
        rendered_config_path = image_path.parent / "omp-control-overlay.yml"
        rendered_config_path.write_bytes(
            b"retry:\n  enabled: false\ndisabledProviders:\n  - native\n"
            b"  - omp-plugins\n  - claude\n  - agent-plugins\n"
            b"  - claude-plugins\n  - codex\n  - gemini\n  - opencode\n"
            b"  - cursor\n  - windsurf\n  - vscode\n  - mcp-json\n"
        )
        rendered_config_path.chmod(0o600)
        replacements = {
            "{{EXECUTABLE}}": str(PROBE),
            "{{WORKSPACE}}": str(workspace),
            "{{IMAGE_PATH}}": str(image_path),
            "{{IMAGE_UTF8}}": self.image_bytes.decode("utf-8"),
            "{{TASK_JSON}}": self.task_bytes.decode("utf-8"),
            "{{RENDERED_CONFIG_PATH}}": str(rendered_config_path),
        }
        return [replacements.get(token, token) for token in scenario["expectedArgv"]]

    def fixture_environment(
        self, adapter, scenario, observation_path: Path
    ) -> dict[str, str]:
        environment = {
            name: f"fixture:{name}" for name in self.oracle["baseEnvironmentAllowlist"]
        }
        selected = scenario["environment"]["credentialGroup"]
        for name in adapter["credentialGroups"][selected]:
            environment[name] = f"fixture-secret:{name}"
        environment.update(
            self.oracle["environmentRules"]["adapterOwnedControls"].get(
                adapter["adapterId"], {}
            )
        )
        environment["OPENPROSE_ADAPTER_OBSERVATION_PATH"] = str(observation_path)
        environment["OPENPROSE_ADAPTER_EXPECTED_ID"] = adapter["adapterId"]
        return environment

    def run_probe(self, argv, *, workspace, environment, stdin, stdin_lifecycle):
        if stdin_lifecycle == "close-after-write":
            completed = subprocess.run(
                [sys.executable, *argv],
                cwd=workspace,
                env=environment,
                input=stdin,
                capture_output=True,
                check=False,
                timeout=5,
            )
            return completed.returncode, completed.stdout, completed.stderr
        process = subprocess.Popen(
            [sys.executable, *argv],
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert (
            process.stdin is not None
            and process.stdout is not None
            and process.stderr is not None
        )
        process.stdin.write(stdin)
        process.stdin.flush()
        lines = []
        while True:
            line = process.stdout.readline()
            self.assertTrue(line, "RPC probe reached EOF before agent_end")
            lines.append(line)
            if json.loads(line).get("type") == "agent_end":
                process.stdin.close()
                break
        stdout = b"".join(lines) + process.stdout.read()
        stderr = process.stderr.read()
        returncode = process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        return returncode, stdout, stderr

    def test_provider_free_probe_records_exact_launch(self) -> None:
        self.assertTrue(PROBE.is_file())
        self.assertTrue(PROBE.stat().st_mode & stat.S_IXUSR)
        for adapter_id, scenario in self.scenarios.items():
            with self.subTest(
                adapter=adapter_id
            ), tempfile.TemporaryDirectory() as root_text:
                temporary = Path(root_text)
                workspace = temporary / "workspace with spaces 雪"
                workspace.mkdir()
                image_path = temporary / "private prompt.md"
                image_path.write_bytes(self.image_bytes)
                image_path.chmod(0o600)
                observation_path = temporary / "observation.json"
                adapter = self.oracle_adapters[adapter_id]
                argv = self.concrete_argv(scenario, workspace, image_path)
                stdin = (
                    b""
                    if scenario["stdin"] is None
                    else (ROOT / scenario["stdin"]["fixture"]).read_bytes()
                )
                environment = self.fixture_environment(
                    adapter, scenario, observation_path
                )
                returncode, stdout_bytes, stderr_bytes = self.run_probe(
                    argv,
                    workspace=workspace,
                    environment=environment,
                    stdin=stdin,
                    stdin_lifecycle=scenario["stdinLifecycle"],
                )
                self.assertEqual(scenario["expectedExitCode"], returncode)
                self.assertEqual(b"", stderr_bytes)
                stdout = [json.loads(line) for line in stdout_bytes.splitlines()]
                self.assertEqual(scenario["fakeStdout"], stdout)
                observation = load_json(observation_path)
                self.assertEqual(
                    "openprose.adapter-probe-observation/1", observation["schema"]
                )
                self.assertEqual(adapter_id, observation["adapterId"])
                self.assertEqual(argv, observation["argv"])
                self.assertEqual(
                    workspace.resolve(), Path(observation["cwd"]).resolve()
                )
                self.assertEqual(len(stdin), observation["stdin"]["byteLength"])
                self.assertEqual(sha256(stdin), observation["stdin"]["sha256"])
                self.assertEqual(
                    stdin, base64.b64decode(observation["stdin"]["base64"])
                )
                expected_environment_names = set(environment) - {
                    "OPENPROSE_ADAPTER_OBSERVATION_PATH",
                    "OPENPROSE_ADAPTER_EXPECTED_ID",
                }
                self.assertEqual(
                    expected_environment_names, set(observation["environmentNames"])
                )
                self.assertEqual(
                    self.oracle["environmentRules"]["adapterOwnedControls"].get(
                        adapter_id, {}
                    ),
                    observation["adapterControls"],
                )
                self.assertIs(observation["shell"], False)
                self.assertIs(observation["outerPty"], False)
                observation_text = observation_path.read_text("utf-8")
                self.assertNotIn("fixture-secret:", observation_text)
                expected_files = {
                    entry["argument"]: entry for entry in scenario.get("files", [])
                }
                for prompt_file in observation["files"]:
                    if os.name != "nt":
                        self.assertEqual("0600", prompt_file["mode"])
                    payload = base64.b64decode(prompt_file["base64"])
                    if payload == self.image_bytes:
                        expected = expected_files["{{IMAGE_PATH}}"]
                    elif payload == (
                        b"retry:\n  enabled: false\ndisabledProviders:\n  - native\n"
                        b"  - omp-plugins\n  - claude\n  - agent-plugins\n"
                        b"  - claude-plugins\n  - codex\n  - gemini\n  - opencode\n"
                        b"  - cursor\n  - windsurf\n  - vscode\n  - mcp-json\n"
                    ):
                        expected = expected_files["{{RENDERED_CONFIG_PATH}}"]
                    else:
                        self.fail("probe observed an unbound private input file")
                    self.assertEqual(expected["byteLength"], prompt_file["byteLength"])
                    self.assertEqual(expected["sha256"], prompt_file["sha256"])

    def test_rpc_probe_correlates_the_actual_prompt_request_id(self) -> None:
        request_id = "dynamic-invocation-7f6b"
        request = (
            canonical_json(
                {"id": request_id, "type": "prompt", "message": "opaque task"}
            )
            + b"\n"
        )
        returncode, stdout, _ = self.run_probe(
            [str(PROBE), "--mode", "rpc"],
            workspace=ROOT,
            environment={"OPENPROSE_ADAPTER_EXPECTED_ID": "prime/rpc"},
            stdin=request,
            stdin_lifecycle="close-after-terminal-event",
        )
        self.assertEqual(0, returncode)
        first = json.loads(stdout.splitlines()[0])
        self.assertEqual(request_id, first["id"])

    def oracle_rejects_strict_claim(self, oracle, recipes) -> list[str]:
        adapters = {entry["adapterId"]: entry for entry in oracle["adapters"]}
        failures = []
        for adapter_id, recipe in recipes.items():
            if (
                "strict-wrapper-conformant" in recipe["admissionClaims"]
                and adapters[adapter_id]["strictAdmission"]["status"] != "admitted"
            ):
                failures.append(adapter_id)
        return failures

    def test_strict_claim_mutation_is_rejected_by_oracle(self) -> None:
        self.assertEqual(
            [], self.oracle_rejects_strict_claim(self.oracle, self.recipes)
        )
        recipes = deepcopy(self.recipes)
        recipes["claude/print-stream-json"]["admissionClaims"] = [
            "strict-wrapper-conformant"
        ]
        self.assertEqual(
            ["claude/print-stream-json"],
            self.oracle_rejects_strict_claim(self.oracle, recipes),
        )

    def test_schema_extension_blocker_is_explicit(self) -> None:
        proposal = self.oracle["proposedRecipeSchemaExtension"]
        self.assertEqual("applied", proposal["status"])
        self.assertEqual(
            ["image-utf8", "rendered-config-path", "daemon-socket-path"],
            proposal["newArgvValues"],
        )
        text = " ".join(proposal["constraints"])
        for required in (
            "one argv element",
            "NUL",
            "byte limits",
            "IMAGE_TOO_LARGE",
            "mode-0600",
            "digests",
            "removed",
        ):
            self.assertIn(required, text)

    def test_recipe_schema_admits_only_the_closed_delivery_placeholders(self) -> None:
        inline = deepcopy(self.recipes["codex/exec-json"])
        inline["launch"]["argv"].insert(-1, {"value": "image-utf8"})
        self.assert_recipe_valid(inline)

        rendered = deepcopy(self.recipes["claude/print-stream-json"])
        rendered["launch"]["argv"][-2] = {"value": "rendered-config-path"}
        self.assert_recipe_valid(rendered)

        isolated_prime = deepcopy(self.recipes["prime/rpc"])
        self.assertIn({"value": "daemon-socket-path"}, isolated_prime["launch"]["argv"])
        self.assert_recipe_valid(isolated_prime)

        invented = deepcopy(inline)
        invented["launch"]["argv"][-2] = {"value": "semantic-program-summary"}
        errors = list(self.recipe_validator.iter_errors(invented))
        self.assertGreater(len(errors), 0)

    def test_probe_fixture_is_the_only_executable(self) -> None:
        source = Path(__file__).read_text("utf-8")
        tree = ast.parse(source)
        forbidden = {"codex", "claude", "prime-agent", "omp", "oh-omp"}
        literal_launches = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if (
                node.func.attr != "run"
                or not node.args
                or not isinstance(node.args[0], ast.List)
            ):
                continue
            elements = node.args[0].elts
            if (
                elements
                and isinstance(elements[0], ast.Constant)
                and elements[0].value in forbidden
            ):
                literal_launches.append(elements[0].value)
        self.assertEqual([], literal_launches)
        self.assertIn("[sys.executable, *argv]", source)

    def test_adversarial_manifest_names_real_tests(self) -> None:
        manifest = load_json(INVARIANTS_PATH)
        self.assertEqual(
            "openprose.adapter-adversarial-invariants/1", manifest["schema"]
        )
        ids = [check["id"] for check in manifest["checks"]]
        self.assertEqual(len(ids), len(set(ids)))
        for check in manifest["checks"]:
            self.assertTrue(hasattr(self, check["test"]), check)

    def test_adapter_functional_alpha_cases_validate(self) -> None:
        paths = sorted(
            (CLI / "conformance" / "cases" / "adapters").glob(
                "*-functional-alpha.json"
            )
        )
        self.assertEqual(4, len(paths))
        expected_ids = {
            "codex/exec-json": ("codex", "exec-json", "cached-chatgpt-login"),
            "claude/print-stream-json": (
                "claude",
                "print-stream-json",
                "claude-subscription",
            ),
            "prime/rpc": ("prime", "rpc", "openrouter"),
            "omp/rpc": ("omp", "rpc", "openrouter"),
        }
        for path in paths:
            case = load_json(path)
            errors = sorted(
                self.case_validator.iter_errors(case), key=lambda item: list(item.path)
            )
            self.assertEqual([], [f"{path}: {error.message}" for error in errors])
            adapter_id = case["controls"]["installedAdapter"]["adapterId"]
            harness, transport, auth_profile = expected_ids[adapter_id]
            argv = case["invocation"]["argv"]
            self.assertEqual(harness, argv[argv.index("--harness") + 1])
            self.assertEqual(transport, argv[argv.index("--transport") + 1])
            self.assertEqual(auth_profile, argv[argv.index("--auth-profile") + 1])
            self.assertTrue(argv[argv.index("--model") + 1])
            self.assertEqual(0, case["expected"]["exitCode"])
            self.assertIs(case["expected"]["startedHarness"], True)
            self.assertEqual(
                "openprose.runner-result/1", case["expected"]["stdout"]["schema"]
            )
            self.assertEqual(
                adapter_id, case["expected"]["resultMatches"]["adapter"]["id"]
            )
            self.assertEqual(
                "not-applicable",
                case["expected"]["resultMatches"]["semantic"]["status"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
