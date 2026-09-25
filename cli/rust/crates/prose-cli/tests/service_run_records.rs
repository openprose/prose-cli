//! Run records: process checks the shared corpus
//! (`cli/conformance/cases/service/run-records/`) cannot express: download
//! caps (lowered by the test-seam `PROSE_TEST_SERVICE_DOWNLOAD_LIMITS`, since
//! 64 MiB fixtures are impossible) and the 1 MiB inline `run show --file` cap.
//! The Bun twin is `cli/bun/test/service-run-records.test.ts`.
#![cfg(feature = "test-seams")]
use serde_json::{Value, json};
use std::path::Path;
use std::process::Command;
use tempfile::TempDir;

const RUN: &str = "run_abcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabca";
const KEY: &str = "rr_test_0123456789abcdef0123456789abcdef";

fn manifest(files: &[&str]) -> Value {
    json!({"method": "GET", "path": format!("/runs/{RUN}"), "status": 200, "body": {
        "run_id": RUN, "created_at": "2026-09-23T20:29:47.788Z", "status": "completed",
        "model": "model-luna", "files": files, "has_patch": false,
        "file_urls": files.iter().map(|file| ((*file).to_owned(), json!(format!("https://x/{file}?tok=SECRET")))).collect::<serde_json::Map<_, _>>()
    }})
}

fn file(path: &str, text: &str) -> Value {
    json!({"method": "GET", "path": format!("/runs/{RUN}/files/{path}"), "status": 200, "bodyText": text})
}

fn prose(root: &Path, args: &[&str], exchanges: &[Value], limits: Option<&str>) -> (i32, Value) {
    let fixture = root.join("fixture.json");
    std::fs::write(
        &fixture,
        json!({"environment": "production", "exchanges": exchanges}).to_string(),
    )
    .unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_prose"));
    command
        .args(["--output", "json", "cli"])
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_STATE_HOME", root.join("state"))
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .env("OPENPROSE_API_KEY", KEY)
        .env("PROSE_TEST_SERVICE_FIXTURE", &fixture);
    if let Some(limits) = limits {
        command.env("PROSE_TEST_SERVICE_DOWNLOAD_LIMITS", limits);
    }
    let output = command.output().unwrap();
    (
        output.status.code().unwrap(),
        serde_json::from_slice(&output.stdout).unwrap(),
    )
}

#[test]
fn a_file_over_the_per_file_cap_is_too_large_and_leaves_no_marker() {
    let temp = TempDir::new().unwrap();
    let (code, report) = prose(
        temp.path(),
        &["run", "download", RUN, "--output-dir", "out"],
        &[manifest(&["a.txt"]), file("a.txt", "123456")],
        Some("5,1000"),
    );
    assert_eq!(code, 10);
    assert_eq!(report["problem"]["code"], "SERVICE_RESPONSE_TOO_LARGE");
    assert_eq!(
        report["problem"]["details"]["reason"],
        "\"a.txt\" is larger than 5 bytes; the output directory is incomplete and has no .prose-run-manifest.json"
    );
    assert!(!temp.path().join("out/.prose-run-manifest.json").exists());
}

#[test]
fn files_over_the_per_run_total_are_too_large() {
    let temp = TempDir::new().unwrap();
    let (code, report) = prose(
        temp.path(),
        &["run", "download", RUN, "--output-dir", "out"],
        &[
            manifest(&["a.txt", "b.txt"]),
            file("a.txt", "123456"),
            file("b.txt", "123456"),
        ],
        Some("100,10"),
    );
    assert_eq!(code, 10);
    assert_eq!(
        report["problem"]["details"]["reason"],
        "the run's files exceed 10 bytes in total; the output directory is incomplete and has no .prose-run-manifest.json"
    );
    assert_eq!(
        std::fs::read_to_string(temp.path().join("out/a.txt")).unwrap(),
        "123456"
    );
}

#[test]
fn files_exactly_at_the_caps_download_and_the_marker_has_no_signed_urls() {
    let temp = TempDir::new().unwrap();
    let (code, report) = prose(
        temp.path(),
        &["run", "download", RUN, "--output-dir", "out"],
        &[
            manifest(&["a.txt", "b/c.txt"]),
            file("a.txt", "12345"),
            file("b/c.txt", "12345"),
        ],
        Some("5,10"),
    );
    assert_eq!(code, 0);
    assert_eq!(report["result"]["fileCount"], 2);
    assert_eq!(report["result"]["totalBytes"], 10);
    let marker = std::fs::read_to_string(temp.path().join("out/.prose-run-manifest.json")).unwrap();
    assert!(!marker.contains("tok="));
    let marker: Value = serde_json::from_str(&marker).unwrap();
    assert_eq!(marker["download"], report["result"]);
    assert!(
        !temp
            .path()
            .join("out/.prose-run-manifest.json.partial")
            .exists()
    );
}

#[test]
fn inline_show_file_is_capped_at_one_mebibyte() {
    // Generated at runtime: a > 1 MiB corpus file would exceed the repository's
    // new-file limit.
    let temp = TempDir::new().unwrap();
    let (code, report) = prose(
        temp.path(),
        &["run", "show", RUN, "--file", "big.txt"],
        &[
            manifest(&["big.txt"]),
            file("big.txt", &"x".repeat(1024 * 1024 + 1)),
        ],
        None,
    );
    assert_eq!(code, 10);
    assert_eq!(report["problem"]["code"], "SERVICE_RESPONSE_TOO_LARGE");
    assert_eq!(
        report["problem"]["details"]["reason"],
        "\"big.txt\" is larger than 1048576 bytes; pass --output-file FILE to save it"
    );
}
