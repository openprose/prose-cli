//! Service organization management: `org show|create|rename|default`,
//! `org member list|role|remove`, `org invite` and
//! `org invitation revoke|accept`. There is no `org delete` (the service always
//! refuses it). `org list` stays the frozen service operation in
//! `service_account.rs`.
//!
//! Every mutation is confirm-class (catalog agent policy `never`): the handler
//! builds the exact request, passes the plan through the confirmation gate and
//! only then sends it. Inputs use the shared text validator; responses are
//! projected onto the closed `service/organizations.schema.json` shapes, and
//! anything else is `SERVICE_PROTOCOL_INVALID`. Mirrors
//! `cli/bun/src/core/service/organizations.ts` byte for byte.
use super::render::{self, valid_text};
use super::{Context, Gate, fs, http};
use crate::RunnerError;
use crate::error::{ErrorCode, human_safe_scalar};
use serde_json::{Map, Value, json};
use std::fmt::Write as _;

/// Organization references and slugs (code points).
const MAX_REF: usize = 256;
/// Account ids and invitation ids: the closed result schema caps both at 128,
/// so a longer value is refused before anything is sent (otherwise the service
/// would apply the mutation and the CLI could not project its echo).
const MAX_ID: usize = 128;
/// Display names (the service allows 200).
const MAX_NAME: usize = 200;
/// The invitation token file (bytes) and the token itself (code points).
const MAX_TOKEN_FILE: u64 = 4096;
const MAX_TOKEN: usize = 512;
/// Invitation lifetime bounds in seconds (the service's own bounds).
const MAX_EXPIRES_IN: u64 = 2_592_000;
/// Largest integer both products represent exactly.
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;
const ROLES: [&str; 3] = ["admin", "developer", "reader"];

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

fn protocol() -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid)
}

/// A required positional argument that passes the shared text validator.
fn text_argument(context: &Context<'_>, name: &str, max: usize) -> Result<String, RunnerError> {
    let value = context.argument(name).unwrap_or_default();
    if valid_text(value, max) {
        Ok(value.to_owned())
    } else {
        Err(invalid(format!(
            "<{name}> must be 1-{max} characters with no control characters"
        )))
    }
}

/// A positional argument that becomes one URL path segment. `.` and `..`
/// pass percent-encoding unchanged and URL normalization would drop them, so
/// the request would reach a different route from the one planned.
fn segment_argument(context: &Context<'_>, name: &str, max: usize) -> Result<String, RunnerError> {
    let value = text_argument(context, name, max)?;
    if value == "." || value == ".." {
        return Err(invalid(format!(
            "<{name}> cannot be `.` or `..`; a URL path segment would drop it"
        )));
    }
    Ok(value)
}

fn role_value(value: &str, label: &str) -> Result<String, RunnerError> {
    if ROLES.contains(&value) {
        Ok(value.to_owned())
    } else {
        Err(invalid(format!(
            "{label} must be admin, developer or reader"
        )))
    }
}

fn segment(value: &str) -> String {
    http::encode_segment(value)
}

/// The service resolves `default` only for `GET /organizations/default`.
const DEFAULT_HINT: &str = "ORG `default` is accepted only by `cli org show`; run `prose cli org show default` and pass the slug it prints";

/// A member or invitation route answered not-found for something other than
/// the organization (the service's `member_not_found` and
/// `invitation_not_found` codes are not on the serviceCode allowlist).
const MEMBER_HINT: &str = "ORG exists but has no such member; run `prose cli org member list ORG` and pass a member number it prints";
const INVITATION_HINT: &str = "ORG exists but has no invitation with this INVITATION_ID; `prose cli org invite` prints the invitation id";
/// A rejected `invitation accept` (the `invalid_invitation` code is dropped).
const ACCEPT_HINT: &str = "the invitation token is invalid, expired, revoked, already used, or was issued to a different account";

fn service_code(error: &RunnerError) -> Option<&str> {
    error
        .details
        .as_ref()
        .and_then(|details| details.get("serviceCode"))
        .and_then(Value::as_str)
}

