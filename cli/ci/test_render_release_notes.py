from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("render_release_notes.py")
SPEC = importlib.util.spec_from_file_location("openprose_render_release_notes", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import release-note renderer: {SCRIPT}")
NOTES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NOTES)

VERSION = "1.2.3-rc.1+build.7"
SOURCE_SHA = "1" * 40
PROVIDER_CHARGE_BOUNDARY = (
    "The run command contacts the selected provider and may incur charges under "
    "the signed-in account. The CLI cannot determine the account or billing route."
)
IMAGE = {
    "formatVersion": "openprose.skill-runtime-image/1",
    "version": "openprose-1.2.3",
    "sha256": "2" * 64,
    "manifestSha256": "3" * 64,
    "purpose": "canonical-language-runtime",
    "releaseEligible": True,
}
HOST = {
    "path": "openprose-windows-process-host.exe",
    "sha256": "4" * 64,
    "byteLength": 98765,
    "admission": False,
}


def artifact(
    path: str, implementation: str, kind: str, platform: str | None
) -> dict[str, object]:
    return {
        "path": path,
        "kind": kind,
        "implementation": implementation,
        "platform": platform,
        "byteLength": len(path) * 101,
        "sha256": hashlib.sha256(path.encode()).hexdigest(),
    }


def make_manifests() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    meta = f"openprose-prose-cli-{VERSION}.tgz"
    for target, platform in NOTES.TARGET_PLATFORMS.items():
        result[target] = {
            "schema": "openprose.local-release-manifest/1",
            "mode": "release",
            "version": VERSION,
            "platform": platform,
            "sourceDateEpoch": 0,
            "releaseEligible": False,
            "publicationAuthorized": False,
            "promotion": {
                "status": "not-performed",
                "requiredAttestation": "protected-release-validator",
            },
            "source": {
                "revision": SOURCE_SHA,
                "verification": "matched-product-doctor",
            },
            "buildProfiles": {
                "rust": {"profile": "release", "testSeamsEnabled": False},
                "bun": {"profile": "release", "testSeamsEnabled": False},
            },
            "bunRuntime": copy.deepcopy(NOTES.BUN_RUNTIME_BY_TARGET[target]),
            "linuxRuntime": (
                {
                    "minimumGlibc": "2.34",
                    "requiredGlibcMaximum": {"rust": "2.34", "bun": "2.34"},
                    "executionEvidence": "ubuntu-22.04-only",
                }
                if target.startswith("linux-")
                else "not-applicable"
            ),
            "image": copy.deepcopy(IMAGE),
            "windowsProcessHost": copy.deepcopy(HOST)
            if target == "win-x64"
            else "not-applicable",
            "windowsJobObjectReleaseAdmission": False,
            "toolchains": {
                "python": "3.10.18",
                "rustc": "rustc 1.87.0",
                "cargo": "cargo 1.87.0",
                "bun": "1.3.5",
                "node": "v24.20.0",
                "npm": "10.8.2",
            },
            "lockfiles": {"cargoSha256": "5" * 64, "bunSha256": "6" * 64},
            "dependencyEvidence": {
                "path": "dependency-evidence.json",
                "byteLength": 12345,
                "sha256": "9" * 64,
                "releasePolicyPassed": False,
            },
            "externalGates": {
                "canonicalProfile": {"sha256": "7" * 64, "byteLength": 321},
                "releaseEvidence": {"sha256": "8" * 64, "byteLength": 654},
                "authorityValidatedByPackager": False,
            },
            "claims": {
                "signing": "not-performed",
                "vulnerabilityReview": "not-performed",
                "networkIsolation": "not-enforced",
                "packageTests": "not-run-by-packager",
            },
            "artifacts": [
                artifact(
                    f"openprose-prose-cli-rust-{VERSION}-{platform}.tar.gz",
                    "rust",
                    "standalone-archive",
                    platform,
                ),
                artifact(
                    f"openprose-prose-cli-bun-{VERSION}-{platform}.tar.gz",
                    "bun",
                    "standalone-archive",
                    platform,
                ),
                artifact(meta, "bun", "npm-meta", None),
                artifact(
                    f"openprose-prose-cli-{platform}-{VERSION}.tgz",
                    "bun",
                    "npm-platform",
                    platform,
                ),
            ],
        }
    return result


