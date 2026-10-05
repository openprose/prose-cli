from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from check_architecture import (
    ALLOWED_BUN_DEPENDENCIES,
    ALLOWED_BUN_SCRIPTS,
    check_repository,
)


class ArchitectureBoundaryTest(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory(prefix="openprose-architecture-")
        root = Path(temporary.name)
        (root / "cli/rust/crates/example/src").mkdir(parents=True)
        (root / "cli/bun/src").mkdir(parents=True)
        (root / "cli/bun/package.json").write_text(
            json.dumps({"name": "fixture", "dependencies": {}}), "utf-8"
        )
        (root / "cli/rust/Cargo.toml").write_text("[workspace]\nmembers=[]\n", "utf-8")
        return temporary, root

    def test_minimal_outer_runners_are_admitted(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/crates/example/src/lib.rs").write_text(
            'pub const TASK_SCHEMA: &str = "openprose.task-envelope/1";\n', "utf-8"
        )
        (root / "cli/bun/src/main.ts").write_text(
            'import fixture from "../../shared/errors/taxonomy.v1.json" with { type: "json" };\nvoid fixture;\n',
            "utf-8",
        )
        self.assertEqual([], check_repository(root))

    def test_only_the_exact_human_safety_fixture_is_admitted(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        fixtures = root / "cli/shared/fixtures/human"
        fixtures.mkdir(parents=True)
        (fixtures / "human-safe-scalars.json").write_text("{}\n", "utf-8")
        (fixtures / "adjacent.json").write_text("{}\n", "utf-8")
        source = root / "cli/rust/crates/example/src/lib.rs"

        source.write_text(
            'const SAFE: &str = include_str!("../../../../shared/fixtures/human/human-safe-scalars.json");\n',
            "utf-8",
        )
        self.assertEqual([], check_repository(root))

        source.write_text(
            'const ADJACENT: &str = include_str!("../../../../shared/fixtures/human/adjacent.json");\n'
            'const ESCAPED: &str = include_str!("../../../../../outside.json");\n',
            "utf-8",
        )
        violations = check_repository(root)
        self.assertEqual(
            ["source-import-escape", "source-import-escape"],
            [violation.rule for violation in violations],
        )

    def test_only_the_exact_portable_config_fixture_is_admitted(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        fixtures = root / "cli/shared/fixtures/config"
        fixtures.mkdir(parents=True)
        (fixtures / "flat-toml-v1.json").write_text("{}\n", "utf-8")
        (fixtures / "adjacent.json").write_text("{}\n", "utf-8")
        source = root / "cli/rust/crates/example/src/lib.rs"

        source.write_text(
            'const CONFIG: &str = include_str!("../../../../shared/fixtures/config/flat-toml-v1.json");\n',
            "utf-8",
        )
        self.assertEqual([], check_repository(root))

        source.write_text(
            'const ADJACENT: &str = include_str!("../../../../shared/fixtures/config/adjacent.json");\n',
            "utf-8",
        )
        violations = check_repository(root)
        self.assertEqual(
            ["source-import-escape"],
            [violation.rule for violation in violations],
        )

    def test_reviewed_shared_json_admissions_are_exact(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/bun/src/main.ts"
        paths = (
            'cli/shared/capabilities/adapters/codex-env-route.v1.json',
            'cli/shared/fixtures/adapters/claude-background-tasks.json',
            'cli/shared/fixtures/adapters/claude-native-turns.json',
            'cli/shared/fixtures/adapters/claude-shutdown.json',
            'cli/shared/fixtures/adapters/claude-task-lifecycle.json',
            'cli/shared/fixtures/adapters/claude-thinking-tokens.json',
            'cli/shared/fixtures/adapters/native-output.v1.json',
            'cli/shared/fixtures/adapters/native-profile.json',
            'cli/shared/fixtures/adapters/sdk-native-limits.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/omp-custom.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/omp-late-progress.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/omp-task-defaults.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/omp.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime-child-telemetry.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime-drain.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime-omitted-result.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime-implicit-turn.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime-queue-telemetry.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime-queued-continuation.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime-turn-transition.json',
            'cli/shared/fixtures/adapters/tool-lifecycle/prime.json',
            'cli/shared/fixtures/config/optional-reporting.json',
            'cli/shared/fixtures/kernel-startup/release.json',
            'cli/shared/fixtures/native-output-budget.json',
            'cli/shared/fixtures/transport-diagnostics.json',
        )
        for path in paths:
            relative = "../../" + path.removeprefix("cli/")
            with self.subTest(path=path):
                source.write_text(f'import data from "{relative}";\n', "utf-8")
                self.assertEqual([], check_repository(root))
                adjacent = relative.rsplit("/", 1)[0] + "/unreviewed.json"
                source.write_text(f'import data from "{adjacent}";\n', "utf-8")
                self.assertIn("source-import-escape", {v.rule for v in check_repository(root)})
        source.write_text('import data from "../../shared/fixtures/../../../../outside.json";\n', "utf-8")
        self.assertIn("source-import-escape", {v.rule for v in check_repository(root)})

    def test_util_admission_is_limited_to_reviewed_adapter_sources(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        for name in ["native-tool-lifecycle", "prime-drain", "protocols", "unreviewed"]:
            source = root / f"cli/bun/src/adapters/{name}.ts"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('import { isDeepStrictEqual } from "node:util";\n', "utf-8")
            rules = {v.rule for v in check_repository(root)}
            with self.subTest(name=name):
                self.assertEqual({"dependency-not-allowed"} if name == "unreviewed" else set(), rules)
            source.unlink()

    def test_kernel_http_dependency_is_exact_and_manifest_scoped(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/rust/crates/prose-runner-core/Cargo.toml"
        source.parent.mkdir(parents=True)
        admitted = 'ureq = { version = "=2.12.1", default-features = false, features = ["tls"] }'
        source.write_text("[dependencies]\n" + admitted, "utf-8")
        self.assertEqual([], check_repository(root))
        for declaration in [
            admitted.replace("=2.12.1", "2.12.1"),
            admitted.replace("2.12.1", "2.12.2"),
            admitted.replace("false", "true"),
            admitted.replace('["tls"]', '["tls", "json"]'),
            admitted.replace(' }', ', git = "https://example.invalid/ureq" }'),
            admitted.replace(' }', ', registry = "other" }'),
            admitted.replace('ureq = {', 'client = { package = "ureq",'),
            'ureq.workspace = true',
        ]:
            with self.subTest(declaration=declaration):
                source.write_text("[dependencies]\n" + declaration, "utf-8")
                self.assertIn("dependency-not-allowed", {v.rule for v in check_repository(root)})
        source.write_text("[dev-dependencies]\n" + admitted, "utf-8")
        self.assertIn("dependency-not-allowed", {v.rule for v in check_repository(root)})
        source.unlink()
        other = root / "cli/rust/crates/prose-cli/Cargo.toml"
        other.parent.mkdir(parents=True)
        other.write_text("[dependencies]\n" + admitted, "utf-8")
        self.assertIn("dependency-not-allowed", {v.rule for v in check_repository(root)})

    def test_read_paths_exclude_declarations_and_unrelated_redaction_values(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/rust/crates/example/src/lib.rs"
        source.write_text('fn capture(secrets: Vec<String>) {\n    let path = config.native_log();\n    let capture = NativeCapture::open(path, secrets.clone(), limit()).unwrap();\n    let record = (std::fs::read("fixture"), secrets);\n}\nimpl NativeCapture {\n    fn open(path: &str, secrets: Vec<String>, limit: usize) -> Self {\n        let mut options = std::fs::OpenOptions::new();\n        options.write(true).create_new(true);\n        Self { file: options.open(path).unwrap(), secrets, limit }\n    }\n}\n', "utf-8")
        self.assertEqual([], check_repository(root))
        source.write_text("fn open(argv: Vec<String>) { forward(argv); }\n", "utf-8")
        self.assertEqual([], check_repository(root))

    def test_nested_argv_paths_and_read_open_aliases_remain_rejected(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/rust/crates/example/src/lib.rs"
        for read in [
            'std::fs::read(&forwarded[0])',
            'std::fs::read(identity::<u8,u16>(&forwarded[0]))',
            'std::fs::read(identity::<Vec<(u8,u16)>,u32>(&forwarded[0]))',
            'std::fs::read(<Pair<u8,u16> as Resolve>::path(&forwarded[0]))',
            'std::fs::read_to_string(resolve(&forwarded[0], "a,b)"))',
            'std::fs::File::open(Path::new(&forwarded[0]))',
            'Input::open(Path::new(&forwarded[0]))',
            'load(resolve(&forwarded[0], r#"a,)"#))',
            'options.open(&forwarded[0])',
            'NativeCapture::open(&forwarded[0], vec![], 1024)',
            'std::fs::OpenOptions::new().read(true).open(&forwarded[0])',
        ]:
            with self.subTest(read=read):
                source.write_text(
                    'use std::fs::read as load;\nuse std::fs::File as Input;\n'
                    'fn boundary(argv: Vec<String>) {\n'
                    'let forwarded = argv;\n'
                    'let mut options = std::fs::OpenOptions::new();\noptions.read(true);\n'
                    f'let _ = {read};\n}}\n', "utf-8")
                self.assertIn("opaque-program-boundary", {v.rule for v in check_repository(root)})

    def test_bun_nested_read_paths_remain_rejected_without_later_argument_taint(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/bun/src/main.ts"
        source.write_text(
            'import { readFile as load } from "node:fs/promises";\n'
            'const argv = process.argv;\n'
            'await load("fixture", argv);\n', "utf-8")
        self.assertEqual([], check_repository(root))
        for expression in [
            'load(resolve(argv[0], "a,b)"))',
            'Bun.file(resolve(argv[0]))',
            'load(/x,y/.test("z") ? "fixture" : argv[2])',
            'load(`${`prefix,`}${argv[2]}`)',
            'load(/[),]/.test("z") ? "fixture" : argv[2])',
            'load(identity<string, string>(argv[2]))',
            'load(/x,y/.test("z") ? "fixture" :\n argv[2])',
        ]:
            source.write_text(
                'import { readFile as load } from "node:fs/promises";\n'
                'const argv = process.argv;\n'
                f'await {expression};\n', "utf-8")
            with self.subTest(expression=expression):
                self.assertIn("opaque-program-boundary", {v.rule for v in check_repository(root)})

    def test_language_token_substrings_do_not_create_false_violations(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/src/main.ts").write_text(
            "const isWellFormedUtf16 = (value: string) => value.length > 0;\n",
            "utf-8",
        )
        self.assertEqual([], check_repository(root))

    def test_rust_lifetimes_and_escaped_strings_are_checked_in_linear_time(
        self,
    ) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/rust/crates/example/src/lib.rs"
        source.write_text(
            "fn checked<'a>(root: &'a str) -> &'a str {\n"
            # Escaped quotes made the former backreference regex branch
            # exponentially while trying to close the final Rust lifetime.
            + '    let _ = "field = \\"value\\"\\n";\n' * 64
            + '    let _ = "Contract Markdown";\n'
            + "    root\n}\n",
            "utf-8",
        )

        started = time.monotonic()
        violations = check_repository(root)
        elapsed = time.monotonic() - started

        self.assertEqual(["language-boundary"], [item.rule for item in violations])
        self.assertLess(
            elapsed,
            1.0,
            "a tiny valid Rust source must not trigger pathological backtracking",
        )

    def test_rust_character_and_raw_literals_preserve_language_checks(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/crates/example/src/lib.rs").write_text(
            "const QUOTE: char = '\"';\n"
            'const KNOWLEDGE: &str = r###"SKILL.md"###;\n',
            "utf-8",
        )

        violations = check_repository(root)

        self.assertEqual(["language-boundary"], [item.rule for item in violations])

    def test_rust_raw_literal_comment_markers_cannot_hide_language_terms(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/crates/example/src/lib.rs").write_text(
            'const TEXT: &str = r#"prefix " // Contract Markdown"#;\n'
            'const BYTES: &[u8] = br#"prefix " // SKILL.md"#;\n'
            'const MANY: &str = r#########"prefix " // ProseScript"#########;\n',
            "utf-8",
        )

        rules = {item.rule for item in check_repository(root)}

        self.assertEqual({"language-boundary"}, rules)

    def test_language_and_ambient_skill_knowledge_are_rejected(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/bun/src/main.ts"
        source.write_text(
            'const format = "Contract Markdown"; // .claude/skills\n', "utf-8"
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual({"language-boundary"}, rules)

    def test_shell_pty_and_program_content_reads_are_rejected(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        source = root / "cli/rust/crates/example/src/lib.rs"
        source.write_text(
            'fn bad(argv: Vec<String>) { Command::new("bash"); '
            "let _ = std::fs::read_to_string(&argv[1]); let _ = openpty(); }\n",
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual({"opaque-program-boundary", "process-boundary"}, rules)

    def test_manifest_and_source_reference_escapes_are_rejected(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/Cargo.toml").write_text(
            '[dependencies]\nlanguage = { path = "../../packages/language" }\n', "utf-8"
        )
        (root / "cli/bun/src/main.ts").write_text(
            'import language from "../../../packages/language";\nvoid language;\n',
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual({"dependency-escape", "source-import-escape"}, rules)

    def test_bun_alias_dynamic_import_and_forwarded_argv_read_bypasses_fail(
        self,
    ) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/src/main.ts").write_text(
            'import { readFile as load } from "node:fs/promises";\n'
            'import * as disk from "node:fs/promises";\n'
            "const forwarded = process.argv.slice(2);\n"
            "const program = forwarded;\n"
            "await load(program[0]);\n"
            "await disk.readFile(program[0]);\n"
            'const moduleName = "../../../packages/language";\n'
            "await import(moduleName);\n",
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual({"dynamic-source-reference", "opaque-program-boundary"}, rules)

    def test_static_import_require_and_url_paths_are_normalized_before_admission(
        self,
    ) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/src/main.ts").write_text(
            'import language from "./safe/../../../../packages/language";\n'
            'const second = require("../../../packages/language/runtime.js");\n'
            'const third = new URL("../../../packages/language/prompt.md", import.meta.url);\n'
            "void language; void second; void third;\n",
            "utf-8",
        )
        violations = check_repository(root)
        self.assertEqual(
            ["source-import-escape", "source-import-escape", "source-import-escape"],
            [violation.rule for violation in violations],
        )

    def test_bun_manifest_dependencies_aliases_and_build_entries_are_closed(
        self,
    ) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/package.json").write_text(
            """{
              "name": "fixture",
              "scripts": {"build": "bun ../../packages/language/build.ts"},
              "imports": {"#language": "../../packages/language/index.ts"},
              "dependencies": {"language-vm": "1.0.0"},
              "optionalDependencies": {"language-local": "file:../../packages/language"}
            }""",
            "utf-8",
        )
        (root / "cli/bun/tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "baseUrl": ".",
                        "paths": {"language/*": ["../../packages/language/*"]},
                    },
                    "include": ["src/**/*.ts", "../../packages/language/**/*.ts"],
                }
            ),
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual(
            {
                "build-script-declaration",
                "dependency-escape",
                "dependency-not-allowed",
                "source-alias",
                "source-declaration-escape",
            },
            rules,
        )

    def test_bun_build_mode_allowlist_keeps_test_seams_explicit(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        manifest = root / "cli/bun/package.json"
        package = {
            "name": "@openprose/prose-cli-bun",
            "scripts": ALLOWED_BUN_SCRIPTS,
            **{
                group: dict(dependencies)
                for group, dependencies in ALLOWED_BUN_DEPENDENCIES.items()
            },
        }
        manifest.write_text(json.dumps(package), "utf-8")
        self.assertEqual([], check_repository(root))

        for label, replacement in {
            "ordinary-seams": ALLOWED_BUN_SCRIPTS["build"] + " --test-seams",
            "ordinary-sentinel": ALLOWED_BUN_SCRIPTS["build"]
            + " --image-dir ../shared/image/sentinel-v1",
            "test-command-not-explicit": ALLOWED_BUN_SCRIPTS["build"]
            + " --test-seams",
        }.items():
            with self.subTest(label=label):
                changed = dict(ALLOWED_BUN_SCRIPTS)
                changed["build:test" if label == "test-command-not-explicit" else "build"] = replacement
                package["scripts"] = changed
                manifest.write_text(json.dumps(package), "utf-8")
                self.assertIn(
                    "build-script-declaration",
                    {violation.rule for violation in check_repository(root)},
                )

    def test_bun_product_dependency_names_versions_and_scopes_are_closed(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        manifest = root / "cli/bun/package.json"
        valid = {
            "name": "@openprose/prose-cli-bun",
            **{
                group: dict(dependencies)
                for group, dependencies in ALLOWED_BUN_DEPENDENCIES.items()
            },
        }
        manifest.write_text(json.dumps(valid), "utf-8")
        self.assertEqual([], check_repository(root))

        mutations = {
            "runtime-demotion": {
                **valid,
                "dependencies": {},
                "devDependencies": {
                    **valid["devDependencies"],
                    "ajv": "8.20.0",
                },
            },
            "test-dependency-promotion": {
                **valid,
                "dependencies": {
                    **valid["dependencies"],
                    "ajv-formats": "3.0.1",
                },
                "devDependencies": {
                    "@types/bun": "1.3.5",
                    "typescript": "5.9.3",
                },
            },
            "development-promotion": {
                **valid,
                "dependencies": {
                    **valid["dependencies"],
                    "typescript": "5.9.3",
                },
                "devDependencies": {"@types/bun": "1.3.5"},
            },
            "runtime-range": {
                **valid,
                "dependencies": {
                    **valid["dependencies"],
                    "ajv": "^8.20.0",
                },
            },
        }
        for label, package in mutations.items():
            with self.subTest(label=label):
                manifest.write_text(json.dumps(package), "utf-8")
                self.assertIn(
                    "dependency-not-allowed",
                    {violation.rule for violation in check_repository(root)},
                )

    def test_duplicate_package_keys_fail_closed(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/package.json").write_text(
            '{"name":"fixture","dependencies":{},"dependencies":{"language":"1"}}',
            "utf-8",
        )
        violations = check_repository(root)
        self.assertEqual(["manifest-invalid"], [item.rule for item in violations])
        self.assertIn("duplicate", violations[0].detail)

    def test_cargo_workspace_dependency_alias_and_source_declarations_are_closed(
        self,
    ) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/Cargo.toml").write_text(
            """[workspace]
members = ["crates/example", "../../packages/language"]

[workspace.dependencies]
syntax = { package = "language-parser", git = "https://example.invalid/language" }
""",
            "utf-8",
        )
        (root / "cli/rust/crates/example/Cargo.toml").write_text(
            """[package]
name = "example"
build = "../../../../packages/language/build.rs"

[build-dependencies]
language = { path = "../../../../packages/language" }
""",
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual(
            {
                "dependency-escape",
                "dependency-not-allowed",
                "dependency-source",
                "source-declaration-escape",
                "workspace-member-escape",
            },
            rules,
        )

    def test_rust_include_path_attribute_shell_alias_and_argv_alias_fail(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/crates/example/src/lib.rs").write_text(
            """use std::process::Command as Launcher;
#[path = "../../../../../packages/language/mod.rs"]
mod syntax;
const PROMPT: &str = include_str!(concat!("../../../../../packages/", "language/SKILL.md"));
fn bad(argv: Vec<String>) {
    let forwarded = argv;
    let program = &forwarded[0];
    let _ = std::fs::read_to_string(program);
    let shell = "powershell.exe";
    let _ = Launcher::new(shell);
    let _ = forkpty();
}
""",
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual(
            {
                "dynamic-source-reference",
                "language-boundary",
                "opaque-program-boundary",
                "process-boundary",
                "source-import-escape",
            },
            rules,
        )

    def test_rust_opaque_read_taint_does_not_cross_function_boundaries(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/crates/example/src/lib.rs").write_text(
            """fn forward(argv: Vec<String>) -> String {
    let launch = argv;
    launch[0].clone()
}

fn inspect_fixture() {
    let launch = fixture_launch();
    let path = launch.image_path();
    let _ = std::fs::read(path);
}
""",
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertNotIn("opaque-program-boundary", rules)

    def test_comments_are_not_mistaken_for_language_or_shell_dependencies(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/src/main.ts").write_text(
            "// Historical text mentions ProseScript, SKILL.md, and import('../../language').\n"
            "const description = 'well-formed outer runner';\n",
            "utf-8",
        )
        self.assertEqual([], check_repository(root))

    def test_cargo_wrapper_and_source_replacement_configuration_fail(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/.cargo").mkdir(parents=True)
        (root / "cli/rust/.cargo/config.toml").write_text(
            """[build]
rustc-wrapper = "../../packages/language/wrapper"

[source.crates-io]
replace-with = "language-vendor"
""",
            "utf-8",
        )
        rules = {violation.rule for violation in check_repository(root)}
        self.assertEqual({"build-script-declaration", "dependency-source"}, rules)

    def test_cargo_patch_and_replace_tables_cannot_override_admitted_dependencies(
        self,
    ) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/Cargo.toml").write_text(
            """[patch.crates-io]
serde = { path = "../../packages/language/serde" }

[replace]
"sha2:0.10.0" = { git = "https://example.invalid/language" }
""",
            "utf-8",
        )
        violations = check_repository(root)
        self.assertEqual(
            ["dependency-source", "dependency-source"],
            [item.rule for item in violations],
        )

    def test_rust_build_script_literal_file_reads_cannot_escape_source_boundary(
        self,
    ) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/rust/crates/example/build.rs").write_text(
            'fn main() { let _ = std::fs::read("../../../../packages/language/prompt.md"); }\n',
            "utf-8",
        )
        violations = check_repository(root)
        self.assertEqual(["source-import-escape"], [item.rule for item in violations])

    def test_typescript_reference_directives_cannot_bypass_import_checks(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/src/main.ts").write_text(
            '/// <reference path="../../../packages/language/types.d.ts" />\n'
            '/// <reference types="language-vm" />\n'
            "export {};\n",
            "utf-8",
        )
        violations = check_repository(root)
        self.assertEqual(
            ["dependency-not-allowed", "source-import-escape"],
            [item.rule for item in violations],
        )

    def test_exact_bundled_terminal_validator_dependency_is_allowed(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        (root / "cli/bun/src/main.ts").write_text(
            'import Ajv2020 from "ajv/dist/2020";\n'
            "export const validator = new Ajv2020();\n",
            "utf-8",
        )
        self.assertEqual([], check_repository(root))

    def test_node_net_is_allowed_only_for_the_exact_prime_owned_service(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        adapters = root / "cli/bun/src/adapters"
        adapters.mkdir(parents=True)
        (adapters / "prime-owned-service.ts").write_text(
            'import { createConnection } from "node:net";\n' "void createConnection;\n",
            "utf-8",
        )

        self.assertEqual([], check_repository(root))

    def test_node_net_remains_rejected_for_every_other_bun_source_path(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        adapters = root / "cli/bun/src/adapters"
        scripts = root / "cli/bun/scripts"
        adapters.mkdir(parents=True)
        scripts.mkdir(parents=True)
        sources = (
            root / "cli/bun/src/main.ts",
            adapters / "prime-owned-service-copy.ts",
            scripts / "prime-owned-service.ts",
        )
        for source in sources:
            source.write_text(
                'import { createConnection } from "node:net";\n'
                "void createConnection;\n",
                "utf-8",
            )

        violations = check_repository(root)

        self.assertEqual(
            sorted(source.relative_to(root).as_posix() for source in sources),
            [violation.path for violation in violations],
        )
        self.assertEqual(
            ["dependency-not-allowed"] * len(sources),
            [violation.rule for violation in violations],
        )

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "requires symlink support")
    def test_symlinked_stable_source_is_never_followed_as_product_code(self) -> None:
        temporary, root = self.fixture()
        self.addCleanup(temporary.cleanup)
        outside = root / "packages/language.ts"
        outside.parent.mkdir(parents=True)
        outside.write_text("export const language = true;\n", "utf-8")
        link = root / "cli/bun/src/language.ts"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        violations = check_repository(root)
        self.assertEqual(["source-symlink"], [item.rule for item in violations])


if __name__ == "__main__":
    unittest.main(verbosity=2)
