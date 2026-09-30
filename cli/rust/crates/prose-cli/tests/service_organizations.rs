//! Service organizations: process-level checks the shared corpus cannot
//! express. Behavior is pinned by `cli/conformance/cases/service/organizations/`.
use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

fn prose(root: &Path, args: &[&str]) -> Output {
    // No credential and a dead proxy: anything that reached the network would
    // fail differently.
    Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_STATE_HOME", root.join("state"))
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .output()
        .unwrap()
}

fn manifest() -> Value {
    serde_json::from_slice(include_bytes!(
        "../../../../shared/service/operations.v1.json"
    ))
    .unwrap()
}

/// Minimal valid argv for every organization mutation (without `--yes`).
const MUTATIONS: &[(&str, &[&str])] = &[
    ("org.create", &["org", "create", "acme-research"]),
    ("org.default", &["org", "default", "acme-research"]),
    (
        "org.invitation.accept",
        &[
            "org",
            "invitation",
            "accept",
            "--token-file",
            "invite.token",
        ],
    ),
    (
        "org.invitation.revoke",
        &["org", "invitation", "revoke", "acme-research", "inv_1"],
    ),
    (
        "org.invite",
        &[
            "org",
            "invite",
            "acme-research",
            "cus_x",
            "--role",
            "reader",
        ],
    ),
    (
        "org.member.remove",
        &["org", "member", "remove", "acme-research", "cus_x"],
    ),
    (
        "org.member.role",
        &["org", "member", "role", "acme-research", "cus_x", "reader"],
    ),
    (
        "org.rename",
        &["org", "rename", "acme-research", "Acme Labs"],
    ),
];

#[test]
fn there_is_no_org_delete_and_every_organization_mutation_is_confirm_class() {
    let manifest = manifest();
    let operations = manifest["operations"].as_array().unwrap();
    assert!(
        !operations
            .iter()
            .any(|operation| operation["command"] == serde_json::json!(["cli", "org", "delete"]))
    );
    let mut mutations = operations
        .iter()
        .filter(|operation| {
            operation["feature"] == "organizations"
                && operation["contract"] == "service/1"
                && operation["mutation"] == true
        })
        .map(|operation| {
            assert_eq!(operation["confirm"], true, "{}", operation["id"]);
            assert_eq!(operation["preview"], true, "{}", operation["id"]);
            operation["id"].as_str().unwrap().to_owned()
        })
        .collect::<Vec<_>>();
    mutations.sort();
    let expected = MUTATIONS
        .iter()
        .map(|(id, _)| (*id).to_owned())
        .collect::<Vec<_>>();
    assert_eq!(mutations, expected);
}

#[test]
fn every_organization_mutation_without_yes_needs_no_key_and_sends_nothing() {
    let temp = TempDir::new().unwrap();
    std::fs::write(temp.path().join("invite.token"), "x".repeat(72)).unwrap();
    for (id, argv) in MUTATIONS {
        let mut args = vec!["--output", "json", "cli"];
        args.extend_from_slice(argv);
        let output = prose(temp.path(), &args);
        assert_eq!(output.status.code(), Some(2), "{id}");
        assert!(output.stderr.is_empty(), "{id}");
        let document: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(document["problem"]["code"], "CONFIRMATION_REQUIRED", "{id}");
        assert_eq!(
            document["problem"]["details"]["plannedRequest"]["operation"], *id,
            "{id}"
        );
    }
}
