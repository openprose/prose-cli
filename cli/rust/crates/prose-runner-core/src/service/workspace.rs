//! Program workspaces: `program list|show|save|visibility|delete|revisions
//! --org ORG` act in an organization's namespace, and `program copy --to ORG`
//! copies a program into one. Every workspace route and query name the client
//! sends is written here and in the manifest only (mirrors Bun
//! `service/workspace.ts`); both are pinned by
//! `cli/conformance/cases/service/programs/`.
use super::Context;
use super::http::{Request, encode_segment};
use super::program_ref::{ref_of, valid_owner, valid_rev, valid_slug};
use crate::RunnerError;
use crate::error::{ErrorCode, quote};
use serde_json::{Map, Value, json};
use std::fmt::Write as _;

/// The query parameter that names the organization namespace of a
/// `/programs` route.
pub const WORKSPACE_QUERY: &str = "workspace";

/// `POST /p/{owner}/{slug}[@rev]/copy`: the copy route, addressed like a
/// program read.
pub fn copy_path(owner: &str, slug: &str, rev: Option<&str>) -> String {
    let mut path = format!("/p/{}/{}", encode_segment(owner), encode_segment(slug));
    if let Some(rev) = rev {
        path.push('@');
        path.push_str(rev);
    }
    path.push_str("/copy");
    path
}

/// Adds the organization namespace to a `/programs` request; personal
/// requests carry no query.
#[must_use]
pub fn scoped(request: Request, organization: Option<&str>) -> Request {
    match organization {
        Some(organization) => request.query(WORKSPACE_QUERY, organization),
        None => request,
    }
}

/// Lowercase letters, digits and interior hyphens, `1..=max` characters.
fn slug_shape(value: &str, max: usize) -> bool {
    let bytes = value.as_bytes();
    (1..=max).contains(&bytes.len())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
        && bytes[0] != b'-'
        && bytes[bytes.len() - 1] != b'-'
}

/// An organization slug that can own programs: lowercase letters, digits and
/// interior hyphens, at most 39 characters (a program owner's limit).
pub fn valid_namespace(value: &str) -> bool {
    slug_shape(value, 39)
}

/// The organization an option names (`--org` or `--to`), checked before any
/// request: an upper-case slug names the lower-case one to pass, and a slug
/// longer than a program owner can be is not an eligible namespace.
pub fn organization_option(
    context: &Context<'_>,
    option: &str,
) -> Result<Option<String>, RunnerError> {
    let Some(given) = context.option(option) else {
        return Ok(None);
    };
    if valid_namespace(given) {
        return Ok(Some(given.to_owned()));
    }
    let lower = given.to_ascii_lowercase();
    if lower != given && valid_namespace(&lower) {
        let error = RunnerError::invocation(format!(
            "{option} {} must be an organization slug in lowercase; pass {}",
            quote(given),
            quote(&lower)
        ));
        return Err(context.corrected(
            error,
            &format!("Pass {lower}: `{{command}}`"),
            context.argv_with_option(option, &lower),
        ));
    }
    if slug_shape(&lower, 63) {
        return Err(RunnerError::invocation(format!(
            "organization {} cannot hold programs: a program owner is at most 39 characters, and this slug has {}",
            quote(given),
            lower.len()
        )));
    }
    let list = context.command("org list");
    let mut error = RunnerError::invocation(format!(
        "{option} {} must be an organization slug as `{list}` prints it: lowercase letters, digits and interior hyphens",
        quote(given)
    ));
    error.action = format!("Run `{list}` to find the organization's slug, then retry with it.");
    Err(error.with_detail("suggestedArgv", context.follow_up_argv(&["org", "list"])))
}

/// `SERVICE_FEATURE_DISABLED` for a service that did not confirm the
/// organization namespace; nothing was changed.
fn unconfirmed_organization(organization: &str) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceFeatureDisabled).with_detail(
        "reason",
        format!(
            "the service did not confirm organization {organization}, so it does not offer organization programs; nothing was changed"
        ),
    )
}

