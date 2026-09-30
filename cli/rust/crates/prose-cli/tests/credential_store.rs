//! The compiled `prose` binary uses the Linux Secret Service
//! through `secret-tool` for the service API key. A fake
//! `secret-tool` on `PATH` stands in for libsecret; nothing here touches the
//! network (every case ends before the first request) or the real keyring.
#![cfg(target_os = "linux")]

use serde_json::Value;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

const SCHEMA: &str = "com.oven-sh.bun.Secret";

/// A fake `secret-tool` recording each call's argv (one call per line, fields
/// separated by `|`) and backed by one state file per `service` attribute.
fn fake_tool(root: &Path) {
    let bin = root.join("bin");
    fs::create_dir_all(&bin).unwrap();
    let script = format!(
        r#"#!/bin/sh
R='{root}'
line=""
for a in "$@"; do line="$line|$a"; done
printf '%s\n' "$line" >> "$R/calls"
[ -f "$R/fail" ] && {{ echo 'secret-tool: Could not connect: No such file or directory' >&2; exit 1; }}
service=""
prev=""
for a in "$@"; do [ "$prev" = service ] && service="$a"; prev="$a"; done
item="$R/item-$service"
case "$1" in
  store) /bin/cat > "$item"; exit 0 ;;
  lookup) [ -f "$item" ] || exit 1; /bin/cat "$item"; exit 0 ;;
  clear) [ -f "$item" ] || exit 1; /bin/rm "$item"; exit 0 ;;
esac
exit 2
"#,
        root = root.display()
    );
    let program = bin.join("secret-tool");
    fs::write(&program, script).unwrap();
    fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
}

fn prose(root: &Path, path: &Path, bus: bool, args: &[&str]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_prose"));
    command
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("HOME", root.join("home"))
        .env("XDG_CONFIG_HOME", root.join("xdg"))
        .env("XDG_STATE_HOME", root.join("state"))
        .env("PATH", path)
        // The host may have the real tool in a system directory, which the
        // binary prefers over PATH; point the (test-seams) list elsewhere.
        .env("PROSE_TEST_SECRET_TOOL_SYSTEM_DIRS", system_dirs(root));
    if bus {
        command.env("DBUS_SESSION_BUS_ADDRESS", "unix:path=/fake/bus");
    }
    command.output().unwrap()
}