/// Sends a request that addresses `org`. A not-found for the literal
/// `default` teaches the fix; on member and invitation routes, a not-found
/// that is not `organization_not_found` names the identifier that missed.
fn send_for(
    context: &mut Context<'_>,
    request: &http::Request,
    org: &str,
    missing: Option<&str>,
) -> Result<Map<String, Value>, RunnerError> {
    let response = context.send(request).map_err(|error| {
        if error.code != ErrorCode::ServiceResourceNotFound {
            return error;
        }
        if org == "default" {
            return error.with_detail("reason", context.localize(DEFAULT_HINT));
        }
        match missing {
            Some(hint) if service_code(&error) != Some("organization_not_found") => {
                error.with_detail("reason", context.localize(hint))
            }
            _ => error,
        }
    })?;
    response.json_object()
}

pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let id = context.operation["id"].as_str().unwrap_or_default();
    match id {
        "org.show" => show(context),
        "org.create" => create(context),
        "org.rename" => rename(context),
        "org.default" => set_default(context),
        "org.member.list" => member_list(context),
        "org.member.role" => member_role(context),
        "org.member.remove" => member_remove(context),
        "org.invite" => invite(context),
        "org.invitation.revoke" => invitation_revoke(context),
        "org.invitation.accept" => invitation_accept(context),
        _ => context.not_implemented(),
    }
}

/// Plans a mutation, passes the confirmation gate, then sends it. `Ok(Err)`
/// carries the `--preview` result. `target` names, for the human preview,
/// what the request acts on that its Summary does not show (the JSON plan
/// carries the path and the body digest); a secret body field is never
/// among them. `missing` is the not-found hint for member and invitation
/// routes.
fn confirm_and_send(
    context: &mut Context<'_>,
    org: &str,
    path: &str,
    body: Option<&Value>,
    target: &[(&str, String)],
    missing: Option<&str>,
) -> Result<Result<Map<String, Value>, Value>, RunnerError> {
    let mut request = http::Request::from_manifest(context.operation, 0, path);
    if let Some(body) = body {
        request = request.json_body(body);
    }
    let planned = context.planned(0, path, &[], request.body.as_deref());
    if let Gate::Preview(result) = context.gate(planned)? {
        context.human = Some(with_target(render::human_result(&result), target));
        return Ok(Err(result));
    }
    Ok(Ok(send_for(context, &request, org, missing)?))
}

/// The human preview with one `Label: value` line per target, right after
/// its `Preview:` line (identical in both ports).
fn with_target(text: String, target: &[(&str, String)]) -> String {
    let Some(end) = text.find('\n') else {
        return text;
    };
    let mut out = text[..=end].to_owned();
    for (label, value) in target {
        let _ = writeln!(out, "{label}: {}", human_safe_scalar(value));
    }
    out.push_str(&text[end + 1..]);
    out
}

/// The organization `cli org show` and `cli org member list` read without
/// ORG: the caller's default.
const DEFAULT_ORG: &str = "default";

fn show(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = if context.argument("ORG").is_none() {
        DEFAULT_ORG.to_owned()
    } else {
        segment_argument(context, "ORG", MAX_REF)?
    };
    let request = http::Request::from_manifest(
        context.operation,
        0,
        format!("/organizations/{}", segment(&org)),
    );
    let body = context.send(&request)?.json_object()?;
    let result = organization_result(&body)?;
    context.human = Some(human_organization("Organization", &result["organization"]));
    Ok(result)
}

/// Organization slugs the service reserves (`validateOrganizationSlug`).
const RESERVED_SLUGS: [&str; 4] = ["openprose", "system", "default", "invitations"];

/// The service's organization slug rule: 1-63 lowercase letters, digits or
/// interior hyphens, not reserved and not UUID-shaped.
fn slug_problem(slug: &str) -> Option<&'static str> {
    let bytes = slug.as_bytes();
    let shape = (1..=63).contains(&bytes.len())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
        && bytes.first() != Some(&b'-')
        && bytes.last() != Some(&b'-');
    if !shape {
        return Some("must be 1-63 lowercase letters, digits or interior hyphens");
    }
    let uuid = bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                *byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)
            }
        });
    (RESERVED_SLUGS.contains(&slug) || uuid).then_some("is reserved")
}

