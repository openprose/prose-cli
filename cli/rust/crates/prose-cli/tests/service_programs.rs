//! Service programs: process-level checks the shared corpus cannot
//! express compactly. Behavior is pinned by
//! `cli/conformance/cases/service/programs/`; the Bun twin is
//! `cli/bun/test/service-programs.test.ts`.
use serde_json::{Value, json};
use std::process::Command;
use tempfile::TempDir;

#[cfg(feature = "test-seams")]
mod seams {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::path::Path;
    use std::process::Output;

    const KEY: &str = "rr_test_0123456789abcdef0123456789abcdef";

    fn prose(root: &Path, args: &[&str], fixture: &Value) -> Output {
        let path = root.join("fixture.json");
        std::fs::write(&path, fixture.to_string()).unwrap();
        Command::new(env!("CARGO_BIN_EXE_prose"))
            .args(args)
            .current_dir(root)
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", root)
            .env("XDG_CONFIG_HOME", root.join("config"))
            .env("XDG_STATE_HOME", root.join("state"))
            .env("HTTPS_PROXY", "http://127.0.0.1:9")
            .env("PROSE_TEST_SERVICE_FIXTURE", path)
            .output()
            .unwrap()
    }

    fn fixture(exchanges: &Value) -> Value {
        json!({
            "environment": "production",
            "credentials": {"production": KEY},
            "storeAvailable": true,
            "exchanges": exchanges,
        })
    }

    const SAVE: &[&str] = &[
        "--output",
        "json",
        "cli",
        "program",
        "save",
        "demo",
        "big.prose.md",
    ];

    #[test]
    fn save_refuses_programs_over_256_kib_before_any_request() {
        // Generated at runtime: a 256 KiB corpus file would bloat the public repository.
        let temp = TempDir::new().unwrap();
        std::fs::write(temp.path().join("big.prose.md"), "x".repeat(256 * 1024 + 1)).unwrap();
        let output = prose(temp.path(), SAVE, &fixture(&json!([])));
        assert_eq!(output.status.code(), Some(2));
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["problem"]["code"], "INVOCATION_INVALID");
        assert_eq!(
            report["problem"]["details"]["reason"],
            "FILE \"big.prose.md\" is larger than 262144 bytes"
        );
    }

    #[test]
    fn save_sends_a_program_of_exactly_256_kib_unchanged() {
        let temp = TempDir::new().unwrap();
        let content = "y".repeat(256 * 1024);
        std::fs::write(temp.path().join("big.prose.md"), &content).unwrap();
        let body = serde_json::to_vec(&json!({"content": content})).unwrap();
        let digest = format!("{:x}", Sha256::digest(&body));
        let program = json!({
            "owner": "alice", "slug": "demo", "visibility": "private", "content": content, "rev": 1,
            "rev_id": "0123456789abcdef", "commit_id": "1111111111111111", "parent_commit_id": null,
            "updated_at": 1
        });
        let exchanges = json!([{
            "method": "PUT", "path": "/programs/demo", "expectedSha256": digest, "status": 200,
            "body": {"program": program, "url": "/p/alice/demo"}
        }]);
        let output = prose(temp.path(), SAVE, &fixture(&exchanges));
        assert_eq!(
            output.status.code(),
            Some(0),
            "{}",
            String::from_utf8_lossy(&output.stdout)
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["result"]["program"]["rev"], 1);
        assert!(report["result"]["program"].get("content").is_none());
    }

    #[test]
    fn list_over_the_schema_bound_is_too_large() {
        let temp = TempDir::new().unwrap();
        let record = json!({
            "owner": "alice", "slug": "demo", "visibility": "private", "rev": 1,
            "rev_id": "0123456789abcdef", "commit_id": "1111111111111111", "parent_commit_id": null,
            "updated_at": 1
        });
        let exchanges = json!([{
            "method": "GET", "path": "/programs", "status": 200,
            "body": {"programs": vec![record; 1001]}
        }]);
        let output = prose(
            temp.path(),
            &["--output", "json", "cli", "program", "list"],
            &fixture(&exchanges),
        );
        assert_eq!(output.status.code(), Some(10));
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["problem"]["code"], "SERVICE_RESPONSE_TOO_LARGE");
    }
}

#[test]
fn draft_without_yes_needs_no_credential_or_network() {
    // Ordinary and test-seam builds alike: the paid draft stops at the gate.
    let temp = TempDir::new().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_prose"))
        .args([
            "--output",
            "json",
            "cli",
            "program",
            "draft",
            "write a haiku",
        ])
        .current_dir(temp.path())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", temp.path())
        .env("XDG_CONFIG_HOME", temp.path().join("config"))
        .env("XDG_STATE_HOME", temp.path().join("state"))
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["problem"]["code"], "CONFIRMATION_REQUIRED");
    // The plan names no internal feature flag.
    let planned = &report["problem"]["details"]["plannedRequest"];
    assert_eq!(planned["operation"], "program.draft");
    assert!(planned.get("flags").is_none(), "{planned}");
}
