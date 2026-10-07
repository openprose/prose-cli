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

    def test_source_preloads_the_windows_helper_target(self):
        self.changed("cli-ci.yml", lambda w, j: [
            step.update(run=step["run"].replace("rustup target add x86_64-pc-windows-msvc --toolchain 1.87.0", ""))
            for step in j["steps"] if step.get("name") == "Prepare locked toolchains and dependencies"
        ])

    def test_homebrew_gates_cannot_be_removed_skipped_or_reordered(self):
        for name, label in (("cli-distribution-check.yml", "Rehearse Homebrew against this build's verified native archives"),
                            ("cli-kernel-rc.yml", "Rehearse Homebrew against these exact release archives")):
            index = next(i for i, step in enumerate(self.workflows[name]["jobs"][JOBS[name]]["steps"])
                         if step.get("name") == label)
            self.changed(name, lambda w, j, i=index: j["steps"].pop(i))
            self.changed(name, lambda w, j, i=index: j["steps"][i].update({"if": "false"}))
            self.changed(name, lambda w, j, i=index: j["steps"][i].update(run="echo skipped"))
            self.changed(name, lambda w, j: j["steps"].reverse())
            self.changed(name, lambda w, j: [step["with"].update(path="unrelated")
                         for step in j["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")])

    def test_release_homebrew_custody_cannot_select_another_source_or_version(self):
        name = "cli-kernel-rc.yml"
        index = next(i for i, step in enumerate(self.workflows[name]["jobs"][JOBS[name]]["steps"])
                     if step.get("name") == "Rehearse Homebrew against these exact release archives")
        for key in ("EXPECTED_SOURCE", "RC_VERSION"):
            self.changed(name, lambda w, j, i=index, k=key: j["steps"][i]["env"].update({k: "unreviewed"}))
        for argument in ("--expected-source", "--expected-version", "--kernel-rc"):
            self.changed(name, lambda w, j, i=index, a=argument: j["steps"][i].update(
                         run=j["steps"][i]["run"].replace(a, "--wrong-argument")))

    def test_genuine_installed_upgrade_gates_cannot_be_bypassed(self):
        for name in ("cli-distribution-check.yml", "cli-kernel-rc.yml"):
            steps = self.workflows[name]["jobs"][JOBS[name]]["steps"]
            for label in ("Fetch pinned genuine previous release inputs",
                          "Qualify actual SDK installations and genuine upgrades"):
                index = next(i for i, step in enumerate(steps) if step.get("name") == label)
                self.changed(name, lambda w, j, i=index: j["steps"].pop(i))
                self.changed(name, lambda w, j, i=index: j["steps"][i].update({"if": "false"}))
                self.changed(name, lambda w, j, i=index: j["steps"][i].update(run="echo skipped"))
            installed = next(i for i, step in enumerate(steps)
                             if step.get("name") == "Qualify actual SDK installations and genuine upgrades")
            for key in ("EXPECTED_SOURCE", "RC_VERSION"):
                self.changed(name, lambda w, j, i=installed, k=key:
                             j["steps"][i]["env"].update({k: "unreviewed"}))
            for argument in ("--candidate-root", "--source", "--version", "--previous-release", "--node", "--npm"):
                self.changed(name, lambda w, j, i=installed, a=argument:
                             j["steps"][i].update(run=j["steps"][i]["run"].replace(a, "--wrong-argument")))
            label = ("Rehearse Homebrew against these exact release archives" if name == "cli-kernel-rc.yml"
                     else "Rehearse Homebrew against the production SDK archives")
            homebrew = next(i for i, step in enumerate(steps) if step.get("name") == label)
            self.changed(name, lambda w, j, i=homebrew:
                         j["steps"][i].update(run=j["steps"][i]["run"].replace("--previous-release", "--wrong-argument")))
            retention = next(i for i, step in enumerate(steps)
                             if step.get("uses", "").startswith("actions/upload-artifact@"))
            for path in ("${{ runner.temp }}/kernel-rc/installed-qualification", "${{ runner.temp }}/previous-rc3"):
                self.changed(name, lambda w, j, i=retention, p=path:
                             j["steps"][i]["with"].update(path=j["steps"][i]["with"]["path"].replace(p, "omitted")))

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

    def test_production_sdk_setup_and_runtime_cannot_be_bypassed(self):
        for name in ("cli-distribution-check.yml", "cli-kernel-rc.yml"):
            for label in ("Prepare the separately locked Agents SDK build interpreter",
                          "Test the actual pinned Agents SDK runtime without provider credentials"):
                index = next(i for i, step in enumerate(self.workflows[name]["jobs"][JOBS[name]]["steps"])
                             if step.get("name") == label)
                self.changed(name, lambda w, j, i=index: j["steps"][i].update({"if": "false"}))
                self.changed(name, lambda w, j, i=index: j["steps"][i].update(run="echo skipped"))
            index = next(i for i, step in enumerate(self.workflows[name]["jobs"][JOBS[name]]["steps"])
                         if step.get("name") == "Prepare the separately locked Agents SDK build interpreter")
            for before, after in (("--require-hashes", ""), ("3.10.20", "3.12"),
                                  ("agents-sdk-python", "distribution-python"),
                                  ("requirements-build.txt", "requirements.txt")):
                self.changed(name, lambda w, j, i=index, a=before, b=after: j["steps"][i].update(
                             run=j["steps"][i]["run"].replace(a, b)))

    def test_distribution_production_sdk_custody_is_required(self):
        name = "cli-distribution-check.yml"
        steps = self.workflows[name]["jobs"][JOBS[name]]["steps"]
        for label in ("Build and verify production SDK standalone and npm installations",
                      "Rehearse Homebrew against the production SDK archives"):
            index = next(i for i, step in enumerate(steps) if step.get("name") == label)
            self.changed(name, lambda w, j, i=index: j["steps"].pop(i))
            self.changed(name, lambda w, j, i=index: j["steps"][i].update({"if": "false"}))
            self.changed(name, lambda w, j, i=index: j["steps"][i].update(run="echo skipped"))
            self.changed(name, lambda w, j, i=index: j["steps"][i]["env"].update(RC_VERSION="unreviewed"))
        retention = next(i for i, step in enumerate(steps)
                         if step.get("uses", "").startswith("actions/upload-artifact@"))
        for path in ("agents-sdk", "package", "logs", "build-report.json", "homebrew"):
            self.changed(name, lambda w, j, i=retention, p=path: j["steps"][i]["with"].update(
                         path=j["steps"][i]["with"]["path"].replace("${{ runner.temp }}/kernel-rc/" + p, "omitted")))
        build = next(i for i, step in enumerate(steps)
                     if step.get("name") == "Build and verify production SDK standalone and npm installations")
        self.changed(name, lambda w, j, i=build: j["steps"].insert(0, j["steps"].pop(i)))

    def test_publisher_sdk_custody_cannot_mutate_reviewed_candidates(self):
        name = "cli-publish.yml"
        steps = self.workflows[name]["jobs"][JOBS[name]]["steps"]
        index = next(i for i, step in enumerate(steps)
                     if step.get("name") == "Verify retained production SDK source and sibling custody")
        self.changed(name, lambda w, j, i=index: j["steps"].pop(i))
        self.changed(name, lambda w, j, i=index: j["steps"][i].update({"if": "false"}))
        self.changed(name, lambda w, j, i=index: j["steps"].insert(0, j["steps"].pop(i)))
        self.changed(name, lambda w, j, i=index: j["steps"][i].update(run="echo verified"))
        for script in ("build_agents_sdk.py", "sign_macos.py", "package_local.py", "build_kernel_rc.py"):
            self.changed(name, lambda w, j, p=script: j["steps"].append(
                         {"name": "Mutate candidate", "run": "python3 cli/ci/" + p}))

    def test_admission_pipeline_cannot_hide_failure(self):
        self.changed(
            "cli-ci.yml",
            lambda w, j: next(
                step
                for step in j["steps"]
                if step.get("name")
                == "Admit current source without provider credentials"
            ).pop("shell"),
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


class HomebrewActionAdmissionTest(unittest.TestCase):
    def test_only_the_reviewed_homebrew_setup_pin_is_admitted(self):
        text = (ROOT / ".github/workflows/cli-distribution-check.yml").read_text()
        reviewed = "Homebrew/actions/setup-homebrew@dc7099b3e807f1e2ecc61f3ecabc840eedd5586a"
        self.assertIn(reviewed, text)
        self.assertEqual([], audit_workflow("cli-distribution-check.yml", text))
        for replacement in ("Homebrew/actions/setup-homebrew@main", "Homebrew/actions/setup-homebrew@" + "0" * 40):
            self.assertTrue(audit_workflow("cli-distribution-check.yml", text.replace(reviewed, replacement)))