/// A slug that satisfies the shape rule, derived from `slug`: lowercased,
/// every other run of characters one hyphen, trimmed to 63.
fn suggested_slug(slug: &str) -> Option<String> {
    let mut out = String::new();
    for character in slug.to_lowercase().chars() {
        if character.is_ascii_lowercase() || character.is_ascii_digit() {
            out.push(character);
        } else if !out.ends_with('-') && !out.is_empty() {
            out.push('-');
        }
    }
    let mut out = out.chars().take(63).collect::<String>();
    while out.ends_with('-') {
        out.pop();
    }
    (slug_problem(&out).is_none() && out != slug).then_some(out)
}

fn create(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let slug = text_argument(context, "SLUG", MAX_REF)?;
    if let Some(problem) = slug_problem(&slug) {
        let mut error = invalid(format!(
            "organization slug {slug_quoted} {problem}; slugs are global and permanent",
            slug_quoted = crate::error::quote(&slug)
        ));
        if let Some(better) = suggested_slug(&slug) {
            // The positional SLUG: the first token after `cli` equal to it
            // that is not the value of --name.
            let mut argv = context.invocation.argv.clone();
            let start = argv.iter().position(|word| word == "cli").unwrap_or(0);
            if let Some(at) = (start..argv.len())
                .find(|&at| argv[at] == slug && (at == 0 || argv[at - 1] != "--name"))
            {
                argv[at].clone_from(&better);
            }
            error.action = format!(
                "Choose a slug that fits the rule, for example `{}`.",
                render::argv_text(&argv)
            );
            error = error.with_detail("suggestedArgv", json!(argv));
        }
        return Err(error);
    }
    let mut body = Map::new();
    if let Some(name) = context.option("--name") {
        if !valid_text(name, MAX_NAME) {
            return Err(invalid(format!(
                "--name must be 1-{MAX_NAME} characters with no control characters"
            )));
        }
        body.insert("name".into(), json!(name));
    }
    body.insert("slug".into(), json!(slug));
    let body = Value::Object(body);
    match confirm_and_send(context, "", "/organizations", Some(&body), &[], None)? {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let result = organization_result(&body)?;
            context.human = Some(human_organization(
                "Created organization",
                &result["organization"],
            ));
            Ok(result)
        }
    }
}

/// The new display name: NAME, or `--name NAME` as `org create` spells it
///.
fn rename_name(context: &Context<'_>) -> Result<String, RunnerError> {
    let positional = context.argument("NAME");
    let option = context.option("--name");
    if let (Some(positional), Some(option)) = (positional, option) {
        if positional != option {
            return Err(invalid(
                "give the new display name once, as NAME or as --name NAME",
            ));
        }
    }
    let Some(value) = positional.or(option) else {
        return Err(invalid(
            "missing the new display name: give NAME or --name NAME",
        ));
    };
    if valid_text(value, MAX_NAME) {
        return Ok(value.to_owned());
    }
    Err(invalid(if positional.is_some() {
        format!("<NAME> must be 1-{MAX_NAME} characters with no control characters")
    } else {
        format!("--name must be 1-{MAX_NAME} characters with no control characters")
    }))
}

fn rename(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = segment_argument(context, "ORG", MAX_REF)?;
    let name = rename_name(context)?;
    let path = format!("/organizations/{}", segment(&org));
    let body = json!({"name": name});
    let target = [("Organization", org.clone())];
    match confirm_and_send(context, &org, &path, Some(&body), &target, None)? {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let result = organization_result(&body)?;
            context.human = Some(human_organization(
                "Renamed organization",
                &result["organization"],
            ));
            Ok(result)
        }
    }
}

fn set_default(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = text_argument(context, "ORG", MAX_REF)?;
    let body = json!({"organization": org});
    let target = [("Organization", org.clone())];
    match confirm_and_send(
        context,
        &org,
        "/organizations/default",
        Some(&body),
        &target,
        None,
    )? {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let result = organization_result(&body)?;
            context.human = Some(human_organization(
                "Default organization",
                &result["organization"],
            ));
            Ok(result)
        }
    }
}