def write_assembly(root: Path, manifests: dict[str, dict[str, object]]) -> None:
    checksums: list[str] = []
    for target, value in manifests.items():
        path = root / f"{target}-release-manifest.json"
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        checksums.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        )
    (root / "SHA256SUMS").write_text("".join(sorted(checksums)), encoding="ascii")


class ReleaseNotesTests(unittest.TestCase):
    def test_functional_alpha_notes_are_one_deterministic_copy_paste_body(self) -> None:
        version = "1.2.3-alpha.4"
        first = NOTES.render_functional_alpha_notes(version)
        second = NOTES.render_functional_alpha_notes(version)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        text = first.decode("utf-8")
        for marker in (
            "## OpenProse CLI functional alpha",
            f"openprose-prose-cli-rust-{version}-<platform>.tar.gz",
            f'ARCHIVE="openprose-prose-cli-$IMPLEMENTATION-{version}-$PLATFORM.tar.gz"',
            "does not execute the OpenProse language",
            "missing or duplicate checksum entry",
            'gh attestation verify "$ASSET" --repo openprose/prose',
            "including the aggregate `SHA256SUMS` asset",
            "GitHub Actions build provenance",
            "does not sign or notarize binaries",
            "does not authorize publication",
            f'INSTALL_PREFIX="$HOME/.local/openprose-cli-{version}-$IMPLEMENTATION-$PLATFORM"',
            f'INSTALL_PREFIX="$HOME/.local/openprose-cli-{version}-npm-$PLATFORM"',
            'test ! -e "$INSTALL_PREFIX"',
            'PROSE="$INSTALL_PREFIX/bin/prose"',
            'install -m 0644 "$SOURCE/README.txt" "$INSTALL_PREFIX/README.txt"',
            'install -m 0644 "$SOURCE/LICENSE" "$INSTALL_PREFIX/LICENSE"',
            'test -f "$INSTALL_PREFIX/README.txt" && test ! -L "$INSTALL_PREFIX/README.txt"',
            'test -f "$INSTALL_PREFIX/LICENSE" && test ! -L "$INSTALL_PREFIX/LICENSE"',
            "The installed `README.txt` retains offline support and safe same-version repair guidance",
            'npm install --global --offline --ignore-scripts --prefix "$INSTALL_PREFIX"',
            'EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
            "test ! -e hello.prose.md",
            '"$PROSE" cli harness list',
            '"$PROSE" cli harness use codex',
            '"$PROSE" cli harness use prime --model openai-codex/gpt-5.4 --auth-profile prime-harness-login',
            '"$PROSE" cli harness use omp --model openai-codex/gpt-5.4 --auth-profile omp-harness-login',
            '"$PROSE" cli harness use claude',
            '"$PROSE" cli doctor',
            '"$PROSE" run hello.prose.md',
            "transports and echoes only the opaque `prose run <path>` task argv",
            "does not open or read the packaged example file",
            "does not evaluate the packaged OpenProse contract",
            "Candidate evidence and these release notes do not authorize or prove publication",
            "Only after this functional alpha has been independently authorized and publicly promoted",
            '"@openprose/prose-cli@alpha"',
            f'"@openprose/prose-cli@{version}"',
            "The offline two-tarball route remains available before promotion and for custody verification",
            "requires `bun >=1.3.14`",
            "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
            "this alpha is not a sandbox or strict descendant-containment boundary",
            "Prime and Claude isolation is advisory",
            "OMP and Codex isolation is unsupported",
            "Ambient harness configuration, plugins, skills, cached account/provider state, and provider-side routing remain external and unbound",
            "### Remove",
            'rm -f -- "$INSTALL_PREFIX/bin/prose" "$INSTALL_PREFIX/examples/hello.prose.md" "$INSTALL_PREFIX/README.txt" "$INSTALL_PREFIX/LICENSE"',
            'npm uninstall --global --prefix "$INSTALL_PREFIX" @openprose/prose-cli "@openprose/prose-cli-$PLATFORM"',
            "https://github.com/openprose/prose/blob/main/cli/SUPPORT.md",
            "https://github.com/openprose/prose/issues/new?template=openprose-cli-bug.yml",
            "https://github.com/openprose/prose/security/advisories/new",
        ):
            self.assertIn(marker, text)
        for forbidden in (
            "$HOME/.local/bin/prose",
            "PROSE=prose",
            "npm root --global",
            'npm install --global --ignore-scripts "./openprose',
            "contract instructions",
            "draft",
        ):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("${{ inputs.version }}", text)
        self.assertNotIn("# Choose exactly one selection:", text)
        verification = text.split("Download `SHA256SUMS` from this release", 1)[
            1
        ].split("### Install", 1)[0]
        self.assertEqual(
            1,
            verification.count('gh attestation verify "$ASSET" --repo openprose/prose'),
        )
        self.assertLess(
            verification.index("sha256sum -c -"),
            verification.index('gh attestation verify "$ASSET"'),
        )
        codex = text.split("### First run", 1)[1].split(
            "The independently copyable alternatives", 1
        )[0]
        codex_commands = (
            PROVIDER_CHARGE_BOUNDARY,
            "npm install --global @openai/codex@0.149.0-alpha.4.1",
            "codex login",
            '"$PROSE" cli harness list',
            '"$PROSE" cli harness use codex',
            '"$PROSE" cli doctor',
            '"$PROSE" run hello.prose.md',
        )
        codex_positions = [codex.index(command) for command in codex_commands]
        self.assertEqual(codex_positions, sorted(codex_positions))
        for start, end, platform in (
            (
                "Prime — macOS Apple silicon only:",
                "OMP — macOS Apple silicon:",
                "darwin-arm64",
            ),
            (
                "OMP — macOS Apple silicon:",
                "OMP — Linux x64:",
                "darwin-arm64",
            ),
            (
                "OMP — Linux x64:",
                "Claude — macOS Apple silicon only:",
                "linux-x64-gnu",
            ),
            (
                "Claude — macOS Apple silicon only:",
                "The example model is illustrative",
                "darwin-arm64",
            ),
        ):
            section = text.split(start, 1)[1].split(end, 1)[0]
            self.assertIn(f"PLATFORM={platform}", section)
            self.assertIn(
                f'INSTALL_PREFIX="$HOME/.local/openprose-cli-{version}-npm-$PLATFORM"',
                section,
            )
            self.assertIn('PROSE="$INSTALL_PREFIX/bin/prose"', section)
            self.assertIn(
                'EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
                section,
            )
            self.assertIn('"$PROSE" cli doctor', section)
            self.assertIn('"$PROSE" run "$EXAMPLE"', section)
            self.assertEqual(section.count(PROVIDER_CHARGE_BOUNDARY), 1)
            self.assertLess(
                section.index(PROVIDER_CHARGE_BOUNDARY),
                section.index('"$PROSE" run "$EXAMPLE"'),
            )
        self.assertEqual(text.count(PROVIDER_CHARGE_BOUNDARY), 5)
        promotion_boundary = text.index(
            "Only after this functional alpha has been independently authorized "
            "and publicly promoted"
        )
        moving_registry = text.index('"@openprose/prose-cli@alpha"')
        exact_registry = text.index(f'"@openprose/prose-cli@{version}"')
        self.assertLess(promotion_boundary, moving_registry)
        self.assertLess(moving_registry, exact_registry)

        standalone_install = text.split(
            "For a standalone archive, extract it and bind the exact installed executable path:",
            1,
        )[1].split(
            "Only after this functional alpha has been independently authorized", 1
        )[
            0
        ]
        installed_members = (
            '"$INSTALL_PREFIX/bin/prose"',
            '"$INSTALL_PREFIX/examples/hello.prose.md"',
            '"$INSTALL_PREFIX/README.txt"',
            '"$INSTALL_PREFIX/LICENSE"',
        )
        for member in installed_members:
            self.assertIn(member, standalone_install)
        self.assertLess(
            standalone_install.index('install -m 0644 "$SOURCE/README.txt"'),
            standalone_install.index('test -f "$INSTALL_PREFIX/README.txt"'),
        )
        standalone_remove = text.split(
            "For a standalone prefix created by the installation block above", 1
        )[1].split("For the exact-version or offline npm prefix", 1)[0]
        for member in installed_members:
            self.assertIn(member, standalone_remove)
        self.assertIn(
            'rmdir "$INSTALL_PREFIX/bin" "$INSTALL_PREFIX/examples" "$INSTALL_PREFIX"',
            standalone_remove,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "alpha-notes.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--functional-alpha-version",
                    version,
                    "--output",
                    str(output),
                ],
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(0, completed.returncode, completed.stderr.decode())
            self.assertEqual(first, output.read_bytes())

    def test_cli_changelog_is_independent_honest_and_linked(self) -> None:
        changelog = (NOTES.CLI / "CHANGELOG.md").read_text("utf-8")
        normalized_changelog = " ".join(changelog.split())
        for marker in (
            "## [Unreleased]",
            "independent CLI implementation under `cli/`",
            "historical `@openprose/prose-cli` implementation",
            "transport-only",
            "does not execute OpenProse semantics",
            "does not claim that an artifact is published",
            "does not authorize publication",
        ):
            self.assertIn(marker, normalized_changelog)
        cli_readme = (NOTES.CLI / "README.md").read_text("utf-8")
        release_readme = (NOTES.CLI / "release" / "README.md").read_text("utf-8")
        self.assertIn("[CLI changelog](CHANGELOG.md)", cli_readme)
        self.assertIn("[CLI changelog](../CHANGELOG.md)", release_readme)
        normalized_cli_readme = " ".join(cli_readme.split())
        for marker in (
            "Availability is established only by an exact GitHub prerelease in this repository or an exact version in the public npm registry",
            "Do not infer availability from this source tree",
            "Candidate or draft creation does not establish public availability",
        ):
            self.assertIn(marker, normalized_cli_readme)
        for prohibited in (
            "No artifact produced by this new CLI implementation has been published",
            "No artifact produced by this implementation is public yet",
            "a GitHub Release, npm package, or npm distribution tag is available now",
            "It has not published an artifact from this implementation",
        ):
            self.assertNotIn(prohibited, normalized_cli_readme)

    def test_release_notes_refuse_omp_prerequisite_authority_drift(self) -> None:
        manifest = json.loads(
            NOTES.FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY.read_text("utf-8")
        )
        recipe = json.loads(NOTES.OMP_ADAPTER_RECIPE_AUTHORITY.read_text("utf-8"))
        omp = next(
            item for item in manifest["adapters"] if item["adapterId"] == "omp/rpc"
        )
        omp["runtimePrerequisites"][0][
            "repairCommand"
        ] = "npm install --global @oh-my-pi/pi-coding-agent@18.0.9"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "functional-alpha.json"
            recipe_path = root / "omp-recipe.json"
            manifest_path.write_text(json.dumps(manifest), "utf-8")
            recipe_path.write_text(json.dumps(recipe), "utf-8")
            with mock.patch.object(
                NOTES, "FUNCTIONAL_ALPHA_ADAPTER_AUTHORITY", manifest_path
            ), mock.patch.object(
                NOTES, "OMP_ADAPTER_RECIPE_AUTHORITY", recipe_path
            ), self.assertRaisesRegex(
                NOTES.ReleaseNotesError, "runtime prerequisite"
            ):
                NOTES.render_functional_alpha_notes("1.2.3-alpha.4")

    def test_deterministic_notes_cover_every_target_surface_and_blocker(self) -> None:
        manifests = make_manifests()
        first = NOTES.render_release_notes(manifests)
        second = NOTES.render_release_notes(copy.deepcopy(manifests))
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        text = first.decode("utf-8")
        self.assertIn(f"# OpenProse CLI v{VERSION} — draft candidate", text)
        self.assertIn(f"Source revision: `{SOURCE_SHA}`", text)
        self.assertIn(f"Manifest SHA-256: `{'3' * 64}`", text)
        self.assertIn(
            "`SHA256SUMS` authenticates no publisher identity on its own", text
        )
        self.assertIn("missing or duplicate checksum entry", text)
        self.assertIn(
            "awk -v name=\"$ASSET\" '$2 == name' SHA256SUMS | shasum -a 256 -c -",
            text,
        )
        self.assertNotIn("shasum -a 256 -c SHA256SUMS", text)
        self.assertIn("if ($Records.Count -ne 1)", text)
        self.assertIn("`openprose-windows-process-host.exe`", text)
        self.assertIn("Job Object release admission: **false**", text)
        self.assertIn("Prime, OMP, Codex, and Claude", text)
        self.assertIn("Hosted OpenProse identity, billing", text)
        self.assertIn("Semantic equivalence, Prose Complete", text)
        self.assertIn("dependency component inventory is attached", text)
        self.assertIn("glibc >= `2.34`; execution evidence is Ubuntu 22.04 only", text)
        normalized_text = " ".join(text.split())
        for marker in (
            "macOS Apple silicon | Prime, OMP, Codex, Claude",
            "macOS Intel | Codex",
            "Linux x64 | Codex, OMP",
            "Linux ARM64 | Codex",
            "Windows | Omitted from the functional alpha",
            "Prime exact admitted versions: `0.7.0`, `0.8.1`",
            "OMP exact admitted version: `18.0.9`",
            "requires `bun >=1.3.14`",
            "Codex exact admitted version: `0.149.0-alpha.4.1`",
            "Claude exact admitted version: `2.1.243`",
            "exact functional-alpha allowlist",
            "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1",
            "official versioned prime-agent release tarball",
            "@oh-my-pi/pi-coding-agent@18.0.9",
            "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
            "@openai/codex@0.149.0-alpha.4.1",
            "@anthropic-ai/claude-code@2.1.243",
            "cli harness use prime --model openai-codex/gpt-5.4 --auth-profile prime-harness-login",
            "cli harness use omp --model openai-codex/gpt-5.4 --auth-profile omp-harness-login",
            "replace it with a fully-qualified provider/model exposed by your selected harness login",
            "harness-managed login with unknown billing",
            "not a subscription-billing claim",
            "examples/hello.prose.md",
            "ad-hoc signed and not notarized",
            "xattr -d com.apple.quarantine",
            "bun-darwin-x64-baseline",
            "bun-linux-x64-baseline",
            "Bun x64 artifacts use baseline CPU runtime variants",
            "Bun runtime variant: `baseline`",
            "Bun runtime variant: `native`",
        ):
            self.assertIn(marker, text)
        for marker in (
            "echo-v0 transports and echoes only the opaque `prose run <path>` task argv",
            "does not open or read the packaged example file",
            "does not evaluate the OpenProse contract",
        ):
            self.assertIn(marker, normalized_text)
        self.assertNotIn("contract instructions", text)
        self.assertNotIn("Developer ID", text)
        self.assertIn(
            f"PROSE='./openprose-prose-cli-rust-{VERSION}-<platform>/prose'", text
        )
        self.assertIn(
            f"PROSE='./openprose-prose-cli-bun-{VERSION}-<platform>/prose'", text
        )
        self.assertIn(
            f"EXAMPLE='./openprose-prose-cli-rust-{VERSION}-<platform>/examples/hello.prose.md'",
            text,
        )
        self.assertIn(
            f"EXAMPLE='./openprose-prose-cli-bun-{VERSION}-<platform>/examples/hello.prose.md'",
            text,
        )
        for command, count in (
            ('"$PROSE" cli harness list', 3),
            ('"$PROSE" cli harness use codex', 3),
            ('"$PROSE" cli doctor', 6),
            ('"$PROSE" run "$EXAMPLE"', 6),
        ):
            self.assertEqual(text.count(command), count, command)
        self.assertIn(
            '"$PROSE" cli harness use prime --model openai-codex/gpt-5.4 '
            "--auth-profile prime-harness-login",
            text,
        )
        self.assertIn(
            '"$PROSE" cli harness use omp --model openai-codex/gpt-5.4 '
            "--auth-profile omp-harness-login",
            text,
        )
        self.assertIn('"$PROSE" cli harness use claude', text)
        for start, end in (
            (
                "Prime — macOS Apple silicon only:",
                "OMP — macOS Apple silicon or Linux x64 only:",
            ),
            (
                "OMP — macOS Apple silicon or Linux x64 only:",
                "Claude — macOS Apple silicon only:",
            ),
            ("Claude — macOS Apple silicon only:", "The example model is illustrative"),
        ):
            section = text.split(start, 1)[1].split(end, 1)[0]
            self.assertIn('NPM_PREFIX="$(npm prefix --global)"', section)
            self.assertIn('PROSE="$NPM_PREFIX/bin/prose"', section)
            self.assertIn(
                'EXAMPLE="$NPM_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"',
                section,
            )
            self.assertIn('test -x "$PROSE" && test -f "$EXAMPLE"', section)
            self.assertIn('"$PROSE" cli doctor', section)
            self.assertIn('"$PROSE" run "$EXAMPLE"', section)
        self.assertIn('xattr -d com.apple.quarantine "$PROSE"', text)
        self.assertNotIn("'<archive-root>/prose'", text)
        for target, platform in NOTES.TARGET_PLATFORMS.items():
            with self.subTest(target=target):
                rust = f"openprose-prose-cli-rust-{VERSION}-{platform}.tar.gz"
                bun = f"openprose-prose-cli-bun-{VERSION}-{platform}.tar.gz"
                npm = f"openprose-prose-cli-{platform}-{VERSION}.tgz"
                self.assertIn(f"### {NOTES.TARGET_LABELS[target]}", text)
                self.assertIn(rust, text)
                self.assertIn(bun, text)
                self.assertIn(npm, text)
                self.assertIn(
                    f"npm install --global --ignore-scripts ./{npm} "
                    f"./openprose-prose-cli-{VERSION}.tgz",
                    text,
                )
        forbidden = (
            "production ready",
            "semantically equivalent",
            "Prose Complete passed",
            "native Windows ready",
            "winner:",
        )
        for claim in forbidden:
            self.assertNotIn(claim, text)

    def test_unknown_missing_malformed_or_divergent_manifest_fails_closed(self) -> None:
        mutations = []
        unknown = make_manifests()
        unknown["linux-x64"]["invented"] = True
        mutations.append((unknown, "unknown or missing fields"))
        missing = make_manifests()
        del missing["darwin-arm"]["image"]
        mutations.append((missing, "unknown or missing fields"))
        version = make_manifests()
        version["darwin-x64"]["version"] = "1.2.4"
        mutations.append((version, "version divergence"))
        image = make_manifests()
        image["linux-arm64"]["image"] = {**IMAGE, "sha256": "9" * 64}
        mutations.append((image, "image divergence"))
        source = make_manifests()
        source["win-x64"]["source"] = {
            **source["win-x64"]["source"],
            "revision": "a" * 40,
        }
        mutations.append((source, "source divergence"))
        host = make_manifests()
        host["win-x64"]["windowsProcessHost"] = {**HOST, "admission": True}
        mutations.append((host, "Windows process host"))
        runtime = make_manifests()
        runtime["darwin-x64"]["bunRuntime"]["compileTarget"] = "bun-darwin-x64"
        mutations.append((runtime, "Bun runtime"))
        for manifests, message in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(
                NOTES.ReleaseNotesError, message
            ):
                NOTES.render_release_notes(manifests)

    def test_artifact_inventory_and_canonical_names_are_closed(self) -> None:
        missing = make_manifests()
        missing["linux-x64"]["artifacts"].pop()
        duplicate = make_manifests()
        duplicate["linux-x64"]["artifacts"][3] = copy.deepcopy(
            duplicate["linux-x64"]["artifacts"][2]
        )
        renamed = make_manifests()
        renamed["linux-x64"]["artifacts"][0]["path"] = "friendly-name.tar.gz"
        malformed = make_manifests()
        malformed["linux-x64"]["artifacts"][0]["implementation"] = []
        for manifests in (missing, duplicate, renamed, malformed):
            with self.subTest(), self.assertRaises(NOTES.ReleaseNotesError):
                NOTES.render_release_notes(manifests)

    def test_untrusted_markdown_or_non_release_claims_are_rejected(self) -> None:
        values = make_manifests()
        values["linux-x64"]["image"]["version"] = "good\n# injected"
        with self.assertRaisesRegex(NOTES.ReleaseNotesError, "image identity"):
            NOTES.render_release_notes(values)
        values = make_manifests()
        values["win-x64"]["publicationAuthorized"] = True
        with self.assertRaisesRegex(NOTES.ReleaseNotesError, "draft-safe"):
            NOTES.render_release_notes(values)

    def test_loader_binds_manifest_bytes_to_checksum_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifests = make_manifests()
            write_assembly(root, manifests)
            loaded = NOTES.load_assembly_manifests(root)
            self.assertEqual(
                NOTES.render_release_notes(loaded),
                NOTES.render_release_notes(manifests),
            )
            path = root / "linux-x64-release-manifest.json"
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(NOTES.ReleaseNotesError, "digest mismatch"):
                NOTES.load_assembly_manifests(root)

    def test_loader_rejects_duplicate_json_keys_symlinks_and_missing_checksums(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_assembly(root, make_manifests())
            manifest = root / "linux-x64-release-manifest.json"
            encoded = manifest.read_text("utf-8").replace(
                '"schema": "openprose.local-release-manifest/1",',
                '"schema": "openprose.local-release-manifest/1", "schema": "duplicate",',
                1,
            )
            manifest.write_text(encoded, encoding="utf-8")
            checksums = (root / "SHA256SUMS").read_text("ascii")
            old = next(
                line for line in checksums.splitlines() if line.endswith(manifest.name)
            )
            new = (
                f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  {manifest.name}"
            )
            (root / "SHA256SUMS").write_text(
                checksums.replace(old, new), encoding="ascii"
            )
            with self.assertRaisesRegex(NOTES.ReleaseNotesError, "duplicate JSON key"):
                NOTES.load_assembly_manifests(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_assembly(root, make_manifests())
            manifest = root / "linux-x64-release-manifest.json"
            real = root / "real-manifest.json"
            manifest.rename(real)
            try:
                manifest.symlink_to(real)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(
                    NOTES.ReleaseNotesError, "non-symlink regular file"
                ):
                    NOTES.load_assembly_manifests(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_assembly(root, make_manifests())
            (root / "SHA256SUMS").write_text(
                f"{'0' * 64}  unrelated.json\n", encoding="ascii"
            )
            with self.assertRaisesRegex(NOTES.ReleaseNotesError, "missing checksum"):
                NOTES.load_assembly_manifests(root)

    def test_cli_writes_exact_bytes_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assembly = root / "assembly"
            assembly.mkdir()
            manifests = make_manifests()
            write_assembly(assembly, manifests)
            output = root / "RELEASE_NOTES.md"
            first = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--assembly",
                    str(assembly),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(first.returncode, 0, first.stderr.decode())
            self.assertEqual(first.stdout, b"")
            self.assertEqual(first.stderr, b"")
            self.assertEqual(output.read_bytes(), NOTES.render_release_notes(manifests))
            second = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--assembly",
                    str(assembly),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(second.returncode, 2)
            self.assertEqual(second.stdout, b"")
            self.assertIn(b"output already exists", second.stderr)


if __name__ == "__main__":
    unittest.main()
