from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import yaml

from check_workflows import (
    ACTION_PINS,
    JOBS,
    ROOT,
    WorkflowLoader,
    audit_repository,
    audit_workflow,
)


class CurrentWorkflowPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = {
            name: (ROOT / ".github/workflows" / name).read_text("utf-8")
            for name in JOBS
        }
        cls.workflows = {
            name: yaml.load(text, Loader=WorkflowLoader)
            for name, text in cls.texts.items()
        }

    def changed(self, name, mutate):
        workflow = deepcopy(self.workflows[name])
        mutate(workflow, workflow["jobs"][JOBS[name]])
        self.assertTrue(audit_workflow(name, yaml.safe_dump(workflow)), name)

    def test_current_workflows_are_admitted(self):
        self.assertEqual([], audit_repository())
        for name, text in self.texts.items():
            with self.subTest(workflow=name):
                self.assertEqual([], audit_workflow(name, text))

    def test_missing_and_retired_workflows_fail_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github/workflows"
            directory.mkdir(parents=True)
            for name, text in self.texts.items():
                (directory / name).write_text(text)
            self.assertEqual([], audit_repository(root))
            (directory / "openprose-cli-alpha-release.yml").write_text("name: retired")
            self.assertTrue(audit_repository(root))
            (directory / "openprose-cli-alpha-release.yml").unlink()
            (directory / "cli-ci.yml").unlink()
            self.assertTrue(audit_repository(root))

    def test_duplicate_keys_and_invalid_shapes_fail_closed(self):
        for text in (
            "on: {}\non: {}\n",
            "[]",
            "jobs: []",
            "on: [pull_request]",
            "jobs: &a {admission: *a}",
        ):
            with self.subTest(text=text):
                self.assertTrue(audit_workflow("cli-ci.yml", text))

    def test_action_pins_and_checkout_custody(self):
        for name, workflow in self.workflows.items():
            job = workflow["jobs"][JOBS[name]]
            for index, step in enumerate(job["steps"]):
                if "uses" not in step:
                    continue
                with self.subTest(workflow=name, action=step["uses"]):
                    self.changed(
                        name,
                        lambda w, j, i=index: j["steps"][i].update(
                            uses=j["steps"][i]["uses"].split("@")[0] + "@main"
                        ),
                    )
            checkout_index = next(
                i
                for i, step in enumerate(job["steps"])
                if step.get("uses", "").startswith("actions/checkout@")
            )
            self.changed(
                name,
                lambda w, j, i=checkout_index: j["steps"][i]["with"].update(
                    {"persist-credentials": "true"}
                ),
            )
            self.changed(
                name,
                lambda w, j, i=checkout_index: j["steps"][i]["with"].update(
                    {"ref": "unreviewed"}
                ),
            )

    def test_floating_toolchain_versions_fail(self):
        fields = {
            "actions/setup-node": ("node-version", "latest"),
            "astral-sh/setup-uv": ("version", "latest"),
            "oven-sh/setup-bun": ("bun-version", "latest"),
        }
        for name, workflow in self.workflows.items():
            for index, step in enumerate(workflow["jobs"][JOBS[name]]["steps"]):
                action = step.get("uses", "").split("@")[0]
                if action in fields:
                    field, value = fields[action]
                    with self.subTest(workflow=name, tool=action):
                        self.changed(
                            name,
                            lambda w, j, i=index, f=field, v=value: j["steps"][i][
                                "with"
                            ].update({f: v}),
                        )

    def test_checks_run_on_main_and_every_pr(self):
        for name in JOBS:
            if name == "cli-publish.yml":
                continue
            with self.subTest(workflow=name):
                self.changed(name, lambda w, j: w["on"].pop("push"))
                self.changed(
                    name, lambda w, j: w["on"]["push"].update(branches=["feature"])
                )
                self.changed(
                    name,
                    lambda w, j: w["on"].update(pull_request={"paths": ["cli/**"]}),
                )
                self.changed(name, lambda w, j: w["on"].update(pull_request_target=""))

    def test_read_only_builds_cannot_publish_or_load_secrets(self):
        for name in JOBS:
            if name == "cli-publish.yml":
                continue
            with self.subTest(workflow=name):
                self.changed(
                    name, lambda w, j: w.update(permissions={"contents": "write"})
                )
                self.changed(
                    name,
                    lambda w, j: j.update(
                        permissions={"contents": "read", "id-token": "write"}
                    ),
                )
                self.changed(name, lambda w, j: j.update(environment="publication"))
                self.changed(
                    name,
                    lambda w, j: j.update(
                        env={"OPENAI_API_KEY": "${{ secrets.OPENAI_API_KEY }}"}
                    ),
                )

    def test_failures_cannot_be_ignored_and_deadlines_are_bounded(self):
        for name in JOBS:
            with self.subTest(workflow=name):
                self.changed(name, lambda w, j: j.update({"continue-on-error": "true"}))
                self.changed(
                    name,
                    lambda w, j: j["steps"][0].update({"continue-on-error": "true"}),
                )
                for timeout in ("0", "61", "${{ inputs.timeout }}"):
                    self.changed(
                        name, lambda w, j, t=timeout: j.update({"timeout-minutes": t})
                    )

    def test_native_platform_coverage_cannot_shrink(self):
        for name in JOBS:
            if name == "cli-publish.yml":
                continue
            self.changed(name, lambda w, j: j["strategy"].update({"fail-fast": "true"}))
            self.changed(
                name,
                lambda w, j: j["strategy"].update(matrix={"os": ["ubuntu-latest"]}),
            )
            self.changed(name, lambda w, j: j.update({"runs-on": "ubuntu-latest"}))

    def test_locked_dependency_guards_and_required_commands(self):
        for name in JOBS:
            if name == "cli-publish.yml":
                continue
            steps = self.workflows[name]["jobs"][JOBS[name]]["steps"]
            prepare = next(
                i
                for i, s in enumerate(steps)
                if s.get("name") == "Prepare locked toolchains and dependencies"
            )
            for old, new in [
                ("--require-hashes", ""),
                ("--frozen-lockfile", ""),
                ("--ignore-scripts", ""),
                ("1.87.0", "stable"),
            ]:
                self.changed(
                    name,
                    lambda w, j, i=prepare, a=old, b=new: j["steps"][i].update(
                        run=j["steps"][i]["run"].replace(a, b)
                    ),
                )
            self.changed(
                name, lambda w, j, i=prepare: j["steps"][i].update({"if": "false"})
            )
            required = {
                "cli-ci.yml": "Admit current source without provider credentials",
                "cli-distribution-check.yml": "Build, package and exercise fresh standalone and npm installations",
                "cli-kernel-rc.yml": "Build and verify fresh standalone and npm installations",
            }[name]
            index = next(i for i, s in enumerate(steps) if s.get("name") == required)
            self.changed(
                name, lambda w, j, i=index: j["steps"][i].update(run="echo skipped")
            )
            self.changed(
                name, lambda w, j, i=index: j["steps"][i].update({"if": "false"})
            )

    def test_diagnostics_survive_failures(self):
        for name in JOBS:
            self.changed(
                name,
                lambda w, j: [
                    s.pop("if", None)
                    for s in j["steps"]
                    if s.get("uses", "").startswith("actions/upload-artifact@")
                ],
            )

    def test_publication_requires_manual_main_and_protected_environment(self):
        name = "cli-publish.yml"
        self.changed(name, lambda w, j: w["on"].update(push=""))
        self.changed(
            name, lambda w, j: j.update({"if": "github.ref == 'refs/heads/feature'"})
        )
        self.changed(name, lambda w, j: j.pop("environment"))
        self.changed(name, lambda w, j: j.update(permissions={"contents": "write"}))
        self.changed(
            name, lambda w, j: w["concurrency"].update({"cancel-in-progress": "true"})
        )
        self.changed(
            name,
            lambda w, j: w["on"]["workflow_dispatch"]["inputs"]["operation"].update(
                options=["publish", "unsafe"]
            ),
        )

    def test_publication_verification_and_bootstrap_are_mandatory(self):
        name = "cli-publish.yml"
        steps = self.workflows[name]["jobs"]["publish"]["steps"]
        fetch = next(
            i
            for i, s in enumerate(steps)
            if s.get("name") == "Fetch exact draft artifacts without executing them"
        )
        publish = next(
            i
            for i, s in enumerate(steps)
            if s.get("name")
            == "Verify required platform trust, sign artifact digests, and publish exact npm bytes"
        )
        self.changed(name, lambda w, j: j["steps"][fetch].update(run="echo fetched"))
        self.changed(name, lambda w, j: j["steps"][fetch].update({"if": "false"}))
        self.changed(
            name,
            lambda w, j: j["steps"][publish].update(run="npm publish --access public"),
        )
        self.changed(
            name,
            lambda w, j: j["steps"][publish]["env"].update(
                NPM_BOOTSTRAP_TOKEN="${{ secrets.NPM_BOOTSTRAP_TOKEN }}"
            ),
        )
        self.changed(
            name,
            lambda w, j: j["steps"][publish].update(
                run=j["steps"][publish]["run"].replace("unset APPLE_NOTARY_KEY_P8", "")
            ),
        )
        self.changed(name, lambda w, j: j["steps"].reverse())


if __name__ == "__main__":
    unittest.main()
