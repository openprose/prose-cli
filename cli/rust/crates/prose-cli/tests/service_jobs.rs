//! Service jobs: process-level checks. Service behavior is pinned by
//! `cli/conformance/cases/service/jobs/` (shared with Bun).
use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

const KEY: &str = "rr_test_0123456789abcdef0123456789abcdef";
const TID: &str = "3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10";
const SECRET: &str = "whsec_unit_test_secret_value";

fn prose(root: &Path, args: &[&str], fixture: Option<&str>) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_prose"));
    command
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_STATE_HOME", root.join("state"))
        .env("OPENPROSE_API_KEY", KEY)
        // Any request that escaped the local checks would fail to connect.
        .env("HTTPS_PROXY", "http://127.0.0.1:9");
    if let Some(fixture) = fixture {
        let path = root.join("fixture.json");
        std::fs::write(&path, fixture).unwrap();
        command.env("PROSE_TEST_SERVICE_FIXTURE", path);
    }
    command.output().unwrap()
}

fn problem(output: &Output) -> Value {
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    report["problem"].clone()
}

#[test]
fn invalid_specs_fail_before_any_request() {
    let temp = TempDir::new().unwrap();
    let cases = [
        (
            format!("{{\"name\":\"{}\"}}", "x".repeat(65_536)),
            "is larger than 65536 bytes",
        ),
        ("[]".to_owned(), "must contain a JSON object"),
        ("\u{feff}{}".to_owned(), "is not valid JSON"),
        (
            "{\"type\":\"schedule\",\"interval_seconds\":59}".to_owned(),
            "interval_seconds must be an integer from 60 to 2678400",
        ),
        (
            "{\"type\":\"schedule\",\"interval_seconds\":2678401}".to_owned(),
            "interval_seconds must be an integer from 60 to 2678400",
        ),
        (
            "{\"type\":\"schedule\",\"interval_seconds\":1e400}".to_owned(),
            "is not valid JSON",
        ),
        (
            "{\"intervalSeconds\":86400}".to_owned(),
            "the request field is interval_seconds",
        ),
    ];
    for (spec, reason) in cases {
        std::fs::write(temp.path().join("spec.json"), &spec).unwrap();
        let output = prose(
            temp.path(),
            &[
                "--output",
                "json",
                "cli",
                "job",
                "create",
                "--spec-file",
                "spec.json",
                "--yes",
            ],
            None,
        );
        assert_eq!(output.status.code(), Some(2), "{reason}");
        let problem = problem(&output);
        assert_eq!(problem["code"], "INVOCATION_INVALID", "{reason}");
        assert!(
            problem["details"]["reason"]
                .as_str()
                .unwrap()
                .contains(reason),
            "{problem}"
        );
    }
}

#[test]
fn detach_without_yes_plans_the_encoded_delete() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--output",
            "json",
            "cli",
            "job",
            "contract",
            "detach",
            TID,
            "exowner1/probe@0123456789abcdef",
        ],
        None,
    );
    assert_eq!(output.status.code(), Some(2));
    let problem = problem(&output);
    assert_eq!(problem["code"], "CONFIRMATION_REQUIRED");
    // The plan says what the request does; the service route stays internal.
    let planned = &problem["details"]["plannedRequest"];
    assert_eq!(planned["method"], "DELETE");
    assert_eq!(planned["description"], "Detach a program from a job.");
    assert!(planned.get("path").is_none());
}

#[test]
fn human_rotate_secret_warns_without_the_secret() {
    if !cfg!(feature = "test-seams") {
        return;
    }
    let temp = TempDir::new().unwrap();
    let fixture = serde_json::json!({
        "environment": "production",
        "credentials": {"production": KEY},
        "storeAvailable": true,
        "exchanges": [{
            "method": "POST",
            "path": format!("/triggers/{TID}/rotate-secret"),
            "status": 200,
            "body": {"status": {}, "endpoint": format!("/webhooks/triggers/{TID}"), "signing_secret": SECRET},
        }],
    });
    let output = prose(
        temp.path(),
        &["cli", "job", "rotate-secret", TID, "--yes"],
        Some(&fixture.to_string()),
    );
    assert_eq!(output.status.code(), Some(0));
    let stdout = String::from_utf8(output.stdout).unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stdout.contains(&format!("signing secret: {SECRET}")));
    assert!(stderr.contains("shown only once"));
    assert!(!stderr.contains(SECRET));
}
