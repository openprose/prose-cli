//! Service wallet: process-level checks that need no service. Behavior
//! against the service is pinned by `cli/conformance/cases/service/wallet/`;
//! projection unit tests live in `prose-runner-core` (`service::wallet`).
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

const CODE: &str = "Q7RC9-V5K2M";

fn prose(root: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(["--output", "json", "cli", "wallet"])
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_STATE_HOME", root.join("state"))
        // Any network attempt fails fast instead of reaching the service.
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .env(
            "OPENPROSE_API_KEY",
            "rr_test_0123456789abcdef0123456789abcdef",
        )
        .output()
        .unwrap()
}

fn report(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn redeem_plan_never_carries_the_code_or_its_digest() {
    let temp = TempDir::new().unwrap();
    std::fs::write(temp.path().join("code.txt"), format!("{CODE}\n")).unwrap();
    for extra in [None, Some("--preview")] {
        let mut args = vec!["redeem", "--code-file", "code.txt"];
        args.extend(extra);
        let output = prose(temp.path(), &args);
        let text = String::from_utf8_lossy(&output.stdout).into_owned()
            + &String::from_utf8_lossy(&output.stderr);
        let digest = hex(&Sha256::digest(format!("{{\"code\":\"{CODE}\"}}")));
        assert!(!text.contains(CODE), "{text}");
        assert!(!text.contains(&digest), "{text}");
        let report = report(&output);
        let planned = if extra.is_some() {
            assert_eq!(output.status.code(), Some(0));
            &report["result"]["plannedRequest"]
        } else {
            assert_eq!(output.status.code(), Some(2));
            &report["problem"]["details"]["plannedRequest"]
        };
        assert_eq!(planned["bodySha256"], Value::Null);
        assert_eq!(planned["description"], "Redeem a one-time credit code.");
        assert!(planned.get("path").is_none());
    }
}

#[test]
fn topup_preview_digests_the_exact_body_and_mints_no_key() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["topup", "--amount-cents", "500", "--preview"],
    );
    assert_eq!(output.status.code(), Some(0));
    let planned = &report(&output)["result"]["plannedRequest"];
    assert_eq!(
        planned["bodySha256"],
        hex(&Sha256::digest(b"{\"amount_cents\":500}"))
    );
    assert_eq!(planned["bodyBytes"], 20);
    assert!(!String::from_utf8_lossy(&output.stdout).contains("idempotencyKey"));
}

#[test]
fn invalid_wallet_inputs_fail_before_any_request() {
    let temp = TempDir::new().unwrap();
    for args in [
        &["events", "--limit", "0"][..],
        &["events", "--limit", "101"],
        &["events", "--before", ""],
        &["usage", "--start", "2026-9-01"],
        &["usage", "--start", "2026-09-02", "--end", "2026-09-01"],
        &["topup", "--amount-cents", "5.00", "--yes"],
        &["topup", "--amount-cents", "0", "--yes"],
        &[
            "topup",
            "--amount-cents",
            "500",
            "--idempotency-key",
            "not-a-uuid",
            "--yes",
        ],
        &["redeem", "--code-file", "missing.txt", "--yes"],
    ] {
        let output = prose(temp.path(), args);
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        assert_eq!(
            report(&output)["problem"]["code"],
            "INVOCATION_INVALID",
            "{args:?}"
        );
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
