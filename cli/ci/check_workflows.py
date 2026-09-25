#!/usr/bin/env python3
"""Statically enforce the fail-closed OpenProse CLI workflow policy."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-draft-release.yml"
ALPHA_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-alpha-release.yml"
PROMOTION_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-alpha-promote.yml"
POST_PUBLIC_WORKFLOW = (
    ROOT / ".github" / "workflows" / "openprose-cli-alpha-post-public.yml"
)
DRAFT_HELPER = ROOT / "cli" / "ci" / "create_draft_release.py"
RELEASE_NOTES_RENDERER = ROOT / "cli" / "ci" / "render_release_notes.py"

ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "a309ff8b426b58ec0e2a45f0f869d46889d02405",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "oven-sh/setup-bun": "3d267786b128fe76c2f16a390aa2448b815359f3",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
}
TARGET_RUNNERS = {
    "linux-x64": "ubuntu-22.04",
    "linux-arm64": "ubuntu-22.04-arm",
    "darwin-arm": "macos-15",
    "darwin-x64": "macos-15-intel",
    "win-x64": "windows-2025",
}
POST_PUBLIC_TARGET_RUNNERS = {
    "linux-x64": "ubuntu-22.04",
    "linux-arm64": "ubuntu-22.04-arm",
    "darwin-arm": "macos-15",
    "darwin-x64": "macos-15-intel",
}
RELEASE_GRAPH = {
    "build-native": "preflight",
    "verify-native": "build-native",
    "profile-admission": "verify-native",
    "package-native": "profile-admission",
    "admit-release-packages": "package-native",
    "assemble": "[package-native, admit-release-packages]",
    "draft": "assemble",
}
ASSEMBLY_EVIDENCE_NAMES = (
    "release-manifest.json",
    "sbom.cdx.json",
    "provenance.json",
    "dependency-evidence.json",
    "SHA256SUMS",
)
ASSEMBLY_TARGET_PLATFORMS = {
    "linux-x64": "linux-x64-gnu",
    "linux-arm64": "linux-arm64-gnu",
    "darwin-arm": "darwin-arm64",
    "darwin-x64": "darwin-x64",
    "win-x64": "win32-x64",
}
PROMOTION_NPM_OPERATIONS = ("bootstrap", "stage-platforms", "stage-meta")
PROMOTION_NPM_TARBALL_URL = "https://registry.npmjs.org/npm/-/npm-11.15.0.tgz"
PROMOTION_NPM_TARBALL_PATH = (
    "${{ runner.temp }}/openprose-alpha-promotion-tool/npm-11.15.0.tgz"
)
PROMOTION_NPM_DOWNLOAD_STEP = """\
      - name: Download the exact staging-capable npm client without credentials
        run: |
          set -eu
          tool_root="$RUNNER_TEMP/openprose-alpha-promotion-tool"
          umask 077
          mkdir "$tool_root"
          env -i PATH=/usr/bin:/bin /usr/bin/curl --disable \\
            --fail --silent --show-error \\
            --proto '=https' --tlsv1.2 \\
            --max-redirs 0 --connect-timeout 15 --max-time 60 \\
            --max-filesize 2901197 \\
            --output "$tool_root/npm-11.15.0.tgz" \\
            https://registry.npmjs.org/npm/-/npm-11.15.0.tgz
          chmod 0400 "$tool_root/npm-11.15.0.tgz"
"""
PROMOTION_GH_RESOLVER_STEP = """\
      - name: Resolve the externally provisioned GitHub CLI verifier
        id: github-cli
        shell: bash
        run: |
          set -euo pipefail
          GH_TOOL="$(python - /usr/bin/gh <<'PY'
          from pathlib import Path
          import sys

          print(Path(sys.argv[1]).resolve(strict=True))
          PY
          )"
          [[ "$GH_TOOL" =~ ^/[A-Za-z0-9._+/@:-]+$ ]]
          printf 'path=%s\\n' "$GH_TOOL" >>"$GITHUB_OUTPUT"
