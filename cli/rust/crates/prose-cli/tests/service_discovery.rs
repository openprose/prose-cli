//! Service discovery: process-level checks that complement the shared corpus
//! (`cli/conformance/cases/service/discovery/`). They need the test-seam
//! fixture transport and are skipped in ordinary builds, which would reach
//! the network (the real transport ignores proxy variables).
use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

const KEY: &str = "rr_test_0123456789abcdef0123456789abcdef";

fn prose(root: &Path, args: &[&str], exchanges: &Value) -> Output {
    let fixture = root.join("fixture.json");
    let document = serde_json::json!({
        "environment": "production",
        "credentials": {"production": KEY},
        "storeAvailable": true,
        "exchanges": exchanges,
    });
    std::fs::write(&fixture, document.to_string()).unwrap();
    Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_STATE_HOME", root.join("state"))
        .env("PROSE_TEST_SERVICE_FIXTURE", fixture)
        .output()
        .unwrap()
}

#[test]
fn example_show_writes_no_file_when_the_list_request_fails() {
    if !cfg!(feature = "test-seams") {
        return;
    }
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--output",
            "json",
            "cli",
            "example",
            "show",
            "private-example",
            "--output-file",
            "example.prose.md",
        ],
        &serde_json::json!([{"method": "GET", "path": "/examples/private", "status": 503, "bodyText": ""}]),
    );
    assert_eq!(output.status.code(), Some(10));
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["operation"], "example.show");
    assert_eq!(report["problem"]["code"], "SERVICE_UNAVAILABLE");
    assert!(!temp.path().join("example.prose.md").exists());
    assert!(!String::from_utf8_lossy(&output.stdout).contains("rr_test_"));
}

#[test]
fn service_status_stops_after_a_malformed_health_body() {
    if !cfg!(feature = "test-seams") {
        return;
    }
    // Only /health is scripted: any other request would be a fixture
    // mismatch instead of the field-named protocol error.
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["--output", "json", "cli", "service", "status"],
        &serde_json::json!([{"method": "GET", "path": "/health", "status": 200, "body": {"status": "ok"}}]),
    );
    assert_eq!(output.status.code(), Some(10));
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["problem"]["code"], "SERVICE_PROTOCOL_INVALID");
    assert_eq!(
        report["problem"]["details"]["reason"],
        "unexpected service status response: models"
    );
}