/// Whether a `GET /programs?workspace=ORG` reply echoes ORG, as a service
/// with organization programs does.
pub fn echoes_organization(body: &Map<String, Value>, organization: &str) -> bool {
    body.get("workspace")
        .and_then(|workspace| workspace.get("slug"))
        .and_then(Value::as_str)
        .is_some_and(|slug| slug.to_ascii_lowercase() == organization)
}

/// Refuses an organization listing the service did not confirm (a service
/// that ignores `?workspace=` lists your own programs).
pub fn require_echo(body: &Map<String, Value>, organization: &str) -> Result<(), RunnerError> {
    if echoes_organization(body, organization) {
        Ok(())
    } else {
        Err(unconfirmed_organization(organization))
    }
}

/// Refuses organization revisions owned by anyone but the organization (a
/// service that ignores `?workspace=` lists yours).
pub fn require_organization_owner(
    revisions: &[Value],
    organization: &str,
) -> Result<(), RunnerError> {
    let foreign = revisions.iter().any(|revision| {
        revision["owner"]
            .as_str()
            .is_none_or(|owner| owner.to_ascii_lowercase() != organization)
    });
    if foreign {
        Err(unconfirmed_organization(organization))
    } else {
        Ok(())
    }
}

/// Confirms the organization before an organization-scoped write, through
/// `GET /programs?workspace=ORG` (manifest request `index`): the caller must
/// be a member (a 404 names the organization) and the service must echo the
/// organization, so a service that ignores `?workspace=` never receives a
/// write meant for the organization.
pub fn confirm_organization(
    context: &mut Context<'_>,
    index: usize,
    organization: &str,
) -> Result<(), RunnerError> {
    let request = scoped(
        Request::from_manifest(context.operation, index, "/programs"),
        Some(organization),
    )
    .class(super::http::TransportClass::Listing);
    let body = match context.send(&request) {
        Ok(response) => response.json_object()?,
        Err(error) => {
            return Err(organization_failure(
                context,
                error,
                organization,
                Role::Reader,
                "listing its programs",
                "--org",
                true,
            ));
        }
    };
    require_echo(&body, organization)
}

/// `OWNER` of `OWNER/SLUG` must be the `--org` organization (letter case
/// ignored).
pub fn require_same_organization(
    context: &Context<'_>,
    owner: &str,
    slug: &str,
    organization: &str,
    given: &str,
) -> Result<(), RunnerError> {
    if owner.is_empty() || owner.eq_ignore_ascii_case(organization) {
        return Ok(());
    }
    let error = RunnerError::invocation(format!(
        "OWNER {} is not --org {}: with --org the program is {organization}/{slug}; pass {}",
        quote(owner),
        quote(organization),
        quote(slug)
    ));
    let bare = given.split_once('/').map_or(given, |(_, rest)| rest);
    Err(context.corrected(
        error,
        &format!("Pass {slug}: `{{command}}`"),
        context.argv_with_argument(given, bare),
    ))
}

/// The organization role a verb needs (reader reads and runs; developer also
/// creates, edits and copies in; admin also changes visibility and deletes).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Reader,
    Developer,
    Admin,
}

impl Role {
    fn name(self) -> &'static str {
        match self {
            Self::Reader => "reader",
            Self::Developer => "developer",
            Self::Admin => "admin",
        }
    }
}

const ROLES_TEXT: &str = "reader can read and run; developer can also create, edit and copy in; admin can also change visibility and delete";

fn service_code(error: &RunnerError) -> Option<&str> {
    error
        .details
        .as_ref()
        .and_then(|details| details.get("serviceCode"))
        .and_then(Value::as_str)
}

/// Whether a failure names an organization the caller cannot see.
pub fn organization_missing(error: &RunnerError) -> bool {
    error.code == ErrorCode::ServiceResourceNotFound
        && service_code(error) == Some("organization_not_found")
}