/// ORG, or without it the caller's default organization's slug: the service
/// resolves `default` only for GET /organizations/default (the operation's
/// request `index`), so that is read first (mirrors Bun `orgOrDefault`).
fn org_or_default(context: &mut Context<'_>, index: usize) -> Result<String, RunnerError> {
    if context.argument("ORG").is_some() {
        return segment_argument(context, "ORG", MAX_REF);
    }
    let request = http::Request::from_manifest(
        context.operation,
        index,
        format!("/organizations/{DEFAULT_ORG}"),
    );
    let body = context.send(&request)?.json_object()?;
    let result = organization_result(&body)?;
    Ok(result["organization"]["slug"]
        .as_str()
        .unwrap_or_default()
        .to_owned())
}

/// An organization's members, numbered: sorted by when they joined (then by
/// account), each `{member: "member N", handle?, role, created_at}` with
/// its account id kept alongside for resolving a member number (identical
/// in both ports). The account id itself is never printed.
fn numbered_members(
    context: &mut Context<'_>,
    org: &str,
    index: usize,
) -> Result<Vec<(String, Value)>, RunnerError> {
    let path = format!("/organizations/{}/members", segment(org));
    let request = http::Request::from_manifest(context.operation, index, path);
    let body = send_for(context, &request, org, None)?;
    let entries = body
        .get("members")
        .and_then(Value::as_array)
        .filter(|entries| entries.len() <= 10_000)
        .ok_or_else(|| invalid_field("members"))?;
    let mut members = entries
        .iter()
        .map(|entry| {
            let object = object_at(Some(entry), "members[]")?;
            let account = text_field(object, "members[].", "account_id", 128)?
                .as_str()
                .unwrap_or_default()
                .to_owned();
            Ok((account, member(Some(entry), "members[]")?))
        })
        .collect::<Result<Vec<_>, RunnerError>>()?;
    members.sort_by(|(left_account, left), (right_account, right)| {
        left["created_at"]
            .as_i64()
            .cmp(&right["created_at"].as_i64())
            .then_with(|| left_account.as_bytes().cmp(right_account.as_bytes()))
    });
    for (number, (_, entry)) in members.iter_mut().enumerate() {
        let mut numbered = Map::new();
        numbered.insert("member".into(), json!(format!("member {}", number + 1)));
        if let Value::Object(fields) = entry {
            numbered.append(fields);
        }
        *entry = Value::Object(numbered);
    }
    Ok(members)
}

/// A `MEMBER` argument: a member number as `cli org member list` prints it
/// (`N` or `member N`), or the member's account id.
enum MemberArgument {
    Number(usize),
    Account(String),
}

fn member_argument(context: &Context<'_>) -> Result<MemberArgument, RunnerError> {
    let value = segment_argument(context, "MEMBER", MAX_ID)?;
    let lower = value.to_ascii_lowercase();
    let digits = lower.strip_prefix("member").map_or(lower.as_str(), |rest| {
        rest.strip_prefix([' ', '-']).unwrap_or(rest)
    });
    let number = (!digits.is_empty()
        && digits.len() <= 5
        && !digits.starts_with('0')
        && digits.bytes().all(|byte| byte.is_ascii_digit()))
    .then(|| digits.parse::<usize>().ok())
    .flatten();
    Ok(number.map_or(MemberArgument::Account(value), MemberArgument::Number))
}

/// The account id and label (`member N`, or the account id the caller named)
/// of a `MEMBER` argument; a member number reads the member list (manifest
/// request 1).
fn resolve_member(context: &mut Context<'_>, org: &str) -> Result<(String, String), RunnerError> {
    match member_argument(context)? {
        MemberArgument::Account(account) => Ok((account.clone(), account)),
        MemberArgument::Number(number) => {
            let members = numbered_members(context, org, 1)?;
            let count = members.len();
            let Some((account, _)) = members.into_iter().nth(number - 1) else {
                let list = context.command(&format!("org member list {org}"));
                let suggested = context.follow_up_argv(&["org", "member", "list", org]);
                let mut error = invalid(format!(
                    "member {number} is not in {}'s member list ({count} member{}); `{list}` numbers them",
                    human_safe_scalar(org),
                    if count == 1 { "" } else { "s" },
                ));
                error.action =
                    format!("List the members with `{list}` and pass one of their numbers.");
                return Err(error.with_detail("suggestedArgv", json!(suggested)));
            };
            Ok((account, format!("member {number}")))
        }
    }
}

