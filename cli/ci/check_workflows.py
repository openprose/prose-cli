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
    "Homebrew/actions/setup-homebrew": "dc7099b3e807f1e2ecc61f3ecabc840eedd5586a",
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
            sdk_custody = by_name["Verify retained production SDK source and sibling custody"]
            require(sdk_custody.get("if") is None and sdk_custody.get("env") is None,
                    "SDK custody must be unconditional and credential-free")
            require(sdk_custody.get("run") ==
                    'python3 cli/ci/publication.py verify --plan "$RELEASE_PLAN" --artifacts "$RUNNER_TEMP/prose-publication"',
                    "SDK custody must verify reviewed fetched bytes")
            require(not any(fragment in step.get("run", "") for step in steps
                            for fragment in ("build_agents_sdk.py", "sign_macos.py", "package_local.py", "build_kernel_rc.py")),
                    "publisher must not reconstruct reviewed candidates")
            publish = by_name[
                "Verify required platform trust, sign artifact digests, and publish exact npm bytes"
            ]
            require(
                steps.index(fetch) < steps.index(sdk_custody) < steps.index(publish),
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
            if name in ("cli-distribution-check.yml", "cli-kernel-rc.yml"):
                sdk_prepare = by_name["Prepare the separately locked Agents SDK build interpreter"]
                sdk_test = by_name["Test the actual pinned Agents SDK runtime without provider credentials"]
                require(sdk_prepare.get("if") is None and sdk_test.get("if") is None,
                        "locked SDK preparation and actual runtime tests must be unconditional")
                require(sdk_prepare.get("run") ==
                        'uv venv --python 3.10.20 --seed "$RUNNER_TEMP/agents-sdk-python"\n'
                        '"$RUNNER_TEMP/agents-sdk-python/bin/python3" -m pip install --require-hashes --only-binary=:all: -r harnesses/agents-sdk/requirements-build.txt\n',
                        "SDK must use a separate pinned hash-locked interpreter")
                require(sdk_test.get("run") ==
                        '"$RUNNER_TEMP/agents-sdk-python/bin/python3" -m unittest discover -s harnesses/agents-sdk -p test_run.py',
                        "test actual SDK graph under pinned build Python")
                require(steps.index(python) < steps.index(sdk_prepare) < steps.index(sdk_test),
                        "prepare pinned SDK interpreter before runtime qualification")
            if name == "cli-ci.yml":
                require(
                    "rustup target add x86_64-pc-windows-msvc --toolchain 1.87.0" in prepare["run"],
                    "preload the Windows helper target before offline admission",
                )
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
                    'python3 cli/ci/build_kernel_rc.py --version "$RC_VERSION" --out "$RUNNER_TEMP/kernel-rc" --agents-sdk-python "$RUNNER_TEMP/agents-sdk-python/bin/python3"',
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
        if name == "cli-distribution-check.yml":
            sdk_build = by_name["Build and verify production SDK standalone and npm installations"]
            sdk_homebrew = by_name["Rehearse Homebrew against the production SDK archives"]
            require(sdk_build.get("if") is None and sdk_homebrew.get("if") is None,
                    "production SDK installations must be qualified unconditionally")
            require(sdk_build.get("env") == {"RC_VERSION": "0.15.0-rc.1"}
                    and sdk_build.get("run") ==
                    'python3 cli/ci/build_kernel_rc.py --version "$RC_VERSION" --out "$RUNNER_TEMP/kernel-rc" --agents-sdk-python "$RUNNER_TEMP/agents-sdk-python/bin/python3"',
                    "production SDK must use the native source-bound RC builder")
            require(sdk_homebrew.get("env") == {"RC_VERSION": "0.15.0-rc.1", "EXPECTED_SOURCE": "${{ github.sha }}"}
                    and sdk_homebrew.get("run") ==
                    '"$RUNNER_TEMP/distribution-python/bin/python3" cli/ci/homebrew_rehearsal.py \\\n'
                    '  --kernel-rc "$RUNNER_TEMP/kernel-rc" \\\n'
                    '  --expected-source "$EXPECTED_SOURCE" --expected-version "$RC_VERSION" \\\n'
                    '  --output "$RUNNER_TEMP/kernel-rc/homebrew"\n',
                    "production Homebrew must bind current source and exact SDK archives")
            retention = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
            retained = retention.get("with", {}).get("path", "").splitlines()
            require(all("${{ runner.temp }}/kernel-rc/" + path in retained for path in
                        ("agents-sdk", "package", "logs", "build-report.json", "homebrew")),
                    "retain SDK trio, release manifest, native probes and Homebrew custody")
            setup = next(step for step in steps if step.get("uses", "").startswith("Homebrew/actions/setup-homebrew@"))
            require(steps.index(sdk_test) < steps.index(sdk_build) < steps.index(setup)
                    < steps.index(sdk_homebrew) < steps.index(retention),
                    "freeze/install SDK before exact archive Homebrew checks and retention")
        if name in ("cli-distribution-check.yml", "cli-kernel-rc.yml"):
            kernel_rc = name == "cli-kernel-rc.yml"
            label = ("Rehearse Homebrew against these exact release archives" if kernel_rc
                     else "Rehearse Homebrew against this build's verified native archives")
            homebrew = by_name[label]
            setup = next(step for step in steps if step.get("uses", "").startswith("Homebrew/actions/setup-homebrew@"))
            retention = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
            require(homebrew.get("if") is None and setup.get("if") is None,
                    "Homebrew admission must not be conditional")
            require(steps.index(by_name[required_step]) < steps.index(setup) < steps.index(homebrew) < steps.index(retention),
                    "test already-built Homebrew archives before retaining results")
            fragments = ['"$RUNNER_TEMP/distribution-python/bin/python3" cli/ci/homebrew_rehearsal.py']
            if kernel_rc:
                fragments += ['--kernel-rc "$RUNNER_TEMP/kernel-rc"', '--expected-source "$EXPECTED_SOURCE"',
                              '--expected-version "$RC_VERSION"', '--output "$RUNNER_TEMP/kernel-rc/homebrew"']
                require(homebrew.get("env") == {"RC_VERSION": "${{ inputs.version || '0.15.0-rc.1' }}",
                                               "EXPECTED_SOURCE": "${{ github.sha }}"},
                        "exact release Homebrew admission must bind workflow source/version")
                evidence_path = "${{ runner.temp }}/kernel-rc/homebrew"
            else:
                fragments += ['--rehearsal "$RUNNER_TEMP/cli-rehearsal"', '--output "$RUNNER_TEMP/cli-homebrew-rehearsal"']
                evidence_path = "${{ runner.temp }}/cli-homebrew-rehearsal"
            require(all(fragment in homebrew.get("run", "") for fragment in fragments),
                    "Homebrew admission must use pinned Python and verified archive custody")
            require(evidence_path in retention.get("with", {}).get("path", "").splitlines(),
                    "retain Homebrew custody and cleanup results")
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
