//! Published results: process-level checks. Behavior is pinned by
//! the shared corpus `cli/conformance/cases/service/results/`; these cover what
//! the corpus leaves to a real process in an ordinary build.
use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

fn prose(root: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_STATE_HOME", root.join("state"))
        // Any accidental network request fails fast instead of reaching a service.
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .output()
        .unwrap()
}

#[test]
fn publish_without_yes_plans_the_body_and_sends_nothing() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--output", "json", "cli", "result", "publish", "demo", "--run", "run_abc",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    let document: Value = serde_json::from_slice(&output.stdout).unwrap();
    let planned = &document["problem"]["details"]["plannedRequest"];
    assert_eq!(document["problem"]["code"], "CONFIRMATION_REQUIRED");
    assert!(planned.get("path").is_none());
    assert_eq!(planned["bodyBytes"], r#"{"run_id":"run_abc"}"#.len());
}

#[test]
fn an_existing_output_file_is_refused_before_any_request() {
    let temp = TempDir::new().unwrap();
    std::fs::write(temp.path().join("result.json"), "keep").unwrap();
    let output = prose(
        temp.path(),
        &[
            "--output",
            "json",
            "cli",
            "result",
            "show",
            "Owner/demo",
            "--latest",
            "--raw",
            "--output-file",
            "result.json",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    let document: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(document["problem"]["code"], "INVOCATION_INVALID");
    assert_eq!(
        std::fs::read_to_string(temp.path().join("result.json")).unwrap(),
        "keep"
    );
}