/// The human name of a member label: `member N`, or `member ACCOUNT`.
fn member_name(label: &str) -> String {
    if label.starts_with("member ") {
        label.to_owned()
    } else {
        format!("member {}", human_safe_scalar(label))
    }
}

fn member_list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = org_or_default(context, 1)?;
    let members = numbered_members(context, &org, 0)?
        .into_iter()
        .map(|(_, entry)| entry)
        .collect::<Vec<_>>();
    let mut text = if members.is_empty() {
        format!("No members in {}.\n", human_safe_scalar(&org))
    } else {
        format!(
            "Members of {} ({}):\n",
            human_safe_scalar(&org),
            members.len()
        )
    };
    for entry in &members {
        let handle = entry["handle"]
            .as_str()
            .map(|handle| format!(" ({})", human_safe_scalar(handle)))
            .unwrap_or_default();
        let _ = writeln!(
            text,
            "{}{handle}  {}",
            entry["member"].as_str().unwrap_or_default(),
            entry["role"].as_str().unwrap_or_default()
        );
    }
    context.human = Some(text);
    Ok(json!({"members": members}))
}

fn member_role(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = segment_argument(context, "ORG", MAX_REF)?;
    member_argument(context)?;
    let role = role_value(context.argument("ROLE").unwrap_or_default(), "<ROLE>")?;
    let (account, label) = resolve_member(context, &org)?;
    let path = format!(
        "/organizations/{}/members/{}",
        segment(&org),
        segment(&account)
    );
    let body = json!({"role": role});
    let target = [("Organization", org.clone()), ("Member", label.clone())];
    match confirm_and_send(
        context,
        &org,
        &path,
        Some(&body),
        &target,
        Some(MEMBER_HINT),
    )? {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let mut member = member(body.get("member"), "member")?;
            member["member"] = json!(label);
            let name = member_name(&label);
            let mut chars = name.chars();
            let name = chars.next().map_or_else(String::new, |first| {
                first.to_ascii_uppercase().to_string() + chars.as_str()
            });
            context.human = Some(format!(
                "{name} is now {}.\n",
                member["role"].as_str().unwrap_or_default()
            ));
            Ok(json!({"member": member}))
        }
    }
}

fn member_remove(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = segment_argument(context, "ORG", MAX_REF)?;
    let (account, label) = resolve_member(context, &org)?;
    let path = format!(
        "/organizations/{}/members/{}",
        segment(&org),
        segment(&account)
    );
    let target = [("Organization", org.clone()), ("Member", label.clone())];
    match confirm_and_send(context, &org, &path, None, &target, Some(MEMBER_HINT))? {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let result = ok(&body)?;
            context.human = Some(format!(
                "Removed {} from {}.\n",
                member_name(&label),
                human_safe_scalar(&org)
            ));
            Ok(result)
        }
    }
}

fn invite(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = segment_argument(context, "ORG", MAX_REF)?;
    let account = text_argument(context, "ACCOUNT_ID", MAX_ID)?;
    let role = role_value(context.option("--role").unwrap_or_default(), "--role")?;
    let mut body = Map::new();
    let mut target = vec![("Organization", org.clone()), ("Account", account.clone())];
    body.insert("accountId".into(), json!(account));
    if let Some(value) = context.option("--expires-in") {
        let seconds = expires_in(value)?;
        body.insert("expiresInSeconds".into(), json!(seconds));
        target.push((
            "Expires in",
            render::duration_text(i64::try_from(seconds).unwrap_or(i64::MAX)),
        ));
    }
    body.insert("role".into(), json!(role));
    let path = format!("/organizations/{}/invitations", segment(&org));
    let body = Value::Object(body);
    match confirm_and_send(context, &org, &path, Some(&body), &target, None)? {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let result = invitation(&body)?;
            let entry = &result["invitation"];
            let text = format!(
                "Invitation {} for {} as {} (expires {}).\nToken: {}\nThe token appears only here. The invitee accepts with `prose cli org invitation accept --token-file FILE`.\n",
                human_safe_scalar(entry["id"].as_str().unwrap_or_default()),
                human_safe_scalar(entry["account_id"].as_str().unwrap_or_default()),
                entry["role"].as_str().unwrap_or_default(),
                super::render::iso_ms(&entry["expires_at"])
                    .unwrap_or_else(|| entry["expires_at"].to_string()),
                human_safe_scalar(result["token"].as_str().unwrap_or_default()),
            );
            context.human = Some(context.localize(&text));
            Ok(result)
        }
    }
}