"""


def require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def job_blocks(workflow: str) -> dict[str, str]:
    jobs_match = re.search(r"(?m)^jobs:\s*$", workflow)
    if jobs_match is None:
        return {}
    body = workflow[jobs_match.end() :]
    matches = list(re.finditer(r"(?m)^  ([a-z][a-z0-9-]*):\s*$", body))
    return {
        match.group(1): body[
            match.start() : matches[index + 1].start()
            if index + 1 < len(matches)
            else len(body)
        ]
        for index, match in enumerate(matches)
    }


def named_step_blocks(job: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^      - name: ([^\n]+)\s*$", job))
    return {
        match.group(1): job[
            match.start() : matches[index + 1].start()
            if index + 1 < len(matches)
            else len(job)
        ]
        for index, match in enumerate(matches)
    }


def job_permission_values(job: str) -> dict[str, str] | None:
    """Return one literal job permission map, or None for ambiguous syntax."""

    matches = list(re.finditer(r"(?m)^    permissions:\s*$", job))
    if len(matches) != 1:
        return None
    values: dict[str, str] = {}
    for line in job[matches[0].end() :].splitlines():
        if not line.strip():
            continue
        indentation = len(line) - len(line.lstrip())
        if indentation <= 4:
            break
        match = re.fullmatch(r"      ([a-z][a-z-]*): (read|write|none)", line)
        if match is None or match.group(1) in values:
            return None
        values[match.group(1)] = match.group(2)
    return values


def run_scripts(workflow: str) -> tuple[str, ...]:
    lines = workflow.splitlines()
    scripts: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)run:\s*(.*)$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        body = [match.group(2)]
        cursor = index + 1
        while cursor < len(lines):
            following = lines[cursor]
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
            cursor += 1
        scripts.append("\n".join(body))
    return tuple(scripts)


def inline_python_trees(workflow_block: str) -> tuple[ast.AST, ...]:
    """Parse literal Python heredocs so policy checks bind executable structure."""

    trees: list[ast.AST] = []
    for script in run_scripts(workflow_block):
        lines = script.splitlines()
        body: list[str] | None = None
        for line in lines:
            if line.strip() == "python - <<'PY'":
                body = []
                continue
            if body is not None and line.strip() == "PY":
                try:
                    trees.append(ast.parse(textwrap.dedent("\n".join(body))))
                except SyntaxError:
                    pass
                body = None
                continue
            if body is not None:
                body.append(line)
    return tuple(trees)


def contains_python_statement(workflow_block: str, source: str) -> bool:
    expected = ast.parse(source).body[0]
    expected_dump = ast.dump(expected, include_attributes=False)
    return any(
        ast.dump(node, include_attributes=False) == expected_dump
        for tree in inline_python_trees(workflow_block)
        for node in ast.walk(tree)
    )


def profile_binds_protected_dependency(workflow_block: str) -> bool:
    dependency_assignment = (
        'dependency_record = {"path": "protected-dependency-evidence.json", '
        '"byteLength": len(dependency_bytes), '
        '"sha256": hashlib.sha256(dependency_bytes).hexdigest(), '
        '"releasePolicyPassed": False}'
    )
    if not contains_python_statement(workflow_block, dependency_assignment):
        return False
    for tree in inline_python_trees(workflow_block):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "admission"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Dict)
            ):
                for key, value in zip(node.value.keys, node.value.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "dependencyEvidence"
                        and isinstance(value, ast.Name)
                        and value.id == "dependency_record"
                    ):
                        return True
    return False


def profile_generates_protected_dependency(workflow_block: str) -> bool:
    return (
        re.search(
            r"(?m)^\s+python control/cli/ci/dependency_evidence\.py report\s*$"
            r"\n^\s+--root candidate\s*$"
            r"\n^\s+> protected-dependency-evidence\.json\s*$",
            workflow_block,
        )
        is not None
    )


def assembly_copies_exact_dependency_evidence(workflow_block: str) -> bool:
    if not contains_python_statement(
        workflow_block,
        'EVIDENCE_NAMES = ("release-manifest.json", "sbom.cdx.json", '
        '"provenance.json", "dependency-evidence.json")',
    ):
        return False
    expected_iter = ast.parse("TARGET_PLATFORMS.items()", mode="eval").body
    expected_iter_dump = ast.dump(expected_iter, include_attributes=False)
    expected_name_iter = ast.parse("sorted(expected_names)", mode="eval").body
    expected_name_iter_dump = ast.dump(expected_name_iter, include_attributes=False)
    expected_branch = ast.parse(
        "if name in EVIDENCE_NAMES:\n"
        '    place_bytes(f"{target}-{name}", candidate_bytes)\n'
        "else:\n"
        "    place_bytes(name, candidate_bytes)"
    ).body[0]
    expected_branch_dump = ast.dump(expected_branch, include_attributes=False)
    expected_checksum = ast.parse(
        'place_bytes(f"{target}-SHA256SUMS", checksum_bytes, MAX_CHECKSUM_BYTES)'
    ).body[0]
    expected_checksum_dump = ast.dump(expected_checksum, include_attributes=False)
    for tree in inline_python_trees(workflow_block):
        package_loops = [
            node
            for node in getattr(tree, "body", [])
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Tuple)
            and tuple(
                item.id for item in node.target.elts if isinstance(item, ast.Name)
            )
            == ("target", "platform_identifier")
            and ast.dump(node.iter, include_attributes=False) == expected_iter_dump
        ]
        for package_loop in package_loops:
            checksum_is_copied = any(
                ast.dump(node, include_attributes=False) == expected_checksum_dump
                for node in package_loop.body
            )
            for node in package_loop.body:
                if (
                    not isinstance(node, ast.For)
                    or not isinstance(node.target, ast.Name)
                    or node.target.id != "name"
                    or ast.dump(node.iter, include_attributes=False)
                    != expected_name_iter_dump
                ):
                    continue
                if checksum_is_copied and any(
                    ast.dump(statement, include_attributes=False)
                    == expected_branch_dump
                    for statement in node.body
                ):
                    return True
    return False


def assembly_has_closed_bounded_checksum_parser(workflow_block: str) -> bool:
    """Bind the protected assembler to a closed, bounded, no-follow byte parser."""

    target_source = "TARGET_PLATFORMS = " + repr(ASSEMBLY_TARGET_PLATFORMS)
    required_statements = (
        "MAX_CHECKSUM_BYTES = 16 * 1024",
        "MAX_CHECKSUM_LINES = 8",
        "MAX_CHECKSUM_LINE_BYTES = 512",
        "MAX_FILE_BYTES = 512 * 1024 * 1024",
        "MAX_PACKAGE_BYTES = 256 * 1024 * 1024",
        "MAX_PACKAGE_SET_BYTES = 1024 * 1024 * 1024",
        "MAX_EVIDENCE_BYTES = 16 * 1024 * 1024",
        "MAX_DIRECTORY_ENTRIES = 64",
        target_source,
        'PORTABLE_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")',
        'CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)")',
        'require(name not in {".", ".."} and "/" not in name and "\\\\" not in name, f"unsafe filename: {name!r}")',
        'require(not stat.S_ISLNK(linked.st_mode) and stat.S_ISREG(linked.st_mode), f"not a non-symlink regular file: {path}")',
        "flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW",
        'require(0 < linked.st_size <= maximum, f"file size is outside its bound: {path}")',
        'require(len(encoded) <= maximum, f"file exceeded its byte bound: {path}")',
        'require(identity(opened) == identity(settled), f"file changed while reading: {path}")',
        'require(destination.parent == output and destination.name == name, f"destination escaped assembly: {name!r}")',
        'require(read_regular(destination, maximum) == encoded, f"conflicting duplicate assembly file: {name}")',
        'require(not destination.exists() and not destination.is_symlink(), f"unexpected assembly destination: {name}")',
        'with destination.open("xb") as opened:\n    opened.write(encoded)',
        'require(read_regular(destination, maximum) == encoded, f"assembled destination differs: {name}")',
        "directory_names(packages_root, expected_package_directories, len(TARGET_PLATFORMS))",
        'directory_names(package, expected_names | {"SHA256SUMS"}, MAX_CHECKSUM_LINES + 1)',
        'require(package_snapshot_bytes <= MAX_PACKAGE_BYTES, f"package target exceeds its aggregate byte bound: {target}")',
        'require(package_set_bytes <= MAX_PACKAGE_SET_BYTES, "package set exceeds its aggregate byte bound")',
        'checksum_bytes = read_regular(package / "SHA256SUMS", MAX_CHECKSUM_BYTES)',
        'require(b"\\r" not in checksum_bytes and checksum_bytes.endswith(b"\\n"), f"noncanonical SHA256SUMS: {target}")',
        'require(0 < len(lines) <= MAX_CHECKSUM_LINES, f"SHA256SUMS line count is outside its bound: {target}")',
        'require(len(line.encode("ascii")) <= MAX_CHECKSUM_LINE_BYTES, f"SHA256SUMS line is too long: {target}")',
        'require(name not in records, f"duplicate SHA256SUMS filename: {name}")',
        'require(set(records) == expected_names, f"SHA256SUMS filename set is not exact: {target}")',
        'require(list(records) == sorted(expected_names), f"SHA256SUMS is not canonically ordered: {target}")',
        "candidate_bytes = read_regular(package / name, MAX_FILE_BYTES)",
        'require(observed_package_bytes <= MAX_PACKAGE_BYTES, f"read package target exceeds its aggregate byte bound: {target}")',
        'require(observed_package_bytes == package_snapshot_bytes, f"package target changed after aggregate custody: {target}")',
        'require(hashlib.sha256(candidate_bytes).hexdigest() == records[name], f"SHA256 mismatch: {target}/{name}")',
        "directory_names(output, assembled_names, MAX_DIRECTORY_ENTRIES)",
        'require(len(checksum) <= MAX_CHECKSUM_BYTES, "assembly SHA256SUMS exceeds its byte bound")',
    )
    if not all(
        contains_python_statement(workflow_block, statement)
        for statement in required_statements
    ):
        return False
    expected_names = ast.parse(
        "expected_names = {"
        "f'openprose-prose-cli-rust-{version}-{platform_identifier}.tar.gz',"
        "f'openprose-prose-cli-bun-{version}-{platform_identifier}.tar.gz',"
        "f'openprose-prose-cli-{version}.tgz',"
        "f'openprose-prose-cli-{platform_identifier}-{version}.tgz',"
        "*EVIDENCE_NAMES}"
    ).body[0]
    expected_names_dump = ast.dump(expected_names, include_attributes=False)
    trees = inline_python_trees(workflow_block)
    return any(
        ast.dump(node, include_attributes=False) == expected_names_dump
        for tree in trees
        for node in ast.walk(tree)
    ) and not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_bytes", "read_text"}
        for tree in trees
        for node in ast.walk(tree)
    )


def workflow_step_blocks(workflow_block: str) -> tuple[str, ...]:
    matches = list(re.finditer(r"(?m)^      -(?:\s|$)", workflow_block))
    return tuple(
        workflow_block[
            match.start() : matches[index + 1].start()
            if index + 1 < len(matches)
            else len(workflow_block)
        ]
        for index, match in enumerate(matches)
    )


def step_uses(step: str) -> str | None:
    match = re.search(r"(?m)^(?:      -\s*|        )uses:\s*([^\s#]+)", step)
    return match.group(1) if match is not None else None


def step_with_values(step: str) -> dict[str, str]:
    with_match = re.search(r"(?m)^        with:\s*$", step)
    if with_match is None:
        return {}
    values: dict[str, str] = {}
    lines = step[with_match.end() :].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line and len(line) - len(line.lstrip()) <= 8:
            break
        match = re.match(r"^          ([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match is None:
            index += 1
            continue
        key, value = match.groups()
        if value in {"|", ">", "|-", ">-"}:
            nested: list[str] = []
            index += 1
            while index < len(lines):
                nested_line = lines[index]
                if nested_line and len(nested_line) - len(nested_line.lstrip()) <= 10:
                    break
                nested.append(nested_line.strip())
                index += 1
            values[key] = value + "\n" + "\n".join(nested)
            continue
        values[key] = value.strip()
        index += 1
    return values


def step_env_values(step: str) -> dict[str, str] | None:
    """Return one literal step environment map, or None for ambiguous syntax."""

    matches = list(re.finditer(r"(?m)^        env:\s*$", step))
    if len(matches) != 1:
        return None
    values: dict[str, str] = {}
    for line in step[matches[0].end() :].splitlines():
        if not line.strip():
            continue
        indentation = len(line) - len(line.lstrip())
        if indentation <= 8:
            break
        match = re.fullmatch(r"          ([A-Z][A-Z0-9_]*): (.+)", line)
        if match is None or match.group(1) in values:
            return None
        values[match.group(1)] = match.group(2)
    return values


def artifact_action_steps(
    workflow_block: str, action: str
) -> tuple[tuple[str, str], ...]:
    transfers: list[tuple[str, str]] = []
    for step in workflow_step_blocks(workflow_block):
        reference = step_uses(step)
        if reference is None or reference.split("@", 1)[0] != action:
            continue
        values = step_with_values(step)
        transfers.append((values.get("name", ""), values.get("path", "")))
    return tuple(transfers)


def has_exact_artifact_download(workflow_block: str, name: str, path: str) -> bool:
    return (name, path) in artifact_action_steps(
        workflow_block, "actions/download-artifact"
    )


def release_package_admission_custody_is_closed(blocks: dict[str, str]) -> bool:
    """Bind admission to control code, original packages, and report-only output."""

    admission = blocks.get("admit-release-packages", "")
    assemble = blocks.get("assemble", "")
    matrix_target = "${{ matrix.target }}"
    expected_admission_downloads = (
        (f"package-{matrix_target}", "packages"),
        ("profile-admission", "protected"),
    )
    expected_admission_uploads = (
        (f"package-admission-{matrix_target}", "release-package-admission.json"),
        (
            f"package-admission-failure-{matrix_target}",
            "release-package-admission-failure.json",
        ),
    )
    if (
        artifact_action_steps(admission, "actions/download-artifact")
        != expected_admission_downloads
    ):
        return False
    if (
        artifact_action_steps(admission, "actions/upload-artifact")
        != expected_admission_uploads
    ):
        return False
    required_admission_fragments = (
        "ref: ${{ github.sha }}",
        "path: control",
        "persist-credentials: false",
        "TARGET_ID: ${{ matrix.target }}",
        "VERSION_INPUT: ${{ inputs.version }}",
        "SOURCE_SHA: ${{ inputs.source_sha }}",
        "CONTROL_SHA: ${{ github.sha }}",
        "WORKFLOW_RUN_ID: ${{ github.run_id }}",
        "WORKFLOW_RUN_ATTEMPT: ${{ github.run_attempt }}",
        "python control/cli/ci/release_package_admission.py",
        "--packages packages",
        "--work-root admission-work",
        "--out release-package-admission.json",
        '--target-id "$TARGET_ID"',
        '--version "$VERSION_INPUT"',
        '--source-sha "$SOURCE_SHA"',
        '--control-sha "$CONTROL_SHA"',
        '--workflow-run-id "$WORKFLOW_RUN_ID"',
        '--workflow-run-attempt "$WORKFLOW_RUN_ATTEMPT"',
        "--profile-preflight protected/profile-preflight.json",
        '--native-verification "protected/verified/native-verification-$TARGET_ID/verification.json"',
        "--timeout-seconds 10",
        "--budget-seconds 300",
        "2> release-package-admission-failure.json",
        'admission_status="$?"',
        'if [[ "$admission_status" -ne 0 ]]; then',
        'report["schema"] == "openprose.release-package-admission-error/1"',
        'set(report) == {"schema", "code", "message"}',
        "cat release-package-admission-failure.json >&2",
        'exit "$admission_status"',
        "test ! -s release-package-admission-failure.json",
        "if: failure()",
        f"name: package-admission-failure-{matrix_target}",
        "path: release-package-admission-failure.json",
        "if-no-files-found: error",
    )
    if not all(fragment in admission for fragment in required_admission_fragments):
        return False
    expected_package_downloads = {
        (f"package-{target}", f"packages/package-{target}") for target in TARGET_RUNNERS
    }
    expected_report_downloads = {
        (
            f"package-admission-{target}",
            f"package-admissions/package-admission-{target}",
        )
        for target in TARGET_RUNNERS
    }
    expected_assemble_downloads = (
        expected_package_downloads
        | expected_report_downloads
        | {
            ("", "native-builds"),
            ("profile-admission", "protected"),
        }
    )
    assemble_downloads = artifact_action_steps(assemble, "actions/download-artifact")
    if (
        len(assemble_downloads) != len(expected_assemble_downloads)
        or set(assemble_downloads) != expected_assemble_downloads
    ):
        return False
    if "pattern: package-*" in assemble or "merge-multiple: true" in assemble:
        return False
    required_assembly_fragments = (
        "ref: ${{ github.sha }}",
        "path: control",
        'package_files = [{"path": "SHA256SUMS", "byteLength": len(checksum_bytes), "sha256": hashlib.sha256(checksum_bytes).hexdigest()}]',
        'package_files.append({"path": name, "byteLength": len(candidate_bytes), "sha256": records[name]})',
        'expected_admission_directories = {f"package-admission-{target}" for target in TARGET_PLATFORMS}',
        'directory_names(admission_dir, {"release-package-admission.json"}, 1)',
        'admission_bytes = read_regular(admission_dir / "release-package-admission.json", MAX_EVIDENCE_BYTES)',
        'corpus_bytes = read_regular(pathlib.Path("control/cli/conformance/release-package/invariants.v1.json"), MAX_EVIDENCE_BYTES)',
        'require(corpus["surfaces"] == [{"id": "direct-rust", "runner": "rust"}, {"id": "direct-bun", "runner": "bun"}, {"id": "npm-launcher", "runner": "bun"}]',
        'require(corpus["claims"] == {"fullPhase7Corpus": False, "languageSemantics": False, "programPortability": False, "providerCalls": "none"}',
        'require(set(admission) == {"schema", "status", "targetId", "version", "sourceSha", "controlSha", "workflowRun", "authorityInputs", "corpus", "package", "installations", "executionToolchain", "surfaces", "cases", "claims"}',
        'require(admission["schema"] == "openprose.release-package-admission/3"',
        'require(admission["targetId"] == target and admission["version"] == version',
        'admission["sourceSha"] == os.environ["SOURCE_SHA"] and admission["controlSha"] == os.environ["CONTROL_SHA"]',
        "CONTROL_SHA: ${{ github.sha }}",
        'admission["workflowRun"] == {"id": int(os.environ["WORKFLOW_RUN_ID"]), "attempt": int(os.environ["WORKFLOW_RUN_ATTEMPT"])}',
        '"profilePreflight": {"byteLength": len(preflight_bytes), "sha256": hashlib.sha256(preflight_bytes).hexdigest()}',
        '"nativeVerification": {"byteLength": len(verification_bytes), "sha256": hashlib.sha256(verification_bytes).hexdigest()}',
        'require(admission["corpus"] == {"byteLength": len(corpus_bytes), "sha256": hashlib.sha256(corpus_bytes).hexdigest()}',
        'require(set(package_report) == {"platform", "mode", "sha256Sums", "releaseManifest", "files", "image", "buildProfiles", "nativeLineage"}',
        '"files": package_custody[target]["files"]',
        'expected_status = "blocked-before-execution" if target == "win-x64" else "passed-provider-free-release-invariants"',
        'expected_execution = "blocked-before-execution" if target == "win-x64" else "performed-posix-invariants"',
        'require(admission["status"] == expected_status and admission["claims"] == expected_claims',
        'require(isinstance(toolchain, dict) and set(toolchain) == {"authority", "node", "npm"}',
        'require(toolchain == {"authority": "not-observed-no-candidate-execution", "node": None, "npm": None}',
        'require(toolchain["authority"] == "reporter-observed-and-finally-reauthenticated-executable-bytes"',
        'set(tool) == {"command", "resolvedPath", "sha256"}',
        'tool["command"] == tool["resolvedPath"] and pathlib.PurePosixPath(tool["command"]).is_absolute()',
        're.fullmatch(r"[0-9a-f]{64}", tool["sha256"]) is not None',
        'require(admission["installations"] == [] and admission["cases"] == [], "Windows package admission must block before candidate execution")',
        'require(case["id"] == expected_case["id"] and case["argv"] == expected_case["argv"]',
        'require(isinstance(observations, list) and [observation.get("surface") if isinstance(observation, dict) else None for observation in observations] == ["direct-rust", "direct-bun", "npm-launcher"]',
        'require(observation["exitCode"] == expected_case["exitCode"]',
        'require(observation["stderr"] == {"byteLength": 0, "sha256": empty_sha256}',
        'require(observation["settlement"] == "settled" and observation["settlementAuthority"] == "direct-and-original-process-group-settled"',
        'expected_projection = {"kind": "version", "runner": runner, "version": version}',
        '"adapter": {"id": "mock/unavailable", "harnessVersion": None, "descriptorDigestSha256": "599d3baf7b7aee1f97855dd4ca13e780c4de0aff410ad5860000631c0ac22fa4"}',
        'require(observation["projection"] == expected_projection',
        'place_bytes(f"{target}-release-package-admission.json", admission_bytes, MAX_EVIDENCE_BYTES)',
    )
    return all(
        fragment in assemble for fragment in required_assembly_fragments
    ) and all(
        fragment in assemble
        for fragment in (
            '"providerCalls": "not-observed"',
            '"semanticEvaluation": False',
            '"programPortabilityEvaluation": False',
            '"releaseEligible": False',
            '"publicationAuthorized": False',
            '"rankingProduced": False',
            '"strictDescendantContainment": False',
            '"runtimeNetworkIsolation": False',
        )
    )


def native_artifact_custody_is_immutable(blocks: dict[str, str]) -> bool:
    """Executed native trees may produce reports, never successor native artifacts."""

    build = blocks.get("build-native", "")
    verify = blocks.get("verify-native", "")
    profile = blocks.get("profile-admission", "")
    package = blocks.get("package-native", "")
    assemble = blocks.get("assemble", "")
    matrix_target = "${{ matrix.target }}"
    build_uploads = artifact_action_steps(build, "actions/upload-artifact")
    verify_uploads = artifact_action_steps(verify, "actions/upload-artifact")
    if build.count("actions/upload-artifact@") != len(build_uploads):
        return False
    if verify.count("actions/upload-artifact@") != len(verify_uploads):
        return False
    if build_uploads != ((f"native-build-{matrix_target}", f"native/{matrix_target}"),):
        return False
    if verify_uploads != (
        (f"native-verification-{matrix_target}", f"verification/{matrix_target}"),
    ):
        return False
    if not has_exact_artifact_download(
        verify, f"native-build-{matrix_target}", "candidate-native"
    ):
        return False
    if not has_exact_artifact_download(
        package, f"native-build-{matrix_target}", "incoming"
    ):
        return False
    if not has_exact_artifact_download(
        package, f"native-verification-{matrix_target}", "verification"
    ):
        return False
    required_fragments = (
        (verify, 'root = pathlib.Path("candidate-native")'),
        (verify, "assert {path.name for path in root.iterdir()} == expected_names"),
        (verify, '"nativeArtifact": {"name": f"native-build-{manifest[\'target\']}"'),
        (verify, '"workflowRunId": int(os.environ["WORKFLOW_RUN_ID"])'),
        (verify, '"workflowRunAttempt": int(os.environ["WORKFLOW_RUN_ATTEMPT"])'),
        (
            verify,
            'containment = {"strictDescendantContainmentEnforced": False, "releaseEligible": False, "blocker": "detached-descendant-containment-not-enforced"}',
        ),
        (
            verify,
            'assert {"runner": report["runner"], "build": report["build"], "image": report["image"]} == expected_report',
        ),
        (verify, 'report_root = pathlib.Path("verification") / manifest["target"]'),
        (verify, 'with report_path.open("xb") as opened:'),
        (verify, "assert report_path.read_bytes() == report_bytes"),
        (profile, "pattern: native-verification-*"),
        (
            profile,
            'assert {path.name for path in root.iterdir()} == {f"native-verification-{target}" for target in expected}',
        ),
        (
            profile,
            'assert {path.name for path in directory.iterdir()} == {"verification.json"}',
        ),
        (
            profile,
            'assert set(value) == {"schema", "target", "sourceSha", "nativeArtifact", "nativeManifestSha256", "windowsJobObjectReleaseAdmission", "windowsProcessHost", "containment", "reports"}',
        ),
        (profile, 'assert value["containment"] == expected_containment'),
        (profile, 'assert value["reports"] == expected_reports'),
        (package, "assert {path.name for path in root.iterdir()} == expected_names"),
        (
            package,
            'assert {path.name for path in verification_root.iterdir()} == {"verification.json"}',
        ),
        (package, "assert verification_bytes == protected_verification.read_bytes()"),
        (
            package,
            'assert verification["nativeArtifact"] == {"name": f"native-build-{os.environ[\'TARGET_ID\']}"',
        ),
        (package, '"files": observed_files}'),
        (
            package,
            'assert verification["nativeManifestSha256"] == hashlib.sha256(manifest_bytes).hexdigest()',
        ),
        (
            package,
            'assert verification["windowsJobObjectReleaseAdmission"] == manifest["windowsJobObjectReleaseAdmission"]',
        ),
        (
            package,
            'assert verification["windowsProcessHost"] == manifest["windowsProcessHost"]',
        ),
        (package, 'assert verification["containment"] == expected_containment'),
        (package, 'assert verification["reports"] == expected_reports'),
        (package, "python cli/ci/package_local.py"),
        (package, '--rust-binary "incoming/prose-rust$SUFFIX"'),
        (package, '--bun-binary "incoming/prose-bun$SUFFIX"'),
        (package, "TARGET_ID: ${{ matrix.target }}"),
        (package, 'elif [[ "$TARGET_ID" == linux-* ]]; then'),
        (package, 'discovered = shutil.which("readelf")'),
        (package, "print(Path(discovered).resolve(strict=True))"),
        (package, 'args+=(--readelf "$READELF_TOOL")'),
        (
            package,
            "args+=(--windows-process-host incoming/openprose-windows-process-host.exe)",
        ),
        (assemble, "pattern: native-build-*"),
        (
            assemble,
            'expected_verified_directories = {f"native-verification-{target}" for target in TARGET_PLATFORMS}',
        ),
        (
            assemble,
            'expected_native_directories = {f"native-build-{target}" for target in TARGET_PLATFORMS}',
        ),
        (
            assemble,
            "directory_names(native_dir, expected_native_names, len(expected_native_names))",
        ),
        (
            assemble,
            'require(verification["nativeArtifact"] == {"name": f"native-build-{target}"',
        ),
        (
            assemble,
            '"files": observed_native_files}, f"native artifact custody mismatch: {target}")',
        ),
        (
            assemble,
            'require(verification["nativeManifestSha256"] == hashlib.sha256(native_manifest_bytes).hexdigest()',
        ),
        (
            assemble,
            'require(verification["windowsJobObjectReleaseAdmission"] == native_manifest["windowsJobObjectReleaseAdmission"]',
        ),
        (
            assemble,
            'require(verification["windowsProcessHost"] == native_manifest["windowsProcessHost"]',
        ),
        (assemble, 'require(verification["containment"] == expected_containment'),
        (assemble, 'require(verification["reports"] == expected_reports'),
    )
    if not all(fragment in block for block, fragment in required_fragments):
        return False
    forbidden = (
        "name: verified-${{ matrix.target }}",
        "name: native-${{ matrix.target }}",
        "path: incoming\n          if-no-files-found",
        '(root / "verification.json").write_text(',
    )
    return not any(token in verify for token in forbidden)


def check_action_pins(
    text: str,
    label: str,
    failures: list[str],
    required: set[str] | None = None,
) -> None:
    observed: set[str] = set()
    for reference in re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", text):
        if "@" not in reference:
            failures.append(f"{label}: action is unpinned: {reference}")
            continue
        action, revision = reference.rsplit("@", 1)
        observed.add(action)
        expected = ACTION_PINS.get(action)
        require(
            expected is not None,
            f"{label}: action is outside the allowlist: {action}",
            failures,
        )
        if expected is not None:
            require(revision == expected, f"{label}: {action} pin drifted", failures)
        require(
            re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
            f"{label}: action pin is not a full commit SHA",
            failures,
        )
    if required is None:
        required = {
            "actions/checkout",
            "actions/setup-python",
            "actions/setup-node",
            "oven-sh/setup-bun",
        }
    require(
        required <= observed,
        f"{label}: required toolchain setup actions are missing",
        failures,
    )


def check_draft_helper(source: str, failures: list[str]) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        failures.append(f"release: draft helper is invalid Python: {error}")
        return
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    require(
        "subprocess" not in imports,
        "release: draft helper must not invoke a mutable release CLI",
        failures,
    )
    literal_policy_fields: dict[str, list[object]] = {
        "draft": [],
        "publicationAuthorized": [],
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value in literal_policy_fields
                and isinstance(value, ast.Constant)
            ):
                literal_policy_fields[key.value].append(value.value)
    require(
        len(literal_policy_fields["draft"]) >= 2
        and all(value is True for value in literal_policy_fields["draft"]),
        "release: every draft helper policy result must be literal draft=true",
        failures,
    )
    require(
        len(literal_policy_fields["publicationAuthorized"]) >= 2
        and all(
            value is False for value in literal_policy_fields["publicationAuthorized"]
        ),
        "release: every draft helper policy result must retain publicationAuthorized=false",
        failures,
    )
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    require(
        not any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr in {"system", "popen", "spawn", "execv", "execve"}
            for call in calls
        ),
        "release: draft helper must not enter a shell or process launcher",
        failures,
    )


def audit(ci: str, release: str, draft_helper: str | None = None) -> list[str]:
    failures: list[str] = []
    if draft_helper is None:
        try:
            draft_helper = DRAFT_HELPER.read_text("utf-8")
        except OSError as error:
            draft_helper = ""
            failures.append(f"release: cannot read draft helper: {error}")
    check_draft_helper(draft_helper, failures)
    for label, text in (("ci", ci), ("release", release)):
        require(
            "permissions:\n  contents: read" in text,
            f"{label}: top-level contents permission must be read",
            failures,
        )
        require(
            "id-token:" not in text,
            f"{label}: id-token permission is forbidden",
            failures,
        )
        require(
            re.search(r"(?m)^\s*(?:#\s*)?packages:\s*", text) is None,
            f"{label}: packages permission is forbidden",
            failures,
        )
        require(
            "continue-on-error" not in text,
            f"{label}: continue-on-error is forbidden",
            failures,
        )
        rust_installs = re.findall(r"rustup toolchain install\s+([^\s]+)", text)
        python_versions = re.findall(r"python-version:\s*[\"']?([^\s\"']+)", text)
        node_versions = re.findall(r"node-version:\s*[\"']?([^\s\"']+)", text)
        bun_versions = re.findall(r"bun-version:\s*[\"']?([^\s\"']+)", text)
        require(
            bool(rust_installs) and set(rust_installs) == {"1.87.0"},
            f"{label}: every Rust install must be exact 1.87.0",
            failures,
        )
        require(
            bool(python_versions) and set(python_versions) == {"3.10.18"},
            f"{label}: every Python runtime must be exact 3.10.18",
            failures,
        )
        require(
            bool(node_versions) and set(node_versions) == {"24.20.0"},
            f"{label}: every Node runtime must be exact 24.20.0",
            failures,
        )
        require(
            bool(bun_versions) and set(bun_versions) == {"1.3.5"},
            f"{label}: every Bun runtime must be exact 1.3.5",
            failures,
        )
        check_action_pins(text, label, failures)
        require(
            text.count("persist-credentials: false") == text.count("actions/checkout@"),
            f"{label}: every checkout must disable persisted credentials",
            failures,
        )

    ci_header = ci[: ci.find("permissions:")]
    require(
        re.search(r"(?m)^  pull_request:\s*$", ci_header) is not None,
        "ci: pull_request trigger is missing",
        failures,
    )
    require(
        re.search(r"(?m)^  push:\s*$", ci_header) is not None
        and "branches: [main]" in ci_header,
        "ci: main push trigger is missing",
        failures,
    )
    require(
        re.search(r"(?m)^  workflow_dispatch:\s*$", ci_header) is not None,
        "ci: manual trigger is missing",
        failures,
    )
    for workflow_path in (
        ".github/workflows/openprose-cli-ci.yml",
        ".github/workflows/openprose-cli-draft-release.yml",
        ".github/workflows/openprose-cli-alpha-release.yml",
        ".github/workflows/openprose-cli-alpha-promote.yml",
        ".github/workflows/openprose-cli-alpha-post-public.yml",
        "cli/ci/run_public_alpha_verification.py",
        "cli/ci/test_run_public_alpha_verification.py",
        "cli/ci/verify_public_alpha.py",
        "cli/ci/test_verify_public_alpha.py",
        "cli/release/alpha-public-verification.schema.json",
        ".github/ISSUE_TEMPLATE/openprose-cli-bug.yml",
        ".github/ISSUE_TEMPLATE/openprose-cli-harness-model.yml",
        "README.md",
        "RELEASE.md",
        "CONTRIBUTING.md",
        "TERMS.md",
        "PRIVACY.md",
    ):
        require(
            ci_header.count(workflow_path) == 2,
            f"ci: both PR/main filters must include {workflow_path}",
            failures,
        )
    require(
        ci_header.count('"LICENSE"') == 2,
        "ci: both PR/main filters must include packaged LICENSE",
        failures,
    )

    release_header = release[: release.find("permissions:")]
    require(
        "workflow_dispatch:" in release_header,
        "release: manual trigger is missing",
        failures,
    )
    for forbidden_trigger in ("pull_request:", "push:", "schedule:", "release:"):
        require(
            forbidden_trigger not in release_header,
            f"release: forbidden trigger {forbidden_trigger}",
            failures,
        )
    for release_input in (
        "version:",
        "source_sha:",
        "authority_run_id:",
        "authority_artifact_id:",
        "draft_only:",
    ):
        require(
            release_input in release_header,
            f"release: required input {release_input} is missing",
            failures,
        )
    require(
        "type: boolean" in release_header and "default: true" in release_header,
        "release: draft-only boolean is not locked on",
        failures,
    )

    for target, runner in TARGET_RUNNERS.items():
        row = re.compile(
            rf"\{{target: {re.escape(target)}, runner: {re.escape(runner)}(?:,|\}})"
        )
        require(
            len(row.findall(ci)) == 1,
            f"ci: native target {target}/{runner} must appear exactly once",
            failures,
        )
        require(
            len(row.findall(release)) == 4,
            f"release: native target {target}/{runner} must appear in build, verify, package, and package admission",
            failures,
        )

    ci_jobs = job_blocks(ci)
    require(
        set(ci_jobs) == {"native"},
        "ci: job set must remain the single native matrix",
        failures,
    )
    require(
        "python -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r cli/ci/requirements-test.txt"
        in ci,
        "ci: Python dependencies must use the closed hash-checked binary-only lock",
        failures,
    )
    ci_steps = named_step_blocks(ci_jobs.get("native", ""))
    posix_step = ci_steps.get(
        "Run full provider-free test and build admission on POSIX", ""
    )
    windows_step = ci_steps.get(
        "Run native Windows static build and test checks without admission", ""
    )
    require(
        re.search(r"(?m)^        if: matrix\.target != 'win-x64'\s*$", posix_step)
        is not None
        and re.search(r"(?m)^        run: python cli/ci/run_local\.py\s*$", posix_step)
        is not None
        and ci.count("python cli/ci/run_local.py") == 1,
        "ci: full local admission must run exactly once and only on the four POSIX targets",
        failures,
    )
    require(
        re.search(r"(?m)^        if: matrix\.target == 'win-x64'\s*$", windows_step)
        is not None
        and "run_local.py" not in windows_step,
        "ci: Windows must use an explicit non-admitting native lane",
        failures,
    )
    windows_commands = (
        "python cli/platform/windows-process-host/scripts/verify_static.py",
        "cargo fmt --manifest-path cli/platform/windows-process-host/Cargo.toml --all -- --check",
        "cargo clippy --manifest-path cli/platform/windows-process-host/Cargo.toml --locked --offline --all-targets --all-features -- -D warnings",
        "cargo test --manifest-path cli/platform/windows-process-host/Cargo.toml --locked --offline --all-features",
        "cargo fmt --manifest-path cli/rust/Cargo.toml --all -- --check",
        "cargo clippy --manifest-path cli/rust/Cargo.toml --workspace --all-targets --features prose-cli/test-seams --locked --offline -- -D warnings",
        "cargo test --manifest-path cli/rust/Cargo.toml --workspace --all-targets --features prose-cli/test-seams --locked --offline",
        "bun run --cwd cli/bun typecheck",
        "bun run --cwd cli/bun test",
        "bun run --cwd cli/bun build",
    )
    for command in windows_commands:
        require(
            windows_step.count(command) == 1,
            f"ci: Windows native lane is missing exact check: {command}",
            failures,
        )
    require(
        ci.count("OPENPROSE_WINDOWS_HOST_ADMISSION=0") == 1
        and "OPENPROSE_WINDOWS_HOST_ADMISSION=1" not in ci,
        "ci: Windows host must remain explicitly non-admitted",
        failures,
    )

    blocks = job_blocks(release)
    expected_jobs = {"preflight", *RELEASE_GRAPH}
    require(
        set(blocks) == expected_jobs,
        "release: job set is not the closed eight-stage graph",
        failures,
    )
    for job in ("preflight", "profile-admission", "assemble", "draft"):
        require(
            re.search(r"(?m)^    runs-on:\s*ubuntu-24\.04\s*$", blocks.get(job, ""))
            is not None,
            f"release: non-candidate control job {job} must remain on Ubuntu 24.04",
            failures,
        )
    for job, dependency in RELEASE_GRAPH.items():
        require(
            re.search(
                rf"(?m)^    needs:\s*{re.escape(dependency)}\s*$", blocks.get(job, "")
            )
            is not None,
            f"release: {job} must depend exactly on {dependency}",
            failures,
        )
    require(
        "needs:" not in blocks.get("preflight", ""),
        "release: preflight must be the graph root",
        failures,
    )

    for job, block in blocks.items():
        expected_permission = "write" if job == "draft" else "read"
        require(
            re.search(
                rf"(?m)^    permissions:\s*\n      contents:\s*{expected_permission}\s*$",
                block,
            )
            is not None,
            f"release: {job} contents permission must be {expected_permission}",
            failures,
        )
        if job != "draft":
            require(
                "contents: write" not in block,
                f"release: {job} must not write repository contents",
                failures,
            )
    require(
        release.count("contents: write") == 1,
        "release: only draft may receive contents:write",
        failures,
    )
    require(
        re.search(
            r"(?m)^    environment: openprose-cli-profile-admission\s*$",
            blocks.get("profile-admission", ""),
        )
        is not None,
        "release: profile admission environment is unprotected",
        failures,
    )
    require(
        re.search(
            r"(?m)^    environment: openprose-cli-draft-release\s*$",
            blocks.get("draft", ""),
        )
        is not None,
        "release: draft environment is unprotected",
        failures,
    )
    require(
        re.search(
            r"(?m)^    if: inputs\.draft_only == true\s*$", blocks.get("draft", "")
        )
        is not None,
        "release: draft job condition must remain exact",
        failures,
    )

    build = blocks.get("build-native", "")
    require(
        'OPENPROSE_REQUIRE_RELEASE_IMAGE: "1"' in build,
        "release: Rust release-image gate is missing",
        failures,
    )
    require(
        "cargo build --manifest-path cli/rust/Cargo.toml --release --locked" in build,
        "release: exact Rust release build is missing",
        failures,
    )
    require(
        'SOURCE_ROOT="$(pwd -P)"' in build
        and 'CARGO_HOME_ROOT="$(cd "${CARGO_HOME:-$HOME/.cargo}" && pwd -P)"' in build
        and 'CARGO_INCREMENTAL=0 RUSTFLAGS="--remap-path-prefix=$SOURCE_ROOT=/openprose-source'
        ' --remap-path-prefix=$CARGO_HOME_ROOT=/cargo-home"'
        in build,
        "release: Rust build must disable incrementality and canonically remap its physical source root and Cargo home",
        failures,
    )
    require(
        "cli/bun/scripts/image-bundle.ts build" in build
        and "--require-release-eligible" in build,
        "release: Bun public release build is missing",
        failures,
    )
    require(
        "OPENPROSE_BUILD_COMMIT:" in build,
        "release: immutable build commit is missing",
        failures,
    )
    for variable in (
        "OPENPROSE_IMAGE_SOURCE_DIR",
        "OPENPROSE_IMAGE_BUNDLE",
        "OPENPROSE_IMAGE_BUNDLE_CHECKSUM",
    ):
        require(
            f"{variable}:" in build,
            f"release: build does not bind {variable}",
            failures,
        )
    for job, block in blocks.items():
        if job != "build-native":
            require(
                re.search(
                    r"\bcargo build\b|\bbun run[^\n]*\bbuild(?::release)?\b", block
                )
                is None,
                f"release: {job} must not compile",
                failures,
            )

    preflight = blocks.get("preflight", "")
    require(
        "python control/cli/ci/release_preflight.py" in preflight,
        "release: trusted preflight authority is not invoked",
        failures,
    )
    require(
        re.search(r"(?m)^    if: github\.ref == 'refs/heads/main'\s*$", preflight)
        is not None,
        "release: preflight must run only from the main control ref",
        failures,
    )
    require(
        "git -C candidate merge-base --is-ancestor" in preflight
        and "refs/remotes/origin/main" in preflight,
        "release: candidate must be an ancestor of main",
        failures,
    )
    require(
        "path: control" in preflight and "path: candidate" in preflight,
        "release: trusted control and candidate checkouts must remain separated",
        failures,
    )
    require(
        "$IMAGE_MANIFEST" in preflight
        and "$IMAGE_BUNDLE" in preflight
        and "$IMAGE_BUNDLE_CHECKSUM" in preflight
        and "$CANONICAL_PROFILE" in preflight
        and "$RELEASE_EVIDENCE" in preflight,
        "release: preflight does not receive every release gate",
        failures,
    )
    require(
        re.search(r"(?m)^      actions: read\s*$", preflight) is not None
        and release.count("actions: read") == 1,
        "release: only preflight must receive read-only Actions authority",
        failures,
    )
    require(
        "artifact-ids: ${{ inputs.authority_artifact_id }}" in preflight
        and "run-id: ${{ inputs.authority_run_id }}" in preflight
        and "github-token: ${{ github.token }}" in preflight
        and "path: protected-authority" in preflight
        and "merge-multiple: true" in preflight,
        "release: preflight must download the exact protected producer artifact by run and artifact ID",
        failures,
    )
    require(
        'github(f"/actions/runs/{run_id}")' in preflight
        and 'github(f"/actions/artifacts/{artifact_id}")' in preflight
        and 'run["path"] == os.environ["PROTECTED_AUTHORITY_WORKFLOW"]' in preflight
        and 'artifact["workflow_run"]["id"] == run_id' in preflight,
        "release: preflight must independently verify protected producer and artifact identity",
        failures,
    )
    require(
        ".github/workflows/openprose-cli-protected-release-authority.yml" in release
        and "openprose-cli-release-authority" in release
        and "openprose-cli-protected-release-authority" in release,
        "release: protected producer workflow, environment, and artifact identities are not closed",
        failures,
    )
    for protected_argument in (
        "--authority-run-metadata",
        "--authority-provenance",
        "--authority-artifact-id",
        "--authority-repository",
        "--authority-workflow-path",
        "--authority-environment",
    ):
        require(
            release.count(protected_argument) == 2,
            f"release: {protected_argument} must bind both protected preflight validations",
            failures,
        )
    require(
        '"candidate/$CANONICAL_PROFILE"' not in release
        and '"candidate/$RELEASE_EVIDENCE"' not in release
        and '--canonical-profile "$CANONICAL_PROFILE"' not in release
        and '--release-evidence "$RELEASE_EVIDENCE"' not in release,
        "release: candidate source must never supply protected pass attestations",
        failures,
    )
    require(
        release.count('--canonical-profile "protected-authority/$CANONICAL_PROFILE"')
        == 1
        and release.count('--release-evidence "protected-authority/$RELEASE_EVIDENCE"')
        == 1
        and release.count(
            '--canonical-profile "authority/protected-authority/$CANONICAL_PROFILE"'
        )
        == 1
        and release.count(
            '--release-evidence "authority/protected-authority/$RELEASE_EVIDENCE"'
        )
        == 1,
        "release: both preflight validations must use carried protected attestations",
        failures,
    )
    require(
        release.count("--authority-run-metadata protected-authority-run.json") == 1
        and release.count(
            "--authority-run-metadata authority/protected-authority-run.json"
        )
        == 1,
        "release: both preflight validations must consume exact verified producer-run metadata",
        failures,
    )
    require(
        "--rust-manifest candidate/cli/rust/Cargo.toml" in preflight
        and "--bun-package candidate/cli/bun/package.json" in preflight,
        "release: preflight does not bind both candidate product versions",
        failures,
    )
    require(
        "if: always()" in preflight,
        "release: failed preflight diagnostics must always upload",
        failures,
    )
    require(
        "job_object_release_admitted: false" in build,
        "release: Windows Job Object admission must remain false",
        failures,
    )
    require(
        "job_object_release_admitted: true" not in build,
        "release: Windows Job Object admission is overclaimed",
        failures,
    )
    require(
        all("${{ inputs." not in script for script in run_scripts(release)),
        "release: dispatch inputs must enter scripts only through quoted environment variables",
        failures,
    )

    verify_native = blocks.get("verify-native", "")
    require(
        "capture_output=True" not in verify_native
        and "MAX_CHILD_OUTPUT_BYTES = 1024 * 1024" in verify_native
        and "CHILD_TIMEOUT_SECONDS = 15" in verify_native
        and "subprocess.Popen(" in verify_native
        and "if overflow.is_set():" in verify_native,
        "release: native verification must use bounded child output and time",
        failures,
    )
    require(
        native_artifact_custody_is_immutable(blocks),
        "release: executed native trees must never become successor artifacts; reports must bind immutable build bytes",
        failures,
    )
    require(
        release_package_admission_custody_is_closed(blocks),
        "release: exact package admission must use protected control code and bind report-only evidence to original package bytes",
        failures,
    )

    for job in (
        "verify-native",
        "profile-admission",
        "package-native",
        "admit-release-packages",
        "assemble",
        "draft",
    ):
        block = blocks.get(job, "")
        require(
            "actions/download-artifact@" in block,
            f"release: {job} must consume uploaded artifacts",
            failures,
        )
        if job not in {"admit-release-packages", "draft"}:
            require(
                "hashlib.sha256" in block,
                f"release: {job} must verify exact hashes",
                failures,
            )
    for job in (
        "preflight",
        "build-native",
        "verify-native",
        "profile-admission",
        "package-native",
        "admit-release-packages",
        "assemble",
    ):
        require(
            "actions/upload-artifact@" in blocks.get(job, ""),
            f"release: {job} must publish its immutable boundary",
            failures,
        )

    forbidden_commands = (
        r"\bnpm\s+publish\b",
        r"\bcargo\s+publish\b",
        r"\bgit\s+push\b",
        r"\bgit\s+tag\b",
        r"\bpromote\b",
        r"\bgh\s+release\s+(?:edit|upload)[^\n]*(?:--draft=false|--latest)",
    )
    for pattern in forbidden_commands:
        require(
            re.search(pattern, release, re.IGNORECASE) is None,
            f"release: forbidden publication operation matches {pattern}",
            failures,
        )
    draft = blocks.get("draft", "")
    require(
        "gh release" not in release,
        "release: mutable gh release CLI is forbidden",
        failures,
    )
    require(
        "python control/cli/ci/create_draft_release.py" in draft
        and "--assembly assembly" in draft,
        "release: protected job must use the trusted audited draft API helper",
        failures,
    )
    require(
        "CONTROL_SHA: ${{ github.sha }}" in draft
        and '--control-sha "$CONTROL_SHA"' in draft,
        "release: draft publication helper must bind exact protected controller identity",
        failures,
    )
    require(
        "native-manifest.json" in blocks.get("assemble", "")
        and "verification.json" in blocks.get("assemble", ""),
        "release: final assembly must retain native byte lineage",
        failures,
    )
    require(
        "dependency-evidence.json" in blocks.get("package-native", ""),
        "release: package must retain exact dependency evidence",
        failures,
    )
    require(
        assembly_copies_exact_dependency_evidence(blocks.get("assemble", "")),
        "release: assembly must structurally copy the exact dependency evidence set",
        failures,
    )
    require(
        assembly_has_closed_bounded_checksum_parser(blocks.get("assemble", "")),
        "release: assembly checksum parsing and destination writes must remain closed, bounded, and no-follow",
        failures,
    )
    profile_admission = blocks.get("profile-admission", "")
    package_native = blocks.get("package-native", "")
    assemble = blocks.get("assemble", "")
    require(
        profile_generates_protected_dependency(profile_admission),
        "release: protected dependency evidence must be generated by trusted control against the exact candidate",
        failures,
    )
    require(
        profile_binds_protected_dependency(profile_admission),
        "release: profile admission must structurally bind protected dependency evidence",
        failures,
    )
    require(
        re.search(
            r"(?m)^\s+protected-dependency-evidence\.json\s*$",
            profile_admission,
        )
        is not None,
        "release: profile admission artifact must retain protected dependency evidence",
        failures,
    )
    require(
        "name: release-preflight" in profile_admission
        and "authority/protected-authority-run.json" in profile_admission
        and "authority/protected-authority" in profile_admission,
        "release: protected profile admission must consume and retain exact external authority bytes",
        failures,
    )
    require(
        "protected/authority/protected-authority/$CANONICAL_PROFILE" in package_native
        and "protected/authority/protected-authority/$RELEASE_EVIDENCE"
        in package_native,
        "release: packaging must consume protected carried attestations, never candidate copies",
        failures,
    )
    for protected_name in (
        "protected-authority-run.json",
        "authority-provenance.json",
        "canonical-profile-attestation.json",
        "release-evidence-attestation.json",
    ):
        require(
            protected_name in assemble,
            f"release: assembly must retain {protected_name}",
            failures,
        )
    require(
        re.search(
            r"(?m)^\s+name: profile-admission\s*$",
            package_native,
        )
        is not None,
        "release: native packaging must download protected profile admission",
        failures,
    )
    require(
        contains_python_statement(
            package_native,
            "assert dependency_bytes == protected_dependency_bytes",
        ),
        "release: native packaging must byte-compare dependency evidence to protected evidence",
        failures,
    )
    require(
        contains_python_statement(
            assemble,
            'place_source(pathlib.Path("protected/protected-dependency-evidence.json"), '
            '"protected-dependency-evidence.json")',
        ),
        "release: assembly must retain the exact protected dependency evidence",
        failures,
    )
    return failures


def audit_alpha(
    alpha: str,
    draft_helper: str | None = None,
    notes_renderer: str | None = None,
) -> list[str]:
    """Keep the deliberately weaker functional-alpha publication path honest."""

    failures: list[str] = []
    if draft_helper is None:
        draft_helper = DRAFT_HELPER.read_text("utf-8")
    if notes_renderer is None:
        notes_renderer = RELEASE_NOTES_RENDERER.read_text("utf-8")
    require(
        "permissions:\n  contents: read" in alpha,
        "alpha: top-level contents permission must be read",
        failures,
    )
    require(
        "continue-on-error" not in alpha,
        "alpha: continue-on-error is forbidden",
        failures,
    )
    header = alpha[: alpha.find("permissions:")]
    require(
        "workflow_dispatch:" in header, "alpha: manual trigger is missing", failures
    )
    for forbidden in ("pull_request:", "push:", "schedule:", "release:"):
        require(
            forbidden not in header, f"alpha: forbidden trigger {forbidden}", failures
        )
    for name in ("version:", "source_sha:", "draft_only:"):
        require(name in header, f"alpha: required input {name} is missing", failures)
    require(
        "default: true" in header and "type: boolean" in header,
        "alpha: draft-only input is not locked on",
        failures,
    )
    require(
        alpha.count("group: openprose-cli-functional-alpha-${{ inputs.version }}") == 1
        and "group: openprose-cli-functional-alpha-${{ inputs.source_sha }}"
        not in alpha
        and re.search(r"(?m)^  cancel-in-progress: false\s*$", alpha) is not None,
        "alpha: release concurrency must serialize non-cancelling runs by exact version",
        failures,
    )
    check_action_pins(alpha, "alpha", failures)
    require(
        alpha.count("persist-credentials: false") == alpha.count("actions/checkout@"),
        "alpha: every checkout must disable persisted credentials",
        failures,
    )
    require(
        set(re.findall(r"rustup toolchain install\s+([^\s]+)", alpha)) == {"1.87.0"},
        "alpha: Rust toolchain must be exact 1.87.0",
        failures,
    )
    require(
        set(re.findall(r"python-version:\s*[\"']?([^\s\"']+)", alpha)) == {"3.10.18"},
        "alpha: Python runtime must be exact 3.10.18",
        failures,
    )
    require(
        set(re.findall(r"node-version:\s*[\"']?([^\s\"']+)", alpha)) == {"24.20.0"},
        "alpha: Node runtime must be exact 24.20.0",
        failures,
    )
    require(
        set(re.findall(r"bun-version:\s*[\"']?([^\s\"']+)", alpha)) == {"1.3.5"},
        "alpha: Bun runtime must be exact 1.3.5",
        failures,
    )
    require(
        "python -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r cli/ci/requirements-test.txt"
        in alpha,
        "alpha: Python dependencies must use the closed hash-checked binary-only lock",
        failures,
    )
    blocks = job_blocks(alpha)
    require(
        tuple(blocks) == ("preflight", "test", "package", "admit", "assemble", "draft"),
        "alpha: job set and order must remain preflight/test/package/admit/assemble/draft",
        failures,
    )
    expected_permissions = {
        "preflight": {"contents": "read"},
        "test": {"contents": "read"},
        "package": {"contents": "read"},
        "admit": {"contents": "read"},
        "assemble": {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
            "artifact-metadata": "write",
        },
        "draft": {"contents": "write"},
    }
    for job, expected in expected_permissions.items():
        require(
            job_permission_values(blocks.get(job, "")) == expected,
            f"alpha: {job} permissions must remain exact and least-privilege",
            failures,
        )
    require(
        alpha.count("id-token: write") == 1
        and alpha.count("attestations: write") == 1
        and alpha.count("artifact-metadata: write") == 1
        and all(
            token
            not in "".join(
                block for name, block in blocks.items() if name != "assemble"
            )
            for token in ("id-token:", "attestations:", "artifact-metadata:")
        ),
        "alpha: only assembly may mint the release-asset attestation",
        failures,
    )
    for job in ("preflight", "test", "assemble", "draft"):
        require(
            re.search(r"(?m)^    runs-on:\s*ubuntu-24\.04\s*$", blocks.get(job, ""))
            is not None,
            f"alpha: non-candidate control job {job} must remain on Ubuntu 24.04",
            failures,
        )
    preflight = blocks.get("preflight", "")
    require(
        "needs:" not in preflight and re.search(r"(?m)^    if:\s*", preflight) is None,
        "alpha: dispatch preflight must be the unconditional graph root",
        failures,
    )
    require(
        alpha.count("NPM_REGISTRY_LINEAGE: cli/release/npm-registry-lineage.v1.json")
        == 1,
        "alpha: pinned npm registry-lineage authority is missing",
        failures,
    )
    test_steps = named_step_blocks(blocks.get("test", ""))
    source_identity = test_steps.get("Validate source and version identity", "")
    source_github_equality = 'test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA"'
    source_control_equality = 'test "$SOURCE_SHA_INPUT" = "$CONTROL_SHA_INPUT"'

    def exact_shell_line(body: str, line: str) -> bool:
        return re.search(rf"(?m)^[ \t]*{re.escape(line)}[ \t]*$", body) is not None

    preflight_steps = workflow_step_blocks(preflight)
    preflight_named_steps = named_step_blocks(preflight)
    dispatch_preflight = preflight_named_steps.get(
        "Require an exact main-branch draft dispatch", ""
    )
    public_docs_preflight = preflight_named_steps.get(
        "Authenticate exact public functional-alpha documentation", ""
    )
    exact_dispatch_environment = {
        "DRAFT_ONLY_INPUT": "${{ inputs.draft_only }}",
        "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
        "VERSION_INPUT": "${{ inputs.version }}",
    }
    exact_dispatch_script = (
        "|\n"
        "          set -euo pipefail\n"
        '          test "$GITHUB_REF" = "refs/heads/main"\n'
        '          test "$DRAFT_ONLY_INPUT" = "true"\n'
        '          [[ "$SOURCE_SHA_INPUT" =~ ^[0-9a-f]{40}$ ]]\n'
        '          [[ "$VERSION_INPUT" =~ ^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)-alpha\\.(0|[1-9][0-9]*)$ ]]\n'
        '          test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA"'
    )
    require(
        re.findall(r"(?m)^    timeout-minutes:\s*(.+)\s*$", preflight) == ["10"]
        and step_env_values(dispatch_preflight) == exact_dispatch_environment
        and run_scripts(dispatch_preflight) == (exact_dispatch_script,)
        and dispatch_preflight.count("        shell: bash") == 1,
        "alpha: dispatch preflight must visibly reject wrong-ref, non-draft, malformed, and non-control inputs",
        failures,
    )
    preflight_action_steps = [
        step for step in preflight_steps if step_uses(step) is not None
    ]
    require(
        tuple(step_uses(step) for step in preflight_action_steps)
        == (
            "actions/checkout@" + ACTION_PINS["actions/checkout"],
            "actions/checkout@" + ACTION_PINS["actions/checkout"],
            "actions/setup-python@" + ACTION_PINS["actions/setup-python"],
        )
        and step_with_values(preflight_action_steps[0])
        == {
            "ref": "${{ github.sha }}",
            "path": "control",
            "fetch-depth": "0",
            "persist-credentials": "false",
        }
        and step_with_values(preflight_action_steps[1])
        == {
            "ref": "${{ inputs.source_sha }}",
            "path": "candidate",
            "persist-credentials": "false",
        }
        and step_with_values(preflight_action_steps[2])
        == {"python-version": '"3.10.18"'},
        "alpha: public-doc preflight must use separate exact control/candidate checkouts and Python",
        failures,
    )
    exact_public_docs_environment = {
        "CONTROL_SHA_INPUT": "${{ github.sha }}",
        "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
        "VERSION_INPUT": "${{ inputs.version }}",
    }
    exact_public_docs_script = (
        "|\n"
        "          set -euo pipefail\n"
        '          test "$(git -C control rev-parse HEAD)" = "$CONTROL_SHA_INPUT"\n'
        '          test "$(git -C candidate rev-parse HEAD)" = "$SOURCE_SHA_INPUT"\n'
        '          test "$SOURCE_SHA_INPUT" = "$CONTROL_SHA_INPUT"\n'
        "          git -C control diff --quiet --ignore-submodules --\n"
        "          git -C control diff --cached --quiet --ignore-submodules --\n"
        "          git -C candidate diff --quiet --ignore-submodules --\n"
        "          git -C candidate diff --cached --quiet --ignore-submodules --\n"
        "          python control/cli/ci/check_alpha_public_docs.py \\\n"
        '            --version "$VERSION_INPUT" \\\n'
        '            --repository-root "$GITHUB_WORKSPACE/candidate"\n'
    )
    require(
        tuple(preflight_named_steps)
        == (
            "Require an exact main-branch draft dispatch",
            "Authenticate exact public functional-alpha documentation",
        )
        and step_env_values(public_docs_preflight) == exact_public_docs_environment
        and run_scripts(public_docs_preflight) == (exact_public_docs_script,)
        and public_docs_preflight.count("        shell: bash") == 1
        and alpha.count("check_alpha_public_docs.py") == 1
        and "candidate/cli/ci/check_alpha_public_docs.py" not in alpha,
        "alpha: controller-owned public-doc preflight must authenticate the exact clean candidate and version once",
        failures,
    )

    require(
        'python cli/ci/check_registry_lineage.py --authority "$NPM_REGISTRY_LINEAGE" --version "$VERSION_INPUT"'
        in source_identity
        and exact_shell_line(source_identity, source_github_equality),
        "alpha: candidate source and version must equal the dispatched controller identity and pinned npm lineage",
        failures,
    )
    require(
        re.search(r"(?m)^    needs:\s*preflight\s*$", blocks.get("test", ""))
        is not None
        and re.search(r"(?m)^    if:\s*", blocks.get("test", "")) is None,
        "alpha: test must depend unconditionally on the visible dispatch preflight",
        failures,
    )
    require(
        re.search(r"(?m)^    needs:\s*test\s*$", blocks.get("package", "")) is not None,
        "alpha: package must depend on test",
        failures,
    )
    require(
        re.search(r"(?m)^    needs:\s*package\s*$", blocks.get("admit", ""))
        is not None,
        "alpha: admission must depend on package",
        failures,
    )
    assemble = blocks.get("assemble", "")
    require(
        "needs: [package, admit]" in assemble,
        "alpha: assembly must depend on package and admission",
        failures,
    )
    require(
        re.search(r"(?m)^    needs:\s*assemble\s*$", blocks.get("draft", ""))
        is not None,
        "alpha: draft must depend only on closed assembly",
        failures,
    )
    require(
        alpha.count("contents: write") == 1
        and "contents: write" in blocks.get("draft", ""),
        "alpha: only draft may write contents",
        failures,
    )
    for job in ("preflight", "test", "package", "admit", "assemble"):
        require(
            "contents: write" not in blocks.get(job, ""),
            f"alpha: {job} must remain read-only",
            failures,
        )
    require(
        "environment: openprose-cli-alpha-release" in blocks.get("draft", ""),
        "alpha: draft environment is unprotected",
        failures,
    )
    require(
        "if: inputs.draft_only == true" in blocks.get("draft", ""),
        "alpha: draft-only condition is missing",
        failures,
    )
    require(
        alpha.count("cli/shared/image/echo-v0") == 3
        and "functional-alpha-placeholder" not in blocks.get("package", ""),
        "alpha: exact echo-v0 image paths are missing",
        failures,
    )
    require(
        blocks.get("package", "").count("--mode alpha") == 2,
        "alpha: both package modes must be alpha",
        failures,
    )
    require(
        "--require-release-eligible" in blocks.get("package", ""),
        "alpha: release-profile image check is missing",
        failures,
    )
    require(
        'OPENPROSE_REQUIRE_RELEASE_IMAGE: "1"' in blocks.get("package", ""),
        "alpha: Rust image gate is missing",
        failures,
    )
    require(
        "OPENPROSE_BUILD_VERSION: ${{ inputs.version }}" in blocks.get("package", ""),
        "alpha: product build version is not the prerelease input",
        failures,
    )
    package = blocks.get("package", "")
    for variable, source_variable in (
        ("OPENPROSE_IMAGE_SOURCE_DIR", "IMAGE_SOURCE_DIR"),
        ("OPENPROSE_IMAGE_BUNDLE", "IMAGE_BUNDLE"),
        ("OPENPROSE_IMAGE_BUNDLE_CHECKSUM", "IMAGE_BUNDLE_CHECKSUM"),
    ):
        require(
            f'{variable}="$SOURCE_ROOT/${source_variable}"' in package,
            f"alpha: {variable} must be independently rooted in each physical checkout",
            failures,
        )
    require(
        "python -m unittest -v cli.ci.test_alpha_package_admission"
        in blocks.get("test", ""),
        "alpha: exact-package admission tests are missing",
        failures,
    )
    require(
        "--only release-reproducibility" in blocks.get("test", ""),
        "alpha: reproducible-release contract tests are missing",
        failures,
    )
    require(
        '"$CARGO_TOOL" build --manifest-path cli/rust/Cargo.toml --release --locked'
        in package,
        "alpha: Rust release build is missing",
        failures,
    )
    require(
        'SOURCE_ROOT="$(pwd -P)"' in package
        and 'CARGO_HOME_ROOT="$(cd "${CARGO_HOME:-$HOME/.cargo}" && pwd -P)"' in package
        and 'CARGO_INCREMENTAL=0 RUSTC="$RUSTC_TOOL"' in package
        and 'RUSTFLAGS="--remap-path-prefix=$SOURCE_ROOT=/openprose-source'
        ' --remap-path-prefix=$CARGO_HOME_ROOT=/cargo-home"' in package,
        "alpha: Rust build must disable incrementality and canonically remap its physical source root and Cargo home",
        failures,
    )
    require(
        package.count("actions/checkout@") == 3
        and "ref: ${{ github.sha }}\n          path: control\n          persist-credentials: false"
        in package
        and package.count("ref: ${{ inputs.source_sha }}\n          path: candidate-")
        == 2
        and "path: candidate-a\n          persist-credentials: false" in package
        and "path: candidate-b\n          persist-credentials: false" in package,
        "alpha: package custody requires one controller and two credential-free candidate checkouts",
        failures,
    )
    for row in (
        "platform: linux-x64-gnu, receipt_os: Linux, receipt_arch: x64",
        "platform: linux-arm64-gnu, receipt_os: Linux, receipt_arch: arm64",
        "platform: darwin-arm64, receipt_os: macOS, receipt_arch: arm64",
        "platform: darwin-x64, receipt_os: macOS, receipt_arch: x64",
    ):
        require(
            row in package, f"alpha: package receipt matrix is missing {row}", failures
        )
    package_steps = named_step_blocks(package)
    roots = package_steps.get("Validate two physical source roots", "")
    require(
        'test "$(git -C control rev-parse HEAD)" = "$CONTROL_SHA_INPUT"' in roots
        and 'test "$(git -C candidate-a rev-parse HEAD)" = "$SOURCE_SHA_INPUT"' in roots
        and 'test "$(git -C candidate-b rev-parse HEAD)" = "$SOURCE_SHA_INPUT"' in roots
        and exact_shell_line(roots, source_control_equality)
        and "os.path.samefile(left, right)" in roots
        and "source roots must be physically distinct" in roots,
        "alpha: both physical builds must use the exact controller revision and remain distinct",
        failures,
    )
    install = package_steps.get("Install exact build inputs", "")
    require(
        "cargo fetch --manifest-path candidate-a/cli/rust/Cargo.toml --locked"
        in install
        and "cargo fetch --manifest-path candidate-b/cli/rust/Cargo.toml --locked"
        in install
        and "bun install --cwd candidate-a/cli/bun --frozen-lockfile" in install
        and "bun install --cwd candidate-b/cli/bun --frozen-lockfile" in install,
        "alpha: both source roots require independent locked dependency materialization",
        failures,
    )
    readelf = package_steps.get("Resolve exact Linux readelf", "")
    require(
        "id: exact-linux-tools" in readelf
        and 'if [[ "$PLATFORM_INPUT" == linux-* ]]' in readelf
        and 'READELF_TOOL="$(python - "$(command -v readelf)"' in readelf
        and '[[ "$READELF_TOOL" =~ ^/[A-Za-z0-9._+/@:-]+$ ]]' in readelf
        and 'printf \'readelf=%s\\n\' "$READELF_TOOL" >>"$GITHUB_OUTPUT"' in readelf
        and package.count("command -v readelf") == 1,
        "alpha: Linux readelf must be resolved once to a direct portable path",
        failures,
    )
    build = package_steps.get("Build two release-profile Rust and Bun cohorts", "")
    require(
        'build_one "$GITHUB_WORKSPACE/candidate-a"' in build
        and 'build_one "$GITHUB_WORKSPACE/candidate-b"' in build
        and build.count("build_one ") == 2
        and 'cd "$requested_root"' in build
        and "cli/bun/scripts/image-bundle.ts build" in build
        and "--require-release-eligible" in build,
        "alpha: two physical roots must each receive a genuine Rust and Bun build",
        failures,
    )
    require(
        all(
            marker in build
            for marker in (
                'CARGO_TOOL="$(direct_path "$(rustup which cargo)")"',
                'RUSTC_TOOL="$(direct_path "$(rustup which rustc)")"',
                'BUN_TOOL="$(direct_path "$(command -v bun)")"',
                'LINKER_TOOL="$(direct_path "$(command -v cc)")"',
                "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER",
                "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER",
                "CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER",
                "CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER",
            )
        ),
        "alpha: both builds must use receipt-compatible direct Cargo, Rustc, Bun, and linker custody",
        failures,
    )
    glibc = package_steps.get("Verify the fixed Linux glibc floor", "")
    require(
        "if: startsWith(matrix.target, 'linux-')" in glibc
        and "python candidate-a/cli/ci/check_linux_glibc.py" in glibc
        and "python candidate-b/cli/ci/check_linux_glibc.py" in glibc
        and glibc.count("check_linux_glibc.py") == 2
        and glibc.count('--readelf "$READELF_INPUT"') == 2
        and "READELF_INPUT: ${{ steps.exact-linux-tools.outputs.readelf }}" in glibc
        and "--rust-binary candidate-a/cli/rust/target/release/prose" in glibc
        and "--bun-binary candidate-a/cli/bun/dist/prose" in glibc
        and "--rust-binary candidate-b/cli/rust/target/release/prose" in glibc
        and "--bun-binary candidate-b/cli/bun/dist/prose" in glibc,
        "alpha: both Linux cohorts must pass the fixed ELF glibc-floor admission",
        failures,
    )
    packaging = package_steps.get("Package two functional-alpha cohorts", "")
    require(
        "python candidate-a/cli/ci/package_local.py" in packaging
        and "python candidate-b/cli/ci/package_local.py" in packaging
        and packaging.count("package_local.py") == 2
        and "--out alpha-package" in packaging
        and '--out "$RUNNER_TEMP/openprose-repro/package-b"' in packaging
        and 'PACKAGE_PLATFORM_ARGS+=(--readelf "$READELF_INPUT")' in packaging
        and packaging.count('"${PACKAGE_PLATFORM_ARGS[@]}"') == 2
        and "READELF_INPUT: ${{ steps.exact-linux-tools.outputs.readelf }}" in packaging
        and "--rust-binary candidate-a/cli/rust/target/release/prose" in packaging
        and "--bun-binary candidate-a/cli/bun/dist/prose" in packaging
        and '--image-manifest "candidate-a/$IMAGE_MANIFEST"' in packaging
        and "--rust-binary candidate-b/cli/rust/target/release/prose" in packaging
        and "--bun-binary candidate-b/cli/bun/dist/prose" in packaging
        and '--image-manifest "candidate-b/$IMAGE_MANIFEST"' in packaging,
        "alpha: both builds must be independently packaged without copying a cohort",
        failures,
    )
    reproducibility = package_steps.get(
        "Capture receipts and verify exact reproducibility", ""
    )
    require(
        reproducibility.count(
            '"$PYTHON_TOOL" control/cli/ci/reproducible_release.py capture'
        )
        == 2
        and "candidate-a/cli/ci/reproducible_release.py" not in reproducibility
        and "candidate-b/cli/ci/reproducible_release.py" not in reproducibility
        and '--package "$GITHUB_WORKSPACE/alpha-package"' in reproducibility
        and '--package "$RUNNER_TEMP/openprose-repro/package-b"' in reproducibility
        and '--left-package "$GITHUB_WORKSPACE/alpha-package"' in reproducibility
        and '--right-package "$RUNNER_TEMP/openprose-repro/package-b"'
        in reproducibility
        and "VERIFY_ARGS=(\n            verify" in reproducibility,
        "alpha: controller-owned receipts and verification must compare the two exact packages",
        failures,
    )
    require(
        'if [[ "$PLATFORM_INPUT" == darwin-* ]]' in reproducibility
        and 'OTOOL_TOOL="$(direct_path /usr/bin/otool)"' in reproducibility
        and 'VERIFY_ARGS+=(--otool "$OTOOL_TOOL")' in reproducibility
        and 'CODESIGN_TOOL="$(direct_path /usr/bin/codesign)"' in reproducibility,
        "alpha: Darwin reproducibility admission must bind exact codesign and otool executables",
        failures,
    )
    require(
        package.count("Path(sys.argv[1]).resolve(strict=True)") == 4
        and all(
            marker in reproducibility
            for marker in (
                'PYTHON_TOOL="$(direct_path',
                'CARGO_TOOL="$(direct_path',
                'RUSTC_TOOL="$(direct_path',
                'BUN_TOOL="$(direct_path',
                'NODE_TOOL="$(direct_path',
                'NPM_TOOL="$(direct_path',
                'LINKER_TOOL="$(direct_path',
                'test -n "${ImageOS:-}"',
                'test -n "${ImageVersion:-}"',
                "READELF_INPUT: ${{ steps.exact-linux-tools.outputs.readelf }}",
                'test "$(direct_path "$READELF_INPUT")" = "$READELF_INPUT"',
                '--tool "readelf=$READELF_INPUT" --tool-version "readelf=$SYSTEM_TOOL_VERSION"',
                'VERIFY_ARGS+=(--readelf "$READELF_INPUT")',
            )
        ),
        "alpha: canonical receipts require direct tools and exact hosted-runner identity",
        failures,
    )
    upload_offset = package.find("uses: actions/upload-artifact@")
    verify_offset = package.find(
        '"$PYTHON_TOOL" control/cli/ci/reproducible_release.py "${VERIFY_ARGS[@]}"'
    )
    require(
        package.count("actions/upload-artifact@") == 1
        and verify_offset >= 0
        and upload_offset > verify_offset
        and "name: alpha-package-${{ matrix.target }}\n          path: alpha-package"
        in package
        and "$RUNNER_TEMP/openprose-repro" not in package[upload_offset:],
        "alpha: only cohort A may be uploaded, strictly after reproducibility admission",
        failures,
    )
    require(
        all("${{" not in script for script in run_scripts(package)),
        "alpha: package shell bodies must receive expressions only through env or action inputs",
        failures,
    )
    require(
        re.search(
            r"(?m)^\s*(?:strip|objcopy|touch|install_name_tool|codesign)\b", package
        )
        is None,
        "alpha: package job must not normalize, mutate, or re-sign built bytes",
        failures,
    )
    require(
        "openProseExecuted" not in alpha
        and 'manifest["releaseEligible"] is False' in assemble
        and 'manifest["publicationAuthorized"] is False' in assemble,
        "alpha: assembly does not enforce non-release claims",
        failures,
    )
    require(
        'image_manifest["purpose"] == "functional-alpha-placeholder"' in alpha,
        "alpha: image purpose is not rebound from source",
        failures,
    )
    require(
        'manifest["image"] == expected_image' in alpha,
        "alpha: package image identity is not exact",
        failures,
    )
    require(
        'manifest["platform"] == expected_platforms[target]' in alpha,
        "alpha: target is not bound to package platform",
        failures,
    )
    require(
        'runtime["minimumGlibc"] == "2.34"' in alpha
        and 'runtime["executionEvidence"] == "ubuntu-22.04-only"' in alpha
        and 'set(runtime["requiredGlibcMaximum"]) == {"rust", "bun"}' in alpha
        and 'tuple(int(part) for part in required.split(".")) <= (2, 34)' in alpha
        and 'manifest["linuxRuntime"] == "not-applicable"' in alpha,
        "alpha: assembled manifests must retain the fixed Linux runtime floor",
        failures,
    )
    require(
        "set(records) == expected_records" in alpha
        and 'expected_records | {"SHA256SUMS"}' in alpha,
        "alpha: package inventory is not closed",
        failures,
    )
    require(
        alpha.count(
            "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)-alpha\\.(0|[1-9][0-9]*)$"
        )
        == 4,
        "alpha: version input must be numbered alpha SemVer at dispatch, build, and draft boundaries",
        failures,
    )
    admit = blocks.get("admit", "")
    require(
        "alpha_package_admission.py admit" in admit,
        "alpha: exact-package admission command is missing",
        failures,
    )
    require(
        "name: alpha-package-${{ matrix.target }}" in admit,
        "alpha: admission does not download one exact target package",
        failures,
    )
    require(
        "name: alpha-admission-${{ matrix.target }}" in admit,
        "alpha: admission report is not uploaded",
        failures,
    )
    require(
        "ref: ${{ github.sha }}\n          path: control\n          persist-credentials: false"
        in assemble
        and "ref: ${{ inputs.source_sha }}\n          path: candidate\n          persist-credentials: false"
        in assemble
        and "python control/cli/ci/alpha_package_admission.py verify-report" in assemble
        and '--image-manifest "candidate/$IMAGE_MANIFEST"' in assemble
        and "python candidate/cli/ci/alpha_package_admission.py" not in assemble,
        "alpha: read-only assembly must verify candidate data with controller-owned admission code",
        failures,
    )
    require(
        "pattern: alpha-admission-*" in assemble,
        "alpha: assembly does not download admission reports",
        failures,
    )
    require(
        'place(f"{target}-alpha-admission.json"' in assemble,
        "alpha: admission reports are not retained in release assets",
        failures,
    )
    require(
        "name: alpha-release-assets" in assemble
        and "path: release-assets" in assemble
        and "actions/upload-artifact@" in assemble,
        "alpha: read-only assembly artifact is missing",
        failures,
    )
    assemble_steps = workflow_step_blocks(assemble)
    admission = named_step_blocks(assemble).get(
        "Revalidate exact package-admission reports", ""
    )
    require(
        exact_shell_line(admission, source_github_equality),
        "alpha: downstream admission must remain bound to the dispatched controller identity",
        failures,
    )
    assembly_indices = [
        index
        for index, step in enumerate(assemble_steps)
        if "- name: Revalidate and assemble exact alpha assets" in step
    ]
    attestation_indices = [
        index
        for index, step in enumerate(assemble_steps)
        if step_uses(step) == "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6"
    ]
    upload_indices = [
        index
        for index, step in enumerate(assemble_steps)
        if step_uses(step)
        == "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        and step_with_values(step).get("name") == "alpha-release-assets"
        and step_with_values(step).get("path") == "release-assets"
    ]
    exact_attestation = (
        len(attestation_indices) == 1
        and "- name: Attest exact closed release assets"
        in assemble_steps[attestation_indices[0]]
        and step_with_values(assemble_steps[attestation_indices[0]])
        == {"subject-path": "release-assets/*"}
    )
    require(
        alpha.count("actions/attest@") == 1
        and len(assembly_indices) == 1
        and len(attestation_indices) == 1
        and len(upload_indices) == 1
        and exact_attestation
        and attestation_indices[0] == assembly_indices[0] + 1
        and upload_indices[0] == attestation_indices[0] + 1,
        "alpha: exact closed release assets must be attested immediately after assembly and before upload",
        failures,
    )
    require(
        "push-to-registry:" not in alpha
        and "npm publish" not in alpha
        and "cargo publish" not in alpha
        and "gh release upload" not in alpha,
        "alpha: attestation must not add an artifact or package publication route",
        failures,
    )
    draft = blocks.get("draft", "")
    require(
        "name: alpha-release-assets" in draft
        and "path: release-assets" in draft
        and "actions/download-artifact@" in draft,
        "alpha: privileged draft does not consume the closed assembly artifact",
        failures,
    )
    require(
        "ref: ${{ github.sha }}" in draft
        and "path: control" in draft
        and "fetch-depth: 0" in draft
        and "ref: ${{ inputs.source_sha }}" not in draft,
        "alpha: privileged draft must check out only the workflow controller",
        failures,
    )
    require(
        "candidate/cli/ci/" not in draft
        and "alpha_package_admission.py" not in draft
        and "pattern: alpha-package-*" not in draft
        and "pattern: alpha-admission-*" not in draft,
        "alpha: privileged draft must not execute or directly consume candidate-controlled package inputs",
        failures,
    )
    draft_steps = named_step_blocks(draft)
    render = draft_steps.get("Render one exact functional-alpha release body", "")
    create = draft_steps.get(
        "Create or resume the exact unpublished GitHub draft prerelease", ""
    )
    retain_authority = draft_steps.get("Retain immutable draft handoff authority", "")
    handoff_summary = draft_steps.get("Publish sanitized draft handoff summary", "")
    require(
        "python control/cli/ci/render_release_notes.py" in render
        and '--functional-alpha-version "$VERSION_INPUT"' in render
        and '--output "$RUNNER_TEMP/openprose-alpha-release-notes.md"' in render
        and draft.count("render_release_notes.py") == 1
        and "RELEASE_NOTES: |" not in draft,
        "alpha: release notes must be rendered once by controller-owned code",
        failures,
    )
    try:
        notes_start = notes_renderer.index("def render_functional_alpha_notes(")
        notes_end = notes_renderer.index("\ndef parser(", notes_start)
        functional_alpha_renderer = notes_renderer[notes_start:notes_end]
    except ValueError:
        functional_alpha_renderer = ""
    quickstart_markers = (
        "## OpenProse CLI functional alpha",
        "### Choose an artifact",
        "openprose-prose-cli-rust-__VERSION__-<platform>.tar.gz",
        "openprose-prose-cli-bun-__VERSION__-<platform>.tar.gz",
        "macOS 13 or newer",
        "bun-darwin-x64-baseline",
        "bun-linux-x64-baseline",
        "bun-darwin-arm64",
        "bun-linux-arm64",
        "bunRuntime",
        "Node.js 22.22.3 consumer compatibility floor",
        "Release CI and admission use exactly Node.js 24.20.0",
        "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1",
        "official versioned prime-agent release tarball",
        "openai-codex/gpt-5.4",
        "replace it with a fully-qualified provider/model exposed by your selected harness login",
        "SHA256SUMS",
        "missing or duplicate checksum entry",
        'awk -v name="$ASSET"',
        'gh attestation verify "$ASSET" --repo openprose/prose',
        "including the aggregate `SHA256SUMS` asset",
        "GitHub Actions build provenance",
        "does not sign or notarize binaries",
        'ARCHIVE="openprose-prose-cli-$IMPLEMENTATION-__VERSION__-$PLATFORM.tar.gz"',
        'INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-$IMPLEMENTATION-$PLATFORM"',
        'INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"',
        'test ! -e "$INSTALL_PREFIX"',
        "test ! -e hello.prose.md",
        'npm install --global --offline --ignore-scripts --prefix "$INSTALL_PREFIX"',
        'PROSE="$INSTALL_PREFIX/bin/prose"',
        'EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
        'GATEKEEPER_PROSE="$PROSE"',
        'GATEKEEPER_PROSE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli-$PLATFORM/bin/prose"',
        'xattr -d com.apple.quarantine "$GATEKEEPER_PROSE"',
        '"$PROSE" cli harness list',
        '"$PROSE" cli harness use codex',
        '"$PROSE" cli harness use prime --model openai-codex/gpt-5.4 --auth-profile prime-harness-login',
        '"$PROSE" cli harness use omp --model openai-codex/gpt-5.4 --auth-profile omp-harness-login',
        '"$PROSE" cli doctor',
        '"$PROSE" run hello.prose.md',
        "never opens a TUI",
        "does not execute the OpenProse language",
    )
    require(
        notes_renderer.count("def render_functional_alpha_notes(") == 1
        and all(marker in functional_alpha_renderer for marker in quickstart_markers),
        "alpha: controller release-note renderer must retain the complete nonsemantic quickstart",
        failures,
    )
    require(
        all(
            forbidden not in functional_alpha_renderer
            for forbidden in (
                "$HOME/.local/bin/prose",
                "PROSE=prose",
                "npm root --global",
            )
        ),
        "alpha: release-note quickstart must not use an unversioned install or ambient PATH candidate",
        failures,
    )
    require(
        'test "$GITHUB_REF" = "refs/heads/main"' in create
        and '[[ "$SOURCE_SHA_INPUT" =~ ^[0-9a-f]{40}$ ]]' in create
        and 'test "$(git -C control rev-parse HEAD)" = "$GITHUB_SHA"' in create
        and exact_shell_line(create, source_github_equality),
        "alpha: privileged draft must run from main at the exact dispatched controller identity",
        failures,
    )
    require(
        len(
            re.findall(rf"(?m)^[ \t]*{re.escape(source_github_equality)}[ \t]*$", alpha)
        )
        == 4
        and len(
            re.findall(
                rf"(?m)^[ \t]*{re.escape(source_control_equality)}[ \t]*$", alpha
            )
        )
        == 2
        and "merge-base --is-ancestor" not in alpha
        and alpha.find(source_github_equality) < alpha.find("actions/attest@"),
        "alpha: source identity must use only exact dispatch, physical-root, pre-attestation, and downstream equality checks",
        failures,
    )
    lineage_offset = create.find(
        'python control/cli/ci/check_registry_lineage.py --authority "control/$NPM_REGISTRY_LINEAGE" --version "$VERSION_INPUT"'
    )
    helper_offset = create.find("python control/cli/ci/create_draft_release.py")
    require(
        lineage_offset >= 0
        and helper_offset > lineage_offset
        and "candidate/cli/ci/check_registry_lineage.py" not in create,
        "alpha: controller-owned npm lineage admission must precede the draft mutation",
        failures,
    )
    require(
        "python control/cli/ci/create_draft_release.py" in create
        and '--repository "$GITHUB_REPOSITORY"' in create
        and '--version "$VERSION_INPUT"' in create
        and '--source-sha "$SOURCE_SHA_INPUT"' in create
        and '--control-sha "$CONTROL_SHA"' in create
        and "--assembly release-assets" in create
        and "--release-kind functional-alpha" in create
        and '--release-notes "$RUNNER_TEMP/openprose-alpha-release-notes.md"' in create
        and '--workflow-run-id "$GITHUB_RUN_ID"' in create
        and '--workflow-run-attempt "$GITHUB_RUN_ATTEMPT"' in create
        and '--authority-output "$AUTHORITY_OUTPUT"' in create
        and create.count("create_draft_release.py") == 1,
        "alpha: privileged draft must use the controller-owned immutable reconciliation helper",
        failures,
    )
    exact_authority_artifact = (
        "openprose-cli-alpha-draft-authority-run-${{ github.run_id }}-attempt-"
        "${{ github.run_attempt }}"
    )
    exact_authority_path = (
        "${{ runner.temp }}/openprose-alpha-draft-authority/"
        "alpha-draft-authority.json"
    )
    require(
        tuple(draft_steps)
        == (
            "Render one exact functional-alpha release body",
            "Create or resume the exact unpublished GitHub draft prerelease",
            "Retain immutable draft handoff authority",
            "Publish sanitized draft handoff summary",
        )
        and step_env_values(create)
        == {
            "GITHUB_TOKEN": "${{ github.token }}",
            "VERSION_INPUT": "${{ inputs.version }}",
            "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
            "CONTROL_SHA": "${{ github.sha }}",
        }
        and alpha.count("${{ github.token }}") == 1
        and 'AUTHORITY_DIRECTORY="$RUNNER_TEMP/openprose-alpha-draft-authority"'
        in create
        and create.count('mkdir -m 700 "$AUTHORITY_DIRECTORY"') == 1
        and "mkdir -p" not in create
        and 'AUTHORITY_OUTPUT="$AUTHORITY_DIRECTORY/alpha-draft-authority.json"'
        in create
        and create.find('mkdir -m 700 "$AUTHORITY_DIRECTORY"')
        < create.find("python control/cli/ci/create_draft_release.py")
        and step_uses(retain_authority)
        == "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"]
        and step_with_values(retain_authority)
        == {
            "name": exact_authority_artifact,
            "path": exact_authority_path,
            "if-no-files-found": "error",
            "retention-days": "90",
        },
        "alpha: draft must create one fresh immutable authority and retain only that exact JSON for 90 days",
        failures,
    )
    exact_summary_environment = {
        "ARTIFACT_NAME": exact_authority_artifact,
        "AUTHORITY_INPUT": exact_authority_path,
    }
    exact_summary_script = (
        "|\n"
        "          set -euo pipefail\n"
        '          python - "$AUTHORITY_INPUT" "$ARTIFACT_NAME" "$GITHUB_STEP_SUMMARY" <<\'PY\'\n'
        "          import hashlib\n"
        "          import json\n"
        "          from pathlib import Path\n"
        "          import re\n"
        "          import sys\n\n"
        "          encoded = Path(sys.argv[1]).read_bytes()\n"
        "          authority = json.loads(encoded)\n"
        "          artifact_name = sys.argv[2]\n"
        '          release_id = authority.get("releaseId")\n'
        "          if (\n"
        '              authority.get("schema") != "openprose.alpha-draft-authority/1"\n'
        '              or authority.get("publicationAuthorized") is not False\n'
        "              or not isinstance(release_id, int)\n"
        "              or isinstance(release_id, bool)\n"
        "              or release_id <= 0\n"
        "              or re.fullmatch(\n"
        '                  r"openprose-cli-alpha-draft-authority-run-[1-9][0-9]*-attempt-[1-9][0-9]*",\n'
        "                  artifact_name,\n"
        "              )\n"
        "              is None\n"
        "          ):\n"
        '              raise SystemExit("functional-alpha draft handoff summary input is invalid")\n'
        "          digest = hashlib.sha256(encoded).hexdigest()\n"
        "          summary = (\n"
        '              "## Functional-alpha draft handoff\\n\\n"\n'
        '              f"- Release ID: `{release_id}`\\n"\n'
        '              f"- Authority SHA-256: `{digest}`\\n"\n'
        '              f"- Retained authority artifact: `{artifact_name}`\\n"\n'
        '              "- Candidate bytes remain unauthorized; download the named retained artifact "\n'
        '              "for the immutable promotion handoff.\\n"\n'
        "          )\n"
        '          with Path(sys.argv[3]).open("a", encoding="utf-8", newline="\\n") as output:\n'
        "              output.write(summary)\n"
        "          PY"
    )
    draft_workflow_steps = workflow_step_blocks(draft)
    create_indices = [
        index
        for index, step in enumerate(draft_workflow_steps)
        if "- name: Create or resume the exact unpublished GitHub draft prerelease"
        in step
    ]
    retain_indices = [
        index
        for index, step in enumerate(draft_workflow_steps)
        if "- name: Retain immutable draft handoff authority" in step
    ]
    summary_indices = [
        index
        for index, step in enumerate(draft_workflow_steps)
        if "- name: Publish sanitized draft handoff summary" in step
    ]
    require(
        step_env_values(handoff_summary) == exact_summary_environment
        and handoff_summary.count("        shell: bash") == 1
        and run_scripts(handoff_summary) == (exact_summary_script,)
        and len(create_indices) == len(retain_indices) == len(summary_indices) == 1
        and retain_indices[0] == create_indices[0] + 1
        and summary_indices[0] == retain_indices[0] + 1
        and artifact_action_steps(draft, "actions/upload-artifact")
        == ((exact_authority_artifact, exact_authority_path),),
        "alpha: retained authority must precede one exact token/path/body-free digest handoff summary",
        failures,
    )
    helper_create_offset = draft_helper.find("def create_draft_release(")
    alpha_loader_offset = draft_helper.find("def load_alpha_assembly(")
    full_loader_offset = draft_helper.find("def load_assembly(")
    helper_create = (
        draft_helper[helper_create_offset:] if helper_create_offset >= 0 else ""
    )
    alpha_loader = (
        draft_helper[alpha_loader_offset:full_loader_offset]
        if 0 <= alpha_loader_offset < full_loader_offset
        else ""
    )
    require(
        'tag = f"cli-v{version}"' in helper_create
        and 'release_name = f"OpenProse CLI v{version} functional alpha"'
        in helper_create
        and "prerelease = True" in helper_create
        and '"draft": True' in helper_create
        and '"publicationAuthorized": False' in helper_create
        and "release_notes = _capture_release_notes(release_notes_path)"
        in helper_create
        and helper_create.count("_resolve_existing_tag(") == 2
        and helper_create.count("_discover_draft(") == 2
        and "body = _recapture_asset(asset)" in helper_create
        and "path.read_bytes()" not in helper_create
        and "path.stat()" not in helper_create,
        "alpha: helper must reauthenticate the exact tag and reconcile only captured bytes",
        failures,
    )
    require(
        'root / "SHA256SUMS", maximum=MAX_ALPHA_CHECKSUM_BYTES' in alpha_loader
        and "root / name, maximum=MAX_ALPHA_ASSET_BYTES" in alpha_loader
        and "if list(declared) != sorted(declared)" in alpha_loader
        and '!= {"SHA256SUMS", *declared}' in alpha_loader
        and "assembly digest mismatch" in alpha_loader
        and "set(declared) != required | artifact_names" in alpha_loader
        and 'manifest.get("mode") != "alpha"' in alpha_loader
        and 'manifest.get("source", {}).get("revision") != source_sha' in alpha_loader,
        "alpha: helper must parse and bind the aggregate checksum to one exact closed inventory",
        failures,
    )
    require(
        helper_create.find("_resolve_existing_tag(") >= 0
        and helper_create.find('method="POST"') >= 0
        and helper_create.find("_resolve_existing_tag(")
        < helper_create.find('method="POST"'),
        "alpha: exact pre-existing tag admission must precede every remote mutation",
        failures,
    )
    require(
        "gh release" not in draft
        and "npm publish" not in draft
        and "cargo publish" not in draft,
        "alpha: privileged draft must not bypass the closed helper or publish packages",
        failures,
    )
    require(
        re.search(r"\bgh\s+release\s+(?:edit|delete)\b", draft) is None,
        "alpha: release replacement or deletion is forbidden",
        failures,
    )
    for target, runner in {
        "linux-x64": "ubuntu-22.04",
        "linux-arm64": "ubuntu-22.04-arm",
        "darwin-arm": "macos-15",
        "darwin-x64": "macos-15-intel",
    }.items():
        require(
            f"{{target: {target}, runner: {runner}}}" in alpha,
            f"alpha: target {target}/{runner} is missing",
            failures,
        )
    require(
        "{target: win-x64," not in blocks.get("package", ""),
        "alpha: unadmitted Windows runtime package is forbidden",
        failures,
    )
    return failures


def _audit_promotion_legacy(promotion: str) -> list[str]:
    """Keep the protected functional-alpha promotion workflow narrow."""

    failures: list[str] = []
    jobs_offset = promotion.find("\njobs:\n")
    header = promotion[:jobs_offset] if jobs_offset >= 0 else promotion
    require(
        "permissions:\n  contents: read\n\nconcurrency:" in header
        and "id-token:" not in header
        and "contents: write" not in header,
        "promotion: top-level permissions must remain contents-read only",
        failures,
    )
    require(
        header.count("  workflow_dispatch:") == 1,
        "promotion: the manual trigger is missing or duplicated",
        failures,
    )
    for forbidden in ("pull_request:", "push:", "schedule:", "release:"):
        require(
            forbidden not in header,
            f"promotion: forbidden trigger {forbidden}",
            failures,
        )
    operation_match = re.search(
        r"(?ms)^      operation:\s*$.*?^        options:\s*$\n"
        r"(?P<options>(?:^          - [^\n]+\n)+)^      version:\s*$",
        header,
    )
    operation_options = (
        re.findall(r"(?m)^          - ([a-z-]+)\s*$", operation_match["options"])
        if operation_match is not None
        else []
    )
    require(
        operation_options
        == ["bootstrap", "stage-platforms", "stage-meta", "settle-and-promote"]
        and "required: true" in (operation_match.group(0) if operation_match else "")
        and "type: choice" in (operation_match.group(0) if operation_match else ""),
        "promotion: operation choices must remain the exact four protected transitions",
        failures,
    )
    require(
        re.findall(r"(?m)^      ([a-z][a-z0-9_]*)\s*:\s*$", header)
        == ["operation", "version", "source_sha", "release_id", "confirmation"],
        "promotion: manual inputs must remain operation, version, source SHA, release ID, and confirmation",
        failures,
    )
    require(
        promotion.count("group: openprose-cli-alpha-promotion-${{ inputs.version }}")
        == 1
        and re.search(r"(?m)^  cancel-in-progress: false\s*$", promotion) is not None,
        "promotion: concurrent transitions must serialize without cancellation by exact version",
        failures,
    )
    require(
        "continue-on-error" not in promotion,
        "promotion: continue-on-error is forbidden",
        failures,
    )
    check_action_pins(
        promotion,
        "promotion",
        failures,
        {
            "actions/checkout",
            "actions/setup-python",
            "actions/setup-node",
            "actions/upload-artifact",
            "actions/download-artifact",
        },
    )
    require(
        promotion.count("persist-credentials: false")
        == promotion.count("actions/checkout@")
        == 4,
        "promotion: every exact controller checkout must disable persisted credentials",
        failures,
    )
    require(
        set(re.findall(r"python-version:\s*[\"']?([^\s\"']+)", promotion))
        == {"3.10.18"},
        "promotion: the controller Python runtime must remain exact",
        failures,
    )

    blocks = job_blocks(promotion)
    operations = ("bootstrap", "stage-platforms", "stage-meta", "settle-and-promote")
    require(
        tuple(blocks) == operations,
        "promotion: job graph must remain the ordered four-operation graph",
        failures,
    )
    require(
        "needs:" not in "".join(blocks.values()),
        "promotion: independently authorized operations must not form an automatic dependency chain",
        failures,
    )
    expected_permissions = {
        "bootstrap": {
            "contents": "read",
            "attestations": "read",
            "id-token": "write",
        },
        "stage-platforms": {
            "contents": "read",
            "attestations": "read",
            "id-token": "write",
        },
        "stage-meta": {
            "contents": "read",
            "attestations": "read",
            "id-token": "write",
        },
        "settle-and-promote": {"contents": "write", "attestations": "read"},
    }
    expected_settlements = {
        "bootstrap": "openprose-alpha-bootstrap-settlement",
        "stage-platforms": "openprose-alpha-platform-stage-settlement",
        "stage-meta": "openprose-alpha-meta-stage-settlement",
        "settle-and-promote": "openprose-alpha-final-settlement",
    }
    # Digest, archive, tree, entry-point, package authentication, and the observed
    # GitHub CLI version/hash remain behavior owned by promote_alpha_release.py
    # and its tests. Workflow policy binds every external tool to that helper
    # without an alternate attestation, hash, or execution path.
    exact_helper_arguments = (
        ">-\n"
        "          python cli/ci/promote_alpha_release.py\n"
        '          --operation "$OPERATION_INPUT"\n'
        '          --repository "$GITHUB_REPOSITORY"\n'
        '          --release-id "$RELEASE_ID_INPUT"\n'
        '          --version "$VERSION_INPUT"\n'
        '          --source-sha "$SOURCE_SHA_INPUT"\n'
        '          --confirmation "$CONFIRMATION_INPUT"\n'
        '          --github-cli "$GH_TOOL"'
    )
    exact_common_environment = {
        "OPERATION_INPUT": "${{ inputs.operation }}",
        "VERSION_INPUT": "${{ inputs.version }}",
        "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
        "RELEASE_ID_INPUT": "${{ inputs.release_id }}",
        "CONFIRMATION_INPUT": "${{ inputs.confirmation }}",
        "GH_TOOL": "${{ steps.github-cli.outputs.path }}",
        "PROMOTION_SETTLEMENT": (
            "${{ runner.temp }}/openprose-alpha-promotion/"
            "npm-publication-settlement.json"
        ),
        "GITHUB_TOKEN": "${{ github.token }}",
    }
    for operation in operations:
        block = blocks.get(operation, "")
        require(
            f"if: github.ref == 'refs/heads/main' && inputs.operation == '{operation}'"
            in block,
            f"promotion: {operation} must be manual, main-only, and operation-specific",
            failures,
        )
        require(
            block.count("environment: openprose-cli-alpha-publish") == 1,
            f"promotion: {operation} must use exactly one protected publish environment",
            failures,
        )
        require(
            job_permission_values(block) == expected_permissions[operation],
            f"promotion: {operation} permissions must remain exact and least-privilege",
            failures,
        )
        require(
            re.search(r"(?m)^    runs-on: ubuntu-24\.04\s*$", block) is not None,
            f"promotion: {operation} must remain on Ubuntu 24.04",
            failures,
        )
        steps = workflow_step_blocks(block)
        resolver_steps = [
            step
            for step in steps
            if re.search(
                r"(?m)^      - name: Resolve the externally provisioned GitHub CLI verifier\s*$",
                step,
            )
        ]
        require(
            len(resolver_steps) == 1
            and resolver_steps[0].rstrip() == PROMOTION_GH_RESOLVER_STEP.rstrip(),
            f"promotion: {operation} must resolve exactly one fixed trusted GitHub CLI verifier",
            failures,
        )
        node_steps = [
            step
            for step in steps
            if (step_uses(step) or "").split("@", 1)[0] == "actions/setup-node"
        ]
        download_steps = [
            step
            for step in steps
            if re.search(
                r"(?m)^      - name: Download the exact staging-capable npm client without credentials\s*$",
                step,
            )
        ]
        if operation in PROMOTION_NPM_OPERATIONS:
            require(
                len(node_steps) == 1
                and step_uses(node_steps[0])
                == "actions/setup-node@" + ACTION_PINS["actions/setup-node"]
                and step_with_values(node_steps[0]) == {"node-version": '"24.20.0"'},
                f"promotion: {operation} must use only exact Node 24.20.0",
                failures,
            )
            require(
                len(download_steps) == 1
                and download_steps[0].rstrip() == PROMOTION_NPM_DOWNLOAD_STEP.rstrip(),
                f"promotion: {operation} must perform one exact credential-free pinned npm download",
                failures,
            )
        else:
            require(
                not node_steps
                and not download_steps
                and all(
                    token not in block
                    for token in (
                        "NPM_TARBALL",
                        "--npm-tarball",
                        "npm-11.15.0.tgz",
                        "/usr/bin/curl",
                        "actions/setup-node@",
                    )
                ),
                "promotion: settle-and-promote must have no npm tool custody",
                failures,
            )
        helper_steps = [step for step in steps if "promote_alpha_release.py" in step]
        run_steps = [step for step in steps if run_scripts(step)]
        expected_step_count = 7 if operation in PROMOTION_NPM_OPERATIONS else 5
        expected_run_count = 3 if operation in PROMOTION_NPM_OPERATIONS else 2
        if operation in PROMOTION_NPM_OPERATIONS:
            expected_step_uses = (
                "actions/checkout@" + ACTION_PINS["actions/checkout"],
                "actions/setup-python@" + ACTION_PINS["actions/setup-python"],
                None,
                "actions/setup-node@" + ACTION_PINS["actions/setup-node"],
                None,
                None,
                "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"],
            )
        else:
            expected_step_uses = (
                "actions/checkout@" + ACTION_PINS["actions/checkout"],
                "actions/setup-python@" + ACTION_PINS["actions/setup-python"],
                None,
                None,
                "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"],
            )
        require(
            len(steps) == expected_step_count
            and len(run_steps) == expected_run_count
            and tuple(step_uses(step) for step in steps) == expected_step_uses
            and len(resolver_steps) == 1
            and steps[2] == resolver_steps[0]
            and len(helper_steps) == 1
            and steps[-2] == helper_steps[0]
            and (
                operation not in PROMOTION_NPM_OPERATIONS
                or (len(download_steps) == 1 and steps[4] == download_steps[0])
            )
            and all(
                step in (*resolver_steps, *download_steps, *helper_steps)
                for step in run_steps
            ),
            f"promotion: {operation} must have only its exact resolver, helper, and permitted download shell routes",
            failures,
        )
        helper_arguments = exact_helper_arguments
        helper_environment = dict(exact_common_environment)
        if operation in PROMOTION_NPM_OPERATIONS:
            helper_arguments += '\n          --npm-tarball "$NPM_TARBALL"'
            helper_environment["NPM_TARBALL"] = PROMOTION_NPM_TARBALL_PATH
        helper_arguments += '\n          --settlement "$PROMOTION_SETTLEMENT"'
        if operation == "bootstrap":
            helper_environment[
                "NPM_TOKEN"
            ] = "${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}"
        require(
            len(helper_steps) == 1
            and run_scripts(helper_steps[0]) == (helper_arguments,)
            and step_env_values(helper_steps[0]) == helper_environment,
            f"promotion: {operation} must cross the exact controller-helper boundary once",
            failures,
        )
        require(
            artifact_action_steps(block, "actions/upload-artifact")
            == (
                (
                    expected_settlements[operation],
                    "${{ runner.temp }}/openprose-alpha-promotion/npm-publication-settlement.json",
                ),
            )
            and "if-no-files-found: warn" in block
            and "retention-days: 90" in block,
            f"promotion: {operation} must retain only its sanitized settlement",
            failures,
        )

    require(
        promotion.count(PROMOTION_NPM_DOWNLOAD_STEP) == 3
        and promotion.count(PROMOTION_NPM_TARBALL_URL) == 3
        and promotion.count("/usr/bin/curl") == 3
        and promotion.count("actions/setup-node@" + ACTION_PINS["actions/setup-node"])
        == 3
        and promotion.count('node-version: "24.20.0"') == 3
        and promotion.count(f"NPM_TARBALL: {PROMOTION_NPM_TARBALL_PATH}") == 3
        and promotion.count('--npm-tarball "$NPM_TARBALL"') == 3,
        "promotion: pinned npm download, Node custody, and helper admission must occur exactly three times",
        failures,
    )
    require(
        promotion.count("id-token: write") == 3
        and promotion.count("attestations: read") == 4
        and promotion.count("contents: write") == 1
        and "contents: write" in blocks.get("settle-and-promote", ""),
        "promotion: every stage must read attestations, only npm stages may use OIDC, and only final settlement may write contents",
        failures,
    )
    require(
        promotion.count(PROMOTION_GH_RESOLVER_STEP) == 4
        and promotion.count(
            "- name: Resolve the externally provisioned GitHub CLI verifier"
        )
        == 4
        and promotion.count("id: github-cli") == 4
        and promotion.count('GH_TOOL="$(python - /usr/bin/gh') == 4
        and promotion.count("/usr/bin/gh") == 4
        and promotion.count("Path(sys.argv[1]).resolve(strict=True)") == 4
        and promotion.count("printf 'path=%s\\n' \"$GH_TOOL\"") == 4
        and promotion.count("GH_TOOL: ${{ steps.github-cli.outputs.path }}") == 4
        and promotion.count('--github-cli "$GH_TOOL"') == 4,
        "promotion: fixed GitHub CLI resolution and the authenticated helper boundary must occur exactly four times",
        failures,
    )
    bootstrap = blocks.get("bootstrap", "")
    no_npm_token = "".join(
        blocks.get(operation, "")
        for operation in ("stage-platforms", "stage-meta", "settle-and-promote")
    )
    require(
        promotion.count("${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}") == 1
        and bootstrap.count("NPM_TOKEN: ${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}")
        == 1
        and "secrets." not in no_npm_token
        and all(
            token not in no_npm_token
            for token in ("NPM_TOKEN:", "NODE_AUTH_TOKEN:", "NPM_CONFIG_TOKEN:")
        ),
        "promotion: the bootstrap npm credential must appear once and never enter OIDC or final jobs",
        failures,
    )
    scripts = "\n".join(run_scripts(promotion))
    require(
        re.search(r"(?m)(?:^|\s)(?:\S*/)?npm(?=\s)", scripts) is None
        and re.search(
            r"(?m)(?:^|\s)(?:\S*/)?(?:wget|fetch|aria2c|httpie)(?=\s)", scripts
        )
        is None
        and "--location" not in scripts
        and re.search(r"(?:^|\s)-L(?:\s|$)", scripts) is None,
        "promotion: direct npm commands, alternate downloaders, and redirect-following are forbidden",
        failures,
    )
    require(
        not any(
            re.search(pattern, scripts)
            for pattern in (
                r"\bcommand\s+-v\s+gh\b",
                r"\bwhich\s+gh\b",
                r"\bwhereis\s+gh\b",
                r"\btype\s+(?:-P\s+)?gh\b",
                r"\bgh\s+attestation\b",
                r"\b(?:brew|apt|apt-get|dnf|yum|snap)\s+install\b[^\n]*\bgh\b",
            )
        )
        and not any(
            token in promotion
            for token in (
                "GH_TOOL_VERSION",
                "GH_TOOL_SHA",
                "GITHUB_CLI_VERSION",
                "GITHUB_CLI_SHA",
            )
        ),
        "promotion: alternate GitHub CLI lookup, installation, attestation, and workflow-owned version/hash routes are forbidden",
        failures,
    )
    require(
        not any(
            token in promotion
            for token in (
                "NPM_TARBALL_SHA",
                "NPM_TARBALL_INTEGRITY",
                "npm.sha1",
                "npm.sha256",
                "npm.sha512",
                "sha1sum",
                "sha256sum",
                "sha512sum",
            )
        ),
        "promotion: npm archive hash authority must remain inside the authenticated helper",
        failures,
    )
    require(
        not any(
            re.search(pattern, scripts)
            for pattern in (
                r"\bnpm\s+(?:publish|unpublish|pack|dist-tag|access|owner|token|deprecate)\b",
                r"\bgh\s+release\b",
                r"\b(?:cargo|bun)\s+(?:build|publish|pack)\b",
                r"\b(?:build_local|package_local|run_local)\.py\b",
                r"(?:^|\s)--otp(?:\s|=)",
            )
        ),
        "promotion: workflow shell steps must not build, repack, publish, approve, or mutate outside the helper",
        failures,
    )
    require(
        promotion.count("python cli/ci/promote_alpha_release.py") == 4
        and "candidate/cli/ci/promote_alpha_release.py" not in promotion,
        "promotion: each operation must invoke only the checked-out controller helper",
        failures,
    )
    return failures


def audit_promotion(promotion: str) -> list[str]:
    """Bind W180/W181 custody to the protected alpha promotion graph."""

    failures: list[str] = []
    jobs_offset = promotion.find("\njobs:\n")
    header = promotion[:jobs_offset] if jobs_offset >= 0 else promotion
    require(
        "permissions:\n  contents: read\n\nconcurrency:" in header
        and "id-token:" not in header
        and "contents: write" not in header,
        "promotion: top-level permissions must remain contents-read only",
        failures,
    )
    require(
        header.count("  workflow_dispatch:") == 1
        and not any(
            trigger in header
            for trigger in ("pull_request:", "push:", "schedule:", "release:")
        ),
        "promotion: only manual dispatch is permitted",
        failures,
    )
    operation_match = re.search(
        r"(?ms)^      operation:\s*$.*?^        options:\s*$\n"
        r"(?P<options>(?:^          - [^\n]+\n)+)^      version:\s*$",
        header,
    )
    operation_options = (
        re.findall(r"(?m)^          - ([a-z-]+)\s*$", operation_match["options"])
        if operation_match is not None
        else []
    )
    require(
        operation_options
        == ["bootstrap", "stage-platforms", "stage-meta", "settle-and-promote"],
        "promotion: operation choices must remain exact",
        failures,
    )
    require(
        re.findall(r"(?m)^      ([a-z][a-z0-9_]*)\s*:\s*$", header)
        == [
            "operation",
            "version",
            "source_sha",
            "release_id",
            "draft_authority_run_id",
            "draft_authority_run_attempt",
            "draft_authority_sha256",
            "confirmation",
        ],
        "promotion: dispatch must identify the exact immutable draft authority",
        failures,
    )
    require(
        promotion.count("group: openprose-cli-alpha-promotion-${{ inputs.version }}")
        == 1
        and re.search(r"(?m)^  cancel-in-progress: false\s*$", promotion) is not None,
        "promotion: concurrent transitions must serialize by exact version",
        failures,
    )
    require(
        "continue-on-error" not in promotion,
        "promotion: continue-on-error is forbidden",
        failures,
    )
    check_action_pins(
        promotion,
        "promotion",
        failures,
        {
            "actions/checkout",
            "actions/setup-python",
            "actions/setup-node",
            "actions/upload-artifact",
            "actions/download-artifact",
        },
    )
    blocks = job_blocks(promotion)
    operations = ("bootstrap", "stage-platforms", "stage-meta", "settle-and-promote")
    require(
        tuple(blocks) == ("preflight", "preverify-attestations", *operations),
        "promotion: job graph must be preflight, one verifier, then four transitions",
        failures,
    )
    require(
        promotion.count("persist-credentials: false")
        == promotion.count("actions/checkout@")
        == 5,
        "promotion: every controller checkout must disable persisted credentials",
        failures,
    )
    require(
        set(re.findall(r"python-version:\s*[\"']?([^\s\"']+)", promotion))
        == {"3.10.18"},
        "promotion: the controller Python runtime must remain exact",
        failures,
    )

    preflight = blocks.get("preflight", "")
    preflight_steps = workflow_step_blocks(preflight)
    require(
        "needs:" not in preflight
        and "if:" not in preflight.split("    steps:", 1)[0]
        and "environment:" not in preflight
        and job_permission_values(preflight) == {"actions": "read", "contents": "read"},
        "promotion: preflight must run unconditionally with read-only repository authority",
        failures,
    )
    require(
        len(preflight_steps) == 5
        and 'test "$GITHUB_REF" = "refs/heads/main"' in preflight_steps[0]
        and "EXPECTED_CONFIRMATION=" in preflight_steps[0]
        and all(
            token in preflight_steps[0]
            for token in (
                "DRAFT_AUTHORITY_RUN_ID_INPUT",
                "DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT",
                "DRAFT_AUTHORITY_SHA256_INPUT",
                "CONFIRMATION_INPUT",
            )
        ),
        "promotion: preflight must reject non-main and malformed or unconfirmed inputs",
        failures,
    )
    producer_steps = [
        step
        for step in preflight_steps
        if re.search(
            r"(?m)^      - name: Authenticate the successful functional-alpha draft producer\s*$",
            step,
        )
    ]
    producer_script = (
        run_scripts(producer_steps[0])[0]
        if len(producer_steps) == 1 and len(run_scripts(producer_steps[0])) == 1
        else ""
    )
    require(
        len(producer_steps) == 1
        and preflight_steps[1] == producer_steps[0]
        and step_env_values(producer_steps[0])
        == {
            "GITHUB_TOKEN": "${{ github.token }}",
            "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
            "DRAFT_AUTHORITY_RUN_ID_INPUT": "${{ inputs.draft_authority_run_id }}",
            "DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT": (
                "${{ inputs.draft_authority_run_attempt }}"
            ),
            "RUN_METADATA": (
                "${{ runner.temp }}/openprose-alpha-draft-producer-run.json"
            ),
        }
        and all(
            token in producer_script
            for token in (
                "set -euo pipefail",
                'test ! -e "$RUN_METADATA"',
                'GH_TOOL="$(python - /usr/bin/gh',
                "Path(sys.argv[1]).resolve(strict=True)",
                "not stat.S_ISREG(metadata.st_mode)",
                "not os.access(path, os.X_OK)",
                '[[ "$GH_TOOL" =~ ^/[A-Za-z0-9._+/@:-]+$ ]]',
                '"$GH_TOOL" api --method GET',
                '"/repos/$GITHUB_REPOSITORY/actions/runs/$DRAFT_AUTHORITY_RUN_ID_INPUT/attempts/$DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT"',
                'chmod 0400 "$RUN_METADATA"',
                "stat.S_ISLNK(before.st_mode)",
                "not stat.S_ISREG(before.st_mode)",
                "before.st_size > 1048576",
                "identity(before) != identity(after)",
                "not isinstance(metadata, dict)",
                'metadata.get("id") != run_id',
                'metadata.get("run_attempt") != run_attempt',
                'metadata.get("path")',
                '!= ".github/workflows/openprose-cli-alpha-release.yml"',
                'metadata.get("event") != "workflow_dispatch"',
                'metadata.get("status") != "completed"',
                'metadata.get("conclusion") != "success"',
                'metadata.get("head_sha") != os.environ["SOURCE_SHA_INPUT"]',
                'repository.get("full_name") != os.environ["GITHUB_REPOSITORY"]',
            )
        )
        and "command -v gh" not in producer_steps[0]
        and "GITHUB_OUTPUT" not in producer_steps[0]
        and "GITHUB_STEP_SUMMARY" not in producer_steps[0]
        and "secrets." not in producer_steps[0],
        "promotion: producer run metadata must authenticate before artifact custody",
        failures,
    )
    producer_downloads = artifact_action_steps(preflight, "actions/download-artifact")
    require(
        producer_downloads
        == (
            (
                "openprose-cli-alpha-draft-authority-run-${{ inputs.draft_authority_run_id }}-attempt-${{ inputs.draft_authority_run_attempt }}",
                "${{ runner.temp }}/openprose-alpha-draft-authority",
            ),
        )
        and "github-token: ${{ github.token }}" in preflight
        and "repository: ${{ github.repository }}" in preflight
        and "run-id: ${{ inputs.draft_authority_run_id }}" in preflight
        and step_uses(preflight_steps[2])
        == "actions/download-artifact@" + ACTION_PINS["actions/download-artifact"]
        and "hashlib.sha256(encoded).hexdigest() != sys.argv[2]" in preflight_steps[3],
        "promotion: preflight must fetch and authenticate one explicitly identified producer artifact",
        failures,
    )
    authority_custody_name = (
        "openprose-alpha-draft-authority-custody-run-${{ github.run_id }}-attempt-"
        "${{ github.run_attempt }}"
    )
    require(
        artifact_action_steps(preflight, "actions/upload-artifact")
        == (
            (
                authority_custody_name,
                "${{ runner.temp }}/openprose-alpha-draft-authority/alpha-draft-authority.json",
            ),
        )
        and step_uses(preflight_steps[4])
        == "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"]
        and "if-no-files-found: error" in preflight
        and "retention-days: 90" in preflight,
        "promotion: preflight must preserve only the exact authority file for this run",
        failures,
    )

    verifier = blocks.get("preverify-attestations", "")
    verifier_steps = workflow_step_blocks(verifier)
    require(
        re.search(r"(?m)^    needs: preflight\s*$", verifier) is not None
        and "environment:" not in verifier
        and job_permission_values(verifier)
        == {"contents": "read", "attestations": "read"}
        and all(
            token not in verifier
            for token in (
                "id-token:",
                "contents: write",
                "NPM_TOKEN",
                "NPM_TARBALL",
                "--npm-tarball",
                "PROMOTION_SETTLEMENT",
                "--settlement",
                "secrets.",
            )
        ),
        "promotion: the sole attestation job must have only contents/attestations read",
        failures,
    )
    verifier_helpers = [
        step for step in verifier_steps if "promote_alpha_release.py" in step
    ]
    require(
        len(verifier_helpers) == 1
        and all(
            token in verifier_helpers[0]
            for token in (
                "--mode verify-attestations",
                '--workflow-run-id "$GITHUB_RUN_ID"',
                '--workflow-run-attempt "$GITHUB_RUN_ATTEMPT"',
                '--draft-authority "$DRAFT_AUTHORITY"',
                '--draft-authority-sha256 "$DRAFT_AUTHORITY_SHA256_INPUT"',
                '--draft-authority-run-id "$DRAFT_AUTHORITY_RUN_ID_INPUT"',
                '--draft-authority-run-attempt "$DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT"',
                '--attestation-evidence "$ATTESTATION_EVIDENCE"',
                '--github-cli "$GH_TOOL"',
            )
        )
        and verifier.count(PROMOTION_GH_RESOLVER_STEP) == 1
        and artifact_action_steps(verifier, "actions/download-artifact")
        == (
            (
                authority_custody_name,
                "${{ runner.temp }}/openprose-alpha-draft-authority",
            ),
        ),
        "promotion: the sole verifier must consume W181 and generate W180 exactly once",
        failures,
    )
    evidence_name = (
        "openprose-alpha-attestation-evidence-run-${{ github.run_id }}-attempt-"
        "${{ github.run_attempt }}"
    )
    require(
        "evidence_sha256: ${{ steps.evidence-identity.outputs.sha256 }}" in verifier
        and "sha256={hashlib.sha256(encoded).hexdigest()}" in verifier
        and artifact_action_steps(verifier, "actions/upload-artifact")
        == (
            (
                evidence_name,
                "${{ runner.temp }}/openprose-alpha-attestation-evidence/github-attestation-evidence.json",
            ),
        )
        and "if-no-files-found: error" in verifier
        and "retention-days: 90" in verifier,
        "promotion: same-run attestation evidence must be digest-bound and retained once",
        failures,
    )

    expected_permissions = {
        "bootstrap": {"contents": "read", "id-token": "write"},
        "stage-platforms": {"contents": "read", "id-token": "write"},
        "stage-meta": {"contents": "read", "id-token": "write"},
        "settle-and-promote": {"contents": "write"},
    }
    expected_settlements = {
        "bootstrap": "openprose-alpha-bootstrap-settlement",
        "stage-platforms": "openprose-alpha-platform-stage-settlement",
        "stage-meta": "openprose-alpha-meta-stage-settlement",
        "settle-and-promote": "openprose-alpha-final-settlement",
    }
    for operation in operations:
        block = blocks.get(operation, "")
        require(
            "needs: [preflight, preverify-attestations]" in block
            and (
                "if: needs.preverify-attestations.result == 'success' && "
                f"inputs.operation == '{operation}'"
            )
            in block
            and block.count("environment: openprose-cli-alpha-publish") == 1
            and job_permission_values(block) == expected_permissions[operation]
            and "attestations:" not in block,
            f"promotion: {operation} must depend on preverification with least privilege",
            failures,
        )
        downloads = artifact_action_steps(block, "actions/download-artifact")
        require(
            downloads
            == (
                (
                    authority_custody_name,
                    "${{ runner.temp }}/openprose-alpha-draft-authority",
                ),
                (
                    evidence_name,
                    "${{ runner.temp }}/openprose-alpha-attestation-evidence",
                ),
            ),
            f"promotion: {operation} must consume the exact W181/W180 same-run handoffs",
            failures,
        )
        helper_steps = [
            step
            for step in workflow_step_blocks(block)
            if "promote_alpha_release.py" in step
        ]
        expected_helper_environment = {
            "OPERATION_INPUT": "${{ inputs.operation }}",
            "VERSION_INPUT": "${{ inputs.version }}",
            "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
            "RELEASE_ID_INPUT": "${{ inputs.release_id }}",
            "DRAFT_AUTHORITY_RUN_ID_INPUT": "${{ inputs.draft_authority_run_id }}",
            "DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT": (
                "${{ inputs.draft_authority_run_attempt }}"
            ),
            "DRAFT_AUTHORITY_SHA256_INPUT": "${{ inputs.draft_authority_sha256 }}",
            "CONFIRMATION_INPUT": "${{ inputs.confirmation }}",
            "DRAFT_AUTHORITY": (
                "${{ runner.temp }}/openprose-alpha-draft-authority/"
                "alpha-draft-authority.json"
            ),
            "ATTESTATION_EVIDENCE": (
                "${{ runner.temp }}/openprose-alpha-attestation-evidence/"
                "github-attestation-evidence.json"
            ),
            "ATTESTATION_EVIDENCE_SHA256": (
                "${{ needs.preverify-attestations.outputs.evidence_sha256 }}"
            ),
            "PROMOTION_SETTLEMENT": (
                "${{ runner.temp }}/openprose-alpha-promotion/"
                "npm-publication-settlement.json"
            ),
            "GITHUB_TOKEN": "${{ github.token }}",
        }
        if operation in PROMOTION_NPM_OPERATIONS:
            expected_helper_environment["NPM_TARBALL"] = PROMOTION_NPM_TARBALL_PATH
        if operation == "bootstrap":
            expected_helper_environment[
                "NPM_TOKEN"
            ] = "${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}"
        require(
            len(helper_steps) == 1
            and all(
                token in helper_steps[0]
                for token in (
                    "--mode execute-transition",
                    '--workflow-run-id "$GITHUB_RUN_ID"',
                    '--workflow-run-attempt "$GITHUB_RUN_ATTEMPT"',
                    '--draft-authority "$DRAFT_AUTHORITY"',
                    '--draft-authority-sha256 "$DRAFT_AUTHORITY_SHA256_INPUT"',
                    '--draft-authority-run-id "$DRAFT_AUTHORITY_RUN_ID_INPUT"',
                    '--draft-authority-run-attempt "$DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT"',
                    '--attestation-evidence "$ATTESTATION_EVIDENCE"',
                    '--attestation-evidence-sha256 "$ATTESTATION_EVIDENCE_SHA256"',
                    '--confirmation "$CONFIRMATION_INPUT"',
                    '--settlement "$PROMOTION_SETTLEMENT"',
                )
            )
            and "--github-cli" not in helper_steps[0]
            and "GH_TOOL" not in block
            and "/usr/bin/gh" not in block
            and "gh attestation" not in block,
            f"promotion: {operation} must consume evidence without verifier custody",
            failures,
        )
        require(
            len(helper_steps) == 1
            and step_env_values(helper_steps[0]) == expected_helper_environment,
            f"promotion: {operation} helper environment must bind exact authority identities",
            failures,
        )
        node_steps = [
            step
            for step in workflow_step_blocks(block)
            if (step_uses(step) or "").split("@", 1)[0] == "actions/setup-node"
        ]
        npm_downloads = [
            step
            for step in workflow_step_blocks(block)
            if "Download the exact staging-capable npm client" in step
        ]
        if operation in PROMOTION_NPM_OPERATIONS:
            require(
                len(node_steps) == 1
                and step_with_values(node_steps[0]) == {"node-version": '"24.20.0"'}
                and len(npm_downloads) == 1
                and npm_downloads[0].rstrip() == PROMOTION_NPM_DOWNLOAD_STEP.rstrip()
                and '--npm-tarball "$NPM_TARBALL"' in helper_steps[0],
                f"promotion: {operation} must retain exact pinned npm custody",
                failures,
            )
        else:
            require(
                not node_steps
                and not npm_downloads
                and all(
                    token not in block
                    for token in ("NPM_TARBALL", "--npm-tarball", "npm-11.15.0.tgz")
                ),
                "promotion: settle-and-promote must have no npm tool custody",
                failures,
            )
        require(
            artifact_action_steps(block, "actions/upload-artifact")
            == (
                (
                    expected_settlements[operation],
                    "${{ runner.temp }}/openprose-alpha-promotion/npm-publication-settlement.json",
                ),
            )
            and "if-no-files-found: warn" in block
            and "retention-days: 90" in block,
            f"promotion: {operation} must retain only its sanitized settlement",
            failures,
        )

    bootstrap = blocks.get("bootstrap", "")
    other_mutations = "".join(blocks.get(name, "") for name in operations[1:])
    require(
        promotion.count("${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}") == 1
        and "NPM_TOKEN: ${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}" in bootstrap
        and "secrets." not in other_mutations
        and all(
            token not in other_mutations
            for token in ("NPM_TOKEN:", "NODE_AUTH_TOKEN:", "NPM_CONFIG_TOKEN:")
        ),
        "promotion: bootstrap token custody must remain isolated",
        failures,
    )
    require(
        promotion.count(PROMOTION_GH_RESOLVER_STEP) == 1
        and promotion.count("--mode verify-attestations") == 1
        and promotion.count('--github-cli "$GH_TOOL"') == 1
        and promotion.count("--mode execute-transition") == 4
        and promotion.count("attestations: read") == 1
        and promotion.count("id-token: write") == 3
        and promotion.count("contents: write") == 1
        and promotion.count("python cli/ci/promote_alpha_release.py") == 5,
        "promotion: verifier and mutation authority counts must remain disjoint and exact",
        failures,
    )
    scripts = "\n".join(run_scripts(promotion))
    require(
        re.search(r"(?m)(?:^|\s)(?:\S*/)?npm(?=\s)", scripts) is None
        and "--location" not in scripts
        and re.search(r"(?:^|\s)-L(?:\s|$)", scripts) is None
        and not any(
            re.search(pattern, scripts)
            for pattern in (
                r"\bnpm\s+(?:publish|unpublish|pack|dist-tag|access|owner|token|deprecate)\b",
                r"\bgh\s+release\b",
                r"\b(?:cargo|bun)\s+(?:build|publish|pack)\b",
                r"(?:^|\s)--otp(?:\s|=)",
            )
        ),
        "promotion: workflow shell must not gain alternate mutation or redirect routes",
        failures,
    )
    return failures


def audit_post_public(post_public: str) -> list[str]:
    """Keep post-public alpha verification read-only and identity-bound."""

    failures: list[str] = []
    jobs_offset = post_public.find("\njobs:\n")
    header = post_public[:jobs_offset] if jobs_offset >= 0 else post_public
    require(
        header.count("  workflow_dispatch:") == 1,
        "post-public: the manual trigger is missing or duplicated",
        failures,
    )
    for forbidden in ("pull_request:", "push:", "schedule:", "release:"):
        require(
            forbidden not in header,
            f"post-public: forbidden trigger {forbidden}",
            failures,
        )
    require(
        re.findall(r"(?m)^      ([a-z][a-z0-9_]*)\s*:\s*$", header)
        == [
            "repository",
            "version",
            "tag",
            "source_sha",
            "release_id",
            "draft_workflow_run_id",
            "draft_workflow_run_attempt",
            "draft_authority_artifact",
            "confirmation",
        ]
        and header.count("        required: true") == 9
        and header.count("        type: string") == 9
        and header.count("        default: openprose/prose") == 1
        and (
            "description: VERIFY PUBLIC ALPHA <repository> <version> <tag> "
            "<source_sha> <release_id>"
        )
        in header,
        "post-public: dispatch inputs and operator confirmation must remain exact",
        failures,
    )
    require(
        (
            "permissions:\n  actions: read\n  contents: read\n"
            "  attestations: read\n\nconcurrency:"
        )
        in header
        and header.count("permissions:") == 1
        and "contents: write" not in header
        and "attestations: write" not in header
        and "id-token:" not in header
        and re.search(r"(?m)^\s*packages:\s*", header) is None,
        "post-public: top-level permissions must remain exact and read-only",
        failures,
    )
    require(
        post_public.count(
            "group: openprose-cli-post-public-alpha-${{ inputs.version }}"
        )
        == 1
        and re.search(r"(?m)^  cancel-in-progress: false\s*$", post_public) is not None,
        "post-public: exact-version checks must serialize without cancellation",
        failures,
    )
    require(
        "continue-on-error" not in post_public
        and re.findall(r"(?m)^    needs: (.+)\s*$", post_public)
        == ["require-main", "[require-main, authenticate-draft-authority]"],
        "post-public: failure weakening and dependency drift are forbidden",
        failures,
    )

    blocks = job_blocks(post_public)
    require(
        tuple(blocks)
        == (
            "require-main",
            "authenticate-draft-authority",
            "verify-public-alpha",
        ),
        "post-public: the workflow must retain main, authority, and matrix jobs",
        failures,
    )
    preflight = blocks.get("require-main", "")
    preflight_steps = named_step_blocks(preflight)
    preflight_step = preflight_steps.get("Require a main-branch dispatch", "")
    exact_preflight = (
        "|\n"
        "          set -euo pipefail\n"
        '          test "$GITHUB_REF" = "refs/heads/main"'
    )
    require(
        re.findall(r"(?m)^    if: (.+)\s*$", preflight) == []
        and re.findall(r"(?m)^    runs-on: (.+)\s*$", preflight) == ["ubuntu-22.04"]
        and re.findall(r"(?m)^    timeout-minutes: (.+)\s*$", preflight) == ["5"]
        and job_permission_values(preflight) == {"contents": "read"}
        and tuple(preflight_steps) == ("Require a main-branch dispatch",)
        and preflight_step.count("        shell: bash") == 1
        and "        env:" not in preflight_step
        and run_scripts(preflight_step) == (exact_preflight,),
        "post-public: an unconditional read-only main-branch preflight must fail wrong-ref dispatches",
        failures,
    )
    authority_job = blocks.get("authenticate-draft-authority", "")
    authority_steps = workflow_step_blocks(authority_job)
    authority_named_steps = named_step_blocks(authority_job)
    authority_downloads = artifact_action_steps(
        authority_job, "actions/download-artifact"
    )
    authority_action_steps = [
        step for step in authority_steps if step_uses(step) is not None
    ]
    authority_validation = authority_named_steps.get(
        "Validate the exact immutable authority producer identity", ""
    )
    producer_authentication = authority_named_steps.get(
        "Authenticate the successful producer workflow attempt", ""
    )
    authority_authentication = authority_named_steps.get(
        "Authenticate the immutable authority bytes", ""
    )
    authority_run_steps = [step for step in authority_steps if run_scripts(step)]
    require(
        re.findall(r"(?m)^    if: (.+)\s*$", authority_job) == []
        and re.findall(r"(?m)^    needs: (.+)\s*$", authority_job) == ["require-main"]
        and re.findall(r"(?m)^    runs-on: (.+)\s*$", authority_job) == ["ubuntu-22.04"]
        and re.findall(r"(?m)^    timeout-minutes: (.+)\s*$", authority_job) == ["10"]
        and job_permission_values(authority_job)
        == {"actions": "read", "contents": "read"}
        and "environment:" not in authority_job
        and "id-token:" not in authority_job,
        "post-public: draft authority preflight must be unconditional and read-only",
        failures,
    )
    require(
        tuple(authority_named_steps)
        == (
            "Validate the exact immutable authority producer identity",
            "Authenticate the successful producer workflow attempt",
            "Download the exact immutable draft authority",
            "Authenticate the immutable authority bytes",
        )
        and tuple(step_uses(step) for step in authority_action_steps)
        == (
            "actions/checkout@" + ACTION_PINS["actions/checkout"],
            "actions/download-artifact@" + ACTION_PINS["actions/download-artifact"],
        )
        and len(authority_steps) == 5
        and len(authority_run_steps) == 3
        and tuple(run_scripts(step) for step in authority_run_steps)
        == tuple(
            run_scripts(step)
            for step in (
                authority_validation,
                producer_authentication,
                authority_authentication,
            )
        ),
        "post-public: draft authority preflight step and action order must remain exact",
        failures,
    )
    require(
        step_env_values(authority_validation)
        == {
            "REPOSITORY_INPUT": "${{ inputs.repository }}",
            "VERSION_INPUT": "${{ inputs.version }}",
            "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
            "RELEASE_ID_INPUT": "${{ inputs.release_id }}",
            "DRAFT_WORKFLOW_RUN_ID": "${{ inputs.draft_workflow_run_id }}",
            "DRAFT_WORKFLOW_RUN_ATTEMPT": ("${{ inputs.draft_workflow_run_attempt }}"),
            "DRAFT_AUTHORITY_ARTIFACT": "${{ inputs.draft_authority_artifact }}",
        }
        and step_env_values(producer_authentication)
        == {
            "GITHUB_TOKEN": "${{ github.token }}",
            "REPOSITORY_INPUT": "${{ inputs.repository }}",
            "DRAFT_WORKFLOW_RUN_ID": "${{ inputs.draft_workflow_run_id }}",
            "DRAFT_WORKFLOW_RUN_ATTEMPT": ("${{ inputs.draft_workflow_run_attempt }}"),
            "RUN_METADATA": (
                "${{ runner.temp }}/openprose-alpha-draft-producer-run.json"
            ),
        }
        and step_env_values(authority_authentication)
        == {
            "PYTHONPATH": "${{ github.workspace }}/authority-control/cli/ci",
            "REPOSITORY_INPUT": "${{ inputs.repository }}",
            "VERSION_INPUT": "${{ inputs.version }}",
            "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
            "RELEASE_ID_INPUT": "${{ inputs.release_id }}",
            "DRAFT_WORKFLOW_RUN_ID": "${{ inputs.draft_workflow_run_id }}",
            "DRAFT_WORKFLOW_RUN_ATTEMPT": ("${{ inputs.draft_workflow_run_attempt }}"),
            "DRAFT_AUTHORITY_ARTIFACT": "${{ inputs.draft_authority_artifact }}",
            "DRAFT_AUTHORITY": (
                "${{ runner.temp }}/openprose-alpha-draft-authority-preflight/"
                "alpha-draft-authority.json"
            ),
            "RUN_METADATA": (
                "${{ runner.temp }}/openprose-alpha-draft-producer-run.json"
            ),
        }
        and authority_validation.count("        shell: bash") == 1
        and producer_authentication.count("        shell: bash") == 1
        and authority_authentication.count("        shell: bash") == 1
        and authority_authentication.count("        id: authority") == 1,
        "post-public: authority preflight environments must remain exact and path-safe",
        failures,
    )
    authority_checkouts = [
        step
        for step in authority_action_steps
        if (step_uses(step) or "").split("@", 1)[0] == "actions/checkout"
    ]
    require(
        len(authority_checkouts) == 1
        and step_with_values(authority_checkouts[0])
        == {
            "ref": "${{ github.sha }}",
            "path": "authority-control",
            "persist-credentials": "false",
        }
        and authority_downloads
        == (
            (
                "${{ inputs.draft_authority_artifact }}",
                "${{ runner.temp }}/openprose-alpha-draft-authority-preflight",
            ),
        )
        and "repository: ${{ inputs.repository }}" in authority_job
        and "run-id: ${{ inputs.draft_workflow_run_id }}" in authority_job
        and "github-token: ${{ github.token }}" in authority_job,
        "post-public: one exact producer artifact must be downloaded without credentials",
        failures,
    )
    require(
        all(
            token in authority_job
            for token in (
                "draft_authority_sha256: ${{ steps.authority.outputs.draft_authority_sha256 }}",
                "draft_workflow_run_id: ${{ steps.authority.outputs.draft_workflow_run_id }}",
                "draft_workflow_run_attempt: ${{ steps.authority.outputs.draft_workflow_run_attempt }}",
                "draft_authority_artifact: ${{ steps.authority.outputs.draft_authority_artifact }}",
                'test "$DRAFT_WORKFLOW_RUN_ID" != "$GITHUB_RUN_ID"',
                'test "$DRAFT_AUTHORITY_ARTIFACT" = "openprose-cli-alpha-draft-authority-run-$DRAFT_WORKFLOW_RUN_ID-attempt-$DRAFT_WORKFLOW_RUN_ATTEMPT"',
                'GH_TOOL="$(command -v gh)"',
                "path = Path(sys.argv[1]).resolve(strict=True)",
                "not stat.S_ISREG(metadata.st_mode)",
                "not os.access(path, os.X_OK)",
                '[[ "$GH_TOOL" =~ ^/[A-Za-z0-9._+/@:-]+$ ]]',
                'api --method GET --header "Accept: application/vnd.github+json"',
                '"/repos/$REPOSITORY_INPUT/actions/runs/$DRAFT_WORKFLOW_RUN_ID/attempts/$DRAFT_WORKFLOW_RUN_ATTEMPT"',
                "import promote_alpha_release as promotion",
                "promotion.load_draft_authority(",
                'metadata.get("path")',
                '!= ".github/workflows/openprose-cli-alpha-release.yml"',
                'metadata.get("conclusion") != "success"',
                "draft_authority_sha256={authority.sha256}",
                "draft_workflow_run_id={authority.workflow_run_id}",
                "draft_workflow_run_attempt=",
                "draft_authority_artifact={artifact}",
            )
        )
        and "render_release_notes.py" not in authority_job,
        "post-public: producer run and immutable W181 bytes must authenticate before fan-out",
        failures,
    )
    job = blocks.get("verify-public-alpha", "")
    require(
        re.findall(r"(?m)^    if: (.+)\s*$", job) == []
        and re.findall(r"(?m)^    needs: (.+)\s*$", job)
        == ["[require-main, authenticate-draft-authority]"],
        "post-public: every verification cell must depend on authority preflight",
        failures,
    )
    exact_matrix = (
        "    strategy:\n"
        "      fail-fast: false\n"
        "      matrix:\n"
        "        include:\n"
        + "".join(
            f"          - {{target: {target}, runner: {runner}}}\n"
            for target, runner in POST_PUBLIC_TARGET_RUNNERS.items()
        )
        + "    runs-on: ${{ matrix.runner }}\n"
    )
    matrix_rows = re.findall(
        r"(?m)^          - \{target: ([a-z0-9-]+), runner: ([a-z0-9.-]+)\}$",
        job,
    )
    require(
        exact_matrix in job
        and tuple(matrix_rows) == tuple(POST_PUBLIC_TARGET_RUNNERS.items())
        and re.findall(r"(?m)^    runs-on: (.+)\s*$", job) == ["${{ matrix.runner }}"]
        and re.search(r"(?m)^    timeout-minutes: 120\s*$", job) is not None,
        "post-public: verification must retain the exact four-target native POSIX matrix",
        failures,
    )
    require(
        job_permission_values(job)
        == {"actions": "read", "contents": "read", "attestations": "read"},
        "post-public: job permissions must remain exact and read-only",
        failures,
    )
    require(
        re.search(r"(?m)^\s+environment:\s*", post_public) is None
        and "id-token:" not in post_public
        and "actions: write" not in post_public
        and "contents: write" not in post_public
        and "attestations: write" not in post_public
        and re.search(r"(?m)^\s*packages:\s*", post_public) is None,
        "post-public: protected environments and write authority are forbidden",
        failures,
    )

    check_action_pins(
        post_public,
        "post-public",
        failures,
        {
            "actions/checkout",
            "actions/setup-python",
            "actions/setup-node",
            "actions/upload-artifact",
            "actions/download-artifact",
        },
    )
    steps = workflow_step_blocks(job)
    action_steps = [step for step in steps if step_uses(step) is not None]
    expected_action_references = (
        "actions/download-artifact@" + ACTION_PINS["actions/download-artifact"],
        "actions/checkout@" + ACTION_PINS["actions/checkout"],
        "actions/checkout@" + ACTION_PINS["actions/checkout"],
        "actions/setup-python@" + ACTION_PINS["actions/setup-python"],
        "actions/setup-node@" + ACTION_PINS["actions/setup-node"],
        "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"],
    )
    require(
        tuple(step_uses(step) for step in action_steps) == expected_action_references,
        "post-public: action set, order, and immutable pins must remain exact",
        failures,
    )
    matrix_downloads = artifact_action_steps(job, "actions/download-artifact")
    require(
        matrix_downloads
        == (
            (
                "${{ needs.authenticate-draft-authority.outputs."
                "draft_authority_artifact }}",
                "${{ runner.temp }}/openprose-alpha-draft-authority-"
                "${{ matrix.target }}",
            ),
        )
        and (
            "run-id: ${{ needs.authenticate-draft-authority.outputs."
            "draft_workflow_run_id }}"
        )
        in job
        and "repository: ${{ inputs.repository }}" in job
        and "github-token: ${{ github.token }}" in job,
        "post-public: every matrix cell must download the same authenticated authority",
        failures,
    )
    checkout_steps = [
        step
        for step in action_steps
        if (step_uses(step) or "").split("@", 1)[0] == "actions/checkout"
    ]
    require(
        len(checkout_steps) == 2
        and step_with_values(checkout_steps[0])
        == {
            "ref": "${{ github.sha }}",
            "path": "control",
            "fetch-depth": "0",
            "persist-credentials": "false",
        }
        and step_with_values(checkout_steps[1])
        == {
            "ref": "${{ inputs.source_sha }}",
            "path": "candidate",
            "persist-credentials": "false",
        },
        "post-public: current control and exact candidate checkouts must remain separate",
        failures,
    )
    python_steps = [
        step
        for step in action_steps
        if (step_uses(step) or "").split("@", 1)[0] == "actions/setup-python"
    ]
    node_steps = [
        step
        for step in action_steps
        if (step_uses(step) or "").split("@", 1)[0] == "actions/setup-node"
    ]
    require(
        len(python_steps) == 1
        and step_with_values(python_steps[0])
        == {
            "python-version": '"3.10.18"',
            "cache": "pip",
            "cache-dependency-path": "control/cli/ci/requirements-test.txt",
        }
        and len(node_steps) == 1
        and step_with_values(node_steps[0]) == {"node-version": '"24.20.0"'},
        "post-public: Python, Node, and dependency-cache custody must remain exact",
        failures,
    )

    named_steps = named_step_blocks(job)
    expected_named_steps = (
        "Download the preauthenticated immutable draft authority",
        "Validate exact workflow and public-alpha identity",
        "Install the exact controller dependency closure",
        "Resolve the native GitHub CLI verifier",
        "Verify all 38 release assets, five npm packages, and native installed journeys",
        "Retain the target-named sanitized post-public verification record",
    )
    require(
        tuple(named_steps) == expected_named_steps,
        "post-public: named verification step set and order must remain exact",
        failures,
    )
    validation = named_steps.get(expected_named_steps[1], "")
    dependency_install = named_steps.get(expected_named_steps[2], "")
    github_cli = named_steps.get(expected_named_steps[3], "")
    controller = named_steps.get(expected_named_steps[4], "")
    upload = named_steps.get(expected_named_steps[5], "")
    exact_identity_environment = {
        "REPOSITORY_INPUT": "${{ inputs.repository }}",
        "VERSION_INPUT": "${{ inputs.version }}",
        "TAG_INPUT": "${{ inputs.tag }}",
        "SOURCE_SHA_INPUT": "${{ inputs.source_sha }}",
        "RELEASE_ID_INPUT": "${{ inputs.release_id }}",
        "CONFIRMATION_INPUT": "${{ inputs.confirmation }}",
    }
    exact_validation_environment = {
        **exact_identity_environment,
        "DRAFT_AUTHORITY_SHA256": (
            "${{ needs.authenticate-draft-authority.outputs."
            "draft_authority_sha256 }}"
        ),
        "DRAFT_WORKFLOW_RUN_ID": (
            "${{ needs.authenticate-draft-authority.outputs." "draft_workflow_run_id }}"
        ),
        "DRAFT_WORKFLOW_RUN_ATTEMPT": (
            "${{ needs.authenticate-draft-authority.outputs."
            "draft_workflow_run_attempt }}"
        ),
        "DRAFT_AUTHORITY_ARTIFACT": (
            "${{ needs.authenticate-draft-authority.outputs."
            "draft_authority_artifact }}"
        ),
    }
    exact_validation = (
        "|\n"
        "          set -euo pipefail\n"
        '          test "$GITHUB_REF" = "refs/heads/main"\n'
        '          test "$REPOSITORY_INPUT" = "$GITHUB_REPOSITORY"\n'
        '          [[ "$REPOSITORY_INPUT" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]\n'
        '          [[ "$VERSION_INPUT" =~ ^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)-alpha\\.(0|[1-9][0-9]*)$ ]]\n'
        '          test "$TAG_INPUT" = "cli-v$VERSION_INPUT"\n'
        '          [[ "$SOURCE_SHA_INPUT" =~ ^[0-9a-f]{40}$ ]]\n'
        '          [[ "$RELEASE_ID_INPUT" =~ ^[1-9][0-9]*$ ]]\n'
        '          [[ "$DRAFT_AUTHORITY_SHA256" =~ ^[0-9a-f]{64}$ ]]\n'
        '          [[ "$DRAFT_WORKFLOW_RUN_ID" =~ ^[1-9][0-9]*$ ]]\n'
        '          [[ "$DRAFT_WORKFLOW_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]]\n'
        '          test "$DRAFT_AUTHORITY_ARTIFACT" = "openprose-cli-alpha-draft-authority-run-$DRAFT_WORKFLOW_RUN_ID-attempt-$DRAFT_WORKFLOW_RUN_ATTEMPT"\n'
        '          test "$CONFIRMATION_INPUT" = "VERIFY PUBLIC ALPHA $REPOSITORY_INPUT $VERSION_INPUT $TAG_INPUT $SOURCE_SHA_INPUT $RELEASE_ID_INPUT"\n'
        '          test "$(git -C control rev-parse HEAD)" = "$GITHUB_SHA"\n'
        '          test "$(git -C candidate rev-parse HEAD)" = "$SOURCE_SHA_INPUT"\n'
        '          git -C control merge-base --is-ancestor "$SOURCE_SHA_INPUT" refs/remotes/origin/main'
    )
    require(
        step_env_values(validation) == exact_validation_environment
        and run_scripts(validation) == (exact_validation,)
        and validation.count("        shell: bash") == 1,
        "post-public: workflow and release identity validation must remain exact",
        failures,
    )
    exact_install = (
        ">-\n"
        "          python -m pip install --disable-pip-version-check\n"
        "          --require-hashes --only-binary=:all:\n"
        "          -r control/cli/ci/requirements-test.txt"
    )
    require(
        run_scripts(dependency_install) == (exact_install,)
        and "        env:" not in dependency_install
        and "        shell:" not in dependency_install,
        "post-public: controller dependencies must use one exact hash-pinned install",
        failures,
    )
    exact_github_cli = (
        "|\n"
        "          set -euo pipefail\n"
        '          GH_TOOL="$(command -v gh)"\n'
        '          GH_TOOL="$(python - "$GH_TOOL" <<\'PY\'\n'
        "          import os\n"
        "          from pathlib import Path\n"
        "          import stat\n"
        "          import sys\n\n"
        "          path = Path(sys.argv[1]).resolve(strict=True)\n"
        "          metadata = path.stat()\n"
        "          if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):\n"
        "              raise SystemExit(1)\n"
        "          print(path)\n"
        "          PY\n"
        '          )"\n'
        '          [[ "$GH_TOOL" =~ ^/[A-Za-z0-9._+/@:-]+$ ]]\n'
        '          printf \'path=%s\\n\' "$GH_TOOL" >>"$GITHUB_OUTPUT"'
    )
    require(
        github_cli.count("        id: github-cli") == 1
        and github_cli.count("        shell: bash") == 1
        and "        env:" not in github_cli
        and run_scripts(github_cli) == (exact_github_cli,),
        "post-public: GitHub CLI discovery must resolve one native executable safely",
        failures,
    )
    exact_controller = (
        ">-\n"
        "          python control/cli/ci/run_public_alpha_verification.py\n"
        '          --repository "$REPOSITORY_INPUT"\n'
        '          --release-id "$RELEASE_ID_INPUT"\n'
        '          --version "$VERSION_INPUT"\n'
        '          --tag "$TAG_INPUT"\n'
        '          --source-sha "$SOURCE_SHA_INPUT"\n'
        '          --target-id "$TARGET_ID"\n'
        '          --workflow-run-id "$GITHUB_RUN_ID"\n'
        '          --workflow-run-attempt "$GITHUB_RUN_ATTEMPT"\n'
        '          --confirmation "$CONFIRMATION_INPUT"\n'
        '          --draft-authority "$DRAFT_AUTHORITY"\n'
        '          --draft-authority-sha256 "$DRAFT_AUTHORITY_SHA256"\n'
        '          --draft-workflow-run-id "$DRAFT_WORKFLOW_RUN_ID"\n'
        '          --draft-workflow-run-attempt "$DRAFT_WORKFLOW_RUN_ATTEMPT"\n'
        '          --lineage "$GITHUB_WORKSPACE/control/cli/release/npm-registry-lineage.v1.json"\n'
        '          --image-manifest "$GITHUB_WORKSPACE/candidate/cli/shared/image/echo-v0/manifest.json"\n'
        '          --evidence "$RUNNER_TEMP/openprose-alpha-public-evidence-$TARGET_ID/alpha-public-verification-$TARGET_ID.json"\n'
        '          --workspace "$RUNNER_TEMP/openprose-alpha-public-assets-$TARGET_ID"\n'
        '          --journey-root "$RUNNER_TEMP/openprose-alpha-public-journey-$TARGET_ID"\n'
        '          --github-cli "$GH_TOOL"'
    )
    exact_controller_environment = {
        **exact_identity_environment,
        "TARGET_ID": "${{ matrix.target }}",
        "GH_TOOL": "${{ steps.github-cli.outputs.path }}",
        "DRAFT_AUTHORITY": (
            "${{ runner.temp }}/openprose-alpha-draft-authority-"
            "${{ matrix.target }}/alpha-draft-authority.json"
        ),
        "DRAFT_AUTHORITY_SHA256": (
            "${{ needs.authenticate-draft-authority.outputs."
            "draft_authority_sha256 }}"
        ),
        "DRAFT_WORKFLOW_RUN_ID": (
            "${{ needs.authenticate-draft-authority.outputs." "draft_workflow_run_id }}"
        ),
        "DRAFT_WORKFLOW_RUN_ATTEMPT": (
            "${{ needs.authenticate-draft-authority.outputs."
            "draft_workflow_run_attempt }}"
        ),
        "GITHUB_TOKEN": "${{ github.token }}",
    }
    require(
        step_env_values(controller) == exact_controller_environment
        and run_scripts(controller) == (exact_controller,)
        and "        shell:" not in controller,
        "post-public: controller target, native tool, and target-named paths must remain exact",
        failures,
    )
    run_steps = [step for step in steps if run_scripts(step)]
    require(
        len(run_steps) == 4
        and all(
            step
            in (
                validation,
                dependency_install,
                github_cli,
                controller,
            )
            for step in run_steps
        ),
        "post-public: direct package-manager, download, build, or mutation routes are forbidden",
        failures,
    )

    require(
        "        if: always()" in upload
        and upload.count("        if: always()") == 1
        and step_uses(upload)
        == "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"]
        and step_with_values(upload)
        == {
            "name": (
                "openprose-alpha-public-verification-${{ matrix.target }}-"
                "${{ inputs.version }}-"
                "${{ github.run_id }}-${{ github.run_attempt }}"
            ),
            "path": (
                "${{ runner.temp }}/openprose-alpha-public-evidence-${{ matrix.target }}/"
                "alpha-public-verification-${{ matrix.target }}.json"
            ),
            "if-no-files-found": "warn",
            "retention-days": "90",
        },
        "post-public: each target-named sanitized evidence record must always be retained for 90 days",
        failures,
    )
    forbidden_credentials = (
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GEMINI_API_KEY",
        "GH_TOKEN",
        "GOOGLE_API_KEY",
        "NODE_AUTH_TOKEN",
        "NPM_CONFIG_TOKEN",
        "NPM_TOKEN",
        "OPENAI_API_KEY",
        "OPENPROSE_TOKEN",
        "OPENROUTER_API_KEY",
        "${{ secrets.",
    )
    scripts = "\n".join(run_scripts(post_public))
    require(
        not any(name in post_public for name in forbidden_credentials)
        and post_public.count("GITHUB_TOKEN: ${{ github.token }}") == 2,
        "post-public: provider and package-publication credentials are forbidden",
        failures,
    )
    require(
        not any(
            re.search(pattern, scripts)
            for pattern in (
                r"(?m)(?:^|\s)(?:\S*/)?(?:npm|pnpm|yarn|bun|cargo|rustup)(?=\s)",
                r"(?m)(?:^|\s)(?:\S*/)?(?:curl|wget|fetch|aria2c|httpie)(?=\s)",
                r"\bgh\s+release\b",
                r"\bgit\s+(?:push|tag|commit|checkout|reset)\b",
                r"\b(?:build_local|package_local|create_draft_release|promote_alpha_release)\.py\b",
                r"(?:^|\s)--(?:otp|approve)(?:\s|=)",
            )
        ),
        "post-public: workflow shell steps must remain read-only and provider-free",
        failures,
    )
    return failures


def main() -> int:
    try:
        failures = audit(
            CI_WORKFLOW.read_text("utf-8"),
            RELEASE_WORKFLOW.read_text("utf-8"),
            DRAFT_HELPER.read_text("utf-8"),
        )
        failures.extend(audit_alpha(ALPHA_WORKFLOW.read_text("utf-8")))
        failures.extend(audit_promotion(PROMOTION_WORKFLOW.read_text("utf-8")))
        failures.extend(audit_post_public(POST_PUBLIC_WORKFLOW.read_text("utf-8")))
    except OSError as error:
        failures = [f"cannot read workflow: {error}"]
    report = {
        "schema": "openprose.workflow-policy-report/1",
        "status": "pass" if not failures else "fail",
        "failures": failures,
    }
    print(json.dumps(report, sort_keys=True))
    for failure in failures:
        print(f"workflow-policy: {failure}", file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
