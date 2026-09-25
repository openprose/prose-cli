//! Service framework: process-level checks that the shared corpus
//! cannot express (build-mode seams). Behavior is pinned by
//! `cli/conformance/cases/service/framework/`.
use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

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
        .env("HTTPS_PROXY", "http://127.0.0.1:9");
    if let Some(fixture) = fixture {
        let path = root.join("fixture.json");
        std::fs::write(&path, fixture).unwrap();
        command.env("PROSE_TEST_SERVICE_FIXTURE", path);
    }
    command.output().unwrap()
}

/// The public projection: `true` copies a member, an object recurses (into
/// each element of an array).
fn project(value: &serde_json::Value, fields: &serde_json::Value) -> serde_json::Value {
    match (value, fields) {
        (_, serde_json::Value::Bool(true)) => value.clone(),
        (serde_json::Value::Array(items), _) => {
            serde_json::Value::Array(items.iter().map(|item| project(item, fields)).collect())
        }
        (serde_json::Value::Object(object), serde_json::Value::Object(allowed)) => {
            serde_json::Value::Object(
                allowed
                    .iter()
                    .filter_map(|(key, sub)| {
                        object
                            .get(key)
                            .map(|item| (key.clone(), project(item, sub)))
                    })
                    .collect(),
            )
        }
        _ => value.clone(),
    }
}

#[test]
fn service_operations_prints_the_manifest_byte_for_byte() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["--output", "json", "cli", "service", "operations"],
        None,
    );
    assert!(output.status.success());
    // One envelope line whose result is the published manifest: the file
    // without the client's own implementation sections.
    assert_eq!(
        output.stdout.iter().filter(|byte| **byte == b'\n').count(),
        1
    );
    let document: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let manifest: serde_json::Value = serde_json::from_slice(include_bytes!(
        "../../../../shared/service/operations.v1.json"
    ))
    .unwrap();
    let projection: serde_json::Value = serde_json::from_slice(include_bytes!(
        "../../../../shared/service/operations-public.v1.json"
    ))
    .unwrap();
    let published = project(&manifest, &projection["fields"]);
    let text = document["result"].to_string();
    assert!(text.len() <= projection["maxBytes"].as_u64().unwrap() as usize);
    for forbidden in projection["forbid"].as_array().unwrap() {
        assert!(!text.contains(forbidden.as_str().unwrap()), "{forbidden}");
    }
    assert_eq!(document["schema"], "openprose.service-operation/1");
    assert_eq!(document["operation"], "service.operations");
    assert_eq!(document["result"], published);
}

#[test]
fn cli_help_is_the_generated_service_topic() {
    let temp = TempDir::new().unwrap();
    let output = prose(temp.path(), &["cli", "--help"], None);
    assert!(output.status.success());
    let help: Value =
        serde_json::from_slice(include_bytes!("../../../../shared/service/help.v1.json")).unwrap();
    let topic = help["topics"]["cli"].as_str().unwrap();
    // A developer build names the executable it was invoked as (here its
    // path) wherever the stored topic says `prose`; a public build prints the
    // topic byte for byte.
    let expected = if cfg!(feature = "dev-endpoint") {
        prose_runner_core::service::render::localize_help_for(
            &prose_runner_core::service::render::shell_quote(env!("CARGO_BIN_EXE_prose")),
            topic,
        )
    } else {
        topic.to_owned()
    };
    assert_eq!(String::from_utf8(output.stdout).unwrap(), expected);
}

#[test]
fn only_test_seam_builds_read_the_service_fixture() {
    // An unreadable fixture fails a test-seam build before dispatch; an
    // ordinary build never opens it and reaches the confirmation gate, which
    // needs no network.
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["--output", "json", "cli", "program", "delete", "demo"],
        Some("not json"),
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    if cfg!(feature = "test-seams") {
        assert_eq!(output.status.code(), Some(10));
        assert_eq!(report["problem"]["code"], "SERVICE_PROTOCOL_INVALID");
    } else {
        assert_eq!(output.status.code(), Some(2));
        assert_eq!(report["problem"]["code"], "CONFIRMATION_REQUIRED");
    }
}

#[cfg(feature = "test-seams")]
#[test]
fn oversize_success_bodies_are_too_large() {
    // Generated at runtime: a > 1 MiB corpus file would exceed the repository's
    // new-file limit. The Bun twin is in cli/bun/test/service-framework.test.ts.
    let temp = TempDir::new().unwrap();
    let fixture = serde_json::json!({
        "environment": "production",
        "exchanges": [{"method": "GET", "path": "/health", "status": 200, "bodyText": "x".repeat(1024 * 1024 + 1)}]
    });
    let output = prose(
        temp.path(),
        &["--output", "json", "cli", "service", "status"],
        Some(&fixture.to_string()),
    );
    assert_eq!(output.status.code(), Some(10));
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["problem"]["code"], "SERVICE_RESPONSE_TOO_LARGE");
    assert_eq!(report["operation"], "service.status");
}