/// `--expires-in`: decimal digits only, 1 to 2592000 seconds.
fn expires_in(value: &str) -> Result<u64, RunnerError> {
    value
        .bytes()
        .all(|byte| byte.is_ascii_digit())
        .then(|| value.parse::<u64>().ok())
        .flatten()
        .filter(|seconds| (1..=MAX_EXPIRES_IN).contains(seconds))
        .ok_or_else(|| {
            invalid(format!(
                "--expires-in must be a whole number of seconds from 1 to {MAX_EXPIRES_IN}"
            ))
        })
}

fn invitation_revoke(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let org = segment_argument(context, "ORG", MAX_REF)?;
    let id = segment_argument(context, "INVITATION_ID", MAX_ID)?;
    let path = format!(
        "/organizations/{}/invitations/{}",
        segment(&org),
        segment(&id)
    );
    let target = [("Organization", org.clone()), ("Invitation", id.clone())];
    match confirm_and_send(context, &org, &path, None, &target, Some(INVITATION_HINT))? {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let result = ok(&body)?;
            context.human = Some(format!(
                "Revoked invitation {} in {}.\n",
                human_safe_scalar(&id),
                human_safe_scalar(&org)
            ));
            Ok(result)
        }
    }
}

fn invitation_accept(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let source = context
        .option("--token-file")
        .unwrap_or_default()
        .to_owned();
    let text = fs::read_text(
        &context.system.current_dir,
        &source,
        MAX_TOKEN_FILE,
        "--token-file",
    )?;
    let token = text
        .strip_suffix("\r\n")
        .or_else(|| text.strip_suffix('\n'))
        .unwrap_or(&text);
    if !valid_text(token, MAX_TOKEN) {
        return Err(invalid(format!(
            "--token-file must hold one invitation token (1-{MAX_TOKEN} characters, no control characters) on a single line"
        )));
    }
    let body = json!({"token": token});
    let sent = confirm_and_send(
        context,
        "",
        "/organizations/invitations/accept",
        Some(&body),
        &[],
        None,
    )
    .map_err(|error| {
        let rejected = error.code == ErrorCode::ServiceRequestRejected
            && matches!(service_code(&error), None | Some("invalid_invitation"));
        if rejected {
            error.with_detail("reason", ACCEPT_HINT)
        } else {
            error
        }
    })?;
    match sent {
        Err(preview) => Ok(preview),
        Ok(body) => {
            let result = organization_result(&body)?;
            context.human = Some(human_organization(
                "Joined organization",
                &result["organization"],
            ));
            Ok(result)
        }
    }
}

// ---- response projections (closed service/organizations.schema.json) ----

/// `SERVICE_PROTOCOL_INVALID` naming the response field that failed.
fn invalid_field(field: &str) -> RunnerError {
    protocol().with_detail(
        "reason",
        format!("the service response has a missing or invalid {field}"),
    )
}

fn field<'v>(
    object: &'v Map<String, Value>,
    prefix: &str,
    key: &str,
) -> (Option<&'v Value>, String) {
    (object.get(key), format!("{prefix}{key}"))
}

fn text_field(
    object: &Map<String, Value>,
    prefix: &str,
    key: &str,
    max: usize,
) -> Result<Value, RunnerError> {
    let (value, name) = field(object, prefix, key);
    value
        .and_then(Value::as_str)
        .filter(|value| valid_text(value, max))
        .map(|value| json!(value))
        .ok_or_else(|| invalid_field(&name))
}

