#!/usr/bin/env python3
"""Audit the current source, packaging, candidate and publication workflows."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
    "astral-sh/setup-uv": "d0d8abe699bfb85fec6de9f7adb5ae17292296ff",
    "oven-sh/setup-bun": "3d267786b128fe76c2f16a390aa2448b815359f3",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "sigstore/cosign-installer": "828df1e55de306ba29db814d6057ddae71883cda",
}
JOBS = {
    "cli-ci.yml": "admission",
    "cli-distribution-check.yml": "package-and-install",
    "cli-kernel-rc.yml": "build",
    "cli-publish.yml": "publish",
}
PLATFORMS = {
    "linux-x64": "ubuntu-22.04",
    "linux-arm64": "ubuntu-22.04-arm",
    "darwin-arm64": "macos-15",
    "darwin-x64": "macos-15-intel",
}


class WorkflowLoader(yaml.BaseLoader):
    """Keep GitHub's `on` key intact and reject ambiguous duplicate keys."""


def mapping(loader: WorkflowLoader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"duplicate or non-scalar workflow key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


WorkflowLoader.add_constructor("tag:yaml.org,2002:map", mapping)


def audit_workflow(name: str, text: str) -> list[str]:
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(f"{name}: {message}")

    try:
        workflow = yaml.load(text, Loader=WorkflowLoader)
        if not isinstance(workflow, dict):
            raise ValueError("workflow must be a mapping")
        require(name in JOBS, "unreviewed workflow")
        if name not in JOBS:
            return failures
        publishing = name == "cli-publish.yml"
        events = workflow.get("on", {})
        require(isinstance(events, dict), "triggers must be explicit")
        require(
            set(events)
            == (
                {"workflow_dispatch"}
                if publishing
                else {"pull_request", "push", "workflow_dispatch"}
            ),
            "unexpected or missing triggers",
        )
        if not publishing:
            require(
                events.get("push") == {"branches": ["main"]},
                "source checks must run on main pushes",
            )
            require(
                events.get("pull_request") in ("", {}),
                "all pull requests must be checked without path filters",
            )
        require(
            workflow.get("permissions") == {"contents": "read"},
            "workflow permissions must be read-only",
        )
        jobs = workflow.get("jobs", {})
        require(set(jobs) == {JOBS[name]}, "unexpected job graph")
        job = jobs[JOBS[name]]
        require(
            job.get("continue-on-error", "false") == "false", "job must fail closed"
        )
        require(
            job.get("timeout-minutes", "").isdigit()
            and 0 < int(job["timeout-minutes"]) <= 60,
            "job needs a bounded timeout",
        )
        if publishing:
            require(
                job.get("environment") == "publication",
                "publication environment required",
            )
            require(
                job.get("if") == "github.ref == 'refs/heads/main'",
                "publication restricted to main",
            )
            require(
                job.get("permissions") == {"contents": "write", "id-token": "write"},
                "publisher permissions must be explicit and narrow",
            )
            require(
                workflow.get("concurrency")
                == {"group": "cli-publication", "cancel-in-progress": "false"},
                "publication must be serialized without cancellation",
            )
        else:
            require(
                job.get("permissions", {"contents": "read"}) == {"contents": "read"},
                "build jobs must not receive write permissions",
            )
            require(
                job.get("environment") is None,
                "builds must not enter publication environments",
            )
            strategy = job["strategy"]
            require(
                strategy.get("fail-fast") == "false",
                "retain failures from every platform",
            )
            matrix = strategy["matrix"]
            if name == "cli-kernel-rc.yml":
                require(
                    matrix
                    == {
                        "include": [
                            {"os": os, "platform": target}
                            for target, os in PLATFORMS.items()
                        ]
                    },
                    "candidate matrix must cover four supported native targets",
                )
                require(
                    job.get("if")
                    == "github.event_name == 'pull_request' || github.ref == 'refs/heads/main'",
                    "candidate build restricted to reviewed source",
                )
            else:
                expected = (
                    ["ubuntu-22.04", "macos-15"]
                    if name == "cli-ci.yml"
                    else list(PLATFORMS.values())
                )
                require(matrix == {"os": expected}, "native admission matrix drift")
                require(job.get("if") is None, "admission must not be conditional")
            require(
                job.get("runs-on") == "${{ matrix.os }}",
                "use the declared native runner",
            )
            require(
                "secrets." not in json.dumps(workflow),
                "provider-free builds must not use secrets",
            )
        steps = job["steps"]
        require(isinstance(steps, list) and bool(steps), "job needs explicit steps")
        names = [step.get("name") for step in steps if "name" in step]
        require(len(names) == len(set(names)), "step names must be unique")
        for step in steps:
            require(
                step.get("continue-on-error", "false") == "false",
                "steps must fail closed",
            )
            if "uses" in step:
                action, separator, revision = step["uses"].partition("@")
                require(
                    bool(separator) and ACTION_PINS.get(action) == revision,
                    f"action must use its reviewed immutable pin: {step['uses']}",
                )
                if action == "actions/checkout":
                    require(
                        step.get("with", {}).get("persist-credentials") == "false",
                        "checkout must not persist credentials",
                    )
                    require(
                        set(step.get("with", {})) <= {"persist-credentials"},
                        "checkout must use the workflow's reviewed commit",
                    )
                if action == "actions/setup-node":
                    require(
                        step.get("with", {}).get("node-version") == "24.20.0",
                        "Node pin drift",
                    )
                if action == "oven-sh/setup-bun":
                    require(
                        step.get("with", {}).get("bun-version") == "1.3.5",
                        "Bun pin drift",
                    )
                if action == "astral-sh/setup-uv":
                    require(
                        step.get("with", {}).get("version") == "0.12.15", "uv pin drift"
                    )
        by_name = {step.get("name"): step for step in steps}
        if publishing:
            inputs = events["workflow_dispatch"]["inputs"]
            require(
                inputs["operation"]["options"] == ["publish", "sign-only"],
                "closed publication operations required",
            )
            require(inputs["plan"]["required"] == "true", "reviewed plan is required")
            fetch = by_name["Fetch exact draft artifacts without executing them"]
            publish = by_name[
                "Verify required platform trust, sign artifact digests, and publish exact npm bytes"
            ]
            require(
                steps.index(fetch) < steps.index(publish),
                "fetch exact bytes before publication",
            )
            require(
                fetch.get("if") is None and publish.get("if") is None,
                "publication verification must not be conditional",
            )
            require(
                fetch["run"]
                == 'python3 cli/ci/publication.py fetch --plan "$RELEASE_PLAN" --artifacts "$RUNNER_TEMP/prose-publication"',
                "fetch must use the current publication verifier",
            )
            require(
                'python3 cli/ci/publication.py "$PUBLICATION_OPERATION"'
                in publish["run"],
                "publish through the current verifier",
            )
            bootstrap = publish["env"]["NPM_BOOTSTRAP_TOKEN"]
            require(
                bootstrap
                == "${{ inputs.operation == 'publish' && inputs.bootstrap_platform_packages && secrets.NPM_BOOTSTRAP_TOKEN || '' }}",
                "bootstrap credential must require explicit bootstrap publication",
            )
            require(
                "unset APPLE_NOTARY_KEY_P8" in publish["run"]
                and "trap 'rm -f" in publish["run"],
                "temporary notary key must be removed and excluded from children",
            )
            preflight = by_name["Validate publisher inputs and toolchain"]
            require(
                preflight.get("if") is None
                and "A reviewed plan on main is required" in preflight["run"]
                and "Invalid plan filename" in preflight["run"]
                and "11.19.0" in preflight["run"],
                "plan containment and npm identity guards required",
            )
        else:
            prepare = by_name["Prepare locked toolchains and dependencies"]
            require(
                prepare.get("if") is None,
                "dependency preparation must not be conditional",
            )
            for fragment in [
                "rustup toolchain install 1.87.0",
                "cargo fetch --locked",
                "--require-hashes --only-binary=:all:",
                "bun install --frozen-lockfile --ignore-scripts",
            ]:
                require(
                    fragment in prepare["run"],
                    f"missing locked dependency guard: {fragment}",
                )
            python = by_name["Prepare pinned Python on every architecture"]
            require("uv python install 3.10.20" in python["run"], "Python pin drift")
            if name == "cli-ci.yml":
                require(
                    by_name["Admit current source without provider credentials"].get(
                        "shell"
                    )
                    == "bash",
                    "source admission needs Bash pipefail so tee cannot hide a failure",
                )
            required_step, command = {
                "cli-ci.yml": (
                    "Admit current source without provider credentials",
                    'python3 cli/ci/run_local.py 2>&1 | tee "$RUNNER_TEMP/cli-admission.log"',
                ),
                "cli-distribution-check.yml": (
                    "Build, package and exercise fresh standalone and npm installations",
                    'python3 cli/ci/rehearse_release.py --output "$RUNNER_TEMP/cli-rehearsal" --trials 1',
                ),
                "cli-kernel-rc.yml": (
                    "Build and verify fresh standalone and npm installations",
                    'python3 cli/ci/build_kernel_rc.py --version "$RC_VERSION" --out "$RUNNER_TEMP/kernel-rc"',
                ),
            }[name]
            require(
                by_name[required_step].get("if") is None,
                "required qualification must not be conditional",
            )
            require(
                by_name[required_step]["run"] == command,
                "required qualification command drift",
            )
        require(
            any(
                step.get("uses", "").startswith("actions/upload-artifact@")
                and step.get("if") == "always()"
                for step in steps
            ),
            "retain evidence on success and failure",
        )
    except (
        yaml.YAMLError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        RecursionError,
    ) as error:
        failures.append(f"{name}: malformed or incomplete workflow: {error}")
    return failures


def audit_repository(root: Path = ROOT) -> list[str]:
    directory = root / ".github" / "workflows"
    actual = {path.name for path in directory.glob("*.y*ml")}
    failures = []
    if actual != set(JOBS):
        failures.append(
            f"workflow inventory drift: expected {sorted(JOBS)}, found {sorted(actual)}"
        )
    for name in JOBS:
        try:
            text = (directory / name).read_text("utf-8")
        except OSError as error:
            failures.append(f"{name}: unavailable workflow: {error}")
        else:
            failures.extend(audit_workflow(name, text))
    return failures


def main() -> int:
    failures = audit_repository()
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print(
            "PASS: current source, distribution, candidate and publication workflow policy"
        )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