/// Explains an organization-scoped failure: an organization the caller is
/// not a member of reads as not found (the service answers a non-member and
/// an unknown organization with the same 404; `any_missing` reads every 404
/// that way, for verbs whose only 404 is the organization), a role that does
/// not allow the verb names the role it needs, and a namespace the service
/// will not let own programs says so. Other failures pass through. `option`
/// is `--org`, or `--to` for `program copy`.
pub fn organization_failure(
    context: &Context<'_>,
    error: RunnerError,
    organization: &str,
    role: Role,
    what: &str,
    option: &str,
    any_missing: bool,
) -> RunnerError {
    if organization_missing(&error)
        || (any_missing && error.code == ErrorCode::ServiceResourceNotFound)
    {
        let error = error.with_detail(
            "reason",
            format!("organization {organization} was not found, or you are not a member of it"),
        );
        return super::not_found::explain_not_found(
            error,
            context.mode,
            "organization",
            organization,
            Some(&["org", "list"]),
        );
    }
    let otherwise = if option == "--org" {
        "act on your own programs without --org"
    } else {
        "copy into another organization"
    };
    let rejected = error.code == ErrorCode::ServiceRequestRejected;
    match service_code(&error) {
        Some("organization_forbidden") if rejected => {
            let role_name = role.name();
            let ask = if role == Role::Admin {
                format!("Ask an admin of {organization} to make you an admin")
            } else {
                format!("Ask an admin of {organization} for the {role_name} role")
            };
            let mut error = error
                .with_detail(
                    "reason",
                    format!(
                        "your role in organization {organization} does not allow this: {what} needs the {role_name} role ({ROLES_TEXT})"
                    ),
                )
                .with_detail("requiredRole", role_name);
            error.action = format!("{ask}, or {otherwise}.");
            error
        }
        Some("workspace_namespace_unavailable") if rejected => {
            let choose = format!("`{}` lists your organizations", context.command("org list"));
            let mut error = error.with_detail(
                "reason",
                format!(
                    "organization {organization} cannot hold programs: its slug is not an eligible program namespace"
                ),
            );
            error.action = if option == "--org" {
                format!("Choose another organization ({choose}), or {otherwise}.")
            } else {
                format!("Copy into another organization ({choose}).")
            };
            error
        }
        _ => error,
    }
}

fn owner_kind(value: &str) -> Option<&'static str> {
    match value {
        "personal" | "user" | "account" => Some("personal"),
        "organization" | "org" => Some("organization"),
        _ => None,
    }
}

/// Adds `owner_kind` and `copied_from` to a projected program record. The
/// service's `owner_kind` (or `ownerKind`) is kept when it is a known value;
/// `copied_from` is `{owner, slug, rev_id?}` or the reference text
/// `OWNER/SLUG[@rev_id]`, projected with its `ref`.
pub fn project_workspace(
    record: &Map<String, Value>,
    out: &mut Map<String, Value>,
) -> Result<(), RunnerError> {
    let kind = record
        .get("owner_kind")
        .filter(|value| !value.is_null())
        .or_else(|| record.get("ownerKind"));
    if let Some(kind) = kind.and_then(Value::as_str).and_then(owner_kind) {
        out.insert("owner_kind".to_owned(), json!(kind));
    }
    let source = record
        .get("copied_from")
        .filter(|value| !value.is_null())
        .or_else(|| record.get("copiedFrom"));
    // Informational: a malformed source is dropped, never fatal to the
    // record.
    if let Some(source) = source.and_then(copied_from) {
        out.insert("copied_from".to_owned(), source);
    }
    Ok(())
}

fn copied_from(source: &Value) -> Option<Value> {
    let (owner, slug, rev): (Option<&str>, Option<&str>, Option<Value>) = match source {
        Value::String(text) => {
            let (name, rev) = text
                .split_once('@')
                .map_or((text.as_str(), None), |(name, rev)| (name, Some(rev)));
            let (owner, slug) = name.split_once('/')?;
            (Some(owner), Some(slug), rev.map(|rev| json!(rev)))
        }
        Value::Object(object) => (
            object.get("owner").and_then(Value::as_str),
            object.get("slug").and_then(Value::as_str),
            object
                .get("rev_id")
                .filter(|value| !value.is_null())
                .cloned(),
        ),
        _ => return None,
    };
    let owner = owner.filter(|owner| valid_owner(owner))?;
    let slug = slug.filter(|slug| valid_slug(slug))?;
    let mut out = Map::new();
    out.insert("owner".to_owned(), json!(owner));
    out.insert("slug".to_owned(), json!(slug));
    if let Some(rev) = rev {
        let rev = rev.as_str().filter(|rev| valid_rev(rev))?;
        out.insert("rev_id".to_owned(), json!(rev));
        out.insert("ref".to_owned(), json!(ref_of(owner, slug, rev)));
    }
    Some(Value::Object(out))
}