fn uuid_field(object: &Map<String, Value>, prefix: &str, key: &str) -> Result<Value, RunnerError> {
    let (value, name) = field(object, prefix, key);
    value
        .and_then(Value::as_str)
        .filter(|value| {
            let bytes = value.as_bytes();
            bytes.len() == 36
                && bytes.iter().enumerate().all(|(index, byte)| {
                    if matches!(index, 8 | 13 | 18 | 23) {
                        *byte == b'-'
                    } else {
                        byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)
                    }
                })
        })
        .map(|value| json!(value))
        .ok_or_else(|| invalid_field(&name))
}

fn slug_field(object: &Map<String, Value>, prefix: &str, key: &str) -> Result<Value, RunnerError> {
    let (value, name) = field(object, prefix, key);
    value
        .and_then(Value::as_str)
        .filter(|value| {
            let bytes = value.as_bytes();
            (1..=64).contains(&bytes.len())
                && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
                && bytes
                    .iter()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
        })
        .map(|value| json!(value))
        .ok_or_else(|| invalid_field(&name))
}

/// A non-negative safe integer. JSON `1.0` counts as 1, as it does for the Bun
/// product's `JSON.parse`.
#[allow(
    clippy::cast_precision_loss,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss
)]
fn epoch_value(value: &Value) -> Option<Value> {
    let safe = MAX_SAFE_INTEGER as f64;
    value
        .as_u64()
        .filter(|number| *number <= MAX_SAFE_INTEGER)
        .or_else(|| {
            value
                .as_f64()
                .filter(|number| number.fract() == 0.0 && *number >= 0.0 && *number <= safe)
                .map(|number| number as u64)
        })
        .map(|number| json!(number))
}

fn epoch_field(object: &Map<String, Value>, prefix: &str, key: &str) -> Result<Value, RunnerError> {
    let (value, name) = field(object, prefix, key);
    value
        .and_then(epoch_value)
        .ok_or_else(|| invalid_field(&name))
}

fn nullable_epoch(
    object: &Map<String, Value>,
    prefix: &str,
    key: &str,
) -> Result<Value, RunnerError> {
    match object.get(key) {
        None | Some(Value::Null) => Ok(Value::Null),
        Some(_) => epoch_field(object, prefix, key),
    }
}

fn role_field(object: &Map<String, Value>, prefix: &str, key: &str) -> Result<Value, RunnerError> {
    let (value, name) = field(object, prefix, key);
    value
        .and_then(Value::as_str)
        .filter(|role| ROLES.contains(role))
        .map(|role| json!(role))
        .ok_or_else(|| invalid_field(&name))
}

fn object_at<'v>(
    value: Option<&'v Value>,
    name: &str,
) -> Result<&'v Map<String, Value>, RunnerError> {
    value
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_field(name))
}

pub(super) fn organization(value: Option<&Value>) -> Result<Value, RunnerError> {
    let object = object_at(value, "organization")?;
    let prefix = "organization.";
    let mut output = Map::new();
    if object.contains_key("created_at") {
        output.insert(
            "created_at".into(),
            epoch_field(object, prefix, "created_at")?,
        );
    }
    output.insert("id".into(), uuid_field(object, prefix, "id")?);
    output.insert("name".into(), text_field(object, prefix, "name", MAX_REF)?);
    if object.contains_key("role") {
        output.insert("role".into(), role_field(object, prefix, "role")?);
    }
    output.insert("slug".into(), slug_field(object, prefix, "slug")?);
    super::render::add_iso(&mut output, &["created_at"]);
    Ok(Value::Object(output))
}

fn organization_result(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    Ok(json!({"organization": organization(body.get("organization"))?}))
}

/// One member's public fields; `name` is `member` or `members[]`. The
/// account id is checked but never printed, and the organization id is
/// the caller's own `ORG`; a `handle` is kept when the service sends one.
fn member(value: Option<&Value>, name: &str) -> Result<Value, RunnerError> {
    let object = object_at(value, name)?;
    let prefix = format!("{name}.");
    text_field(object, &prefix, "account_id", 128)?;
    let mut projected = json!({
        "created_at": epoch_field(object, &prefix, "created_at")?,
        "role": role_field(object, &prefix, "role")?,
    });
    if let Some(handle) = object
        .get("handle")
        .and_then(Value::as_str)
        .filter(|handle| valid_text(handle, 64))
    {
        projected["handle"] = json!(handle);
    }
    if let Value::Object(map) = &mut projected {
        super::render::add_iso(map, &["created_at"]);
    }
    Ok(projected)
}

