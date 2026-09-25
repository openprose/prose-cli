//! The compiled `prose` binary uses the macOS login keychain through
//! `/usr/bin/security -i`. A fake `security` (the test-seam
//! `PROSE_TEST_MACOS_SECURITY`) stands in for the real tool; nothing here
//! touches a keychain or the network (every case ends before the first
//! request). The Bun twin is `cli/bun/test/credential-store-macos-cli.test.ts`.
#![cfg(target_os = "macos")]

use serde_json::Value;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

const TOKEN: &str = "rr_test_0123456789abcdef0123456789abcdef";

/// A fake `security -i` that records each session's standard input
/// (`stdin-<n>`) and replies with `reply-<n>` (stdout), `error-<n>` (stderr)
/// and `exit-<n>` (status).
fn fake(root: &Path) -> std::path::PathBuf {
    let program = root.join("security");
    let script = format!(
        r#"#!/bin/sh
R='{root}'
n=$(/bin/cat "$R/n" 2>/dev/null || echo 0)
echo $((n + 1)) > "$R/n"
/bin/cat > "$R/stdin-$n"
[ -f "$R/reply-$n" ] && /bin/cat "$R/reply-$n"
[ -f "$R/error-$n" ] && /bin/cat "$R/error-$n" >&2
exit $(/bin/cat "$R/exit-$n" 2>/dev/null || echo 0)
"#,
        root = root.display()
    );
    fs::write(&program, script).unwrap();
    fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
    program
}

fn prose(root: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("HOME", root.join("home"))
        .env("PATH", "/usr/bin:/bin")
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .env("PROSE_TEST_MACOS_SECURITY", fake(root))
        .output()
        .unwrap()
}

fn sessions(root: &Path) -> Vec<String> {
    (0..)
        .map_while(|n| fs::read_to_string(root.join(format!("stdin-{n}"))).ok())
        .collect()
}

fn json(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|_| {
        panic!(
            "stdout is not JSON: {}\nstderr: {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

#[test]
fn macos_logout_and_signed_out_status_use_security() {
    let temp = TempDir::new().unwrap();
    let root = temp.path();
    let output = prose(root, &["cli", "auth", "logout", "--json"]);
    let report = json(&output);
    assert_eq!(output.status.code(), Some(0), "{report}");
    assert_eq!(report["result"]["authenticated"], false);
    assert_eq!(
        sessions(root),
        ["delete-generic-password -s org.openprose.cli.production -a api-key\n"]
    );
    // No item: status probes, finds nothing and sends nothing.
    fs::write(
        root.join("error-1"),
        "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n",
    )
    .unwrap();
    let output = prose(root, &["cli", "auth", "status", "--json"]);
    let report = json(&output);
    assert_eq!(output.status.code(), Some(0), "{report}");
    assert_eq!(report["result"]["authenticated"], false);
    assert_eq!(report["result"]["credentialSource"], "none");
    assert_eq!(
        sessions(root)[1],
        "find-generic-password -s org.openprose.cli.production -a api-key\n"
    );
}

#[test]
fn macos_earlier_item_needing_a_prompt_names_the_keychain_and_the_variable() {
    let temp = TempDir::new().unwrap();
    let root = temp.path();
    fs::write(root.join("reply-0"), "\"icmt\"<blob>=<NULL>\n").unwrap();
    fs::write(
        root.join("error-1"),
        "security: SecKeychainItemCopyContent: User interaction is not allowed.\n",
    )
    .unwrap();
    let output = prose(root, &["cli", "auth", "status", "--json"]);
    let report = json(&output);
    assert_eq!(output.status.code(), Some(10), "{report}");
    let error = &report["problem"];
    assert_eq!(error["code"], "CREDENTIAL_STORE_UNAVAILABLE");
    assert!(
        error["details"]["reason"]
            .as_str()
            .unwrap()
            .starts_with("the macOS keychain needs approval in a prompt"),
        "{error}"
    );
    assert!(
        error["action"]
            .as_str()
            .unwrap()
            .contains("OPENPROSE_API_KEY")
    );
}

#[test]
fn macos_malformed_stored_value_is_rejected_by_the_caller_and_not_migrated() {
    let temp = TempDir::new().unwrap();
    let root = temp.path();
    fs::write(root.join("reply-0"), "\"icmt\"<blob>=<NULL>\n").unwrap();
    fs::write(root.join("reply-1"), "not-a-key\n").unwrap();
    let output = prose(root, &["cli", "auth", "status", "--json"]);
    let report = json(&output);
    assert_eq!(output.status.code(), Some(10), "{report}");
    assert_eq!(report["problem"]["code"], "SERVICE_AUTH_REQUIRED");
    assert_eq!(
        report["problem"]["details"]["credentialProblem"],
        "malformed"
    );
    assert_eq!(sessions(root).len(), 2, "no migration of an invalid value");
    assert!(!format!("{report}").contains(TOKEN));
}
