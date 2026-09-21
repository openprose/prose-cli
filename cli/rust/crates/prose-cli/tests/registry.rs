#![cfg(feature = "test-seams")]
use serde_json::{Value, json};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
fn receipt() -> Value {
    serde_json::from_slice(include_bytes!(
        "../../../../shared/fixtures/registry/single-file.receipt.json"
    ))
    .unwrap()
}
fn invoke(root: &Path, args: &[&str], fixture: Value, environment: &[(&str, &str)]) -> Output {
    let fixture_path = root.join("fixture.json");
    fs::write(&fixture_path, fixture.to_string()).unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_prose"));
    command
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("HOME", root.join("home"))
        .env("XDG_CONFIG_HOME", root.join("xdg"))
        .env("PROSE_TEST_SERVICE_FIXTURE", fixture_path);
    for (key, value) in environment {
        command.env(key, value);
    }
    command.output().unwrap()
}
fn fixture(exchanges: Value) -> Value {
    json!({"environment":"production","credential":null,"storeAvailable":true,"exchanges":exchanges})
}
fn report(output: &Output) -> Value {
    assert!(
        output.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}
#[test]
fn registry_plus_build_path_and_selected_credentials_are_exact() {
    let root = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(root.path()).unwrap();
    let mut receipt = receipt();
    receipt["reference"]["version"] = json!("1.0.0+build");
    let value = json!({"receipt":receipt,"withdrawn":true});
    let output = invoke(
        &root,
        &[
            "cli",
            "package",
            "withdraw",
            "example/hello@1.0.0+build",
            "--json",
        ],
        fixture(
            json!([{"method":"POST","path":"/registry/v1/organizations/example/packages/hello/versions/1.0.0+build/withdraw","origin":"https://run-prose-production.openprose.workers.dev","status":200,"body":value}]),
        ),
        &[
            (
                "OPENPROSE_API_KEY",
                "rr_test_11111111111111111111111111111111",
            ),
            ("OPENPROSE_STAGING_API_KEY", "invalid-other-environment"),
        ],
    );
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(report(&output)["result"], value);
    let output = invoke(
        &root,
        &[
            "cli",
            "package",
            "withdraw",
            "example/hello@1.0.0",
            "--json",
        ],
        fixture(json!([])),
        &[(
            "OPENPROSE_STAGING_API_KEY",
            "rr_test_11111111111111111111111111111111",
        )],
    );
    assert_eq!(report(&output)["problem"]["code"], "SERVICE_AUTH_REQUIRED");
}
#[test]
fn anonymous_public_listing_supports_cursor_and_unavailable_store_only() {
    let root = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(root.path()).unwrap();
    let listing = json!({"packages":[],"nextCursor":"public:hello:1.0.0+build"});
    let fixture = json!({"environment":"production","credential":null,"storeAvailable":false,"exchanges":[{"method":"GET","path":"/registry/v1/organizations/example/packages?cursor=public%3Ahello%3A1.0.0%2Bbuild","status":200,"body":listing}]});
    let args = [
        "cli",
        "package",
        "list",
        "example",
        "--cursor",
        "public:hello:1.0.0+build",
        "--json",
    ];
    let output = invoke(&root, &args, fixture.clone(), &[]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(report(&output)["result"], listing);
    let output = invoke(&root, &args, fixture, &[("OPENPROSE_API_KEY", "bad-token")]);
    assert_eq!(
        report(&output)["problem"]["code"],
        "SERVICE_PROTOCOL_INVALID"
    );
    let output = invoke(
        &root,
        &["cli", "package", "list", "example", "--json"],
        json!({"environment":"production","credential":"bad-stored-token","storeAvailable":true,"exchanges":[]}),
        &[],
    );
    assert_eq!(
        report(&output)["problem"]["code"],
        "SERVICE_PROTOCOL_INVALID"
    );
}
#[test]
fn fetch_rejects_existing_targets_wrong_hash_and_noncanonical_artifacts() {
    let root = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(root.path()).unwrap();
    let receipt = receipt();
    let canonical =
        include_str!("../../../../shared/fixtures/registry/single-file.canonical.json").to_owned();
    let exchanges = json!([{"method":"GET","path":"/registry/v1/organizations/example/packages/hello/versions/1.0.0","status":200,"body":receipt},{"method":"GET","path":"/registry/v1/organizations/example/packages/hello/versions/1.0.0/artifact","status":200,"body":canonical}]);
    fs::create_dir(root.join("existing")).unwrap();
    let output = invoke(
        &root,
        &[
            "cli",
            "package",
            "fetch",
            "example/hello@1.0.0",
            "--output-dir",
            "existing",
            "--json",
        ],
        fixture(exchanges.clone()),
        &[],
    );
    assert_eq!(output.status.code(), Some(2));
    assert!(
        fs::read_dir(root.join("existing"))
            .unwrap()
            .next()
            .is_none()
    );
    let output = invoke(
        &root,
        &[
            "cli",
            "package",
            "fetch",
            "example/hello@1.0.0",
            "--output-dir",
            "bad",
            "--sha256",
            "0000000000000000000000000000000000000000000000000000000000000000",
            "--json",
        ],
        fixture(exchanges.clone()),
        &[],
    );
    assert_eq!(output.status.code(), Some(10));
    assert!(!root.join("bad").exists());
    let mut tampered = exchanges;
    tampered[1]["body"] = json!(format!(" {canonical}"));
    let output = invoke(
        &root,
        &[
            "cli",
            "package",
            "fetch",
            "example/hello@1.0.0",
            "--output-dir",
            "tampered",
            "--json",
        ],
        fixture(tampered),
        &[],
    );
    assert_eq!(
        report(&output)["problem"]["code"],
        "SERVICE_PROTOCOL_INVALID"
    );
    assert!(!root.join("tampered").exists());
}
#[test]
fn publish_refuses_wrong_upload_expectation_and_does_not_echo_raw_service_errors() {
    let root = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(root.path()).unwrap();
    fs::write(root.join("hello.md"), b"# Hello\n").unwrap();
    let args = [
        "cli",
        "package",
        "publish",
        "hello.md",
        "--organization",
        "example",
        "--name",
        "hello",
        "--version",
        "1.0.0",
        "--json",
    ];
    let exchange = json!({"method":"POST","path":"/registry/v1/organizations/example/packages/hello/versions","status":201,"body":receipt(),"expectedBody":"wrong"});
    let output = invoke(
        &root,
        &args,
        fixture(json!([exchange])),
        &[(
            "OPENPROSE_API_KEY",
            "rr_test_11111111111111111111111111111111",
        )],
    );
    assert_eq!(
        report(&output)["problem"]["code"],
        "SERVICE_PROTOCOL_INVALID"
    );
    let exchange = json!({"method":"POST","path":"/registry/v1/organizations/example/packages/hello/versions","status":409,"body":{"error":"do-not-echo-service-body"}});
    let output = invoke(
        &root,
        &args,
        fixture(json!([exchange])),
        &[(
            "OPENPROSE_API_KEY",
            "rr_test_11111111111111111111111111111111",
        )],
    );
    assert_eq!(
        report(&output)["problem"]["code"],
        "SERVICE_PROTOCOL_INVALID"
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains("do-not-echo"));
}