/// The system directories for this test root: `<root>/system` if present.
fn system_dirs(root: &Path) -> String {
    let system = root.join("system");
    if system.exists() {
        system.display().to_string()
    } else {
        String::new()
    }
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

fn calls(root: &Path) -> Vec<String> {
    fs::read_to_string(root.join("calls"))
        .unwrap_or_default()
        .lines()
        .map(str::to_owned)
        .collect()
}

fn attributes(environment: &str) -> String {
    format!("|xdg:schema|{SCHEMA}|service|org.openprose.cli.{environment}|account|api-key")
}

#[test]
fn credential_store_auth_status_and_logout_use_secret_tool() {
    let temp = TempDir::new().unwrap();
    fake_tool(temp.path());
    let bin = temp.path().join("bin");
    {
        let environment = "production";
        let item = temp
            .path()
            .join(format!("item-org.openprose.cli.{environment}"));
        fs::write(&item, "rr_test_0123456789abcdef0123456789abcdef").unwrap();
        let output = prose(
            temp.path(),
            &bin,
            true,
            &["--output", "json", "cli", "auth", "logout"],
        );
        let report = json(&output);
        assert_eq!(output.status.code(), Some(0), "{report}");
        assert_eq!(report["schema"], "openprose.service-account/1");
        assert_eq!(report["environment"], environment);
        assert_eq!(report["authenticated"], false);
        assert_eq!(report["problem"], Value::Null);
        assert!(!item.exists(), "logout removed the stored key");
        assert_eq!(
            calls(temp.path()).last().unwrap(),
            &format!("|clear{}", attributes(environment))
        );

        // Signed out: status reads the store, finds nothing, sends nothing.
        let output = prose(
            temp.path(),
            &bin,
            true,
            &["--output", "json", "cli", "auth", "status"],
        );
        let report = json(&output);
        assert_eq!(output.status.code(), Some(0), "{report}");
        assert_eq!(report["authenticated"], false);
        assert_eq!(report["credentialSource"], "none");
        assert_eq!(
            calls(temp.path()).last().unwrap(),
            &format!("|lookup{}", attributes(environment))
        );
        // Logout of a missing item stays successful (idempotent).
        let output = prose(
            temp.path(),
            &bin,
            true,
            &["--output", "json", "cli", "auth", "logout"],
        );
        assert_eq!(output.status.code(), Some(0));
    }
    // No call ever carried a key on argv.
    assert!(
        calls(temp.path())
            .iter()
            .all(|call| !call.contains("rr_test_"))
    );
}

#[test]
fn credential_store_missing_tool_or_dbus_names_the_environment_variable() {
    let temp = TempDir::new().unwrap();
    fake_tool(temp.path());
    let bin = temp.path().join("bin");
    let empty = temp.path().join("empty");
    fs::create_dir_all(&empty).unwrap();
    // (PATH, D-Bus): missing tool; tool present but no session bus; the tool's own D-Bus failure.
    for (path, bus, fail) in [
        (&empty, true, false),
        (&bin, false, false),
        (&bin, true, true),
    ] {
        if fail {
            fs::write(temp.path().join("fail"), "").unwrap();
        }
        let before = calls(temp.path()).len();
        let output = prose(
            temp.path(),
            path,
            bus,
            &["--output", "json", "cli", "wallet", "balance"],
        );
        let report = json(&output);
        assert_ne!(output.status.code(), Some(0), "{report}");
        let problem = if report["problem"].is_object() {
            &report["problem"]
        } else {
            &report
        };
        assert_eq!(problem["code"], "CREDENTIAL_STORE_UNAVAILABLE", "{report}");
        let reason = problem["details"]["reason"].as_str().unwrap_or_default();
        assert!(reason.contains("OPENPROSE_API_KEY"), "{report}");
        if !bus {
            assert_eq!(
                calls(temp.path()).len(),
                before,
                "no session bus: nothing spawned"
            );
        }

        let output = prose(
            temp.path(),
            path,
            bus,
            &["--output", "json", "cli", "auth", "logout"],
        );
        let report = json(&output);
        assert_eq!(
            report["problem"]["code"], "CREDENTIAL_STORE_UNAVAILABLE",
            "{report}"
        );
    }
}

fn run_json(root: &Path, path: &Path, args: &[&str]) -> (Option<i32>, Value) {
    let mut full = vec!["--output", "json", "cli"];
    full.extend_from_slice(args);
    let output = prose(root, path, true, &full);
    (output.status.code(), json(&output))
}

fn problem(report: &Value) -> &Value {
    if report["problem"].is_object() {
        &report["problem"]
    } else {
        report
    }
}

/// A malformed or newline-terminated stored value is returned unchanged by the
/// store and rejected by the caller, exactly as the Bun build does: service
/// commands report `SERVICE_AUTH_REQUIRED` with a reason (no request is sent),
/// the account `auth status` reports `SERVICE_PROTOCOL_INVALID`, and `auth logout`
/// still recovers.
#[test]
fn credential_store_malformed_item_is_rejected_by_callers_like_bun() {
    let temp = TempDir::new().unwrap();
    fake_tool(temp.path());
    let bin = temp.path().join("bin");
    let item = temp.path().join("item-org.openprose.cli.production");
    for value in [
        "garbage-value",
        "rr_test_0123456789abcdef0123456789abcdef\n",
    ] {
        fs::write(&item, value).unwrap();
        let (code, report) = run_json(temp.path(), &bin, &["wallet", "balance"]);
        assert_eq!(code, Some(10), "{report}");
        let problem = problem(&report);
        assert_eq!(problem["code"], "SERVICE_AUTH_REQUIRED", "{report}");
        let reason = problem["details"]["reason"].as_str().unwrap_or_default();
        assert!(
            reason.contains("OPENPROSE_API_KEY") && reason.contains("not a valid"),
            "{report}"
        );
        assert!(
            problem["details"]["serviceStatus"].is_null(),
            "no request: {report}"
        );

        let (code, report) = run_json(temp.path(), &bin, &["auth", "status"]);
        assert_eq!(code, Some(10), "{report}");
        assert_eq!(
            problem_code(&report),
            "SERVICE_PROTOCOL_INVALID",
            "{report}"
        );

        let (code, report) = run_json(temp.path(), &bin, &["auth", "logout"]);
        assert_eq!(code, Some(0), "{report}");
        assert!(!item.exists());
    }
}

fn problem_code(report: &Value) -> String {
    problem(report)["code"]
        .as_str()
        .unwrap_or_default()
        .to_owned()
}

/// A `secret-tool` in a system directory wins over one earlier on PATH, so a
/// shadowing program never sees a lookup or the key.
#[test]
fn credential_store_system_tool_wins_over_a_path_shadow() {
    let temp = TempDir::new().unwrap();
    let shadow = temp.path().join("shadow");
    fake_tool(&shadow);
    fake_tool(temp.path());
    fs::rename(temp.path().join("bin"), temp.path().join("system")).unwrap();
    let (code, report) = run_json(temp.path(), &shadow.join("bin"), &["auth", "status"]);
    assert_eq!(code, Some(0), "{report}");
    assert_eq!(report["authenticated"], false, "{report}");
    assert!(calls(&shadow).is_empty(), "the PATH shadow was never run");
    assert_eq!(calls(temp.path()).len(), 1, "the system tool answered");
}
