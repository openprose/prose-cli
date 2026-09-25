from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from check_workflows import audit, audit_alpha, audit_post_public, audit_promotion


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-draft-release.yml"
ALPHA_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-alpha-release.yml"
PROMOTION_WORKFLOW = ROOT / ".github" / "workflows" / "openprose-cli-alpha-promote.yml"
POST_PUBLIC_WORKFLOW = (
    ROOT / ".github" / "workflows" / "openprose-cli-alpha-post-public.yml"
)
DRAFT_HELPER = ROOT / "cli" / "ci" / "create_draft_release.py"
TARGET_PLATFORMS = {
    "linux-x64": "linux-x64-gnu",
    "linux-arm64": "linux-arm64-gnu",
    "darwin-arm": "darwin-arm64",
    "darwin-x64": "darwin-x64",
    "win-x64": "win32-x64",
}
EVIDENCE_NAMES = (
    "release-manifest.json",
    "sbom.cdx.json",
    "provenance.json",
    "dependency-evidence.json",
)


def assembly_script() -> str:
    workflow = RELEASE_WORKFLOW.read_text("utf-8")
    step = workflow.index("      - name: Assemble only hash-verified bytes")
    heredoc = workflow.index("          python - <<'PY'\n", step) + len(
        "          python - <<'PY'\n"
    )
    end = workflow.index("\n          PY", heredoc)
    return textwrap.dedent(workflow[heredoc:end])


