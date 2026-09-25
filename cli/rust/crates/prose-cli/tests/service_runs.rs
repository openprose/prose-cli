//! Service run lifecycle: process-level checks the shared corpus cannot
//! hold. Behavior is pinned by `cli/conformance/cases/service/runs/`; these
//! cover inputs generated at runtime (files over 1 MiB would exceed the
//! repository's new-file limit) and the journal's file modes. The Bun twins are
//! in `cli/bun/test/service-runs.test.ts`.
use serde_json::{Value, json};
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

const KEY: &str = "rr_test_0123456789abcdef0123456789abcdef";
const SESSION: &str = "5f0c8a1e-3b2d-4c6e-9f70-1a2b3c4d5e6f";
const RUN: &str = "run_4f1c2d3e4f5a6b7c4f1c2d3e4f5a6b7c4f1c2d3e4f5a6b7c4f1c2d3e4f5a6b7c";
const MEBIBYTE: usize = 1024 * 1024;

fn prose(root: &Path, args: &[&str], fixture: Option<&Value>) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_prose"));
    command
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_STATE_HOME", root.join("state"))
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .env("OPENPROSE_API_KEY", KEY);
    if let Some(fixture) = fixture {
        let path = root.join("fixture.json");
        std::fs::write(&path, fixture.to_string()).unwrap();
        command.env("PROSE_TEST_SERVICE_FIXTURE", path);
    }
    command.output().unwrap()
}

fn submit_args<'a>(extra: &[&'a str]) -> Vec<&'a str> {
    let mut args = vec!["--output", "json", "cli", "run", "submit"];
    args.extend_from_slice(extra);
    args
}

fn problem(output: &Output) -> Value {
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    report["problem"].clone()
}

#[test]
fn input_file_over_one_mebibyte_is_rejected_before_any_request() {
    let temp = TempDir::new().unwrap();
    std::fs::write(temp.path().join("p.prose.md"), "Reply ok.\n").unwrap();
    std::fs::write(temp.path().join("big.txt"), "x".repeat(MEBIBYTE + 1)).unwrap();
    // An empty fixture proves nothing was sent (test-seam builds fail on any request).
    let fixture = json!({"environment": "production", "exchanges": []});
    let output = prose(
        temp.path(),
        &submit_args(&["p.prose.md", "--input", "notes=@big.txt", "--yes"]),
        Some(&fixture),
    );
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    let problem = problem(&output);
    assert_eq!(problem["code"], "INVOCATION_INVALID");
    assert_eq!(
        problem["details"]["reason"],
        "--input notes file \"big.txt\" is larger than 1048576 bytes"
    );
}

#[test]
fn program_file_over_one_mebibyte_is_rejected_before_any_request() {
    let temp = TempDir::new().unwrap();
    std::fs::write(temp.path().join("big.prose.md"), "x".repeat(MEBIBYTE + 1)).unwrap();
    let fixture = json!({"environment": "production", "exchanges": []});
    let output = prose(
        temp.path(),
        &submit_args(&["big.prose.md", "--yes"]),
        Some(&fixture),
    );
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert_eq!(
        problem(&output)["details"]["reason"],
        "program file \"big.prose.md\" is larger than 1048576 bytes"
    );
}

#[test]
fn an_input_file_of_exactly_one_mebibyte_is_planned() {
    if !cfg!(feature = "test-seams") {
        return;
    }
    let temp = TempDir::new().unwrap();
    std::fs::write(temp.path().join("p.prose.md"), "Reply ok.\n").unwrap();
    std::fs::write(temp.path().join("max.txt"), "y".repeat(MEBIBYTE)).unwrap();
    let fixture = json!({"environment": "production", "exchanges": [
        {"method": "GET", "path": "/run/quote", "status": 200,
         "body": {"hold": {"hold_usd": "1.02", "ttl_seconds": 900}, "pricing_policy_id": "p"}}
    ]});
    let output = prose(
        temp.path(),
        &submit_args(&["p.prose.md", "--input", "notes=@max.txt", "--preview"]),
        Some(&fixture),
    );
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    let planned = &report["result"]["plannedRequest"];
    assert!(planned["bodyBytes"].as_u64().unwrap() > MEBIBYTE as u64);
    assert_eq!(planned["quote"]["hold"]["hold_usd"], "1.02");
}

#[cfg(unix)]
#[test]
fn the_run_journal_is_private_and_holds_no_credential() {
    use std::os::unix::fs::PermissionsExt;
    if !cfg!(feature = "test-seams") {
        return;
    }
    let temp = TempDir::new().unwrap();
    std::fs::write(temp.path().join("p.prose.md"), "Reply ok.\n").unwrap();
    let frame = |sequence: u64, kind: &str, extra: Value| {
        let mut data = json!({"type": kind, "run_id": RUN, "sequence": sequence});
        data.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        json!({"id": sequence.to_string(), "event": kind, "data": data})
    };
    let fixture = json!({"environment": "production", "ids": [SESSION], "exchanges": [
        {"method": "POST", "path": "/run", "query": {"live": "1", "session": SESSION}, "status": 200,
         "sse": {"end": "close", "frames": [
             frame(1, "status", json!({"status": "running"})),
             frame(2, "run_complete", json!({"status": "completed", "files": []})),
         ]}}
    ]});
    let output = prose(
        temp.path(),
        &submit_args(&["p.prose.md", "--yes"]),
        Some(&fixture),
    );
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let runs = temp.path().join("state/openprose/cli/production/runs");
    let entry = runs.join(format!("{SESSION}.json"));
    let mode = |path: &Path| std::fs::metadata(path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&runs), 0o700);
    assert_eq!(mode(&temp.path().join("state/openprose")), 0o700);
    assert_eq!(mode(&entry), 0o600);
    let text = std::fs::read_to_string(&entry).unwrap();
    assert!(!text.contains("rr_test_"));
    let value: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(value["runId"], RUN);
    assert_eq!(value["lastSequence"], 2);
}
