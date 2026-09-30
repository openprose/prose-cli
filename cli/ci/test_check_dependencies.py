from __future__ import annotations

import unittest

from check_dependencies import (
    BUN_DEVELOPMENT_DEPENDENCIES,
    NODE_MINIMUM,
    BUN_PRODUCTION_DEPENDENCIES,
    assess,
    bun_manifest_contract_failures,
    build_mode_contract_failures,
    expected_versions,
)
from package_local import NODE_MINIMUM as PACKAGE_NODE_MINIMUM


class DependencyContractTest(unittest.TestCase):
    def test_bun_runtime_and_development_dependency_contract_is_exact(self) -> None:
        self.assertEqual(
            {"ajv": "8.20.0", "undici": "7.30.0"},
            BUN_PRODUCTION_DEPENDENCIES,
        )
        self.assertEqual(
            {
                "@types/bun": "1.3.5",
                "ajv-formats": "3.0.1",
                "typescript": "5.9.3",
            },
            BUN_DEVELOPMENT_DEPENDENCIES,
        )
        self.assertEqual([], bun_manifest_contract_failures())

    def test_bun_dependency_scope_or_version_regression_fails(self) -> None:
        valid = {
            "dependencies": dict(BUN_PRODUCTION_DEPENDENCIES),
            "devDependencies": dict(BUN_DEVELOPMENT_DEPENDENCIES),
        }
        mutations = {
            "ajv-demoted": {
                "dependencies": {},
                "devDependencies": {
                    **BUN_DEVELOPMENT_DEPENDENCIES,
                    "ajv": "8.20.0",
                },
            },
            "format-promoted": {
                "dependencies": {
                    **BUN_PRODUCTION_DEPENDENCIES,
                    "ajv-formats": "3.0.1",
                },
                "devDependencies": {"@types/bun": "1.3.5", "typescript": "5.9.3"},
            },
            "typescript-promoted": {
                "dependencies": {
                    **BUN_PRODUCTION_DEPENDENCIES,
                    "typescript": "5.9.3",
                },
                "devDependencies": {"@types/bun": "1.3.5"},
            },
            "version-range": {
                **valid,
                "dependencies": {"ajv": "^8.20.0"},
            },
        }
        for label, manifest in mutations.items():
            with self.subTest(label=label):
                self.assertTrue(bun_manifest_contract_failures(manifest))

    def test_default_build_modes_match_the_rust_image_and_seam_contract(self) -> None:
        self.assertEqual([], build_mode_contract_failures())

    def test_build_mode_contract_rejects_sentinel_or_test_seams_in_ordinary_builds(
        self,
    ) -> None:
        ordinary = (
            "bun --no-env-file --config=./config/empty-bunfig.toml run "
            "./scripts/image-bundle.ts build"
        )
        test_only = (
            "bun --no-env-file --config=./config/empty-bunfig.toml run "
            "./scripts/image-bundle.ts build-test"
        )
        release = f"{ordinary} --require-release-eligible"
        rust = (
            'let source = image_dir.join("echo-v0");\n'
            'let bundle = image_dir.join("embedded/current.bundle.bin");\n'
            'let checksum = image_dir.join("embedded/current.bundle.sha256");\n'
            'const TEST_SEAMS_FEATURE_ENV: &str = "CARGO_FEATURE_TEST_SEAMS";\n'
            'let source = image_dir.join("sentinel-v1");\n'
            '"test-seams cannot be enabled for a release build";\n'
        )
        rust_cli_manifest = (
            '[features]\ndefault = []\n'
            'test-seams = ["prose-runner-core/test-seams"]\n'
        )
        rust_core_manifest = "[features]\ndefault = []\ntest-seams = []\n"
        rust_runner = (
            '#[cfg(all(feature = "test-seams", not(debug_assertions)))]\n'
            'compile_error!("test-seams cannot be enabled for a release build");\n'
            'const ENABLED: bool = cfg!(feature = "test-seams");\n'
        )

        self.assertEqual(
            [],
            build_mode_contract_failures(
                scripts={
                    "build": ordinary,
                    "build:test": test_only,
                    "build:release": release,
                    "check": "bun run typecheck && bun run test && bun run build",
                },
                rust_build=rust,
                rust_cli_manifest=rust_cli_manifest,
                rust_core_manifest=rust_core_manifest,
                rust_runner=rust_runner,
            ),
        )
        for label, scripts in {
            "ordinary-seams": {"build": f"{ordinary} --test-seams"},
            "ordinary-sentinel": {"build": f"{ordinary} --image-dir ../shared/image/sentinel-v1"},
            "release-seams": {"build:release": f"{release} --test-seams"},
            "release-sentinel": {"build:release": f"{release} --image-dir ../shared/image/sentinel-v1"},
            "test-not-explicit": {"build:test": f"{ordinary} --test-seams"},
            "check-test-build": {"check": "bun run typecheck && bun run test && bun run build:test"},
        }.items():
            with self.subTest(label=label):
                candidate = {
                    "build": ordinary,
                    "build:test": test_only,
                    "build:release": release,
                    "check": "bun run typecheck && bun run test && bun run build",
                    **scripts,
                }
                self.assertTrue(
                    build_mode_contract_failures(
                        scripts=candidate,
                        rust_build=rust,
                        rust_cli_manifest=rust_cli_manifest,
                        rust_core_manifest=rust_core_manifest,
                        rust_runner=rust_runner,
                    )
                )

        for label, overrides in {
            "cli-default-feature": {
                "rust_cli_manifest": rust_cli_manifest.replace(
                    "default = []", 'default = ["test-seams"]'
                )
            },
            "cli-no-propagation": {
                "rust_cli_manifest": rust_cli_manifest.replace(
                    '["prose-runner-core/test-seams"]', "[]"
                )
            },
            "core-no-feature": {"rust_core_manifest": "[features]\ndefault = []\n"},
            "debug-assertion-seams": {
                "rust_runner": rust_runner.replace(
                    'cfg!(feature = "test-seams")', "cfg!(debug_assertions)"
                )
            },
            "release-not-closed": {
                "rust_runner": rust_runner.replace(
                    'compile_error!("test-seams cannot be enabled for a release build");',
                    "",
                )
            },
        }.items():
            with self.subTest(label=label):
                self.assertTrue(
                    build_mode_contract_failures(
                        scripts={
                            "build": ordinary,
                            "build:test": test_only,
                            "build:release": release,
                            "check": "bun run typecheck && bun run test && bun run build",
                        },
                        rust_build=rust,
                        rust_cli_manifest=overrides.get(
                            "rust_cli_manifest", rust_cli_manifest
                        ),
                        rust_core_manifest=overrides.get(
                            "rust_core_manifest", rust_core_manifest
                        ),
                        rust_runner=overrides.get("rust_runner", rust_runner),
                    )
                )

    def test_node_floor_matches_the_packaged_launcher_contract(self) -> None:
        self.assertEqual(".".join(map(str, NODE_MINIMUM)), PACKAGE_NODE_MINIMUM)

    def test_exact_pins_and_supported_runtime_floors_pass(self) -> None:
        expected = expected_versions()
        commands = {
            "rustc": expected["rustc"],
            "cargo": expected["cargo"],
            "bun": expected["bun"],
            "node": "22.22.3",
            "npm": "10.0.0",
        }

        def command(argv: tuple[str, ...]) -> str:
            return commands[argv[0]]

        report = assess(
            command=command,
            distribution=lambda name: expected[name],
            python_version=(3, 10, 0),
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["failures"], [])

    def test_node_consumer_floor_is_enforced_as_a_complete_version(self) -> None:
        expected = expected_versions()

        def report_for(node: str) -> dict[str, object]:
            commands = {
                "rustc": expected["rustc"],
                "cargo": expected["cargo"],
                "bun": expected["bun"],
                "node": node,
                "npm": "10.0.0",
            }
            return assess(
                command=lambda argv: commands[argv[0]],
                distribution=lambda name: expected[name],
                python_version=(3, 10, 0),
            )

        below = report_for("22.22.2")
        self.assertEqual(below["status"], "fail")
        self.assertIn(
            "node: requires version >= 22.22.3, observed 22.22.2",
            below["failures"],
        )
        self.assertEqual(report_for("22.22.3")["status"], "pass")
        self.assertEqual(report_for("24.20.0")["status"], "pass")

    def test_drift_and_missing_dependencies_fail_with_each_observation(self) -> None:
        def command(argv: tuple[str, ...]) -> str | None:
            return {"node": "18.20.0", "npm": None}.get(argv[0], "0.0.0")

        report = assess(
            command=command,
            distribution=lambda _name: "0.0.0",
            python_version=(3, 9, 19),
        )
        self.assertEqual(report["status"], "fail")
        failures = "\n".join(report["failures"])
        for name in ("rustc", "cargo", "bun", "jsonschema", "referencing", "node", "npm", "python"):
            self.assertIn(name, failures)


if __name__ == "__main__":
    unittest.main(verbosity=2)