def create_assembly_fixture(root: Path, version: str = "1.2.3") -> None:
    corpus_source = (
        ROOT / "cli" / "conformance" / "release-package" / "invariants.v1.json"
    )
    corpus_bytes = corpus_source.read_bytes()
    corpus = json.loads(corpus_bytes)
    control_corpus = (
        root
        / "control"
        / "cli"
        / "conformance"
        / "release-package"
        / "invariants.v1.json"
    )
    control_corpus.parent.mkdir(parents=True)
    control_corpus.write_bytes(corpus_bytes)
    fixture_image = {
        "version": "release-image-v1",
        "imageSha256": "e" * 64,
        "manifestSha256": "f" * 64,
    }
    package_image = {
        "formatVersion": "openprose.skill-runtime-image/1",
        "version": fixture_image["version"],
        "sha256": fixture_image["imageSha256"],
        "manifestSha256": fixture_image["manifestSha256"],
        "releaseEligible": True,
    }
    build_profiles = {
        implementation: {"profile": "release", "testSeamsEnabled": False}
        for implementation in ("rust", "bun")
    }
    packages = root / "packages"
    for target, platform_identifier in TARGET_PLATFORMS.items():
        package = packages / f"package-{target}"
        package.mkdir(parents=True)
        names = {
            f"openprose-prose-cli-rust-{version}-{platform_identifier}.tar.gz",
            f"openprose-prose-cli-bun-{version}-{platform_identifier}.tar.gz",
            f"openprose-prose-cli-{version}.tgz",
            f"openprose-prose-cli-{platform_identifier}-{version}.tgz",
            *EVIDENCE_NAMES,
        }
        for name in names:
            encoded = f"fixture:{name}\n".encode()
            if name == "release-manifest.json":
                encoded = (
                    json.dumps(
                        {
                            "mode": "release",
                            "platform": platform_identifier,
                            "image": package_image,
                            "buildProfiles": build_profiles,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                ).encode()
            (package / name).write_bytes(encoded)
        checksum = "".join(
            f"{hashlib.sha256((package / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted(names)
        )
        (package / "SHA256SUMS").write_text(checksum, "ascii")

    protected = root / "protected"
    for relative in (
        "profile-admission.json",
        "protected-dependency-evidence.json",
        "authority/protected-authority-run.json",
        "authority/protected-authority/authority-provenance.json",
        "authority/protected-authority/canonical-profile-attestation.json",
        "authority/protected-authority/release-evidence-attestation.json",
    ):
        path = protected / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fixture:{relative}\n".encode())
    (protected / "profile-preflight.json").write_text(
        json.dumps({"image": fixture_image}, sort_keys=True) + "\n", "utf-8"
    )
    expected_image = {
        "formatVersion": "openprose.skill-runtime-image/1",
        "version": fixture_image["version"],
        "sha256": fixture_image["imageSha256"],
        "releaseEligible": True,
    }
    expected_reports = {
        implementation: {
            "runner": {"name": implementation, "version": version, "commit": "a" * 40},
            "build": {"profile": "release", "testSeamsEnabled": False},
            "image": expected_image,
        }
        for implementation in ("rust", "bun")
    }
    for target in TARGET_PLATFORMS:
        platform_identifier = TARGET_PLATFORMS[target]
        suffix = ".exe" if target == "win-x64" else ""
        native_names = {"prose-rust" + suffix, "prose-bun" + suffix}
        if target == "win-x64":
            native_names.add("openprose-windows-process-host.exe")
        native_dir = root / "native-builds" / f"native-build-{target}"
        native_dir.mkdir(parents=True)
        for name in native_names:
            encoded = f"fixture:{target}:{name}\n".encode()
            (native_dir / name).write_bytes(encoded)
        products = {
            implementation: {
                "path": f"prose-{implementation}{suffix}",
                "byteLength": (native_dir / f"prose-{implementation}{suffix}")
                .stat()
                .st_size,
                "sha256": hashlib.sha256(
                    (native_dir / f"prose-{implementation}{suffix}").read_bytes()
                ).hexdigest(),
            }
            for implementation in ("rust", "bun")
        }
        windows_host = None
        if target == "win-x64":
            host_bytes = (
                native_dir / "openprose-windows-process-host.exe"
            ).read_bytes()
            windows_host = {
                "path": "openprose-windows-process-host.exe",
                "byteLength": len(host_bytes),
                "sha256": hashlib.sha256(host_bytes).hexdigest(),
                "admission": False,
            }
        native_manifest = {
            "schema": "openprose.native-build/1",
            "target": target,
            "sourceSha": "a" * 40,
            "build": {"profile": "release", "testSeamsEnabled": False},
            "windowsJobObjectReleaseAdmission": False,
            "windowsProcessHost": windows_host,
            "products": products,
        }
        (native_dir / "native-manifest.json").write_text(
            json.dumps(native_manifest, sort_keys=True) + "\n", "utf-8"
        )
        native_names.add("native-manifest.json")
        native_files = {
            name: {
                "byteLength": (native_dir / name).stat().st_size,
                "sha256": hashlib.sha256((native_dir / name).read_bytes()).hexdigest(),
            }
            for name in native_names
        }
        manifest_bytes = (native_dir / "native-manifest.json").read_bytes()
        verification = {
            "schema": "openprose.native-verification/1",
            "target": target,
            "sourceSha": "a" * 40,
            "nativeArtifact": {
                "name": f"native-build-{target}",
                "workflowRunId": 123,
                "workflowRunAttempt": 1,
                "files": native_files,
            },
            "nativeManifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "windowsJobObjectReleaseAdmission": False,
            "windowsProcessHost": windows_host,
            "containment": {
                "strictDescendantContainmentEnforced": False,
                "releaseEligible": False,
                "blocker": "detached-descendant-containment-not-enforced",
            },
            "reports": expected_reports,
        }
        target_dir = protected / "verified" / f"native-verification-{target}"
        target_dir.mkdir(parents=True)
        (target_dir / "verification.json").write_text(
            json.dumps(verification, sort_keys=True) + "\n", "utf-8"
        )

        package = packages / f"package-{target}"
        checksum_bytes = (package / "SHA256SUMS").read_bytes()
        release_manifest_bytes = (package / "release-manifest.json").read_bytes()
        package_files = [
            {
                "path": path.name,
                "byteLength": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(package.iterdir(), key=lambda candidate: candidate.name)
        ]
        claims = {
            "providerCalls": "not-observed",
            "semanticEvaluation": False,
            "programPortabilityEvaluation": False,
            "releaseEligible": False,
            "publicationAuthorized": False,
            "rankingProduced": False,
            "strictDescendantContainment": False,
            "runtimeNetworkIsolation": False,
            "candidateExecution": "blocked-before-execution"
            if target == "win-x64"
            else "performed-posix-invariants",
        }
        lineage = {
            "nativeArtifact": {
                "name": f"native-build-{target}",
                "workflowRunId": 123,
                "workflowRunAttempt": 1,
            },
            "nativeManifestSha256": verification["nativeManifestSha256"],
            "files": native_files,
            "products": {
                implementation: {
                    "nativePath": f"prose-{implementation}{suffix}",
                    "nativeSha256": native_files[f"prose-{implementation}{suffix}"][
                        "sha256"
                    ],
                    "packagedBinarySha256": native_files[
                        f"prose-{implementation}{suffix}"
                    ]["sha256"],
                }
                for implementation in ("rust", "bun")
            },
            "windowsProcessHost": windows_host
            if windows_host is not None
            else "not-applicable",
        }
        preflight_bytes = (protected / "profile-preflight.json").read_bytes()
        verification_bytes = (target_dir / "verification.json").read_bytes()

        def projection(case_id: str, runner: str) -> dict[str, object]:
            if case_id == "version":
                return {"kind": "version", "runner": runner, "version": version}
            if case_id == "help":
                contract = next(
                    case["stdout"] for case in corpus["cases"] if case["id"] == "help"
                )
                return {
                    "kind": "help",
                    "byteLength": contract["byteLength"],
                    "sha256": contract["sha256"],
                }
            if case_id == "config-explain":
                return {
                    "schema": "openprose.configuration-explanation/1",
                    "harness": "openprose",
                    "transport": "auto",
                    "output": "json",
                    "color": False,
                }
            if case_id == "harness-list":
                return {
                    "schema": "openprose.harness-list/1",
                    "selected": "openprose",
                    "availability": "unavailable",
                    "detectedVersion": None,
                    "strictWrapperConformant": False,
                    "testOnly": True,
                    "admissionBlock": "test-seams-disabled",
                }
            if case_id in {"doctor-default", "doctor-mock-refused"}:
                harness = "mock" if case_id == "doctor-mock-refused" else "openprose"
                return {
                    "schema": "openprose.doctor-report/1",
                    "runner": {"name": runner, "version": version, "commit": "a" * 40},
                    "build": {"profile": "release", "testSeamsEnabled": False},
                    "image": {
                        key: package_image[key]
                        for key in (
                            "formatVersion",
                            "version",
                            "sha256",
                            "releaseEligible",
                        )
                    },
                    "ready": False,
                    "harness": harness,
                    "error": "HARNESS_UNAVAILABLE"
                    if harness == "mock"
                    else "HOSTED_UNAVAILABLE",
                }
            return {
                "schema": "openprose.runner-result/1",
                "runner": {"name": runner, "version": version, "commit": "a" * 40},
                "adapter": {
                    "id": "mock/unavailable",
                    "harnessVersion": None,
                    "descriptorDigestSha256": "599d3baf7b7aee1f97855dd4ca13e780c4de0aff410ad5860000631c0ac22fa4",
                },
                "transport": "deterministic",
                "terminal": {
                    "classification": "runner-error",
                    "transportCompleted": False,
                    "terminalEventObserved": False,
                    "exitCode": None,
                    "signal": None,
                },
                "semantic": {"status": "unknown", "terminalEnvelopeDigestSha256": None},
                "error": {
                    "code": "HARNESS_UNAVAILABLE",
                    "exitCode": 10,
                    "details": {
                        "harness": "mock",
                        "admissionStatus": "blocked",
                        "admissionBlock": "test-seams-disabled",
                        "fallbackAttempted": False,
                    },
                },
                "runnerExitCode": 10,
            }

        case_records = []
        if target != "win-x64":
            for expected_case in corpus["cases"]:
                observations = []
                for surface, runner in (
                    ("direct-rust", "rust"),
                    ("direct-bun", "bun"),
                    ("npm-launcher", "bun"),
                ):
                    stdout = f"{expected_case['id']}:{surface}\n".encode()
                    observations.append(
                        {
                            "surface": surface,
                            "exitCode": expected_case["exitCode"],
                            "stdout": {
                                "byteLength": len(stdout),
                                "sha256": hashlib.sha256(stdout).hexdigest(),
                            },
                            "stderr": {
                                "byteLength": 0,
                                "sha256": hashlib.sha256(b"").hexdigest(),
                            },
                            "settlement": "settled",
                            "settlementAuthority": "direct-and-original-process-group-settled",
                            "projection": projection(expected_case["id"], runner),
                        }
                    )
                case_records.append(
                    {
                        "id": expected_case["id"],
                        "argv": expected_case["argv"],
                        "observations": observations,
                    }
                )
        admission = {
            "schema": "openprose.release-package-admission/3",
            "status": "blocked-before-execution"
            if target == "win-x64"
            else "passed-provider-free-release-invariants",
            "targetId": target,
            "version": version,
            "sourceSha": "a" * 40,
            "controlSha": "b" * 40,
            "workflowRun": {"id": 123, "attempt": 1},
            "authorityInputs": {
                "profilePreflight": {
                    "byteLength": len(preflight_bytes),
                    "sha256": hashlib.sha256(preflight_bytes).hexdigest(),
                },
                "nativeVerification": {
                    "byteLength": len(verification_bytes),
                    "sha256": hashlib.sha256(verification_bytes).hexdigest(),
                },
            },
            "corpus": {
                "byteLength": len(corpus_bytes),
                "sha256": hashlib.sha256(corpus_bytes).hexdigest(),
            },
            "package": {
                "platform": platform_identifier,
                "mode": "release",
                "sha256Sums": {
                    "byteLength": len(checksum_bytes),
                    "sha256": hashlib.sha256(checksum_bytes).hexdigest(),
                },
                "releaseManifest": {
                    "byteLength": len(release_manifest_bytes),
                    "sha256": hashlib.sha256(release_manifest_bytes).hexdigest(),
                },
                "files": package_files,
                "image": package_image,
                "buildProfiles": build_profiles,
                "nativeLineage": lineage,
            },
            "installations": [] if target == "win-x64" else [{}, {}, {}],
            "executionToolchain": (
                {
                    "authority": "not-observed-no-candidate-execution",
                    "node": None,
                    "npm": None,
                }
                if target == "win-x64"
                else {
                    "authority": "reporter-observed-and-finally-reauthenticated-executable-bytes",
                    "node": {
                        "command": "/opt/openprose-test/node",
                        "resolvedPath": "/opt/openprose-test/node",
                        "sha256": "7" * 64,
                    },
                    "npm": {
                        "command": "/opt/openprose-test/npm",
                        "resolvedPath": "/opt/openprose-test/npm",
                        "sha256": "8" * 64,
                    },
                }
            ),
            "surfaces": {"direct-rust": {}, "direct-bun": {}, "npm-launcher": {}},
            "cases": case_records,
            "claims": claims,
        }
        admission_dir = root / "package-admissions" / f"package-admission-{target}"
        admission_dir.mkdir(parents=True)
        (admission_dir / "release-package-admission.json").write_text(
            json.dumps(
                admission, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n",
            "utf-8",
        )


def run_assembly(
    root: Path, version: str = "1.2.3"
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["VERSION_INPUT"] = version
    environment["SOURCE_SHA"] = "a" * 40
    environment["CONTROL_SHA"] = "b" * 40
    environment["WORKFLOW_RUN_ID"] = "123"
    environment["WORKFLOW_RUN_ATTEMPT"] = "1"
    return subprocess.run(
        [sys.executable, "-c", assembly_script()],
        cwd=root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )


class WorkflowPolicyTest(unittest.TestCase):
    def test_ci_filters_cover_every_release_and_public_guidance_input(self) -> None:
        ci = CI_WORKFLOW.read_text("utf-8")
        release = RELEASE_WORKFLOW.read_text("utf-8")
        helper = DRAFT_HELPER.read_text("utf-8")
        self.assertEqual([], audit(ci, release, helper))
        required = (
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
        )
        for path in required:
            with self.subTest(path=path):
                self.assertEqual(ci.count(f'"{path}"'), 2)
                changed = ci.replace(f', "{path}"', "", 1)
                if changed == ci:
                    changed = ci.replace(f'"{path}", ', "", 1)
                self.assertNotEqual(changed, ci)
                failures = audit(changed, release, helper)
                self.assertTrue(any(path in failure for failure in failures), failures)

    def test_post_public_workflow_is_one_exact_read_only_verification(self) -> None:
        workflow = POST_PUBLIC_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_post_public(workflow))
        mutations = (
            workflow.replace(
                "  workflow_dispatch:", "  push:\n  workflow_dispatch:", 1
            ),
            workflow.replace("      tag:\n", "", 1),
            workflow.replace(
                "      confirmation:\n",
                "      model:\n        required: true\n        type: string\n"
                "      confirmation:\n",
                1,
            ),
            workflow.replace(
                '          test "$GITHUB_REF" = "refs/heads/main"',
                '          test -n "$GITHUB_REF"',
                1,
            ),
            workflow.replace("    needs: require-main\n", "", 1),
            workflow.replace(
                "  verify-public-alpha:\n",
                "  verify-public-alpha:\n" "    if: github.ref == 'refs/heads/main'\n",
                1,
            ),
            workflow.replace("  attestations: read", "  attestations: write", 1),
            workflow.replace("      contents: read", "      contents: write", 1),
            workflow.replace(
                "    runs-on: ${{ matrix.runner }}",
                "    environment: unprotected\n    runs-on: ${{ matrix.runner }}",
                1,
            ),
            workflow.replace(
                "          - {target: linux-arm64, runner: ubuntu-22.04-arm}\n",
                "",
                1,
            ),
            workflow.replace(
                "          - {target: darwin-x64, runner: macos-15-intel}\n",
                "          - {target: darwin-x64, runner: macos-15-intel}\n"
                "          - {target: win-x64, runner: windows-2025}\n",
                1,
            ),
            workflow.replace(
                "          - {target: darwin-x64, runner: macos-15-intel}",
                "          - {target: linux-x64, runner: ubuntu-22.04}",
                1,
            ),
            workflow.replace(
                "          - {target: darwin-arm, runner: macos-15}",
                "          - {target: darwin-arm, runner: ubuntu-22.04}",
                1,
            ),
            workflow.replace(
                "    runs-on: ${{ matrix.runner }}", "    runs-on: ubuntu-22.04", 1
            ),
            workflow.replace("    timeout-minutes: 120", "    timeout-minutes: 0", 1),
            workflow.replace(
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/checkout@v4",
                1,
            ),
            workflow.replace(
                "          ref: ${{ github.sha }}", "          ref: main", 1
            ),
            workflow.replace("          path: candidate", "          path: control", 1),
            workflow.replace(
                "          persist-credentials: false",
                "          persist-credentials: true",
                1,
            ),
            workflow.replace(
                '          python-version: "3.10.18"',
                '          python-version: "3.11"',
                1,
            ),
            workflow.replace(
                '          node-version: "24.20.0"', '          node-version: "24"', 1
            ),
            workflow.replace(" --require-hashes", "", 1),
            workflow.replace(" --only-binary=:all:", "", 1),
            workflow.replace(
                "-r control/cli/ci/requirements-test.txt",
                "-r candidate/cli/ci/requirements-test.txt",
                1,
            ),
            workflow.replace("command -v gh", "printf /usr/bin/gh", 1),
            workflow.replace(".resolve(strict=True)", ".resolve()", 1),
            workflow.replace(
                "          TARGET_ID: ${{ matrix.target }}",
                "          TARGET_ID: linux-x64",
                1,
            ),
            workflow.replace('--target-id "$TARGET_ID"', '--target-id "linux-x64"', 1),
            workflow.replace(
                "python control/cli/ci/run_public_alpha_verification.py",
                "python candidate/cli/ci/run_public_alpha_verification.py",
                1,
            ),
            workflow.replace('          --tag "$TAG_INPUT"\n', "", 1),
            workflow.replace(
                '          --evidence "$RUNNER_TEMP/openprose-alpha-public-evidence-$TARGET_ID/alpha-public-verification-$TARGET_ID.json"',
                '          --evidence "/tmp/alpha-public-verification.json"',
                1,
            ),
            workflow.replace(
                "          name: openprose-alpha-public-verification-${{ matrix.target }}-${{ inputs.version }}-${{ github.run_id }}-${{ github.run_attempt }}",
                "          name: openprose-alpha-public-verification-${{ inputs.version }}-${{ github.run_id }}-${{ github.run_attempt }}",
                1,
            ),
            workflow.replace(
                "          GITHUB_TOKEN: ${{ github.token }}",
                "          GITHUB_TOKEN: ${{ github.token }}\n"
                "          NPM_TOKEN: ${{ secrets.NPM_TOKEN }}",
                1,
            ),
            workflow.replace(
                "      - name: Install the exact controller dependency closure",
                "      - name: Direct package-manager probe\n"
                "        run: npm view @openprose/prose-cli\n"
                "      - name: Install the exact controller dependency closure",
                1,
            ),
            workflow.replace(
                "      - name: Install the exact controller dependency closure",
                "      - name: Alternate public download\n"
                "        run: curl https://example.invalid/archive\n"
                "      - name: Install the exact controller dependency closure",
                1,
            ),
            workflow.replace(
                "      - name: Install the exact controller dependency closure",
                "      - name: Mutate the release\n"
                "        run: gh release edit cli-v0.0.0-alpha.1\n"
                "      - name: Install the exact controller dependency closure",
                1,
            ),
            workflow.replace("        if: always()", "        if: success()", 1),
            workflow.replace(
                "          retention-days: 90", "          retention-days: 1", 1
            ),
            workflow.replace(
                "          path: ${{ runner.temp }}/openprose-alpha-public-evidence-${{ matrix.target }}/alpha-public-verification-${{ matrix.target }}.json",
                "          path: ${{ runner.temp }}",
                1,
            ),
            workflow.replace("      draft_workflow_run_id:\n", "", 1),
            workflow.replace(
                "      draft_authority_artifact:\n",
                "      mutable_authority_artifact:\n",
                1,
            ),
            workflow.replace("  actions: read", "  actions: write", 1),
            workflow.replace(
                "  authenticate-draft-authority:\n    needs: require-main",
                "  authenticate-draft-authority:\n    needs: []",
                1,
            ),
            workflow.replace("    timeout-minutes: 10", "    timeout-minutes: 120", 1),
            workflow.replace("      actions: read", "      actions: write", 1),
            workflow.replace(
                "          path: authority-control",
                "          path: control",
                1,
            ),
            workflow.replace(
                '          test "$DRAFT_WORKFLOW_RUN_ID" != "$GITHUB_RUN_ID"\n',
                "",
                1,
            ),
            workflow.replace(
                "openprose-cli-alpha-draft-authority-run-$DRAFT_WORKFLOW_RUN_ID-attempt-$DRAFT_WORKFLOW_RUN_ATTEMPT",
                "openprose-cli-alpha-draft-authority-latest",
                1,
            ),
            workflow.replace("api --method GET", "api --method POST", 1),
            workflow.replace("/attempts/$DRAFT_WORKFLOW_RUN_ATTEMPT", "/attempts/1", 1),
            workflow.replace(
                "          GITHUB_TOKEN: ${{ github.token }}",
                "          GITHUB_TOKEN: untrusted",
                1,
            ),
            workflow.replace(
                "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
                "actions/download-artifact@v4",
                1,
            ),
            workflow.replace(
                "          name: ${{ inputs.draft_authority_artifact }}",
                "          name: openprose-cli-alpha-draft-authority-latest",
                1,
            ),
            workflow.replace(
                "          repository: ${{ inputs.repository }}",
                "          repository: fork/prose",
                1,
            ),
            workflow.replace(
                "          run-id: ${{ inputs.draft_workflow_run_id }}",
                "          run-id: ${{ github.run_id }}",
                1,
            ),
            workflow.replace("          github-token: ${{ github.token }}\n", "", 1),
            workflow.replace("promotion.load_draft_authority(", "json.loads(", 1),
            workflow.replace(
                '!= ".github/workflows/openprose-cli-alpha-release.yml"',
                '!= ".github/workflows/other.yml"',
                1,
            ),
            workflow.replace(
                'metadata.get("conclusion") != "success"',
                'metadata.get("conclusion") != "cancelled"',
                1,
            ),
            workflow.replace(
                'output.write(f"draft_authority_sha256={authority.sha256}\\n")',
                'output.write(f"draft_authority_sha256={"f" * 64}\\n")',
                1,
            ),
            workflow.replace(
                "    needs: [require-main, authenticate-draft-authority]",
                "    needs: require-main",
                1,
            ),
            workflow.replace(
                "name: ${{ needs.authenticate-draft-authority.outputs.draft_authority_artifact }}",
                "name: ${{ inputs.draft_authority_artifact }}",
                1,
            ),
            workflow.replace(
                "run-id: ${{ needs.authenticate-draft-authority.outputs.draft_workflow_run_id }}",
                "run-id: ${{ inputs.draft_workflow_run_id }}",
                1,
            ),
            workflow.replace(
                "path: ${{ runner.temp }}/openprose-alpha-draft-authority-${{ matrix.target }}",
                "path: ${{ runner.temp }}/openprose-alpha-draft-authority",
                1,
            ),
            workflow.replace(
                "DRAFT_AUTHORITY_SHA256: ${{ needs.authenticate-draft-authority.outputs.draft_authority_sha256 }}",
                "DRAFT_AUTHORITY_SHA256: ${{ inputs.source_sha }}",
                1,
            ),
            workflow.replace('          --draft-authority "$DRAFT_AUTHORITY"\n', "", 1),
            workflow.replace(
                '          --draft-authority-sha256 "$DRAFT_AUTHORITY_SHA256"\n',
                "",
                1,
            ),
            workflow.replace(
                '          --draft-workflow-run-id "$DRAFT_WORKFLOW_RUN_ID"\n',
                "",
                1,
            ),
            workflow.replace(
                '          --draft-workflow-run-attempt "$DRAFT_WORKFLOW_RUN_ATTEMPT"\n',
                "",
                1,
            ),
            workflow.replace(
                "          python - <<'PY'\n" "          import hashlib\n",
                "          python authority-control/cli/ci/render_release_notes.py\n"
                "          python - <<'PY'\n"
                "          import hashlib\n",
                1,
            ),
            workflow.replace(
                "  verify-public-alpha:\n",
                "      - run: printf unexpected\\n" "  verify-public-alpha:\n",
                1,
            ),
            workflow.replace(
                "  authenticate-draft-authority:\n",
                "  authenticate-draft-authority:\n" "    environment: privileged\n",
                1,
            ),
            workflow.replace(
                "          GITHUB_TOKEN: ${{ github.token }}",
                "          GITHUB_TOKEN: ${{ github.token }}\n"
                "          NPM_TOKEN: ${{ secrets.NPM_TOKEN }}",
                1,
            ),
            workflow.replace(
                "  verify-public-alpha:\n",
                "  bypass:\n    runs-on: ubuntu-22.04\n    steps: []\n"
                "  verify-public-alpha:\n",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(workflow, changed)
                self.assertTrue(audit_post_public(changed))

    def test_functional_alpha_workflow_is_manual_draft_only_and_nonsemantic(
        self,
    ) -> None:
        alpha = ALPHA_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_alpha(alpha))
        mutations = (
            alpha.replace("--mode alpha", "--mode release", 1),
            alpha.replace(
                'manifest["releaseEligible"] is False',
                'manifest["releaseEligible"] is True',
                1,
            ),
            alpha.replace(
                "environment: openprose-cli-alpha-release",
                "environment: unprotected",
                1,
            ),
            alpha.replace("cli/shared/image/echo-v0", "cli/shared/image/sentinel-v1"),
            alpha.replace(
                'manifest["platform"] == expected_platforms[target]',
                'manifest["platform"]',
                1,
            ),
            alpha.replace(
                "set(records) == expected_records",
                "set(records) >= expected_records",
                1,
            ),
            alpha.replace(
                'image_manifest["purpose"] == "functional-alpha-placeholder"',
                'image_manifest["purpose"]',
                1,
            ),
            alpha.replace("needs: [package, admit]", "needs: package", 1),
            alpha.replace("needs: assemble", "needs: [package, admit]", 1),
            alpha.replace("alpha_package_admission.py admit", "echo admit", 1),
            alpha.replace("alpha_package_admission.py verify-report", "echo verify", 1),
            alpha.replace(
                "python -m unittest -v cli.ci.test_alpha_package_admission",
                "echo skip-alpha-admission-tests",
                1,
            ),
            alpha.replace(
                "--only release-reproducibility",
                "--only release-notes",
                1,
            ),
            alpha.replace(
                '--remap-path-prefix=$CARGO_HOME_ROOT=/cargo-home"',
                '"',
                1,
            ),
            alpha.replace("pattern: alpha-admission-*", "pattern: alpha-package-*", 1),
            alpha.replace(
                'place(f"{target}-alpha-admission.json"',
                'place(f"{target}-ignored-admission.json"',
                1,
            ),
            alpha.replace(
                "OPENPROSE_BUILD_VERSION: ${{ inputs.version }}",
                "OPENPROSE_BUILD_VERSION: 0.1.0",
                1,
            ),
            alpha.replace(
                'OPENPROSE_IMAGE_BUNDLE="$SOURCE_ROOT/$IMAGE_BUNDLE"',
                'OPENPROSE_IMAGE_BUNDLE="$IMAGE_BUNDLE"',
                1,
            ),
            alpha.replace(
                "python candidate-a/cli/ci/check_linux_glibc.py",
                "echo skip-linux-glibc-admission",
                1,
            ),
            alpha.replace(
                "if: startsWith(matrix.target, 'linux-')",
                "if: startsWith(matrix.target, 'darwin-')",
                1,
            ),
            alpha.replace(
                'runtime["minimumGlibc"] == "2.34"', 'runtime["minimumGlibc"]', 1
            ),
            alpha.replace(
                'tuple(int(part) for part in required.split(".")) <= (2, 34)',
                'tuple(int(part) for part in required.split("."))',
                1,
            ),
            alpha.replace("path: candidate-b", "path: candidate-a", 1),
            alpha.replace(
                'build_one "$GITHUB_WORKSPACE/candidate-b"',
                'build_one "$GITHUB_WORKSPACE/candidate-a"',
                1,
            ),
            alpha.replace('cd "$requested_root"', "cd candidate-a", 1),
            alpha.replace(
                "cargo fetch --manifest-path candidate-b/cli/rust/Cargo.toml --locked",
                "echo skip-second-cargo-fetch",
                1,
            ),
            alpha.replace(
                'READELF_TOOL="$(python - "$(command -v readelf)"',
                'READELF_TOOL="$(command -v readelf)" #',
                1,
            ),
            alpha.replace(
                '--readelf "$READELF_INPUT"',
                "--readelf /usr/bin/readelf",
                1,
            ),
            alpha.replace(
                "python candidate-b/cli/ci/package_local.py",
                "cp -R alpha-package candidate-b-package",
                1,
            ),
            alpha.replace(
                "--rust-binary candidate-b/cli/rust/target/release/prose",
                "--rust-binary candidate-a/cli/rust/target/release/prose",
                1,
            ),
            alpha.replace(
                '"$PYTHON_TOOL" control/cli/ci/reproducible_release.py capture',
                '"$PYTHON_TOOL" candidate-a/cli/ci/reproducible_release.py capture',
                1,
            ),
            alpha.replace(
                '--tool "readelf=$READELF_INPUT" --tool-version "readelf=$SYSTEM_TOOL_VERSION"',
                "echo skip-readelf-receipt-custody",
                1,
            ),
            alpha.replace(
                'VERIFY_ARGS+=(--readelf "$READELF_INPUT")',
                "echo skip-controller-readelf-verification",
                1,
            ),
            alpha.replace(
                '--package "$RUNNER_TEMP/openprose-repro/package-b"',
                '--package "$GITHUB_WORKSPACE/alpha-package"',
                1,
            ),
            alpha.replace(
                '--right-package "$RUNNER_TEMP/openprose-repro/package-b"',
                '--right-package "$GITHUB_WORKSPACE/alpha-package"',
                1,
            ),
            alpha.replace(
                'VERIFY_ARGS+=(--otool "$OTOOL_TOOL")',
                "echo skip-darwin-minimum-os-admission",
                1,
            ),
            alpha.replace(
                '"$PYTHON_TOOL" control/cli/ci/reproducible_release.py "${VERIFY_ARGS[@]}"',
                "echo skip-reproducibility-admission",
                1,
            ),
            alpha.replace(
                "path: alpha-package\n          if-no-files-found: error",
                "path: $RUNNER_TEMP/openprose-repro/package-b\n          if-no-files-found: error",
                1,
            ),
            alpha.replace(
                'build_one "$GITHUB_WORKSPACE/candidate-b"',
                'strip "$GITHUB_WORKSPACE/candidate-a/cli/rust/target/release/prose"\n          build_one "$GITHUB_WORKSPACE/candidate-b"',
                1,
            ),
            alpha.replace(
                '--version "$VERSION_INPUT"',
                "--version '${{ inputs.version }}'",
                1,
            ),
            alpha.replace(
                "  package:\n    needs: test\n    permissions:\n      contents: read",
                "  package:\n    needs: test\n    permissions:\n      contents: write",
                1,
            ),
            alpha.replace(
                'test -n "${ImageVersion:-}"',
                "echo accept-unknown-runner-image",
                1,
            ),
            alpha.replace(
                'LINKER_TOOL="$(direct_path "$(command -v cc)")"',
                'LINKER_TOOL="$(command -v cc)"',
                1,
            ),
            alpha.replace(
                "Path(sys.argv[1]).resolve(strict=True)",
                "Path(sys.argv[1])",
                1,
            ),
            alpha.replace("-alpha\.(0|[1-9][0-9]*)$ ]]", "$ ]]", 1),
            alpha.replace(
                "group: openprose-cli-functional-alpha-${{ inputs.version }}",
                "group: openprose-cli-functional-alpha-${{ inputs.source_sha }}",
                1,
            ),
            alpha.replace(
                "python control/cli/ci/create_draft_release.py",
                "python candidate/cli/ci/create_draft_release.py",
                1,
            ),
            alpha.replace("--release-kind functional-alpha", "--release-kind full", 1),
            alpha.replace(
                '--release-notes "$RUNNER_TEMP/openprose-alpha-release-notes.md"',
                '--release-notes "$RUNNER_TEMP/unbound-notes.md"',
                1,
            ),
            alpha.replace(
                "python control/cli/ci/render_release_notes.py",
                "python candidate/cli/ci/render_release_notes.py",
                1,
            ),
            alpha.replace(
                "ref: ${{ github.sha }}\n          path: control",
                "ref: ${{ inputs.source_sha }}\n          path: control",
                1,
            ),
            alpha.replace(
                "ref: ${{ github.sha }}\n          path: control\n          fetch-depth: 0",
                "ref: ${{ github.sha }}\n          path: control",
                1,
            ),
            alpha.replace(
                'test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA"',
                'test -n "$SOURCE_SHA_INPUT"',
                1,
            ),
            alpha.replace(
                'test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA"',
                'test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA" || true',
                1,
            ),
            alpha.replace(
                'test "$SOURCE_SHA_INPUT" = "$CONTROL_SHA_INPUT"',
                'test "$SOURCE_SHA_INPUT" != "$CONTROL_SHA_INPUT"',
                1,
            ),
            alpha.replace(
                'test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA"',
                'git -C . merge-base --is-ancestor "$SOURCE_SHA_INPUT" "$GITHUB_SHA"',
                1,
            ),
            alpha.replace(
                'test "$GITHUB_REF" = "refs/heads/main"', 'test -n "$GITHUB_REF"', 1
            ),
            alpha.replace(
                "name: alpha-release-assets\n          path: release-assets",
                "pattern: alpha-package-*\n          path: release-assets",
                1,
            ),
            alpha.replace("    runs-on: ubuntu-24.04", "    runs-on: ubuntu-22.04", 1),
            alpha.replace(" --require-hashes --only-binary=:all:", "", 1),
            alpha.replace(
                "NPM_REGISTRY_LINEAGE: cli/release/npm-registry-lineage.v1.json",
                "NPM_REGISTRY_LINEAGE: cli/release/unbound-lineage.json",
                1,
            ),
            alpha.replace(
                'python cli/ci/check_registry_lineage.py --authority "$NPM_REGISTRY_LINEAGE" --version "$VERSION_INPUT"',
                "echo skip-candidate-registry-lineage",
                1,
            ),
            alpha.replace(
                'python control/cli/ci/check_registry_lineage.py --authority "control/$NPM_REGISTRY_LINEAGE" --version "$VERSION_INPUT"',
                "echo skip-controller-registry-lineage",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index, length=len(changed)):
                self.assertNotEqual(alpha, changed)
                self.assertTrue(audit_alpha(changed))

    def test_alpha_draft_helper_and_single_body_policy_rejects_custody_drift(
        self,
    ) -> None:
        alpha = ALPHA_WORKFLOW.read_text("utf-8")
        helper = DRAFT_HELPER.read_text("utf-8")
        renderer = (ROOT / "cli" / "ci" / "render_release_notes.py").read_text("utf-8")
        self.assertEqual([], audit_alpha(alpha, helper, renderer))
        helper_mutations = (
            helper.replace(
                "body = _recapture_asset(asset)",
                "body = asset.path.read_bytes()",
                1,
            ),
            helper.replace(
                "if list(declared) != sorted(declared):",
                "if False:",
                1,
            ),
            helper.replace(
                "if set(declared) != required | artifact_names:",
                "if set(declared) < required | artifact_names:",
                1,
            ),
            helper.replace(
                'manifest.get("source", {}).get("revision") != source_sha',
                "False",
                1,
            ),
            helper.replace('tag = f"cli-v{version}"', 'tag = f"wrong-v{version}"', 1),
            helper.replace("prerelease = True", "prerelease = False", 1),
            helper.replace(
                "release_notes = _capture_release_notes(release_notes_path)",
                'release_notes = release_notes_path.read_text("utf-8")',
                1,
            ),
        )
        for changed in helper_mutations:
            with self.subTest(kind="helper", length=len(changed)):
                self.assertNotEqual(helper, changed)
                self.assertTrue(audit_alpha(alpha, changed, renderer))

        renderer_mutations = (
            renderer.replace(
                "does not execute the OpenProse language", "executes OpenProse"
            ),
            renderer.replace("missing or duplicate checksum entry", "checksum entry"),
            renderer.replace('"$PROSE" cli doctor', "echo skip-doctor"),
            renderer.replace(
                'PROSE="$INSTALL_PREFIX/bin/prose"',
                "PROSE=prose",
                1,
            ),
            renderer.replace(
                'INSTALL_PREFIX="$HOME/.local/openprose-cli-__VERSION__-npm-$PLATFORM"',
                'INSTALL_PREFIX="$HOME/.local"',
            ),
            renderer.replace(
                "test ! -e hello.prose.md",
                "echo overwrite-hello-example",
                1,
            ),
        )
        for changed in renderer_mutations:
            with self.subTest(kind="renderer", length=len(changed)):
                self.assertNotEqual(renderer, changed)
                self.assertTrue(audit_alpha(alpha, helper, changed))

    def test_alpha_assembly_cannot_trust_a_candidate_supplied_validator(self) -> None:
        alpha = ALPHA_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_alpha(alpha))
        weakened = alpha.replace(
            "python control/cli/ci/alpha_package_admission.py verify-report",
            "python candidate/cli/ci/alpha_package_admission.py verify-report",
            1,
        )
        self.assertNotEqual(alpha, weakened)
        self.assertTrue(audit_alpha(weakened))

    def test_alpha_attests_only_the_closed_assembly_before_artifact_upload(
        self,
    ) -> None:
        alpha = ALPHA_WORKFLOW.read_text("utf-8")
        pin = "1e69f48acb82d1966a394da916b4c1698aa569d6"
        assemble = alpha.split("\n  assemble:\n", 1)[1].split("\n  draft:\n", 1)[0]
        expected_permissions = (
            "    permissions:\n"
            "      contents: read\n"
            "      id-token: write\n"
            "      attestations: write\n"
            "      artifact-metadata: write\n"
        )
        attestation = (
            "      - name: Attest exact closed release assets\n"
            f"        uses: actions/attest@{pin}\n"
            "        with:\n"
            "          subject-path: release-assets/*\n"
        )
        upload = (
            "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n"
            "        with:\n"
            "          name: alpha-release-assets\n"
            "          path: release-assets\n"
            "          if-no-files-found: error\n"
            "          retention-days: 14\n"
        )
        self.assertEqual([], audit_alpha(alpha))
        self.assertEqual(1, alpha.count(f"actions/attest@{pin}"))
        self.assertIn(expected_permissions, assemble)
        self.assertIn(attestation, assemble)
        self.assertLess(
            assemble.index("- name: Revalidate and assemble exact alpha assets"),
            assemble.index("- name: Attest exact closed release assets"),
        )
        self.assertLess(
            assemble.index("- name: Attest exact closed release assets"),
            assemble.index("uses: actions/upload-artifact@"),
        )

        mutations = (
            alpha.replace(pin, "0" * 40, 1),
            alpha.replace(attestation, "", 1),
            alpha.replace(
                "          subject-path: release-assets/*",
                "          subject-path: release-assets",
                1,
            ),
            alpha.replace(
                "          subject-path: release-assets/*",
                "          subject-path: release-assets/*.tar.gz",
                1,
            ),
            alpha.replace(
                "          subject-path: release-assets/*",
                "          subject-path: '**/*'",
                1,
            ),
            alpha.replace(
                "          subject-path: release-assets/*",
                "          subject-path: release-assets/*\n"
                "          push-to-registry: true",
                1,
            ),
            alpha.replace("      id-token: write\n", "", 1),
            alpha.replace("      attestations: write\n", "", 1),
            alpha.replace("      artifact-metadata: write\n", "", 1),
            alpha.replace(
                "  test:\n",
                "  test:\n    permissions:\n      id-token: write\n",
                1,
            ),
            alpha.replace(
                attestation,
                "",
                1,
            ).replace(upload, upload + attestation, 1),
            alpha.replace(
                "      - name: Attest exact closed release assets",
                "      - name: Publish packages\n        run: npm publish\n"
                "      - name: Attest exact closed release assets",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(alpha, changed)
                self.assertTrue(audit_alpha(changed))

    def test_alpha_dispatch_and_public_docs_preflight_are_unconditional_and_exact(
        self,
    ) -> None:
        alpha = ALPHA_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_alpha(alpha))
        preflight = alpha.split("\n  preflight:\n", 1)[1].split("\n  test:\n", 1)[0]
        self.assertNotIn("    if:", preflight)
        self.assertIn("    permissions:\n      contents: read\n", preflight)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', preflight)
        self.assertIn('test "$DRAFT_ONLY_INPUT" = "true"', preflight)
        self.assertIn('test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA"', preflight)
        self.assertIn(
            "ref: ${{ github.sha }}\n          path: control\n"
            "          fetch-depth: 0\n          persist-credentials: false",
            preflight,
        )
        self.assertIn(
            "ref: ${{ inputs.source_sha }}\n          path: candidate\n"
            "          persist-credentials: false",
            preflight,
        )
        self.assertIn(
            "python control/cli/ci/check_alpha_public_docs.py \\\n"
            '            --version "$VERSION_INPUT" \\\n'
            '            --repository-root "$GITHUB_WORKSPACE/candidate"',
            preflight,
        )
        self.assertIn("  test:\n    needs: preflight\n", alpha)

        mutations = (
            alpha.replace("  preflight:\n", "  preflight:\n    if: success()\n", 1),
            alpha.replace(
                'test "$DRAFT_ONLY_INPUT" = "true"',
                'test -n "$DRAFT_ONLY_INPUT"',
                1,
            ),
            alpha.replace(
                "python control/cli/ci/check_alpha_public_docs.py",
                "python candidate/cli/ci/check_alpha_public_docs.py",
                1,
            ),
            alpha.replace(
                '--repository-root "$GITHUB_WORKSPACE/candidate"',
                '--repository-root "$GITHUB_WORKSPACE/control"',
                1,
            ),
            alpha.replace("  test:\n    needs: preflight\n", "  test:\n", 1),
            alpha.replace(
                'test "$SOURCE_SHA_INPUT" = "$GITHUB_SHA"',
                'test -n "$SOURCE_SHA_INPUT"',
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(alpha, changed)
                self.assertTrue(audit_alpha(changed))

    def test_alpha_draft_retains_one_immutable_sanitized_handoff_authority(
        self,
    ) -> None:
        alpha = ALPHA_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_alpha(alpha))
        draft = alpha.split("\n  draft:\n", 1)[1]
        self.assertIn(
            'AUTHORITY_DIRECTORY="$RUNNER_TEMP/openprose-alpha-draft-authority"',
            draft,
        )
        self.assertIn('mkdir -m 700 "$AUTHORITY_DIRECTORY"', draft)
        self.assertIn(
            'AUTHORITY_OUTPUT="$AUTHORITY_DIRECTORY/alpha-draft-authority.json"',
            draft,
        )
        self.assertIn('--workflow-run-id "$GITHUB_RUN_ID"', draft)
        self.assertIn('--workflow-run-attempt "$GITHUB_RUN_ATTEMPT"', draft)
        self.assertIn('--authority-output "$AUTHORITY_OUTPUT"', draft)
        self.assertIn(
            "name: openprose-cli-alpha-draft-authority-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}\n"
            "          path: ${{ runner.temp }}/openprose-alpha-draft-authority/alpha-draft-authority.json\n"
            "          if-no-files-found: error\n"
            "          retention-days: 90",
            draft,
        )
        for marker in (
            "Functional-alpha draft handoff",
            "Release ID",
            "Authority SHA-256",
            "Retained authority artifact",
            "Candidate bytes remain unauthorized",
        ):
            self.assertIn(marker, draft)

        mutations = (
            alpha.replace('mkdir -m 700 "$AUTHORITY_DIRECTORY"', "mkdir -p /tmp", 1),
            alpha.replace('--workflow-run-id "$GITHUB_RUN_ID" \\\n', "", 1),
            alpha.replace('--workflow-run-attempt "$GITHUB_RUN_ATTEMPT" \\\n', "", 1),
            alpha.replace('--authority-output "$AUTHORITY_OUTPUT"\n', "", 1),
            alpha.replace(
                "          retention-days: 90", "          retention-days: 1", 1
            ),
            alpha.replace(
                "          path: ${{ runner.temp }}/openprose-alpha-draft-authority/alpha-draft-authority.json",
                "          path: release-assets",
                1,
            ),
            alpha.replace("Authority SHA-256", "Authority path", 1),
            alpha.replace(
                "Candidate bytes remain unauthorized",
                "Candidate bytes are authorized",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(alpha, changed)
                self.assertTrue(audit_alpha(changed))

    def _legacy_alpha_promotion_workflow_has_one_exact_protected_operation_graph(
        self,
    ) -> None:
        promotion = PROMOTION_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_promotion(promotion))
        mutations = (
            promotion.replace(
                "  workflow_dispatch:", "  push:\n  workflow_dispatch:", 1
            ),
            promotion.replace("          - stage-meta\n", "", 1),
            promotion.replace(
                "          - settle-and-promote", "          - publish-all", 1
            ),
            promotion.replace(
                "if: github.ref == 'refs/heads/main' && inputs.operation == 'bootstrap'",
                "if: inputs.operation == 'bootstrap'",
                1,
            ),
            promotion.replace(
                "environment: openprose-cli-alpha-publish",
                "environment: openprose-cli-unprotected",
                1,
            ),
            promotion.replace("      id-token: write\n", "", 1),
            promotion.replace(
                "  settle-and-promote:\n",
                "  settle-and-promote:\n    needs: stage-meta\n",
                1,
            ),
            promotion.replace(
                "  settle-and-promote:\n",
                "  settle-and-promote:\n    permissions:\n      id-token: write\n",
                1,
            ),
            promotion.replace(
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/checkout@v4",
                1,
            ),
            promotion.replace(
                "python cli/ci/promote_alpha_release.py",
                "python candidate/cli/ci/promote_alpha_release.py",
                1,
            ),
            promotion.replace('          --release-id "$RELEASE_ID_INPUT"\n', "", 1),
            promotion.replace(
                "${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}",
                "${{ secrets.AMBIENT_TOKEN }}",
                1,
            ),
            promotion.replace(
                "  stage-platforms:\n",
                "  stage-platforms:\n    env:\n      NPM_TOKEN: ${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}\n",
                1,
            ),
            promotion.replace(
                "          python cli/ci/promote_alpha_release.py",
                "          npm publish\n          python cli/ci/promote_alpha_release.py",
                1,
            ),
            promotion.replace(
                "          python cli/ci/promote_alpha_release.py",
                "          npm pack\n          python cli/ci/promote_alpha_release.py",
                1,
            ),
            promotion.replace(
                "          python cli/ci/promote_alpha_release.py",
                "          gh release edit\n          python cli/ci/promote_alpha_release.py",
                1,
            ),
            promotion.replace(
                '--settlement "$PROMOTION_SETTLEMENT"',
                '--settlement "$PROMOTION_SETTLEMENT" --otp 123456',
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(promotion, changed)
                self.assertTrue(audit_promotion(changed))

    def _legacy_alpha_promotion_pins_download_and_authenticated_npm_custody(
        self,
    ) -> None:
        promotion = PROMOTION_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_promotion(promotion))
        download_step = textwrap.dedent(
            """\
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
        )
        # Restore the workflow indentation removed by dedent so mutations exercise
        # the complete step instead of merely matching isolated command fragments.
        download_step = textwrap.indent(download_step, "      ")
        self.assertEqual(3, promotion.count(download_step))

        settle_marker = "      - name: Verify the public npm cohort and promote the exact GitHub draft\n"
        moved_to_settle = promotion.replace(download_step, "", 1).replace(
            settle_marker, download_step + settle_marker, 1
        )
        mutations = (
            # Fixed source identity and workflow-owned hash overrides.
            promotion.replace(
                "https://registry.npmjs.org/npm/-/npm-11.15.0.tgz",
                "https://registry.npmjs.org/npm/-/npm-11.15.1.tgz",
                1,
            ),
            promotion.replace(
                '          chmod 0400 "$tool_root/npm-11.15.0.tgz"',
                '          printf %s "deadbeef" > "$tool_root/npm.sha256"\n'
                '          chmod 0400 "$tool_root/npm-11.15.0.tgz"',
                1,
            ),
            # Curl policy bounds, redirect refusal, and exact output custody.
            promotion.replace("--disable \\", "--disable-epsv \\", 1),
            promotion.replace("--proto '=https'", "--proto '=http,https'", 1),
            promotion.replace("--tlsv1.2", "--tlsv1.0", 1),
            promotion.replace("--max-redirs 0", "--max-redirs 1", 1),
            promotion.replace("--connect-timeout 15", "--connect-timeout 30", 1),
            promotion.replace("--max-time 60", "--max-time 0", 1),
            promotion.replace("--max-filesize 2901197", "--max-filesize 0", 1),
            promotion.replace("/usr/bin/curl --disable", "curl --disable", 1),
            promotion.replace(
                "/usr/bin/curl --disable",
                "/usr/bin/curl --disable --location",
                1,
            ),
            promotion.replace("env -i PATH=/usr/bin:/bin", "env PATH=/usr/bin:/bin", 1),
            promotion.replace(
                'tool_root="$RUNNER_TEMP/openprose-alpha-promotion-tool"',
                'tool_root="/tmp/openprose-alpha-promotion-tool"',
                1,
            ),
            promotion.replace('mkdir "$tool_root"', 'mkdir -p "$tool_root"', 1),
            promotion.replace(
                'npm-11.15.0.tgz" \\\n',
                'npm-client.tgz" \\\n',
                1,
            ),
            promotion.replace("chmod 0400", "chmod 0600", 1),
            # Placement, exact count, credential isolation, and downloader choice.
            moved_to_settle,
            promotion.replace(download_step, download_step + download_step, 1),
            promotion.replace(
                "        run: |\n          set -eu",
                "        env:\n          NODE_AUTH_TOKEN: ${{ secrets.AMBIENT_TOKEN }}\n"
                "        run: |\n          set -eu",
                1,
            ),
            promotion.replace("/usr/bin/curl --disable", "/usr/bin/wget", 1),
            promotion.replace(
                "      - name: Publish the first platform package cohort and meta package\n",
                "      - name: Download through an alternate program\n"
                "        run: python -c \"import urllib.request; urllib.request.urlopen('https://example.invalid/tool')\"\n"
                "      - name: Publish the first platform package cohort and meta package\n",
                1,
            ),
            promotion.replace(
                '          chmod 0400 "$tool_root/npm-11.15.0.tgz"',
                '          chmod 0400 "$tool_root/npm-11.15.0.tgz"\n'
                "          npm install --global npm@11.15.0",
                1,
            ),
            # Exact Node setup and the helper-owned authentication boundary.
            promotion.replace('node-version: "24.20.0"', 'node-version: "24.19.0"', 1),
            promotion.replace(
                settle_marker,
                "      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020\n"
                "        with:\n"
                '          node-version: "24.20.0"\n' + settle_marker,
                1,
            ),
            promotion.replace('--npm-tarball "$NPM_TARBALL"', "", 1),
            promotion.replace(
                '          --npm-tarball "$NPM_TARBALL"',
                '          --npm-tarball "/tmp/npm-11.15.0.tgz"',
                1,
            ),
            promotion.replace(
                "          NPM_TARBALL: ${{ runner.temp }}/openprose-alpha-promotion-tool/npm-11.15.0.tgz",
                "          NPM_TARBALL: ${{ runner.temp }}/unverified/npm-11.15.0.tgz",
                1,
            ),
            promotion.replace(
                '          --npm-tarball "$NPM_TARBALL"',
                '          --npm-tarball "$NPM_TARBALL" --npm-sha256 deadbeef',
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(promotion, changed)
                self.assertTrue(audit_promotion(changed))

    def _legacy_alpha_promotion_pins_the_attestation_verifier_boundary(self) -> None:
        promotion = PROMOTION_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_promotion(promotion))
        resolver = textwrap.indent(
            textwrap.dedent(
                """\
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
            ),
            "      ",
        )
        self.assertEqual(4, promotion.count(resolver))
        settle_marker = "      - name: Verify the public npm cohort and promote the exact GitHub draft\n"
        moved_resolver = promotion.replace(resolver, "", 1).replace(
            settle_marker, resolver + settle_marker, 1
        )
        late_resolver = promotion.replace(resolver, "", 1).replace(
            "      - name: Retain sanitized bootstrap settlement\n",
            resolver + "      - name: Retain sanitized bootstrap settlement\n",
            1,
        )
        mutations = (
            # Permissions and resolver placement/count remain exact in every job.
            promotion.replace("      attestations: read\n", "", 1),
            promotion.replace(
                "      contents: write\n      attestations: read\n",
                "      contents: write\n      attestations: read\n      id-token: write\n",
                1,
            ),
            moved_resolver,
            late_resolver,
            promotion.replace(resolver, resolver + resolver, 1),
            # Fixed executable lookup, strict resolution, closed output, and body.
            promotion.replace("/usr/bin/gh", "/usr/local/bin/gh", 1),
            promotion.replace(
                'GH_TOOL="$(python - /usr/bin/gh',
                'GH_TOOL="$(python - "$(command -v gh)"',
                1,
            ),
            promotion.replace(".resolve(strict=True)", ".resolve()", 1),
            promotion.replace(
                '[[ "$GH_TOOL" =~ ^/[A-Za-z0-9._+/@:-]+$ ]]',
                'test -n "$GH_TOOL"',
                1,
            ),
            promotion.replace(
                "printf 'path=%s\\n' \"$GH_TOOL\"",
                "printf 'executable=%s\\n' \"$GH_TOOL\"",
                1,
            ),
            promotion.replace(
                'printf \'path=%s\\n\' "$GH_TOOL" >>"$GITHUB_OUTPUT"',
                'printf \'path=%s\\nversion=latest\\nsha256=deadbeef\\n\' "$GH_TOOL" >>"$GITHUB_OUTPUT"',
                1,
            ),
            # Helper environment/argv are the sole route to version/hash evidence.
            promotion.replace(
                "          GH_TOOL: ${{ steps.github-cli.outputs.path }}\n", "", 1
            ),
            promotion.replace(
                "          GH_TOOL: ${{ steps.github-cli.outputs.path }}",
                "          GH_TOOL: /usr/bin/gh",
                1,
            ),
            promotion.replace('          --github-cli "$GH_TOOL"\n', "", 1),
            promotion.replace(
                '          --github-cli "$GH_TOOL"',
                "          --github-cli /usr/bin/gh",
                1,
            ),
            promotion.replace(
                "          GH_TOOL: ${{ steps.github-cli.outputs.path }}",
                "          GH_TOOL: ${{ steps.github-cli.outputs.path }}\n"
                "          GH_TOOL_VERSION: latest",
                1,
            ),
            # No alternate gh lookup, install, download, or direct attestation route.
            promotion.replace(
                "      - name: Publish the first platform package cohort and meta package\n",
                "      - name: Alternate GitHub CLI lookup\n"
                "        run: command -v gh\n"
                "      - name: Publish the first platform package cohort and meta package\n",
                1,
            ),
            promotion.replace(
                "      - name: Publish the first platform package cohort and meta package\n",
                "      - name: Install another GitHub CLI\n"
                "        run: brew install gh\n"
                "      - name: Publish the first platform package cohort and meta package\n",
                1,
            ),
            promotion.replace(
                "      - name: Publish the first platform package cohort and meta package\n",
                "      - name: Download another GitHub CLI\n"
                "        run: curl https://example.invalid/gh\n"
                "      - name: Publish the first platform package cohort and meta package\n",
                1,
            ),
            promotion.replace(
                "      - name: Publish the first platform package cohort and meta package\n",
                "      - name: Alternate attestation route\n"
                "        uses: actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6\n"
                "      - name: Publish the first platform package cohort and meta package\n",
                1,
            ),
            promotion.replace(
                "          python cli/ci/promote_alpha_release.py",
                "          gh attestation verify release.tgz\n"
                "          python cli/ci/promote_alpha_release.py",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(promotion, changed)
                self.assertTrue(audit_promotion(changed))

    def test_alpha_promotion_workflow_has_one_exact_protected_operation_graph(
        self,
    ) -> None:
        promotion = PROMOTION_WORKFLOW.read_text("utf-8")
        self.assertEqual(3, promotion.count("id-token: write"))
        self.assertIn(
            "      - name: Authenticate the successful functional-alpha draft producer\n",
            promotion,
        )
        self.assertIn(
            '"/repos/$GITHUB_REPOSITORY/actions/runs/$DRAFT_AUTHORITY_RUN_ID_INPUT/attempts/$DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT"',
            promotion,
        )
        self.assertIn(
            'metadata.get("path")\n'
            '              != ".github/workflows/openprose-cli-alpha-release.yml"',
            promotion,
        )
        self.assertEqual([], audit_promotion(promotion))
        mutations = (
            promotion.replace(
                "  workflow_dispatch:", "  push:\n  workflow_dispatch:", 1
            ),
            promotion.replace("      draft_authority_sha256:\n", "", 1),
            promotion.replace(
                'test "$GITHUB_REF" = "refs/heads/main"',
                'test "$GITHUB_REF" = "refs/heads/release"',
                1,
            ),
            promotion.replace(
                "          EXPECTED_CONFIRMATION=", "          OTHER=", 1
            ),
            promotion.replace(
                "  preflight:\n",
                "  preflight:\n    if: inputs.operation == 'bootstrap'\n",
                1,
            ),
            promotion.replace(
                "Authenticate the successful functional-alpha draft producer",
                "Trust the supplied draft producer",
                1,
            ),
            promotion.replace(
                "/actions/runs/$DRAFT_AUTHORITY_RUN_ID_INPUT/attempts/$DRAFT_AUTHORITY_RUN_ATTEMPT_INPUT",
                "/actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT",
                1,
            ),
            promotion.replace(
                'metadata.get("id") != run_id',
                'metadata.get("id") != int(os.environ["GITHUB_RUN_ID"])',
                1,
            ),
            promotion.replace("if not isinstance(metadata, dict):\n", "", 1),
            promotion.replace(
                'metadata.get("run_attempt") != run_attempt',
                'metadata.get("run_attempt") != 1',
                1,
            ),
            promotion.replace(
                ".github/workflows/openprose-cli-alpha-release.yml",
                ".github/workflows/openprose-cli-alpha-promote.yml",
                1,
            ),
            promotion.replace(
                'metadata.get("event") != "workflow_dispatch"',
                'metadata.get("event") != "push"',
                1,
            ),
            promotion.replace(
                'metadata.get("status") != "completed"',
                'metadata.get("status") != "in_progress"',
                1,
            ),
            promotion.replace(
                'metadata.get("conclusion") != "success"',
                'metadata.get("conclusion") != "neutral"',
                1,
            ),
            promotion.replace(
                'metadata.get("head_sha") != os.environ["SOURCE_SHA_INPUT"]',
                'metadata.get("head_sha") != os.environ["GITHUB_SHA"]',
                1,
            ),
            promotion.replace(
                'repository.get("full_name") != os.environ["GITHUB_REPOSITORY"]',
                'repository.get("full_name") != "openprose/other"',
                1,
            ),
            promotion.replace(
                "run-id: ${{ inputs.draft_authority_run_id }}",
                "run-id: ${{ github.run_id }}",
                1,
            ),
            promotion.replace(
                "openprose-cli-alpha-draft-authority-run-${{ inputs.draft_authority_run_id }}-attempt-${{ inputs.draft_authority_run_attempt }}",
                "openprose-cli-alpha-draft-authority-latest",
                1,
            ),
            promotion.replace(
                "  preverify-attestations:\n",
                "  preverify-attestations:\n    environment: openprose-cli-alpha-publish\n",
                1,
            ),
            promotion.replace(
                "      attestations: read\n", "      id-token: write\n", 1
            ),
            promotion.replace(
                "      contents: read\n      id-token: write\n",
                "      contents: read\n",
                1,
            ),
            promotion.replace(
                "  bootstrap:\n",
                "  bootstrap:\n    permissions:\n      contents: write\n      id-token: write\n",
                1,
            ),
            promotion.replace(
                "  bootstrap:\n    needs: [preflight, preverify-attestations]",
                "  bootstrap:\n    needs: preflight",
                1,
            ),
            promotion.replace(
                "  stage-meta:\n",
                "  stage-meta:\n    permissions:\n      attestations: read\n",
                1,
            ),
            promotion.replace(
                '--attestation-evidence-sha256 "$ATTESTATION_EVIDENCE_SHA256"',
                '--github-cli "/usr/bin/gh"',
                1,
            ),
            promotion.replace(
                '          --draft-authority "$DRAFT_AUTHORITY"\n', "", 1
            ),
            promotion.replace(
                "${{ needs.preverify-attestations.outputs.evidence_sha256 }}",
                "${{ inputs.draft_authority_sha256 }}",
                1,
            ),
            promotion.replace(
                "  stage-platforms:\n",
                "  stage-platforms:\n    env:\n      NPM_TOKEN: ${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}\n",
                1,
            ),
            promotion.replace(
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/checkout@v4",
                1,
            ),
            promotion.replace(
                "python cli/ci/promote_alpha_release.py",
                "python candidate/cli/ci/promote_alpha_release.py",
                1,
            ),
            promotion.replace(
                "python cli/ci/promote_alpha_release.py --mode execute-transition",
                "npm publish\n          python cli/ci/promote_alpha_release.py --mode execute-transition",
                1,
            ),
            promotion.replace(
                "python cli/ci/promote_alpha_release.py --mode execute-transition",
                "gh release edit\n          python cli/ci/promote_alpha_release.py --mode execute-transition",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(promotion, changed)
                self.assertTrue(audit_promotion(changed))

    def test_alpha_promotion_pins_download_and_authenticated_npm_custody(
        self,
    ) -> None:
        promotion = PROMOTION_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_promotion(promotion))
        mutations = (
            promotion.replace(
                "https://registry.npmjs.org/npm/-/npm-11.15.0.tgz",
                "https://registry.npmjs.org/npm/-/npm-11.15.1.tgz",
                1,
            ),
            promotion.replace("--proto '=https'", "--proto '=http,https'", 1),
            promotion.replace("--max-redirs 0", "--max-redirs 1", 1),
            promotion.replace("--max-time 60", "--max-time 0", 1),
            promotion.replace("--max-filesize 2901197", "--max-filesize 0", 1),
            promotion.replace("/usr/bin/curl --disable", "curl --disable", 1),
            promotion.replace('mkdir "$tool_root"', 'mkdir -p "$tool_root"', 1),
            promotion.replace("chmod 0400", "chmod 0600", 1),
            promotion.replace('node-version: "24.20.0"', 'node-version: "24.19.0"', 1),
            promotion.replace('--npm-tarball "$NPM_TARBALL"', "", 1),
            promotion.replace(
                "  settle-and-promote:\n",
                "  settle-and-promote:\n    env:\n      NPM_TARBALL: /tmp/npm.tgz\n",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(promotion, changed)
                self.assertTrue(audit_promotion(changed))

    def test_alpha_promotion_pins_the_attestation_verifier_boundary(self) -> None:
        promotion = PROMOTION_WORKFLOW.read_text("utf-8")
        self.assertEqual([], audit_promotion(promotion))
        self.assertEqual(
            1,
            promotion.count(
                "      - name: Resolve the externally provisioned GitHub CLI verifier\n"
            ),
        )
        mutations = (
            promotion.replace("      attestations: read\n", "", 1),
            promotion.replace(
                "      contents: read\n      attestations: read",
                "      contents: write\n      attestations: read",
                1,
            ),
            promotion.replace("/usr/bin/gh", "/usr/local/bin/gh", 1),
            promotion.replace(".resolve(strict=True)", ".resolve()", 1),
            promotion.replace(
                '[[ "$GH_TOOL" =~ ^/[A-Za-z0-9._+/@:-]+$ ]]',
                'test -n "$GH_TOOL"',
                1,
            ),
            promotion.replace('          --github-cli "$GH_TOOL"\n', "", 1),
            promotion.replace(
                "--mode verify-attestations", "--mode execute-transition", 1
            ),
            promotion.replace(
                "          NPM_TOKEN: ${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}",
                "          GH_TOOL: /usr/bin/gh\n          NPM_TOKEN: ${{ secrets.OPENPROSE_NPM_BOOTSTRAP_TOKEN }}",
                1,
            ),
            promotion.replace(
                "      - name: Publish the first platform package cohort and meta package\n",
                "      - name: Alternate attestation route\n"
                "        run: gh attestation verify release.tgz\n"
                "      - name: Publish the first platform package cohort and meta package\n",
                1,
            ),
        )
        for mutation_index, changed in enumerate(mutations):
            with self.subTest(index=mutation_index):
                self.assertNotEqual(promotion, changed)
                self.assertTrue(audit_promotion(changed))

    def texts(self) -> tuple[str, str]:
        return CI_WORKFLOW.read_text("utf-8"), RELEASE_WORKFLOW.read_text("utf-8")

    def test_committed_workflows_satisfy_the_closed_policy(self) -> None:
        self.assertEqual(audit(*self.texts()), [])

    def test_windows_ci_is_native_only_while_posix_keeps_full_admission(self) -> None:
        ci, release = self.texts()
        mutations = (
            ci.replace(
                "        if: matrix.target != 'win-x64'\n        run: python cli/ci/run_local.py",
                "        run: python cli/ci/run_local.py",
                1,
            ),
            ci.replace(
                "        if: matrix.target != 'win-x64'",
                "        if: matrix.target == 'win-x64'",
                1,
            ),
            ci.replace(
                "        if: matrix.target == 'win-x64'\n        run: |\n          python cli/platform/windows-process-host/scripts/verify_static.py",
                "        run: |\n          python cli/platform/windows-process-host/scripts/verify_static.py",
                1,
            ),
            ci.replace(
                "          cargo test --manifest-path cli/platform/windows-process-host/Cargo.toml --locked --offline --all-features\n",
                "",
                1,
            ),
            ci.replace(
                "          cargo test --manifest-path cli/rust/Cargo.toml --workspace --all-targets --features prose-cli/test-seams --locked --offline\n",
                "",
                1,
            ),
            ci.replace("          bun run --cwd cli/bun test\n", "", 1),
            ci.replace("          bun run --cwd cli/bun build\n", "", 1),
            ci.replace(
                "OPENPROSE_WINDOWS_HOST_ADMISSION=0",
                "OPENPROSE_WINDOWS_HOST_ADMISSION=1",
                1,
            ),
            ci.replace(
                "          bun run --cwd cli/bun build",
                "          python cli/ci/run_local.py",
                1,
            ),
        )
        for changed in mutations:
            with self.subTest(length=len(changed)):
                self.assertNotEqual(ci, changed)
                self.assertTrue(audit(changed, release))

    def test_floating_action_permissions_and_failure_weakening_are_rejected(
        self,
    ) -> None:
        ci, release = self.texts()
        mutations = [
            (
                ci.replace(
                    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                    "actions/checkout@v4",
                    1,
                ),
                release,
            ),
            (
                ci,
                release.replace(
                    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0",
                    1,
                ),
            ),
            (
                ci.replace('python-version: "3.10.18"', 'python-version: "3.10"', 1),
                release,
            ),
            (ci.replace(" --require-hashes --only-binary=:all:", "", 1), release),
            (ci, release.replace('node-version: "24.20.0"', 'node-version: "24"', 1)),
            (
                ci,
                release.replace(
                    "permissions:\n      contents: read",
                    "permissions:\n      contents: write",
                    1,
                ),
            ),
            (
                ci,
                release.replace(
                    "timeout-minutes: 10",
                    "continue-on-error: true\n    timeout-minutes: 10",
                    1,
                ),
            ),
            (ci, release + "\n# npm publish\n"),
            (ci, release + "\n# cargo publish\n"),
            (ci, release + "\n# git tag forbidden\n"),
            (ci, release + "\n# git push forbidden\n"),
            (ci, release + "\n# promote forbidden\n"),
            (ci, release + "\n# id-token: write\n"),
            (ci, release + "\n# packages: write\n"),
        ]
        for changed_ci, changed_release in mutations:
            with self.subTest(mutation=len(changed_ci) + len(changed_release)):
                self.assertNotEqual((changed_ci, changed_release), (ci, release))
                self.assertTrue(audit(changed_ci, changed_release))

    def test_target_graph_trigger_and_release_gate_drift_are_rejected(self) -> None:
        ci, release = self.texts()
        mutations = [
            (ci.replace("  pull_request:", "  pull_request_DISABLED:", 1), release),
            (
                ci.replace(
                    ', ".github/workflows/openprose-cli-draft-release.yml"', "", 1
                ),
                release,
            ),
            (ci, release.replace("linux-arm64", "linux-riscv64", 1)),
            (
                ci,
                release.replace(
                    "    runs-on: ubuntu-24.04", "    runs-on: ubuntu-22.04", 1
                ),
            ),
            (
                ci,
                release.replace("needs: profile-admission", "needs: verify-native", 1),
            ),
            (
                ci,
                release.replace(
                    'OPENPROSE_REQUIRE_RELEASE_IMAGE: "1"',
                    'OPENPROSE_REQUIRE_RELEASE_IMAGE: "0"',
                    1,
                ),
            ),
            (
                ci,
                release.replace(
                    '--remap-path-prefix=$CARGO_HOME_ROOT=/cargo-home"',
                    '"',
                    1,
                ),
            ),
            (
                ci,
                release.replace(
                    "cli/bun/scripts/image-bundle.ts build",
                    "cli/bun/scripts/image-bundle.ts check",
                    1,
                ),
            ),
            (ci, release.replace("        if: always()", "", 1)),
            (ci, release.replace('"$VERSION_INPUT"', "'${{ inputs.version }}'", 1)),
            (
                ci,
                release.replace(
                    "    if: inputs.draft_only == true", "    if: always()", 1
                ),
            ),
            (
                ci,
                release.replace(
                    "    environment: openprose-cli-profile-admission",
                    "    # environment: openprose-cli-profile-admission",
                    1,
                ),
            ),
            (
                ci,
                release.replace(
                    "    environment: openprose-cli-draft-release",
                    "    # environment: openprose-cli-draft-release",
                    1,
                ),
            ),
            (ci.replace('"LICENSE", ', "", 1), release),
            (
                ci.replace(
                    "          persist-credentials: false",
                    "          persist-credentials: true",
                    1,
                ),
                release,
            ),
            (
                ci,
                release.replace(
                    "python control/cli/ci/create_draft_release.py",
                    "gh release create",
                    1,
                ),
            ),
            (
                ci,
                release.replace(
                    "    if: github.ref == 'refs/heads/main'", "    if: always()", 1
                ),
            ),
            (
                ci,
                release.replace(
                    "git -C candidate merge-base --is-ancestor",
                    "git -C candidate merge-base",
                    1,
                ),
            ),
            (ci, release.replace("path: control", "path: candidate", 1)),
            (
                ci,
                release.replace(
                    '"dependency-evidence.json"', '"dependency-inventory-missing.json"'
                ),
            ),
        ]
        for changed_ci, changed_release in mutations:
            with self.subTest(mutation=len(changed_ci) + len(changed_release)):
                self.assertNotEqual((changed_ci, changed_release), (ci, release))
                self.assertTrue(audit(changed_ci, changed_release))

    def test_release_package_admission_graph_and_custody_drift_are_rejected(
        self,
    ) -> None:
        ci, release = self.texts()
        admission_offset = release.index("  admit-release-packages:")

        def mutate_admission(old: str, new: str) -> str:
            prefix = release[:admission_offset]
            suffix = release[admission_offset:]
            self.assertIn(old, suffix)
            return prefix + suffix.replace(old, new, 1)

        report_download = (
            "          name: package-admission-linux-x64\n"
            "          path: package-admissions/package-admission-linux-x64"
        )
        mutations = [
            release.replace(
                "  admit-release-packages:\n    needs: package-native",
                "  admit-release-packages:\n    needs: profile-admission",
                1,
            ),
            release.replace(
                "    needs: [package-native, admit-release-packages]",
                "    needs: package-native",
                1,
            ),
            release.replace(
                "python control/cli/ci/release_package_admission.py",
                "python candidate/cli/ci/release_package_admission.py",
                1,
            ),
            mutate_admission(
                "ref: ${{ github.sha }}\n          path: control",
                "ref: ${{ inputs.source_sha }}\n          path: control",
            ),
            mutate_admission(
                '--control-sha "$CONTROL_SHA"', '--control-sha "$SOURCE_SHA"'
            ),
            release.replace(
                report_download, report_download.replace("linux-x64", "darwin-arm"), 1
            ),
            release.replace(
                '"semanticEvaluation": False', '"semanticEvaluation": True', 1
            ),
            release.replace(
                '"providerCalls": "not-observed"', '"providerCalls": "none"', 1
            ),
            release.replace(
                'pathlib.Path("control/cli/conformance/release-package/invariants.v1.json")',
                'pathlib.Path("candidate/cli/conformance/release-package/invariants.v1.json")',
                1,
            ),
            release.replace(
                'require(admission["corpus"] == {',
                'require(admission["corpus"] != {',
                1,
            ),
            release.replace(
                'observation.get("surface") if isinstance(observation, dict) else None for observation in observations] == ["direct-rust", "direct-bun", "npm-launcher"]',
                'observation.get("surface") if isinstance(observation, dict) else None for observation in observations]',
                1,
            ),
            release.replace(
                'require(observation["exitCode"] == expected_case["exitCode"]',
                "require(True",
                1,
            ),
            release.replace(
                'require(observation["stderr"] == {"byteLength": 0, "sha256": empty_sha256}',
                "require(True",
                1,
            ),
            release.replace(
                'require(observation["settlement"] == "settled" and observation["settlementAuthority"] == "direct-and-original-process-group-settled"',
                "require(True",
                1,
            ),
            release.replace(
                'require(observation["projection"] == expected_projection',
                "require(True",
                1,
            ),
            release.replace(
                "          name: package-admission-${{ matrix.target }}\n          path: release-package-admission.json",
                "          name: package-admission-${{ matrix.target }}\n          path: packages",
                1,
            ),
            release.replace(
                "          name: package-admission-failure-${{ matrix.target }}\n          path: release-package-admission-failure.json",
                "          name: package-admission-failure-${{ matrix.target }}\n          path: release-package-admission.json",
                1,
            ),
            mutate_admission("        if: failure()", "        if: success()"),
            mutate_admission(
                'report["schema"] == "openprose.release-package-admission-error/1"',
                'report["schema"] == "openprose.release-package-admission/3"',
            ),
            release.replace(
                "          name: package-linux-x64\n          path: packages/package-linux-x64",
                "          pattern: package-*\n          path: packages/package-linux-x64",
                1,
            ),
        ]
        for index, changed in enumerate(mutations):
            with self.subTest(index=index):
                self.assertNotEqual(changed, release)
                self.assertTrue(audit(ci, changed))

    def test_assembly_rejects_tampered_package_admission_reports(self) -> None:
        def mutate_report(root: Path, target: str, mutate) -> None:
            path = (
                root
                / "package-admissions"
                / f"package-admission-{target}"
                / "release-package-admission.json"
            )
            report = json.loads(path.read_text("utf-8"))
            mutate(report)
            path.write_text(
                json.dumps(
                    report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n",
                "utf-8",
            )

        mutations = (
            (
                "target-swap",
                "linux-x64",
                lambda report: report.__setitem__("targetId", "darwin-arm"),
            ),
            (
                "source-swap",
                "linux-x64",
                lambda report: report.__setitem__("sourceSha", "9" * 40),
            ),
            (
                "control-swap",
                "linux-x64",
                lambda report: report.__setitem__("controlSha", "9" * 40),
            ),
            (
                "run-swap",
                "linux-x64",
                lambda report: report["workflowRun"].__setitem__("id", 124),
            ),
            (
                "file-omission",
                "linux-x64",
                lambda report: report["package"]["files"].pop(),
            ),
            (
                "weak-claim",
                "linux-x64",
                lambda report: report["claims"].__setitem__("semanticEvaluation", True),
            ),
            (
                "provider-overclaim",
                "linux-x64",
                lambda report: report["claims"].__setitem__("providerCalls", "none"),
            ),
            (
                "schema-downgrade",
                "linux-x64",
                lambda report: report.__setitem__(
                    "schema", "openprose.release-package-admission/2"
                ),
            ),
            (
                "toolchain-digest",
                "linux-x64",
                lambda report: report["executionToolchain"]["npm"].__setitem__(
                    "sha256", "invalid"
                ),
            ),
            (
                "windows-toolchain",
                "win-x64",
                lambda report: report.__setitem__(
                    "executionToolchain",
                    {
                        "authority": "reporter-observed-and-finally-reauthenticated-executable-bytes",
                        "node": {},
                        "npm": {},
                    },
                ),
            ),
            (
                "corpus-swap",
                "linux-x64",
                lambda report: report["corpus"].__setitem__("sha256", "9" * 64),
            ),
            (
                "argv-swap",
                "linux-x64",
                lambda report: report["cases"][0].__setitem__("argv", ["--help"]),
            ),
            (
                "surface-swap",
                "linux-x64",
                lambda report: report["cases"][0]["observations"][0].__setitem__(
                    "surface", "direct-bun"
                ),
            ),
            (
                "exit-swap",
                "linux-x64",
                lambda report: report["cases"][0]["observations"][0].__setitem__(
                    "exitCode", 10
                ),
            ),
            (
                "stderr-contamination",
                "linux-x64",
                lambda report: report["cases"][0]["observations"][0].__setitem__(
                    "stderr", {"byteLength": 1, "sha256": "9" * 64}
                ),
            ),
            (
                "settlement-swap",
                "linux-x64",
                lambda report: report["cases"][0]["observations"][0].__setitem__(
                    "settlement", "timed-out"
                ),
            ),
            (
                "projection-string",
                "linux-x64",
                lambda report: report["cases"][0]["observations"][0].__setitem__(
                    "projection", "passed"
                ),
            ),
            (
                "projection-drift",
                "linux-x64",
                lambda report: report["cases"][0]["observations"][0][
                    "projection"
                ].__setitem__("version", "9.9.9"),
            ),
            (
                "windows-overclaim",
                "win-x64",
                lambda report: report.__setitem__(
                    "status", "passed-provider-free-release-invariants"
                ),
            ),
            (
                "windows-execution",
                "win-x64",
                lambda report: report["cases"].append({"id": "executed"}),
            ),
        )
        for label, target, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                create_assembly_fixture(root)
                mutate_report(root, target, mutate)
                result = run_assembly(root)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse((root / "assembly/SHA256SUMS").exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_assembly_fixture(root)
            report = (
                root
                / "package-admissions/package-admission-linux-x64/release-package-admission.json"
            )
            report.unlink()
            (report.parent / "reuploaded-package.tgz").write_bytes(b"forbidden")
            result = run_assembly(root)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "assembly/SHA256SUMS").exists())

    def test_dependency_evidence_must_be_in_the_actual_assembly_copy_loop(self) -> None:
        ci, release = self.texts()
        exact = (
            "EVIDENCE_NAMES = (\n"
            '              "release-manifest.json",\n'
            '              "sbom.cdx.json",\n'
            '              "provenance.json",\n'
            '              "dependency-evidence.json",\n'
            "          )"
        )
        omitted = (
            "EVIDENCE_NAMES = (\n"
            '              "release-manifest.json",\n'
            '              "sbom.cdx.json",\n'
            '              "provenance.json",\n'
            "          )"
        )
        self.assertIn(exact, release)
        changed = release.replace(exact, omitted, 1)
        self.assertIn("dependency-evidence.json", changed)
        self.assertTrue(audit(ci, changed))

    def test_assembly_checksum_custody_must_remain_closed_and_bounded(self) -> None:
        ci, release = self.texts()
        mutations = [
            release.replace(
                "MAX_CHECKSUM_BYTES = 16 * 1024", "MAX_CHECKSUM_BYTES = 0", 1
            ),
            release.replace("MAX_CHECKSUM_LINES = 8", "MAX_CHECKSUM_LINES = 80", 1),
            release.replace(
                "MAX_CHECKSUM_LINE_BYTES = 512", "MAX_CHECKSUM_LINE_BYTES = 0", 1
            ),
            release.replace(
                "MAX_FILE_BYTES = 512 * 1024 * 1024", "MAX_FILE_BYTES = 0", 1
            ),
            release.replace(
                "MAX_PACKAGE_BYTES = 256 * 1024 * 1024", "MAX_PACKAGE_BYTES = 0", 1
            ),
            release.replace(
                "MAX_PACKAGE_SET_BYTES = 1024 * 1024 * 1024",
                "MAX_PACKAGE_SET_BYTES = 0",
                1,
            ),
            release.replace(
                "MAX_EVIDENCE_BYTES = 16 * 1024 * 1024", "MAX_EVIDENCE_BYTES = 0", 1
            ),
            release.replace(
                '"linux-x64": "linux-x64-gnu",', '"linux-x64": "linux-x64-musl",', 1
            ),
            release.replace(
                'f"openprose-prose-cli-bun-{version}-{platform_identifier}.tar.gz",',
                'f"openprose-prose-cli-bun-{version}-{platform_identifier}.zip",',
                1,
            ),
            release.replace('and "\\\\" not in name', "and True", 1),
            release.replace("and stat.S_ISREG(linked.st_mode)", "and True", 1),
            release.replace(" | os.O_NOFOLLOW", "", 1),
            release.replace('destination.open("xb")', 'destination.open("wb")', 1),
            release.replace(
                "destination.parent == output", "destination.parent != output", 1
            ),
            release.replace(
                'directory_names(package, expected_names | {"SHA256SUMS"}, MAX_CHECKSUM_LINES + 1)',
                "directory_names(package, expected_names, MAX_CHECKSUM_LINES + 1)",
                1,
            ),
            release.replace(
                'read_regular(package / "SHA256SUMS", MAX_CHECKSUM_BYTES)',
                'read_regular(package / "SHA256SUMS", MAX_FILE_BYTES)',
                1,
            ),
            release.replace(
                "0 < len(lines) <= MAX_CHECKSUM_LINES", "0 < len(lines)", 1
            ),
            release.replace("name not in records", "True", 1),
            release.replace(
                "set(records) == expected_names", "set(records) <= expected_names", 1
            ),
            release.replace("list(records) == sorted(expected_names)", "True", 1),
            release.replace("package_snapshot_bytes <= MAX_PACKAGE_BYTES", "True", 1),
            release.replace("package_set_bytes <= MAX_PACKAGE_SET_BYTES", "True", 1),
            release.replace("observed_package_bytes <= MAX_PACKAGE_BYTES", "True", 1),
            release.replace(
                "observed_package_bytes == package_snapshot_bytes", "True", 1
            ),
            release.replace(
                "read_regular(package / name, MAX_FILE_BYTES)",
                "(package / name).read_bytes()",
                1,
            ),
            release.replace(
                "read_regular(destination, maximum) == encoded",
                "True",
                1,
            ),
        ]
        for index, changed in enumerate(mutations):
            with self.subTest(index=index, length=len(changed)):
                self.assertNotEqual(changed, release)
                self.assertTrue(audit(ci, changed))

    def test_assembly_executes_the_closed_manifest_and_rejects_adversarial_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_assembly_fixture(root)
            result = run_assembly(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            assembled = root / "assembly"
            self.assertEqual(len(tuple(assembled.iterdir())), 64)
            checksum_lines = (assembled / "SHA256SUMS").read_text("ascii").splitlines()
            self.assertEqual(len(checksum_lines), 63)
            self.assertEqual(
                checksum_lines,
                sorted(checksum_lines, key=lambda line: line.split("  ", 1)[1]),
            )

        def add_extra_file(root: Path) -> None:
            (root / "packages/package-linux-x64/unexpected.bin").write_bytes(
                b"unexpected"
            )

        def add_traversal_name(root: Path) -> None:
            checksum = root / "packages/package-linux-x64/SHA256SUMS"
            lines = checksum.read_text("ascii").splitlines(keepends=True)
            digest, _ = lines[0].split("  ", 1)
            lines[0] = f"{digest}  ../escape\n"
            checksum.write_text("".join(lines), "ascii")

        def exceed_checksum_byte_bound(root: Path) -> None:
            (root / "packages/package-linux-x64/SHA256SUMS").write_bytes(
                b"x" * (16 * 1024 + 1)
            )

        def duplicate_checksum_name(root: Path) -> None:
            checksum = root / "packages/package-linux-x64/SHA256SUMS"
            lines = checksum.read_text("ascii").splitlines(keepends=True)
            lines[-1] = lines[0]
            checksum.write_text("".join(lines), "ascii")

        def exceed_checksum_line_bound(root: Path) -> None:
            checksum = root / "packages/package-linux-x64/SHA256SUMS"
            lines = checksum.read_text("ascii").splitlines(keepends=True)
            checksum.write_text("".join((*lines, lines[0])), "ascii")

        def exceed_package_aggregate_bound(root: Path) -> None:
            source = root / "packages/package-linux-x64/release-manifest.json"
            with source.open("r+b") as opened:
                opened.truncate(256 * 1024 * 1024 + 1)

        def replace_source_with_symlink(root: Path) -> None:
            source = root / "packages/package-linux-x64/release-manifest.json"
            outside = root / "outside.json"
            outside.write_bytes(source.read_bytes())
            source.unlink()
            source.symlink_to(outside)

        mutations = (
            ("extra-file", add_extra_file),
            ("traversal-name", add_traversal_name),
            ("checksum-byte-bound", exceed_checksum_byte_bound),
            ("duplicate-name", duplicate_checksum_name),
            ("checksum-line-bound", exceed_checksum_line_bound),
            ("package-aggregate-bound", exceed_package_aggregate_bound),
            ("symlink-source", replace_source_with_symlink),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                create_assembly_fixture(root)
                mutate(root)
                result = run_assembly(root)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse((root / "assembly/SHA256SUMS").exists())

    def test_candidate_cannot_supply_or_impersonate_protected_release_authority(
        self,
    ) -> None:
        ci, release = self.texts()
        mutations = [
            release.replace(
                '--canonical-profile "protected-authority/$CANONICAL_PROFILE"',
                '--canonical-profile "candidate/$CANONICAL_PROFILE"',
                1,
            ),
            release.replace(
                '--release-evidence "authority/protected-authority/$RELEASE_EVIDENCE"',
                '--release-evidence "candidate/$RELEASE_EVIDENCE"',
                1,
            ),
            release.replace(
                '--canonical-profile "protected/authority/protected-authority/$CANONICAL_PROFILE"',
                '--canonical-profile "$CANONICAL_PROFILE"',
                1,
            ),
            release.replace(
                "artifact-ids: ${{ inputs.authority_artifact_id }}",
                "name: openprose-cli-protected-release-authority",
                1,
            ),
            release.replace("run-id: ${{ inputs.authority_run_id }}", "run-id: 1", 1),
            release.replace("merge-multiple: true", "merge-multiple: false", 1),
            release.replace(
                'run["path"] == os.environ["PROTECTED_AUTHORITY_WORKFLOW"]',
                'run["path"] != os.environ["PROTECTED_AUTHORITY_WORKFLOW"]',
                1,
            ),
            release.replace('artifact["workflow_run"]["id"] == run_id', "True", 1),
            release.replace("      actions: read", "      actions: write", 1),
            release.replace(
                ".github/workflows/openprose-cli-protected-release-authority.yml",
                ".github/workflows/openprose-cli-draft-release.yml",
                1,
            ),
            release.replace("openprose-cli-release-authority", "unprotected", 1),
            release.replace(
                "authority/protected-authority-run.json", "candidate/run.json", 1
            ),
        ]
        for changed in mutations:
            with self.subTest(mutation=len(changed)):
                self.assertNotEqual(changed, release)
                self.assertTrue(audit(ci, changed))

    def test_native_verification_child_execution_must_remain_bounded(self) -> None:
        ci, release = self.texts()
        mutations = [
            release.replace(
                "MAX_CHILD_OUTPUT_BYTES = 1024 * 1024", "MAX_CHILD_OUTPUT_BYTES = 0", 1
            ),
            release.replace(
                "CHILD_TIMEOUT_SECONDS = 15", "CHILD_TIMEOUT_SECONDS = 0", 1
            ),
            release.replace("subprocess.Popen(", "subprocess.run(", 1),
            release.replace("if overflow.is_set():", "if False:", 1),
            release.replace(
                "command,\n                  stdin=subprocess.DEVNULL,",
                "command,\n                  capture_output=True,",
                1,
            ),
        ]
        for changed in mutations:
            with self.subTest(mutation=len(changed)):
                self.assertNotEqual(changed, release)
                self.assertTrue(audit(ci, changed))

    def test_executed_native_trees_can_only_emit_digest_bound_reports(self) -> None:
        ci, release = self.texts()
        extra_upload = (
            "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n"
            "        if: always()\n"
            "        with:\n"
            "          name: leaked-executed-${{ matrix.target }}\n"
            "          path: candidate-native\n"
            "          if-no-files-found: error\n"
        )
        verification_upload = (
            "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n"
            "        with:\n"
            "          name: native-verification-${{ matrix.target }}"
        )
        mutations = [
            release.replace(
                "name: native-build-${{ matrix.target }}",
                "name: native-${{ matrix.target }}",
                1,
            ),
            release.replace(
                "path: native/${{ matrix.target }}", "path: candidate-native", 1
            ),
            release.replace(
                "path: verification/${{ matrix.target }}", "path: candidate-native", 1
            ),
            release.replace(
                "name: native-verification-${{ matrix.target }}",
                "name: verified-${{ matrix.target }}",
                1,
            ),
            release.replace(
                'root = pathlib.Path("candidate-native")',
                'root = pathlib.Path("verification")',
                1,
            ),
            release.replace(
                'report_root = pathlib.Path("verification") / manifest["target"]',
                "report_root = root",
                1,
            ),
            release.replace(
                '"workflowRunId": int(os.environ["WORKFLOW_RUN_ID"])',
                '"workflowRunId": 0',
                1,
            ),
            release.replace(
                '"workflowRunAttempt": int(os.environ["WORKFLOW_RUN_ATTEMPT"])',
                '"workflowRunAttempt": 0',
                1,
            ),
            release.replace(
                "pattern: native-verification-*", "pattern: native-build-*", 1
            ),
            release.replace(
                'assert {path.name for path in directory.iterdir()} == {"verification.json"}',
                'assert (directory / "verification.json").exists()',
                1,
            ),
            release.replace(
                "name: native-build-${{ matrix.target }}\n          path: incoming",
                "name: native-verification-${{ matrix.target }}\n          path: incoming",
                1,
            ),
            release.replace(
                "name: native-verification-${{ matrix.target }}\n          path: verification",
                "name: native-build-${{ matrix.target }}\n          path: verification",
                1,
            ),
            release.replace(
                "assert verification_bytes == protected_verification.read_bytes()",
                "assert verification_bytes",
                1,
            ),
            release.replace('"files": observed_files}', '"files": {}}', 1),
            release.replace(
                "pattern: native-build-*\n          path: native-builds",
                "pattern: package-*\n          path: native-builds",
                1,
            ),
            release.replace(
                'require(verification["nativeArtifact"] == {"name": f"native-build-{target}"',
                'require(verification["nativeArtifact"] != {"name": f"native-build-{target}"',
                1,
            ),
            release.replace(
                'require(verification["nativeManifestSha256"] == hashlib.sha256(native_manifest_bytes).hexdigest()',
                "require(True",
                1,
            ),
            release.replace(verification_upload, extra_upload + verification_upload, 1),
            release.replace(
                "python cli/ci/package_local.py", "python attacker-controlled.py", 1
            ),
            release.replace(
                '--rust-binary "incoming/prose-rust$SUFFIX"',
                '--rust-binary "attacker/prose-rust$SUFFIX"',
                1,
            ),
            release.replace(
                '--bun-binary "incoming/prose-bun$SUFFIX"',
                '--bun-binary "attacker/prose-bun$SUFFIX"',
                1,
            ),
            release.replace(
                'elif [[ "$TARGET_ID" == linux-* ]]; then',
                'elif [[ "$TARGET_ID" == darwin-* ]]; then',
                1,
            ),
            release.replace(
                'discovered = shutil.which("readelf")',
                'discovered = "incoming/prose-rust"',
                1,
            ),
            release.replace(
                "print(Path(discovered).resolve(strict=True))",
                "print(discovered)",
                1,
            ),
            release.replace(
                'args+=(--readelf "$READELF_TOOL")',
                'args+=(--readelf "incoming/prose-rust")',
                1,
            ),
            release.replace(
                "args+=(--windows-process-host incoming/openprose-windows-process-host.exe)",
                "args+=(--windows-process-host attacker/host.exe)",
                1,
            ),
            release.replace(
                'assert {"runner": report["runner"], "build": report["build"], "image": report["image"]} == expected_report',
                "assert report",
                1,
            ),
            release.replace(
                'assert value["reports"] == expected_reports',
                'assert set(value["reports"]) == {"rust", "bun"}',
                1,
            ),
            release.replace(
                'assert verification["reports"] == expected_reports',
                'assert set(verification["reports"]) == {"rust", "bun"}',
                1,
            ),
            release.replace(
                'require(verification["reports"] == expected_reports',
                'require(set(verification["reports"]) == {"rust", "bun"}',
                1,
            ),
            release.replace(
                '"blocker": "detached-descendant-containment-not-enforced"',
                '"blocker": None',
                1,
            ),
            release.replace('report_path.open("xb")', 'report_path.open("wb")', 1),
        ]
        for index, changed in enumerate(mutations):
            with self.subTest(index=index):
                self.assertNotEqual(changed, release)
                self.assertTrue(audit(ci, changed))

    def test_protected_dependency_evidence_is_structurally_bound_end_to_end(
        self,
    ) -> None:
        ci, release = self.texts()
        mutations = [
            release.replace(
                "python control/cli/ci/dependency_evidence.py report",
                "python candidate/cli/ci/dependency_evidence.py report",
                1,
            ),
            release.replace("--root candidate\n", "--root control\n", 1),
            release.replace(
                'place_source(pathlib.Path("protected/protected-dependency-evidence.json"), "protected-dependency-evidence.json")',
                'pathlib.Path("protected/protected-dependency-evidence.json")',
                1,
            ),
            release.replace(
                "assert dependency_bytes == protected_dependency_bytes",
                "assert dependency_bytes != protected_dependency_bytes",
                1,
            ),
        ]
        for changed in mutations:
            with self.subTest(mutation=len(changed)):
                self.assertNotEqual(changed, release)
                self.assertIn("protected-dependency-evidence.json", changed)
                self.assertTrue(audit(ci, changed))

    def test_draft_helper_cannot_weaken_literal_draft_or_enter_a_shell(self) -> None:
        ci, release = self.texts()
        helper = DRAFT_HELPER.read_text("utf-8")
        mutations = [
            helper.replace('"draft": True', '"draft": False', 1),
            "import subprocess\n" + helper,
            helper.replace(
                '"publicationAuthorized": False', '"publicationAuthorized": True', 1
            ),
        ]
        for changed in mutations:
            with self.subTest(length=len(changed)):
                self.assertNotEqual(changed, helper)
                self.assertTrue(audit(ci, release, changed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