/// `owner_kind=KIND  copied_from=REF` for a human listing line; empty when
/// the record has neither.
pub fn workspace_columns(program: &Value) -> String {
    let mut text = String::new();
    if let Some(kind) = program["owner_kind"].as_str() {
        text.push_str("  owner_kind=");
        text.push_str(kind);
    }
    if program["copied_from"].is_object() {
        text.push_str("  copied_from=");
        text.push_str(&copied_text(&program["copied_from"]));
    }
    text
}

/// `OWNER/SLUG@rev_id` of a `copied_from`, or `OWNER/SLUG` when the service
/// named no revision.
pub fn copied_text(source: &Value) -> String {
    source["ref"].as_str().map_or_else(
        || {
            format!(
                "{}/{}",
                source["owner"].as_str().unwrap_or_default(),
                source["slug"].as_str().unwrap_or_default()
            )
        },
        str::to_owned,
    )
}

/// Human `program show` notes (stderr): who owns the namespace and where the
/// program was copied from.
pub fn workspace_notes(program: &Value) -> String {
    let mut text = String::new();
    if let Some(kind) = program["owner_kind"].as_str() {
        let _ = writeln!(
            text,
            "Owner: {} ({kind})",
            program["owner"].as_str().unwrap_or_default()
        );
    }
    if program["copied_from"].is_object() {
        let _ = writeln!(
            text,
            "Copied from: {}",
            copied_text(&program["copied_from"])
        );
    }
    text
}

#[cfg(test)]
mod tests {
    use super::{copy_path, project_workspace, valid_namespace};
    use serde_json::{Map, Value, json};

    fn projected(record: &Value) -> Result<Value, crate::RunnerError> {
        let mut out = Map::new();
        project_workspace(record.as_object().expect("object"), &mut out)?;
        Ok(Value::Object(out))
    }

    #[test]
    fn owner_kind_and_copied_from_are_projected_from_either_shape() {
        assert_eq!(
            projected(&json!({"owner_kind": "org", "copied_from": "alice/hello@fedcba9876543210"}))
                .expect("projects"),
            json!({"owner_kind": "organization", "copied_from": {"owner": "alice", "slug": "hello", "rev_id": "fedcba9876543210", "ref": "alice/hello@fedcba9876543210"}})
        );
        assert_eq!(
            projected(&json!({"ownerKind": "user", "copiedFrom": {"owner": "alice", "slug": "hello", "rev_id": null}}))
                .expect("projects"),
            json!({"owner_kind": "personal", "copied_from": {"owner": "alice", "slug": "hello"}})
        );
        assert_eq!(
            projected(&json!({"owner_kind": "team"})).expect("projects"),
            json!({})
        );
        for bad in [
            json!("hello"),
            json!("alice/Hello"),
            json!("alice/hello@1"),
            json!(7),
            json!({"owner": "alice"}),
        ] {
            assert_eq!(
                projected(&json!({ "copied_from": bad })).expect("projects"),
                json!({})
            );
        }
    }

    #[test]
    fn namespaces_and_copy_routes() {
        assert!(valid_namespace("acme") && valid_namespace("a-1"));
        for bad in ["Acme", "-acme", "acme-", "", &"a".repeat(40)] {
            assert!(!valid_namespace(bad), "{bad}");
        }
        assert_eq!(copy_path("alice", "hello", None), "/p/alice/hello/copy");
        assert_eq!(
            copy_path("alice", "hello", Some("0123456789abcdef")),
            "/p/alice/hello@0123456789abcdef/copy"
        );
    }
}
