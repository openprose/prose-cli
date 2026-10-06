//! Production configuration commands through the compiled CLI. No provider,
//! credential store, executable discovery, or source-language execution is needed.
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use tempfile::TempDir;

struct Fixture {
    root: TempDir,
}

impl Fixture {
    fn new() -> Self {
        let root = TempDir::new().unwrap();
        fs::create_dir(root.path().join("home")).unwrap();
        Self { root }
    }

    fn path(&self, relative: &str) -> PathBuf {
        self.root.path().join(relative)
    }

    fn write(&self, relative: &str, bytes: &[u8]) {
        let path = self.path(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }

    fn invoke(&self, args: &[&str], config_root: Option<&Path>) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_prose"));
        command
            .args(args)
            .current_dir(self.root.path())
            .env_clear()
            .env("HOME", self.path("home"))
            .env("USERPROFILE", self.path("home"))
            .env("XDG_CONFIG_HOME", self.path("legacy"))
            .env("PATH", self.path("absent-bin"));
        if let Some(root) = config_root {
            command.env("PROSE_CONFIG_DIR", root);
        }
        command.output().unwrap()
    }
}

fn report(output: &Output, exit: i32) -> Value {
    assert_eq!(
        output.status.code(),
        Some(exit),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        output.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn migration_preserves_exact_bytes_and_never_overwrites() {
    let fixture = Fixture::new();
    let original = b"# retained comment\r\nharness = \"codex\"\r\nmodel = \"explicit-model\"\r\nverbose = true\r\n";
    fixture.write("legacy/openprose/cli.toml", original);
    let active = report(
        &fixture.invoke(&["cli", "config", "explain", "--json"], None),
        0,
    );
    assert_eq!(active["diagnostics"][0]["code"], "LEGACY_CONFIG_ACTIVE");
    assert!(!fixture.path("home/.prose").exists());
    let migrated = report(
        &fixture.invoke(&["cli", "config", "migrate", "--json"], None),
        0,
    );
    assert_eq!(migrated["mutation"]["operation"], "migrate");
    assert_eq!(migrated["mutation"]["changed"], true);
    assert_eq!(
        fs::read(fixture.path("home/.prose/cli.toml")).unwrap(),
        original
    );
    assert_eq!(
        fs::read(fixture.path("legacy/openprose/cli.toml")).unwrap(),
        original
    );
    let rejected = report(
        &fixture.invoke(&["cli", "config", "migrate", "--json"], None),
        2,
    );
    assert_eq!(rejected["code"], "CONFIG_INVALID");
    assert_eq!(
        fs::read(fixture.path("home/.prose/cli.toml")).unwrap(),
        original
    );
    assert_eq!(
        fs::read(fixture.path("legacy/openprose/cli.toml")).unwrap(),
        original
    );
}

#[test]
fn invalid_migration_and_unknown_unset_leave_every_file_unchanged() {
    for original in [
        "harness = \"agents-sdk\"\nauth_profile = \"cached-chatgpt-login\"\n",
        "harness = \"codex\"\nunknown_preference = \"reject-me\"\n",
    ] {
        let fixture = Fixture::new();
        fixture.write("legacy/openprose/cli.toml", original.as_bytes());
        let failed = report(
            &fixture.invoke(&["cli", "config", "migrate", "--json"], None),
            2,
        );
        assert_eq!(failed["code"], "CONFIG_INVALID");
        assert!(!fixture.path("home/.prose/cli.toml").exists());
        assert_eq!(
            fs::read(fixture.path("legacy/openprose/cli.toml")).unwrap(),
            original.as_bytes()
        );
    }
    let fixture = Fixture::new();
    let original = b"# preserved\ntimeout = \"2m\"\n";
    fixture.write("home/.prose/cli.toml", original);
    let failed = report(
        &fixture.invoke(
            &["cli", "config", "unset", "unknown_preference", "--json"],
            None,
        ),
        2,
    );
    assert_eq!(failed["code"], "INVOCATION_INVALID");
    assert_eq!(
        fs::read(fixture.path("home/.prose/cli.toml")).unwrap(),
        original
    );
}

#[test]
fn unset_preserves_crlf_comments_and_repairs_bundle_without_materializing_defaults() {
    let fixture = Fixture::new();
    let original = b"# chosen harness\r\nharness = \"codex\"\r\n# keep comment\r\nmodel = \"old-model\"\r\nauth_profile = \"openrouter\"\r\nverbose = true\r\n";
    fixture.write("home/.prose/cli.toml", original);
    let invalid = report(
        &fixture.invoke(&["cli", "config", "explain", "--json"], None),
        2,
    );
    assert_eq!(invalid["code"], "CONFIG_INVALID");
    let output = report(
        &fixture.invoke(
            &["cli", "config", "unset", "model", "auth_profile", "--json"],
            None,
        ),
        0,
    );
    assert_eq!(
        output["mutation"]["keys"],
        serde_json::json!(["model", "auth_profile"])
    );
    assert_eq!(
        fs::read(fixture.path("home/.prose/cli.toml")).unwrap(),
        b"# chosen harness\r\nharness = \"codex\"\r\n# keep comment\r\nverbose = true\r\n"
    );
    assert!(output["values"]["model"]["value"].is_null());
    assert_eq!(
        output["values"]["authProfile"]["value"],
        "cached-chatgpt-login"
    );
    assert_eq!(
        output["values"]["authProfile"]["source"]["location"],
        "built-in:codex"
    );
    let repeated = report(
        &fixture.invoke(
            &["cli", "config", "unset", "model", "auth_profile", "--json"],
            None,
        ),
        0,
    );
    assert_eq!(repeated["mutation"]["changed"], false);
}

#[test]
fn absent_unset_creates_neither_default_home_nor_override_directory() {
    for override_root in [false, true] {
        let fixture = Fixture::new();
        let custom = fixture.path("custom root");
        let output = report(
            &fixture.invoke(
                &["cli", "config", "unset", "timeout", "--json"],
                override_root.then_some(custom.as_path()),
            ),
            0,
        );
        assert_eq!(output["mutation"]["changed"], false);
        assert!(!fixture.path("home/.prose").exists());
        assert!(!custom.exists());
    }
}

#[test]
fn legacy_unset_creates_only_filtered_canonical_copy() {
    let fixture = Fixture::new();
    let original = b"# retained\r\ntimeout = \"2m\"\r\nverbose = true\r\n";
    fixture.write("legacy/openprose/cli.toml", original);
    let output = report(
        &fixture.invoke(&["cli", "config", "unset", "timeout", "--json"], None),
        0,
    );
    assert_eq!(
        output["mutation"]["sourcePath"],
        fixture.path("legacy/openprose/cli.toml").to_str().unwrap()
    );
    assert_eq!(
        fs::read(fixture.path("legacy/openprose/cli.toml")).unwrap(),
        original
    );
    assert_eq!(
        fs::read(fixture.path("home/.prose/cli.toml")).unwrap(),
        b"# retained\r\nverbose = true\r\n"
    );
}

#[test]
fn rejected_profile_never_appears_anywhere_in_error_output() {
    let fixture = Fixture::new();
    let sentinel = "private-profile-sentinel-84919";
    let original = format!("harness = \"agents-sdk\"\nauth_profile = \"{sentinel}\"\n");
    fixture.write("home/.prose/cli.toml", original.as_bytes());
    let output = fixture.invoke(&["cli", "config", "explain", "--json"], None);
    let failed = report(&output, 2);
    assert_eq!(failed["code"], "CONFIG_INVALID");
    assert!(failed["details"]["configurationExplanation"]["diagnostics"].is_array());
    assert!(!String::from_utf8_lossy(&output.stdout).contains(sentinel));
    assert!(!String::from_utf8_lossy(&output.stderr).contains(sentinel));
    assert_eq!(
        fs::read(fixture.path("home/.prose/cli.toml")).unwrap(),
        original.as_bytes()
    );
}

#[test]
fn exact_target_uses_its_cwd_and_keeps_opaque_tokens_and_contextual_defaults() {
    let fixture = Fixture::new();
    fixture.write(".prose/cli.toml", b"timeout = \"5m\"\n");
    fixture.write("work space/.prose/cli.toml", b"timeout = \"6m\"\n");
    let output = report(
        &fixture.invoke(
            &[
                "cli",
                "config",
                "explain",
                "--json",
                "--",
                "--cwd",
                "work space",
                "--harness",
                "agents-sdk",
                "--",
                "run",
                "--model",
                "literal-model",
            ],
            None,
        ),
        0,
    );
    assert_eq!(
        output["target"]["argv"],
        serde_json::json!(["prose", "run", "--model", "literal-model"])
    );
    assert_eq!(
        output["cwd"]["value"],
        fs::canonicalize(fixture.path("work space"))
            .unwrap()
            .to_str()
            .unwrap()
    );
    assert_eq!(output["values"]["timeout"]["value"], "6m");
    assert_eq!(output["values"]["model"]["value"], "gpt-6.1-sol");
    assert_eq!(output["values"]["authProfile"]["value"], "openai-api-key");
    assert_eq!(output["runtime"]["transport"], "jsonl");
    assert_eq!(output["runtime"]["billingOwner"], "user-provider");
    assert_eq!(
        output["values"]["model"]["source"]["location"],
        "built-in:agents-sdk"
    );
    assert_eq!(output["values"].as_object().unwrap().len(), 19);
    assert!(!fixture.path("home/.prose").exists());
    assert_eq!(
        fs::read(fixture.path("work space/.prose/cli.toml")).unwrap(),
        b"timeout = \"6m\"\n"
    );
}

#[test]
fn overridden_invalid_profile_is_not_disclosed_as_a_candidate() {
    let fixture = Fixture::new();
    let sentinel = "rejected-profile-sentinel-765";
    fixture.write(
        "home/.prose/cli.toml",
        format!("harness = \"agents-sdk\"\nauth_profile = \"{sentinel}\"\n").as_bytes(),
    );
    let raw = fixture.invoke(
        &[
            "cli",
            "config",
            "explain",
            "--json",
            "--",
            "--auth-profile",
            "openai-api-key",
            "run",
            "subject",
        ],
        None,
    );
    let output = report(&raw, 0);
    assert_eq!(output["values"]["authProfile"]["value"], "openai-api-key");
    assert!(!String::from_utf8_lossy(&raw.stdout).contains(sentinel));
    assert!(!output["diagnostics"].as_array().unwrap().is_empty());
}

#[test]
fn sdk_unsupported_permissions_fail_during_pure_explanation() {
    let fixture = Fixture::new();
    let output = report(
        &fixture.invoke(
            &[
                "cli",
                "config",
                "explain",
                "--json",
                "--",
                "--harness",
                "agents-sdk",
                "--permission-mode",
                "read-only",
                "run",
                "subject",
            ],
            None,
        ),
        2,
    );
    assert_eq!(output["code"], "CONFIG_INVALID");
    assert_eq!(output["boundary"], "configuration");
    assert!(output["details"]["configurationExplanation"]["runtime"]["permissionMode"].is_null());
    assert!(!fixture.path("home/.prose").exists());
}

#[test]
fn ignored_legacy_classification_and_conflicts_are_visible_without_values() {
    let fixture = Fixture::new();
    fixture.write("home/.prose/cli.toml", b"timeout = \"2m\"\n");
    fixture.write("legacy/openprose/cli.toml", b"timeout = \"9m\"\n");
    let output = report(
        &fixture.invoke(&["cli", "config", "explain", "--json"], None),
        0,
    );
    let diagnostics = serde_json::to_string(&output["diagnostics"]).unwrap();
    assert!(diagnostics.contains("LEGACY_CONFIG_IGNORED"));
    assert!(diagnostics.contains("timeout"));
    fixture.write(
        "legacy/openprose/cli.toml",
        b"api_key = \"rejected-legacy-secret\"\n",
    );
    let raw = fixture.invoke(&["cli", "config", "explain", "--json"], None);
    let output = report(&raw, 0);
    let diagnostics = serde_json::to_string(&output["diagnostics"])
        .unwrap()
        .to_lowercase();
    assert!(diagnostics.contains("invalid") || diagnostics.contains("unknown"));
    assert!(!String::from_utf8_lossy(&raw.stdout).contains("rejected-legacy-secret"));
}

#[test]
fn selected_file_candidates_identify_the_assignment_line() {
    let fixture = Fixture::new();
    fixture.write(
        "home/.prose/cli.toml",
        b"# original preference\ntimeout = \"2m\"\n",
    );
    let output = report(
        &fixture.invoke(&["cli", "config", "explain", "--json"], None),
        0,
    );
    let chosen = output["candidates"]["timeout"]
        .as_array()
        .unwrap()
        .iter()
        .find(|candidate| candidate["selected"] == true)
        .unwrap();
    assert!(chosen["source"]["location"]
        .as_str()
        .unwrap()
        .ends_with("cli.toml:2"));
    assert_eq!(chosen["value"], "2m");
}

#[test]
fn project_locations_show_only_visited_candidates_and_stop_at_boundary() {
    let fixture = Fixture::new();
    fs::create_dir(fixture.path(".git")).unwrap();
    fs::create_dir_all(fixture.path("child/deep")).unwrap();
    fixture.write(".prose/cli.toml", b"timeout = \"3m\"\n");
    let args = [
        "cli",
        "config",
        "explain",
        "--json",
        "--",
        "--cwd",
        "child/deep",
        "run",
        "subject",
    ];
    for present in [true, false] {
        if !present {
            fs::remove_file(fixture.path(".prose/cli.toml")).unwrap();
        }
        let output = report(&fixture.invoke(&args, None), 0);
        let project_locations: Vec<&Value> = output["locations"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|location| location["role"] == "project")
            .collect();
        assert_eq!(project_locations.len(), 3);
        let canonical = fs::canonicalize(fixture.root.path()).unwrap();
        for (index, relative) in [
            "child/deep/.prose/cli.toml",
            "child/.prose/cli.toml",
            ".prose/cli.toml",
        ]
        .iter()
        .enumerate()
        {
            assert_eq!(
                project_locations[index]["path"],
                canonical.join(relative).to_str().unwrap()
            );
            assert_eq!(project_locations[index]["present"], present && index == 2);
            assert_eq!(project_locations[index]["selected"], present && index == 2);
        }
        if present {
            assert_eq!(output["values"]["timeout"]["value"], "3m");
        } else {
            assert!(output["projectConfigPath"].is_null());
        }
    }
}

#[test]
fn invalid_ignored_legacy_root_has_safe_diagnostic_without_blocking_canonical() {
    let fixture = Fixture::new();
    fixture.write("home/.prose/cli.toml", b"timeout = \"2m\"\n");
    let raw = Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(["cli", "config", "explain", "--json"])
        .current_dir(fixture.root.path())
        .env_clear()
        .env("HOME", fixture.path("home"))
        .env("XDG_CONFIG_HOME", "relative-legacy-root-sentinel")
        .env("PATH", fixture.path("absent-bin"))
        .output()
        .unwrap();
    let output = report(&raw, 0);
    assert_eq!(output["values"]["timeout"]["value"], "2m");
    assert!(output["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .any(|diagnostic| diagnostic["source"] == "XDG_CONFIG_HOME"));
    assert!(!String::from_utf8_lossy(&raw.stdout).contains("relative-legacy-root-sentinel"));
}

#[cfg(unix)]
#[test]
fn readonly_physical_alias_loads_once_but_mutations_refuse_symlink() {
    let fixture = Fixture::new();
    let original = b"# one physical preference\ntimeout = \"2m\"\n";
    fixture.write("legacy/openprose/cli.toml", original);
    fs::create_dir_all(fixture.path("home/.prose")).unwrap();
    std::os::unix::fs::symlink(
        fixture.path("legacy/openprose/cli.toml"),
        fixture.path("home/.prose/cli.toml"),
    )
    .unwrap();
    let output = report(
        &fixture.invoke(&["cli", "config", "explain", "--json"], None),
        0,
    );
    assert_eq!(output["values"]["timeout"]["value"], "2m");
    assert_eq!(
        output["candidates"]["timeout"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|candidate| candidate["value"] == "2m")
            .count(),
        1
    );
    assert!(!output["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .any(|diagnostic| diagnostic["code"] == "LEGACY_CONFIG_IGNORED"));
    for args in [
        vec!["cli", "config", "migrate", "--json"],
        vec!["cli", "config", "unset", "timeout", "--json"],
    ] {
        let failed = report(&fixture.invoke(&args, None), 2);
        assert_eq!(failed["code"], "CONFIG_INVALID");
        assert!(fs::symlink_metadata(fixture.path("home/.prose/cli.toml"))
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(
            fs::read(fixture.path("legacy/openprose/cli.toml")).unwrap(),
            original
        );
    }
}