fn invitation(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    let object = object_at(body.get("invitation"), "invitation")?;
    let prefix = "invitation.";
    let mut projected = json!({
        "account_id": text_field(object, prefix, "account_id", 128)?,
        "consumed_at": nullable_epoch(object, prefix, "consumed_at")?,
        "created_at": epoch_field(object, prefix, "created_at")?,
        "expires_at": epoch_field(object, prefix, "expires_at")?,
        "id": text_field(object, prefix, "id", 128)?,
        "revoked_at": nullable_epoch(object, prefix, "revoked_at")?,
        "role": role_field(object, prefix, "role")?,
    });
    if let Value::Object(map) = &mut projected {
        super::render::add_iso(
            map,
            &["consumed_at", "created_at", "expires_at", "revoked_at"],
        );
    }
    let token = text_field(body, "", "token", MAX_TOKEN)?;
    Ok(json!({"invitation": projected, "token": token}))
}

fn ok(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    if body.get("ok") == Some(&Value::Bool(true)) {
        Ok(json!({"ok": true}))
    } else {
        Err(invalid_field("ok"))
    }
}

fn human_organization(heading: &str, organization: &Value) -> String {
    let mut text = format!(
        "{heading}: {}\nName: {}\nID: {}\n",
        human_safe_scalar(organization["slug"].as_str().unwrap_or_default()),
        human_safe_scalar(organization["name"].as_str().unwrap_or_default()),
        organization["id"].as_str().unwrap_or_default(),
    );
    if let Some(role) = organization["role"].as_str() {
        let _ = writeln!(text, "Your role: {role}");
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn organization_projection_drops_unknown_fields_and_checks_shapes() {
        let value = json!({
            "id": "5b0e7c1a-3f2d-4e8b-9a61-2c4d8e0f1a23",
            "slug": "acme-research",
            "name": "Acme Research",
            "created_by": "cus_x",
            "created_at": 1,
            "role": "admin"
        });
        let projected = organization(Some(&value)).unwrap();
        assert!(projected.get("created_by").is_none());
        assert_eq!(projected["role"], "admin");
        for (key, bad) in [
            ("id", json!("5B0E7C1A-3F2D-4E8B-9A61-2C4D8E0F1A23")),
            ("slug", json!("Bad")),
            ("name", json!("tab\there")),
            ("role", json!("owner")),
            ("created_at", json!(-1)),
            ("created_at", json!(9_007_199_254_740_992_u64)),
        ] {
            let mut broken = value.clone();
            broken[key] = bad;
            let error = organization(Some(&broken)).unwrap_err();
            assert_eq!(
                error.details.unwrap()["reason"],
                format!("the service response has a missing or invalid organization.{key}")
            );
        }
    }

    #[test]
    fn expires_in_is_bounded_digits() {
        assert_eq!(expires_in("60").unwrap(), 60);
        assert_eq!(expires_in("2592000").unwrap(), 2_592_000);
        for bad in [
            "0",
            "2592001",
            "-1",
            "+5",
            "1.5",
            "",
            " 5",
            "99999999999999999999",
        ] {
            assert!(expires_in(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn invitation_projection_requires_token_and_nullable_times() {
        let body = json!({
            "invitation": {
                "id": "inv-1",
                "organization_id": "5b0e7c1a-3f2d-4e8b-9a61-2c4d8e0f1a23",
                "account_id": "cus_invitee",
                "role": "reader",
                "created_by": "cus_admin",
                "created_at": 1,
                "expires_at": 2
            },
            "token": "t"
        });
        let projected = invitation(body.as_object().unwrap()).unwrap();
        assert_eq!(projected["invitation"]["consumed_at"], Value::Null);
        assert!(projected["invitation"].get("created_by").is_none());
        let mut missing = body.clone();
        missing.as_object_mut().unwrap().remove("token");
        assert!(invitation(missing.as_object().unwrap()).is_err());
    }
}
